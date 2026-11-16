"""M3 Pro 리뷰 회귀 시험 — hooks/sb_recall.py (P1-13, P1-8, P1-9, P1-12)."""
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / 'bin'))
sys.path.insert(0, str(KIT / 'hooks'))
import sb_recall  # noqa: E402

CLEAN_KEYS = ('CLAUDE_MEM_INTERNAL', 'SB_RECALL', 'SB_RECALL_AUTOMATION', 'SB_RECALL_SKIP_PROJECTS',
              'SB_RECALL_AUTOMATION_CORRECTION', 'SB_CODEX_DETECTOR_FAIL_OPEN', 'SB_RECALL_ENRICH_FAIL_OPEN')


class _Env(unittest.TestCase):
    def setUp(self):
        clean = {k: v for k, v in os.environ.items() if k not in CLEAN_KEYS}
        self.env = patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, 'state')
        os.makedirs(self.state)

    def run_main(self, payload, argv=('sb_recall.py',), corr='', items=None, extra=()):
        logs, out = [], io.StringIO()
        item = {'id': 7, 'date': '09-01', 'title': '거래처 단가', 'project': 'alpha'}
        patches = [patch.object(sb_recall, 'STATE_DIR', self.state),
                   patch.object(sb_recall, 'recall_log', side_effect=logs.append),
                   patch.object(sb_recall, 'BACKEND', 'recalld'),
                   patch.object(sb_recall, 'superseded_ids', return_value=set()),
                   patch.object(sb_recall, 'BODIES', 0),
                   patch('sys.argv', list(argv)),
                   patch('sys.stdin', io.StringIO(json.dumps(payload, ensure_ascii=False))),
                   patch('sys.stdout', out)] + list(extra)
        self.cn = patch.object(sb_recall, 'correction_note', return_value=corr).start()
        self.rs = patch.object(sb_recall, 'recalld_search', return_value=[dict(item)] if items is None else items).start()
        self.addCleanup(patch.stopall)
        for p in patches:
            p.start()
        try:
            if not any(getattr(p, 'attribute', None) == 'enrich' for p in extra):
                with patch.object(sb_recall, 'enrich', side_effect=lambda its, sid: its):
                    sb_recall.main()
            else:
                sb_recall.main()
        finally:
            for p in reversed(patches):
                p.stop()
        raw = out.getvalue().strip()
        ctx = json.loads(raw)['hookSpecificOutput']['additionalContext'] if raw else ''
        return ctx, logs


# ── P1-13: 자동화 판정이 교정 처리보다 먼저 ─────────────────────────────────────────
class AutomationBeforeCorrectionTests(_Env):
    def test_automation_skips_correction_note(self):
        payload = {'prompt': '틀렸어 다시 해. OK only', 'session_id': 's-auto', 'cwd': '/Users/x/alpha'}
        out, logs = self.run_main(payload, corr='[교정 감지] 가짜 규칙')
        self.cn.assert_not_called()
        self.rs.assert_not_called()
        self.assertEqual(out, '')
        self.assertEqual([r.get('why') for r in logs], ['probe_phrase'])

    def test_tmp_cwd_session_gets_no_correction_context(self):
        payload = {'prompt': '이 저장소 구조를 정리해서 다음 단계 계획을 세워줘', 'session_id': 's-tmp',
                   'cwd': '/private/tmp/run-1'}
        out, _ = self.run_main(payload, corr='[교정 감지] 가짜 규칙')
        self.cn.assert_not_called()
        self.assertEqual(out, '')

    def test_switch_restores_previous_behavior(self):
        os.environ['SB_RECALL_AUTOMATION_CORRECTION'] = '1'
        payload = {'prompt': '틀렸어 다시 해. OK only', 'session_id': 's-auto', 'cwd': '/Users/x/alpha'}
        out, logs = self.run_main(payload, corr='[교정 감지] 가짜 규칙')
        self.cn.assert_called_once()
        self.assertIn('교정 감지', out)
        self.rs.assert_not_called()
        self.assertTrue(logs[0]['automation'])

    def test_human_prompt_still_gets_correction(self):
        payload = {'prompt': '거래처 단가 계약 정리해줘', 'session_id': 's-plain', 'cwd': '/Users/x/alpha'}
        out, _ = self.run_main(payload, corr='[교정 감지] 진짜 규칙')
        self.cn.assert_called_once()
        self.assertIn('교정 감지', out)
        self.assertIn('#7', out)


