"""M3-T5 — L2 검증 실패 후보 종결(expire). 기본 꺼짐, 모든 쓰기는 임시 DB에서만 일어난다."""
import contextlib
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
import sb_audit  # noqa: E402
import sb_state  # noqa: E402

PY = sys.executable
ENV_KEYS = ('SB_HOME', 'SB_STATE_DB', 'SB_STATE_PILOT_SCOPES', 'SB_VERIFY_ALLOWED_ROOTS', 'SB_PROJECT_ALIASES',
            'SB_CLAUDE_MEM_DB', 'SB_L2_EXPIRE', 'SB_LOOPS_PATH', 'SB_EVAL_NOW')


class ExpireBase(unittest.TestCase):
    def setUp(self):
        self._tmpobj = tempfile.TemporaryDirectory(prefix='sb-m3-t5-')
        self.addCleanup(self._tmpobj.cleanup)
        self.tmp = Path(self._tmpobj.name)
        self.home = self.tmp / 'sbhome'
        self.db = self.tmp / 'state.db'
        self.loops = self.tmp / 'loops.jsonl'
        self.loops.write_text('', encoding='utf-8')
        (self.tmp / 'aliases.json').write_text('{}', encoding='utf-8')
        self._old = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._restore)
        os.environ.update({'SB_HOME': str(self.home), 'SB_STATE_DB': str(self.db),
                           'SB_STATE_PILOT_SCOPES': 'alpha,beta', 'SB_VERIFY_ALLOWED_ROOTS': str(self.tmp),
                           'SB_PROJECT_ALIASES': str(self.tmp / 'aliases.json'),
                           'SB_CLAUDE_MEM_DB': str(self.tmp / 'none.db'), 'SB_LOOPS_PATH': str(self.loops),
                           'SB_EVAL_NOW': '2026-11-16T00:00:00+00:00'})
        os.environ.pop('SB_L2_EXPIRE', None)
        sb_state.migrate(str(self.db))

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def candidate(self, body, scope='alpha', key='deploy.target', verification=None):
        body = 'val-' + body  # 반박 문구('measured: zzz-other')의 부분 문자열이 되지 않게
        r = sb_state.propose({'scope_id': scope, 'fact_key': key, 'body': body, 'value_json': body,
                              'kind': 'measured_fact', 'source': 't', 'write_id': 'c-%s-%s-%s' % (scope, key, body)})
        cid = r['candidate_id']
        for i, res in enumerate(verification or []):
            ev = self.tmp / ('ev-%d-%d.txt' % (cid, i))
            if res == 'unknown':
                target = str(self.tmp / 'missing.txt')
            else:
                ev.write_text('measured: %s\n' % (body if res == 'confirmed' else 'zzz-other'), encoding='utf-8')
                target = str(ev)
            out = sb_state.verify(cid, 'file_contains', target)
            self.assertEqual(out['result'], res)
        return cid

    def status(self, cid):
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            return db.execute('SELECT status FROM state_candidate WHERE id=?', (cid,)).fetchone()[0]

    def transitions(self):
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            if 'state_candidate_transition' not in sb_state.tables(db):
                return None
            return db.execute('SELECT candidate_id, from_status, to_status, reason, basis_verification_id '
                              'FROM state_candidate_transition ORDER BY id').fetchall()

    def write_vault(self, conf):
        (self.home / 'config').mkdir(parents=True, exist_ok=True)
        (self.home / 'config' / 'vault.json').write_text(json.dumps(conf), encoding='utf-8')


