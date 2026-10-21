"""2026-10-20 sb_recalld 하이브리드(키워드+벡터 결합 → 한국어 재순위 섞기) 계약.
모델·Chroma 없이 가짜 객체로 순서·판정·폴백만 본다."""

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import sb_recalld  # noqa: E402


class FuseTests(unittest.TestCase):
    def test_scores_normalized_by_max_and_weighted(self) -> None:
        # 이전 방식(최소 0 정규화). 키워드 1위(a)는 벡터에 없고, 벡터 1위(b)는 키워드 2위.
        with patch.object(sb_recalld, 'FUSE_NORM', 'zero'):
            order = sb_recalld.fuse([('a', 10.0), ('b', 5.0)], [('b', 0.8), ('c', 0.4)], lex_weight=0.3)
        # a = .3*1.0 = .30, b = .3*.5 + .7*1.0 = .85, c = .7*.5 = .35
        self.assertEqual(order, ['b', 'c', 'a'])

    def test_minmax_keeps_keyword_top_above_vector_tail(self) -> None:
        # 기본(min-max): 후보 안 최저점은 0이 돼, 벡터 꼴찌(c)가 키워드 1위(a)를 넘지 못한다.
        order = sb_recalld.fuse([('a', 10.0), ('b', 5.0)], [('b', 0.8), ('c', 0.4)], lex_weight=0.3)
        # a = .3*1 = .30, b = .3*0 + .7*1 = .70, c = 0
        self.assertEqual(order, ['b', 'a', 'c'])

    def test_keyword_heavy_weight_flips_order(self) -> None:
        order = sb_recalld.fuse([('a', 10.0), ('b', 5.0)], [('b', 0.8), ('c', 0.4)], lex_weight=0.9)
        self.assertEqual(order[0], 'a')

    def test_empty_side_and_ties_are_stable(self) -> None:
        self.assertEqual(sb_recalld.fuse([], [(2, 0.5), (1, 0.5)]), [1, 2])
        self.assertEqual(sb_recalld.fuse([(3, 1.0)], []), [3])


class FakeModel:
    def __init__(self, scores):
        self.scores = scores
        self.pairs = None

    def predict(self, pairs, batch_size=16):
        self.pairs = pairs
        return self.scores


def reranker(model):
    r = sb_recalld.Reranker.__new__(sb_recalld.Reranker)
    r.model, r.error = model, None
    return r


class RerankerTests(unittest.TestCase):
    def test_blends_fused_rank_with_reranker_rank(self) -> None:
        # 재순위기는 마지막 후보(4)를 1위로 본다. 섞으면 4는 결합 1위(1)와 동점 근처로 올라오되
        # 결합 순위를 완전히 버리지 않는다: 1 = 1/11+1/14, 4 = 1/14+1/11 → 동점은 결합 순위 앞쪽(1)이 이긴다.
        model = FakeModel([0.1, 0.2, 0.3, 0.9])
        out = reranker(model).order('q', [1, 2, 3, 4], ['t1', 't2', 't3', 't4'])
        self.assertEqual(out[:2], [1, 4])
        self.assertEqual(sorted(out), [1, 2, 3, 4])
        self.assertEqual(model.pairs[0], ('q', 't1'))

    def test_strong_agreement_keeps_order(self) -> None:
        out = reranker(FakeModel([0.9, 0.5, 0.1])).order('q', [7, 8, 9], ['a', 'b', 'c'])
        self.assertEqual(out, [7, 8, 9])

    def test_no_model_or_single_candidate_returns_none(self) -> None:
        self.assertIsNone(reranker(None).order('q', [1, 2], ['a', 'b']))
        self.assertIsNone(reranker(FakeModel([1.0])).order('q', [1], ['a']))


def vectors(col):
    v = sb_recalld.Vectors.__new__(sb_recalld.Vectors)
    v.lock = threading.Lock()
    v.col, v.client, v.stack, v.error = col, None, None, None
    v.opened = 10 ** 12   # 갱신 스레드가 뜨지 않게
    return v


