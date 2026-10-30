import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import xml.dom.minidom
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inst = load('sbkit_install', 'installer/install.py')
trust = load('sbkit_codex_trust', 'installer/codex_trust.py')
doctor = load('sbkit_doctor', 'installer/doctor.py')
sys.path.insert(0, str(ROOT / 'hooks'))
import codex_hook  # noqa: E402


class StripOursTest(unittest.TestCase):
    def test_strips_current_and_moved_kit_keeps_others(self):
        vpy = inst.posix(inst.VPY)
        hooks = {'SessionStart': [
            {'hooks': [{'type': 'command', 'command': inst.py_cmd('hooks/session_context.py')}]},
            # 키트 폴더를 옮기기 전의 옛 항목
            {'hooks': [{'type': 'command', 'command': '"%s" "/old/place/kit/hooks/sb_recall.py"' % vpy}]},
            {'hooks': [{'type': 'command', 'command': 'node /somewhere/else/hooks/sb_recall.py'}]},
            {'matcher': 'x', 'hooks': [{'type': 'command', 'command': 'echo user-hook'}]},
        ]}
        out = inst._strip_ours(hooks)
        cmds = [h['command'] for g in out['SessionStart'] for h in g['hooks']]
        self.assertEqual(cmds, ['node /somewhere/else/hooks/sb_recall.py', 'echo user-hook'])

    def test_reinstall_is_idempotent(self):
        hooks = {}
        for _ in range(2):
            hooks = inst._strip_ours(hooks)
            inst._codex_add(hooks, 'Stop', None, 'hooks/codex_hook.py', ['summarize'], 60)
        self.assertEqual(len(hooks['Stop']), 1)


