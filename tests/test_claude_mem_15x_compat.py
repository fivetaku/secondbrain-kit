"""claude-mem 15.18.3 스키마에서 키트의 DB 읽기 경로가 그대로 도는지(fixtures/claude-mem/schema-15.18.3.sql).

픽스처는 npm 패키지의 SessionStore 마이그레이션으로 만든 빈 DB에서 뽑은 표 정의다. 행은 모두 가짜다.
"""
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import consolidate  # noqa: E402
import sb_claude_mem  # noqa: E402
import sb_fts_ko  # noqa: E402
import sb_health  # noqa: E402
import sb_memory  # noqa: E402
import sb_pii_sweep  # noqa: E402
import sb_search  # noqa: E402
import sb_sources  # noqa: E402
import sb_vault  # noqa: E402

SCHEMA = ROOT / 'tests' / 'fixtures' / 'claude-mem' / 'schema-15.18.3.sql'


def _iso(ms: int) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(ms / 1000))


class Schema15Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / 'claude-mem.db')
        old = int(time.time() * 1000) - 3 * 86400000
        cls.old = old
        with contextlib.closing(sqlite3.connect(cls.db)) as db:
            db.executescript(SCHEMA.read_text(encoding='utf-8'))
            db.execute('INSERT INTO sdk_sessions(content_session_id, memory_session_id, project, platform_source, '
                       'started_at, started_at_epoch, status, cwd) VALUES (?,?,?,?,?,?,?,?)',
                       ('cs-1', 'ms-1', 'demo', 'claude', _iso(old), old, 'completed', '/tmp/fake/demo'))
            db.execute('INSERT INTO sdk_sessions(content_session_id, memory_session_id, project, platform_source, '
                       'started_at, started_at_epoch, status) VALUES (?,?,?,?,?,?,?)',
                       ('cs-2', 'ms-2', 'other', 'codex', _iso(old), old, 'completed'))
            rows = [('ms-1', 'demo', '[demo·결정] 회수 포트를 바꾸기로 함'), ('ms-1', 'demo', '벡터 백필 순서 확인'),
                    ('ms-2', 'other', '다른 프로젝트의 가짜 관측 제목')]
            for i, (ms, proj, title) in enumerate(rows):
                t = old + i * 1000
                db.execute('INSERT INTO observations(memory_session_id, project, type, title, subtitle, facts, narrative, '
                           'concepts, files_read, files_modified, prompt_number, created_at, created_at_epoch, metadata) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (ms, proj, 'decision', title, '부제', '["사실"]', '가짜 서술 keyword 본문', '[]', '[]', '[]', 1,
                            _iso(t), t, json.dumps({'dedup_key': 'k%d' % i})))
            db.execute('INSERT INTO session_summaries(memory_session_id, project, request, prompt_number, created_at, '
                       'created_at_epoch) VALUES (?,?,?,?,?,?)', ('ms-1', 'demo', '가짜 요청', 1, _iso(old), old))
            db.execute('INSERT INTO user_prompts(session_db_id, content_session_id, prompt_number, prompt_text, '
                       'created_at, created_at_epoch) VALUES (1,?,?,?,?,?)', ('cs-1', 1, '가짜 발화 포트', _iso(old), old))
            db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fixture_is_15x_shape(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            cols = {r[1] for r in db.execute('PRAGMA table_info(observations)')}
            scols = {r[1] for r in db.execute('PRAGMA table_info(sdk_sessions)')}
        # 키트가 읽는 열이 모두 있다
        self.assertTrue({'id', 'memory_session_id', 'project', 'text', 'type', 'title', 'subtitle', 'facts', 'narrative',
                         'concepts', 'created_at', 'created_at_epoch', 'metadata', 'merged_into_project',
                         'title_norm_key'} <= cols)
        self.assertTrue({'id', 'content_session_id', 'memory_session_id', 'project', 'platform_source',
                         'started_at_epoch', 'cwd'} <= scols)

    def test_search_paths(self):
        hits = sb_search.fts_search('keyword', sb_search.Scope('project', ['demo']), 5, db_path=self.db)
        self.assertEqual(sorted(h.obs_id for h in hits), [1, 2])
        got = sb_search.hydrate([1, 2, 3], db_path=self.db)
        self.assertEqual(sorted(got), [1, 2, 3])

    def test_health_vault_sources_consolidate(self):
        with contextlib.closing(sb_health._ro(self.db)) as db:
            self.assertEqual(sb_health._max_obs(db), 3)
            self.assertTrue(sb_health.check_decisions(db)['ok'])
        info = sb_vault.read_claude_mem(self.db)
        self.assertTrue(info['readable'])
        self.assertEqual(info['max_id'], 3)
        self.assertIn('/tmp/fake/demo', [s['cwd'] for s in info['sessions']])
        self.assertEqual(sb_sources.stage3(seed=1, n=3, db=self.db)['stage'], 3)
        self.assertIn(('demo', consolidate.iso_week(_iso(self.old))), consolidate.list_targets(db_path=self.db, since_days=14))
        self.assertTrue(sb_memory.already_saved('k0', db_path=self.db))

    def test_pii_and_ko_index(self):
        with mock.patch.dict(os.environ, {'SB_CLAUDE_MEM_DB': self.db}):
            changed = sb_pii_sweep.sweep_sqlite(lambda s: s.replace('가짜', '**'), dry_run=True)
            self.assertEqual(changed['prompts'], [1])
            self.assertEqual(sb_pii_sweep._projects({'prompts': [1]}), {'prompts': {1: 'demo'}})
        with tempfile.TemporaryDirectory() as d:
            r = sb_fts_ko.build(index_path=str(Path(d) / 'ko.sqlite'), db_path=self.db, full=True)
        self.assertEqual(r['indexed'], 3)

    def test_relabel_update_keeps_fts_triggers(self):
        # 15.x 의 FTS 트리거(observations_au)가 키트의 project 변경과 충돌하지 않는다
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('UPDATE observations SET project=? WHERE id=?', ('x', 1))
            n = db.execute('SELECT count(*) FROM observations_fts WHERE observations_fts MATCH ?', ('"keyword"',)).fetchone()[0]
            db.rollback()
        self.assertEqual(n, 3)


class ReadCheckedTests(unittest.TestCase):
    def test_tested_range_not_widened(self):
        self.assertFalse(sb_claude_mem.in_tested('15.18.3'))
        self.assertTrue(sb_claude_mem.read_checked('15.18.3'))
        self.assertFalse(sb_claude_mem.read_checked('15.18.4'))
        self.assertFalse(sb_claude_mem.read_checked('15.18.3-beta.1'))
        self.assertFalse(sb_claude_mem.read_checked(None))

    def test_health_message_says_read_only(self):
        with mock.patch.object(sb_claude_mem, 'installed_version', return_value='15.18.3'):
            r = sb_health.check_claude_mem_version()
        self.assertFalse(r['ok'])
        self.assertIn('읽기 경로만', r['msg'])
        with mock.patch.object(sb_claude_mem, 'installed_version', return_value='15.19.0'):
            self.assertNotIn('읽기 경로만', sb_health.check_claude_mem_version()['msg'])

    def test_worker_args_parse(self):
        text = (ROOT / 'tests' / 'fixtures' / 'claude-mem' / '15.18.3.cjs').read_text(encoding='utf-8')
        withs, ver, ok = sb_claude_mem.parse_worker_spec(text)
        self.assertTrue(ok)
        self.assertEqual(withs, ['onnxruntime>=1.20', 'protobuf<7', 'chromadb==1.5.9', 'pydantic<2.14'])
        self.assertEqual(ver, '0.2.6')


if __name__ == '__main__':
    unittest.main()
