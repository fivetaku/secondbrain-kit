"""V5 — sb_briefing 과 SessionStart 훅 계약. 격리 HOME/SB_HOME/PWD/DB/log 로 실제 subprocess 를 검사한다.

훅: kit 의 hooks/session_context.py (SB_SESSION_HOOK 로 변경 가능). stdin 훅 JSON 의 cwd 로 scope 를 푼다.
kit 에서는 브리핑이 기본 ON 이다(SB_STATE_BRIEFING=0 이면 미결 목록만). 구 운영판은 opt-in(기본 off)이었다.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / 'bin'
sys.path.insert(0, str(BIN))
import sb_state  # noqa: E402

PY = sys.executable
HOOK = Path(os.environ.get('SB_SESSION_HOOK') or ROOT / 'hooks' / 'session_context.py')


def loop(i, project, title, status='open', value='medium', next_action=None, next_review=None):
    return {'id': 'L%04d' % i, 'title': title, 'project': project, 'status': status, 'value': value,
            'next_action': next_action, 'next_review': next_review}


@unittest.skipUnless(HOOK.exists(), 'session hook missing: %s' % HOOK)
class BriefingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sb-briefing-'))
        self.home = self.tmp / 'home'; self.cwd = self.home / 'work' / 'alpha'; self.cwd.mkdir(parents=True)
        (self.home / 'work' / 'beta').mkdir()
        sb = self.home / '.secondbrain'; (sb / 'loops').mkdir(parents=True); (sb / 'logs').mkdir(); (sb / 'config').mkdir()
        (sb / 'config' / 'project_aliases.json').write_text(json.dumps({'alpha': [str(self.cwd)], 'beta': [str(self.home / 'work' / 'beta')]}), encoding='utf-8')
        self.loops = sb / 'loops' / 'loops.jsonl'; self.loops.write_text('', encoding='utf-8')
        self.state = sb / 'state.db'; self.log = sb / 'logs' / 'injection.jsonl'
        self.env = {'HOME': str(self.home), 'USERPROFILE': str(self.home), 'SB_HOME': str(sb),
                    'SB_CLAUDE_MEM_DIR': str(self.home / '.claude-mem'),
                    'PATH': os.environ['PATH'], 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'PYTHONIOENCODING': 'utf-8',
                    'PYTHONDONTWRITEBYTECODE': '1', 'SB_PROJECT_ALIASES': str(sb / 'config' / 'project_aliases.json'),
                    'SB_LOOPS_PATH': str(self.loops), 'SB_STATE_DB': str(self.state), 'SB_INJECTION_LOG': str(self.log),
                    'SB_STATE_PILOT_SCOPES': 'alpha,beta', 'SB_VERIFY_ALLOWED_ROOTS': str(self.tmp),
                    'SB_CLAUDE_MEM_DB': str(self.tmp / 'no-claude-mem.db'), 'SB_EVAL_NOW': '2026-09-17T00:00:00+00:00'}
        if os.name == 'nt':  # Windows 는 SYSTEMROOT 없이 파이썬 서브프로세스가 뜨지 않는다
            self.env.update({k: os.environ[k] for k in ('SYSTEMROOT', 'TEMP', 'TMP') if k in os.environ})
        os.environ.update({k: self.env[k] for k in ('SB_HOME', 'SB_STATE_DB', 'SB_STATE_PILOT_SCOPES', 'SB_VERIFY_ALLOWED_ROOTS', 'SB_PROJECT_ALIASES',
                                                     'SB_CLAUDE_MEM_DB', 'SB_LOOPS_PATH', 'SB_EVAL_NOW')})

    def write_loops(self, loops):
        self.loops.write_text(''.join(json.dumps(l, ensure_ascii=False) + '\n' for l in loops), encoding='utf-8')

    def put_head(self, scope, key, body, verified=True):
        if not self.state.exists():
            # $SB_HOME/state.db 는 운영 DB 로 보호된다 — 설치기처럼 명시 승인(env + 인자)으로 만든다
            os.environ['SB_STATE_ALLOW_OPERATIONAL_MIGRATION'] = '1'
            try:
                sb_state.migrate(str(self.state), allow_operational=True)
            finally:
                os.environ.pop('SB_STATE_ALLOW_OPERATIONAL_MIGRATION', None)
        if not verified:
            with sqlite3.connect(self.state) as db:
                db.execute("INSERT INTO state(memory_kind, scope_id, fact_key, version, is_head, recorded_at, body, source, write_id, dedup_key) "
                           "VALUES ('fact',?,?,1,1,'now',?,'legacy',?,?)", (scope, key, body, key, '%s:legacy:%s' % (scope, key)))
            return
        ev = self.tmp / ('ev-%s.txt' % key); ev.write_text('measured: %s\n' % body, encoding='utf-8')
        r = sb_state.propose({'scope_id': scope, 'fact_key': key, 'body': body, 'kind': 'measured_fact', 'source': 't', 'write_id': key}, str(self.state))
        sb_state.verify(r['candidate_id'], 'file_contains', str(ev), db_path=str(self.state))
        sb_state.accept(r['candidate_id'], 0, str(self.state))

    def hook(self, briefing='1', cwd=None):
        env = dict(self.env, SB_HOOK_DEBUG='1')
        if briefing is not None:
            env['SB_STATE_BRIEFING'] = briefing
        where = str(cwd or self.cwd)
        p = subprocess.run([PY, str(HOOK), '--harness', 'codex'], cwd=where, env=env, input=json.dumps({'cwd': where}),
                           capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.strip(), 'hook printed nothing; stderr:\n' + p.stderr)
        payload = json.loads(p.stdout.strip().splitlines()[-1])
        return payload['hookSpecificOutput']['additionalContext'], p

    def cli(self, *args):
        p = subprocess.run([PY, str(BIN / 'sb_briefing.py'), *args], cwd=str(self.cwd), env=self.env, capture_output=True, text=True,
                           encoding='utf-8')
        try:
            return p.returncode, json.loads(p.stdout.strip().splitlines()[-1]), p
        except (ValueError, IndexError):
            return p.returncode, None, p


class TrackPreservationTests(BriefingBase):
    def test_two_tracks_preserved_with_ids_first_action_and_state(self):
        self.write_loops([loop(21, 'alpha', '트랙1 배포', 'open', 'high', 'deploy.yml 검토', '2026-09-20'),
                          loop(22, 'alpha', '트랙2 문서', 'open', 'low', 'README 갱신'), loop(23, 'beta', '베타', 'open', 'high', 'x')])
        self.put_head('alpha', 'deploy.target', 'staging-2')
        text, _ = self.hook()
        for needle in ('L0021', 'L0022', 'deploy.yml 검토', 'README 갱신', '첫 행동: deploy.yml 검토', 'staging-2', 'operationally measured'):
            self.assertIn(needle, text)
        self.assertNotIn('L0023', text)
        self.assertLess(text.find('deploy.yml 검토'), text.find('README 갱신'))

    def test_closing_one_track_keeps_the_other(self):
        self.write_loops([loop(21, 'alpha', '트랙1', 'done', 'high', 'a'), loop(22, 'alpha', '트랙2', 'open', 'low', 'b')])
        text, _ = self.hook()
        self.assertIn('L0022', text); self.assertNotIn('L0021', text)

    def test_waiting_and_cautions_are_marked_without_asserting_neglect(self):
        self.write_loops([loop(21, 'alpha', '외부 답변 대기', 'waiting_external', 'high', 'x'),
                          loop(22, 'alpha', '다음 행동 없는 트랙', 'open', 'low'), loop(23, 'alpha', '기한 지난', 'open', 'low', 'y', '2026-09-01')])
        self.put_head('alpha', 'deploy.target', 'staging-1', verified=False)
        text, _ = self.hook()
        self.assertIn('대기', text); self.assertIn('L0021', text)
        self.assertIn('다음 행동 없음: L0022', text); self.assertIn('재검토일 경과: L0023', text); self.assertIn('미검증', text)
        self.assertNotIn('방치', text)


class BudgetAndDegradationTests(BriefingBase):
    def test_many_tracks_report_omission_within_budget_and_metrics_match(self):
        self.write_loops([loop(60 + i, 'alpha', '대량 트랙 %02d ' % i + '가' * 40, 'open', 'medium', '행동 %d' % i) for i in range(50)])
        text, _ = self.hook()
        self.assertIn('생략', text); self.assertLessEqual(len(text), 2000); self.assertLessEqual(len(text.encode('utf-8')), 8192)
        rec = json.loads(self.log.read_text(encoding='utf-8').strip().splitlines()[-1])
        self.assertEqual((rec['context_chars'], rec['briefing']), (len(text), 1))
        if 'context_utf8_bytes' in rec:  # kit 훅은 현재 바이트 수를 기록하지 않는다 — 기록하면 일치해야 한다
            self.assertEqual(rec['context_utf8_bytes'], len(text.encode('utf-8')))
        self.assertEqual(rec['omitted'], 47)

    def test_long_state_bodies_are_truncated_to_budget(self):
        self.write_loops([loop(21, 'alpha', '트랙1', 'open', 'high', 'a')])
        for i in range(12):
            self.put_head('alpha', 'long.%d' % i, '긴 본문 ' + '나' * 300)
        text, _ = self.hook()
        self.assertLessEqual(len(text), 2000); self.assertLessEqual(len(text.encode('utf-8')), 8192); self.assertIn('L0021', text)

    def test_state_db_missing_keeps_loops(self):
        self.write_loops([loop(21, 'alpha', '트랙1', 'open', 'high', 'a'), loop(22, 'alpha', '트랙2', 'open', 'low', 'b')])
        text, _ = self.hook()
        self.assertIn('L0021', text); self.assertIn('L0022', text); self.assertIn('조회 불가', text)
        self.assertFalse(self.state.exists(), 'hook must not create the state db')

    def test_malformed_loops_line_still_emits_hook_json(self):
        self.loops.write_text(json.dumps(loop(21, 'alpha', '트랙1', 'open', 'high', 'a')) + '\n{broken\n', encoding='utf-8')
        text, p = self.hook()
        self.assertIn('L0021', text); self.assertNotIn('Traceback', p.stderr)
        text_off, p2 = self.hook(briefing='0')
        self.assertIn('L0021', text_off); self.assertNotIn('Traceback', p2.stderr)

    def test_korean_emoji_metrics_and_valid_json(self):
        self.write_loops([loop(31, 'alpha', '🚀 한글·이모지 제목 테스트 🎯', 'open', 'high', '✅ 확인')])
        text, _ = self.hook()
        self.assertIn('🚀', text); self.assertIn('✅ 확인', text)
        rec = json.loads(self.log.read_text(encoding='utf-8').strip().splitlines()[-1])
        self.assertEqual(rec['context_chars'], len(text))
        self.assertGreater(len(text.encode('utf-8')), len(text))
        if 'context_utf8_bytes' in rec:
            self.assertGreater(rec['context_utf8_bytes'], rec['context_chars'])


class OffParityAndReadonlyTests(BriefingBase):
    def test_flag_off_shows_loops_only_and_default_is_briefing(self):
        self.write_loops([loop(21, 'alpha', '트랙1', 'open', 'high', 'deploy.yml 검토', '2026-09-20'), loop(22, 'alpha', '트랙2', 'open', 'low', 'b')])
        self.put_head('alpha', 'deploy.target', 'staging-2')
        off, _ = self.hook(briefing='0')
        for needle in ('L0021', 'L0022', 'deploy.yml 검토'):
            self.assertIn(needle, off)
        self.assertNotIn('staging-2', off)
        on, _ = self.hook(briefing='1')
        self.assertIn('staging-2', on)
        default, _ = self.hook(briefing=None)
        self.assertEqual(default, on, 'kit default is briefing on')

    def test_hook_writes_only_allowlisted_injection_log(self):
        self.write_loops([loop(21, 'alpha', '트랙1', 'open', 'high', 'a')]); self.put_head('alpha', 'k', 'v')
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.tmp.rglob('*') if p.is_file()}
        self.hook()
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.tmp.rglob('*') if p.is_file()}
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        self.assertEqual(changed, {str(self.log)})

    def test_briefing_cli_scope_and_portfolio_contract(self):
        self.write_loops([loop(21, 'alpha', 'A', 'open', 'high', 'a'), loop(22, 'beta', 'B', 'open', 'high', 'b'), loop(23, 'gamma', 'G', 'open')])
        code, j, _ = self.cli('--scope', 'alpha', '--json')
        self.assertEqual((code, j['query_mode'], j['scope']), (0, 'briefing', 'alpha'))
        self.assertEqual([i['loop_id'] for i in j['items'] if i['section'] == 'track'], ['L0021'])
        self.assertEqual(j['first_action'], {'loop_id': 'L0021', 'action': 'a'})
        code, j, p = self.cli('--all', '--scopes', 'alpha,beta', '--json')
        self.assertEqual(sorted({i['scope_id'] for i in j['items']}), ['alpha', 'beta']); self.assertNotIn('L0023', p.stdout)
        code, j, _ = self.cli('--all', '--json')
        self.assertEqual((code, j['status']), (2, 'error'))
        code, j, _ = self.cli('--scope', '', '--json')
        self.assertEqual((code, j['status']), (2, 'error'))


if __name__ == '__main__':
    unittest.main()