class ClaudeHooksTest(unittest.TestCase):
    def _install(self, version, existing=None):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / '.claude').mkdir()
            (home / '.claude' / 'settings.json').write_text(json.dumps(existing or {}), encoding='utf-8')
            with mock.patch.object(inst, 'HOME', home), mock.patch.object(inst, 'DRY', False), \
                    mock.patch.object(inst, 'claude_version', return_value=version):
                inst.claude_hooks(None)
            return json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))['hooks']

    def test_failure_hook_only_on_supported_claude(self):
        # 2.1.101 이전은 모르는 훅 이벤트 하나로 settings.json 전체를 무시한다
        self.assertNotIn('PostToolUseFailure', self._install((2, 1, 90)))
        self.assertNotIn('PostToolUseFailure', self._install((0,)))
        self.assertIn('PostToolUseFailure', self._install((2, 1, 306)))

    def test_user_hook_with_same_role_is_not_duplicated(self):
        user = {'hooks': {'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'python3 /x/hooks/sb-recall.py'}]}]}}
        cmds = [h['command'] for g in self._install((2, 1, 306), user)['UserPromptSubmit'] for h in g['hooks']]
        self.assertEqual(cmds, ['python3 /x/hooks/sb-recall.py'])


class HasEquivalentTest(unittest.TestCase):
    def _hooks(self, event, command):
        return {event: [{'hooks': [{'type': 'command', 'command': command}]}]}

    def test_user_hook_with_arguments_is_same_role(self):
        # 마지막 토큰('claude')만 보던 옛 판별은 인자가 붙은 명령을 다른 훅으로 봤다
        ours = inst.py_cmd('hooks/session_context.py', '--harness', 'claude')
        self.assertTrue(inst._has_equivalent(
            self._hooks('SessionStart', 'python3 /x/hooks/session_context.py claude'), 'SessionStart', ours))

    def test_sh_wrapper_is_same_role(self):
        self.assertTrue(inst._has_equivalent(
            self._hooks('Stop', 'bash "/x/hooks/recall_gate.sh"'), 'Stop', inst.py_cmd('hooks/recall_gate.py')))
        self.assertTrue(inst._has_equivalent(
            self._hooks('SessionStart', '/x/session_context.sh claude'), 'SessionStart',
            inst.py_cmd('hooks/session_context.py', '--harness', 'claude')))

    def test_hyphen_name_and_windows_path(self):
        ours = inst.py_cmd('hooks/sb_recall.py')
        self.assertTrue(inst._has_equivalent(self._hooks('UserPromptSubmit', 'python3 /x/sb-recall.py'),
                                             'UserPromptSubmit', ours))
        self.assertTrue(inst._has_equivalent(self._hooks('UserPromptSubmit', '"C:\\Users\\Jo Doe\\h\\sb_recall.py" codex'),
                                             'UserPromptSubmit', ours))

    def test_other_scripts_and_other_events_are_not_same_role(self):
        ours = inst.py_cmd('hooks/session_context.py', '--harness', 'claude')
        # 옛 판별은 우리 명령의 마지막 인자 'claude' 를 스크립트 이름으로 착각했다
        self.assertFalse(inst._has_equivalent(self._hooks('SessionStart', 'python3 /x/claude.py'), 'SessionStart', ours))
        self.assertFalse(inst._has_equivalent(self._hooks('SessionStart', 'node /x/session_context.py.js'),
                                              'SessionStart', ours))
        self.assertFalse(inst._has_equivalent(self._hooks('Stop', 'python3 /x/session_context.py'), 'SessionStart', ours))

    def test_install_does_not_double_register(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / '.claude').mkdir()
            user = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command',
                                                           'command': 'python3 /x/hooks/session_context.py claude'}]}],
                              'Stop': [{'hooks': [{'type': 'command', 'command': 'bash /x/hooks/recall_gate.sh'}]}]}}
            (home / '.claude' / 'settings.json').write_text(json.dumps(user), encoding='utf-8')
            with mock.patch.object(inst, 'HOME', home), mock.patch.object(inst, 'DRY', False), \
                    mock.patch.object(inst, 'claude_version', return_value=(2, 1, 306)):
                for _ in range(2):   # 재설치해도 같다
                    inst.claude_hooks(None)
            hooks = json.loads((home / '.claude' / 'settings.json').read_text(encoding='utf-8'))['hooks']
        self.assertEqual(len(hooks['SessionStart']), 1)
        self.assertEqual(len(hooks['Stop']), 1)
        self.assertEqual(len(hooks['PreToolUse']), 1)   # 사용자 훅이 없는 이벤트엔 키트 훅을 건다
        self.assertTrue(inst._is_ours(hooks['PreToolUse'][0]['hooks'][0]['command']))

    def test_same_script_is_kit_old_form_but_name_only_match_is_loose(self):
        # 같은 .py 이름(경로·인자만 다름)은 키트의 예전 형식, .sh 래퍼 등 이름만 겹치면 다른 도구의 것일 수 있다
        ours = inst.py_cmd('hooks/session_context.py', '--harness', 'claude')
        old = 'python3 /old/hooks/session_context.py claude'
        self.assertEqual(inst._find_equivalent(self._hooks('SessionStart', old), 'SessionStart', ours), (old, True))
        self.assertEqual(inst._find_equivalent(self._hooks('UserPromptSubmit', 'python3 /x/sb-recall.py'),
                                               'UserPromptSubmit', inst.py_cmd('hooks/sb_recall.py')),
                         ('python3 /x/sb-recall.py', True))
        sh = 'bash ~/tool/session_context.sh'
        self.assertEqual(inst._find_equivalent(self._hooks('SessionStart', sh), 'SessionStart', ours), (sh, False))
        both = {'SessionStart': [{'hooks': [{'command': sh}]}, {'hooks': [{'command': old}]}]}
        self.assertEqual(inst._find_equivalent(both, 'SessionStart', ours), (old, True))   # 같은 .py 를 먼저
        self.assertIsNone(inst._find_equivalent({}, 'SessionStart', ours))

    def test_install_log_names_the_loose_user_hook(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / '.claude').mkdir()
            user = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'bash ~/tool/session_context.sh'}]}],
                              'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'python3 /x/sb-recall.py'}]}]}}
            (home / '.claude' / 'settings.json').write_text(json.dumps(user), encoding='utf-8')
            with mock.patch.object(inst, 'HOME', home), mock.patch.object(inst, 'DRY', False), \
                    mock.patch.object(inst, 'LOG', []) as log, mock.patch('sys.stdout'), \
                    mock.patch.object(inst, 'claude_version', return_value=(2, 1, 306)):
                inst.claude_hooks(None)
        warned = [m for m in log if '⚠' in m]
        self.assertEqual(len(warned), 1, log)
        self.assertIn('⚠ 사용자 훅 bash ~/tool/session_context.sh 을 같은 역할로 보고 건너뜀', warned[0])
        self.assertIn('SessionStart', warned[0])
        # 키트의 예전 형식(같은 .py)은 기존처럼 ⚠ 없이 건너뛴다
        self.assertTrue(any('건너뜀: UserPromptSubmit' in m for m in log), log)


