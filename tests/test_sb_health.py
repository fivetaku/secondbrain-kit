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


class NightlyOrderTests(unittest.TestCase):
    def test_pii_is_opt_in_and_runs_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            mask = Path(d) / 'pii_mask.py'
            mask.write_text('def mask(s):\n    return s\n')
            with patch.dict(os.environ, {'SB_PII_MASK': str(mask)}):
                os.environ.pop('SB_NIGHTLY_PII', None)
                names = [s.name for s in nightly.build_stages()]
                self.assertNotIn('pii', names)          # 마스킹 파일이 있어도 기본은 끔
                os.environ['SB_NIGHTLY_PII'] = '1'
                try:
                    names = [s.name for s in nightly.build_stages()]
                finally:
                    os.environ.pop('SB_NIGHTLY_PII', None)
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
