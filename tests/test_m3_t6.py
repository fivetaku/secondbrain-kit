"""M3 T6 (L3 규칙 소비) — 세션 시작 규칙 제목, sb_context 규칙 최소 몫, 20,000B 넘는 규칙 파일의 출처 블록 단위 자르기."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / 'bin'
HOOK = ROOT / 'hooks' / 'session_context.py'
sys.path.insert(0, str(BIN))
import sb_context  # noqa: E402
import sb_rules  # noqa: E402
import sb_search  # noqa: E402


def rule(i: int, body: str = '본문', mtime: float = 0.0) -> dict:
    return {'path': '/work/alpha/memory/r{:03d}.md'.format(i), 'mtime': mtime or 1000.0 - i,
            'title': '규칙 {}'.format(i), 'name': 'r{}'.format(i), 'body': body}


def big_rules_file(count: int = 40, body: str = '가' * 300) -> str:
    """운영본 생성기처럼 20,000B 를 넘는 규칙 파일. 본문 안에 '## ' 로 시작하는 가짜 제목 줄을 넣는다."""
    head = [sb_rules.MARKER, sb_rules.HEADING + 'alpha', '', '설명 줄', '']
    blocks = ['## 규칙 {}\n{}\n## 가짜 제목 {}\n\n{}/work/alpha/memory/r{}.md\n'.format(i, body, i, sb_rules.SOURCE, i)
              for i in range(count)]
    return '\n'.join(head + blocks) + '\n'


class SplitAndFitTests(unittest.TestCase):
    def test_split_round_trips_and_ignores_fake_headings(self) -> None:
        text = big_rules_file(5)
        head, blocks, tail = sb_rules.split_blocks(text)
        self.assertEqual(head + ''.join(b for _, b in blocks) + tail, text)
        self.assertEqual(sb_rules.titles(text), ['규칙 {}'.format(i) for i in range(5)])

    def test_titles_of_generated_file_match_rules(self) -> None:
        text = sb_rules.render('alpha', [rule(i, '줄1\n## 본문 속 제목\n줄3') for i in range(4)])
        self.assertEqual(sb_rules.titles(text), ['규칙 {}'.format(i) for i in range(4)])

    def test_hand_written_file_without_sources_uses_headings(self) -> None:
        text = '# 손 규칙\n\n## 하나\n내용\n## 둘\n내용\n'
        self.assertEqual(sb_rules.titles(text), ['하나', '둘'])
        self.assertEqual(sb_rules.titles('# 제목 없음\n본문만\n'), [])

    def test_windows_line_endings(self) -> None:
        text = big_rules_file(3).replace('\n', '\r\n')
        self.assertEqual(sb_rules.titles(text), ['규칙 0', '규칙 1', '규칙 2'])
        fitted, dropped = sb_rules.fit_rules(text, 1200)
        self.assertGreater(dropped, 0)
        self.assertLessEqual(len(fitted.encode('utf-8')), 1200)

    def test_small_file_unchanged(self) -> None:
        text = big_rules_file(3)
        self.assertEqual(sb_rules.fit_rules(text), (text, 0))

    def test_over_limit_keeps_whole_top_blocks(self) -> None:
        text = big_rules_file(40)
        self.assertGreater(len(text.encode('utf-8')), sb_rules.READ_LIMIT)
        fitted, dropped = sb_rules.fit_rules(text)
        self.assertLessEqual(len(fitted.encode('utf-8')), sb_rules.READ_LIMIT)
        kept = sb_rules.titles(fitted)
        self.assertEqual(kept, ['규칙 {}'.format(i) for i in range(len(kept))])   # 상위(최신)부터 연속
        self.assertEqual(len(kept) + dropped, 40)
        self.assertGreater(dropped, 0)
        # 블록 중간에서 끊지 않는다 — 남은 블록마다 출처 줄이 있다
        self.assertEqual(fitted.count(sb_rules.SOURCE), len(kept))
        self.assertIn('규칙 {}건 생략'.format(dropped), fitted)
        self.assertTrue(fitted.startswith(sb_rules.MARKER))

    def test_old_byte_cut_would_split_block(self) -> None:
        # 기준선: 예전 [:20000] 절단은 마지막 블록의 출처 줄을 잃는다(가짜 제목·출처 없는 제목이 남는다)
        text = big_rules_file(40)
        cut = text.encode('utf-8')[:20000].decode('utf-8', errors='replace')
        self.assertNotEqual(cut.count('\n## 규칙 '), cut.count(sb_rules.SOURCE))

    def test_no_block_structure_cut_at_line(self) -> None:
        text = '\n'.join('줄 {}'.format(i) for i in range(5000)) + '\n'
        fitted, _ = sb_rules.fit_rules(text, 1000)
        self.assertLessEqual(len(fitted.encode('utf-8')), 1000)
        self.assertIn('이하 생략', fitted)
        body_lines = fitted.split('\n\n(')[0].splitlines()
        self.assertTrue(all(line.startswith('줄 ') for line in body_lines))


class SearchRulesModeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.rules = Path(temp.name) / 'rules'
        self.rules.mkdir()
        (self.rules / 'alpha.md').write_text(big_rules_file(40), encoding='utf-8')
        env = patch.dict(os.environ, {'SB_RULES_DIR': str(self.rules)})
        env.start()
        self.addCleanup(env.stop)

    def test_rules_mode_trims_by_block(self) -> None:
        body = sb_search._rules_items('alpha')['items'][0]['body']
        self.assertLessEqual(len(body.encode('utf-8')), 20000)
        self.assertEqual(body.count('\n## 규칙 '), body.count(sb_rules.SOURCE))
        self.assertIn('생략', body)

    def test_rules_mode_old_cut_switch(self) -> None:
        with patch.dict(os.environ, {'SB_RULES_FIT': '0'}):
            body = sb_search._rules_items('alpha')['items'][0]['body']
        self.assertEqual(body.encode('utf-8'), (self.rules / 'alpha.md').read_bytes()[:20000])


class ContextRulesShareTests(unittest.TestCase):
    RULES = [{'body': sb_rules.render('work', [rule(i) for i in range(5)])}]

    def layers(self, many_facts: int):
        facts = [{'fact_key': 'k{}'.format(i), 'body': '사실 ' + '나' * 200, 'evidence_ref': 'prompt:{}'.format(i),
                  'checked_at': '2026-11-01'} for i in range(many_facts)]
        return lambda name, scope: {'current': facts, 'next': [], 'rules': self.RULES}[name]

    def build(self, max_bytes: int, facts: int = 40, env=None) -> str:
        with patch.dict(os.environ, env or {}), patch.object(sb_context, 'mode', side_effect=self.layers(facts)), \
                patch.object(sb_context, 'recall', return_value=[]):
            return sb_context.build('', 'work', max_bytes, '')

    def test_rules_survive_full_budget(self) -> None:
        block = self.build(3000)
        self.assertIn('## 규칙(L3, 제목만)', block)
        self.assertIn('- 규칙 0', block)
        self.assertIn('- 규칙 1', block)
        self.assertIn('(용량 한도로 이하 생략)', block)
        self.assertLessEqual(len(block.rsplit('\n', 1)[0].encode('utf-8')), 3000)
        self.assertLess(block.index('## 확정 사실(L2)'), block.index('## 규칙(L3, 제목만)'))

    def test_min_share_env_and_baseline(self) -> None:
        self.assertNotIn('## 규칙', self.build(3000, env={'SB_CONTEXT_RULES_MIN': '0'}))   # 예전 동작: 규칙 블록 누락
        block = self.build(3000, env={'SB_CONTEXT_RULES_MIN': '1'})
        self.assertIn('- 규칙 0', block)
        self.assertNotIn('- 규칙 1', block)

    def test_room_left_shows_all_titles(self) -> None:
        block = self.build(6000, facts=1)
        for i in range(5):
            self.assertIn('- 규칙 {}'.format(i), block)
        self.assertNotIn('생략', block)

    def test_tiny_budget_still_bounded(self) -> None:
        block = self.build(250)
        self.assertLessEqual(len(block.rsplit('\n', 1)[0].encode('utf-8')), 250)

    def test_fake_headings_not_titles(self) -> None:
        rules = [{'body': big_rules_file(3)}]
        with patch.object(sb_context, 'mode', side_effect=lambda n, s: rules if n == 'rules' else []), \
                patch.object(sb_context, 'recall', return_value=[]):
            block = sb_context.build('', 'work', 6000, '')
        self.assertNotIn('가짜 제목', block)
        self.assertIn('- 규칙 2', block)


class SessionRulesBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rules = self.root / 'sbhome' / 'rules'
        self.cwd = self.root / 'alpha'
        self.cwd.mkdir()
        aliases = self.root / 'aliases.json'
        aliases.write_text(json.dumps({'alpha': [str(self.cwd)]}), encoding='utf-8')
        self.env = dict(os.environ, HOME=str(self.root), USERPROFILE=str(self.root), SB_HOME=str(self.root / 'sbhome'),
                        SB_PROJECT_ALIASES=str(aliases), SB_LOOPS_PATH=str(self.root / 'loops.jsonl'),
                        SB_STATE_BRIEFING='0', SB_RECALLD_AUTOSTART='0', SB_HEALTH='0', SB_VAULT_NUDGE='0',
                        SB_LOOPS_EXPOSURE='0', SB_INJECTION_LOG=str(self.root / 'inj.jsonl'))
        for key in ('SB_RULES_DIR', 'SB_CORRECTION_QUEUE', 'SB_RECALL_STATE_DIR', 'SB_RULES_BRIEF_MAX'):
            self.env.pop(key, None)

    def run_hook(self) -> str:
        p = subprocess.run([sys.executable, str(HOOK), '--harness', 'claude'], input=json.dumps({'cwd': str(self.cwd)}),
                           env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)['hookSpecificOutput']['additionalContext'] if p.stdout.strip() else ''

    def write_rules(self, count: int) -> None:
        self.rules.mkdir(parents=True, exist_ok=True)
        (self.rules / 'alpha.md').write_text(sb_rules.render('alpha', [rule(i, '줄\n## 가짜') for i in range(count)]),
                                             encoding='utf-8')

    def test_top_titles_default_five(self) -> None:
        self.write_rules(8)
        text = self.run_hook()
        self.assertIn('규칙 8건', text)
        for i in range(5):
            self.assertIn('  · 규칙 {}'.format(i), text)
        self.assertNotIn('  · 규칙 5', text)
        self.assertIn('…외 3건', text)
        self.assertNotIn('가짜', text)
        row = json.loads((self.root / 'inj.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(row['rules'], 5)

    def test_max_env_and_off(self) -> None:
        self.write_rules(3)
        self.env['SB_RULES_BRIEF_MAX'] = '2'
        text = self.run_hook()
        self.assertIn('  · 규칙 1', text)
        self.assertNotIn('  · 규칙 2', text)
        self.env['SB_RULES_BRIEF_MAX'] = '0'
        self.assertEqual(self.run_hook(), '')

    def test_no_rules_file_injects_nothing(self) -> None:
        self.assertEqual(self.run_hook(), '')
        self.rules.mkdir(parents=True)
        (self.rules / 'alpha.md').write_text('# 손 규칙\n제목 없음\n', encoding='utf-8')
        self.assertEqual(self.run_hook(), '')
        self.assertFalse((self.root / 'inj.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