class ChromaPinTest(unittest.TestCase):
    def test_chroma_uvx_pins_pydantic(self):
        i = inst.CHROMA_UVX.index('pydantic<2.14')
        self.assertEqual(inst.CHROMA_UVX[i - 1], '--with')
        self.assertEqual(inst.CHROMA_UVX[-2:], ['--from', 'chroma-mcp==0.2.6'])

    def test_worker_path_gets_pinned_tool(self):
        # 워커는 자기 인자로 uvx 를 부르므로 같은 요구를 만족하는 고정 도구를 깔아 둔다
        with mock.patch.object(inst, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            inst.pin_chroma_mcp({'uv': 'uv'})
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ['uv', 'tool', 'install'])
        self.assertIn('pydantic<2.14', argv)
        self.assertEqual(argv[-1], 'chroma-mcp==0.2.6')
        self.assertNotIn('--from', argv)

    def test_pin_failure_is_warned(self):
        with mock.patch.object(inst, 'run', return_value=subprocess.CompletedProcess([], 2)), \
                mock.patch.object(inst, 'WARNINGS', []) as warnings:
            inst.pin_chroma_mcp({'uv': 'uv'})
        self.assertEqual(len(warnings), 1)

    def test_doctor_uses_worker_args_which_the_pinned_tool_satisfies(self):
        # 워커 인자 = 설치기 고정 인자에서 --python·pydantic 고정을 뺀 것. 어긋나면 uvx 가 고정 도구를 재사용하지 않는다
        i = inst.CHROMA_UVX.index('pydantic<2.14')
        self.assertEqual(doctor.WORKER_CHROMA_ARGS[:-1], inst.CHROMA_UVX[2:i - 1] + inst.CHROMA_UVX[i + 1:])
        self.assertEqual(doctor.WORKER_CHROMA_ARGS[-1], 'chroma-mcp')

    def _doctor_chroma(self, rc, stderr):
        res = subprocess.CompletedProcess([], rc, '', stderr)
        with mock.patch.object(doctor, 'rows', []) as rows, \
                mock.patch.object(doctor.sb_config, 'claude_mem_settings', return_value={}), \
                mock.patch.object(doctor.shutil, 'which', return_value='/bin/uvx'), \
                mock.patch.object(doctor.subprocess, 'run', return_value=res) as sp:
            doctor.check_chroma_mcp()
        return rows[0], sp.call_args

    def test_doctor_flags_pydantic_import_error(self):
        err = ("ImportError: cannot import name 'eval_type_backport' from 'pydantic._internal._typing_extra'")
        (level, name, detail), call = self._doctor_chroma(1, err)
        self.assertEqual(level, '❌')
        self.assertIn('pydantic', detail)
        argv = call[0][0]
        self.assertEqual(argv[:3], ['/bin/uvx', '--python', '3.13'])
        self.assertEqual(argv[-2:], ['chroma-mcp', '--help'])
        self.assertNotIn('VIRTUAL_ENV', call[1]['env'])

    def test_doctor_ok_when_chroma_mcp_starts(self):
        (level, _, _), _ = self._doctor_chroma(0, '')
        self.assertEqual(level, '✅')


