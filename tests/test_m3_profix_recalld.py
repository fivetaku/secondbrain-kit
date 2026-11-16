"""M3 Pro 리뷰 P1-3: project 쪽 1위 기록이 문턱을 못 넘으면, 반환 목록 안에서 문턱을 넘는 다른 기록이 gate 를 열 수 있어야 한다.
주입률(DOE)이 바뀌는 수정이라 SB_RECALLD_GATE_ANY=1 일 때만 켜고, 기본은 예전처럼 살아 있는 첫 키워드 행으로 판정한다."""
import contextlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import sb_recalld  # noqa: E402

Q = '거래처 단가 계약 어떻게 됐지'


class GateAnyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        db = str(root / 'cm.sqlite')
        with contextlib.closing(sqlite3.connect(db)) as con:
            con.execute('''CREATE TABLE observations (
                id INTEGER PRIMARY KEY, project TEXT, title TEXT, subtitle TEXT, narrative TEXT, text TEXT,
                facts TEXT, concepts TEXT, created_at TEXT, created_at_epoch INTEGER, content_hash TEXT, metadata TEXT)''')
            con.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', [
                (1, 'work', '거래처 메모', '', '거래처 메모', '', '[]', '[]', '2026-09-01T00:00:00Z', 1, 'h1', '{}'),
                (2, 'other', '거래처 단가 계약 회신', '', '거래처 단가 계약 회신 발송', '', '[]', '[]',
                 '2026-09-01T00:00:00Z', 2, 'h2', '{}'),
            ])
            con.commit()
        env = patch.dict(os.environ, {'SB_CLAUDE_MEM_DB': db, 'SB_KO_INDEX': str(root / 'ko.sqlite'),
                                      'SB_HOME': str(root / 'sb-home'), 'SB_CLAUDE_MEM_DIR': str(root / 'cm'),
                                      'SB_RECALLD_GATE_ANY': ''})
        env.start()
        self.addCleanup(env.stop)
        hyb = patch.object(sb_recalld, 'HYBRID', False)
        hyb.start()
        self.addCleanup(hyb.stop)

    def recall(self, project, limit=5, **env):
        with patch.dict(os.environ, env):
            return sb_recalld.Engine().recall(Q, project, limit)

    def test_switch_on_lets_passing_other_project_row_open_gate(self):
        out = self.recall('work', SB_RECALLD_GATE_ANY='1')
        self.assertTrue(out['gate']['pass'])
        self.assertEqual(out['gate']['matched'], 3)
        self.assertIn(2, [i['id'] for i in out['items']])
        self.assertEqual(out['items'][0]['id'], 1)   # 보여주는 순서는 그대로(같은 프로젝트 먼저)

    def test_default_keeps_first_row_judgement(self):
        out = self.recall('work')
        self.assertFalse(out['gate']['pass'])          # 예전 동작 유지 — 그림자 비교 전 운영 주입률을 바꾸지 않는다
        self.assertEqual(out['gate']['matched'], 1)

    def test_switch_on_only_counts_rows_actually_returned(self):
        # limit=1 이면 반환 목록에 2번이 없다 — 주입되지 않는 기록으로 gate 를 열지 않는다
        out = self.recall('work', limit=1, SB_RECALLD_GATE_ANY='1')
        self.assertEqual([i['id'] for i in out['items']], [1])
        self.assertFalse(out['gate']['pass'])

    def test_switch_on_same_as_before_when_first_row_passes(self):
        on = self.recall('', SB_RECALLD_GATE_ANY='1')
        off = self.recall('')
        self.assertEqual(on['gate'], off['gate'])
        self.assertTrue(on['gate']['pass'])


if __name__ == '__main__':
    unittest.main()
