"""sb_pii_sweep·sb_memory 선택형 마스킹 계약 — 마스킹 모듈이 있으면 가리고, 없으면 아무것도 하지 않는다."""
import contextlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import sb_memory  # noqa: E402
import sb_pii_sweep  # noqa: E402

MASKER = '''import re
NAMES = {"홍길동": "홍○○", "인하": "담당자"}
def mask(s):
    for n, v in NAMES.items():
        if len(n) <= 2:
            s = re.sub(r"(?<![가-힣])" + n + r"(?=\\s?(?:님|씨))", v, s)
        else:
            s = s.replace(n, v)
    return s
'''


class SweepTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.db = str(root / 'cm.sqlite')
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.executescript('''
                CREATE TABLE observations (id INTEGER PRIMARY KEY, project TEXT, title TEXT, subtitle TEXT,
                    narrative TEXT, text TEXT, facts TEXT, concepts TEXT);
                CREATE TABLE session_summaries (id INTEGER PRIMARY KEY, project TEXT, request TEXT, investigated TEXT,
                    learned TEXT, completed TEXT, next_steps TEXT, notes TEXT);
                CREATE TABLE user_prompts (id INTEGER PRIMARY KEY, session_db_id INTEGER, prompt_text TEXT);
                CREATE TABLE sdk_sessions (id INTEGER PRIMARY KEY, project TEXT);
                INSERT INTO observations VALUES (1,'w','홍길동님 회신','', '확인하고 승인하며 수수료 인하 검토','', '[]','[]');
                INSERT INTO observations VALUES (2,'w','일반 기록','', '인하님께 전달','', '[]','[]');
                INSERT INTO session_summaries VALUES (1,'w','홍길동 요청','','','','','');
                INSERT INTO user_prompts VALUES (1,1,'확인하고 정리해줘');
            ''')
        mask_path = root / 'mask.py'
        mask_path.write_text(MASKER, encoding='utf-8')
        env = patch.dict(os.environ, {'SB_CLAUDE_MEM_DB': self.db, 'SB_PII_MASK': str(mask_path),
                                      'SB_HOME': str(root / 'sb')})
        env.start()
        self.addCleanup(env.stop)
        sb_memory._MASKER.clear()
        self.addCleanup(sb_memory._MASKER.clear)

    def row(self, sql):
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            return db.execute(sql).fetchone()

    def test_masks_names_without_breaking_ordinary_words(self):
        mask = sb_memory._pii_masker()
        changed = sb_pii_sweep.sweep_sqlite(mask)
        self.assertEqual(changed, {'observations': [1, 2], 'summaries': [1], 'prompts': []})
        title, narrative = self.row('SELECT title, narrative FROM observations WHERE id=1')
        self.assertEqual(title, '홍○○님 회신')
        self.assertEqual(narrative, '확인하고 승인하며 수수료 인하 검토')   # 낱말 속 '인하'는 그대로
        self.assertEqual(self.row('SELECT narrative FROM observations WHERE id=2')[0], '담당자님께 전달')
        self.assertEqual(self.row('SELECT prompt_text FROM user_prompts WHERE id=1')[0], '확인하고 정리해줘')

    def test_dry_run_and_idempotent(self):
        mask = sb_memory._pii_masker()
        self.assertTrue(any(sb_pii_sweep.sweep_sqlite(mask, dry_run=True).values()))
        self.assertEqual(self.row('SELECT title FROM observations WHERE id=1')[0], '홍길동님 회신')
        sb_pii_sweep.sweep_sqlite(mask)
        self.assertFalse(any(sb_pii_sweep.sweep_sqlite(mask).values()))

    def test_no_masker_means_no_op(self):
        with patch.dict(os.environ, {'SB_PII_MASK': '0'}):
            sb_memory._MASKER.clear()
            self.assertIsNone(sb_memory._pii_masker())


if __name__ == '__main__':
    unittest.main()