class DoctorClaudeEventsTest(unittest.TestCase):
    def test_failure_hook_checked_only_on_supported_claude(self):
        with mock.patch.object(doctor.install, 'claude_version', return_value=(2, 1, 119)):
            self.assertIn('PostToolUseFailure', doctor.claude_events())
        with mock.patch.object(doctor.install, 'claude_version', return_value=(2, 1, 118)):
            self.assertNotIn('PostToolUseFailure', doctor.claude_events())
        with mock.patch.object(doctor.install, 'claude_version', return_value=(0,)):
            self.assertNotIn('PostToolUseFailure', doctor.claude_events())
        self.assertEqual(set(doctor.CLAUDE_HOOK_SCRIPTS), set(doctor.claude_events()) | {'PostToolUseFailure'})

    def test_hook_ok_for_kit_or_user_equivalent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'settings.json'
            p.write_text(json.dumps({'hooks': {
                'PostToolUseFailure': [{'hooks': [{'command': doctor.install.py_cmd('hooks/sb_error_recall.py')}]}],
                'UserPromptSubmit': [{'hooks': [{'command': 'python3 /x/sb-recall.py'}]}]}}), encoding='utf-8')
            self.assertEqual(doctor._claude_hook_status(p, 'PostToolUseFailure'), ('✅', ''))
            # 설치기가 키트의 예전 형식(같은 .py)으로 보고 건너뛴 경우
            self.assertEqual(doctor._claude_hook_status(p, 'UserPromptSubmit'), ('✅', 'python3 /x/sb-recall.py'))
            self.assertEqual(doctor._claude_hook_status(p, 'SessionStart')[0], '❌')

    def _check_claude(self, hooks):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / '.claude').mkdir()
            (home / '.claude' / 'settings.json').write_text(json.dumps({'hooks': hooks}), encoding='utf-8')
            with mock.patch.object(doctor, 'rows', []) as rows, mock.patch.object(doctor, 'HOME', home), \
                    mock.patch.object(doctor.shutil, 'which', return_value='/bin/claude'), \
                    mock.patch.object(doctor, 'check_recall_smoke'), \
                    mock.patch.object(doctor.install, 'claude_version', return_value=(2, 1, 306)):
                doctor.check_claude()
        return next(r for r in rows if r[1] == 'Claude 훅')

    def _kit_hooks(self, **override):
        hooks = {e: [{'hooks': [{'command': doctor.install.py_cmd(s)}]}] for e, s in doctor.CLAUDE_HOOK_SCRIPTS.items()}
        hooks.update({e: [{'hooks': [{'command': c}]}] for e, c in override.items()})
        return hooks

    def test_check_claude_warns_when_only_a_name_alike_user_hook_fills_in(self):
        self.assertEqual(self._check_claude(self._kit_hooks())[0], '✅')
        # 키트의 예전 형식(같은 .py, 경로·인자만 다름)은 기존처럼 ✅
        self.assertEqual(self._check_claude(self._kit_hooks(UserPromptSubmit='python3 /x/sb-recall.py'))[0], '✅')
        # 다른 도구의 같은 이름 훅(.sh)으로만 채워지면 ⚠ 와 그 명령
        level, _, detail = self._check_claude(self._kit_hooks(SessionStart='bash ~/tool/session_context.sh'))
        self.assertEqual(level, '⚠')
        self.assertIn('SessionStart(bash ~/tool/session_context.sh)', detail)
        # 누락이 섞이면 ❌ 가 우선, ⚠ 내용도 함께
        hooks = self._kit_hooks(SessionStart='bash ~/tool/session_context.sh')
        hooks.pop('Stop')
        level, _, detail = self._check_claude(hooks)
        self.assertEqual(level, '❌')
        self.assertIn('누락: Stop', detail)
        self.assertIn('session_context.sh', detail)


