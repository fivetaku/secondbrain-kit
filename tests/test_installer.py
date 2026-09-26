import importlib.util
import json
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