class VectorsTests(unittest.TestCase):
    def test_post_filters_doc_type_best_chunk_and_no_where(self) -> None:
        col = MagicMock()
        col.query.return_value = {
            'metadatas': [[{'sqlite_id': 1, 'doc_type': 'observation'}, {'sqlite_id': 1, 'doc_type': 'observation'},
                           {'sqlite_id': 9, 'doc_type': 'user_prompt'}, {'sqlite_id': 2, 'doc_type': 'observation'},
                           {'sqlite_id': True, 'doc_type': 'observation'}]],
            'distances': [[0.5, 0.2, 0.0, 0.4, 0.1]],
        }
        out = vectors(col).search('apple', 3)
        self.assertEqual([i for i, _ in out], [1, 2])
        self.assertAlmostEqual(out[0][1], 0.8)
        kwargs = col.query.call_args.kwargs
        self.assertNotIn('where', kwargs)            # doc_type 은 where 로 걸지 않는다(chroma #4089)
        self.assertEqual(kwargs['n_results'], 27)    # 관측 외 청크 몫만큼 9배

    def test_returns_none_while_closed_or_refreshing(self) -> None:
        self.assertIsNone(vectors(None).search('q', 3))
        v = vectors(MagicMock())
        v.lock.acquire()
        try:
            self.assertIsNone(v.search('q', 3))
        finally:
            v.lock.release()

    def test_query_error_is_recorded_not_raised(self) -> None:
        col = MagicMock()
        col.query.side_effect = RuntimeError('hnsw gone')
        v = vectors(col)
        self.assertIsNone(v.search('q', 3))
        self.assertIn('hnsw gone', v.error)


def engine(lexical_rows, vec, order=None, meta=None, texts=None):
    e = sb_recalld.Engine.__new__(sb_recalld.Engine)
    e.ko = MagicMock()
    e.lock = threading.Lock()
    e.last_sync = 10 ** 12
    e.df, e.n_docs = {}, 1
    e.vectors = MagicMock()
    e.vectors.search.return_value = vec
    e.reranker = MagicMock()
    e.reranker.order.return_value = order
    patches = [
        patch.object(sb_recalld.Engine, 'nouns', return_value=['계약', '단가', '거래처']),
        patch.object(sb_recalld.Engine, '_search', side_effect=lambda conn, match, project, n: lexical_rows),
        patch.object(sb_recalld.Engine, '_meta', return_value=meta or {}),
        patch.object(sb_recalld.Engine, '_texts', return_value=texts or {}),
        patch.object(sb_recalld, 'superseded_ids', return_value=set()),
        patch.object(sb_recalld, 'HYBRID', True),
    ]
    return e, patches


class EngineHybridTests(unittest.TestCase):
    LEX = [(10, -9.0, 'work', '거래처 단가 계약 갱신'), (11, -5.0, 'work', '거래처 메모')]

    def run_recall(self, e, patches):
        for p in patches:
            p.start()
        try:
            return e.recall('거래처 단가 계약 어떻게 됐어', '', 5)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_shows_hybrid_order_but_gates_on_keyword_top(self) -> None:
        # 벡터만 찾은 20번이 재순위 1위로 보이지만, 주입 판정은 키워드 1위(10번 본문)로 한다.
        e, patches = engine(self.LEX, [(20, 0.9), (10, 0.5)], order=[20, 10, 11],
                            texts={10: 'a', 11: 'b', 20: 'c'})
        d = self.run_recall(e, patches)
        self.assertEqual([it['id'] for it in d['items']], [20, 10, 11])
        self.assertTrue(d['gate']['pass'])
        self.assertEqual(d['gate']['matched'], 3)
        e.reranker.order.assert_called_once()

    def test_falls_back_to_keyword_order_without_vectors(self) -> None:
        e, patches = engine(self.LEX, None)
        d = self.run_recall(e, patches)
        self.assertEqual([it['id'] for it in d['items']], [10, 11])
        e.reranker.order.assert_not_called()

    def test_reranker_failure_keeps_fused_order(self) -> None:
        e, patches = engine(self.LEX, [(20, 0.9), (10, 0.5)], texts={10: 'a', 11: 'b', 20: 'c'})
        e.reranker.order.side_effect = RuntimeError('mps oom')
        d = self.run_recall(e, patches)
        self.assertEqual([it['id'] for it in d['items']][0], 20)   # 결합 순위(벡터 1위가 키워드 몫 0.3을 이김)

    def test_fresh_live_items_dropped_from_display_and_gate(self) -> None:
        # 키워드 1위(10번)가 방금 생긴 실시간 관찰이면 보이지도 않고 판정에도 안 쓰인다 → 11번 본문으로 판정.
        meta = {10: {'live': True, 'epoch': 10 ** 14, 'title': 't10'}, 11: {'live': False, 'epoch': 0, 'title': 't11'}}
        e, patches = engine(self.LEX, [(10, 0.9)], order=[10, 11], meta=meta, texts={10: 'a', 11: 'b'})
        d = self.run_recall(e, patches)
        self.assertEqual([it['id'] for it in d['items']], [11])
        self.assertEqual(d['gate']['matched'], 1)    # '거래처 메모'에는 '거래처'만 있다
        self.assertFalse(d['gate']['pass'])


if __name__ == '__main__':
    unittest.main()