# ── P1-8: Codex 자동화 판정기를 못 불러오면 닫힌 쪽으로 ──────────────────────────────
class CodexDetectorFailClosedTests(_Env):
    PAYLOAD = {'prompt': '거래처 단가 계약 정리해줘', 'session_id': 's-codex', 'cwd': '/Users/x/alpha'}

    def test_import_failure_injects_nothing_and_logs(self):
        with patch.dict(sys.modules, {'codex_hook': None}):   # import 가 ImportError
            out, logs = self.run_main(self.PAYLOAD, argv=('sb_recall.py', 'codex'), corr='[교정 감지] x')
        self.assertEqual(out, '')
        self.cn.assert_not_called()
        self.rs.assert_not_called()
        self.assertEqual(len(logs), 1)
        self.assertIs(logs[0]['automation'], True)
        self.assertEqual(logs[0]['why'], 'codex_detector_error')
        self.assertEqual(os.listdir(self.state), [])

    def test_detector_exception_injects_nothing(self):
        broken = type(sys)('codex_hook')

        def boom(raw):
            raise RuntimeError('broken')
        broken.is_automation = boom
        with patch.dict(sys.modules, {'codex_hook': broken}):
            out, logs = self.run_main(self.PAYLOAD, argv=('sb_recall.py', 'codex'))
        self.assertEqual(out, '')
        self.assertEqual(logs[0]['why'], 'codex_detector_error')

    def test_detector_ok_and_interactive_still_recalls(self):
        ok = type(sys)('codex_hook')
        ok.is_automation = lambda raw: False
        with patch.dict(sys.modules, {'codex_hook': ok}):
            out, _ = self.run_main(self.PAYLOAD, argv=('sb_recall.py', 'codex'))
        self.assertIn('#7', out)

    def test_switch_restores_fail_open(self):
        os.environ['SB_CODEX_DETECTOR_FAIL_OPEN'] = '1'
        with patch.dict(sys.modules, {'codex_hook': None}):
            out, _ = self.run_main(self.PAYLOAD, argv=('sb_recall.py', 'codex'))
        self.assertIn('#7', out)

    def test_claude_path_unaffected(self):
        with patch.dict(sys.modules, {'codex_hook': None}):
            out, _ = self.run_main(self.PAYLOAD)
        self.assertIn('#7', out)


# ── P1-9: enrich 실패 시 거르지 않은 후보를 내보내지 않는다 ────────────────────────────
class EnrichFailClosedTests(_Env):
    def make_db(self, path, with_sessions=True):
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY, memory_session_id TEXT, created_at TEXT, '
                    'project TEXT, created_at_epoch INTEGER, metadata TEXT)')
        if with_sessions:
            con.execute('CREATE TABLE sdk_sessions (memory_session_id TEXT, content_session_id TEXT)')
            con.execute("INSERT INTO sdk_sessions VALUES ('m-cur', 'SID-CUR'), ('m-oth', 'SID-OTH')")
        now = time.time() * 1000
        con.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?)', [
            (1, 'm-cur', '2026-11-16T10:00:00', 'alpha', int(now - 2 * 3600e3), ''),
            (2, 'm-oth', '2026-11-16T11:55:00', 'alpha', int(now - 5 * 60e3), ''),
            (3, 'm-oth', '2026-10-07T09:00:00', 'alpha', int(now - 40 * 86400e3), ''),
        ])
        con.commit()
        con.close()

    def items(self):
        return [{'id': i, 'date': '', 'kind': '', 'title': 't%d' % i} for i in (1, 2, 3)]

    def enrich(self, db):
        sb_recall.LAST.pop('enrich_failed', None)
        with patch.object(sb_recall.sb_config, 'claude_mem_db', return_value=db):
            return [it['id'] for it in sb_recall.enrich(self.items(), 'SID-CUR')]

    def test_normal_db_drops_own_and_fresh(self):
        db = os.path.join(self.tmp.name, 'ok.db')
        self.make_db(db)
        self.assertEqual(self.enrich(db), [3])
        self.assertNotIn('enrich_failed', sb_recall.LAST)

    def test_missing_db_returns_nothing(self):
        self.assertEqual(self.enrich(os.path.join(self.tmp.name, 'none.db')), [])
        self.assertTrue(sb_recall.LAST.get('enrich_failed'))

    def test_schema_mismatch_returns_nothing(self):
        db = os.path.join(self.tmp.name, 'old.db')
        self.make_db(db, with_sessions=False)
        self.assertEqual(self.enrich(db), [])
        self.assertEqual(sb_recall.LAST.get('enrich_failed'), 'OperationalError')

    def test_switch_restores_fail_open(self):
        os.environ['SB_RECALL_ENRICH_FAIL_OPEN'] = '1'
        self.assertEqual(self.enrich(os.path.join(self.tmp.name, 'none.db')), [1, 2, 3])

    def test_main_logs_enrich_failure_without_fail_notice(self):
        payload = {'prompt': '거래처 단가 계약 정리해줘', 'session_id': 'SID-CUR', 'cwd': '/Users/x/alpha'}
        missing = patch.object(sb_recall.sb_config, 'claude_mem_db', return_value=os.path.join(self.tmp.name, 'none.db'))
        enrich_real = patch.object(sb_recall, 'enrich', side_effect=sb_recall.enrich)
        out, logs = self.run_main(payload, extra=(missing, enrich_real))
        self.assertNotIn('#7', out)
        self.assertNotIn('회수 실패', out)
        self.assertTrue(logs[-1].get('enrich_failed'))
        self.assertEqual(logs[-1]['ids'], [])


