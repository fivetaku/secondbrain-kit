import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'hooks' / 'session_context.py'


class SessionContextTests(unittest.TestCase):
    """2026-09-16 memory-layer-refactor M2: 폴더 이름(basename) 매칭 없음.
    미결의 project 는 project_aliases.json 에 등록된 경로(scope_id) 로만 현재 폴더에 연결된다."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / 'ledger.jsonl'
        self.aliases = self.root / 'aliases.json'
        # 기본: openchat-bridge 프로젝트 ↔ <root>/openchat-bridge 경로
        self.set_aliases({'openchat-bridge': [str(self.root / 'openchat-bridge')]})
        self.env = dict(os.environ, HOME=str(self.root), USERPROFILE=str(self.root),
                        SB_HOME=str(self.root / 'sbhome'),
                        SB_LOOPS_PATH=str(self.ledger),
                        SB_PROJECT_ALIASES=str(self.aliases),
                        SB_STATE_BRIEFING='0')

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


if __name__ == '__main__':
    unittest.main()