class ExpireGateTests(ExpireBase):
    def test_default_off_changes_nothing_and_creates_no_table(self):
        cid = self.candidate('a', verification=['contradicted'])
        out = sb_state.expire()
        self.assertEqual((out['status'], out['enabled'], out['changed'], out['matched']), ('disabled', False, 0, 1))
        self.assertEqual(self.status(cid), 'proposed')
        self.assertIsNone(self.transitions())

    def test_env_on_enables(self):
        os.environ['SB_L2_EXPIRE'] = '1'
        cid = self.candidate('a', verification=['contradicted'])
        out = sb_state.expire()
        self.assertEqual((out['enabled_by'], out['changed']), ('env:SB_L2_EXPIRE', 1))
        self.assertEqual(self.status(cid), 'rejected')

    def test_vault_flag_enables_and_env_zero_overrides(self):
        self.write_vault({'layers': {'l2': {'reject_on_verify_fail': True}}})
        self.assertEqual(sb_state.expire_enabled(), (True, 'vault.json:layers.l2.reject_on_verify_fail'))
        os.environ['SB_L2_EXPIRE'] = '0'
        self.assertEqual(sb_state.expire_enabled()[0], False)
        os.environ.pop('SB_L2_EXPIRE')
        self.write_vault({'layers': {'l2': {'reject_on_verify_fail': 'true'}}})  # 문자열은 켜지 않는다
        self.assertEqual(sb_state.expire_enabled(), (False, 'default'))
        (self.home / 'config' / 'vault.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(sb_state.expire_enabled(), (False, 'default'))

    def test_dry_run_is_read_only_even_when_enabled(self):
        os.environ['SB_L2_EXPIRE'] = '1'
        cid = self.candidate('a', verification=['contradicted'])
        out = sb_state.expire(dry_run=True)
        self.assertEqual((out['matched'], out['changed'], out['candidate_ids']), (1, 0, [cid]))
        self.assertEqual(self.status(cid), 'proposed')
        self.assertIsNone(self.transitions())


class ExpireSelectionTests(ExpireBase):
    def setUp(self):
        super().setUp()
        os.environ['SB_L2_EXPIRE'] = '1'

    def test_only_last_failed_proposed_candidates_close(self):
        bad = self.candidate('bad', verification=['contradicted'])
        recovered = self.candidate('rec', verification=['contradicted', 'confirmed'])
        relapsed = self.candidate('rel', verification=['confirmed', 'contradicted'])
        unknown = self.candidate('unk', verification=['unknown'])
        never = self.candidate('none')
        # 절차 오류 rejected(user_decision 를 file_contains 로 검증)는 다시 검증하면 되므로 닫지 않는다(11-16 검토 P1)
        r = sb_state.propose({'scope_id': 'alpha', 'fact_key': 'd', 'body': 'x', 'kind': 'user_decision',
                              'source': 't', 'write_id': 'ud'})
        self.assertEqual(sb_state.verify(r['candidate_id'], 'file_contains', str(self.loops))['result'], 'rejected')
        out = sb_state.expire()
        self.assertEqual(sorted(out['candidate_ids']), sorted([bad, relapsed]))
        self.assertEqual(out['by_last_result'], {'contradicted': 2})
        self.assertEqual(out['changed'], 2)
        for cid in (bad, relapsed):
            self.assertEqual(self.status(cid), 'rejected')
        for cid in (recovered, unknown, never, r['candidate_id']):
            self.assertEqual(self.status(cid), 'proposed')
        rows = self.transitions()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(t[1:3] == ('proposed', 'rejected') for t in rows))
        self.assertEqual({t[3] for t in rows}, {'expire:last_verification_contradicted'})
        self.assertTrue(all(t[4] for t in rows))

    def test_idempotent_and_head_untouched(self):
        ok = self.candidate('good', verification=['confirmed'])
        sb_state.accept(ok, 0)
        bad = self.candidate('bad', verification=['contradicted'])
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            before = db.execute('SELECT * FROM state ORDER BY id').fetchall()
        self.assertEqual(sb_state.expire()['changed'], 1)
        second = sb_state.expire()
        self.assertEqual((second['changed'], second['matched']), (0, 0))
        self.assertEqual(len(self.transitions()), 1)
        with contextlib.closing(sqlite3.connect(str(self.db))) as db:
            self.assertEqual(db.execute('SELECT * FROM state ORDER BY id').fetchall(), before)
        self.assertEqual((self.status(ok), self.status(bad)), ('accepted', 'rejected'))
        with self.assertRaises(sb_state.StateError):  # 닫힌 후보는 채택 경로로 되살아나지 않는다
            sb_state.accept(bad, 1)

    def test_pilot_scope_gate(self):
        a = self.candidate('a', scope='alpha', verification=['contradicted'])
        b = self.candidate('b', scope='beta', verification=['contradicted'])
        os.environ['SB_STATE_PILOT_SCOPES'] = 'alpha'
        out = sb_state.expire()
        self.assertEqual((out['changed'], out['skipped_scope_gate']), (1, 1))
        self.assertEqual((self.status(a), self.status(b)), ('rejected', 'proposed'))

    def test_existing_db_without_transition_table_is_migrated_in_place(self):
        cid = self.candidate('a', verification=['contradicted'])
        self.assertIsNone(self.transitions())
        self.assertEqual(sb_state.expire()['changed'], 1)
        self.assertEqual(len(self.transitions()), 1)
        self.assertEqual(self.status(cid), 'rejected')
        # 이미 표가 있어도 다시 돌릴 수 있다
        self.candidate('b', key='other', verification=['contradicted'])
        self.assertEqual(sb_state.expire()['changed'], 1)
        self.assertEqual(len(self.transitions()), 2)

    def test_missing_schema_and_db(self):
        empty = self.tmp / 'empty.db'
        sqlite3.connect(str(empty)).close()
        with self.assertRaises(sb_state.StateError) as cm:
            sb_state.expire(str(empty))
        self.assertEqual(cm.exception.code, 'schema_missing')
        with self.assertRaises(sb_state.StateError) as cm:
            sb_state.expire(str(self.tmp / 'nope.db'))
        self.assertEqual(cm.exception.code, 'unavailable')

    def test_cli(self):
        cid = self.candidate('a', verification=['contradicted'])
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}

        def run(*args, **extra):
            p = subprocess.run([PY, str(BIN / 'sb_state.py'), '--db', str(self.db), 'expire', *args],
                               capture_output=True, text=True, encoding='utf-8', env={**env, **extra}, cwd=str(self.tmp))
            return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])

        code, out = run('--dry-run')
        self.assertEqual((code, out['matched'], out['changed']), (0, 1, 0))
        code, out = run(SB_L2_EXPIRE='0')
        self.assertEqual((code, out['status']), (0, 'disabled'))
        self.assertEqual(self.status(cid), 'proposed')
        code, out = run()
        self.assertEqual((code, out['changed']), (0, 1))
        self.assertEqual(self.status(cid), 'rejected')


class AuditRejectedTests(ExpireBase):
    def test_rejected_candidate_not_counted_as_open(self):
        os.environ['SB_L2_EXPIRE'] = '1'
        ok = self.candidate('good', verification=['confirmed'])
        sb_state.accept(ok, 0)
        self.candidate('bad', verification=['contradicted'])
        before = sb_audit.audit_scope('alpha')
        item = [i for i in before['items'] if i['layer'] == 'L2'][0]
        self.assertIn('verification_contradicted', item['reason_codes'])
        self.assertIn('value_conflict', item['reason_codes'])
        sb_state.expire()
        after = sb_audit.audit_scope('alpha')
        item = [i for i in after['items'] if i['layer'] == 'L2'][0]
        self.assertNotIn('verification_contradicted', item['reason_codes'])
        self.assertNotIn('value_conflict', item['reason_codes'])
        self.assertNotIn('candidate:', item['basis'] or '')


if __name__ == '__main__':
    unittest.main()
