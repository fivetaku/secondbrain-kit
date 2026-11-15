"""M3-T2 scope 표기 통일 — hooks/sb_recall.py 의 hook_project·norm_project 와 refine 비교."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / 'bin'))
sys.path.insert(0, str(KIT / 'hooks'))
import sb_recall  # noqa: E402


class _FakeHome(unittest.TestCase):
    """가짜 홈(tempfile) — 실제 사용자 홈·별칭 파일을 읽지 않는다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.home = root / 'Users' / 'kimuser'
        self.home.mkdir(parents=True)
        (self.home / 'proj_a').mkdir()
        self.aliases = root / 'project_aliases.json'
        env = {'HOME': str(self.home), 'USERPROFILE': str(self.home), 'USER': 'kimuser',
               'USERNAME': 'kimuser', 'LOGNAME': 'kimuser', 'SB_HOME': str(root / 'sb'),
               'SB_PROJECT_ALIASES': str(self.aliases)}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop('SB_SCOPE_NORM', None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class NormProjectTest(_FakeHome):
    def test_home_forms_share_one_key(self):
        forms = ['~', '~/', str(self.home), str(self.home) + '/', 'kimuser', 'KimUser',
                 '/Users/kimuser', '/users/kimuser', 'C:\\Users\\kimuser', '/home/kimuser']
        self.assertEqual({sb_recall.norm_project(f) for f in forms}, {sb_recall.HOME_KEY})

    def test_other_users_home_path_is_home_key(self):
        # 다른 PC 의 홈 폴더 경로형 꼬리표도 '홈' — 이름만 있는 다른 사용자명은 그냥 이름이다
        self.assertEqual(sb_recall.norm_project('/Users/someoneelse'), sb_recall.HOME_KEY)
        self.assertEqual(sb_recall.norm_project('someoneelse'), 'someoneelse')

    def test_case_and_separator_variants(self):
        keys = {sb_recall.norm_project(x) for x in ('Second_Brain', 'second-brain', 'SECOND-BRAIN', ' second_brain ')}
        self.assertEqual(len(keys), 1)
        self.assertNotEqual(sb_recall.norm_project('secondbrain'), sb_recall.norm_project('second-brain-kit'))

    def test_path_tag_matches_folder_name(self):
        self.assertEqual(sb_recall.norm_project('/Users/kimuser/aitop100-agent'), sb_recall.norm_project('aitop100-agent'))
        self.assertEqual(sb_recall.norm_project('D:\\work\\Proj_A'), sb_recall.norm_project('proj-a'))
        self.assertEqual(sb_recall.norm_project('~/proj_a'), sb_recall.norm_project('proj-a'))

    def test_empty(self):
        for v in ('', None, '   ', '/'):
            self.assertEqual(sb_recall.norm_project(v), '')

    def test_off_switch(self):
        os.environ['SB_SCOPE_NORM'] = '0'
        self.assertEqual(sb_recall.norm_project('Second_Brain'), 'Second_Brain')
        self.assertEqual(sb_recall.norm_project('~'), '~')


class HookProjectTest(_FakeHome):
    def test_default_is_folder_name(self):
        self.assertEqual(sb_recall.hook_project(str(self.home / 'proj_a')), 'proj_a')
        self.assertEqual(sb_recall.hook_project(str(self.home / 'proj_a') + os.sep), 'proj_a')
        self.assertEqual(sb_recall.hook_project(''), '')
        self.assertEqual(sb_recall.hook_project(None), '')

    def test_home_is_single_key(self):
        for cwd in (str(self.home), str(self.home) + os.sep, '~'):
            p = sb_recall.hook_project(cwd)
            self.assertEqual(p, 'kimuser', cwd)   # 색인에 쌓인 홈 꼬리표 표기(정확 일치 질의용)
            self.assertEqual(sb_recall.norm_project(p), sb_recall.HOME_KEY)

    def test_windows_style_cwd(self):
        self.assertEqual(sb_recall.hook_project('C:\\Users\\kimuser\\Proj_A'), 'Proj_A')

    def test_alias_path_wins(self):
        self.aliases.write_text(json.dumps({'team-proj': [str(self.home / 'proj_a')], 'legacy': ['proj_a']}),
                                encoding='utf-8')
        self.assertEqual(sb_recall.hook_project(str(self.home / 'proj_a')), 'team-proj')
        # 같은 경로만 — 하위 폴더나 이름 별칭으로는 바꾸지 않는다
        sub = self.home / 'proj_a' / 'sub'
        sub.mkdir()
        self.assertEqual(sb_recall.hook_project(str(sub)), 'sub')

    def test_broken_alias_file_falls_back(self):
        self.aliases.write_text('{not json', encoding='utf-8')
        self.assertEqual(sb_recall.hook_project(str(self.home / 'proj_a')), 'proj_a')

    def test_off_switch(self):
        os.environ['SB_SCOPE_NORM'] = '0'
        self.aliases.write_text(json.dumps({'team-proj': [str(self.home / 'proj_a')]}), encoding='utf-8')
        self.assertEqual(sb_recall.hook_project(str(self.home / 'proj_a')), 'proj_a')
        self.assertEqual(sb_recall.hook_project(str(self.home)), 'kimuser')


class RefineScopeTest(_FakeHome):
    def items(self):
        return [
            {'id': 1, 'project': '/Users/kimuser', 'title': '무관한 제목', '_d': '2026-11-01'},
            {'id': 2, 'project': 'other', 'title': '무관한 제목', '_d': '2026-11-10'},
            {'id': 3, 'project': 'Second_Brain', 'title': '다른 표기', '_d': '2026-11-02'},
        ]

    def test_home_tag_forms_are_same_project(self):
        with mock.patch.object(sb_recall, 'superseded_ids', return_value=set()):
            out = sb_recall.refine(self.items(), '배포 스크립트 고쳐줘', 'kimuser')
        self.assertEqual([it['id'] for it in out], [1])   # 홈 기록은 남고, 공유 단어 없는 다른 프로젝트는 빠진다

    def test_name_variant_sorted_first(self):
        items = self.items()
        items[1]['title'] = '배포 스크립트 정리'   # 공유 단어 2개 → 다른 프로젝트여도 남는다
        with mock.patch.object(sb_recall, 'superseded_ids', return_value=set()):
            out = sb_recall.refine(items, '배포 스크립트 고쳐줘', 'second-brain')
        self.assertEqual([it['id'] for it in out][0], 3)
        self.assertIn(2, [it['id'] for it in out])

    def test_off_switch_keeps_exact_compare(self):
        os.environ['SB_SCOPE_NORM'] = '0'
        with mock.patch.object(sb_recall, 'superseded_ids', return_value=set()):
            out = sb_recall.refine(self.items(), '배포 스크립트 고쳐줘', 'kimuser')
        self.assertEqual(out, [])


class HookMainProjectTest(_FakeHome):
    """훅 main 이 hook_project 로 정한 project 를 recalld 에 넘긴다(홈 cwd 에서 홈 꼬리표)."""

    def test_main_passes_home_key(self):
        seen = {}

        def fake_recalld(prompt, project):
            seen['project'] = project
            return []

        state = Path(self.tmp.name) / 'state'
        with mock.patch.object(sb_recall, 'recalld_search', fake_recalld), \
                mock.patch.object(sb_recall, 'BACKEND', 'recalld'), \
                mock.patch.object(sb_recall, 'STATE_DIR', str(state)), \
                mock.patch.object(sb_recall, 'correction_note', lambda *a: ''), \
                mock.patch.object(sb_recall, 'recall_log', lambda e: None), \
                mock.patch.object(sb_recall.sys, 'stdin') as stdin, \
                mock.patch.object(sb_recall.sys, 'argv', ['sb_recall.py']):
            stdin.read.return_value = json.dumps({'prompt': '배포 스크립트 어디 있는지 찾아줘', 'session_id': 't2',
                                                  'cwd': str(self.home) + os.sep})
            sb_recall.main()
        self.assertEqual(seen['project'], 'kimuser')


class HookSubprocessTest(_FakeHome):
    def test_hook_runs_fail_open_with_shadow_getattr(self):
        # 그림자 도구는 getattr(hook, 'hook_project'/'norm_project') 로 쓴다 — 모듈 최상위에 있어야 한다
        self.assertTrue(callable(getattr(sb_recall, 'hook_project', None)))
        self.assertTrue(callable(getattr(sb_recall, 'norm_project', None)))
        env = dict(os.environ, SB_RECALL='0')
        res = subprocess.run([sys.executable, str(KIT / 'hooks' / 'sb_recall.py')],
                             input=json.dumps({'prompt': '배포 스크립트 어디 있는지 찾아줘', 'cwd': str(self.home)}).encode(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
        self.assertEqual(res.returncode, 0)


if __name__ == '__main__':
    unittest.main()
