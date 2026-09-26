"""Offline contract tests; no worker or user database access."""

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import sb_memory as memory

try:  # wrap 백필 주입기는 kit 에 선택 모듈 — 없으면 해당 시험만 건너뛴다
    importlib.import_module('inject_wraps')
    HAS_INJECTOR = True
except ImportError:
    HAS_INJECTOR = False


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / 'memory?#.db')
        self.log = str(Path(self.temp.name) / 'journal.jsonl')
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('CREATE TABLE observations (id INTEGER, title TEXT, metadata TEXT)')
            db.commit()
        env = patch.dict(os.environ, {'SB_HOME': str(Path(self.temp.name) / 'sbhome'),
                                      'SB_CLAUDE_MEM_DIR': str(Path(self.temp.name) / 'cmem'),
                                      'SB_CLAUDE_MEM_DB': self.db,
                                      'SB_MEM_JOURNAL': self.log,
                                      'SB_MEM_BASE_URL': 'http://offline.invalid'})
        env.start()
        self.addCleanup(env.stop)
        self.post = patch.object(memory, '_post', return_value=(200, '{"success":true,"id":42}')).start()
        self.sleep = patch.object(memory.time, 'sleep').start()
        self.addCleanup(patch.stopall)
        self.prov = memory.build_provenance('test', 'manual', 'test', 'tester', sid='s1')

    def events(self):
        return [json.loads(line) for line in Path(self.log).read_text(encoding='utf-8').splitlines()]

    def test_success_returns_id_and_preserves_metadata(self):
        self.assertEqual(memory.save_memory('text', 'title', 'project', self.prov), 42)
        url, payload, timeout = self.post.call_args.args
        self.assertEqual(url, 'http://offline.invalid/api/memory/save')
        self.assertEqual(payload['metadata'], dict(self.prov, dedup_key='test:s1'))
        self.assertEqual(timeout, 10)
        self.assertEqual(self.events()[0]['event'], 'saved')

    def test_invalid_responses_retry_then_fail(self):
        for response in [(500, '{"success":true,"id":1}'), (200, 'not json'),
                         (200, '{"success":false,"id":1}'), (200, '{"success":true}'),
                         (200, '{"success":true,"id":true}'), (200, '[]'),
                         (200, '{"success":1,"id":1}'), (200, '{"success":true,"id":"1"}')]:
            with self.subTest(response=response):
                self.post.reset_mock()
                self.sleep.reset_mock()
                self.post.return_value = response
                with self.assertRaises(RuntimeError):
                    memory.save_memory('text', 'title', 'project', self.prov)
                self.assertEqual(self.post.call_count, 3)
                self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.5, 1.0])
        self.assertTrue(all(event['event'] == 'failed' for event in self.events()))

    def test_network_exception_retries_and_recovers(self):
        self.post.side_effect = [OSError('offline'), (200, '{"success":true,"id":7}')]
        self.assertEqual(memory.save_memory('t', 't', 'p', self.prov), 7)
        self.assertEqual(self.post.call_count, 2)

    def test_network_exception_exhausts(self):
        self.post.side_effect = TimeoutError('offline')
        with self.assertRaises(RuntimeError):
            memory.save_memory('t', 't', 'p', self.prov, retries=0)
        self.assertEqual(self.post.call_count, 1)
        self.sleep.assert_not_called()

    def test_existing_key_skips_without_post(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('INSERT INTO observations VALUES (1, ?, ?)',
                       ('title', json.dumps({'dedup_key': 'test:s1'})))
            db.commit()
        before = Path(self.db).read_bytes()
        self.assertEqual(memory.save_memory('text', 'title', 'p', self.prov), -1)
        self.post.assert_not_called()
        self.assertEqual(self.events()[0]['event'], 'skipped')
        self.assertEqual(Path(self.db).read_bytes(), before)

    def test_missing_db_is_not_created(self):
        target = str(Path(self.temp.name) / 'missing.db')
        self.assertFalse(memory.already_saved('key', target))
        self.assertFalse(Path(target).exists())

    def test_empty_sid_uses_content_hash(self):
        self.prov['sid'] = ''
        self.prov['content_hash'] = 'stale'
        self.assertEqual(memory.dedup_key(self.prov, 'text', 'title'),
                         'test:' + memory.content_hash('text', 'title'))
        self.assertEqual(len(memory.content_hash('text')), 16)
        self.assertNotEqual(memory.content_hash('ab', 'c'), memory.content_hash('a', 'bc'))

    def test_provenance_schema_and_invalid_kind(self):
        with self.assertRaises(ValueError):
            memory.build_provenance('s', 'invalid', 'o', 'c')
        self.assertEqual(set(self.prov), {'source', 'kind', 'origin', 'created_by', 'sid',
                                        'source_ids', 'content_hash', 'verification',
                                        'created_at', 'schema'})
        self.assertEqual(self.prov['schema'], 'sb-prov-1')
        self.assertNotIn('.', self.prov['created_at'])
        self.assertEqual(memory.build_provenance('s', 'import', 'o', 'c', extra={})['extra'], {})

    def test_empty_text_or_title_rejected(self):
        for text, title in [('', 't'), ('t', ''), ('  ', 't'), ('t', '\n')]:
            with self.assertRaises(ValueError):
                memory.save_memory(text, title, 'p', self.prov)
        self.post.assert_not_called()

    def test_defaults_follow_sb_config(self):
        home = Path(self.temp.name) / 'sbhome'
        cmem = Path(self.temp.name) / 'cmem'
        with patch.dict(os.environ, {}, clear=False):
            for key in ('SB_MEM_JOURNAL', 'SB_CLAUDE_MEM_DB', 'SB_MEM_BASE_URL', 'SB_STATE_DB'):
                os.environ.pop(key, None)
            cmem.mkdir()
            (cmem / 'settings.json').write_text(json.dumps({'CLAUDE_MEM_WORKER_PORT': '41234'}), encoding='utf-8')
            memory.journal({'event': 'probe'})
            self.assertTrue((home / 'logs' / 'memory_journal.jsonl').is_file())
            self.assertEqual(memory.state_db_path(), home / 'state.db')
            self.assertFalse(memory.already_saved('k'))  # claude_mem_db() 가 없으면 만들지 않고 False
            self.assertFalse((cmem / 'claude-mem.db').exists())
            self.assertEqual(memory.save_memory('text', 'title', 'p', self.prov), 42)
            self.assertEqual(self.post.call_args.args[0], 'http://127.0.0.1:41234/api/memory/save')
            os.environ['SB_MEM_BASE_URL'] = 'http://env.invalid:9/'
            memory.save_memory('text2', 'title2', 'p', memory.build_provenance('t', 'manual', 'o', 'c', sid='s2'))
            self.assertEqual(self.post.call_args.args[0], 'http://env.invalid:9/api/memory/save')

    def run_injector(self, items):
        injector = importlib.import_module('inject_wraps')
        source = Path(self.temp.name) / 'wraps.json'
        source.write_text(json.dumps(items), encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = injector.main([str(source)])
        return status, output.getvalue()

    @unittest.skipUnless(HAS_INJECTOR, 'inject_wraps not shipped in this kit')
    def test_injector_batch_duplicates_and_invalid(self):
        item = {'sid': 'session', 'title': 'Title', 'wrap': 'Summary', 'date': '2026-09-05'}
        status, output = self.run_injector([item, item, {'sid': '', 'title': ''}])
        self.assertEqual(status, 0)
        self.assertIn('성공 1 / 중복스킵 1 / 무효 1 / 실패 0', output)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual([e['event'] for e in self.events()], ['saved', 'skipped', 'invalid'])

    @unittest.skipUnless(HAS_INJECTOR, 'inject_wraps not shipped in this kit')
    def test_injector_empty_sid_dedup(self):
        item = {'sid': '', 'title': 'Title', 'wrap': 'Summary'}
        status, output = self.run_injector([item, item])
        self.assertEqual(status, 0)
        self.assertIn('성공 1 / 중복스킵 1 / 무효 0 / 실패 0', output)
        self.assertEqual(self.post.call_count, 1)

    @unittest.skipUnless(HAS_INJECTOR, 'inject_wraps not shipped in this kit')
    def test_injector_failure_exits_one_and_continues(self):
        self.post.return_value = (200, '{"success":false}')
        status, output = self.run_injector([{'sid': 'a', 'title': 'A'}, {'sid': 'b', 'title': 'B'}])
        self.assertEqual(status, 1)
        self.assertIn('성공 0 / 중복스킵 0 / 무효 0 / 실패 2', output)
        self.assertEqual(self.post.call_count, 6)


if __name__ == '__main__':
    unittest.main()
