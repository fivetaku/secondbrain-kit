import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import sb_scope  # noqa: E402


class ScopeResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for p in ('a/shared', 'b/shared', 'proj/sub/deep'):
            (self.root / p).mkdir(parents=True)

    def test_same_name_folders_get_different_scope(self):
        a = sb_scope.resolve_scope_id(str(self.root / 'a/shared'), aliases={}, use_git=False)
        b = sb_scope.resolve_scope_id(str(self.root / 'b/shared'), aliases={}, use_git=False)
        self.assertNotEqual(a[0], b[0])
        self.assertEqual(a[1], 'realpath')

    def test_alias_path_wins_and_covers_subfolders(self):
        aliases = {'proj': [str(self.root / 'proj')]}
        sid, method, _ = sb_scope.resolve_scope_id(str(self.root / 'proj/sub/deep'), aliases=aliases, use_git=False)
        self.assertEqual((sid, method), ('proj', 'alias-path'))

    def test_longest_prefix_alias_wins(self):
        aliases = {'proj': [str(self.root / 'proj')], 'sub': [str(self.root / 'proj/sub')]}
        sid, _, _ = sb_scope.resolve_scope_id(str(self.root / 'proj/sub/deep'), aliases=aliases, use_git=False)
        self.assertEqual(sid, 'sub')

    def test_name_alias_not_applied_when_ambiguous(self):
        aliases = {'x': ['shared'], 'y': ['shared']}
        _, method, _ = sb_scope.resolve_scope_id(str(self.root / 'a/shared'), aliases=aliases, use_git=False)
        self.assertEqual(method, 'realpath')

    def test_loop_matches_scope_no_basename_fallback(self):
        self.assertTrue(sb_scope.loop_matches_scope('demo-proj', 'demo-proj'))
        self.assertTrue(sb_scope.loop_matches_scope('demo_proj', 'demo-proj'))
        self.assertFalse(sb_scope.loop_matches_scope('shared', '/private/tmp/a/shared'))
        self.assertFalse(sb_scope.loop_matches_scope(None, 'demo-proj'))

    def test_default_aliases_file_lives_under_sb_home_config(self):
        home = self.root / 'sbhome'
        (home / 'config').mkdir(parents=True)
        (home / 'config' / 'project_aliases.json').write_text(
            json.dumps({'proj': [str(self.root / 'proj')]}), encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if k != 'SB_PROJECT_ALIASES'}
        env['SB_HOME'] = str(home)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(sb_scope.aliases_path(), str(home / 'config' / 'project_aliases.json'))
            sid, method, _ = sb_scope.resolve_scope_id(str(self.root / 'proj/sub'), use_git=False)
        self.assertEqual((sid, method), ('proj', 'alias-path'))

    def test_alias_path_does_not_match_sibling_prefix(self):
        (self.root / 'proj-other').mkdir()
        aliases = {'proj': [str(self.root / 'proj')]}
        _, method, _ = sb_scope.resolve_scope_id(str(self.root / 'proj-other'), aliases=aliases, use_git=False)
        self.assertEqual(method, 'realpath')

    def test_alias_path_comparison_uses_normcase(self):
        # Windows 의 normcase(소문자화)를 흉내 — 대소문자만 다른 별칭 경로도 같은 scope 로 귀속돼야 한다
        upper = str(self.root / 'proj').upper() if os.name == 'nt' else str(self.root) + os.sep + 'PROJ'
        aliases = {'proj': [upper]}
        with mock.patch.object(sb_scope.os.path, 'normcase', side_effect=lambda p: p.lower()), \
                mock.patch.object(sb_scope.os.path, 'realpath', side_effect=lambda p: os.path.abspath(p)):
            sid, method, _ = sb_scope.resolve_scope_id(str(self.root / 'proj/sub'), aliases=aliases, use_git=False)
        self.assertEqual((sid, method), ('proj', 'alias-path'))

    def test_windows_drive_alias_is_a_path_entry_not_a_name(self):
        self.assertTrue(sb_scope.is_path_entry('/abs/path'))
        self.assertTrue(sb_scope.is_path_entry('~/work'))
        self.assertFalse(sb_scope.is_path_entry('legacy-name'))
        if os.name == 'nt':
            self.assertTrue(sb_scope.is_path_entry('C:\\work\\proj'))

    def test_require_scope_rejects_empty(self):
        with self.assertRaises(ValueError):
            sb_scope.require_scope('')
        with self.assertRaises(ValueError):
            sb_scope.require_scope(None)
        self.assertEqual(sb_scope.require_scope('global'), 'global')

    def test_normalize_git_url(self):
        n = sb_scope.normalize_git_url
        self.assertEqual(n('git@github.com:Org/Repo.git'), 'github.com/org/repo')
        self.assertEqual(n('https://github.com/org/repo.git'), 'github.com/org/repo')
        self.assertEqual(n('ssh://git@github.com/org/repo'), 'github.com/org/repo')


if __name__ == '__main__':
    unittest.main()
