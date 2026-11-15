"""sb_meeting 계약: 변환기 3종, 민감도 게이트, 고정 500자 구간, 앞뒤 구간, 카드 조건, 벡터 RRF.

가짜 회의만 쓰고 모든 색인은 임시 폴더에 만든다. Ollama 는 부르지 않는다(벡터는 가짜 임베딩).
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import sb_meeting as mt  # noqa: E402


def _filler(n, seed):
    """검색어와 겹치지 않는 채움 글자 n자(숫자·ASCII 없이 한글 음절 반복)."""
    base = '가나다라마바사아자차카타파하'[seed % 7:] + '거너더러머버서어저처'
    out = (base * (n // len(base) + 1))[:n]
    return out


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / 'src'
        self.src.mkdir()
        self.idx = self.root / 'index'
        patcher = patch.dict(os.environ, {'SB_HOME': str(self.root / 'home'),
                                          'SB_OLLAMA_URL': 'http://127.0.0.1:9'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def meetnote(self, name, mid, title, sensitivity='__absent__', text=None, created='2026-10-01T09:00:00+09:00',
                 summary='요약'):
        data = {'id': mid, 'source': 'meetnote', 'title': title, 'created_at': created,
                'participants': [{'name': 'a'}, {'name': 'b'}], 'summary': summary,
                'transcription_text': text if text is not None else _filler(1200, 1)}
        if sensitivity != '__absent__':
            data['sensitivity'] = sensitivity
        return _write_json(self.src / name, data)


class ConverterTests(Fixture):
    def test_meetnote_json_fields(self):
        p = self.meetnote('a.json', 'm1', '주간 회의', 'Public')
        doc = mt.from_meetnote_json(p)
        self.assertEqual(doc['id'], 'm1')
        self.assertEqual(doc['date'], '2026-10-01')
        self.assertEqual(doc['participants_count'], 2)
        self.assertEqual(doc['sensitivity'], 'public')
        self.assertEqual(doc['source'], 'meetnote')
        self.assertEqual(len(doc['text']), 1200)

    def test_meetnote_json_without_sensitivity_is_none(self):
        doc = mt.from_meetnote_json(self.meetnote('b.json', 'm2', '회의'))
        self.assertIsNone(doc['sensitivity'])
        self.assertEqual(mt.gate(doc), 'missing_sensitivity')

    def test_meetnote_json_legacy_copy_shape(self):
        p = _write_json(self.src / 'c.json', {'id': 'old', 'title': 't', 'date': '2026-09-03',
                                              'text': '본문', 'summary': None})
        doc = mt.from_meetnote_json(p)
        self.assertEqual((doc['date'], doc['text'], doc['sensitivity']), ('2026-09-03', '본문', None))

    def test_corpus_dir(self):
        d = self.src / 'abc123'
        d.mkdir()
        (d / 'transcript.txt').write_text('전사 본문', encoding='utf-8')
        (d / 'meta.md').write_text('# meta — abc123\n\n- meeting_id: full-id\n- id8: abc123\n- title: 기획 회의\n'
                                   '- date: 2026-08-04\n- sensitivity: public\n- duration: 10m\n', encoding='utf-8')
        doc = mt.from_corpus_dir(d)
        self.assertEqual((doc['id'], doc['title'], doc['date'], doc['sensitivity'], doc['source']),
                         ('full-id', '기획 회의', '2026-08-04', 'public', 'corpus'))
        self.assertEqual(doc['text'], '전사 본문')
        self.assertEqual(mt.discover(self.src), [('corpus', d)])

    def test_text_front_matter(self):
        p = self.src / 'note.md'
        p.write_text('---\nid: t1\ntitle: "메모 회의"\ndate: 2026-07-01\nsensitivity: public\n---\n본문 줄\n',
                     encoding='utf-8')
        doc = mt.from_text(p)
        self.assertEqual((doc['id'], doc['title'], doc['date'], doc['sensitivity']),
                         ('t1', '메모 회의', '2026-07-01', 'public'))
        self.assertEqual(doc['text'], '본문 줄\n')

    def test_text_sidecar_and_argument_override(self):
        p = self.src / 'plain.txt'
        p.write_text('그냥 본문', encoding='utf-8')
        _write_json(self.src / 'plain.meta.json', {'id': 's1', 'date': '2026-07-02', 'sensitivity': 'public'})
        doc = mt.from_text(p)
        self.assertEqual((doc['id'], doc['sensitivity'], doc['text']), ('s1', 'public', '그냥 본문'))
        doc = mt.from_text(p, {'sensitivity': 'private'})
        self.assertEqual(doc['sensitivity'], 'private')
        # 사이드카 json 은 회의로 따로 잡히지 않는다
        self.assertEqual(mt.discover(self.src), [('text', p)])

    def test_text_without_meta_is_rejected(self):
        p = self.src / 'bare.txt'
        p.write_text('본문', encoding='utf-8')
        doc = mt.from_text(p)
        self.assertEqual((doc['id'], doc['sensitivity']), ('bare', None))
        self.assertEqual(mt.gate(doc), 'missing_sensitivity')


class GateAndIndexTests(Fixture):
    def three(self):
        self.meetnote('pub.json', 'pub', '공개 회의', 'public', text=_filler(700, 2) + '예산안확정')
        self.meetnote('none.json', 'none', '표기 없는 회의', text='예산안확정 ' + _filler(300, 3))
        self.meetnote('priv.json', 'priv', '비공개 회의', 'private', text='예산안확정 ' + _filler(300, 4))

    def test_three_fake_meetings_index_only_public(self):
        self.three()
        result = mt.index([self.src], self.idx, no_vector=True)
        self.assertEqual((result['seen'], result['indexed']), (3, 1))
        self.assertEqual(result['rejected'], {'missing_sensitivity': 1, 'private': 1})
        self.assertEqual(result['vector']['skipped'], 'disabled')
        with sqlite3.connect(str(self.idx / mt.FTS_NAME)) as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT meeting_id FROM meetings')], ['pub'])
            self.assertEqual({r[0] for r in conn.execute('SELECT meeting_id FROM chunks')}, {'pub'})
        found = mt.search('예산안확정', 5, self.idx, no_vector=True)
        self.assertEqual({h['meeting_id'] for h in found['hits']}, {'pub'})
        st = mt.status(self.idx)
        self.assertEqual((st['meetings'], st['chunks']), (1, 2))

    def test_other_sensitivity_and_invalid(self):
        self.meetnote('x.json', 'x', '내부', 'internal')
        self.meetnote('y.json', 'y', '날짜 없음', 'public', created='')
        result = mt.index([self.src], self.idx, no_vector=True)
        self.assertEqual(result['indexed'], 0)
        self.assertEqual(result['rejected'], {'other_sensitivity': 1, 'invalid': 1})

    def test_raw_text_is_not_stored_in_index(self):
        self.meetnote('pub.json', 'pub', '공개 회의', 'public', text='고유문장열쇠말 ' + _filler(100, 1))
        mt.index([self.src], self.idx, no_vector=True)
        raw = (self.idx / mt.FTS_NAME).read_bytes()
        self.assertNotIn('고유문장열쇠말 '.encode('utf-8'), raw)

    def test_becoming_private_removes_meeting(self):
        p = self.meetnote('pub.json', 'pub', '공개 회의', 'public', text='공개단어 ' + _filler(100, 1))
        mt.index([self.src], self.idx, no_vector=True)
        self.assertEqual(len(mt.search('공개단어', 3, self.idx, no_vector=True)['hits']), 1)
        data = json.loads(p.read_text(encoding='utf-8'))
        data['sensitivity'] = 'private'
        _write_json(p, data)
        # 다시 색인하기 전에도 검색은 소스를 다시 확인해 빼낸다
        self.assertEqual(mt.search('공개단어', 3, self.idx, no_vector=True)['hits'], [])
        mt.index([self.src], self.idx, no_vector=True)
        self.assertEqual(mt.status(self.idx)['meetings'], 0)

    def test_changed_source_is_dropped_until_reindexed(self):
        p = self.meetnote('pub.json', 'pub', '공개 회의', 'public', text='변경감지단어 ' + _filler(100, 1))
        mt.index([self.src], self.idx, no_vector=True)
        data = json.loads(p.read_text(encoding='utf-8'))
        data['transcription_text'] = '변경감지단어 바뀐 본문'
        _write_json(p, data)
        out = mt.search('변경감지단어', 3, self.idx, no_vector=True)
        self.assertEqual((out['hits'], out['dropped_meetings']), ([], 1))

    def test_other_source_meetings_survive(self):
        a, b = self.root / 'a', self.root / 'b'
        a.mkdir(), b.mkdir()
        _write_json(a / '1.json', {'id': 'a1', 'title': 'A', 'created_at': '2026-01-01', 'sensitivity': 'public',
                                   'transcription_text': '본문'})
        _write_json(b / '2.json', {'id': 'b1', 'title': 'B', 'created_at': '2026-01-02', 'sensitivity': 'public',
                                   'transcription_text': '본문'})
        mt.index([a], self.idx, no_vector=True)
        mt.index([b], self.idx, no_vector=True)
        self.assertEqual(mt.status(self.idx)['meetings'], 2)
        trash = a / '1.json'
        trash.rename(self.root / 'moved.json')
        mt.index([a], self.idx, no_vector=True)
        self.assertEqual(mt.status(self.idx)['meetings'], 1)

    def test_default_index_dir_is_sb_home(self):
        self.assertEqual(mt.default_index_dir(), self.root / 'home' / 'index')
        self.assertEqual(mt.status()['exists'], False)
        self.assertFalse((self.root / 'home').exists())


class ChunkTests(Fixture):
    def test_fixed_500_no_overlap(self):
        text = ''.join(chr(0xAC00 + i % 500) for i in range(1234))
        pieces = mt.chunk_text(text)
        self.assertEqual([o for o, _ in pieces], [0, 500, 1000])
        self.assertEqual([len(p) for _, p in pieces], [500, 500, 234])
        self.assertEqual(''.join(p for _, p in pieces), text)
        self.assertEqual(mt.chunk_text(''), [])
        self.assertEqual([len(p) for _, p in mt.chunk_text('가' * 500)], [500])
        self.assertEqual([len(p) for _, p in mt.chunk_text('가' * 501)], [500, 1])

    def test_index_records_boundaries(self):
        self.meetnote('pub.json', 'pub', '공개 회의', 'public', text='가' * 1001)
        mt.index([self.src], self.idx, no_vector=True)
        with sqlite3.connect(str(self.idx / mt.FTS_NAME)) as conn:
            rows = list(conn.execute('SELECT seq, offset, length FROM chunks ORDER BY seq'))
        self.assertEqual(rows, [(0, 0, 500), (1, 500, 500), (2, 1000, 1)])


class NeighborTests(Fixture):
    def setUp(self):
        super().setUp()
        parts = [_filler(500, i) for i in range(5)]
        parts[2] = ' 수박 ' + parts[2][4:]
        parts[0] = '포도 ' + parts[0][3:]
        parts[4] = parts[4][:497] + ' 사과'
        self.parts = parts
        self.meetnote('pub.json', 'pub', '공개 회의', 'public', text=''.join(parts))
        mt.index([self.src], self.idx, no_vector=True)

    def test_middle_hit_gets_both_neighbors(self):
        hit = mt.search('수박', 1, self.idx, no_vector=True)['hits'][0]
        self.assertEqual((hit['seq'], hit['seqs']), (2, [1, 2, 3]))
        self.assertEqual(hit['text'], ''.join(self.parts[1:4]))
        self.assertEqual(set(hit), {'meeting_id', 'date', 'title', 'seq', 'seqs', 'text'})

    def test_edges_have_one_neighbor(self):
        first = mt.search('포도', 1, self.idx, no_vector=True)['hits'][0]
        self.assertEqual((first['seqs'], first['text']), ([0, 1], ''.join(self.parts[:2])))
        last = mt.search('사과', 1, self.idx, no_vector=True)['hits'][0]
        self.assertEqual((last['seqs'], last['text']), ([3, 4], ''.join(self.parts[3:])))

    def test_k_limits_hits_and_no_match(self):
        self.assertEqual(len(mt.search('수박 포도 사과', 2, self.idx, no_vector=True)['hits']), 2)
        self.assertEqual(mt.search('없는말', 3, self.idx, no_vector=True)['hits'], [])
        self.assertEqual(mt.search('수박', 0, self.idx, no_vector=True)['hits'], [])


class CardTests(Fixture):
    def test_card_conditions(self):
        self.assertTrue(mt.card_matches('주간 기획회의에서 뭐 정했지', '주간 기획 회의', '2026-10-01'))
        self.assertTrue(mt.card_matches('2026-10-01 회의 내용', '다른 이름', '2026-10-01'))
        self.assertTrue(mt.card_matches('2026년 10월 1일 결정', '다른 이름', '2026-10-01'))
        self.assertTrue(mt.card_matches('10월 1일에 뭐 했지', '다른 이름', '2026-10-01'))
        self.assertFalse(mt.card_matches('예산은 얼마로 정했지', '주간 기획 회의', '2026-10-01'))
        self.assertFalse(mt.card_matches('10월 11일 회의', '다른 이름', '2026-10-01'))
        self.assertFalse(mt.card_matches('2026-10-02', '다른 이름', '2026-10-01'))
        self.assertFalse(mt.card_matches('회 의', '회', '2026-10-01'))

    def test_search_attaches_cards_only_when_named(self):
        self.meetnote('pub.json', 'pub', '주간 기획 회의', 'public', text='예산단어 ' + _filler(100, 1),
                      summary='카드 요약')
        mt.index([self.src], self.idx, no_vector=True)
        plain = mt.search('예산단어', 3, self.idx, no_vector=True)
        self.assertEqual(plain['cards'], [])
        named = mt.search('주간 기획 회의 예산단어', 3, self.idx, no_vector=True)
        self.assertEqual([c['meeting_id'] for c in named['cards']], ['pub'])
        self.assertEqual(named['cards'][0]['summary'], '카드 요약')
        dated = mt.search('10월 1일 예산단어', 3, self.idx, no_vector=True)
        self.assertEqual(len(dated['cards']), 1)


class VectorTests(Fixture):
    def fake_embed(self, texts):
        # '벡터정답' 이 들어간 글은 한 방향, 나머지는 다른 방향
        return [[1.0, 0.0] if '벡터정답' in t else [0.0, 1.0] for t in texts]

    def test_vector_built_and_fused(self):
        self.meetnote('pub.json', 'pub', '공개 회의', 'public',
                      text=_filler(500, 1) + '벡터정답' + _filler(496, 2) + _filler(500, 3))
        with patch.object(mt, 'MIN_FREE_BYTES', 0):
            result = mt.index([self.src], self.idx, embed=self.fake_embed)
        self.assertTrue(result['vector']['built'])
        self.assertEqual(result['vector']['vectors'], 3)
        self.assertTrue((self.idx / mt.VEC_NAME).exists())
        out = mt.search('벡터정답 질문', 1, self.idx, embed=self.fake_embed)
        self.assertEqual(out['query_mode'], 'fts+vector')
        self.assertEqual(out['hits'][0]['seq'], 1)
        self.assertTrue(mt.status(self.idx)['vector']['current'])
        # 재색인 때 같은 구간 벡터는 다시 쓴다
        with patch.object(mt, 'MIN_FREE_BYTES', 0):
            again = mt.index([self.src], self.idx, embed=self.fake_embed)
        self.assertEqual((again['vector']['embedded'], again['vector']['reused']), (0, 3))

    def test_stale_vectors_ignored_and_low_disk_skips(self):
        self.meetnote('pub.json', 'pub', '공개 회의', 'public', text='벡터정답 ' + _filler(100, 1))
        with patch.object(mt, 'MIN_FREE_BYTES', 0):
            mt.index([self.src], self.idx, embed=self.fake_embed)
        with patch.object(mt, 'MIN_FREE_BYTES', 1 << 62):
            result = mt.index([self.src], self.idx, embed=self.fake_embed)
        self.assertEqual(result['vector']['skipped'], 'disk_below_5gb')
        self.assertFalse(mt.status(self.idx)['vector']['current'])
        self.assertEqual(mt.search('벡터정답', 1, self.idx, embed=self.fake_embed)['query_mode'], 'fts')

    def test_ollama_unreachable_skips_vector(self):
        self.meetnote('pub.json', 'pub', '공개 회의', 'public')
        with patch.object(mt, 'MIN_FREE_BYTES', 0):
            result = mt.index([self.src], self.idx)
        self.assertEqual(result['vector']['skipped'], 'ollama_unavailable')

    def test_rrf(self):
        self.assertEqual(mt.rrf([[1, 2, 3], [3, 1]]), [1, 3, 2])


class CliTests(Fixture):
    def test_cli_index_search_status(self):
        self.meetnote('pub.json', 'pub', '공개 회의', 'public', text='명령어 ' + _filler(100, 1))
        self.meetnote('priv.json', 'priv', '비공개', 'private')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mt.main(['index', '--source', str(self.src), '--index-dir', str(self.idx),
                                      '--no-vector']), 0)
        self.assertEqual(json.loads(out.getvalue())['indexed'], 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mt.main(['search', '명령어', '--k', '1', '--index-dir', str(self.idx),
                                      '--no-vector']), 0)
        self.assertEqual(json.loads(out.getvalue())['hits'][0]['meeting_id'], 'pub')
        env = dict(os.environ)
        proc = subprocess.run([sys.executable, str(BIN / 'sb_meeting.py'), 'status', '--index-dir', str(self.idx)],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['meetings'], 1)

    def test_cli_missing_source_fails(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(mt.main(['index', '--source', str(self.root / 'nope'), '--index-dir', str(self.idx)]), 1)


if __name__ == '__main__':
    unittest.main()
