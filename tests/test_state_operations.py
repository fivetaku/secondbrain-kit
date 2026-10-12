"""V3 — 후보·검증·채택(sb_state) 계약 시험. 모든 쓰기는 임시 DB에서만 일어난다."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / 'bin'
sys.path.insert(0, str(BIN))
import sb_state  # noqa: E402

PY = sys.executable


class StateOpsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sb-state-ops-'))
        self.db = self.tmp / 'state.db'
        self.env_patch = {'SB_HOME': str(self.tmp / 'sbhome'), 'SB_STATE_DB': str(self.db), 'SB_STATE_PILOT_SCOPES': 'proj-a,proj-b,a:b,a',
                          'SB_VERIFY_ALLOWED_ROOTS': str(self.tmp), 'SB_PROJECT_ALIASES': str(self.tmp / 'aliases.json'),
                          'SB_CLAUDE_MEM_DB': str(self.tmp / 'claude-mem.db'), 'PYTHONDONTWRITEBYTECODE': '1'}
        self._old = {k: os.environ.get(k) for k in self.env_patch}
        os.environ.update(self.env_patch)
        os.environ.pop('SB_STATE_FAIL_BEFORE_INSERT', None)
        sb_state.migrate(str(self.db))
        (self.tmp / 'aliases.json').write_text(json.dumps({'proj-a': ['/nonexistent/proj-a', 'legacy-a']}), encoding='utf-8')
        self.evidence = self.tmp / 'deploy.txt'
        self.evidence.write_text('deploy target: staging-2\nport: 8443\n', encoding='utf-8')

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def req(self, **over):
        base = {'scope_id': 'proj-a', 'fact_key': 'deploy.target', 'body': 'staging-2', 'value_json': 'staging-2',
                'kind': 'measured_fact', 'source': 'cli', 'write_id': 'w1', 'expected_version': 0}
        base.update(over)
        return base

    def cli(self, *args, env_extra=None):
        env = {**os.environ, **(env_extra or {})}
        p = subprocess.run([PY, str(BIN / 'sb_state.py'), '--db', str(self.db), *args],
                           capture_output=True, text=True, encoding='utf-8', env=env, cwd=str(self.tmp))
        try:
            payload = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
        except ValueError:
            payload = {'raw': p.stdout}
        return p.returncode, payload, p.stderr

    def propose(self, **over):
        return sb_state.propose(self.req(**over))

    def confirm(self, cid, target=None):
        return sb_state.verify(cid, 'file_contains', target or str(self.evidence))

    def heads(self, scope='proj-a', fact_key='deploy.target'):
        with sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True) as db:
            return db.execute('SELECT id, version, body FROM state WHERE scope_id=? AND fact_key=? AND is_head=1',
                              (scope, fact_key)).fetchall()


class CandidateGateTests(StateOpsBase):
    def test_propose_never_changes_head_and_ignores_client_verified_flag(self):
        r = self.propose(verification='confirmed', verified=True)
        self.assertEqual(r['candidate_status'], 'proposed')
        self.assertEqual(self.heads(), [])
        with self.assertRaises(sb_state.StateError) as cm:
            sb_state.accept(r['candidate_id'], 0)
        self.assertEqual(cm.exception.code, 'verification_failed')
        self.assertEqual(self.heads(), [])

    def test_inference_is_never_accepted(self):
        r = self.propose(kind='inference')
        v = sb_state.verify(r['candidate_id'], 'file_contains', str(self.evidence))
        self.assertEqual(v['result'], 'rejected')
        with self.assertRaises(sb_state.StateError) as cm:
            sb_state.accept(r['candidate_id'], 0)
        self.assertEqual(cm.exception.code, 'verification_failed')
        self.assertEqual(self.heads(), [])

    def test_user_decision_requires_user_utterance_check_and_is_labelled_user_confirmed(self):
        record = self.tmp / 'chat.md'
        record.write_text('user: 배포 대상은 staging-2 로 확정한다\n', encoding='utf-8')
        r = self.propose(kind='user_decision', write_id='d1')
        wrong = sb_state.verify(r['candidate_id'], 'file_contains', str(record))
        self.assertEqual(wrong['result'], 'rejected')
        with self.assertRaises(sb_state.StateError):
            sb_state.accept(r['candidate_id'], 0)
        ok = sb_state.verify(r['candidate_id'], 'user_utterance_check', str(record) + '#1-1')
        self.assertEqual(ok['result'], 'confirmed')
        acc = sb_state.accept(r['candidate_id'], 0)
        self.assertEqual(acc['label'], 'user-confirmed')
        head = sb_state.query('head', 'proj-a', 'deploy.target')['items'][0]
        self.assertEqual((head['verification_status'], head['label']), ('verified', 'user-confirmed'))

    def test_cross_scope_allows_other_project_evidence_only_when_asked(self):
        import sqlite3
        con = sqlite3.connect(str(self.tmp / 'claude-mem.db'))
        con.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY, project TEXT, title TEXT, '
                    'narrative TEXT, text TEXT, facts TEXT)')
        con.execute("INSERT INTO observations VALUES (7, 'other-proj', '배포 대상은 staging-2', '', '', '')")
        con.commit()
        con.close()
        r = self.propose(write_id='x1')
        plain = sb_state.verify(r['candidate_id'], 'observation_ref', 'observation:7')
        self.assertEqual(plain['result'], 'rejected')
        r2 = self.propose(write_id='x2')
        crossed = sb_state.verify(r2['candidate_id'], 'observation_ref', 'observation:7', cross_scope=True)
        self.assertEqual(crossed['result'], 'confirmed')
        self.assertFalse(sb_state._CROSS_SCOPE['on'])   # 호출이 끝나면 꺼진다
        r3 = self.propose(write_id='x3')
        self.assertEqual(sb_state.verify(r3['candidate_id'], 'observation_ref', 'observation:7')['result'], 'rejected')

    def test_measured_fact_contradicted_evidence_blocks_accept(self):
        r = self.propose(body='prod-9', value_json='prod-9')
        v = self.confirm(r['candidate_id'])
        self.assertEqual(v['result'], 'contradicted')
        with self.assertRaises(sb_state.StateError) as cm:
            sb_state.accept(r['candidate_id'], 0)
        self.assertEqual(cm.exception.code, 'verification_failed')

    def test_target_outside_allowed_roots_and_unregistered_method_are_rejected(self):
        r = self.propose()
        with self.assertRaises(sb_state.StateError):
            sb_state.verify(r['candidate_id'], 'file_contains', '/etc/hosts')
        with self.assertRaises(sb_state.StateError):
            sb_state.verify(r['candidate_id'], 'shell', 'echo hi')
        with self.assertRaises(sb_state.StateError):
            sb_state.verify(r['candidate_id'], 'file_contains', 'relative/path.txt')

    def test_line_span_forms_and_hash_in_filename(self):
        record = self.tmp / 'notes#v2.md'
        record.write_text('header\nuser: 배포 대상은 staging-2 로 확정한다\nfooter\n', encoding='utf-8')
        r = self.propose(kind='user_decision', write_id='span')
        for target in (str(record) + '#L2-L2', str(record) + '#2-3', str(record) + '#L2', str(record)):
            with self.subTest(target=target):
                self.assertEqual(sb_state.verify(r['candidate_id'], 'user_utterance_check', target)['result'], 'confirmed')
        self.assertEqual(sb_state.verify(r['candidate_id'], 'user_utterance_check', str(record) + '#L1-L1')['result'],
                         'contradicted')
        with self.assertRaises(sb_state.StateError):
            sb_state.verify(r['candidate_id'], 'user_utterance_check', str(record) + '#L3-L1')

    def test_split_target_uses_last_hash_and_keeps_windows_drive(self):
        st = sb_state.split_target
        self.assertEqual(st('C:\\x\\y.md#L1-L3'), ('C:\\x\\y.md', 'L1-L3'))
        self.assertEqual(st('C:\\x\\a#b.md#1-3'), ('C:\\x\\a#b.md', '1-3'))
        self.assertEqual(st('C:\\x\\a#b.md'), ('C:\\x\\a#b.md', ''))
        self.assertEqual(st('/abs/f.json#/a/0'), ('/abs/f.json', '/a/0'))
        self.assertEqual(st('/abs/a#b/f.json#/k'), ('/abs/a#b/f.json', '/k'))
        self.assertEqual(st('/abs/plain.md'), ('/abs/plain.md', ''))

    def test_allowed_roots_split_on_os_pathsep_and_default_to_sb_home(self):
        other = Path(tempfile.mkdtemp(prefix='sb-state-ops-other-'))
        ev = other / 'ev.txt'; ev.write_text('staging-2\n', encoding='utf-8')
        r = self.propose()
        os.environ['SB_VERIFY_ALLOWED_ROOTS'] = os.pathsep.join([str(self.tmp), str(other)])
        self.assertEqual(sb_state.verify(r['candidate_id'], 'file_contains', str(ev))['result'], 'confirmed')
        # 기본값(env 없음): SB_HOME + 별칭 경로만 — 임시 루트 밖은 거부
        os.environ.pop('SB_VERIFY_ALLOWED_ROOTS')
        home = self.tmp / 'sbhome'; home.mkdir(exist_ok=True)
        inside = home / 'ev.txt'; inside.write_text('staging-2\n', encoding='utf-8')
        self.assertEqual(sb_state.verify(r['candidate_id'], 'file_contains', str(inside))['result'], 'confirmed')
        with self.assertRaises(sb_state.StateError):
            sb_state.verify(r['candidate_id'], 'file_contains', str(ev))
        (self.tmp / 'aliases.json').write_text(json.dumps({'proj-a': [str(other)]}), encoding='utf-8')
        self.assertEqual(sb_state.verify(r['candidate_id'], 'file_contains', str(ev))['result'], 'confirmed')

    def test_pilot_gate_default_is_all_scopes(self):
        os.environ.pop('SB_STATE_PILOT_SCOPES')
        r = self.propose(scope_id='any-scope', write_id='free'); self.confirm(r['candidate_id'])
        self.assertEqual(sb_state.accept(r['candidate_id'], 0)['version'], 1)
        os.environ['SB_STATE_PILOT_SCOPES'] = 'proj-a'
        r2 = self.propose(scope_id='blocked-scope', write_id='blocked'); self.confirm(r2['candidate_id'])
        with self.assertRaises(sb_state.StateError) as cm:
            sb_state.accept(r2['candidate_id'], 0)
        self.assertEqual(cm.exception.code, 'verification_failed')

    def test_operational_db_guard_uses_sb_home(self):
        operational = self.tmp / 'sbhome' / 'state.db'
        with self.assertRaises(sb_state.StateError):
            sb_state.migrate(str(operational))
        self.assertFalse(operational.exists())
        os.environ['SB_STATE_ALLOW_OPERATIONAL_MIGRATION'] = '1'
        try:
            self.assertIn('state_candidate', sb_state.migrate(str(operational), allow_operational=True)['created'])
        finally:
            os.environ.pop('SB_STATE_ALLOW_OPERATIONAL_MIGRATION')

    def test_observation_ref_checks_project_scope(self):
        cm_db = self.tmp / 'claude-mem.db'
        with sqlite3.connect(cm_db) as db:
            db.execute('CREATE TABLE observations(id INTEGER PRIMARY KEY, project TEXT, title TEXT, narrative TEXT, '
                       'text TEXT, facts TEXT)')
            db.execute("INSERT INTO observations VALUES (1,'other-proj','x','deploy target staging-2','','')")
            db.execute("INSERT INTO observations VALUES (2,'legacy-a','x','deploy target staging-2','','')")
        r = self.propose()
        bad = sb_state.verify(r['candidate_id'], 'observation_ref', 'observation:1')
        self.assertEqual(bad['result'], 'rejected')
        good = sb_state.verify(r['candidate_id'], 'observation_ref', 'observation:2')
        self.assertEqual(good['result'], 'confirmed')
        acc = sb_state.accept(r['candidate_id'], 0)
        self.assertEqual(acc['label'], 'operationally measured')


class IdempotencyTests(StateOpsBase):
    def test_same_key_same_payload_returns_existing_and_stores_once(self):
        a = self.propose(); b = self.propose()
        self.assertEqual((a['idempotent'], b['idempotent'], a['candidate_id']), (False, True, b['candidate_id']))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM state_candidate').fetchone()[0], 1)

    def test_same_key_different_payload_is_conflict(self):
        self.propose()
        with self.assertRaises(sb_state.StateError) as cm:
            self.propose(body='different')
        self.assertEqual(cm.exception.code, 'conflict')
        code, out, _ = self.cli('propose', '--file', self._write_req(body='different'))
        self.assertEqual((code, out['error']), (5, 'conflict'))

    def test_delimiter_collision_keeps_scopes_apart(self):
        a = self.propose(scope_id='a:b', source='c', write_id='w')
        b = self.propose(scope_id='a', source='b:c', write_id='w')
        self.assertNotEqual(a['logical_key'], b['logical_key'])
        self.assertFalse(b['idempotent'])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM state_candidate').fetchone()[0], 2)

    def test_transport_retry_via_cli_reuses_library_candidate(self):
        lib = self.propose()
        code, out, _ = self.cli('propose', '--file', self._write_req())
        self.assertEqual((code, out['idempotent'], out['candidate_id']), (0, True, lib['candidate_id']))

    def test_accept_twice_is_idempotent_and_does_not_add_versions(self):
        r = self.propose(); self.confirm(r['candidate_id'])
        first = sb_state.accept(r['candidate_id'], 0)
        second = sb_state.accept(r['candidate_id'], 0)
        self.assertEqual((first['version'], second['idempotent'], second['version']), (1, True, 1))
        self.confirm(r['candidate_id'])  # 동일 값 재검증은 verification 만 추가
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM state').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM state_verification').fetchone()[0], 2)

    def _write_req(self, **over):
        p = self.tmp / ('req-%d.json' % len(list(self.tmp.glob('req-*.json'))))
        p.write_text(json.dumps(self.req(**over)), encoding='utf-8')
        return str(p)


class VersionAndTimeTests(StateOpsBase):
    def test_stale_expected_version_is_rejected_with_exit_3(self):
        r = self.propose(); self.confirm(r['candidate_id']); sb_state.accept(r['candidate_id'], 0)
        r2 = self.propose(write_id='w2', body='staging-2', expected_version=None)
        self.confirm(r2['candidate_id'])
        code, out, _ = self.cli('accept', '--candidate', str(r2['candidate_id']), '--expected-version', '0')
        self.assertEqual((code, out['error'], out['head_version']), (3, 'stale_version', 1))
        self.assertEqual(len(self.heads()), 1)

    def test_observed_at_null_stays_null_and_bad_timestamps_rejected(self):
        r = self.propose(observed_at=None); self.confirm(r['candidate_id']); sb_state.accept(r['candidate_id'], 0)
        head = sb_state.query('head', 'proj-a')['items'][0]
        self.assertIsNone(head['observed_at']); self.assertIsNotNone(head['recorded_at'])
        for bad in ('2026-09-17T10:00:00', '2999-01-01T00:00:00+00:00', 'yesterday'):
            with self.assertRaises(sb_state.StateError):
                self.propose(write_id='x' + bad, observed_at=bad)

    def test_older_observation_does_not_replace_newer_head(self):
        r = self.propose(observed_at='2026-09-10T00:00:00+00:00'); self.confirm(r['candidate_id'])
        sb_state.accept(r['candidate_id'], 0)
        old = self.propose(write_id='late', observed_at='2026-09-01T00:00:00+00:00', expected_version=None)
        self.confirm(old['candidate_id'])
        with self.assertRaises(sb_state.StateError) as cm:
            sb_state.accept(old['candidate_id'], 1)
        self.assertEqual(cm.exception.extra.get('reason'), 'stale_observation')
        self.assertEqual(self.heads()[0][1], 1)


class AtomicityTests(StateOpsBase):
    def test_failure_injected_before_insert_rolls_everything_back(self):
        r = self.propose(); self.confirm(r['candidate_id']); sb_state.accept(r['candidate_id'], 0)
        r2 = self.propose(write_id='w2', expected_version=None); self.confirm(r2['candidate_id'])
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        code, out, err = self.cli('accept', '--candidate', str(r2['candidate_id']), '--expected-version', '1',
                                  env_extra={'SB_STATE_FAIL_BEFORE_INSERT': '1'})
        self.assertNotEqual(code, 0)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM state WHERE is_head=1').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT version FROM state WHERE is_head=1').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT status FROM state_candidate WHERE id=?',
                                        (r2['candidate_id'],)).fetchone()[0], 'proposed')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM state_acceptance').fetchone()[0], 1)

    def test_eight_processes_full_accounting_single_head(self):
        cids = []
        for i in range(8):
            r = self.propose(write_id='p%d' % i, body='v%d' % i, value_json=None)
            self.evidence.write_text(self.evidence.read_text(encoding='utf-8') + 'v%d\n' % i, encoding='utf-8')
            self.assertEqual(self.confirm(r['candidate_id'])['result'], 'confirmed')
            cids.append(r['candidate_id'])
        procs = [subprocess.Popen([PY, str(BIN / 'sb_state.py'), '--db', str(self.db), 'accept', '--candidate',
                                   str(c), '--expected-version', '0'], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding='utf-8', env=dict(os.environ)) for c in cids]
        results = []
        for p in procs:
            out, _err = p.communicate(timeout=60)
            results.append((p.returncode, json.loads(out.strip().splitlines()[-1])))
        codes = [c for c, _ in results]
        counts = {'ok': codes.count(0), 'stale_version': codes.count(3), 'busy': codes.count(4),
                  'other': len([c for c in codes if c not in (0, 3, 4)])}
        self.assertEqual(sum(counts.values()), 8, counts)
        self.assertEqual(counts['other'], 0, results)
        self.assertEqual(counts['ok'], 1, counts)
        for code, out in results:
            self.assertIn(out.get('error', 'ok') if code else 'ok', ('ok', 'stale_version', 'busy'))
        self.assertEqual(len(self.heads()), 1)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM state_candidate WHERE status='accepted'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM state_acceptance').fetchone()[0], 1)

    def test_busy_is_distinct_from_stale_version(self):
        r = self.propose(); self.confirm(r['candidate_id'])
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute('BEGIN IMMEDIATE')
        try:
            old = sb_state.BUSY_TIMEOUT_MS
            sb_state.BUSY_TIMEOUT_MS = 200
            with self.assertRaises(sb_state.StateError) as cm:
                sb_state.accept(r['candidate_id'], 0)
            self.assertEqual(cm.exception.code, 'busy')
        finally:
            sb_state.BUSY_TIMEOUT_MS = old
            holder.execute('ROLLBACK'); holder.close()
        self.assertEqual(sb_state.accept(r['candidate_id'], 0)['version'], 1)


class ConstraintTests(StateOpsBase):
    def _conn(self):
        db = sqlite3.connect(self.db, isolation_level=None)
        db.execute('PRAGMA foreign_keys = ON')
        return db

    def test_verification_exactly_one_target_and_existing_fk(self):
        r = self.propose()
        db = self._conn()
        base = "INSERT INTO state_verification(candidate_id, state_id, scope_id, method, target, result, checked_at) VALUES "
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(base + "(NULL, NULL, 'proj-a', 'file_contains', 't', 'confirmed', 'now')")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(base + "(?, 1, 'proj-a', 'file_contains', 't', 'confirmed', 'now')", (r['candidate_id'],))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(base + "(999, NULL, 'proj-a', 'file_contains', 't', 'confirmed', 'now')")
        with self.assertRaises(sqlite3.IntegrityError):  # scope mismatch trigger
            db.execute(base + "(?, NULL, 'proj-b', 'file_contains', 't', 'confirmed', 'now')", (r['candidate_id'],))
        db.execute(base + "(?, NULL, 'proj-a', 'file_contains', 't', 'unknown', 'now')", (r['candidate_id'],))
        db.close()

    def test_acceptance_requires_confirmed_verification_of_same_candidate(self):
        r = self.propose(); other = self.propose(write_id='w2')
        v_other = sb_state.verify(other['candidate_id'], 'file_contains', str(self.evidence))
        db = self._conn()
        db.execute("INSERT INTO state(memory_kind, scope_id, fact_key, version, is_head, recorded_at, body, source, "
                   "write_id, dedup_key) VALUES ('fact','proj-a','k',1,1,'now','b','s','w','legacy:key')")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('INSERT INTO state_acceptance VALUES (?, 1, ?, ?, ?)',
                       (r['candidate_id'], v_other['verification_id'], 'operationally measured', 'now'))
        db.close()


class ScopeIsolationTests(StateOpsBase):
    def test_heads_of_other_scopes_are_separate_and_legacy_head_unverified(self):
        r = self.propose(); self.confirm(r['candidate_id']); sb_state.accept(r['candidate_id'], 0)
        rb = self.propose(scope_id='proj-b', body='staging-2'); self.confirm(rb['candidate_id'])
        sb_state.accept(rb['candidate_id'], 0)
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO state(memory_kind, scope_id, fact_key, version, is_head, recorded_at, body, source, "
                       "write_id, dedup_key) VALUES ('fact','proj-a','legacy.key',1,1,'now','old','s','w','proj-a:s:w')")
        a = sb_state.query('head', 'proj-a')
        self.assertEqual({i['fact_key']: i['verification_status'] for i in a['items']},
                         {'deploy.target': 'verified', 'legacy.key': 'unverified'})
        self.assertEqual([i['scope_id'] for i in sb_state.query('head', 'proj-b')['items']], ['proj-b'])
        self.assertEqual(sb_state.query('head', 'proj-c')['status'], 'empty')

    def test_history_lists_all_versions_oldest_first(self):
        r = self.propose(); self.confirm(r['candidate_id']); sb_state.accept(r['candidate_id'], 0)
        r2 = self.propose(write_id='w2', body='staging-3', value_json=None, expected_version=1)
        self.evidence.write_text('staging-3\n', encoding='utf-8'); self.confirm(r2['candidate_id']); sb_state.accept(r2['candidate_id'], 1)
        hist = sb_state.query('history', 'proj-a', 'deploy.target')
        self.assertEqual([(i['version'], i['is_head']) for i in hist['items']], [(1, False), (2, True)])


if __name__ == '__main__':
    unittest.main()
