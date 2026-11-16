"""sb_pii_sweep·sb_memory 선택형 마스킹 계약 — 마스킹 모듈이 있으면 가리고, 없으면 아무것도 하지 않는다."""
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


class ChromaBackupRetentionTests(unittest.TestCase):
    def test_keeps_newest_backups_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stamps = ['20261001-030000', '20261002-030000', '20261003-030000', '20261004-030000', '20261005-030000']
            for s in stamps:
                (root / ('chroma-before-piisweep-' + s)).mkdir()
                (root / ('chroma-before-piisweep-' + s) / 'chroma.sqlite3').write_text('x')
            (root / 'unrelated').mkdir()
            self.assertEqual(sb_pii_sweep.prune_chroma_backups(root, keep=3), 2)
            left = sorted(p.name for p in root.iterdir())
            self.assertEqual(left, ['chroma-before-piisweep-' + s for s in stamps[2:]] + ['unrelated'])
            self.assertEqual(sb_pii_sweep.prune_chroma_backups(root, keep=0), 0)   # 0 이면 정리 안 함


class WriterLockTests(unittest.TestCase):
    """--chroma 는 워커가 Chroma 쓰기 잠금을 풀 때까지 원본을 열지 않고, 여는 동안 같은 잠금을 쥔다."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.chroma = Path(temp.name) / 'chroma'
        self.chroma.mkdir()
        self.lock = self.chroma / sb_pii_sweep.WRITER_LOCK

    def _write_owner(self, pid):
        self.lock.write_text(json.dumps({'pid': pid, 'ownerId': 'x', 'dataDir': str(self.chroma),
                                         'acquiredAt': '2026-01-01T00:00:00Z'}), encoding='utf-8')

    def test_claims_and_releases(self):
        path = sb_pii_sweep.claim_writer_lock(self.chroma, wait=0)
        rec = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(rec['pid'], os.getpid())
        for key in ('ownerId', 'dataDir', 'acquiredAt'):   # claude-mem 이 읽는 필드 — 빠지면 깨진 잠금으로 본다
            self.assertIsInstance(rec[key], str)
        sb_pii_sweep.release_writer_lock(path)
        self.assertFalse(self.lock.exists())

    def test_stale_lock_from_dead_worker_is_replaced(self):
        self._write_owner(999999)
        with patch.object(sb_pii_sweep, '_pid_alive', return_value=False):
            path = sb_pii_sweep.claim_writer_lock(self.chroma, wait=0)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['pid'], os.getpid())

    def test_live_owner_means_busy(self):
        self._write_owner(424242)
        with patch.object(sb_pii_sweep, '_pid_alive', return_value=True):
            with self.assertRaises(sb_pii_sweep.ChromaBusy):
                sb_pii_sweep.claim_writer_lock(self.chroma, wait=0)
        self.assertEqual(json.loads(self.lock.read_text(encoding='utf-8'))['pid'], 424242)   # 남의 잠금은 그대로

    def test_release_leaves_foreign_lock(self):
        self._write_owner(424242)
        sb_pii_sweep.release_writer_lock(self.lock)
        self.assertTrue(self.lock.exists())

    def test_waits_until_worker_releases(self):
        self._write_owner(424242)
        calls = []

        def alive(pid):
            calls.append(pid)
            if len(calls) >= 2:   # 두 번째 확인 전에 워커가 정상 종료하며 잠금을 지웠다
                self.lock.unlink()
            return True
        with patch.object(sb_pii_sweep, '_pid_alive', side_effect=alive), patch.object(sb_pii_sweep.time, 'sleep'):
            path = sb_pii_sweep.claim_writer_lock(self.chroma, wait=60)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['pid'], os.getpid())

    def test_pid_alive(self):
        self.assertTrue(sb_pii_sweep._pid_alive(os.getpid()))
        self.assertFalse(sb_pii_sweep._pid_alive(0))
        self.assertFalse(sb_pii_sweep._pid_alive('x'))

    def test_busy_skips_vectors_but_keeps_sqlite_result(self):
        with patch.object(sb_pii_sweep.sb_memory, '_pii_masker', return_value=lambda s: s), \
                patch.object(sb_pii_sweep, 'sweep_sqlite', return_value={'observations': [], 'summaries': [], 'prompts': []}), \
                patch.object(sb_pii_sweep, 'sweep_chroma', side_effect=sb_pii_sweep.ChromaBusy('PID 1')), \
                patch('builtins.print') as out:
            self.assertEqual(sb_pii_sweep.main(['--chroma', '--json']), 0)
        res = json.loads(out.call_args[0][0])
        self.assertEqual(res['status'], 'ok')
        self.assertIn('skipped', res['chroma'])


if __name__ == '__main__':
    unittest.main()
