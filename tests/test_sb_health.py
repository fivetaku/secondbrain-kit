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

    def test_ensure_worker_respects_test_env(self) -> None:
        with patch.object(sb_health, 'worker_up', return_value=False), \
                patch.object(sb_health.subprocess, 'run') as run:
            self.assertFalse(sb_health.ensure_worker(wait=0))
        run.assert_not_called()


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
