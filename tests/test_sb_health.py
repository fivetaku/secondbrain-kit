"""sb_health 점검·경고 문구와, 그것을 쓰는 야간 배치 순서·저장 자가복구 계약."""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import nightly  # noqa: E402
import sb_health  # noqa: E402
import sb_memory  # noqa: E402


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix='sb-health-test-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = patch.dict(os.environ, {'SB_HOME': str(self.home), 'SB_WORKER_AUTOSTART': '0',
                                      'SB_OLLAMA_AUTOSTART': '0'})
        env.start()
        self.addCleanup(env.stop)
        (self.home / 'logs').mkdir()

    def status(self, **data) -> None:
        (self.home / 'logs' / 'nightly_status.json').write_text(json.dumps(data), encoding='utf-8')

    def test_nightly_missing_failed_and_stale(self) -> None:
        self.assertFalse(sb_health.check_nightly()['ok'])
        self.status(date=date.today().isoformat(), status='ok')
        self.assertTrue(sb_health.check_nightly()['ok'])
        self.status(date=date.today().isoformat(), status='failed', failed_stages=['automemory'])
        r = sb_health.check_nightly()
        self.assertFalse(r['ok'])
        self.assertIn('automemory', r['msg'])
        self.status(date=(date.today() - timedelta(days=3)).isoformat(), status='ok')
        r = sb_health.check_nightly()
        self.assertFalse(r['ok'])
        self.assertEqual(r['age_days'], 3)
        self.assertIn('3일째 미실행', r['msg'])

    def test_warning_line_lists_only_failures(self) -> None:
        self.assertEqual(sb_health.warning_line({'a': {'ok': True, 'msg': ''}}), '')
        line = sb_health.warning_line({'a': {'ok': False, 'msg': 'Ollama 꺼짐'},
                                       'b': {'ok': True, 'msg': ''},
                                       'c': {'ok': False, 'msg': '벡터 미반영 9건'}})
        self.assertTrue(line.startswith('⚠ 기록층 점검: '))
        self.assertIn('Ollama 꺼짐 · 벡터 미반영 9건', line)

    def test_spool_reports_only_new_expirations(self) -> None:
        cm = self.home / 'cm'
        (cm / 'state' / 'hook-spool' / 'expired').mkdir(parents=True)
        for i in range(3):
            (cm / 'state' / 'hook-spool' / 'expired' / ('e%d.json' % i)).write_text('{}')
        with patch.dict(os.environ, {'SB_CLAUDE_MEM_DIR': str(cm)}):
            first = sb_health.check_spool({})
            again = sb_health.check_spool({'spool': {'expired': 3}})
        self.assertEqual(first['new_expired'], 3)
        self.assertFalse(first['ok'])
        self.assertTrue(again['ok'])

    def cm(self) -> Path:
        cm = self.home / 'cm'
        cm.mkdir(exist_ok=True)
        env = patch.dict(os.environ, {'SB_CLAUDE_MEM_DIR': str(cm)})
        env.start()
        self.addCleanup(env.stop)
        return cm

    def test_chroma_pending_missing_normal_and_backlog(self) -> None:
        cm = self.cm()
        r = sb_health.check_chroma_pending()
        self.assertTrue(r['ok'])
        self.assertEqual(r['skipped'], 'no sync state')
        state = cm / 'chroma-sync-state.json'
        state.write_text(json.dumps({'a': {'observations': 3, 'pending': {'observations': [1, 2], 'prompts': [5]}},
                                     'b': {'observations': 1}, 'odd': 7}), encoding='utf-8')
        r = sb_health.check_chroma_pending()
        self.assertTrue(r['ok'])
        self.assertEqual((r['pending'], r['kinds']), (3, {'observations': 2, 'prompts': 1}))
        state.write_text(json.dumps({'a': {'pending': {'observations': list(range(150))}},
                                     'b': {'pending': {'summaries': list(range(60))}}}), encoding='utf-8')
        r = sb_health.check_chroma_pending()
        self.assertFalse(r['ok'])
        self.assertIsNone(r['fix'])   # 원인 해소 + 워커 재시작이 필요 — 자동 복구 대상이 아니다
        self.assertIn('Chroma 대기 210건', sb_health.warning_line({'chroma-pending': r}))
        state.write_text('{broken', encoding='utf-8')
        r = sb_health.check_chroma_pending()
        self.assertFalse(r['ok'])
        self.assertIn('판독 실패', r['msg'])

    def test_chroma_log_counts_recent_tagged_lines_only(self) -> None:
        cm = self.cm()
        from datetime import datetime
        now = datetime(2026, 10, 30, 12, 0, 0)
        r = sb_health.check_chroma_log(now)
        self.assertTrue(r['ok'])
        self.assertEqual(r['skipped'], 'no logs')
        logs = cm / 'logs'
        logs.mkdir()
        err = '[{}.123] [ERROR] [CHROMA_SYNC] circuit breaker open, skipping write\n'
        # 어제 장애 30줄(이미 끝난 장애) + 오늘 훅 줄(명령 문자열에 같은 문구) + INFO 줄 — 모두 등급에 안 들어간다
        (logs / 'claude-mem-2026-10-29.log').write_text(err.format('2026-10-29 09:00:00') * 30, encoding='utf-8')
        today = logs / 'claude-mem-2026-10-30.log'
        today.write_text('[2026-10-30 11:00:00.000] [INFO ] [HOOK  ] → PostToolUse: Bash(grep "prewarm failed")\n'
                         '[2026-10-30 11:00:01.000] [ERROR] [HOOK  ] Chroma search failed in command text\n'
                         '[2026-10-30 11:00:02.000] [INFO ] [CHROMA_SYNC] Chroma search failed (info only)\n'
                         + err.format('2026-10-30 05:00:00'), encoding='utf-8')
        r = sb_health.check_chroma_log(now)
        self.assertTrue(r['ok'])
        self.assertEqual((r['recent'], r['total_2d'], r['last']), (0, 31, '2026-10-30 05:00:00'))
        with today.open('a', encoding='utf-8') as f:
            f.write(err.format('2026-10-30 10:30:00') * 15)
            f.write('[2026-10-30 10:31:00.000] [WARN ] [CHROMA_MCP] prewarm failed: spawn error\n' * 5)
        r = sb_health.check_chroma_log(now)
        self.assertFalse(r['ok'])
        self.assertEqual(r['recent'], 20)
        self.assertIsNone(r['fix'])
        self.assertIn('최근 6시간 20건', r['msg'])

    def test_chroma_log_counts_ollama_batch_add_failures(self) -> None:
        # Ollama 가 꺼졌을 때 워커가 남기는 실제 줄 모양. 예전 패턴으로는 하루 수백 줄이 0건으로 보였다.
        cm = self.cm()
        from datetime import datetime
        (cm / 'logs').mkdir()
        line = ('[2026-10-30 11:00:00.000] [ERROR] [CHROMA_SYNC] Batch add failed — watermark will not advance for '
                'this batch {collection=cm__claude-mem, batchSize=6} chroma-mcp tool "chroma_add_documents" returned '
                "error: Failed to add documents to collection 'cm__claude-mem': Failed to connect to Ollama. Please "
                'check that Ollama is downloaded, running and accessible.\n')
        (cm / 'logs' / 'claude-mem-2026-10-30.log').write_text(
            line * 19 + '[2026-10-30 11:01:00.000] [WARN ] [CHROMA_MCP] Failed to connect to Ollama\n',
            encoding='utf-8')
        r = sb_health.check_chroma_log(datetime(2026, 10, 30, 12, 0, 0))
        self.assertFalse(r['ok'])
        self.assertEqual(r['recent'], 20)   # 한 줄에 두 문구가 함께 있어도 한 번만 센다

    def test_restart_worker_respects_test_env(self) -> None:
        with patch.object(sb_health.subprocess, 'run') as run:
            self.assertEqual(sb_health.restart_worker(wait=0), 'fail')
        run.assert_not_called()

    def restart_with(self, pids, up: bool = True):
        """재시작 전후 /api/health pid 를 pids 순서로 돌려주며 restart_worker 를 부른다."""
        root = self.home / 'plugin'
        (root / 'scripts').mkdir(parents=True, exist_ok=True)
        (root / 'scripts' / 'bun-runner.js').write_text('', encoding='utf-8')
        with patch.dict(os.environ, {'SB_WORKER_AUTOSTART': '1'}), \
                patch.object(sb_health, 'plugin_root', return_value=root), \
                patch.object(sb_health.shutil, 'which', return_value='node'), \
                patch.object(sb_health, '_wait_worker', return_value=up), \
                patch.object(sb_health, 'ensure_worker', return_value=up), \
                patch.object(sb_health, 'worker_pid', side_effect=pids), \
                patch.object(sb_health.subprocess, 'run') as run:
            os.environ.pop('SB_MEM_BASE_URL', None)
            return sb_health.restart_worker(wait=0), run, root

    def test_restart_worker_uses_claude_mem_restart_command(self) -> None:
        result, run, root = self.restart_with([100, 200])
        self.assertEqual(result, 'ok')   # pid 가 바뀌었다 — 새 워커가 기동 백필을 돈다
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], 'node')
        self.assertEqual(cmd[-2:], [str(root / 'scripts' / 'worker-service.cjs'), 'restart'])

    def test_restart_worker_noop_when_pid_unchanged(self) -> None:
        # restart 가 아무것도 안 했는데 원래 워커가 응답하면 ok 가 아니다
        self.assertEqual(self.restart_with([100, 100])[0], 'noop')
        self.assertEqual(self.restart_with([100, None])[0], 'noop')   # pid 를 못 읽으면 바뀐 걸 확인 못 한다
        self.assertEqual(self.restart_with([None, 300])[0], 'ok')
        self.assertEqual(self.restart_with([100], up=False)[0], 'fail')

    def test_worker_pid_reads_health_pid(self) -> None:
        class Resp:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.body

        with patch.object(sb_health.urllib.request, 'urlopen', return_value=Resp(b'{"status":"ok","pid":4321}')):
            self.assertEqual(sb_health.worker_pid(), 4321)
        with patch.object(sb_health.urllib.request, 'urlopen', return_value=Resp(b'{"status":"ok"}')):
            self.assertIsNone(sb_health.worker_pid())
        with patch.object(sb_health.urllib.request, 'urlopen', side_effect=urllib.error.URLError('down')):
            self.assertIsNone(sb_health.worker_pid())

    def test_run_checks_includes_chroma_items(self) -> None:
        self.cm()
        with patch.dict(os.environ, {'SB_CLAUDE_MEM_DB': str(self.home / 'none.db'), 'SB_HEALTH_OLLAMA': '0'}):
            r = sb_health.run_checks(services=False, recall=False)
        self.assertTrue(r['chroma-pending']['ok'])
        self.assertTrue(r['chroma-log']['ok'])

    def test_ensure_worker_respects_test_env(self) -> None:
        with patch.object(sb_health, 'worker_up', return_value=False), \
                patch.object(sb_health.subprocess, 'run') as run:
            self.assertFalse(sb_health.ensure_worker(wait=0))
        run.assert_not_called()


