import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / 'bin'
HOOK = Path(os.environ.get('SB_SESSION_HOOK') or ROOT / 'hooks' / 'session_context.py')


class SetActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'loops.jsonl'
        self.env = mock.patch.dict(os.environ, {
            'SB_HOME': str(Path(self.temp.name) / 'sbhome'), 'PYTHONIOENCODING': 'utf-8',
            'SB_STATE_DB': str(Path(self.temp.name) / 'no-state.db'),
            'SB_CLAUDE_MEM_DIR': str(Path(self.temp.name) / 'no-claude-mem'),
            'SB_LOOPS_PATH': str(self.path), 'LOOPS_DIR': self.temp.name,
            'PYTHONDONTWRITEBYTECODE': '1',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.a = self._add('트랙A', 'A 첫 행동')
        self.b = self._add('트랙B', 'B 첫 행동')

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(BIN / 'loops.py')] + list(args),
                              env=os.environ.copy(), capture_output=True, text=True, encoding='utf-8', timeout=20)

    def _add(self, title, action):
        r = self.cli('add', title, '--project', 'demo-proj', '--action', action)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().split(': ')[1]

    def ledger(self):
        return {l['id']: l for l in map(json.loads, self.path.read_text(encoding='utf-8').splitlines()) if l}

    def test_updates_open_loop(self):
        r = self.cli('set-action', self.a, 'A 두번째')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ledger()[self.a]['next_action'], 'A 두번째')
        self.assertIn('action_updated_at', self.ledger()[self.a])

    def test_rejects_empty_action(self):
        r = self.cli('set-action', self.a, '   ')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.ledger()[self.a]['next_action'], 'A 첫 행동')

    def test_expected_action_conflict(self):
        r = self.cli('set-action', self.a, 'X', '--expected-action', '구값')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('충돌', r.stderr)
        ok = self.cli('set-action', self.a, 'X', '--expected-action', 'A 첫 행동')
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_rejects_closed_loop(self):
        self.assertEqual(self.cli('close', self.a).returncode, 0)
        r = self.cli('set-action', self.a, 'Y')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('done', r.stderr)

    def test_two_tracks_preserved_and_close_is_independent(self):
        self.cli('set-action', self.a, 'A 갱신')
        self.assertEqual(self.cli('close', self.a).returncode, 0)
        led = self.ledger()
        self.assertEqual(led[self.b]['status'], 'open')
        self.assertEqual(led[self.b]['next_action'], 'B 첫 행동')
        self.assertEqual(led[self.a]['status'], 'done')

    def _hook(self, cwd, env):
        return subprocess.run([sys.executable, str(HOOK), '--harness', 'codex'], cwd=str(cwd), env=env,
                              input=json.dumps({'cwd': str(cwd)}), capture_output=True, text=True,
                              encoding='utf-8', timeout=20)

    @unittest.skipUnless(HOOK.exists(), 'session hook missing')
    def test_session_context_shows_both_tracks_with_id_action_review(self):
        aliases = Path(self.temp.name) / 'aliases.json'
        proj = Path(self.temp.name) / 'demo-dir'
        proj.mkdir()
        aliases.write_text(json.dumps({'demo-proj': [str(proj)]}), encoding='utf-8')
        env = {**os.environ, 'SB_PROJECT_ALIASES': str(aliases)}
        r = self._hook(proj, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        ctx = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
        for needle in (self.a, self.b, 'A 첫 행동', 'B 첫 행동', '다음 노출'):
            self.assertIn(needle, ctx)
        # 동명 폴더(다른 경로)는 이 scope 미결을 받지 않는다 (출력이 없거나, 있어도 이 미결은 없다)
        other = Path(self.temp.name) / 'other' / 'demo-dir'
        other.mkdir(parents=True)
        r2 = self._hook(other, env)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertNotIn(self.a, r2.stdout)


if __name__ == '__main__':
    unittest.main()
