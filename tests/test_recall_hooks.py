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


class RecallGateTest(unittest.TestCase):
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

    def test_korean_payload_is_read_as_utf8(self):
        out = self.gate(hook_event_name='PreToolUse', tool_name='AskUserQuestion',
                        tool_input={'questions': [{'question': '이번달 한 것 정리할까요?'}]})
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')


class RecallPromptTest(unittest.TestCase):
    def test_time_pattern(self):
        sys.path.insert(0, str(KIT / 'hooks'))
        import sb_recall
        for q in ('이번달에 한것들 정리해줘봐', '지난주에 뭐 했었지', '그거 어떻게 됐지?', '9/10 브리핑 다시'):
            self.assertTrue(sb_recall.TIME_PAT.search(q), q)
        for q in ('시트 서식 바꿔줘', '이 함수 리팩터링해줘'):
            self.assertFalse(sb_recall.TIME_PAT.search(q), q)


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
