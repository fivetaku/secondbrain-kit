"""M3-T3 회수: 재정렬 뒤 같은 프로젝트 몫, 자동화 프롬프트 판정, worker 대체 경로의 같은 몫·교차 거르기."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / 'bin'))
sys.path.insert(0, str(KIT / 'hooks'))
sys.path.insert(0, str(KIT / 'tests'))
import sb_recalld  # noqa: E402
import sb_recall  # noqa: E402
from test_recalld_hybrid import engine  # noqa: E402

P = lambda r: r[2]  # noqa: E731


class SameMinTests(unittest.TestCase):
    def test_default_is_two_thirds_rounded_up(self):
        with patch.dict(os.environ, {'SB_RECALLD_SAME_MIN': ''}):
            self.assertEqual([sb_recalld.same_min(n) for n in (1, 3, 5, 10)], [1, 2, 4, 7])

    def test_env_count_ratio_off_and_invalid(self):
        for raw, want in (('1', 1), ('9', 3), ('0', 0), ('-1', 0), ('0.34', 2), ('x', 2)):
            with patch.dict(os.environ, {'SB_RECALLD_SAME_MIN': raw}):
                self.assertEqual(sb_recalld.same_min(3), want, raw)


class ShareTests(unittest.TestCase):
    ROWS = [(1, 0, 'b', ''), (2, 0, 'c', ''), (3, 0, 'a', ''), (4, 0, 'b', ''), (5, 0, 'a', ''), (6, 0, 'a', '')]

    def ids(self, rows):
        return [r[0] for r in rows]

    def test_pulls_same_project_up_replacing_lowest_other(self):
        out = sb_recalld.same_project_share(self.ROWS, 'a', 3, P)
        self.assertEqual(self.ids(out), [1, 3, 5])

    def test_already_enough_keeps_order(self):
        rows = [(3, 0, 'a', ''), (1, 0, 'b', ''), (5, 0, 'a', ''), (6, 0, 'a', '')]
        self.assertEqual(self.ids(sb_recalld.same_project_share(rows, 'a', 3, P)), [3, 1, 5])

    def test_short_supply_uses_what_exists(self):
        rows = [(1, 0, 'b', ''), (2, 0, 'c', ''), (4, 0, 'b', ''), (5, 0, 'a', '')]
        self.assertEqual(self.ids(sb_recalld.same_project_share(rows, 'a', 3, P)), [1, 2, 5])
        none = [(1, 0, 'b', ''), (2, 0, 'c', ''), (4, 0, 'b', '')]
        self.assertEqual(self.ids(sb_recalld.same_project_share(none, 'a', 3, P)), [1, 2, 4])

    def test_no_project_or_off(self):
        self.assertEqual(self.ids(sb_recalld.same_project_share(self.ROWS, '', 3, P)), [1, 2, 3])
        self.assertEqual(self.ids(sb_recalld.same_project_share(self.ROWS, 'a', 3, P, k=0)), [1, 2, 3])
        with patch.dict(os.environ, {'SB_RECALLD_SAME_MIN': '0'}):
            self.assertEqual(self.ids(sb_recalld.same_project_share(self.ROWS, 'a', 3, P)), [1, 2, 3])


class EngineShareTests(unittest.TestCase):
    # 키워드 순서는 같은 프로젝트(work)가 앞인데, 하이브리드 재정렬이 다른 프로젝트(other)를 위로 올린다.
    LEX = [(10, -9.0, 'work', '거래처 단가 계약 갱신'), (11, -8.0, 'work', '거래처 단가'),
           (20, -3.0, 'other', '거래처'), (21, -2.0, 'other', '단가'), (22, -1.0, 'other', '계약')]
    ORDER = [20, 21, 22, 10, 11]

    def run_recall(self, env=None, order=None, lex=None):
        e, patches = engine(lex or self.LEX, [(20, 0.9)], order=order or self.ORDER,
                            texts={i: 't' for i in (10, 11, 20, 21, 22)})
        with patch.dict(os.environ, env or {'SB_RECALLD_SAME_MIN': ''}):
            for p in patches:
                p.start()
            try:
                return e.recall('거래처 단가 계약 어떻게 됐어', 'work', 3)
            finally:
                for p in reversed(patches):
                    p.stop()

    def test_hybrid_reorder_keeps_two_of_three_same_project(self):
        d = self.run_recall()
        self.assertEqual([it['id'] for it in d['items']], [20, 10, 11])
        self.assertEqual(sum(it['project'] == 'work' for it in d['items']), 2)
        self.assertTrue(d['gate']['pass'])            # 판정은 키워드 1위(10번)로 그대로
        self.assertEqual(d['gate']['matched'], 3)

    def test_off_restores_hybrid_order(self):
        d = self.run_recall(env={'SB_RECALLD_SAME_MIN': '0'})
        self.assertEqual([it['id'] for it in d['items']], [20, 21, 22])
        self.assertEqual(d['gate']['matched'], 3)

    def test_keyword_candidate_dropped_by_reranker_still_fills_share(self):
        # 재순위 결과에 10·11이 빠져도 키워드 후보에서 같은 프로젝트 몫을 채운다
        d = self.run_recall(order=[20, 21, 22])
        self.assertEqual([it['id'] for it in d['items']], [20, 10, 11])


class AutomationTests(unittest.TestCase):
    def setUp(self):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ('CLAUDE_MEM_INTERNAL', 'SB_RECALL', 'SB_RECALL_AUTOMATION', 'SB_RECALL_SKIP_PROJECTS')}
        self.env = patch.dict(os.environ, clean, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def auto(self, prompt='배포 스크립트 고쳐줘', cwd='/Users/x/work/alpha'):
        return sb_recall.is_automation_prompt({'cwd': cwd}, prompt)

    def test_plain_prompt_is_not_automation(self):
        self.assertFalse(self.auto())
        self.assertFalse(self.auto(cwd=''))
        self.assertFalse(self.auto(cwd='C:\\Users\\x\\work\\alpha'))

    def test_env_markers(self):
        with patch.dict(os.environ, {'CLAUDE_MEM_INTERNAL': '1'}):
            self.assertTrue(self.auto())
        with patch.dict(os.environ, {'SB_RECALL': '0'}):
            self.assertTrue(self.auto())

    def test_tmp_cwds(self):
        for cwd in ('/tmp', '/tmp/x', '/private/tmp/run', '/private/var/folders/ab/T/tmpq', tempfile.gettempdir()):
            self.assertTrue(self.auto(cwd=cwd), cwd)
        self.assertFalse(self.auto(cwd='/tmpfoo/alpha'))

    def test_home_under_tmp_is_not_automation(self):
        with tempfile.TemporaryDirectory() as t:
            home = os.path.join(os.path.realpath(t), 'Users', 'u')
            with patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home}):
                self.assertFalse(self.auto(cwd=os.path.join(home, 'proj')))
                self.assertTrue(self.auto(cwd=os.path.join(os.path.realpath(t), 'other')))

    def test_probe_phrases_and_loop_markers(self):
        for p in ('Reply exactly: PONG', 'say hi. OK only', 'Respond with only the number',
                  '<<autonomous-loop>> 다음 단계', '[SYSTEM NOTIFICATION] 작업 끝남'):
            self.assertTrue(self.auto(prompt=p), p)

    def test_skip_projects_default_and_override(self):
        self.assertTrue(self.auto(cwd='/Users/x/insane-writer'))
        self.assertTrue(self.auto(cwd='/Users/x/model-probe/'))
        with patch.dict(os.environ, {'SB_RECALL_SKIP_PROJECTS': 'alpha'}):
            self.assertTrue(self.auto())
            self.assertFalse(self.auto(cwd='/Users/x/insane-writer'))
        with patch.dict(os.environ, {'SB_RECALL_SKIP_PROJECTS': ''}):
            self.assertFalse(self.auto(cwd='/Users/x/insane-writer'))

    def test_detection_can_be_turned_off(self):
        with patch.dict(os.environ, {'SB_RECALL_AUTOMATION': '0'}):
            self.assertFalse(self.auto(prompt='Reply exactly: PONG', cwd='/tmp/x'))

    def test_main_logs_automation_and_injects_nothing(self):
        logs, out = [], io.StringIO()
        payload = {'prompt': 'Reply exactly: 거래처 단가 계약 정리', 'session_id': 's-auto', 'cwd': '/Users/x/alpha'}
        with tempfile.TemporaryDirectory() as state, \
                patch.object(sb_recall, 'STATE_DIR', state), \
                patch.object(sb_recall, 'recall_log', side_effect=logs.append), \
                patch.object(sb_recall, 'correction_note', return_value=''), \
                patch.object(sb_recall, 'recalld_search') as rs, \
                patch.object(sb_recall, 'search') as ws, \
                patch('sys.stdin', io.StringIO(json.dumps(payload, ensure_ascii=False))), \
                patch('sys.stdout', out):
            sb_recall.main()
        rs.assert_not_called()
        ws.assert_not_called()
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(len(logs), 1)
        self.assertIs(logs[0]['automation'], True)
        self.assertNotIn('prompt', logs[0])
        self.assertNotIn('ids', logs[0])

    def test_main_non_automation_still_recalls(self):
        logs, out = [], io.StringIO()
        cwd = '/Users/x/alpha'
        payload = {'prompt': '거래처 단가 계약 정리해줘', 'session_id': 's-plain', 'cwd': cwd}
        item = {'id': 7, 'date': '09-01', 'title': '거래처 단가', 'project': os.path.basename(cwd)}
        with tempfile.TemporaryDirectory() as state, \
                patch.object(sb_recall, 'STATE_DIR', state), \
                patch.object(sb_recall, 'recall_log', side_effect=logs.append), \
                patch.object(sb_recall, 'correction_note', return_value=''), \
                patch.object(sb_recall, 'BACKEND', 'recalld'), \
                patch.object(sb_recall, 'recalld_search', return_value=[item]), \
                patch.object(sb_recall, 'enrich', side_effect=lambda items, sid: items), \
                patch.object(sb_recall, 'superseded_ids', return_value=set()), \
                patch.object(sb_recall, 'BODIES', 0), \
                patch('sys.stdin', io.StringIO(json.dumps(payload, ensure_ascii=False))), \
                patch('sys.stdout', out):
            sb_recall.main()
        self.assertIn('#7', out.getvalue())
        self.assertNotIn('automation', logs[-1])
        self.assertEqual(logs[-1]['ids'], [7])


def table(*rows):
    return '### Nov 1, 2026\n' + '\n'.join('| #%d | 10:00 | 🔵 | %s | ~50 |' % r for r in rows)


class WorkerSearchTests(unittest.TestCase):
    def run_search(self, proj_rows, glob_rows, project='alpha', proj_fail=False):
        def fake(path, params):
            if params.get('project'):
                if proj_fail:
                    raise OSError('down')
                return table(*proj_rows)
            return table(*glob_rows)
        with patch.object(sb_recall, '_get', side_effect=fake):
            return sb_recall.search('거래처 단가 계약 정리', project)

    def test_project_hits_kept_and_tagged_cross_filtered(self):
        out = self.run_search([(1, '아무 제목'), (2, '다른 제목'), (3, '세번째'), (4, '네번째')],
                              [(9, '거래처 단가 갱신'), (8, '거래처 메모'), (1, '아무 제목')])
        self.assertEqual([it['id'] for it in out], [1, 2, 3, 4, 9])   # 8번은 공유 단어 1개라 빠진다
        self.assertEqual({it['project'] for it in out[:4]}, {'alpha'})
        self.assertNotIn('project', out[4])

    def test_project_query_failure_falls_back_to_filtered_global(self):
        out = self.run_search([], [(9, '거래처 단가 갱신'), (8, '거래처 메모')], proj_fail=True)
        self.assertEqual([it['id'] for it in out], [9])

    def test_no_project_keeps_one_shared_token_rule(self):
        out = self.run_search([], [(9, '거래처 단가 갱신'), (8, '거래처 메모'), (7, '무관')], project='')
        self.assertEqual([it['id'] for it in out], [9, 8])

    def test_refine_keeps_project_hits_after_enrich(self):
        out = self.run_search([(1, '아무 제목'), (2, '다른 제목')], [(9, '거래처 단가 갱신')])
        with patch.object(sb_recall, 'superseded_ids', return_value=set()):
            kept = sb_recall.refine(out, '거래처 단가 계약 정리', 'alpha')
        self.assertEqual([it['id'] for it in kept][:2], [1, 2])


if __name__ == '__main__':
    unittest.main()
