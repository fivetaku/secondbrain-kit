"""hooks/sb_error_recall.py — 도구 실패 시 같은 오류를 다룬 과거 기록 회수(2026-10-21 도입, 직접 테스트가 없던 공백)."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parent.parent
SCRIPT = KIT / 'hooks' / 'sb_error_recall.py'
sys.path.insert(0, str(KIT / 'hooks'))
import sb_error_recall  # noqa: E402

ERR = "Exit code 1\nTraceback: ModuleNotFoundError: No module named 'kiwipiepy' in /opt/kit/bin/sb_fts_ko.py line 42"
ITEMS = [{'id': 11, 'title': 'kiwipiepy 설치 누락 — venv 재생성으로 해결', 'date': '2026-10-01', 'project': 'kit'},
         {'id': 12, 'title': '형태소 색인 import 오류', 'date': '2026-09-20', 'project': 'other-project-long-name'},
         {'id': 13, 'title': '세 번째', 'date': '2026-09-01', 'project': 'kit'}]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ErrorRecallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log = self.root / 'error_recall.jsonl'
        for name, value in (('STATE_DIR', str(self.root / 'state')), ('LOG', str(self.log))):
            p = mock.patch.object(sb_error_recall, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {'SB_RECALLD_URL': 'http://127.0.0.1:9'})
        env.start()
        self.addCleanup(env.stop)
        for k in ('SB_ERROR_RECALL', 'SB_ERROR_RECALL_AB', 'SB_ERROR_RECALL_ARM', 'CLAUDE_MEM_INTERNAL'):
            os.environ.pop(k, None)
        self.urls = []
        self.response = {'gate': {'pass': True}, 'items': ITEMS}

    def fake_urlopen(self, url, timeout=None):
        self.urls.append(url)
        return _Resp(json.dumps(self.response).encode('utf-8'))

    def run_hook(self, payload):
        out = io.StringIO()
        with mock.patch.object(sb_error_recall.urllib.request, 'urlopen', self.fake_urlopen), \
                mock.patch('sys.stdin', io.StringIO(json.dumps(payload, ensure_ascii=False))), \
                mock.patch('sys.stdout', out):
            sb_error_recall.main()
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def logs(self):
        return [json.loads(x) for x in self.log.read_text(encoding='utf-8').splitlines()] if self.log.exists() else []

    @staticmethod
    def failure(err=ERR, sid='s1', tool='Bash'):
        return {'hook_event_name': 'PostToolUseFailure', 'session_id': sid, 'tool_name': tool, 'error': err}

    # ── 실험 배정 ────────────────────────────────────────────────
    def test_default_is_always_on(self):
        self.assertEqual({sb_error_recall.arm('sid-%d' % i) for i in range(40)}, {'on'})

    def test_ab_only_when_enabled_and_by_session_hash(self):
        os.environ['SB_ERROR_RECALL_AB'] = '1'
        arms = {}
        for i in range(40):
            sid = 'sid-%d' % i
            arms[sid] = sb_error_recall.arm(sid)
            want = 'on' if int(hashlib.sha256(sid.encode()).hexdigest(), 16) % 2 == 0 else 'control'
            self.assertEqual(arms[sid], want)
            self.assertEqual(sb_error_recall.arm(sid), arms[sid])   # 같은 세션은 늘 같은 쪽
        self.assertEqual(set(arms.values()), {'on', 'control'})

    def test_forced_arm_overrides(self):
        os.environ['SB_ERROR_RECALL_AB'] = '1'
        os.environ['SB_ERROR_RECALL_ARM'] = 'control'
        self.assertEqual(sb_error_recall.arm('any'), 'control')

    # ── 출력 형식 ────────────────────────────────────────────────
    def test_injects_at_most_two_items_in_format(self):
        out = self.run_hook(self.failure())
        spec = out['hookSpecificOutput']
        self.assertEqual(spec['hookEventName'], 'PostToolUseFailure')
        lines = spec['additionalContext'].splitlines()
        self.assertTrue(lines[0].startswith('[세컨브레인 오류 회수]'))
        self.assertIn('get_observations([ID])', lines[0])
        self.assertEqual(lines[1:], ['- #11 10-01 kit · kiwipiepy 설치 누락 — venv 재생성으로 해결',
                                     '- #12 09-20 other-project-long · 형태소 색인 import 오류'])
        self.assertIn('limit=2', self.urls[0])
        self.assertIn('q=Bash+', self.urls[0])   # 도구 이름 + 오류 문구로 묻는다
        row = self.logs()[-1]
        self.assertEqual((row['arm'], row['ids'], row['injected']), ('on', [11, 12], True))
        self.assertNotIn('kiwipiepy', json.dumps(row, ensure_ascii=False))   # 로그에 오류 원문을 남기지 않는다

    def test_control_arm_logs_but_injects_nothing(self):
        os.environ['SB_ERROR_RECALL_ARM'] = 'control'
        self.assertIsNone(self.run_hook(self.failure()))
        row = self.logs()[-1]
        self.assertEqual((row['arm'], row['ids'], row['injected']), ('control', [11, 12], False))

    # ── 문턱 ─────────────────────────────────────────────────────
    def test_relevance_gate_fail_injects_nothing(self):
        self.response = {'gate': {'pass': False}, 'items': ITEMS}
        self.assertIsNone(self.run_hook(self.failure()))
        self.assertEqual(self.logs()[-1]['injected'], False)

    def test_short_or_non_actionable_errors_are_skipped(self):
        for err in ('Exit code 1', 'The user doesn\'t want to proceed with this tool use. It was rejected.',
                    'Permission to use Bash with command rm has been denied, user denied it'):
            self.assertIsNone(self.run_hook(self.failure(err=err)))
        self.assertIsNone(self.run_hook(dict(self.failure(), is_interrupt=True)))
        self.assertEqual(self.urls, [])   # 회수 질의조차 하지 않는다
        self.assertEqual(self.logs(), [])

    def test_same_signature_in_session_is_not_reinjected(self):
        self.assertIsNotNone(self.run_hook(self.failure()))
        again = ERR.replace('42', '97').replace('/opt/kit', '/srv/other')   # 숫자·경로만 다른 같은 오류
        self.assertIsNone(self.run_hook(self.failure(err=again)))
        self.assertTrue(self.logs()[-1]['repeat_in_session'])
        self.assertEqual(len(self.urls), 1)

    def test_ids_already_shown_in_session_are_not_repeated(self):
        self.run_hook(self.failure())                  # #11·#12 를 보여 줌
        self.response = {'gate': {'pass': True}, 'items': [ITEMS[0], ITEMS[2]]}
        out = self.run_hook(self.failure(err='Error: build failed because the lockfile is corrupt and unreadable'))
        lines = out['hookSpecificOutput']['additionalContext'].splitlines()[1:]
        self.assertEqual([ln.split()[1] for ln in lines], ['#13'])

    def test_disabled_by_env(self):
        os.environ['SB_ERROR_RECALL'] = '0'
        self.assertIsNone(self.run_hook(self.failure()))
        self.assertEqual(self.urls, [])

    def test_signature_ignores_paths_numbers_and_hashes(self):
        a = sb_error_recall.signature('Error at /a/b/c.py:12 commit deadbeef1 failed')
        b = sb_error_recall.signature('Error at /x/y.py:7 commit 0123abcd9 failed')
        self.assertEqual(a, b)


class _FakeRecalld(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({'gate': {'pass': True}, 'items': [
            {'id': 7, 'title': '한글 제목 🚀 윈도우 인코딩', 'date': '2026-10-02', 'project': 'kit'}]}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ErrorRecallEncodingTest(unittest.TestCase):
    def test_windows_codepage_stdio_still_emits_utf8_json(self):
        """윈도우 기본 코드페이지를 흉내 낸다. 고치기 전에는 cp949 로 못 찍는 글자(이모지)에서 출력이 통째로 사라졌다."""
        srv = HTTPServer(('127.0.0.1', 0), _FakeRecalld)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items() if k not in ('PYTHONUTF8', 'SB_ERROR_RECALL', 'SB_ERROR_RECALL_ARM',
                                                                   'SB_RECALL_STATE_DIR', 'SB_ERROR_RECALL_LOG')}
            env.update(SB_HOME=home, PYTHONIOENCODING='cp949',
                       SB_RECALLD_URL='http://127.0.0.1:%d' % srv.server_address[1])
            payload = {'session_id': 'enc', 'tool_name': 'Bash',
                       'error': '오류: 색인 파일을 열 수 없음 — database disk image is malformed'}
            res = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                 capture_output=True, env=env, timeout=30)
        ctx = json.loads(res.stdout.decode('utf-8'))['hookSpecificOutput']['additionalContext']
        self.assertIn('한글 제목 🚀 윈도우 인코딩', ctx)


if __name__ == '__main__':
    unittest.main()
