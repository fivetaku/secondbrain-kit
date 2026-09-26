import os
import contextlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import sb_memory  # noqa: E402


class StateLayerTests(unittest.TestCase):
    """2026-09-16 memory-layer-refactor M4: L2 현재 상태 층 파일럿."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / 'state.db')
        self.env = mock.patch.dict(os.environ, {'SB_HOME': str(Path(self.temp.name) / 'sbhome'), 'SB_STATE_DB': self.db,
                                                'SB_STATE_PILOT_SCOPES': 'demo-proj,other'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def put(self, fact_key='port', body='37701', write_id='w1', scope='demo-proj', **kw):
        return sb_memory.state_put(scope, fact_key, body, 'fact', 'test', write_id, **kw)

    def test_schema_columns(self):
        self.put()
        with contextlib.closing(sqlite3.connect(self.db)) as db:  # 윈도우는 열린 DB 파일을 못 지운다
            cols = [r[1] for r in db.execute('PRAGMA table_info(state)')]
        for c in ('memory_kind', 'scope_id', 'fact_key', 'version', 'is_head',
                  'observation_ref', 'observed_at', 'recorded_at', 'body'):
            self.assertIn(c, cols)

    def test_new_version_replaces_head_atomically(self):
        first = self.put(write_id='w1')
        second = self.put(body='37702', write_id='w2')
        self.assertEqual(second['superseded'], first['id'])
        head = sb_memory.state_head('demo-proj', 'port')
        self.assertEqual(len(head), 1)
        self.assertEqual((head[0]['body'], head[0]['version']), ('37702', 2))
        hist = sb_memory.state_history('demo-proj', 'port')
        self.assertEqual([h['version'] for h in hist], [1, 2])
        self.assertEqual(hist[0]['superseded_by'], second['id'])
        self.assertEqual(hist[0]['is_head'], 0)

    def test_other_scope_or_fact_key_head_untouched(self):
        a = self.put(scope='demo-proj', write_id='w1')
        b = self.put(scope='other', write_id='w2')
        c = self.put(scope='demo-proj', fact_key='path', body='/x', write_id='w3')
        self.put(scope='demo-proj', body='new', write_id='w4')
        self.assertEqual(sb_memory.state_head('other', 'port')[0]['id'], b['id'])
        self.assertEqual(sb_memory.state_head('demo-proj', 'path')[0]['id'], c['id'])
        self.assertEqual(sb_memory.state_history('demo-proj', 'port')[0]['id'], a['id'])
        # head 조회는 scope 를 넘지 않는다
        self.assertEqual({r['scope_id'] for r in sb_memory.state_head('demo-proj')}, {'demo-proj'})

    def test_rejects_missing_scope_and_non_pilot_scope(self):
        with self.assertRaises(ValueError):
            sb_memory.state_put('', 'k', 'b', 'fact', 'test', 'w')
        with self.assertRaises(ValueError):
            sb_memory.state_put('not-pilot', 'k', 'b', 'fact', 'test', 'w')

    def test_rejects_bad_kind_and_empty_fields(self):
        with self.assertRaises(ValueError):
            sb_memory.state_put('demo-proj', 'k', 'b', 'bogus', 'test', 'w')
        with self.assertRaises(ValueError):
            self.put(fact_key='  ')
        with self.assertRaises(ValueError):
            self.put(body='')

    def test_version_conflict(self):
        self.put(write_id='w1')
        with self.assertRaises(ValueError):
            self.put(body='x', write_id='w2', expected_version=0)
        r = self.put(body='x', write_id='w3', expected_version=1)
        self.assertEqual(r['version'], 2)

    def test_dedup_key_is_per_write_not_per_session(self):
        # 같은 세션(source)에서 두 건 — write_id 가 다르면 둘 다 저장된다
        r1 = self.put(fact_key='a', write_id='sess1:1')
        r2 = self.put(fact_key='b', write_id='sess1:2')
        self.assertFalse(r1['skipped'] or r2['skipped'])
        # 같은 write_id 재시도는 생략
        r3 = self.put(fact_key='a', write_id='sess1:1')
        self.assertTrue(r3['skipped'])
        self.assertEqual(len(sb_memory.state_history('demo-proj', 'a')), 1)
        with self.assertRaises(ValueError):
            sb_memory.state_dedup_key('demo-proj', 'test', '')

    def test_observed_at_stays_null_when_unknown(self):
        self.put()
        row = sb_memory.state_head('demo-proj', 'port')[0]
        self.assertIsNone(row['observed_at'])
        self.assertTrue(row['recorded_at'])

    def test_memory_kind_separate_from_provenance_kind(self):
        self.assertNotIn('fact', sb_memory.PROVENANCE_KINDS)
        with self.assertRaises(ValueError):
            sb_memory.build_provenance('t', 'fact', 'o', 'c')

    def test_pilot_scopes_default_allows_all_and_explicit_list_restricts(self):
        # kit 기본값: 미설정·빈 값 = 전 scope 허용 (구 운영판은 단일 scope 한정이 기본이었다 — 의도적 변경)
        for unset in (None, '', ' , '):
            with self.subTest(env=unset), mock.patch.dict(os.environ, {}):
                if unset is None:
                    os.environ.pop('SB_STATE_PILOT_SCOPES', None)
                else:
                    os.environ['SB_STATE_PILOT_SCOPES'] = unset
                self.assertTrue(sb_memory.state_scope_allowed('anything'))
                r = self.put(scope='free-scope', write_id='free-%r' % unset)
                self.assertFalse(r['skipped'])
        with mock.patch.dict(os.environ, {'SB_STATE_PILOT_SCOPES': '*'}):
            self.assertTrue(sb_memory.state_scope_allowed('x'))
        with mock.patch.dict(os.environ, {'SB_STATE_PILOT_SCOPES': 'a, b'}):
            self.assertEqual(sb_memory.state_pilot_scopes(), ('a', 'b'))
            self.assertTrue(sb_memory.state_scope_allowed('b'))
            self.assertFalse(sb_memory.state_scope_allowed('c'))
            with self.assertRaises(ValueError):
                self.put(scope='c', write_id='blocked')

    def test_default_state_db_is_under_sb_home(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop('SB_STATE_DB', None)
            self.assertEqual(sb_memory.state_db_path(), Path(self.temp.name) / 'sbhome' / 'state.db')

    def test_concurrent_puts_keep_single_head(self):
        errors = []

        def worker(n):
            try:
                self.put(body=str(n), write_id=f'c{n}')
            except Exception as exc:  # 잠금 대기 실패 등은 기록만
                errors.append(exc)
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            heads = db.execute(
                "SELECT count(*) FROM state WHERE scope_id='demo-proj' AND fact_key='port' AND is_head=1").fetchone()[0]
        self.assertEqual(heads, 1)
        total = len(sb_memory.state_history('demo-proj', 'port'))
        self.assertEqual(total + len(errors), 8)


if __name__ == '__main__':
    unittest.main()