@unittest.skipIf(os.name == 'nt' or not shutil.which('git') or not shutil.which('sh'), 'POSIX sh·git 필요')
class BootstrapUpdateTest(unittest.TestCase):
    """bootstrap.sh 갱신 경로를 임시 원격(bare)·클론으로 실측한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = dict(os.environ, HOME=str(self.d), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')
        self.pub, self.remote, self.kit = self.d / 'pub', self.d / 'remote.git', self.d / 'kit'
        self.git(None, 'init', '-q', '-b', 'main', str(self.pub))
        (self.pub / 'install.sh').write_text('#!/bin/sh\necho INSTALL-RAN\n', encoding='utf-8')
        (self.pub / 'install.sh').chmod(0o755)   # 실제 저장소처럼 100755 — 아니면 bootstrap 의 chmod 가 변경으로 잡힌다
        (self.pub / 'a.txt').write_text('v1\n', encoding='utf-8')
        self.git(self.pub, 'add', '-A')
        self.git(self.pub, 'commit', '-q', '-m', 'c1')
        self.git(None, 'clone', '-q', '--bare', str(self.pub), str(self.remote))
        self.git(self.pub, 'remote', 'add', 'origin', str(self.remote))

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, cwd, *args):
        cmd = ['git'] + (['-C', str(cwd)] if cwd else []) + list(args)
        return subprocess.run(cmd, env=self.env, check=True, capture_output=True, text=True).stdout.strip()

    def bootstrap(self, fail_status=False):
        env = dict(self.env, SBKIT_REPO=self.remote.as_uri(), SBKIT_DIR=str(self.kit))
        if fail_status:   # 'git status' 만 실패하는 git 을 PATH 앞에 둔다 — 나머지 명령은 진짜 git 으로
            fake = self.d / 'fakebin'
            fake.mkdir(exist_ok=True)
            (fake / 'git').write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = status ] && { echo "fatal: simulated" >&2; '
                                      'exit 128; }; done\nexec "%s" "$@"\n' % shutil.which('git'), encoding='utf-8')
            (fake / 'git').chmod(0o755)
            env['PATH'] = str(fake) + os.pathsep + env.get('PATH', '')
        return subprocess.run(['sh', str(ROOT / 'bootstrap.sh')], env=env, capture_output=True, text=True)

    def publish(self, amend=False, name='b.txt'):
        (self.pub / name).write_text(name + '\n', encoding='utf-8')
        self.git(self.pub, 'add', '-A')
        self.git(self.pub, 'commit', '-q', '-m', 'rewrite' if amend else 'next', *(['--amend'] if amend else []))
        self.git(self.pub, 'push', '-q', '-f', 'origin', 'main')

    def backups(self):
        return [b for b in self.git(self.kit, 'branch', '--format=%(refname:short)').split() if b.startswith('sbkit-backup-')]

    def test_revlist_failure_changes_nothing(self):
        # 로컬 커밋 판정(rev-list)이 실패하면 백업 없이 reset --hard 하지 말고 그대로 멈춘다(ps1 의 throw 와 같게).
        # 작업 트리의 'origin/main' 파일이 rev-list 만 모호성으로 실패시킨다 — merge 는 경로를 안 보고,
        # 예전 코드는 그 파일을 stash 로 치운 뒤 reset 이 성공해 로컬 커밋이 어느 브랜치에도 남지 않았다.
        self.assertEqual(self.bootstrap().returncode, 0)
        (self.kit / 'local.txt').write_text('local\n', encoding='utf-8')
        self.git(self.kit, 'add', 'local.txt')
        self.git(self.kit, 'commit', '-q', '-m', 'my-local-commit')
        local = self.git(self.kit, 'rev-parse', 'HEAD')
        self.publish(amend=True, name='c.txt')
        (self.kit / 'origin').mkdir()
        (self.kit / 'origin' / 'main').write_text('note\n', encoding='utf-8')
        r = self.bootstrap()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('아무것도 바꾸지 않고 멈춥니다', r.stderr)
        self.assertNotIn('INSTALL-RAN', r.stdout)
        self.assertEqual(self.git(self.kit, 'rev-parse', 'HEAD'), local)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.git(self.kit, 'stash', 'list'), '')
        self.assertEqual((self.kit / 'origin' / 'main').read_text(encoding='utf-8'), 'note\n')

    def test_status_failure_changes_nothing(self):
        # 미커밋 변경 판정(git status)이 실패해도 '변경 없음'으로 읽어 stash 없이 reset --hard 하지 말고 멈춘다.
        self.assertEqual(self.bootstrap().returncode, 0)
        head = self.git(self.kit, 'rev-parse', 'HEAD')
        (self.kit / 'a.txt').write_text('mine\n', encoding='utf-8')
        self.publish(amend=True, name='c.txt')   # 빨리감기가 안 되게 원격 이력을 다시 쓴다
        r = self.bootstrap(fail_status=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('git status 실패', r.stderr)
        self.assertNotIn('INSTALL-RAN', r.stdout)
        self.assertEqual((self.kit / 'a.txt').read_text(encoding='utf-8'), 'mine\n')
        self.assertEqual(self.git(self.kit, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.git(self.kit, 'stash', 'list'), '')

    def test_update_paths(self):
        r = self.bootstrap()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('INSTALL-RAN', r.stdout)
        # 정상 빨리감기
        self.publish(name='b.txt')
        r = self.bootstrap()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git(self.kit, 'rev-parse', 'HEAD'), self.git(self.remote, 'rev-parse', 'main'))
        self.assertEqual(self.backups(), [])
        # (a) 받기 실패(네트워크) — 로컬 변경을 stash 하거나 reset 하지 않고 지금 체크아웃으로 설치
        (self.kit / 'a.txt').write_text('mine\n', encoding='utf-8')
        self.git(self.kit, 'remote', 'set-url', 'origin', (self.d / 'missing.git').as_uri())
        r = self.bootstrap()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('최신본을 받지 못했습니다', r.stderr)
        self.assertIn('INSTALL-RAN', r.stdout)
        self.assertEqual((self.kit / 'a.txt').read_text(encoding='utf-8'), 'mine\n')
        self.assertEqual(self.git(self.kit, 'stash', 'list'), '')
        # (b) 로컬 커밋 + 원격 이력 다시 쓰기 — 로컬 커밋은 백업 브랜치, 미커밋 변경은 stash, 그 뒤 origin/main
        self.git(self.kit, 'remote', 'set-url', 'origin', self.remote.as_uri())
        (self.kit / 'local.txt').write_text('local\n', encoding='utf-8')
        self.git(self.kit, 'add', 'local.txt')
        self.git(self.kit, 'commit', '-q', '-m', 'my-local-commit')
        self.publish(amend=True, name='c.txt')
        r = self.bootstrap()
        self.assertEqual(r.returncode, 0, r.stderr)
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertIn(backups[0], r.stdout)   # 안내에 실제 브랜치 이름
        self.assertIn('my-local-commit', self.git(self.kit, 'log', '--format=%s', backups[0]))
        self.assertEqual(self.git(self.kit, 'rev-parse', 'HEAD'), self.git(self.remote, 'rev-parse', 'main'))
        self.assertIn('sbkit-update-', self.git(self.kit, 'stash', 'list'))
        self.assertEqual(self.git(self.kit, 'status', '--porcelain'), '')
        # 로컬 커밋 없이 이력만 다시 쓰이면 백업 브랜치를 만들지 않는다
        self.publish(amend=True, name='d.txt')
        r = self.bootstrap()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.backups()), 1)
        self.assertNotIn('sbkit-backup-', r.stdout)
        self.assertEqual(self.git(self.kit, 'rev-parse', 'HEAD'), self.git(self.remote, 'rev-parse', 'main'))


class SafetyTest(unittest.TestCase):
    def test_sibling_folder_is_not_ours(self):
        other = inst.posix(inst.KIT) + '-archive/hooks/sb_recall.py'
        self.assertFalse(inst._is_ours('python "%s"' % other))

    def test_corrupt_json_aborts_instead_of_overwriting(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'settings.json'
            f.write_text('{"hooks": {', encoding='utf-8')
            with self.assertRaises(SystemExit):
                inst.read_json(f)
            self.assertEqual(f.read_text(encoding='utf-8'), '{"hooks": {')
            self.assertEqual(inst.read_json(Path(d) / 'missing.json'), {})

    def test_write_json_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(inst, 'DRY', False):
            f = Path(d) / 'settings.json'
            inst.write_json(f, {'a': '한글'})
            self.assertEqual(json.loads(f.read_text(encoding='utf-8')), {'a': '한글'})
            self.assertEqual([x.name for x in Path(d).iterdir()], ['settings.json'])

    def test_dry_run_uninstall_runs_no_commands(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(inst, 'HOME', Path(d)), mock.patch.object(inst, 'DRY', True), \
                mock.patch.object(inst.subprocess, 'run') as sp:
            inst.uninstall()
        sp.assert_not_called()


class CodexAutomationCacheTest(unittest.TestCase):
    def test_missing_transcript_is_retried_not_cached(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(codex_hook, 'CACHE', Path(d) / 'cache'), \
                mock.patch.dict(codex_hook.os.environ, {}, clear=False):
            for k in ('SB_CODEX_CAPTURE_EXEC', 'CLAUDE_MEM_INTERNAL', 'SB_CODEX_SKIP'):
                codex_hook.os.environ.pop(k, None)
            tp = Path(d) / 'rollout.jsonl'
            payload = {'session_id': 's1', 'transcript_path': str(tp)}
            self.assertFalse(codex_hook.is_automation(payload))  # 아직 로그 없음
            tp.write_text(json.dumps({'type': 'session_meta', 'payload': {'originator': 'codex_exec'}}) + '\n')
            self.assertTrue(codex_hook.is_automation(payload))   # 다음 이벤트에서 재판정
            tp.write_text('')
            self.assertTrue(codex_hook.is_automation(payload))   # 확정 판정은 캐시


class HookOutputEncodingTest(unittest.TestCase):
    def test_session_context_output_is_ascii(self):
        sc = load('sbkit_session_context', 'hooks/session_context.py')
        out = sc.emit('claude', '한글 🗂 브리핑')
        out.encode('ascii')
        self.assertEqual(json.loads(out)['hookSpecificOutput']['additionalContext'], '한글 🗂 브리핑')


class CodexWindowsCommandTest(unittest.TestCase):
    def test_backslash_unquoted_without_spaces(self):
        with mock.patch.object(inst, 'VPY', Path('C:/Users/me/.secondbrain/.venv/Scripts/python.exe')), \
                mock.patch.object(inst, 'KIT', Path('C:/Users/me/secondbrain-kit')):
            hooks = {}
            inst._codex_add(hooks, 'Stop', None, 'hooks/codex_hook.py', ['summarize'], 60)
        win = hooks['Stop'][0]['hooks'][0]['commandWindows']
        self.assertNotIn('/', win)
        self.assertNotIn('"', win)
        self.assertTrue(win.endswith('codex_hook.py summarize'))

    def test_quoted_when_path_has_space(self):
        with mock.patch.object(inst, 'VPY', Path('C:/Users/John Doe/.secondbrain/.venv/Scripts/python.exe')), \
                mock.patch.object(inst, 'KIT', Path('C:/Users/John Doe/secondbrain-kit')):
            hooks = {}
            inst._codex_add(hooks, 'Stop', None, 'hooks/codex_hook.py', ['summarize'], 60)
        win = hooks['Stop'][0]['hooks'][0]['commandWindows']
        self.assertTrue(win.startswith('"C:\\Users\\John Doe\\'))
        self.assertEqual(win.count('"'), 4)


class LaunchdPlistTest(unittest.TestCase):
    def test_plist_escapes_paths(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            with mock.patch.object(inst, 'HOME', home), \
                    mock.patch.object(inst, 'KIT', home / 'a&b <kit>'), \
                    mock.patch.object(inst, 'SB_HOME', home / 'sb&home'), \
                    mock.patch.object(inst, 'DRY', False), \
                    mock.patch.object(inst, 'run'), \
                    mock.patch.object(inst.subprocess, 'run'), \
                    mock.patch.object(inst.os, 'getuid', return_value=501, create=True):
                inst._launchd('nightly', ['bin/nightly.py'], {'hour': 5, 'minute': 7})
            plist = home / 'Library/LaunchAgents/com.secondbrain-kit.nightly.plist'
            doc = xml.dom.minidom.parse(str(plist))  # 잘못된 XML 이면 예외
            strings = [n.firstChild.data for n in doc.getElementsByTagName('string') if n.firstChild]
            self.assertIn(str(home / 'a&b <kit>' / 'bin/nightly.py'), strings)


class AppServerTimeoutTest(unittest.TestCase):
    def test_call_times_out_when_server_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / 'fake_codex.py'
            fake.write_text(textwrap.dedent('''
                import sys, json, time
                line = sys.stdin.readline()
                rid = json.loads(line)['id']
                print(json.dumps({'id': rid, 'result': {}}), flush=True)
                time.sleep(30)
            '''))
            real_popen = subprocess.Popen
            with mock.patch.object(trust.subprocess, 'Popen',
                                   side_effect=lambda argv, **kw: real_popen(
                                       [sys.executable, str(fake)], **kw)):
                srv = trust.AppServer('codex')
            try:
                import time
                t = time.time()
                with self.assertRaises(RuntimeError):
                    srv.call('hooks/list', {}, timeout=1)
                self.assertLess(time.time() - t, 5)
            finally:
                srv.close()


if __name__ == '__main__':
    unittest.main()
