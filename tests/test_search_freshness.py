"""Read-through freshness against real, temporary SQLite and Chroma stores."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from chromadb.api.client import Client
from chromadb.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
sb_embedding = importlib.import_module('sb_embedding')
sb_lock = importlib.import_module('sb_lock')
ko = importlib.import_module('sb_fts_ko')
searcher = importlib.import_module('sb_search')


class Encoder:
    def encode(self, texts, **kwargs):
        return [[1.0, 0.0] for text in texts]


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db = self.root / 'source?#.sqlite'
        self.index = self.root / 'ko.sqlite'
        self.chroma = self.root / 'chroma'
        self.snapshot = self.root / 'snapshot'
        env = patch.dict(os.environ, {
            'SB_CLAUDE_MEM_DB': str(self.db), 'SB_KO_INDEX': str(self.index),
            'SB_CHROMA_PATH': str(self.chroma), 'SB_CHROMA_SNAPSHOT': str(self.snapshot),
            'SB_CHROMA_SNAPSHOT_TTL': '600', 'HF_HUB_OFFLINE': '1',
            'SB_HOME': str(self.root / 'sb-home'), 'SB_CLAUDE_MEM_DIR': str(self.root / 'claude-mem'),
        })
        env.start()
        self.addCleanup(env.stop)
        model = patch.object(sb_embedding.LocalEmbeddingFunction, '_load', return_value=Encoder())
        model.start()
        self.addCleanup(model.stop)
        self.writer = sqlite3.connect(self.db)
        self.addCleanup(self.writer.close)
        self.writer.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT,
                subtitle TEXT, narrative TEXT, text TEXT, facts TEXT, concepts TEXT,
                created_at TEXT, created_at_epoch INTEGER, metadata TEXT, content_hash TEXT);
            CREATE VIRTUAL TABLE observations_fts USING fts5(
                title, subtitle, narrative, text, facts, concepts,
                content='observations', content_rowid='id');
            CREATE TRIGGER observation_insert AFTER INSERT ON observations BEGIN
                INSERT INTO observations_fts(rowid,title,subtitle,narrative,text,facts,concepts)
                VALUES(new.id,new.title,new.subtitle,new.narrative,new.text,new.facts,new.concepts);
            END;
        ''')
        sb_embedding.register()
        self.client = Client(settings=Settings(is_persistent=True, persist_directory=str(self.chroma),
                                              anonymized_telemetry=False))
        self.addCleanup(self.client.close)
        config = {
            'schema': 'sb-embedding-1', 'model_id': 'test/local', 'revision': 'a' * 40,
            'local_path': str(self.root), 'device': 'cpu', 'dtype': 'float32',
            'query_prompt': '<Q>', 'document_prompt': '', 'max_chars': 6000,
            'max_tokens': 32, 'normalize': True, 'trust_remote_code': False, 'dimension': 2,
        }
        self.collection = self.client.create_collection(
            'cm__claude-mem', embedding_function=sb_embedding.make_local_ef(config))
        self.insert(1, '기존기억')
        self.vector(1, '기존기억')
        ko.build()
        warm = self.query('기존기억')
        self.assertTrue(warm['meta']['vector_ok'], warm['meta']['vector_error'])
        self.assertEqual(warm['meta']['fts_backend'], 'ko')

    def insert(self, ident, text):
        self.writer.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (ident, 'alpha', 'discovery', text, '', text, text, '[]', '[]',
                             '2026-09-06T00:00:00Z', 1788652800000, '{}', None))
        self.writer.commit()

    def vector(self, ident, text):
        self.collection.add(ids=[str(ident)], documents=[text], metadatas=[{
            'sqlite_id': ident, 'project': 'alpha', 'doc_type': 'observation',
            'custom_field': 'preserved', 'nested_json': '{"origin":"synthetic"}',
        }])

    def query(self, text):
        return searcher.search(text, searcher.Scope('global', []), limit=20)

    def assert_both(self, result, ident):
        self.assertTrue(result['meta']['vector_ok'], result['meta']['vector_error'])
        rows = {row['obs_id']: row for row in result['results']}
        self.assertIn(ident, rows)
        self.assertEqual(rows[ident]['sources'], ['fts', 'vector'])

    def test_warm_caches_ordinary_query_sees_committed_save(self):
        self.insert(2, '우주망원경')
        self.vector(2, '우주망원경')
        result = self.query('우주망원경')
        self.assert_both(result, 2)
        self.assertFalse(result['meta']['snapshot']['reused'])

    def test_delayed_vector_commit_after_lexical_refresh(self):
        self.insert(2, '우주망원경')
        # Establish the prior lexical refresh independently of vector completion.
        ko.build()
        pending = self.query('우주망원경')
        self.assertEqual(next(row for row in pending['results'] if row['obs_id'] == 2)['sources'], ['fts'])
        self.vector(2, '우주망원경')
        self.assert_both(self.query('우주망원경'), 2)

    def test_ordinary_queries_before_and_after_async_vector_commit(self):
        self.insert(2, '우주망원경')
        pending = self.query('우주망원경')
        row = next(row for row in pending['results'] if row['obs_id'] == 2)
        self.assertEqual(row['sources'], ['fts'])
        self.assertTrue(pending['meta']['snapshot']['reused'])
        self.vector(2, '우주망원경')
        completed = self.query('우주망원경')
        self.assert_both(completed, 2)
        self.assertFalse(completed['meta']['snapshot']['reused'])

    def test_unchanged_search_reuses_both_caches_and_preserves_source(self):
        before = {p.name: p.read_bytes() for p in self.root.glob('source*') if not p.name.endswith('-shm')}
        chroma_before = {str(p.relative_to(self.chroma)): p.read_bytes()
                         for p in self.chroma.rglob('*') if p.is_file() and not p.name.endswith('-shm')}
        index_before = self.index.read_bytes()
        with patch.object(ko, '_body', side_effect=AssertionError('unchanged document retokenized')), \
                patch.object(searcher.shutil, 'copytree', side_effect=AssertionError('unchanged snapshot copied')):
            result = self.query('기존기억')
        self.assert_both(result, 1)
        self.assertTrue(result['meta']['snapshot']['reused'])
        self.assertEqual(index_before, self.index.read_bytes())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob('source*') if not p.name.endswith('-shm')})
        self.assertEqual(chroma_before, {str(p.relative_to(self.chroma)): p.read_bytes()
                                        for p in self.chroma.rglob('*') if p.is_file() and not p.name.endswith('-shm')})

    def test_update_without_hash_change_and_delete_are_fresh(self):
        self.writer.execute("UPDATE observations SET title='우주망원경', narrative='우주망원경', text='우주망원경', project='beta' WHERE id=1")
        self.writer.commit()
        self.collection.update(ids=['1'], documents=['우주망원경'], metadatas=[{'project': 'beta', 'custom_field': 'updated'}])
        self.assert_both(self.query('우주망원경'), 1)
        self.assertEqual(ko.search_ko('기존기억', 20), [])
        self.assertEqual([row[0] for row in ko.search_ko('우주망원경', 20, projects=['beta'])], [1])
        with contextlib.closing(sqlite3.connect(self.snapshot / 'chroma.sqlite3')) as db:
            metadata = dict(db.execute("SELECT key, string_value FROM embedding_metadata WHERE key IN ('custom_field','nested_json')"))
        self.assertEqual(metadata, {'custom_field': 'updated', 'nested_json': '{"origin":"synthetic"}'})
        self.writer.execute('DELETE FROM observations WHERE id=1')
        self.writer.commit()
        self.collection.delete(ids=['1'])
        result = self.query('우주망원경')
        self.assertEqual(result['results'], [])
        self.assertEqual(ko.search_ko('우주망원경', 20), [])
        hits, meta = searcher.vector_search('우주망원경', searcher.Scope('global', []), 20)
        self.assertTrue(meta['ok'], meta['error'])
        self.assertEqual(hits, [])

    def test_custom_source_without_explicit_index_never_touches_default(self):
        with patch.dict(os.environ):
            os.environ.pop('SB_KO_INDEX')
            with patch.object(ko, 'build', side_effect=AssertionError('must not build default corpus')), \
                    patch.object(ko, 'open_index', side_effect=AssertionError('must not open default corpus')):
                result = self.query('기존기억')
        self.assertEqual(result['meta']['fts_backend'], 'fts5')

    def test_explicit_index_with_different_source_is_not_rebound(self):
        other = self.root / 'other.sqlite'
        with contextlib.closing(sqlite3.connect(other)) as dst:
            self.writer.backup(dst)
        before = self.index.read_bytes()
        with patch.dict(os.environ, {'SB_CLAUDE_MEM_DB': str(other)}), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            result = self.query('기존기억')
        self.assertEqual(result['meta']['fts_backend'], 'fts5')
        self.assertTrue(errors.getvalue())
        self.assertEqual(self.index.read_bytes(), before)
        with self.assertRaises(ValueError):
            ko.ensure_current(index_path=str(self.db), db_path=str(self.db))

    def test_refresh_failure_rolls_back_and_does_not_serve_stale_ko(self):
        self.insert(2, '우주망원경')
        self.insert(3, '전파망원경')
        before = self.index.read_bytes()
        body = ko._body

        def fail_third(row):
            if row['id'] == 3:
                raise sqlite3.OperationalError('synthetic index failure')
            return body(row)

        with patch.object(ko, '_body', side_effect=fail_third), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            result = self.query('우주망원경')
        self.assertEqual(result['meta']['fts_backend'], 'fts5')
        self.assertTrue(errors.getvalue())
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(ko.search_ko('우주망원경', 20), [])
        self.assertEqual(next(row for row in self.query('우주망원경')['results'] if row['obs_id'] == 2)['sources'], ['fts'])

    def test_query_waits_for_refresh_lock_and_rechecks_after_commit(self):
        for backend, target, module in [('ko', self.index, ko), ('vector', self.snapshot, searcher)]:
            with self.subTest(backend=backend):
                attempted = threading.Event()
                released = threading.Event()
                original = module._flock
                lock_path = target.parent / (target.name + '.lock')

                def observed_lock(stream, operation, timeout=10.0):
                    if Path(stream.name) == lock_path:
                        try:
                            sb_lock.lock(stream, operation | sb_lock.LOCK_NB)
                        except BlockingIOError:
                            attempted.set()
                            if not released.wait(10):
                                raise TimeoutError('test release event')
                    original(stream, operation, timeout)

                with lock_path.open('a') as held, ThreadPoolExecutor(max_workers=1) as pool:
                    sb_lock.lock(held, sb_lock.LOCK_EX)
                    with patch.object(module, '_flock', side_effect=observed_lock):
                        future = pool.submit(self.query, '우주망원경')
                        try:
                            self.assertTrue(attempted.wait(10), 'query did not contend on refresh lock')
                            self.assertFalse(future.done())
                            ident = 2 if backend == 'ko' else 3
                            self.insert(ident, '우주망원경')
                            self.vector(ident, '우주망원경')
                            # Vector case deliberately leaves lexical state unchanged
                            # after its earlier source read; only vector is asserted.
                        finally:
                            sb_lock.lock(held, sb_lock.LOCK_UN)
                            released.set()
                        result = future.result(timeout=20)
                row = next(row for row in result['results'] if row['obs_id'] == ident)
                self.assertIn('vector', row['sources'])
                if backend == 'ko':
                    self.assertIn('fts', row['sources'])

    def test_build_and_query_serialize_and_inspection_stays_read_only(self):
        self.insert(2, '우주망원경')
        self.vector(2, '우주망원경')
        building = threading.Event()
        contended = threading.Event()
        release = threading.Event()
        original_body, original_lock = ko._body, ko._flock

        def body(row):
            building.set()
            if not release.wait(10):
                raise TimeoutError('test build release')
            return original_body(row)

        def lock(stream, operation, timeout=10.0):
            try:
                sb_lock.lock(stream, operation | sb_lock.LOCK_NB)
            except BlockingIOError:
                contended.set()
            original_lock(stream, operation, timeout)

        with patch.object(ko, '_body', side_effect=body) as tokenize, \
                patch.object(ko, '_flock', side_effect=lock), ThreadPoolExecutor(max_workers=2) as pool:
            builder = pool.submit(ko.build)
            try:
                self.assertTrue(building.wait(10))
                # Inspection does not join a refresh or expose uncommitted rows.
                self.assertEqual(ko.search_ko('우주망원경', 20), [])
                query = pool.submit(self.query, '우주망원경')
                self.assertTrue(contended.wait(10))
                self.assertFalse(query.done())
            finally:
                release.set()
            self.assertEqual(builder.result(timeout=20)['indexed'], 1)
            result = query.result(timeout=20)
        self.assert_both(result, 2)
        self.assertEqual(tokenize.call_count, 1)

    def test_reader_pin_blocks_snapshot_publication(self):
        self.insert(2, '우주망원경')
        self.vector(2, '우주망원경')
        pin_path = self.snapshot.parent / (self.snapshot.name + '.readers.lock')
        publishing = threading.Event()
        original = searcher._flock
        before = (self.snapshot / 'snapshot_meta.json').read_bytes()

        def lock(stream, operation, timeout=10.0):
            if Path(stream.name) == pin_path and operation == sb_lock.LOCK_EX:
                try:
                    sb_lock.lock(stream, operation | sb_lock.LOCK_NB)
                except BlockingIOError:
                    publishing.set()
            original(stream, operation, timeout)

        with pin_path.open('a') as pin, patch.object(searcher, '_flock', side_effect=lock), \
                ThreadPoolExecutor(max_workers=1) as pool:
            sb_lock.lock(pin, sb_lock.LOCK_SH)
            future = pool.submit(self.query, '우주망원경')
            try:
                self.assertTrue(publishing.wait(10))
                self.assertFalse(future.done())
                self.assertEqual((self.snapshot / 'snapshot_meta.json').read_bytes(), before)
            finally:
                sb_lock.lock(pin, sb_lock.LOCK_UN)
            result = future.result(timeout=20)
        self.assert_both(result, 2)
        self.assertNotEqual((self.snapshot / 'snapshot_meta.json').read_bytes(), before)

    def test_concurrent_queries_publish_one_refresh(self):
        self.insert(2, '우주망원경')
        self.vector(2, '우주망원경')
        barrier = threading.Barrier(3)

        def query():
            barrier.wait(timeout=10)
            return self.query('우주망원경')

        with patch.object(ko, '_body', wraps=ko._body) as body, \
                patch.object(searcher.shutil, 'copytree', wraps=searcher.shutil.copytree) as copy, \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(query) for _ in range(2)]
            barrier.wait(timeout=10)
            results = [future.result(timeout=20) for future in futures]
        for result in results:
            self.assert_both(result, 2)
        self.assertEqual(body.call_count, 1)
        self.assertEqual(copy.call_count, 1)
        self.assertEqual(sorted(result['meta']['snapshot']['reused'] for result in results), [False, True])

    def test_lock_timeout_reports_failure_instead_of_stale_vector(self):
        self.insert(2, '우주망원경')
        self.vector(2, '우주망원경')
        original = ko._flock
        lock_path = self.snapshot.parent / (self.snapshot.name + '.lock')
        with lock_path.open('a') as held:
            sb_lock.lock(held, sb_lock.LOCK_EX)
            with patch.object(searcher, '_flock', side_effect=lambda stream, operation: original(stream, operation, timeout=0)):
                result = self.query('우주망원경')
        self.assertFalse(result['meta']['vector_ok'])
        self.assertIn('timed out', result['meta']['vector_error'])
        self.assertEqual(next(row for row in result['results'] if row['obs_id'] == 2)['sources'], ['fts'])

    def test_missing_lexical_index_is_built_by_ordinary_search(self):
        self.index.unlink()
        self.assert_both(self.query('기존기억'), 1)
        self.assertTrue(self.index.exists())

    def test_log_drain_and_metadata_only_update_remain_fresh(self):
        self.collection.modify(configuration={'hnsw': {'sync_threshold': 2, 'batch_size': 2}})
        # Native Chroma loads these persistence thresholds when opening a segment.
        self.client.close()
        self.client = Client(settings=Settings(is_persistent=True, persist_directory=str(self.chroma),
                                              anonymized_telemetry=False))
        self.addCleanup(self.client.close)
        self.collection = self.client.get_collection('cm__claude-mem')
        self.insert(2, '우주망원경')
        self.vector(2, '우주망원경')
        self.assert_both(self.query('우주망원경'), 2)
        self.collection.update(ids=['2'], metadatas=[{'project': 'beta'}])
        hits, meta = searcher.vector_search('우주망원경', searcher.Scope('project', ['beta']), 20)
        self.assertTrue(meta['ok'], meta['error'])
        self.assertEqual([hit.obs_id for hit in hits], [2])
        self.assertFalse(meta['snapshot']['reused'])
        with contextlib.closing(sqlite3.connect(self.chroma / 'chroma.sqlite3')) as db:
            watermarks = db.execute('SELECT seq_id FROM max_seq_id ORDER BY seq_id').fetchall()
            retained = db.execute('SELECT seq_id FROM embeddings_queue ORDER BY seq_id').fetchall()
        self.assertEqual(watermarks, [(2,), (3,)])
        self.assertEqual(retained, [(2,), (3,)])

    def test_checkpoint_does_not_invalidate_committed_generation(self):
        self.client.close()
        with contextlib.closing(sqlite3.connect(self.chroma / 'chroma.sqlite3')) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute("UPDATE collection_metadata SET str_value=str_value")
            db.commit()
            self.query('기존기억')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            with patch.object(searcher.shutil, 'copytree', side_effect=AssertionError('checkpoint copied snapshot')):
                result = self.query('기존기억')
        self.assert_both(result, 1)
        self.assertTrue(result['meta']['snapshot']['reused'])

    def test_actual_cli_refreshes_warm_caches(self):
        self.insert(2, '우주망원경')
        self.vector(2, '우주망원경')
        shim = self.root / 'shim'
        shim.mkdir()
        # Only the local embedding model boundary is faked in the real CLI process.
        (shim / 'sitecustomize.py').write_text(
            'import sb_embedding\n'
            'class Encoder:\n'
            '    def encode(self, texts, **kwargs): return [[1.0, 0.0] for text in texts]\n'
            'sb_embedding.LocalEmbeddingFunction._load = lambda self: Encoder()\n')
        script = Path(__file__).resolve().parents[1] / 'bin' / 'sb_search.py'
        completed = subprocess.run(
            [sys.executable, str(script), '우주망원경', '--global', '--json'],
            env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(shim), str(script.parent)])},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assert_both(payload, 2)
        self.assertFalse(payload['meta']['snapshot']['reused'])


if __name__ == '__main__':
    unittest.main()
