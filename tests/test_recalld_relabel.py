"""sb_recalld(상주 회수: 형태소 검색 + 주입 문턱)와 sb_relabel(프로젝트 재분류·되돌리기) 계약."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import sb_recalld  # noqa: E402
import sb_relabel  # noqa: E402


class GateTests(unittest.TestCase):
    def test_gate_thresholds(self):
        self.assertTrue(sb_recalld.gate_pass(3, 0.3))       # 명사 3개 일치
        self.assertTrue(sb_recalld.gate_pass(2, 0.67))      # 2개 + 커버리지 60% 이상
        self.assertFalse(sb_recalld.gate_pass(2, 0.4))      # 2개지만 질의의 일부만
        self.assertFalse(sb_recalld.gate_pass(1, 1.0))
        self.assertFalse(sb_recalld.gate_pass(0, 0.0))


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.db = str(root / 'cm.sqlite')
        self.index = str(root / 'ko.sqlite')
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('''CREATE TABLE observations (
                id INTEGER PRIMARY KEY, project TEXT, title TEXT, subtitle TEXT, narrative TEXT, text TEXT,
                facts TEXT, concepts TEXT, created_at TEXT, created_at_epoch INTEGER, content_hash TEXT, metadata TEXT)''')
            rows = [
                (1, 'work', '[2026-09-01·완료] 거래처 단가 계약서 회신', '', '거래처 단가 계약서 검토 회신 발송', '',
                 '[]', '[]', '2026-09-26T00:00:00Z', 1, 'h1', '{"extra":{"session_date":"2026-09-01"}}'),
                (2, 'work', '공급사 매입 규모 연도별 요약', '', '공급사 매입 집중도 분석', '', '[]', '[]',
                 '2026-09-21T00:00:00Z', 2, 'h2', '{}'),
                (3, 'work', '회수 훅 디버깅', '', '거래처 단가 검색 결과 확인용 도구 작업', '', '[]', '[]',
                 '2026-09-26T00:00:00Z', 3, 'h3', '{}'),
            ]
            db.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows)
            db.commit()
        env = patch.dict(os.environ, {'SB_CLAUDE_MEM_DB': self.db, 'SB_KO_INDEX': self.index,
                                      'SB_HOME': str(root / 'sb-home'), 'SB_CLAUDE_MEM_DIR': str(root / 'cm')})
        env.start()
        self.addCleanup(env.stop)


class RecallEngineTests(Fixture):
    def test_recall_returns_items_gate_and_session_date(self):
        engine = sb_recalld.Engine()
        out = engine.recall('거래처 단가 계약서 회신 어떻게 됐지', 'work', 5)
        self.assertTrue(out['gate']['pass'])
        self.assertEqual(out['items'][0]['id'], 1)
        self.assertEqual(out['items'][0]['date'], '2026-09-01')   # 적재일이 아니라 세션 날짜

    def test_unrelated_prompt_fails_gate(self):
        engine = sb_recalld.Engine()
        out = engine.recall('파이썬 리스트 정렬하는 법', 'work', 5)
        self.assertFalse(out['gate']['pass'])

    def test_fresh_live_observation_is_excluded_before_gate(self):
        # 방금 생긴 실시간 관찰(진행 중인 작업)이 판정 1위를 차지해 무관 질문을 통과시키면 안 된다
        import time
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (9, 'work', 'CSV 파일 합치는 파이썬 코드 평가', '', 'CSV 파일 합치는 파이썬 코드 질의로 평가함', '',
                        '[]', '[]', '2026-09-27T00:00:00Z', int(time.time() * 1000), 'h9', '{}'))
            db.commit()
        engine = sb_recalld.Engine()
        out = engine.recall('파이썬으로 CSV 파일 합치는 코드', 'work', 5)
        self.assertNotIn(9, [i['id'] for i in out['items']])
        self.assertFalse(out['gate']['pass'])

    def test_project_first_then_other_projects(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute("UPDATE observations SET project='kit' WHERE id=3")
            db.commit()
        engine = sb_recalld.Engine()
        ids = [i['id'] for i in engine.recall('거래처 단가 검색', 'work', 5)['items']]
        self.assertLess(ids.index(1), ids.index(3))


class RelabelTests(Fixture):
    def project(self, oid):
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            return db.execute('SELECT project FROM observations WHERE id=?', (oid,)).fetchone()[0]

    def test_apply_and_undo_with_backup_and_ko_sync(self):
        sb_recalld.Engine()   # ko 색인 생성
        with contextlib.redirect_stdout(open(os.devnull, 'w', encoding='utf-8')):
            journal = sb_relabel.apply({3: 'kit'}, note='test')
        self.assertEqual(self.project(3), 'kit')
        self.assertEqual(self.project(1), 'work')
        j = json.loads(Path(journal).read_text(encoding='utf-8'))
        self.assertTrue(Path(j['backup']).exists())
        with contextlib.closing(sqlite3.connect(self.index)) as ko:
            self.assertEqual(ko.execute('SELECT project FROM ko_fts WHERE obs_id=3').fetchone()[0], 'kit')
        with contextlib.redirect_stdout(open(os.devnull, 'w', encoding='utf-8')):
            sb_relabel.undo(str(journal))
        self.assertEqual(self.project(3), 'work')

    def test_dry_run_changes_nothing(self):
        with contextlib.redirect_stdout(open(os.devnull, 'w', encoding='utf-8')):
            self.assertIsNone(sb_relabel.apply({3: 'kit'}, dry_run=True))
        self.assertEqual(self.project(3), 'work')


if __name__ == '__main__':
    unittest.main()