# ── P1-12a: 세션 상태 파일 이름 충돌 ─────────────────────────────────────────────
class StateNameTests(_Env):
    def test_uuid_and_plain_names_unchanged(self):
        with patch.object(sb_recall, 'STATE_DIR', self.state):
            for sid in ('01a06fdd-c175-7000-bfc7-8960a4114677', 'a_b', 'nosession'):
                self.assertEqual(sb_recall.state_file(sid, '.json'), os.path.join(self.state, sid + '.json'))

    def test_special_chars_do_not_collide(self):
        with patch.object(sb_recall, 'STATE_DIR', self.state):
            names = {sb_recall.state_file(s, '.json') for s in ('a/b', 'a_b', 'a.b', 'a b')}
        self.assertEqual(len(names), 4)

    def test_distinct_sessions_keep_separate_ledgers(self):
        outs = []
        for sid in ('a/b', 'a_b', 'a.b'):
            out, _ = self.run_main({'prompt': '거래처 단가 계약 정리해줘', 'session_id': sid, 'cwd': '/Users/x/alpha'})
            outs.append('#7' in out)
        self.assertEqual(outs, [True, True, True])

    def test_correction_note_uses_same_naming(self):
        seen = []
        real_locked = sb_recall._Locked

        def spy(path):
            seen.append(path)
            return real_locked(path)
        with patch.object(sb_recall, 'STATE_DIR', self.state), patch.object(sb_recall, '_Locked', side_effect=spy), \
                patch.object(sb_recall, 'spawn_correction_check'), patch.object(sb_recall, 'CORRECTION_QUEUE', os.path.join(self.tmp.name, 'q')):
            sb_recall.correction_note('a/b', '프롬프트', '/Users/x')
            want = sb_recall.state_file('a/b', '.corr.json')
        self.assertEqual(seen, [want])
        self.assertTrue(os.path.basename(seen[0]).startswith('h_'))


# ── P1-12b: 잠금 경합 시 무잠금 실행·비원자 저장 ───────────────────────────────────
class LockAndAtomicWriteTests(_Env):
    PAYLOAD = {'prompt': '거래처 단가 계약 정리해줘', 'session_id': 's-lock', 'cwd': '/Users/x/alpha'}

    def test_lock_timeout_skips_recall_but_keeps_correction(self):
        def timeout(self_):
            raise TimeoutError('lock timeout')
        out, logs = self.run_main(self.PAYLOAD, corr='[교정 감지] 규칙',
                                  extra=(patch.object(sb_recall._Locked, '__enter__', timeout),))
        self.rs.assert_not_called()
        self.assertIn('교정 감지', out)
        self.assertNotIn('#7', out)
        self.assertFalse(os.path.exists(os.path.join(self.state, 's-lock.json')))

    def test_lock_open_error_keeps_previous_behavior(self):
        def oserr(self_):
            raise PermissionError('no lock file')
        out, _ = self.run_main(self.PAYLOAD, extra=(patch.object(sb_recall._Locked, '__enter__', oserr),))
        self.assertIn('#7', out)

    def test_state_write_is_atomic(self):
        path = os.path.join(self.state, 's-lock.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'ids': [5], 'last_prompt_sha': 'x'}, fh)
        real_dump = json.dump

        def partial(obj, fh, *a, **kw):
            fh.write('{"ids": [')
            raise OSError('disk full')
        with patch.object(sb_recall.json, 'dump', side_effect=partial):
            self.run_main(self.PAYLOAD)
        self.assertEqual(json.load(open(path, encoding='utf-8'))['ids'], [5])   # 기존 원장이 잘리지 않는다
        self.assertEqual(sorted(os.listdir(self.state)), ['s-lock.json', 's-lock.json.lock'])
        self.assertIs(json.dump, real_dump)

    def test_state_written_normally(self):
        self.run_main(self.PAYLOAD)
        st = json.load(open(os.path.join(self.state, 's-lock.json'), encoding='utf-8'))
        self.assertEqual(st['ids'], [7])


if __name__ == '__main__':
    unittest.main()
