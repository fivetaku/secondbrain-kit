"""recall_gate / sb_recall / sb_timeline 회귀 테스트 (2026-09-26 윈도우 실사용에서 발견된 결함)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
GATE = KIT / 'hooks' / 'recall_gate.py'
sys.path.insert(0, str(KIT / 'bin'))
import sb_timeline  # noqa: E402


def run_hook(script, payload, env_extra=None):
    env = dict(os.environ)
    # 윈도우 기본 코드페이지를 흉내 — 훅이 스스로 UTF-8로 읽어야 한다
    env.pop('PYTHONIOENCODING', None)
    env.pop('PYTHONUTF8', None)
    env.update(env_extra or {})
    res = subprocess.run([sys.executable, str(script)], input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
    out = res.stdout.decode('utf-8')
    return json.loads(out) if out.strip() else None


class _GateHarness:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {'SB_RECALL_GATE_DIR': self.tmp.name}
        self.sid = 'test-%d' % time.time_ns()

    def tearDown(self):
        self.tmp.cleanup()

    def needs(self):
        import hashlib
        p = Path(self.tmp.name) / (hashlib.sha256(self.sid.encode()).hexdigest() + '.needs')
        p.touch()
        return p

    def gate(self, **kw):
        kw.setdefault('session_id', self.sid)
        return run_hook(GATE, kw, self.env)

    def transcript(self, *events):
        p = Path(self.tmp.name) / 'transcript.jsonl'
        with open(p, 'w', encoding='utf-8') as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + '\n')
        return str(p)

    @staticmethod
    def user(text):
        return {'type': 'user', 'message': {'role': 'user', 'content': text}}

    @staticmethod
    def tool(name, **inp):
        return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': name, 'input': inp}]}}


class RecallGateTest(_GateHarness, unittest.TestCase):
    def test_subagent_start_injects_usage(self):
        out = self.gate(hook_event_name='SubagentStart', agent_type='general-purpose')
        ctx = out['hookSpecificOutput']['additionalContext']
        self.assertEqual(out['hookSpecificOutput']['hookEventName'], 'SubagentStart')
        self.assertIn('sb timeline', ctx)
        self.assertIn('get_observations', ctx)

    def test_stop_blocks_once_when_needed_and_not_recalled(self):
        self.needs()
        out = self.gate(hook_event_name='Stop', stop_hook_active=False)
        self.assertEqual(out['decision'], 'block')
        self.assertIsNone(self.gate(hook_event_name='Stop', stop_hook_active=False))  # 프롬프트당 1회

    def test_stop_passes_when_stop_hook_active(self):
        self.needs()
        self.assertIsNone(self.gate(hook_event_name='Stop', stop_hook_active=True))

    def test_stop_passes_without_needs(self):
        self.assertIsNone(self.gate(hook_event_name='Stop', stop_hook_active=False))

    def test_powershell_recall_counts(self):
        self.needs()
        time.sleep(0.05)
        self.gate(hook_event_name='PostToolUse', tool_name='PowerShell',
                  tool_input={'command': "sb timeline --since 2026-09-01"})
        self.assertIsNone(self.gate(hook_event_name='Stop', stop_hook_active=False))

    def test_recall_before_new_prompt_does_not_count(self):
        self.gate(hook_event_name='PostToolUse', tool_name='Bash', tool_input={'command': "sb search 'x'"})
        time.sleep(0.05)
        self.needs()  # 회상 이후 새 프롬프트
        out = self.gate(hook_event_name='Stop', stop_hook_active=False)
        self.assertEqual(out['decision'], 'block')

    def test_stop_reads_transcript_recall_after_last_prompt(self):
        self.needs()
        path = self.transcript(self.user('지난주 N 작업'), self.tool('Bash', command="sb recall '작업'"))
        self.assertIsNone(self.gate(hook_event_name='Stop', transcript_path=path))

    def test_stop_ignores_recall_before_last_prompt(self):
        self.needs()
        path = self.transcript(self.tool('Bash', command="sb recall 'x'"), self.user('새 질문'),
                               self.tool('Bash', command='ls'))
        self.assertEqual(self.gate(hook_event_name='Stop', transcript_path=path)['decision'], 'block')

    def test_tool_results_are_not_treated_as_new_prompt(self):
        self.needs()
        result = {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}}
        path = self.transcript(self.user('질문'), self.tool('mcp__plugin_claude-mem_mcp-search__get_observations', ids=[1]),
                               result)
        self.assertIsNone(self.gate(hook_event_name='Stop', transcript_path=path))

    def test_ask_passes_when_session_recalled_in_transcript(self):
        path = self.transcript(self.user('a'), self.tool('PowerShell', command='sb search --mode current'),
                               self.user('b'))
        self.assertIsNone(self.gate(hook_event_name='PreToolUse', tool_name='AskUserQuestion',
                                    tool_input={}, transcript_path=path))

    def test_korean_payload_is_read_as_utf8(self):
        out = self.gate(hook_event_name='PreToolUse', tool_name='AskUserQuestion',
                        tool_input={'questions': [{'question': '이번달 한 것 정리할까요?'}]})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')


class RecallRunTest(_GateHarness, unittest.TestCase):
    """2026-10-21 '회상은 실행이어야 한다' — recall 이라는 글자를 언급만 한 명령은 회상이 아니다."""

    MENTIONS = ('cat hooks/recall_gate.py', 'grep -rn recall hooks/', 'ls ~/.claude-mem', 'grep -n "sb search" README.md',
                'echo sb search', 'vim bin/sb_search.py', 'python -m py_compile bin/sb_search.py', 'cat bin/loops.py',
                'git commit -m "sb search 개선"', 'python3 -c "import sb_search"', 'sb health', 'python bin/loops.py add --title x')
    RUNS = ('sb search --mode current', "sb recall '배포'", 'sb timeline --since 2026-09-01', 'sb state head', 'sb loops list',
            "cd /x && sb search 'y' --global", 'SB_HOME=/tmp/s sb search x', 'out=$(sb recall x)', 'sb.cmd search x',
            '& "C:\\Users\\u\\bin\\sb.cmd" recall x', 'python bin/sb_search.py --mode current',
            '.venv/bin/python ~/kit/bin/sb_search.py q', 'py -3 bin/sb.py search q', 'C:\\Py\\python.exe C:\\kit\\bin\\loops.py list',
            'python3 bin/loops.py list', 'sqlite3 -readonly ~/.claude-mem/claude-mem.db "SELECT 1"', 'echo a\nsb recall x')
    # 2026-10-30: 부분 문자열 방식(main)에선 셌는데 실행 판정에서 빠지던 꼴 — 공백 든 따옴표 경로와 래퍼 접두
    RUNS_QUOTED_WRAPPED = (
        'python "C:\\Users\\a b\\secondbrain-kit\\bin\\sb_search.py" --mode current',
        "python3 '/home/a b/kit/bin/sb_search.py' q", "'/opt/my kit/bin/sb' search x",
        '"C:\\Program Files\\Python312\\python.exe" "C:\\a b\\bin\\loops.py" list',
        'timeout 30 sb search x', 'timeout -k 5 30s sb recall x', 'env SB_HOME=/tmp/s sb search x',
        'env -u SB_STATE_BRIEFING sb search --mode current', 'time sb timeline --since 2026-10-01',
        'uv run python bin/sb_search.py q', 'uv run --project kit bin/sb_search.py q', 'nohup sb state head',
        '/usr/bin/env python3 bin/sb_search.py q', 'SB_HOME="/tmp/a b" sb search x', 'echo "$(sb search x)"',
        'cd "/x y" && timeout 10 python "/x y/bin/sb_search.py" q', 'echo "a; b" ; sb search y',
        'git commit -m "$(cat <<\'EOF\'\nmsg\nEOF\n)" && sb search x')
    # 따옴표 안 문자열·커밋 메시지 속 줄바꿈 뒤 'sb search' 는 명령 자리가 아니다
    MENTIONS_QUOTED = (
        'echo "a; sb search x"', "echo 'x | sb recall y'", 'git commit -m "fix\n\nsb search 개선"',
        'git commit -m "$(cat <<\'EOF\'\nfeat: "따옴표" 포함\n\nsb search 개선\nEOF\n)"',
        'echo "sqlite3 ~/.claude-mem/claude-mem.db"', 'timeout 30 cat bin/sb_search.py', 'uv run pytest -k sb_search',
        "python -c 'import sb_search; sb_search.main()'", 'env | grep sb_search.py')

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(KIT / 'hooks'))
        import recall_gate
        cls.rg = recall_gate

    def test_command_classification(self):
        for cmd in self.MENTIONS:
            self.assertFalse(self.rg.is_recall_command(cmd), cmd)
        for cmd in self.RUNS:
            self.assertTrue(self.rg.is_recall_command(cmd), cmd)

    def test_quoted_paths_and_wrappers_count(self):
        for cmd in self.RUNS_QUOTED_WRAPPED:
            self.assertTrue(self.rg.is_recall_command(cmd), cmd)

    def test_quoted_strings_and_commit_messages_do_not_count(self):
        for cmd in self.MENTIONS_QUOTED:
            self.assertFalse(self.rg.is_recall_command(cmd), cmd)

    def test_interpreter_options_with_values_count(self):
        # 2026-10-30: 값을 따로 받는 인터프리터 옵션(-X utf8·-W ignore) 뒤의 스크립트 — 윈도우에서 흔한 꼴
        for cmd in ('python -X utf8 C:\\kit\\bin\\sb_search.py q', 'py -3 -W ignore -X dev bin\\sb_search.py --mode current',
                    '& "C:\\Py\\python.exe" -X utf8 "C:\\a b\\bin\\sb_search.py" q',
                    'python3 -W ignore::DeprecationWarning bin/loops.py list'):
            self.assertTrue(self.rg.is_recall_command(cmd), cmd)
            self.assertTrue(self.rg.is_recall_command(cmd, powershell=True), cmd)
        for cmd in ('python -X utf8 -m py_compile bin/sb_search.py', 'python -X utf8 -c "import sb_search"'):
            self.assertFalse(self.rg.is_recall_command(cmd), cmd)

    def test_powershell_backslash_is_not_escape(self):
        # PowerShell 은 백틱이 이스케이프다 — 끝이 \ 인 따옴표 경로 뒤 명령을 따옴표 안으로 오판하지 않는다
        cmd = 'Set-Location "C:\\Users\\a b\\"; sb search x'
        self.assertTrue(self.rg.is_recall_command(cmd, powershell=True))
        self.assertTrue(self.rg._is_recall_tool('PowerShell', {'command': cmd}))
        self.assertFalse(self.rg._is_recall_tool('Bash', {'command': cmd}))   # bash 에선 \" 가 이스케이프라 따옴표 안
        for cmd in ('echo "a`"; sb search x"', 'echo a`; sb search x', 'echo "`sb search x"', 'echo a `sb search x'):
            self.assertFalse(self.rg.is_recall_command(cmd, powershell=True), cmd)
        self.assertTrue(self.rg.is_recall_command('echo "$(sb search x)"', powershell=True))

    def test_bash_backslash_escape_outside_quotes(self):
        self.assertTrue(self.rg._is_recall_tool('Bash', {'command': "echo don\\'t; sb search x"}))
        for cmd in ('echo don\\\'t "; sb search x"', 'echo a\\; sb search x'):
            self.assertFalse(self.rg.is_recall_command(cmd), cmd)

    def test_powershell_quoted_path_marker_unblocks_ask(self):
        self.gate(hook_event_name='PostToolUse', tool_name='PowerShell',
                  tool_input={'command': 'cd "C:\\Users\\a b\\"; sb search --mode current'})
        self.assertIsNone(self.ask())

    def test_many_quoted_candidates_stay_fast(self):
        # 후보마다 따옴표 상태를 처음부터 다시 훑으면 제곱 시간 — 이어서 센다
        start = time.monotonic()
        self.assertFalse(self.rg.is_recall_command('echo "' + '; sb search ' * 20000 + '"'))
        self.assertLess(time.monotonic() - start, 2.0)

    def test_repeated_interpreter_options_stay_fast(self):
        # '-X -X …' 를 두 옵션 대안이 나눠 먹으면 역추적이 지수로 는다(토큰마다 약 1.6배) — 값은 '-' 로 시작하지 못한다
        start = time.monotonic()
        self.assertFalse(self.rg.is_recall_command('python ' + '-X ' * 40 + 'foo.py'))
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(self.rg.is_recall_command('python -X utf8 -W ignore bin/sb_search.py q'))

    def test_claude_mem_mcp_tools_count(self):
        for name in ('mcp__plugin_claude-mem_mcp-search__search', 'mcp__plugin_claude-mem_mcp-search__get_observations',
                     'mcp__claude_mem__timeline'):
            self.assertTrue(self.rg._is_recall_tool(name, {}), name)
        self.assertFalse(self.rg._is_recall_tool('Read', {'file_path': 'hooks/recall_gate.py'}))

    def ask(self, path=None):
        kw = {'transcript_path': path} if path else {}
        return self.gate(hook_event_name='PreToolUse', tool_name='AskUserQuestion', tool_input={}, **kw)

    def test_mention_marker_does_not_unblock_ask(self):
        self.gate(hook_event_name='PostToolUse', tool_name='Bash', tool_input={'command': 'cat hooks/recall_gate.py'})
        self.assertEqual(self.ask()['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_run_marker_unblocks_ask(self):
        self.gate(hook_event_name='PostToolUse', tool_name='Bash', tool_input={'command': "sb recall '배포'"})
        self.assertIsNone(self.ask())

    def test_mention_in_transcript_does_not_unblock_ask(self):
        path = self.transcript(self.user('a'), self.tool('Bash', command='grep -rn recall hooks/'),
                               self.tool('Bash', command='cat bin/sb_search.py'), self.user('b'))
        self.assertEqual(self.ask(path)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_run_in_transcript_unblocks_ask(self):
        path = self.transcript(self.user('a'), self.tool('Bash', command='python bin/loops.py list'), self.user('b'))
        self.assertIsNone(self.ask(path))

    def test_stop_blocks_when_transcript_only_mentions_recall(self):
        self.needs()
        path = self.transcript(self.user('지난주 뭐 했었지?'), self.tool('Bash', command='ls ~/.claude-mem'))
        self.assertEqual(self.gate(hook_event_name='Stop', transcript_path=path)['decision'], 'block')


class RecalldTimeoutTest(unittest.TestCase):
    """2026-10-20 재순위기 도입 뒤 질의당 0.4~0.9초 — recalld 대기 기본 3초, SB_RECALLD_TIMEOUT 으로 조정."""

    def timeout_used(self, value):
        import io
        from unittest import mock
        sys.path.insert(0, str(KIT / 'hooks'))
        import sb_recall
        import sb_recalld
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(url, timeout=None):
            seen['timeout'] = timeout
            return Resp(json.dumps({'db': sb_recalld.db_path(), 'gate': {'pass': False}, 'items': []}).encode('utf-8'))

        with mock.patch.dict(os.environ, {}), mock.patch.object(sb_recall.urllib.request, 'urlopen', fake):
            os.environ.pop('SB_RECALLD_TIMEOUT', None)
            if value is not None:
                os.environ['SB_RECALLD_TIMEOUT'] = value
            self.assertEqual(sb_recall.recalld_search('배포 스크립트 고쳐줘', 'p'), [])
        return seen['timeout']

    def test_default_is_three_seconds(self):
        self.assertEqual(self.timeout_used(None), 3.0)

    def test_env_override(self):
        self.assertEqual(self.timeout_used('0.8'), 0.8)

    def test_invalid_env_falls_back(self):
        for bad in ('abc', '0', '-1', ''):
            self.assertEqual(self.timeout_used(bad), 3.0, bad)


class RecallPromptTest(unittest.TestCase):
    def test_time_pattern(self):
        sys.path.insert(0, str(KIT / 'hooks'))
        import sb_recall
        for q in ('이번달에 한것들 정리해줘봐', '지난주에 뭐 했었지', '그거 어떻게 됐지?', '9/10 브리핑 어떻게 했었지?'):
            self.assertTrue(sb_recall.is_history_question(q), q)
        # 2026-10-05: 날짜만 있는 요청('9/10 브리핑 다시')은 기간·이력 질문이 아니다(오탐 9/10건의 주원인)
        for q in ('시트 서식 바꿔줘', '이 함수 리팩터링해줘', '9/10 브리핑 다시'):
            self.assertFalse(sb_recall.is_history_question(q), q)


class TimelineTest(unittest.TestCase):
    def test_work_date_prefers_session_date(self):
        meta = json.dumps({'kind': 'import', 'extra': {'session_date': '2026-09-02'}})
        self.assertEqual(sb_timeline.work_date('[2026-09-03] x', '2026-09-26T07:00:00Z', meta), '2026-09-02')
        self.assertEqual(sb_timeline.work_date('[2026-09-03·완료] x', '2026-09-26T07:00:00Z', '{}'), '2026-09-03')
        self.assertEqual(sb_timeline.work_date('plain', '2026-09-26T07:00:00Z', None), '2026-09-26')

    def test_clean_title(self):
        self.assertEqual(sb_timeline.clean_title('[2026-09-01·완료] 회신 발송'), '[완료] 회신 발송')
        self.assertEqual(sb_timeline.clean_title('[2026-09-01] 회신'), '회신')
        self.assertEqual(sb_timeline.clean_title('no date'), 'no date')


if __name__ == '__main__':
    unittest.main()


class StopGateScopeTest(unittest.TestCase):
    """Stop 게이트 표식(.needs)은 기간·이력 질문에만 남는다 — 회수 목록만 붙은 프롬프트는 되돌리지 않는다."""

    def _run(self, prompt):
        import hashlib
        import io
        from unittest import mock
        sys.path.insert(0, str(KIT / 'hooks'))
        import sb_recall
        with tempfile.TemporaryDirectory() as gate, tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {'SB_RECALL_GATE_DIR': gate}), \
                mock.patch.object(sb_recall, 'STATE_DIR', state), \
                mock.patch.object(sb_recall, 'gate', return_value=True), \
                mock.patch.object(sb_recall, 'search', return_value=[{'id': 1}]), \
                mock.patch.object(sb_recall, 'enrich', return_value=[{'id': 1, 'date': '09-01', 'title': 't'}]), \
                mock.patch('sys.stdin', io.StringIO(json.dumps({'prompt': prompt, 'session_id': 's'}))), \
                mock.patch('sys.stdout', io.StringIO()):
            sb_recall.main()
            return (Path(gate) / (hashlib.sha256(b's').hexdigest() + '.needs')).exists()

    def test_plain_prompt_with_recall_items_sets_no_marker(self):
        self.assertFalse(self._run('배포 스크립트 고쳐줘'))

    def test_period_question_sets_marker(self):
        self.assertTrue(self._run('이번달에 한 거 정리해줘'))
