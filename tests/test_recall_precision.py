"""2026-10-05 감사 반영: 기간 질문 판정 정밀화, 회수 주입 잡음 제거, Stop 게이트 오탐 보정,
상태층 cross-scope 검증, 수집 제외 프로젝트 저장 차단, 메모리 폴더 project 기본값."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
sys.path.insert(0, str(ROOT / 'hooks'))
import recall_gate  # noqa: E402
import sb  # noqa: E402
import sb_memory  # noqa: E402
import sb_recall  # noqa: E402


class HistoryQuestionTests(unittest.TestCase):
    CASES = [
        ("<pasted_content id='x'>일정 7/16 회의</pasted_content> 이거 정리해줘", False),
        ("<pasted_content id='x'>잘린 붙여넣기 9/28 회의 했던 거", False),
        ("판매계획 9~12월에 반영 탭으로 연결 바꿔줘", False),
        ("최근 추세 기준으로 가정 다시 잡아줘", False),
        ("요청했던 안으로 정리해서 시트 만들어줘", False),
        ("이 시트에서 C열에 9/22 이후 수정된것만 조건부 서식으로 걸어서 확인할 수 있어?", False),
        ("A부터 순차적으로 진행해줘", False),
        ("단가는 왜 내려가는 가정이었지?! 그냥 목표였나?", True),
        ("저번주에 정리 도구 만들어놓고 쓰는 중인데 잘 되었는지", True),
        ("이번달에 한것들 정리해줘봐", True),
        ("거래처 계약 건 어떻게 됐어?", True),
        ("9/28에 보낸 메일 회신 왔나?", True),
        ("그거 어디까지 했지?", True),
    ]

    def test_cases(self) -> None:
        for prompt, expected in self.CASES:
            with self.subTest(prompt=prompt):
                self.assertEqual(sb_recall.is_history_question(prompt), expected)


class RefineTests(unittest.TestCase):
    def items(self):
        return [
            {'id': 1, 'title': '[automemory] 거래처 단가 계약 옛 판', 'project': 'work', '_d': '2026-09-26'},
            {'id': 2, 'title': '거래처 단가 계약 9/28 갱신', 'project': 'work', '_d': '2026-09-29'},
            {'id': 3, 'title': 'Daily 브리핑 9/30 매출', 'project': 'work', '_d': '2026-09-30'},
            {'id': 4, 'title': '운동 일정 정리', 'project': 'life', '_d': '2026-10-01'},
            {'id': 5, 'title': '거래처 단가 계약 메모', 'project': 'other', '_d': '2026-08-01'},
        ]

    def test_drops_superseded_brief_and_unrelated_cross_project_then_newest_first(self) -> None:
        with patch.object(sb_recall, 'superseded_ids', return_value={1}):
            out = sb_recall.refine(self.items(), '거래처 단가 계약 어떻게 됐어?', 'work')
        self.assertEqual([it['id'] for it in out], [2, 5])

    def test_brief_kept_when_asked(self) -> None:
        with patch.object(sb_recall, 'superseded_ids', return_value=set()):
            out = sb_recall.refine(self.items(), 'Daily 브리핑 매출 어제 어땠어', 'work')
        self.assertIn(3, [it['id'] for it in out])


class GateTests(unittest.TestCase):
    def write(self, events) -> str:
        fd, path = tempfile.mkstemp(suffix='.jsonl')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + '\n')
        self.addCleanup(os.unlink, path)
        return path

    def test_agent_with_recall_prompt_counts_as_recall(self) -> None:
        path = self.write([
            {'type': 'user', 'message': {'content': '지난주 한 일 정리해줘'}},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Agent', 'input': {'prompt': '`sb timeline --since 2026-09-26` 로 정리'}}]}},
        ])
        self.assertTrue(recall_gate.transcript_recall(path))

    def test_notification_turn_is_not_gated(self) -> None:
        path = self.write([
            {'type': 'user', 'message': {'content': '지난주 한 일 정리해줘'}},
            {'type': 'user', 'message': {'content': [{'type': 'text', 'text': '<task-notification>\n<task-id>x</task-id>'}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '결과 전달'}]}},
        ])
        self.assertEqual(recall_gate.transcript_recall(path), 'notification')


class SaveGuardTests(unittest.TestCase):
    def test_excluded_projects(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / 'config').mkdir()
            (Path(home) / 'config' / 'automemory_exclude.txt').write_text('Workspace-private-notes\n', encoding='utf-8')
            settings = {'CLAUDE_MEM_EXCLUDED_PROJECTS': 'C:\\Users\\u\\.codex\\memories,C:/w/_sbeval/**'}
            with patch.dict(os.environ, {'SB_HOME': home}), \
                    patch.object(sb_memory.sb_config, 'claude_mem_settings', return_value=settings):
                os.environ.pop('SB_SAVE_ALLOW_EXCLUDED', None)
                self.assertTrue(sb_memory.is_excluded_project('private-notes'))
                self.assertTrue(sb_memory.is_excluded_project('_sbeval'))
                self.assertFalse(sb_memory.is_excluded_project('memories'))
                self.assertFalse(sb_memory.is_excluded_project('work'))
                with patch.dict(os.environ, {'SB_SAVE_ALLOW_EXCLUDED': '1'}):
                    self.assertFalse(sb_memory.is_excluded_project('private-notes'))

    def test_save_refuses_excluded_without_posting(self) -> None:
        with patch.object(sb_memory, 'is_excluded_project', return_value=True), \
                patch.object(sb_memory, '_post') as post, patch.object(sb_memory, 'journal'):
            with self.assertRaises(RuntimeError):
                sb_memory.save_memory('본문', '제목', 'private-notes',
                                      sb_memory.build_provenance(source='t', kind='manual', origin='t',
                                                                 created_by='t', content_hash='h'))
        post.assert_not_called()

    def test_default_project_from_memory_folder(self) -> None:
        path = os.path.join('C:' + os.sep, 'u', '.claude', 'projects', 'C--Users-u-Workspace-work', 'memory')
        with patch('sync_automemory.project_from_slug', return_value='Workspace-work'), \
                patch('sync_automemory.project_map', return_value={'Workspace-work': 'work'}):
            self.assertEqual(sb._default_project(path), 'work')
        self.assertEqual(sb._default_project(os.path.join('C:' + os.sep, 'w', 'work')), 'work')


class RelabelHygieneTests(unittest.TestCase):
    def test_prune_keeps_newest_relabel_backups_only(self) -> None:
        import sb_relabel
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'claude-mem.db'
            db.write_text('x')
            other = Path(d) / 'claude-mem.bak-20260101-000000-dupfix.db'
            other.write_text('x')
            for i in range(8):
                p = Path(d) / ('claude-mem.bak-2026010%d-000000-relabel.db' % i)
                p.write_text('x')
                os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
            self.assertEqual(sb_relabel.prune_backups(db, keep=3), 5)
            left = sorted(p.name for p in Path(d).glob('*.bak-*'))
        self.assertIn(other.name, left)          # 재분류 백업이 아닌 것은 건드리지 않는다
        self.assertEqual(len([n for n in left if n.endswith('-relabel.db')]), 3)
        self.assertIn('claude-mem.bak-20260107-000000-relabel.db', left)

    def test_extra_env_pattern_from_config(self) -> None:
        import re
        import sb_relabel
        base = re.compile(sb_relabel.ENV_PATTERN, re.I)
        self.assertFalse(base.search('사내 보안 에이전트 제거'))
        self.assertTrue(re.compile(sb_relabel.ENV_PATTERN + '|보안 에이전트', re.I).search('사내 보안 에이전트 제거'))


if __name__ == '__main__':
    unittest.main()
