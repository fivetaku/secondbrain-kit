"""키트가 기본 위치(~/secondbrain-kit)가 아닌 곳에 있어도 동작하는지 — 격리 HOME 아래 다른 폴더로 복사한 키트를
자식 프로세스로 돌린다. 볼트 이전 판처럼 vault.json 이 없는 상태에서 4·5단계·sb vault status 가 예외 없이 끝나는지도 본다.
실제 doctor 는 uvx·네트워크를 쓰므로 1단계 행은 가짜로 바꿔 끼운다."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DRIVER = textwrap.dedent('''
    import json, sys
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import sb_setup, sb_vault, sb_health, sb_claude_mem
    out = {'kit': str(sb_setup.KIT), 'vault_before': sb_vault.vault_json_path().exists()}
    doctor = sb_setup._load_doctor()
    out['doctor_kit'] = str(doctor.KIT)
    sb_setup._doctor_rows = lambda: [('❌', 'claude-mem 설정', '없음')]
    out['stage1'] = sb_setup.stage1({})
    out['status'] = sb_vault.status()
    out['stage5'] = sb_setup.stage5({})
    doc = sb_setup.load()
    out['stage4'] = sb_setup.stage4({}, doc)
    out['vault_after'] = sb_vault.vault_json_path().exists()
    sb_claude_mem.installed_version = lambda: '99.0.0'
    out['health'] = sb_health.check_claude_mem_version()
    print(json.dumps(out, ensure_ascii=False))
''')


class OtherKitLocationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sb-kitloc-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / 'home'
        self.sbhome = self.home / '.secondbrain'
        (self.sbhome / 'config').mkdir(parents=True)
        # 기본 위치(<홈>/secondbrain-kit)에는 아무것도 두지 않는다 — 거기를 가정하면 실패해야 한다
        self.kit = self.home / 'Workspace' / 'secondbrain-kit'
        shutil.copytree(ROOT, self.kit, ignore=shutil.ignore_patterns('.git', 'tests', 'eval', 'docs', '__pycache__', '*.pyc'))
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('CI', 'SB_NO_SETUP', 'SB_LOOPS_PATH', 'SB_VAULT_LLM_URL', 'SB_MEM_BASE_URL')}
        self.env.update({'HOME': str(self.home), 'SB_HOME': str(self.sbhome), 'CLAUDE_CONFIG_DIR': str(self.home / '.claude'),
                         'SB_CLAUDE_MEM_DIR': str(self.home / '.claude-mem'), 'SB_VAULT_LLM': 'off',
                         'SB_PROJECT_ALIASES': str(self.sbhome / 'config' / 'project_aliases.json'),
                         'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})

    def _py(self, args, timeout=60):
        return subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding='utf-8',
                              timeout=timeout, env=self.env, cwd=str(self.root), stdin=subprocess.DEVNULL)

    def test_kit_resolves_from_its_own_folder(self):
        r = self._py(['-c', DRIVER, str(self.kit / 'bin')])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertFalse((self.home / 'secondbrain-kit').exists())
        self.assertEqual(out['kit'], str(self.kit))
        self.assertEqual(out['doctor_kit'], str(self.kit))
        # 1단계 안내는 실제 키트 위치의 설치기를 가리킨다(상대 경로 'installer/install.py' 를 남기지 않는다)
        hint = out['stage1']['hint']
        self.assertEqual(out['stage1']['verdict'], 'stop')
        self.assertIn(str(self.kit / 'installer' / 'install.py'), hint)
        self.assertNotIn('python installer/', hint)
        self.assertIn(str(self.kit / 'installer' / 'install.py'), out['health']['msg'])

    def test_missing_vault_json_goes_to_defaults(self):
        r = self._py(['-c', DRIVER, str(self.kit / 'bin')])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertFalse(out['vault_before'])
        self.assertEqual((out['status']['configured'], out['status']['default'], out['status']['scopes']), (False, False, 0))
        self.assertIsNone(out['status']['last_apply'])
        self.assertIn(out['stage5']['verdict'], ('pending', 'pass'))     # 주입 기록 없음 → 판정 대기, 오류 아님
        self.assertNotEqual([m['name'] for m in out['stage5']['metrics']], ['error'])
        self.assertEqual(out['stage4']['verdict'], 'pass')                # 기록 없음 → 기본 볼트
        self.assertTrue(out['vault_after'])

    def test_vault_status_cli_without_vault_json(self):
        r = self._py([str(self.kit / 'bin' / 'sb.py'), 'vault', 'status'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('없음', r.stdout)
        r = self._py([str(self.kit / 'bin' / 'sb.py'), 'vault', 'status', '--json'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(json.loads(r.stdout)['configured'])
        self.assertFalse((self.sbhome / 'config' / 'vault.json').exists())   # status 는 읽기만 한다

    def test_doctor_codex_trust_hint_points_to_kit(self):
        """doctor 의 Codex 훅 신뢰 ❌ 안내도 복사본 위치의 codex_trust.py 를 가리킨다(상대 경로 'installer/' 없음).
        codex·app-server 는 가짜로 바꿔 끼운다(미신뢰 훅 1개)."""
        drv = textwrap.dedent('''
            import importlib.util, json, sys, types
            from pathlib import Path
            kit = Path(sys.argv[1])
            spec = importlib.util.spec_from_file_location('sbkit_doctor_loc', str(kit / 'installer' / 'doctor.py'))
            d = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(d)
            d.shutil.which = lambda n: '/bin/codex'
            d._has_kit_hook = lambda p, e: True
            cmd = '"py" "%s/hooks/sb_recall.py"' % str(d.KIT).replace('\\\\', '/')
            d.subprocess.run = lambda *a, **k: types.SimpleNamespace(
                stdout=json.dumps([{'command': cmd, 'trustStatus': 'untrusted'}]), stderr='')
            d.check_codex()
            print(json.dumps([r for r in d.rows if r[1] == 'Codex 훅 신뢰'], ensure_ascii=False))
        ''')
        r = self._py(['-c', drv, str(self.kit)], timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], '❌')
        self.assertIn(str(self.kit / 'installer' / 'codex_trust.py'), rows[0][2])
        self.assertNotIn('python installer/', rows[0][2])

    def test_install_done_message_points_to_kit(self):
        """설치 완료 문구의 --doctor 안내가 복사본 위치의 install.py 를 가리킨다. 설치 단계는 모두 빈 함수로 바꿔 끼운다."""
        drv = textwrap.dedent('''
            import importlib.util, sys
            from pathlib import Path
            kit = Path(sys.argv[1])
            spec = importlib.util.spec_from_file_location('sbkit_install_loc', str(kit / 'installer' / 'install.py'))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            for name in ('check_prereqs', 'setup_venv', 'install_claude_mem', 'pin_chroma_mcp', 'setup_embedding',
                         'merge_claude_mem_settings', 'claude_hooks', 'codex_setup', 'rules', 'sb_launcher',
                         'schedule', 'vault_onboarding', 'setup_stage'):
                setattr(m, name, lambda *a, **k: {})
            del m.WARNINGS[:]
            sys.argv = ['install.py', '--no-vault', '--no-setup']
            m.main()
        ''')
        r = self._py(['-c', drv, str(self.kit)], timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('완료. 확인: python "%s" --doctor' % (self.kit / 'installer' / 'install.py'), r.stdout)

    def test_setup_run_non_interactive_from_other_location(self):
        """복사본의 sb_setup.py 로 CLI 전체 경로 — 1단계(가짜 ❌)에서 멈추고, 입력을 기다리지 않는다."""
        drv = textwrap.dedent('''
            import sys
            sys.path.insert(0, sys.argv[1])
            import sb_setup
            sb_setup._doctor_rows = lambda: [('❌', 'venv', '없음')]
            sys.exit(sb_setup.main(['run', '--non-interactive', '--json']))
        ''')
        r = self._py(['-c', drv, str(self.kit / 'bin')], timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out['stopped_at'], out['verdict']), (1, 'stop'))
        self.assertIn(str(self.kit / 'installer' / 'install.py'), out['hint'])


if __name__ == '__main__':
    unittest.main()