class VectorBackfillTests(unittest.TestCase):
    """벡터 미반영은 워커 시작 때만 백필된다 — 서비스가 떠 있으면 --fix 가 하루 1회 워커를 재시작한다."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix='sb-backfill-test-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / 'logs').mkdir()
        (self.home / 'logs' / 'nightly.log').write_text('', encoding='utf-8')   # installed_home() 표식
        env = patch.dict(os.environ, {'SB_HOME': str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('SB_HEALTH_NO_WRITE', None)
        self.addCleanup(patch.stopall)
        self.restart = patch.object(sb_health, 'restart_worker', return_value='ok').start()
        patch.object(sb_health, 'ensure_worker', return_value=True).start()
        patch.object(sb_health, 'worker_up', return_value=True).start()
        patch.object(sb_health, 'uses_ollama', return_value=False).start()
        self.state = self.home / 'logs' / 'worker-restart.json'

    @staticmethod
    def backlog(worker_ok: bool = True) -> dict:
        return {'worker': {'ok': worker_ok}, 'vector': {'ok': False, 'missing': 120, 'fix': 'worker'}}

    def test_live_worker_restarted_once_per_day(self) -> None:
        self.assertIn('vector-backfill:ok', sb_health.fix(self.backlog()))
        self.assertIn('vector-backfill:cooldown', sb_health.fix(self.backlog()))
        self.assertEqual(self.restart.call_count, 1)
        self.assertEqual(json.loads(self.state.read_text(encoding='utf-8'))['date'], date.today().isoformat())
        self.state.write_text(json.dumps({'date': (date.today() - timedelta(days=1)).isoformat()}), encoding='utf-8')
        self.assertIn('vector-backfill:ok', sb_health.fix(self.backlog()))   # 다음 날엔 다시 한 번
        self.assertEqual(self.restart.call_count, 2)

    def test_failed_restart_still_uses_the_days_chance(self) -> None:
        self.restart.return_value = 'fail'
        self.assertIn('vector-backfill:fail', sb_health.fix(self.backlog()))
        self.assertIn('vector-backfill:cooldown', sb_health.fix(self.backlog()))
        self.assertEqual(self.restart.call_count, 1)

    def test_noop_restart_recorded_not_ok(self) -> None:
        self.restart.return_value = 'noop'   # 재시작 전후 pid 가 같다 — 원래 워커가 응답했을 뿐
        self.assertIn('vector-backfill:noop', sb_health.fix(self.backlog()))
        state = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertEqual((state['date'], state['result']), (date.today().isoformat(), 'noop'))
        self.assertIn('vector-backfill:cooldown', sb_health.fix(self.backlog()))
        self.assertEqual(self.restart.call_count, 1)

    def test_no_restart_when_not_needed_or_useless(self) -> None:
        def tags(done):
            return [d for d in done if d.startswith('vector-backfill')]
        self.assertEqual(tags(sb_health.fix(self.backlog(worker_ok=False))), [])   # 꺼져 있던 워커는 기동이 백필한다
        with patch.object(sb_health, 'uses_ollama', return_value=True), \
                patch.object(sb_health, 'ensure_ollama', return_value=False), \
                patch.object(sb_health, 'ollama_up', return_value=False):
            self.assertEqual(tags(sb_health.fix(self.backlog())), [])   # 임베딩 서버가 아직 꺼져 있으면 무의미
        ok = {'worker': {'ok': True}, 'vector': {'ok': True, 'missing': 3}, 'ko-index': {'ok': False, 'fix': None}}
        self.assertEqual(tags(sb_health.fix(ok)), [])
        self.restart.assert_not_called()
        self.assertFalse(self.state.exists())   # 하루 1회 기회를 쓰지 않았다

    def test_ollama_started_now_then_restart_backfills(self) -> None:
        with patch.object(sb_health, 'uses_ollama', return_value=True), \
                patch.object(sb_health, 'ensure_ollama', return_value=True), \
                patch.object(sb_health, 'ollama_up', return_value=True):
            backlog = self.backlog()
            backlog['vector']['fix'] = 'ollama'
            done = sb_health.fix(backlog)
        self.assertEqual(done, ['ollama:ok', 'vector-backfill:ok'])
        self.restart.assert_called_once()


class RecallSelfTestTests(unittest.TestCase):
    def db(self, rows):
        import sqlite3
        con = sqlite3.connect(':memory:')
        con.execute('CREATE TABLE observations (id INTEGER, project TEXT, title TEXT, created_at_epoch INTEGER)')
        con.executemany('INSERT INTO observations VALUES (?,?,?,?)', rows)
        return con

    def test_server_down_is_a_fixable_failure(self) -> None:
        import sb_recalld
        with patch.object(sb_recalld, 'is_up', return_value=False):
            r = sb_health.check_recall(self.db([]))
        self.assertFalse(r['ok'])
        self.assertEqual(r['fix'], 'recalld')

    def test_sample_must_come_back(self) -> None:
        import io
        import sb_recalld
        con = self.db([(10, 'work', '거래처 단가 계약 회신 정리 완료', 1_000_000)])

        def fake(ids):
            body = json.dumps({'items': [{'id': i} for i in ids]}).encode('utf-8')
            return io.BytesIO(body)

        with patch.object(sb_recalld, 'is_up', return_value=True), \
                patch.object(sb_recalld, 'superseded_ids', return_value=set()):
            with patch.object(sb_health.urllib.request, 'urlopen', return_value=fake([10, 3])):
                self.assertTrue(sb_health.check_recall(con)['ok'])
            with patch.object(sb_health.urllib.request, 'urlopen', return_value=fake([3, 4])):
                bad = sb_health.check_recall(con)
        self.assertFalse(bad['ok'])
        self.assertIn('#10', bad['msg'])

    def test_usage_counts_recent_events_only(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            logs = Path(home) / 'logs'
            logs.mkdir()
            import time as _t
            now = _t.strftime('%Y-%m-%dT%H:%M:%S')
            (logs / 'recall.jsonl').write_text('\n'.join([
                json.dumps({'ts': now, 'ids': [1], 'hint': True}),
                json.dumps({'ts': now, 'ids': []}),
                json.dumps({'ts': '2020-01-01T00:00:00', 'ids': [2]})]), encoding='utf-8')
            (logs / 'recall-gate.jsonl').write_text(json.dumps({'ts': now, 'decision': 'block'}), encoding='utf-8')
            with patch.dict(os.environ, {'SB_HOME': home}):
                u = sb_health.recall_usage(7)
        self.assertEqual((u['injections'], u['empty'], u['history_hints'], u['gate_block']), (2, 1, 1, 1))


class EnvironmentAwareTests(unittest.TestCase):
    def test_ollama_checked_only_when_installed_for_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with patch.object(sb_health.Path, 'home', return_value=Path(home)):
                os.environ.pop('SB_HEALTH_OLLAMA', None)
                self.assertFalse(sb_health.uses_ollama())        # 기본 임베딩 PC — 경고 대상 아님
                (Path(home) / '.chroma_env').write_text('CHROMA_OPENAI_API_KEY=ollama\n', encoding='utf-8')
                self.assertTrue(sb_health.uses_ollama())
                with patch.dict(os.environ, {'SB_HEALTH_OLLAMA': '0'}):
                    self.assertFalse(sb_health.uses_ollama())

    def test_startup_check_skips_worker_and_recall(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            env = {'SB_HOME': home, 'SB_CLAUDE_MEM_DIR': home, 'SB_CLAUDE_MEM_DB': str(Path(home) / 'none.db'),
                   'SB_HEALTH_OLLAMA': '0'}
            with patch.dict(os.environ, env), \
                    patch.object(sb_health, 'worker_up', side_effect=AssertionError('must not probe worker')), \
                    patch.object(sb_health, 'check_recall', side_effect=AssertionError('must not self-test')):
                r = sb_health.run_checks(worker=False, recall=False)
        self.assertNotIn('worker', r)
        self.assertNotIn('ollama', r)


class NightlyOrderTests(unittest.TestCase):
    def test_pii_runs_before_preflight_and_can_be_turned_off(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            mask = Path(d) / 'pii_mask.py'
            mask.write_text('def mask(s):\n    return s\n')
            with patch.dict(os.environ, {'SB_PII_MASK': str(mask)}):
                os.environ.pop('SB_NIGHTLY_PII', None)
                names = [s.name for s in nightly.build_stages()]
                os.environ['SB_NIGHTLY_PII'] = '0'
                try:
                    off = [s.name for s in nightly.build_stages()]
                finally:
                    os.environ.pop('SB_NIGHTLY_PII', None)
            with patch.dict(os.environ, {'SB_PII_MASK': '0'}):
                masked_off = [s.name for s in nightly.build_stages()]
        self.assertNotIn('pii', off)
        self.assertNotIn('pii', masked_off)
        # 마스킹 모듈이 있으면 기존처럼 돈다. 단 워커를 죽이므로 반드시 워커 재기동(preflight) 앞
        self.assertEqual(names[:3], ['pii', 'preflight', 'ko-index'])
        self.assertLess(names.index('preflight'), names.index('automemory'))
        self.assertEqual(names[-1], 'health')


class SaveSelfHealTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix='sb-save-heal-')
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {'SB_HOME': tmp.name, 'SB_PII_MASK': '0'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('SB_MEM_BASE_URL', None)
        patch.object(sb_memory, 'already_saved', return_value=False).start()
        patch.object(sb_memory.time, 'sleep').start()
        self.addCleanup(patch.stopall)

    def prov(self):
        return sb_memory.build_provenance(source='t', kind='manual', origin='t', created_by='t', content_hash='h')

    def test_connection_refused_starts_worker_then_saves(self) -> None:
        refused = urllib.error.URLError(ConnectionRefusedError(10061, 'refused'))
        with patch.object(sb_memory, '_post', side_effect=[refused, (200, '{"success":true,"id":7}')]), \
                patch.object(sb_health, 'ensure_worker', return_value=True) as ensure:
            self.assertEqual(sb_memory.save_memory('본문', '제목', 'proj', self.prov()), 7)
        ensure.assert_called_once()

    def test_explicit_base_url_never_autostarts(self) -> None:
        refused = urllib.error.URLError(ConnectionRefusedError(10061, 'refused'))
        with patch.object(sb_memory, '_post', side_effect=[refused, (200, '{"success":true,"id":8}')]), \
                patch.object(sb_health, 'ensure_worker') as ensure:
            sb_memory.save_memory('본문', '제목', 'proj', self.prov(), base_url='http://127.0.0.1:1')
        ensure.assert_not_called()

    def test_other_errors_do_not_autostart(self) -> None:
        with patch.object(sb_memory, '_post', side_effect=[(500, 'x'), (200, '{"success":true,"id":9}')]), \
                patch.object(sb_health, 'ensure_worker') as ensure:
            sb_memory.save_memory('본문', '제목', 'proj', self.prov())
        ensure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
