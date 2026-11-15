"""M3 T4 (L1) — 실제로 보여 준 미결만 노출로 기록해 백오프·stale 퇴장을 다시 돌린다.

- loops.py exposed-ids: stdin 본문의 미결 ID 중 열린 것·재노출일 도래·오늘 미노출만 출력(읽기 전용).
- 세션 시작 브리핑 훅: 트랙 줄 '[L…]'로 보여 준 미결을 하루 한 번 record-exposure. SB_LOOPS_EXPOSURE=0 이면 끔.
- 키트에는 미결 자동 종결 경로가 없다 — 이 사실이 바뀌면 scope 정규화 키 검사를 붙이도록 시험으로 고정한다.
격리: tempfile 가짜 HOME/SB_HOME, 실제 subprocess.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / 'bin'
HOOK = ROOT / 'hooks' / 'session_context.py'
sys.path.insert(0, str(BIN))
import loops  # noqa: E402
import sb_store  # noqa: E402

TODAY = datetime.date.today()
T = TODAY.isoformat()
YESTERDAY = (TODAY - datetime.timedelta(days=1)).isoformat()
TOMORROW = (TODAY + datetime.timedelta(days=1)).isoformat()


def loop(lid, project='alpha', status='open', **extra):
    rec = {'id': lid, 'title': '미결 ' + lid, 'status': status, 'date_opened': '2026-09-01',
           'project': project, 'value': 'high', 'expose_count': 0, 'next_action': '확인하기'}
    rec.update(extra)
    return rec


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sb-m3t4-'))
        self.addCleanup(self._cleanup)
        self.home = self.tmp / 'home'
        self.cwd = self.home / 'work' / 'alpha'
        self.cwd.mkdir(parents=True)
        (self.home / 'work' / 'beta').mkdir()
        self.sb = self.home / '.secondbrain'
        (self.sb / 'loops').mkdir(parents=True)
        (self.sb / 'logs').mkdir()
        (self.sb / 'config').mkdir()
        self.aliases = self.sb / 'config' / 'project_aliases.json'
        self.aliases.write_text(json.dumps({'alpha': [str(self.cwd)], 'beta': [str(self.home / 'work' / 'beta')]}),
                                encoding='utf-8')
        self.ledger = self.sb / 'loops' / 'loops.jsonl'
        self.env = {'HOME': str(self.home), 'USERPROFILE': str(self.home), 'SB_HOME': str(self.sb),
                    'SB_CLAUDE_MEM_DIR': str(self.home / '.claude-mem'), 'PATH': os.environ.get('PATH', ''),
                    'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1', 'SB_CODEX_CAPTURE_EXEC': '1',
                    'SB_PROJECT_ALIASES': str(self.aliases), 'SB_LOOPS_PATH': str(self.ledger),
                    'SB_STATE_DB': str(self.sb / 'state.db'), 'SB_INJECTION_LOG': str(self.sb / 'logs' / 'injection.jsonl'),
                    'SB_STATE_PILOT_SCOPES': 'alpha,beta', 'SB_CLAUDE_MEM_DB': str(self.tmp / 'no-claude-mem.db'),
                    'SB_RECALLD_AUTOSTART': '0', 'SB_HEALTH': '0', 'SB_VAULT_NUDGE': '0'}
        if os.name == 'nt':  # Windows 는 SYSTEMROOT 없이 파이썬 서브프로세스가 뜨지 않는다
            self.env.update({k: os.environ[k] for k in ('SYSTEMROOT', 'TEMP', 'TMP') if k in os.environ})
        patch = mock.patch.dict(os.environ, {'SB_HOME': str(self.sb), 'SB_LOOPS_PATH': str(self.ledger)})
        patch.start()
        self.addCleanup(patch.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, records):
        sb_store.save_loops(records, str(self.ledger))

    def ledger_by_id(self):
        return {r['id']: r for r in sb_store.load_loops(str(self.ledger))}

    def cli(self, *args, stdin=''):
        return subprocess.run([sys.executable, '-B', str(BIN / 'loops.py')] + list(args), input=stdin, env=self.env,
                              capture_output=True, text=True, encoding='utf-8', timeout=20)

    def hook(self, **env):
        e = dict(self.env, **env)
        p = subprocess.run([sys.executable, '-B', str(HOOK), '--harness', 'codex'], cwd=str(self.cwd), env=e,
                           input=json.dumps({'cwd': str(self.cwd)}), capture_output=True, text=True,
                           encoding='utf-8', timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout.strip().splitlines()[-1])['hookSpecificOutput']['additionalContext'] if p.stdout.strip() else ''


class ExposedIdsTests(_Base):
    def test_filters_to_open_due_and_not_yet_exposed_today(self):
        self.write([loop('L0000aaa1'),                                    # 기본: 재노출일 없음 → 대상
                    loop('L0000aaa2', next_review=YESTERDAY),             # 재노출일 지남 → 대상
                    loop('L0000aaa3', next_review=TOMORROW),              # 아직 이름 → 제외
                    loop('L0000aaa4', last_exposed=T, next_review=T),     # 오늘 이미 셈 → 제외
                    loop('L0000aaa5', status='done', closed_at=T),        # 닫힘 → 제외
                    loop('L0000aaa6', status='waiting_external'),         # 외부 대기도 열린 미결 → 대상
                    loop('L0000aaa7')])                                   # 본문에 안 나옴 → 제외
        body = ('1. 첫째 L0000aaa1 / 처리: close L0000aaa2\n L0000aaa3 L0000aaa4 L0000aaa5 L0000aaa6 '
                'L0000aaa1 중복 · 원장에 없는 L0000ffff · 단어 속 XL0000aaa7Y')
        p = self.cli('exposed-ids', stdin=body)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.split(), ['L0000aaa1', 'L0000aaa2', 'L0000aaa6'])

    def test_is_readonly(self):
        self.write([loop('L0000aaa1')])
        before = self.ledger.read_bytes()
        self.cli('exposed-ids', stdin='L0000aaa1')
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertFalse((self.ledger.parent / 'OPEN.md').exists())

    def test_output_feeds_record_exposure(self):
        self.write([loop('L0000aaa1'), loop('L0000aaa2', status='dropped', closed_at=T)])
        ids = self.cli('exposed-ids', stdin='L0000aaa1 L0000aaa2').stdout.split()
        p = self.cli('record-exposure', *ids)
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = self.ledger_by_id()
        self.assertEqual((rec['L0000aaa1']['expose_count'], rec['L0000aaa1']['last_exposed']), (1, T))
        self.assertNotIn('last_exposed', rec['L0000aaa2'])


class RecordShownTests(_Base):
    def test_once_per_day_and_backoff(self):
        self.write([loop('L0000aaa1')])
        self.assertEqual(loops.record_shown(['L0000aaa1', 'L0000aaa1']), ['L0000aaa1'])
        rec = self.ledger_by_id()['L0000aaa1']
        self.assertEqual((rec['expose_count'], rec['last_exposed'], rec['next_review']), (1, T, TOMORROW))
        self.assertEqual(loops.record_shown(['L0000aaa1']), [])
        self.assertEqual(self.ledger_by_id()['L0000aaa1']['expose_count'], 1)

    def test_sixth_exposure_retires_to_stale(self):
        self.write([loop('L0000aaa1', expose_count=5, last_exposed=YESTERDAY, next_review=T)])
        self.assertEqual(loops.record_shown(['L0000aaa1']), ['L0000aaa1'])
        rec = self.ledger_by_id()['L0000aaa1']
        self.assertEqual((rec['status'], rec['stale_at'], rec['expose_count']), ('stale', T, 6))

    def test_no_eligible_ids_does_not_touch_ledger(self):
        self.write([loop('L0000aaa1', next_review=TOMORROW), loop('L0000aaa2', status='done', closed_at=T)])
        before = self.ledger.read_bytes()
        self.assertEqual(loops.record_shown(['L0000aaa1', 'L0000aaa2', 'L0000ffff']), [])
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertFalse((self.ledger.parent / 'OPEN.md').exists())

    def test_lock_timeout_raises_and_leaves_ledger(self):
        import sb_lock
        self.write([loop('L0000aaa1')])
        before = self.ledger.read_bytes()
        with open(str(self.ledger) + '.lock', 'a', encoding='utf-8') as held:
            sb_lock.lock_with_timeout(held, sb_lock.LOCK_EX, 1.0)
            try:
                code = ('import sys; sys.path.insert(0, %r); import loops\n'
                        'try:\n    loops.record_shown(["L0000aaa1"], timeout=0.2); print("wrote")\n'
                        'except TimeoutError:\n    print("timeout")\n') % str(BIN)
                p = subprocess.run([sys.executable, '-B', '-c', code], env=self.env, capture_output=True, text=True,
                                   encoding='utf-8', timeout=20)
            finally:
                sb_lock.lock(held, sb_lock.LOCK_UN)
        self.assertEqual(p.stdout.strip(), 'timeout', p.stderr)
        self.assertEqual(self.ledger.read_bytes(), before)


class SessionHookExposureTests(_Base):
    def records(self):
        # value medium: high·마감 임박은 아침 문자 대상이라 세션 시작에서 세지 않는다(따로 시험)
        return [loop('L0000aaa1', next_review=YESTERDAY, value='medium'),   # 이 scope·재노출일 지남 → 노출
                loop('L0000aaa2', next_review=TOMORROW, value='medium'),    # 이 scope지만 아직 → 보여도 세지 않음
                loop('L0000bbb1', project='beta', value='medium')]          # 다른 scope → 안 보이고 안 셈

    def test_briefing_records_shown_tracks_once_per_day(self):
        self.write(self.records())
        text = self.hook()
        self.assertIn('[L0000aaa1]', text)
        self.assertIn('[L0000aaa2]', text)
        self.assertNotIn('L0000bbb1', text)
        rec = self.ledger_by_id()
        self.assertEqual((rec['L0000aaa1']['expose_count'], rec['L0000aaa1']['last_exposed']), (1, T))
        self.assertEqual(rec['L0000aaa1']['next_review'], TOMORROW)
        self.assertNotIn('last_exposed', rec['L0000aaa2'])
        self.assertNotIn('last_exposed', rec['L0000bbb1'])
        after_first = self.ledger.read_bytes()
        self.hook()   # 같은 날 두 번째 세션은 다시 세지 않는다
        self.assertEqual(self.ledger.read_bytes(), after_first)

    def test_fallback_loop_list_also_records(self):
        self.write(self.records())
        text = self.hook(SB_STATE_BRIEFING='0')
        self.assertIn('[L0000aaa1]', text)
        self.assertEqual(self.ledger_by_id()['L0000aaa1']['expose_count'], 1)

    def test_env_off_disables_recording(self):
        self.write(self.records())
        before = self.ledger.read_bytes()
        text = self.hook(SB_LOOPS_EXPOSURE='0')
        self.assertIn('[L0000aaa1]', text)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_unbracketed_mentions_are_not_exposure(self):
        # 다른 scope 미결 ID가 브리핑 본문에 지나가듯 나와도(트랙 줄 '[..]' 아님) 노출로 치지 않는다
        import importlib.util
        spec = importlib.util.spec_from_file_location('sc_t4', str(HOOK))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.write(self.records())
        for k in ('CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_MEM_INTERNAL', 'SB_RECALL'):   # 이 시험을 돌리는 세션의 표지와 분리
            p = mock.patch.dict(os.environ, {k: ''})
            p.start()
            self.addCleanup(p.stop)
            os.environ.pop(k, None)
        self.assertEqual(mod.record_exposure('주의: 연결된 미결 [L0000bbb1] 닫힘\n트랙 1건:\n  · [L0000aaa1] 할 일'),
                         ['L0000aaa1'])
        self.assertNotIn('last_exposed', self.ledger_by_id()['L0000bbb1'])


class NoAutoCloseInKitTests(unittest.TestCase):
    def test_kit_has_no_automatic_loop_close_path(self):
        """키트의 미결 종결은 사람이 부르는 loops.py close/drop 뿐이다. 자동 종결이 생기면
        같은 scope 정규화 키일 때만 닫고 나머지는 close_candidate 로 남기는 검사를 함께 넣어야 한다(M3 T4)."""
        pat = re.compile(r'auto_close|autoclose|auto-close|close_candidate')
        hits = []
        for d in ('bin', 'hooks', 'installer'):
            base = ROOT / d
            if not base.exists():
                continue
            for p in base.rglob('*.py'):
                for n, line in enumerate(p.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
                    if pat.search(line):
                        hits.append('%s:%d' % (p.relative_to(ROOT), n))
        self.assertEqual(hits, [], '자동 종결 경로가 생겼다 — scope 정규화 키 검사를 붙여라')
        src = (BIN / 'loops.py').read_text(encoding='utf-8')
        self.assertEqual(src.count("i['status'] = 'done'"), 1)   # mutate 의 close 분기 하나뿐


if __name__ == '__main__':
    unittest.main()
