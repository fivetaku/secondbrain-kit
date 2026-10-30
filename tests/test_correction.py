"""hooks/sb_correction.py — 세션 중 사용자 교정 판정(2026-10-21 도입, 직접 테스트가 없던 공백).
LLM 호출은 모두 가짜(모의 함수 또는 이 테스트가 띄우는 로컬 HTTP 서버)다."""
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
SCRIPT = KIT / 'hooks' / 'sb_correction.py'
sys.path.insert(0, str(KIT / 'hooks'))
import sb_correction  # noqa: E402

LLM_ENV = ('SB_CORRECTION_LLM_URL', 'SB_CORRECTION_MODEL', 'SB_CORRECTION_LLM_KEY', 'SB_CORRECTION_QUEUE')
PROMPT = '아니 그렇게 하지 말고, 앞으로는 배포 전에 항상 테스트부터 돌려'


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.queue = self.home / 'index' / 'correction_candidates.jsonl'
        env = mock.patch.dict(os.environ, {'SB_HOME': str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for k in LLM_ENV:
            os.environ.pop(k, None)
        q = mock.patch.object(sb_correction, 'QUEUE', str(self.queue))
        q.start()
        self.addCleanup(q.stop)

    def run_main(self, verdict=None, ev=None):
        ev = ev or {'sid': 'sess-1', 'prompt': PROMPT, 'cwd': '/work/proj'}
        fake = mock.Mock(return_value=verdict or {})
        with mock.patch.object(sb_correction, 'classify', fake), \
                mock.patch('sys.stdin', io.StringIO(json.dumps(ev, ensure_ascii=False))):
            sb_correction.main()
        return fake

    def rows(self):
        return [json.loads(x) for x in self.queue.read_text(encoding='utf-8').splitlines()] if self.queue.exists() else []

    def test_without_llm_config_nothing_is_called_or_written(self):
        fake = self.run_main({'is_correction': True, 'durable_rule': True, 'rule': '규칙'})
        fake.assert_not_called()
        self.assertFalse(self.queue.exists())
        self.assertFalse((self.home / 'index').exists())

    def test_url_without_model_is_still_off(self):
        os.environ['SB_CORRECTION_LLM_URL'] = 'http://127.0.0.1:9/v1/chat/completions'
        self.run_main({'is_correction': True, 'durable_rule': True, 'rule': '규칙'}).assert_not_called()
        self.assertFalse(self.queue.exists())

    def test_durable_rule_is_queued_with_cwd(self):
        os.environ.update(SB_CORRECTION_LLM_URL='http://127.0.0.1:9/v1/chat/completions', SB_CORRECTION_MODEL='small')
        fake = self.run_main({'is_correction': True, 'durable_rule': True, 'rule': '  배포 전에는 항상 테스트부터 돌린다  '})
        self.assertEqual(fake.call_args[0][0], PROMPT)
        [row] = self.rows()
        self.assertEqual((row['sid'], row['cwd'], row['rule'], row['model']),
                         ('sess-1', '/work/proj', '배포 전에는 항상 테스트부터 돌린다', 'small'))
        self.assertRegex(row['ts'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$')   # session_context 의 7일 창과 같은 형식
        self.assertEqual(len(row['prompt_sha']), 16)
        self.assertNotIn(PROMPT, json.dumps(row, ensure_ascii=False))   # 발화 원문은 남기지 않는다

    def test_non_durable_or_empty_rule_is_not_queued(self):
        os.environ.update(SB_CORRECTION_LLM_URL='http://x', SB_CORRECTION_MODEL='m')
        for verdict in ({'is_correction': True, 'durable_rule': False, 'rule': '이번만'},
                        {'is_correction': False, 'durable_rule': True, 'rule': '규칙'},
                        {'is_correction': True, 'durable_rule': True, 'rule': '   '}):
            self.run_main(verdict)
        self.assertFalse(self.queue.exists())

    def test_config_file_under_sb_home_and_env_override(self):
        (self.home / 'index').mkdir(parents=True)
        (self.home / 'index' / 'correction_llm.json').write_text(
            json.dumps({'url': 'http://file/v1', 'model': 'file-model', 'key': 'k'}), encoding='utf-8')
        self.assertEqual(sb_correction._llm_conf(), {'url': 'http://file/v1', 'model': 'file-model', 'key': 'k'})
        os.environ['SB_CORRECTION_MODEL'] = 'env-model'
        self.assertEqual(sb_correction._llm_conf()['model'], 'env-model')

    def test_classify_request_and_lenient_json_parsing(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen['req'] = req
            content = '판정 결과:\n{"is_correction": true, "durable_rule": true, "rule": "r"} 끝'
            return _Resp(json.dumps({'choices': [{'message': {'content': content}}]}).encode('utf-8'))

        with mock.patch.object(sb_correction.urllib.request, 'urlopen', fake_urlopen):
            v = sb_correction.classify(PROMPT, {'url': 'http://llm/v1/chat/completions', 'model': 'm', 'key': 'secret'})
            self.assertEqual(v, {'is_correction': True, 'durable_rule': True, 'rule': 'r'})
            self.assertEqual(seen['req'].get_header('Authorization'), 'Bearer secret')
            body = json.loads(seen['req'].data)
            self.assertEqual(body['model'], 'm')
            self.assertIn(PROMPT, body['messages'][0]['content'])
            sb_correction.classify(PROMPT, {'url': 'http://llm', 'model': 'm', 'key': ''})
            self.assertIsNone(seen['req'].get_header('Authorization'))   # 키가 없으면 헤더도 없다


class _FakeLLM(BaseHTTPRequestHandler):
    """받은 발화(<<<...>>>)를 그대로 규칙으로 돌려준다 — 입력이 깨지면 규칙도 깨진다."""

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode('utf-8'))
        msg = body['messages'][0]['content']
        said = msg[msg.index('<<<') + 3:msg.index('>>>')]
        verdict = {'is_correction': True, 'durable_rule': True, 'rule': said}
        out = json.dumps({'choices': [{'message': {'content': json.dumps(verdict, ensure_ascii=False)}}]}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class CorrectionProcessTest(unittest.TestCase):
    def test_windows_codepage_stdin_keeps_korean(self):
        """sb_recall 이 UTF-8 로 넘기는 발화를 윈도우 기본 코드페이지(cp949)로 읽으면 깨지거나 디코딩 오류로 조용히 끝났다."""
        srv = HTTPServer(('127.0.0.1', 0), _FakeLLM)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with tempfile.TemporaryDirectory() as home:
            queue = Path(home) / 'q.jsonl'
            env = {k: v for k, v in os.environ.items() if k not in ('PYTHONUTF8',) + LLM_ENV}
            env.update(SB_HOME=home, PYTHONIOENCODING='cp949', SB_CORRECTION_QUEUE=str(queue), SB_CORRECTION_MODEL='m',
                       SB_CORRECTION_LLM_URL='http://127.0.0.1:%d/v1/chat/completions' % srv.server_address[1])
            ev = {'sid': 's', 'prompt': PROMPT, 'cwd': '/work/프로젝트'}
            res = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(ev, ensure_ascii=False).encode('utf-8'),
                                 capture_output=True, env=env, timeout=30)
            self.assertEqual(res.returncode, 0, res.stderr)
            [row] = [json.loads(x) for x in queue.read_text(encoding='utf-8').splitlines()]
        self.assertEqual((row['rule'], row['cwd']), (PROMPT, '/work/프로젝트'))


if __name__ == '__main__':
    unittest.main()
