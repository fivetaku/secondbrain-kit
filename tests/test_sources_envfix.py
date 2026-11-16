"""다른 PC 보고서(11-16 binna-work) 반영: 관측기 세션 제외, Antigravity 대화 감지."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bin'))
import sb_sources  # noqa: E402


def write(p, text='{}\n'):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')


class EnvFixTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(prefix='sbenv-')
        self.h = Path(self.t.name)

    def tearDown(self):
        self.t.cleanup()

    def by_kind(self):
        return {s['kind']: s for s in sb_sources.detect_sessions(str(self.h))}

    def test_observer_sessions_excluded_and_reported(self):
        proj = self.h / '.claude' / 'projects'
        write(proj / '-Users-x-work-alpha' / 'a.jsonl', json.dumps({'cwd': '/Users/x/work/alpha'}) + '\n')
        for i in range(3):
            write(proj / '-Users-x--claude-mem-observer-sessions' / ('o%d.jsonl' % i))
        write(proj / 'C--Users-x--claude-mem-work-view' / 'w.jsonl')
        s = self.by_kind()['claude_sessions']['summary']
        self.assertEqual((s['files'], s['excluded_observer'], s['cwd_count']), (1, 4, 1))

    def test_antigravity_counted_without_bodies(self):
        conv = self.h / '.gemini' / 'antigravity' / 'conversations'
        write(conv / 'c1.pb', 'secret body')
        write(conv / 'sub' / 'c2.json', '{"text": "secret"}')
        k = self.by_kind()
        self.assertIn('antigravity_sessions', k)
        a = k['antigravity_sessions']
        self.assertEqual((a['summary']['files'], a['mode'], a['summary']['cwd_count']), (2, 'skip', 0))
        self.assertNotIn('secret', json.dumps(a))

    def test_no_antigravity_dir_no_entry(self):
        write(self.h / '.gemini' / 'tmp' / 'x' / 'chats' / 'a.json')
        self.assertNotIn('antigravity_sessions', self.by_kind())


if __name__ == '__main__':
    unittest.main()


class DoctorRemoteSyncTests(unittest.TestCase):
    def run_check(self, settings):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location('doctor_envfix', str(ROOT / 'installer' / 'doctor.py'))
        d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(d)
        rows = []
        with mock.patch.object(d.sb_config, 'claude_mem_settings', return_value=settings), \
                mock.patch.object(d, 'rec', side_effect=lambda lvl, name, msg: rows.append((lvl, name, msg))):
            d.check_settings()
        return rows

    def test_filled_remote_keys_warn_without_values(self):
        rows = self.run_check({'CLAUDE_MEM_MODE': 'code--ko', 'CLAUDE_MEM_CLOUD_SYNC_TOKEN': 'tok-SECRET',
                               'CLAUDE_MEM_PRO_MEMORY_KEY': 'k-SECRET', 'CLAUDE_MEM_PRO_MEMORY_MODEL': ''})
        warn = [r for r in rows if r[1] == 'claude-mem 원격 동기화']
        self.assertEqual(len(warn), 1)
        self.assertIn('2개', warn[0][2])
        self.assertNotIn('SECRET', warn[0][2])

    def test_empty_remote_keys_no_warning(self):
        rows = self.run_check({'CLAUDE_MEM_MODE': 'code--ko', 'CLAUDE_MEM_CLOUD_SYNC_TOKEN': ''})
        self.assertFalse([r for r in rows if r[1] == 'claude-mem 원격 동기화'])
