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


HAS_NUMPY = mt._numpy() is not None


@unittest.skipUnless(HAS_NUMPY, 'numpy 없음: 벡터 단계는 건너뛰는 것이 계약')
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
        self.assertEqual(out['query_mode'], 'vector')
        self.assertEqual(out['hits'][0]['seq'], 1)
        fused = mt.search('벡터정답 질문', 1, self.idx, embed=self.fake_embed, fusion='rrf')
        self.assertEqual((fused['query_mode'], fused['hits'][0]['seq']), ('fts+vector', 1))
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
        # 못 만든 회차에는 옛 벡터 파일을 지운다
        self.assertIsNone(mt.status(self.idx)['vector'])
        self.assertFalse((self.idx / mt.VEC_NAME).exists())
        self.assertFalse((self.idx / mt.VEC_IDS_NAME).exists())
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


class ReviewRegressionTests(Fixture):
    """11-16 검토 반영: 사이드카·front-matter 충돌, 키 대소문자·중복, meta.md 본문, 저장 내용, id 충돌, 결합."""

    def write(self, rel, text):
        path = self.src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_shared_stem_sidecar_cannot_unlock_private_front_matter(self):
        self.write('a.md', '---\nsensitivity: private\ndate: 2026-01-01\n---\n비밀본문단어\n')
        self.write('a.txt', '공개본문단어\n')
        _write_json(self.src / 'a.meta.json', {'sensitivity': 'public', 'date': '2026-01-01'})
        result = mt.index([self.src], self.idx, no_vector=True)
        self.assertEqual(result['indexed'], 0)
        self.assertEqual(result['rejected'], {'ambiguous_meta': 2})
        self.assertEqual(mt.status(self.idx)['meetings'], 0)

    def test_sidecar_public_does_not_override_private_front_matter(self):
        p = self.write('b.md', '---\nsensitivity: private\ndate: 2026-01-01\n---\n본문\n')
        _write_json(self.src / 'b.meta.json', {'sensitivity': 'public'})
        self.assertEqual(mt.from_text(p)['sensitivity'], 'private')
        _write_json(self.src / 'b.md.meta.json', {'sensitivity': 'public'})
        self.assertEqual(mt.from_text(p)['sensitivity'], 'private')
        # 인자 meta 의 public 도 front-matter 의 private 을 풀지 못한다
        self.assertEqual(mt.from_text(p, {'sensitivity': 'public'})['sensitivity'], 'private')
        result = mt.index([self.src], self.idx, no_vector=True)
        self.assertEqual((result['indexed'], result['rejected']), (0, {'private': 1}))

    def test_exact_name_sidecar_is_unambiguous(self):
        self.write('c.md', '---\ndate: 2026-01-01\n---\n정확사이드카단어\n')
        self.write('c.txt', '다른 파일\n')
        _write_json(self.src / 'c.md.meta.json', {'id': 'c-md', 'sensitivity': 'public'})
        _write_json(self.src / 'c.txt.meta.json', {'id': 'c-txt', 'sensitivity': 'private', 'date': '2026-01-01'})
        result = mt.index([self.src], self.idx, no_vector=True)
        self.assertEqual((result['indexed'], result['rejected']), (1, {'private': 1}))

    def test_sidecar_null_or_mixed_values(self):
        p = self.write('d.md', '---\nsensitivity: public\ndate: 2026-01-01\n---\n본문\n')
        _write_json(self.src / 'd.meta.json', {'sensitivity': None})
        self.assertIsNone(mt.from_text(p)['sensitivity'])
        _write_json(self.src / 'd.meta.json', {'sensitivity': 'internal'})
        self.assertEqual(mt.from_text(p)['sensitivity'], 'internal')
        _write_json(self.src / 'd.meta.json', ['not', 'object'])
        with self.assertRaises(mt.MetaError):
            mt.from_text(p)

    def test_strictest_sensitivity(self):
        f = mt.strictest_sensitivity
        self.assertIsNone(f([]))
        self.assertEqual(f(['public', ' Public ']), 'public')
        self.assertEqual(f(['public', 'private']), 'private')
        self.assertEqual(f(['internal', 'public']), 'internal')
        self.assertEqual(f(['internal', 'private']), 'private')
        self.assertIsNone(f(['public', '']))
        self.assertIsNone(f(['public', None]))

    def test_front_matter_duplicate_and_case_keys(self):
        p = self.write('e.md', '---\nsensitivity: private\nsensitivity: public\ndate: 2026-01-01\n---\nb\n')
        self.assertEqual(mt.from_text(p)['sensitivity'], 'private')
        p = self.write('f.md', '---\nSensitivity: private\nsensitivity: public\ndate: 2026-01-01\n---\nb\n')
        self.assertEqual(mt.from_text(p)['sensitivity'], 'private')
        p = self.write('g.md', '---\nSENSITIVITY: Public\nID: G1\ndate: 2026-01-01\n---\nb\n')
        doc = mt.from_text(p)
        self.assertEqual((doc['sensitivity'], doc['id']), ('public', 'G1'))
        # 같은 키는 앞의 값이 이긴다(meta.md 와 같은 규칙)
        p = self.write('h.md', '---\ntitle: 첫 제목\ntitle: 둘째\nsensitivity: public\n---\nb\n')
        self.assertEqual(mt.from_text(p)['title'], '첫 제목')

    def test_meta_md_case_duplicate_and_body_bullets(self):
        d = self.src / 'm'
        self.write('m/transcript.txt', '회의 내용\n')
        self.write('m/meta.md', '- Sensitivity: private\n- sensitivity: public\n- date: 2026-01-01\n')
        self.assertEqual(mt.from_corpus_dir(d)['sensitivity'], 'private')
        self.write('m/meta.md', '# 회의\n- date: 2026-01-01\n## 요약\n- sensitivity: public 로 할지 논의함\n')
        self.assertIsNone(mt.from_corpus_dir(d)['sensitivity'])
        self.write('m/meta.md', '# 회의\n\n- date: 2026-01-01\n\n본문 문단\n- sensitivity: public\n')
        self.assertIsNone(mt.from_corpus_dir(d)['sensitivity'])
        # 운영 meta.md 꼴(제목 한 줄 + 빈 줄 + 글머리표 묶음)은 그대로 읽는다
        self.write('m/meta.md', '# meta — m\n\n- meeting_id: mid\n- date: 2026-01-01\n- sensitivity: public\n')
        self.assertEqual(mt.from_corpus_dir(d)['sensitivity'], 'public')

    def test_bom_files_are_read(self):
        bom = '﻿'
        (self.src / 'n.json').write_text(bom + json.dumps({'id': 'n', 'created_at': '2026-01-01',
                                                            'sensitivity': 'public', 'transcription_text': '본문'},
                                                           ensure_ascii=False), encoding='utf-8')
        self.assertEqual(mt.from_meetnote_json(self.src / 'n.json')['sensitivity'], 'public')
        p = self.write('o.txt', '본문\n')
        (self.src / 'o.meta.json').write_text(bom + json.dumps({'sensitivity': 'public'}), encoding='utf-8')
        self.assertEqual(mt.from_text(p)['sensitivity'], 'public')
        d = self.src / 'q'
        self.write('q/transcript.txt', '내용')
        self.write('q/meta.md', bom + '- sensitivity: public\n- date: 2026-01-01\n')
        self.assertEqual(mt.from_corpus_dir(d)['sensitivity'], 'public')

    def test_index_file_holds_no_plain_tokens_summary_or_pii(self):
        text = ('평문토큰감지어 회의 연락처 010-1234-5678 메일 kim.ab@example.com 계좌 110-234-567890 '
                + _filler(200, 1))
        self.meetnote('pub.json', 'pub', '공개 회의', 'public', text=text, summary='요약감지어 문장')
        mt.index([self.src], self.idx, no_vector=True)
        raw = (self.idx / mt.FTS_NAME).read_bytes()
        for needle in ('평문토큰감지어', '요약감지어', '010-1234-5678', '01012345678', '110-234-567890',
                       'example.com', 'kim.ab'):
            self.assertNotIn(needle.encode('utf-8'), raw, needle)
        with sqlite3.connect(str(self.idx / mt.FTS_NAME)) as conn:
            cols = [r[1] for r in conn.execute('PRAGMA table_info(meetings)')]
            body = ' '.join(r[0] for r in conn.execute('SELECT body FROM chunk_fts'))
        self.assertNotIn('summary', cols)
        self.assertTrue(body and all(t.startswith('h') and len(t) == 17 for t in body.split()))
        # 해시해도 형태소 질의는 그대로 맞고, 카드 요약은 소스에서 읽는다
        hit = mt.search('평문토큰감지어', 1, self.idx, no_vector=True)['hits']
        self.assertEqual([h['meeting_id'] for h in hit], ['pub'])
        cards = mt.search('공개 회의', 1, self.idx, no_vector=True)['cards']
        self.assertEqual(cards[0]['summary'], '요약감지어 문장')
        # 개인정보 꼴은 토큰에서 빠져 검색어로도 맞지 않는다
        self.assertEqual(mt.search('010-1234-5678', 1, self.idx, no_vector=True)['hits'], [])

    def test_deleted_meeting_not_left_in_file(self):
        self.meetnote('pub.json', 'pub', '지움감지제목', 'public', text='본문 ' + _filler(100, 1))
        mt.index([self.src], self.idx, no_vector=True)
        (self.src / 'pub.json').rename(self.root / 'gone.json')
        mt.index([self.src], self.idx, no_vector=True)
        for name in (mt.FTS_NAME, mt.FTS_NAME + '-wal', mt.FTS_NAME + '-journal'):
            path = self.idx / name
            if path.exists():
                self.assertNotIn('지움감지제목'.encode('utf-8'), path.read_bytes(), name)

    def test_old_schema_index_is_rebuilt(self):
        self.idx.mkdir()
        with sqlite3.connect(str(self.idx / mt.FTS_NAME)) as conn:
            conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute("INSERT INTO meta VALUES ('schema', 'sb-meeting-1')")
            conn.execute('CREATE TABLE leftover (body TEXT)')
            conn.execute("INSERT INTO leftover VALUES ('옛평문')")
        self.meetnote('pub.json', 'pub', '공개 회의', 'public', text='새본문단어 ' + _filler(100, 1))
        result = mt.index([self.src], self.idx, no_vector=True)
        self.assertEqual(result['indexed'], 1)
        self.assertNotIn('옛평문'.encode('utf-8'), (self.idx / mt.FTS_NAME).read_bytes())
        foreign = self.root / 'foreign'
        foreign.mkdir()
        with sqlite3.connect(str(foreign / mt.FTS_NAME)) as conn:
            conn.execute('CREATE TABLE other (x)')
        with self.assertRaises(ValueError):
            mt.index([self.src], foreign, no_vector=True)
        self.assertTrue((foreign / mt.FTS_NAME).exists())

    def test_id_collision_across_sources_is_rejected_not_overwritten(self):
        a, b = self.root / 'a', self.root / 'b'
        a.mkdir(), b.mkdir()
        _write_json(a / '1.json', {'id': 'same', 'title': 'A', 'created_at': '2026-01-01',
                                   'sensitivity': 'public', 'transcription_text': '에이소스단어'})
        _write_json(b / '2.json', {'id': 'same', 'title': 'B', 'created_at': '2026-01-02',
                                   'sensitivity': 'public', 'transcription_text': '비소스단어'})
        mt.index([a], self.idx, no_vector=True)
        result = mt.index([b], self.idx, no_vector=True)
        self.assertEqual((result['indexed'], result['rejected']), (0, {'id_conflict': 1}))
        self.assertEqual(len(mt.search('에이소스단어', 1, self.idx, no_vector=True)['hits']), 1)
        # 다른 소스의 거부된(private) 문서가 같은 id 의 public 회의를 지우지 않는다
        _write_json(b / '2.json', {'id': 'same', 'created_at': '2026-01-02', 'sensitivity': 'private',
                                   'transcription_text': '비소스단어'})
        result = mt.index([b], self.idx, no_vector=True)
        self.assertEqual(result['rejected'], {'private': 1})
        self.assertEqual(mt.status(self.idx)['meetings'], 1)

    def test_case_variant_source_path_is_same_source(self):
        src = self.root / 'CaseSrc'
        src.mkdir()
        _write_json(src / '1.json', {'id': 'c1', 'created_at': '2026-01-01', 'sensitivity': 'public',
                                     'transcription_text': '본문'})
        mt.index([src], self.idx, no_vector=True)
        other = self.root / 'casesrc'
        if not other.exists():
            self.skipTest('case-sensitive file system')
        self.assertEqual(mt.index([other], self.idx, no_vector=True)['rejected'], {})
        (src / '1.json').rename(self.root / 'moved.json')
        mt.index([other], self.idx, no_vector=True)
        self.assertEqual(mt.status(self.idx)['meetings'], 0)

    @unittest.skipUnless(HAS_NUMPY, 'numpy 없음')
    def test_default_fusion_is_vector_first_and_bad_fusion_fails(self):
        self.meetnote('pub.json', 'pub', '공개 회의', 'public',
                      text='예산단어 ' + _filler(496, 1) + '정답벡터' + _filler(496, 2))

        def embed(texts):
            return [[1.0, 0.0] if ('정답벡터' in t or '예산단어' in t and len(t) < 50) else [0.0, 1.0]
                    for t in texts]
        with patch.object(mt, 'MIN_FREE_BYTES', 0):
            mt.index([self.src], self.idx, embed=embed)
        out = mt.search('예산단어', 1, self.idx, embed=embed)
        self.assertEqual((out['query_mode'], out['hits'][0]['seq']), ('vector', 1))
        self.assertEqual(mt.search('예산단어', 1, self.idx, no_vector=True)['hits'][0]['seq'], 0)
        with self.assertRaises(ValueError):
            mt.search('예산단어', 1, self.idx, fusion='nope')


if __name__ == '__main__':
    unittest.main()
