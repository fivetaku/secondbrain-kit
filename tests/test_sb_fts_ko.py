"""Offline Korean FTS contracts; all databases live in temporary directories."""

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

try:
    from kiwipiepy import Kiwi
except ImportError:
    KIWI_AVAILABLE = False
else:
    KIWI_AVAILABLE = True

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import sb_config
import sb_fts_ko as ko


class KoreanFTSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ko.tokenize_ko('검색을 개선했다')

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = str(Path(temp.name) / 'source?#.sqlite')
        self.index = str(Path(temp.name) / 'index?#.sqlite')
        with contextlib.closing(sqlite3.connect(self.source)) as db:
            db.execute('''CREATE TABLE observations (
                id INTEGER PRIMARY KEY, project TEXT, title TEXT, subtitle TEXT,
                narrative TEXT, text TEXT, facts TEXT, concepts TEXT,
                created_at_epoch INTEGER, content_hash TEXT)''')
            db.commit()
        self.home = str(Path(temp.name) / 'sb-home')
        self.cm_dir = str(Path(temp.name) / 'claude-mem')
        env = patch.dict(os.environ, {'SB_CLAUDE_MEM_DB': self.source,
                                      'SB_KO_INDEX': self.index,
                                      'SB_HOME': self.home,
                                      'SB_CLAUDE_MEM_DIR': self.cm_dir})
        env.start()
        self.addCleanup(env.stop)

    def insert(self, obs_id: int = 1, project: str = 'alpha',
               text: str = '검색을 개선했다', content_hash: str = 'hash-1') -> None:
        with contextlib.closing(sqlite3.connect(self.source)) as db:
            db.execute('INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (obs_id, project, '', '', '', text, '[]', '[]', 123, content_hash))
            db.commit()

    def test_korean_search_and_source_preservation(self) -> None:
        self.insert()
        before = Path(self.source).read_bytes()
        result = ko.build()
        self.assertEqual(result['indexed'], 1)
        self.assertEqual([row[0] for row in ko.search_ko('검색', 10)], [1])
        self.assertEqual(Path(self.source).read_bytes(), before)

    def test_identifiers_are_preserved_and_searchable(self) -> None:
        identifiers = ['linear_sync.py', 'v0.6.6', 'bge-m3-ko', '/tmp/my_file.py']
        self.insert(text=' '.join(identifiers))
        tokens = ko.tokenize_ko('LINEAR_SYNC.PY v0.6.6 bge-m3-ko /tmp/my_file.py')
        ko.build()
        for identifier in identifiers:
            with self.subTest(identifier=identifier):
                self.assertIn(identifier, tokens)
                self.assertEqual([row[0] for row in ko.search_ko(identifier, 10)], [1])

    def test_unchanged_skip_and_new_id(self) -> None:
        self.insert()
        ko.build()
        result = ko.build()
        self.assertEqual((result['indexed'], result['skipped'], result['removed']), (0, 1, 0))
        self.insert(2, text='색인을 추가했다', content_hash='hash-2')
        result = ko.build(batch=1)
        self.assertEqual((result['indexed'], result['skipped'], result['max_id']), (1, 1, 2))

    def test_hash_change_replaces_old_body(self) -> None:
        self.insert(text='바나나')
        ko.build()
        with contextlib.closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE observations SET text='자동차', content_hash='hash-2'")
            db.commit()
        self.assertEqual(ko.build()['indexed'], 1)
        self.assertEqual(ko.search_ko('바나나', 10), [])
        self.assertEqual([row[0] for row in ko.search_ko('자동차', 10)], [1])

    def test_deleted_source_rows_are_removed(self) -> None:
        self.insert()
        ko.build()
        with contextlib.closing(sqlite3.connect(self.source)) as db:
            db.execute('DELETE FROM observations')
            db.commit()
        self.assertEqual(ko.build()['removed'], 1)
        self.assertEqual(ko.search_ko('검색', 10), [])
        with contextlib.closing(ko.open_index(create=False)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ko_docs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM ko_fts').fetchone()[0], 0)

    def test_projects_filter_and_limit(self) -> None:
        self.insert()
        self.insert(2, project='beta')
        self.insert(3, project="quoted'project")
        ko.build()
        self.assertEqual([row[0] for row in ko.search_ko('검색', 10, ['beta'])], [2])
        self.assertEqual([row[0] for row in ko.search_ko('검색', 10, ["quoted'project"])], [3])
        self.assertEqual(ko.search_ko('검색', 10, ['missing']), [])
        self.assertEqual(len(ko.search_ko('검색', 1)), 1)
        self.assertEqual(ko.search_ko('검색', 0), [])

    def test_full_and_tokenizer_version_rebuild(self) -> None:
        self.insert()
        ko.build()
        self.assertEqual(ko.build(full=True)['indexed'], 1)
        with contextlib.closing(ko.open_index()) as db:
            db.execute("UPDATE ko_meta SET value='old-version' WHERE key='tokenizer'")
            db.commit()
        result = ko.build()
        self.assertEqual((result['indexed'], result['skipped']), (1, 0))
        self.assertNotEqual(ko.status()['tokenizer'], 'old-version')

    def test_all_body_columns_are_indexed(self) -> None:
        self.insert(text='')
        with contextlib.closing(sqlite3.connect(self.source)) as db:
            db.execute('''UPDATE observations SET title=?, subtitle=?, narrative=?,
                       text=?, facts=?, concepts=?''',
                       ('사과', '바나나', '자동차', '기차',
                        json.dumps(['토끼']), json.dumps(['고양이'])))
            db.commit()
        ko.build()
        for query in ['사과', '바나나', '자동차', '기차', '토끼', '고양이']:
            with self.subTest(query=query):
                self.assertEqual([row[0] for row in ko.search_ko(query, 10)], [1])

    def test_missing_status_and_empty_query_do_not_create_index(self) -> None:
        self.assertFalse(ko.status()['exists'])
        self.assertEqual(ko.search_ko('', 10), [])
        self.assertEqual(ko.ko_query_tokens('!!!'), [])
        self.assertFalse(Path(self.index).exists())

    def test_invalid_batch_does_not_touch_source(self) -> None:
        self.insert()
        before = Path(self.source).read_bytes()
        for batch in [0, -1]:
            with self.subTest(batch=batch), self.assertRaises(ValueError):
                ko.build(batch=batch)
        self.assertEqual(Path(self.source).read_bytes(), before)

    def test_cli_commands_use_temporary_environment(self) -> None:
        self.insert(text='검색을 개선했다 linear_sync.py')
        commands = [['build', '--full'], ['status'],
                    ['search', '검색', '--limit', '1', '--projects', 'alpha'],
                    ['tokens', 'linear_sync.py']]
        for command in commands:
            with self.subTest(command=command), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(ko.main(command), 0)
                self.assertTrue(output.getvalue().strip())

    def test_import_failure_uses_bigram_metadata_and_search(self) -> None:
        self.insert()
        with patch.object(ko, '_loaded', False), patch.object(ko, '_kiwi', None), \
                patch.dict(sys.modules, {'kiwipiepy': None}):
            self.assertIn('검색', ko.tokenize_ko('검색을'))
            self.assertIn('linear_sync.py', ko.tokenize_ko('LINEAR_SYNC.PY'))
            ko.build()
            self.assertEqual(ko.status()['tokenizer'], 'bigram-1')
            self.assertEqual([row[0] for row in ko.search_ko('검색', 10)], [1])
            with contextlib.closing(ko.open_index(create=False)) as db:
                self.assertEqual(db.execute('SELECT tokenizer FROM ko_docs').fetchone()[0], 'bigram-1')
            result = ko.build()
            self.assertEqual((result['indexed'], result['skipped']), (0, 1))
            self.assertEqual([row[0] for row in ko.search_ko('검색', 10)], [1])

    @unittest.skipUnless(KIWI_AVAILABLE, 'kiwipiepy is required for Kiwi recovery')
    def test_bigram_index_rebuilds_when_kiwi_returns(self) -> None:
        self.test_import_failure_uses_bigram_metadata_and_search()
        self.assertEqual(ko.build()['indexed'], 1)
        self.assertEqual(ko.status()['tokenizer'], ko.TOKENIZER_VERSION)
        self.assertEqual([row[0] for row in ko.search_ko('검색', 10)], [1])

    def test_source_and_hardlink_cannot_be_opened_as_index(self) -> None:
        self.insert()
        before = Path(self.source).read_bytes()
        os.link(self.source, self.index)
        for target in [self.source, self.index]:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    ko.build(index_path=target, db_path=self.source)
                with self.assertRaises(ValueError):
                    ko.open_index(target)
        self.assertEqual(Path(self.source).read_bytes(), before)

    def test_inspection_connection_rejects_writes(self) -> None:
        self.insert()
        ko.build()
        with contextlib.closing(ko.open_index(create=False)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM ko_docs')
        self.assertEqual(ko.status()['documents'], 1)

    def test_cli_help_and_invalid_command(self) -> None:
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        script = str(Path(__file__).resolve().parents[1] / 'bin' / 'sb_fts_ko.py')
        help_result = subprocess.run([sys.executable, script, '--help'], env=env,
                                     capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('build', help_result.stdout)
        invalid = subprocess.run([sys.executable, script, 'invalid-command'], env=env,
                                 capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn('invalid choice', invalid.stderr)
        self.assertFalse(Path(self.index).exists())

    def test_defaults_follow_sb_config_and_env_overrides_win(self) -> None:
        self.assertEqual(ko._path(), Path(self.index).resolve())
        self.assertEqual(ko._source_path(), Path(self.source).resolve())
        with patch.dict(os.environ):
            os.environ.pop('SB_KO_INDEX')
            os.environ.pop('SB_CLAUDE_MEM_DB')
            self.assertEqual(ko._path(), (Path(self.home) / 'index' / 'ko_fts.sqlite').resolve())
            self.assertEqual(ko._source_path(), Path(sb_config.claude_mem_db()).resolve())
            self.assertEqual(ko._source_path(), (Path(self.cm_dir) / 'claude-mem.db').resolve())
            self.assertFalse(ko.status()['exists'])
            self.assertFalse((Path(self.home) / 'index').exists())

    def test_lock_timeout_message(self) -> None:
        lock_path = Path(self.index + '.lock')
        with lock_path.open('a') as held, lock_path.open('a') as waiter:
            ko.sb_lock.lock(held, ko.sb_lock.LOCK_EX)
            with self.assertRaisesRegex(TimeoutError, 'timed out'):
                ko._flock(waiter, ko.sb_lock.LOCK_EX, timeout=0)


if __name__ == '__main__':
    unittest.main()
