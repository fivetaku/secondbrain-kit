import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / 'hooks' / 'session_context.py'


class _HookHarness(unittest.TestCase):
    """임시 SB_HOME·미결 원장·별칭으로 훅을 실제 프로세스로 돌리는 공용 준비(테스트 메서드 없음)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / 'ledger.jsonl'
        self.aliases = self.root / 'aliases.json'
        # 기본: openchat-bridge 프로젝트 ↔ <root>/openchat-bridge 경로
        self.set_aliases({'openchat-bridge': [str(self.root / 'openchat-bridge')]})
        self.env = dict(os.environ, HOME=str(self.root), USERPROFILE=str(self.root),
                        SB_HOME=str(self.root / 'sbhome'), SB_CODEX_CAPTURE_EXEC='1',
                        SB_LOOPS_PATH=str(self.ledger),
                        SB_PROJECT_ALIASES=str(self.aliases),
                        SB_STATE_BRIEFING='0', SB_RECALLD_AUTOSTART='0')
        for key in ('SB_CORRECTION_QUEUE', 'SB_RECALL_STATE_DIR'):
            self.env.pop(key, None)

    def set_aliases(self, table: dict) -> None:
        self.aliases.write_text(json.dumps(table), encoding='utf-8')

    def run_hook(self, records: list, folder: str = 'openchat-bridge', harness: str = 'claude'):
        self.ledger.write_text(''.join(json.dumps(row) + '\n' for row in records),
                               encoding='utf-8')
        cwd = self.root / folder
        cwd.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([sys.executable, str(SCRIPT), '--harness', harness],
                                input=json.dumps({'cwd': str(cwd)}), env=self.env,
                                capture_output=True, text=True, timeout=20, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def context(self, records: list, folder: str = 'openchat-bridge') -> str:
        payload = self.run_hook(records, folder)
        if payload is None:  # 주입할 내용이 없으면 아무것도 출력하지 않는다
            return ''
        text = payload['hookSpecificOutput']['additionalContext']
        self.assertEqual(payload['systemMessage'], '🗂 세컨브레인\n' + text)
        return text


class SessionContextTests(_HookHarness):
    """2026-09-16 memory-layer-refactor M2: 폴더 이름(basename) 매칭 없음.
    미결의 project 는 project_aliases.json 에 등록된 경로(scope_id) 로만 현재 폴더에 연결된다."""

    def test_codex_harness_omits_system_message(self) -> None:
        payload = self.run_hook([dict(id='c', title='c', project='openchat-bridge', status='open')],
                                harness='codex')
        self.assertNotIn('systemMessage', payload)
        self.assertIn('[c]', payload['hookSpecificOutput']['additionalContext'])

    def test_briefing_path_is_default(self) -> None:
        self.env.pop('SB_STATE_BRIEFING')
        payload = self.run_hook([dict(id='b', title='b', project='openchat-bridge', status='open',
                                      next_action='다음')])
        self.assertIn('재개 브리핑', payload['hookSpecificOutput']['additionalContext'])

    def test_exact_normalized_matching(self) -> None:
        records = [dict(id=str(i), title=project, project=project, status='open')
                   for i, project in enumerate(['openchat-bridge', 'openchat_bridge',
                                                 'bridge', 'openchat-bridge-v2'])]
        text = self.context(records)
        self.assertIn('📌 이 scope(openchat-bridge) 미결 2건:', text)
        self.assertIn('[0]', text)
        self.assertIn('[1]', text)
        self.assertNotIn('[2]', text)
        self.assertNotIn('[3]', text)

    def test_priority_date_id_and_limit(self) -> None:
        records = [dict(id=ident, title=ident, project='openchat-bridge', status='open',
                        value=value, next_review=date, next_action='실행')
                   for ident, value, date in [('low', 'low', '2020-01-01'),
                       ('medium', 'medium', '2020-01-01'), ('missing', 'high', None),
                       ('late', 'high', '2026-10-01'), ('b', 'high', '2026-09-01'),
                       ('a', 'high', '2026-09-01')]]
        text = self.context(records)
        self.assertIn('미결 3건', text)
        self.assertLess(text.index('[a]'), text.index('[b]'))
        self.assertLess(text.index('[b]'), text.index('[late]'))
        for ident in ('low', 'medium', 'missing'):
            self.assertNotIn('[' + ident + ']', text)
        self.assertIn('→ 다음 한 수: 실행', text)
        self.assertIn('다음 노출: 2026-09-01', text)

    def test_alias_path_normalizes_project_name(self) -> None:
        self.set_aliases({'OpenChat_Bridge': [str(self.root / 'other repo')]})
        text = self.context([dict(id='alias', title='별칭', project='OPENCHAT _BRIDGE',
                                  status='waiting_external')], 'other repo')
        self.assertIn('[alias]', text)

    def test_no_basename_fallback_for_same_name_folders(self) -> None:
        # 별칭에 경로가 없으면 폴더 이름이 project 와 같아도 붙이지 않는다.
        self.set_aliases({})
        text = self.context([dict(id='shared', title='공유', project='shared', status='open')],
                            'a/shared')
        self.assertNotIn('[shared]', text)
        self.assertNotIn('📌', text)

    def test_subfolder_inherits_alias_scope(self) -> None:
        text = self.context([dict(id='sub', title='하위', project='openchat-bridge', status='open')],
                            'openchat-bridge/src/deep')
        self.assertIn('[sub]', text)

    def test_value_order_precedes_date(self) -> None:
        text = self.context([dict(id=value, title=value, project='openchat-bridge',
                                  status='open', value=value, next_review=date)
                             for value, date in [('low', '2000-01-01'),
                                                 ('medium', '2020-01-01'),
                                                 ('high', '2099-01-01')]])
        self.assertLess(text.index('[high]'), text.index('[medium]'))
        self.assertLess(text.index('[medium]'), text.index('[low]'))

    def test_missing_dates_follow_dated_items_and_status_is_filtered(self) -> None:
        records = [dict(id=ident, title=ident, project='openchat-bridge', status=status,
                        value='medium', **extra)
                   for ident, status, extra in [('none', 'open', {}),
                       ('empty', 'open', {'next_review': ''}),
                       ('dated', 'waiting_external', {'next_review': '2099-01-01'}),
                       ('closed', 'closed', {'next_review': '2000-01-01'})]]
        text = self.context(records)
        self.assertLess(text.index('[dated]'), text.index('[empty]'))
        self.assertLess(text.index('[empty]'), text.index('[none]'))
        self.assertNotIn('[closed]', text)

    def test_absent_alias_file_and_no_match(self) -> None:
        self.aliases.unlink()
        text = self.context([dict(id='other', title='다른 프로젝트', project='bridge',
                                  status='open')])
        self.assertNotIn('📌', text)
        self.assertNotIn('칸반', text)

    # --- M3 계측 (2026-09-16) ---
    def test_injection_metrics_logged_without_body(self) -> None:
        log = self.root / 'inj.jsonl'
        self.env['SB_INJECTION_LOG'] = str(log)
        records = [dict(id=f'L{i}', title=f'이모지 😀 테스트 {i}', project='openchat-bridge',
                        status='open', value='high', next_action='다음 🚀',
                        next_review='2026-09-01') for i in range(5)]
        text = self.context(records)
        row = json.loads(log.read_text(encoding='utf-8').splitlines()[-1])
        self.assertLessEqual({'context_chars', 'items', 'omitted', 'scope_id', 'harness'}, set(row))
        self.assertNotIn('text', row)
        self.assertEqual(row['context_chars'], len(text))
        self.assertEqual(row['harness'], 'claude')
        self.assertEqual((row['items'], row['omitted']), (3, 2))
        self.assertEqual(row['scope_id'], 'openchat-bridge')

    def test_unwritable_log_does_not_block_session(self) -> None:
        self.env['SB_INJECTION_LOG'] = '/dev/null/nope/inj.jsonl'
        text = self.context([dict(id='x', title='x', project='openchat-bridge', status='open')])
        self.assertIn('[x]', text)


class CorrectionBriefingTests(_HookHarness):
    """2026-10-21: 세션 중 알림을 못 받고 끝난 교정 후보 — 같은 폴더·최근 7일·최대 3건·한 번만 세션 시작 브리핑에."""

    def setUp(self) -> None:
        super().setUp()
        self.queue = self.root / 'sbhome' / 'index' / 'correction_candidates.jsonl'
        self.state = self.root / 'sbhome' / 'index' / 'sb-recall-state'
        self.here = self.root / 'openchat-bridge'

    @staticmethod
    def ts(days_ago: float = 0) -> str:
        import time
        return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - days_ago * 86400))

    def queue_rows(self, rows, path=None) -> None:
        path = path or self.queue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')

    def row(self, rule, cwd=None, days_ago=0.0, sid='old-session'):
        return {'ts': self.ts(days_ago), 'sid': sid, 'cwd': str(self.here) if cwd is None else cwd, 'rule': rule}

    def test_same_folder_candidate_shown_once(self) -> None:
        self.queue_rows([self.row('배포 전에는 항상 테스트를 먼저 돌린다')])
        text = self.context([])
        self.assertIn('📝 지난 세션에서 규칙으로 저장되지 않은 사용자 교정 1건', text)
        self.assertIn('· 배포 전에는 항상 테스트를 먼저 돌린다', text)
        self.assertNotIn('배포 전에는', self.context([]))   # 한 번 보여 준 후보는 다시 안 뜬다
        self.assertTrue((self.state / 'corrections.briefed.json').exists())   # 기록은 SB_HOME 아래

    def test_filters_folder_age_empty_cwd_and_notified(self) -> None:
        notified = self.row('세션 중에 이미 알린 교정')
        self.queue_rows([self.row('다른 폴더 교정', cwd=str(self.root / 'elsewhere')),
                         self.row('하위 폴더 교정', cwd=str(self.here / 'src')),
                         self.row('오래된 교정', days_ago=8),
                         self.row('빈 cwd 교정', cwd=''),
                         notified, self.row('대조군 교정')])
        self.state.mkdir(parents=True, exist_ok=True)
        key = notified['ts'] + '|' + notified['rule'][:40]   # sb_recall.pending_corrections 와 같은 키
        (self.state / 'old-session.corr.json').write_text(json.dumps({'notified': [key], 'checked': []}), encoding='utf-8')
        text = self.context([])
        self.assertIn('사용자 교정 1건', text)
        self.assertIn('· 대조군 교정', text)
        for rule in ('다른 폴더', '하위 폴더', '오래된', '빈 cwd', '이미 알린'):
            self.assertNotIn(rule, text)

    def test_at_most_three_latest_and_rest_next_time(self) -> None:
        self.queue_rows([self.row('규칙 %d' % i, days_ago=0.01 * (5 - i)) for i in range(5)])
        text = self.context([])
        self.assertIn('사용자 교정 5건', text)
        for i in (2, 3, 4):
            self.assertIn('· 규칙 %d' % i, text)
        for i in (0, 1):
            self.assertNotIn('· 규칙 %d' % i, text)
        text = self.context([])   # 보여 준 3건만 '브리핑함'으로 남고, 못 보여 준 2건은 다음 세션에
        self.assertIn('사용자 교정 2건', text)
        self.assertIn('· 규칙 0', text)
        self.assertNotIn('· 규칙 4', text)

    def test_folder_compared_by_realpath(self) -> None:
        self.here.mkdir(parents=True, exist_ok=True)
        link = self.root / 'bridge-link'
        try:
            os.symlink(self.here, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('심볼릭 링크를 만들 수 없는 환경')
        self.queue_rows([self.row('링크로 기록된 교정', cwd=str(link))])
        self.assertIn('링크로 기록된 교정', self.context([]))

    def test_queue_path_from_env_and_codex_harness(self) -> None:
        alt = self.root / 'alt-queue.jsonl'
        self.env['SB_CORRECTION_QUEUE'] = str(alt)
        self.queue_rows([self.row('환경변수 대기열 교정')], path=alt)
        payload = self.run_hook([], harness='codex')
        self.assertIn('환경변수 대기열 교정', payload['hookSpecificOutput']['additionalContext'])

    def recall_note(self, sid: str) -> str:
        """같은 SB_HOME 으로 sb_recall(UserPromptSubmit)을 돌려 [교정 감지] 주입문을 받는다(판정 작업은 띄우지 않는다)."""
        result = subprocess.run([sys.executable, str(SCRIPT.parent / 'sb_recall.py')],
                                input=json.dumps({'prompt': '/x', 'session_id': sid, 'cwd': str(self.here)}),
                                env=dict(self.env, SB_CORRECTION='0'), capture_output=True, text=True, timeout=20,
                                encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['hookSpecificOutput']['additionalContext'] if result.stdout.strip() else ''

    def test_resumed_session_does_not_repeat_briefed_candidate(self) -> None:
        # 2026-10-30: 같은 세션 ID 로 이어지는 시작(resume·compact)에서 브리핑에 뜬 후보를 다음 프롬프트 [교정 감지]가 또 보여 줬다
        self.queue_rows([self.row('브리핑에 뜬 교정', sid='same-sid'),
                         self.row('브리핑 안 된 교정', sid='same-sid', cwd=str(self.root / 'elsewhere'))])
        self.assertIn('브리핑에 뜬 교정', self.context([]))
        note = self.recall_note('same-sid')
        self.assertNotIn('브리핑에 뜬 교정', note)
        self.assertIn('브리핑 안 된 교정', note)   # 대조군: 브리핑하지 않은 같은 세션 후보는 그대로 알린다

    # 2026-10-30: corrections.briefed.json 갱신(읽기·합치기·교체)은 *.corr.json 과 같은 <파일>.lock 잠금 안에서 한다
    def inproc(self):
        """같은 프로세스에서 correction_lines 를 부를 준비 — 경쟁·교체 실패를 끼워 넣으려고."""
        sys.path.insert(0, str(SCRIPT.parent))
        import session_context
        import sb_lock
        env = mock.patch.dict(os.environ, {'SB_CORRECTION_QUEUE': str(self.queue), 'SB_RECALL_STATE_DIR': str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        return session_context, sb_lock

    def briefed_keys(self) -> list:
        return json.loads((self.state / 'corrections.briefed.json').read_text(encoding='utf-8'))

    def test_lock_held_elsewhere_skips_record_quietly(self) -> None:
        _, sb_lock = self.inproc()
        self.queue_rows([self.row('잠금 중 교정')])
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / 'corrections.briefed.json.lock', 'a') as lk:   # 다른 세션이 갱신 중
            sb_lock.lock(lk, sb_lock.LOCK_EX)
            self.assertIn('잠금 중 교정', self.context([]))   # 브리핑은 그대로, 기록만 건너뛴다
            sb_lock.lock(lk, sb_lock.LOCK_UN)
        self.assertFalse((self.state / 'corrections.briefed.json').exists())
        self.assertIn('잠금 중 교정', self.context([]))   # 기록을 못 했으니 다음 세션에 한 번 더 뜬다
        self.assertNotIn('잠금 중 교정', self.context([]))

    def test_record_merges_keys_written_by_concurrent_session(self) -> None:
        sc, sb_lock = self.inproc()
        self.queue_rows([self.row('우리 세션 교정')])
        other = self.ts() + '|다른 세션 교정'
        real = sb_lock.lock_with_timeout

        def racing(stream, op, timeout=10.0):   # 우리가 처음 읽은 뒤·잠그기 전에 다른 세션이 키를 써 넣고 끝났다
            (self.state / 'corrections.briefed.json').write_text(json.dumps([other]), encoding='utf-8')
            return real(stream, op, timeout)

        with mock.patch.object(sb_lock, 'lock_with_timeout', racing):
            self.assertIn('  · 우리 세션 교정', sc.correction_lines(str(self.here)))
        keys = self.briefed_keys()
        self.assertIn(other, keys)
        self.assertEqual(len(keys), 2)

    def test_replace_retries_once_when_reader_holds_file(self) -> None:
        sc, _ = self.inproc()
        self.queue_rows([self.row('교체 재시도 교정')])
        real, calls = os.replace, []

        def flaky(src, dst):   # 윈도우: sb_recall 이 읽는 순간의 교체는 PermissionError
            calls.append(dst)
            if len(calls) == 1:
                raise PermissionError(13, 'in use')
            return real(src, dst)

        with mock.patch.object(os, 'replace', flaky):
            sc.correction_lines(str(self.here))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.briefed_keys()), 1)

    def test_broken_queue_lines_do_not_block(self) -> None:
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.queue.write_text('not json\n' + json.dumps(self.row('정상 교정'), ensure_ascii=False) + '\n[1]\n',
                              encoding='utf-8')
        text = self.context([dict(id='x', title='x', project='openchat-bridge', status='open')])
        self.assertIn('[x]', text)
        self.assertIn('정상 교정', text)


if __name__ == '__main__':
    unittest.main()
