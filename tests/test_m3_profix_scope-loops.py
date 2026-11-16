"""M3 Pro 리뷰(scope-loops 묶음) 회귀 시험 — bin/loops.py, bin/sb_scope.py. 가짜 홈·임시 원장만 쓴다.

P1-4: record-exposure 가 exposable() 검사 없이 노출을 세어, 같은 날 재실행·닫힌 미결·재노출일 전 미결도
      백오프가 진행되거나(하루 6번이면 stale) 원장이 조작됐다 → 대상 아닌 ID는 건너뛴다(끄기 SB_LOOPS_EXPOSURE_STRICT=0).
P2-4: 손상된 expose_count('3'·null·2.5·[]·-1·100·true)가 트레이스백 또는 조용히 틀린 백오프를 냈다
      → record-exposure 가 원장을 바꾸지 않고 오류(rc 2)로 끝난다.
P1-6: _norm_name 이 공백을 없애 project_key 와 반대로 판정했다('my app'≠'my-app', 'my app'=='myapp')
      → 공백 연속을 '-' 로(끄기 SB_SCOPE_NAME_LEGACY=1).
P2-1: NFC/NFD 정규화가 없어 같은 한글 이름이 다른 프로젝트로 갈렸다.
P2-2: project_aliases.json 이 JSON 객체가 아니면 AttributeError 로 세션 브리핑 전체가 조용히 사라졌다.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / 'bin'
HOOK = ROOT / 'hooks' / 'session_context.py'
sys.path.insert(0, str(BIN))
import sb_scope  # noqa: E402
import sb_store  # noqa: E402

TODAY = datetime.date.today()
T = TODAY.isoformat()
YESTERDAY = (TODAY - datetime.timedelta(days=1)).isoformat()
TOMORROW = (TODAY + datetime.timedelta(days=1)).isoformat()


def loop(lid='L0000aaa1', **extra):
    rec = {'id': lid, 'title': '미결 ' + lid, 'status': 'open', 'date_opened': '2026-09-01',
           'project': 'alpha', 'value': 'high', 'expose_count': 0}
    rec.update(extra)
    return rec


class _LedgerBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sb-m3fix-loops-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.ledger = self.root / 'sb' / 'loops' / 'loops.jsonl'
        self.ledger.parent.mkdir(parents=True)
        self.env = {'HOME': str(self.root / 'home'), 'USERPROFILE': str(self.root / 'home'),
                    'SB_HOME': str(self.root / 'sb'), 'SB_LOOPS_PATH': str(self.ledger),
                    'PATH': os.environ.get('PATH', ''), 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
        if os.name == 'nt':
            self.env.update({k: os.environ[k] for k in ('SYSTEMROOT', 'TEMP', 'TMP') if k in os.environ})

    def write(self, records):
        sb_store.save_loops(records, str(self.ledger))

    def by_id(self):
        return {r['id']: r for r in sb_store.load_loops(str(self.ledger))}

    def cli(self, *args, **env):
        return subprocess.run([sys.executable, '-B', str(BIN / 'loops.py')] + list(args), env=dict(self.env, **env),
                              capture_output=True, text=True, encoding='utf-8', timeout=20)


class RecordExposureGateTests(_LedgerBase):
    """P1-4"""

    def test_same_day_rerun_counts_once(self):
        self.write([loop()])
        for _ in range(2):
            p = self.cli('record-exposure', 'L0000aaa1')
            self.assertEqual(p.returncode, 0, p.stderr)
        rec = self.by_id()['L0000aaa1']
        self.assertEqual((rec['expose_count'], rec['last_exposed'], rec['next_review']), (1, T, TOMORROW))
        self.assertIn('건너뜀', p.stdout)

    def test_six_same_day_runs_do_not_retire(self):
        self.write([loop()])
        for _ in range(6):
            self.assertEqual(self.cli('record-exposure', 'L0000aaa1').returncode, 0)
        rec = self.by_id()['L0000aaa1']
        self.assertEqual((rec['status'], rec['expose_count']), ('open', 1))

    def test_closed_states_are_untouched(self):
        recs = [loop('L0000aaa1', status='done', closed_at=T), loop('L0000aaa2', status='dropped', closed_at=T),
                loop('L0000aaa3', status='stale', stale_at=T, expose_count=6), loop('L0000aaa4', status='snoozed')]
        self.write(recs)
        p = self.cli('record-exposure', 'L0000aaa1', 'L0000aaa2', 'L0000aaa3', 'L0000aaa4')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sb_store.load_loops(str(self.ledger)), recs)

    def test_future_next_review_is_untouched(self):
        rec = loop(expose_count=2, last_exposed=YESTERDAY, next_review=(TODAY + datetime.timedelta(days=5)).isoformat())
        self.write([rec])
        self.assertEqual(self.cli('record-exposure', 'L0000aaa1').returncode, 0)
        self.assertEqual(self.by_id()['L0000aaa1'], rec)

    def test_mixed_batch_records_only_eligible(self):
        self.write([loop('L0000aaa1'), loop('L0000aaa2', status='done', closed_at=T)])
        p = self.cli('record-exposure', 'L0000aaa1', 'L0000aaa2')
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = self.by_id()
        self.assertEqual(rec['L0000aaa1']['expose_count'], 1)
        self.assertNotIn('last_exposed', rec['L0000aaa2'])
        self.assertIn('L0000aaa2', p.stdout)

    def test_unknown_id_still_errors_without_change(self):
        self.write([loop()])
        before = self.ledger.read_bytes()
        p = self.cli('record-exposure', 'L0000aaa1', 'L0000ffff')
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_legacy_switch_restores_unconditional_count(self):
        self.write([loop()])
        for _ in range(2):
            self.assertEqual(self.cli('record-exposure', 'L0000aaa1', SB_LOOPS_EXPOSURE_STRICT='0').returncode, 0)
        self.assertEqual(self.by_id()['L0000aaa1']['expose_count'], 2)


class ExposeCountCorruptionTests(_LedgerBase):
    """P2-4 — 손상값은 원장 변경 없이 깔끔한 오류(rc 2, 트레이스백 없음)."""

    def test_corrupt_expose_count_rejected(self):
        for bad in ('3', None, 2.5, [], -1, 100, True):
            with self.subTest(bad=bad):
                self.write([loop(expose_count=bad)])
                before = self.ledger.read_bytes()
                p = self.cli('record-exposure', 'L0000aaa1')
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn('expose_count', p.stderr)
                self.assertNotIn('Traceback', p.stderr)
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_missing_expose_count_is_zero(self):
        rec = loop()
        rec.pop('expose_count')
        self.write([rec])
        self.assertEqual(self.cli('record-exposure', 'L0000aaa1').returncode, 0)
        self.assertEqual(self.by_id()['L0000aaa1']['expose_count'], 1)


class _ScopeEnv(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {})
        p.start()
        self.addCleanup(p.stop)
        for k in ('SB_SCOPE_NAME_LEGACY', 'SB_SCOPE_NORM', 'SB_HOME_ALIASES'):
            os.environ.pop(k, None)


class NameRuleTests(_ScopeEnv):
    """P1-6 — loop/state 이름 비교가 project_key 와 같은 공백 규칙을 쓴다."""

    def test_space_becomes_dash(self):
        self.assertTrue(sb_scope.loop_matches_scope('my app', 'my-app'))
        self.assertTrue(sb_scope.loop_matches_scope('My_App', 'my  app'))
        self.assertFalse(sb_scope.loop_matches_scope('my app', 'myapp'))

    def test_agrees_with_project_key(self):
        for a, b in (('my app', 'my-app'), ('my app', 'myapp'), ('Demo_Proj', 'demo-proj'), ('x', 'y')):
            with self.subTest(a=a, b=b):
                self.assertEqual(sb_scope.loop_matches_scope(a, b), sb_scope.project_key(a) == sb_scope.project_key(b))

    def test_existing_rules_kept(self):
        self.assertTrue(sb_scope.loop_matches_scope('demo_proj', 'demo-proj'))
        self.assertTrue(sb_scope.loop_matches_scope('Demo-Proj', 'demo-proj'))
        self.assertTrue(sb_scope.loop_matches_scope('OPENCHAT _BRIDGE', 'OpenChat_Bridge'))   # '-' 옆 공백
        self.assertFalse(sb_scope.loop_matches_scope('shared', '/private/tmp/a/shared'))

    def test_legacy_switch(self):
        os.environ['SB_SCOPE_NAME_LEGACY'] = '1'
        self.assertTrue(sb_scope.loop_matches_scope('my app', 'myapp'))
        self.assertFalse(sb_scope.loop_matches_scope('my app', 'my-app'))


class NfcTests(_ScopeEnv):
    """P2-1"""

    def test_nfc_nfd_equal(self):
        nfc = unicodedata.normalize('NFC', '세컨브레인')
        nfd = unicodedata.normalize('NFD', nfc)
        self.assertNotEqual(nfc, nfd)
        self.assertTrue(sb_scope.loop_matches_scope(nfd, nfc))
        self.assertEqual(sb_scope.project_key(nfd), sb_scope.project_key(nfc))
        self.assertEqual(sb_scope.project_key('/x/' + nfd), sb_scope.project_key(nfc))
        self.assertEqual(sb_scope.slash_key('/x/' + nfd), sb_scope.slash_key('/x/' + nfc))


class AliasFileShapeTests(unittest.TestCase):
    """P2-2"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sb-m3fix-alias-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / 'home'
        self.cwd = self.home / 'work' / 'alpha'
        self.cwd.mkdir(parents=True)
        self.sb = self.home / '.secondbrain'
        (self.sb / 'loops').mkdir(parents=True)
        (self.sb / 'config').mkdir()
        self.aliases = self.sb / 'config' / 'project_aliases.json'

    def test_non_object_is_empty(self):
        for content in ('[]', 'null', '"str"', '3'):
            with self.subTest(content=content):
                self.aliases.write_text(content, encoding='utf-8')
                self.assertEqual(sb_scope._load_aliases(str(self.aliases)), {})
                sid, method, real = sb_scope.resolve_scope_id(str(self.cwd), aliases=sb_scope._load_aliases(
                    str(self.aliases)), use_git=False)
                self.assertEqual((sid, method), (real, 'realpath'))

    def test_session_brief_survives_non_object_aliases(self):
        self.aliases.write_text('[]', encoding='utf-8')
        ledger = self.sb / 'loops' / 'loops.jsonl'
        sb_store.save_loops([loop(project=os.path.realpath(str(self.cwd)), next_action='확인하기')], str(ledger))
        env = {'HOME': str(self.home), 'USERPROFILE': str(self.home), 'SB_HOME': str(self.sb),
               'SB_CLAUDE_MEM_DIR': str(self.home / '.claude-mem'), 'PATH': os.environ.get('PATH', ''),
               'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
               'SB_PROJECT_ALIASES': str(self.aliases), 'SB_LOOPS_PATH': str(ledger),
               'SB_STATE_DB': str(self.sb / 'state.db'), 'SB_CLAUDE_MEM_DB': str(self.root / 'none.db'),
               'SB_INJECTION_LOG': str(self.sb / 'inj.jsonl'), 'SB_RECALLD_AUTOSTART': '0', 'SB_HEALTH': '0',
               'SB_VAULT_NUDGE': '0', 'SB_LOOPS_EXPOSURE': '0', 'SB_CODEX_CAPTURE_EXEC': '1'}
        if os.name == 'nt':
            env.update({k: os.environ[k] for k in ('SYSTEMROOT', 'TEMP', 'TMP') if k in os.environ})
        p = subprocess.run([sys.executable, '-B', str(HOOK), '--harness', 'codex'], cwd=str(self.cwd), env=env,
                           input=json.dumps({'cwd': str(self.cwd)}), capture_output=True, text=True,
                           encoding='utf-8', timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('L0000aaa1', p.stdout)


if __name__ == '__main__':
    unittest.main()
