"""M3 Pro 리뷰(setup 묶음) 회귀 시험 — 5단계 검증 판정. 가짜 홈·임시 DB만 쓴다.

P1-1+P2-6: 최소 표본·시간창 미강제, DB 없음·손상·문자열 id 때 다른 프로젝트 지표 생략, ms 없을 때 p95 생략,
           소표본 p95 판정 → 판정할 수 없으면 pending, 문턱을 어긴 지표가 있으면 표본이 적어도 stop.
P1-2: 훅의 자동화 줄에는 ids 가 없어 automation_injections 가 구조상 늘 0 — 자동화 시도 수를 따로 세고,
      자동화 표본이 없으면 그 지표는 판정 보류(pass=None, 단계 전체는 막지 않음).
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / 'bin'))
import sb_setup  # noqa: E402


def _ts(days_ago: float = 0.0) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - days_ago * 86400))


class Stage5Env(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sb-m3fix-setup-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.sbhome = self.root / 'home' / '.secondbrain'
        self.memdir = self.root / 'home' / '.claude-mem'
        self.sbhome.mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith('SB_SETUP_VERIFY')}
        env.update(HOME=str(self.root / 'home'), SB_HOME=str(self.sbhome), SB_CLAUDE_MEM_DIR=str(self.memdir))
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def make_db(self, rows=((1, 'alpha'), (2, 'alpha'), (3, 'alpha'), (101, 'beta'))):
        self.memdir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.memdir / 'claude-mem.db'))
        con.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY, project TEXT, title TEXT)')
        con.executemany('INSERT INTO observations VALUES (?,?,?)', [(i, p, 't') for i, p in rows])
        con.commit()
        con.close()

    def write_log(self, rows):
        log = sb_setup.injections_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')

    def inj(self, n=12, ids=(1, 2), ms=100, days_ago=0.0, **extra):
        out = []
        for i in range(n):
            r = {'ts': _ts(days_ago), 'project': 'alpha', 'ids': list(ids)}
            if ms is not None:
                r['ms'] = ms + i
            r.update(extra)
            out.append(r)
        return out

    def stage5(self):
        r = sb_setup.stage5({})
        return r['verdict'], {m['name']: m for m in r['metrics']}


class P11MinimumSampleTests(Stage5Env):
    def test_single_injection_is_pending_not_pass(self):
        self.make_db()
        self.write_log(self.inj(1))
        v, ms = self.stage5()
        self.assertEqual(v, 'pending')
        self.assertIsNone(ms['recall_injections']['pass'])

    def test_enough_recent_injections_pass(self):
        self.make_db()
        self.write_log(self.inj(12))
        v, ms = self.stage5()
        self.assertEqual(v, 'pass', ms)
        self.assertEqual(ms['cross_scope_ratio']['value'], 0.0)
        self.assertTrue(ms['p95_ms']['pass'])

    def test_violation_wins_over_small_sample(self):
        """표본이 적어도 이미 문턱을 어긴 지표가 있으면 stop."""
        self.make_db()
        self.write_log(self.inj(2, ids=(101,)))
        v, ms = self.stage5()
        self.assertEqual(v, 'stop')
        self.assertFalse(ms['cross_scope_ratio']['pass'])

    def test_min_override_restores_old_behavior(self):
        self.make_db()
        self.write_log(self.inj(1))
        with mock.patch.dict(os.environ, {'SB_SETUP_VERIFY_MIN': '1'}):
            v, _ = self.stage5()
        self.assertEqual(v, 'pass')


class P11WindowTests(Stage5Env):
    def test_old_rows_outside_window_do_not_count(self):
        self.make_db()
        self.write_log(self.inj(12, days_ago=400))
        v, ms = self.stage5()
        self.assertEqual(v, 'pending')
        self.assertEqual(ms['recall_injections']['value'], 0)

    def test_rows_without_ts_are_outside_window(self):
        self.make_db()
        rows = self.inj(12)
        for r in rows:
            r.pop('ts')
        self.write_log(rows)
        v, _ = self.stage5()
        self.assertEqual(v, 'pending')

    def test_window_off_counts_old_rows(self):
        self.make_db()
        self.write_log(self.inj(12, days_ago=400))
        with mock.patch.dict(os.environ, {'SB_SETUP_VERIFY_DAYS': '0'}):
            v, _ = self.stage5()
        self.assertEqual(v, 'pass')

    def test_tz_aware_ts_is_understood(self):
        self.make_db()
        rows = self.inj(12)
        for r in rows:
            r['ts'] = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())
        self.write_log(rows)
        v, _ = self.stage5()
        self.assertEqual(v, 'pass')


class P11CrossScopeUnverifiableTests(Stage5Env):
    def test_missing_db_is_pending_with_metric(self):
        self.write_log(self.inj(12))
        v, ms = self.stage5()
        self.assertEqual(v, 'pending')
        self.assertIn('cross_scope_ratio', ms)
        self.assertIsNone(ms['cross_scope_ratio']['pass'])

    def test_corrupt_db_is_pending(self):
        self.memdir.mkdir(parents=True)
        (self.memdir / 'claude-mem.db').write_bytes(b'not a sqlite database at all' * 100)
        self.write_log(self.inj(12))
        v, ms = self.stage5()
        self.assertEqual(v, 'pending')
        self.assertIsNone(ms['cross_scope_ratio']['pass'])

    def test_numeric_string_ids_are_read(self):
        self.make_db()
        self.write_log(self.inj(12, ids=('1', '101')))
        v, ms = self.stage5()
        self.assertEqual(v, 'stop')
        self.assertEqual(ms['cross_scope_ratio']['value'], 1.0)

    def test_unparseable_ids_are_unverifiable(self):
        self.make_db()
        self.write_log(self.inj(12, ids=('x', 'y')))
        v, ms = self.stage5()
        self.assertEqual(v, 'pending')
        self.assertIsNone(ms['cross_scope_ratio']['pass'])

    def test_ids_missing_from_db_are_unverifiable(self):
        self.make_db()
        self.write_log(self.inj(12, ids=(500, 501)))
        v, ms = self.stage5()
        self.assertEqual(v, 'pending')
        self.assertIsNone(ms['cross_scope_ratio']['pass'])


class P11P26LatencyTests(Stage5Env):
    def test_missing_ms_is_pending_with_metric(self):
        self.make_db()
        self.write_log(self.inj(12, ms=None))
        v, ms = self.stage5()
        self.assertEqual(v, 'pending')
        self.assertIn('p95_ms', ms)
        self.assertIsNone(ms['p95_ms']['pass'])

    def test_few_ms_samples_do_not_pass_p95(self):
        """ms 1건(2900)만으로 p95 가 통과로 판정되지 않는다."""
        self.make_db()
        rows = self.inj(12, ms=None)
        rows[0]['ms'] = 2900
        self.write_log(rows)
        v, ms = self.stage5()
        self.assertEqual(v, 'pending')
        self.assertIsNone(ms['p95_ms']['pass'])

    def test_only_minimal_metrics_never_pass(self):
        """DB 없음·ms 없음·1건 — 지표 둘만으로 pass 가 나오던 경우."""
        self.write_log([{'ts': _ts(), 'project': 'alpha', 'ids': [1]}])
        v, _ = self.stage5()
        self.assertEqual(v, 'pending')


class P12AutomationMetricTests(Stage5Env):
    def test_attempts_counted_from_hook_shaped_rows(self):
        self.make_db()
        rows = self.inj(12) + [{'ts': _ts(), 'project': 'alpha', 'automation': True, 'why': 'tmp_cwd'}] * 3
        self.write_log(rows)
        v, ms = self.stage5()
        self.assertEqual(v, 'pass', ms)
        self.assertEqual(ms['automation_attempts']['value'], 3)
        self.assertIn('tmp_cwd', ms['automation_attempts']['evidence'])
        self.assertEqual((ms['automation_injections']['value'], ms['automation_injections']['pass']), (0, True))

    def test_no_automation_sample_is_undecided_but_not_blocking(self):
        self.make_db()
        self.write_log(self.inj(12))
        v, ms = self.stage5()
        self.assertEqual(v, 'pass')
        self.assertEqual(ms['automation_attempts']['value'], 0)
        self.assertIsNone(ms['automation_injections']['pass'])

    def test_automation_row_with_ids_stops(self):
        self.make_db()
        rows = self.inj(12) + [{'ts': _ts(), 'project': 'alpha', 'ids': [1], 'automation': True, 'why': 'probe'}]
        self.write_log(rows)
        v, ms = self.stage5()
        self.assertEqual(v, 'stop')
        self.assertEqual((ms['automation_injections']['value'], ms['automation_injections']['pass']), (1, False))

    def test_real_hook_automation_rows_feed_attempts(self):
        """실제 훅(하위 프로세스)이 임시 폴더 세션에서 남긴 줄로 자동화 시도가 잡힌다."""
        cwd = tempfile.TemporaryDirectory(prefix='sb-m3fix-cwd-')
        self.addCleanup(cwd.cleanup)
        env = dict(os.environ, SB_RECALL_STATE_DIR=str(self.root / 'state'), PYTHONIOENCODING='utf-8')
        for k in ('CLAUDE_MEM_INTERNAL', 'SB_RECALL', 'SB_RECALL_AUTOMATION', 'SB_RECALL_SKIP_PROJECTS'):
            env.pop(k, None)
        payload = {'session_id': 's-auto', 'cwd': os.path.realpath(cwd.name),
                   'prompt': '이 저장소 구조를 정리해서 다음 단계 계획을 세워줘'}
        subprocess.run([sys.executable, str(KIT / 'hooks' / 'sb_recall.py')], input=json.dumps(payload),
                       text=True, env=env, cwd=str(self.root), capture_output=True, timeout=60)
        log = sb_setup.injections_log()
        hook_rows = [json.loads(x) for x in log.read_text(encoding='utf-8').splitlines() if x.strip()]
        self.assertTrue(hook_rows and all(r.get('automation') and 'ids' not in r for r in hook_rows), hook_rows)
        self.make_db()
        with log.open('a', encoding='utf-8') as f:
            f.write(''.join(json.dumps(r) + '\n' for r in self.inj(12)))
        v, ms = self.stage5()
        self.assertEqual(v, 'pass', ms)
        self.assertEqual(ms['automation_attempts']['value'], len(hook_rows))
        self.assertTrue(ms['automation_injections']['pass'])


if __name__ == '__main__':
    unittest.main()
