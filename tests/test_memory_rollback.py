"""V9a — 복구·호환: 마이그레이션 원자성, backup API 복제, 구버전(레거시 API) 읽기 호환, 신규 기록 보존, 운영 DB 보호.

모든 시험은 임시 SB_HOME/DB/복제본에서만 한다. "운영 DB"는 임시 SB_HOME 아래 state.db 로 흉내 내며,
읽기 전용 backup 소스로만 쓰고 바이트가 변하지 않음을 확인한다.
구버전 코드 루트: SB_BASELINE_ROOT (기본: 이 kit — sb_memory 의 레거시 state_head/state_put API 가 그대로 남아 있다).
동결 스냅샷을 따로 갖고 있으면 SB_BASELINE_ROOT 로 지정해 교차 검증한다.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / 'bin'
sys.path.insert(0, str(BIN))
import sb_state  # noqa: E402

PY = sys.executable
BASELINE_ROOT = Path(os.environ.get('SB_BASELINE_ROOT') or ROOT)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def backup_copy(src: Path, dst: Path) -> None:
    with sqlite3.connect(src.as_uri() + '?mode=ro', uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(BASELINE_ROOT.is_dir(), 'frozen baseline root missing: %s' % BASELINE_ROOT)
        self.tmp = Path(tempfile.mkdtemp(prefix='sb-rollback-'))
        self.db = self.tmp / 'state.db'
        self.sb_home = self.tmp / 'sbhome'
        os.environ.update({'SB_HOME': str(self.sb_home), 'SB_STATE_DB': str(self.db), 'SB_STATE_PILOT_SCOPES': 'alpha', 'SB_VERIFY_ALLOWED_ROOTS': str(self.tmp),
                           'SB_PROJECT_ALIASES': str(self.tmp / 'aliases.json'), 'SB_CLAUDE_MEM_DB': str(self.tmp / 'none.db')})
        (self.tmp / 'aliases.json').write_text('{}', encoding='utf-8')

    def legacy_state(self, path: Path):
        from sb_memory import _STATE_SCHEMA
        with sqlite3.connect(path) as db:
            db.executescript(_STATE_SCHEMA)
            db.execute("INSERT INTO state(memory_kind, scope_id, fact_key, version, is_head, recorded_at, body, source, write_id, dedup_key) "
                       "VALUES ('fact','alpha','legacy.key',1,1,'2026-09-01T00:00:00+00:00','old','legacy','w','alpha:legacy:w')")

    def add_verified(self, path: Path, key: str, body: str):
        ev = self.tmp / (key + '.txt'); ev.write_text('measured: %s\n' % body, encoding='utf-8')
        r = sb_state.propose({'scope_id': 'alpha', 'fact_key': key, 'body': body, 'kind': 'measured_fact', 'source': 't', 'write_id': key}, str(path))
        sb_state.verify(r['candidate_id'], 'file_contains', str(ev), db_path=str(path))
        sb_state.accept(r['candidate_id'], 0, str(path))

    def old_code_heads(self, path: Path):
        code = ("import sys, json; sys.path.insert(0, 'bin'); from sb_memory import state_head, state_history; "
                "print(json.dumps({'heads': state_head('alpha', db_path=sys.argv[1]), 'hist': state_history('alpha', 'legacy.key', db_path=sys.argv[1])}))")
        p = subprocess.run([PY, '-c', code, str(path)], cwd=str(BASELINE_ROOT), capture_output=True, text=True,
                           encoding='utf-8', env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_migration_is_atomic_and_retryable(self):
        self.legacy_state(self.db)
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE VIEW state_acceptance AS SELECT 1 AS x')  # 세 번째 테이블 생성이 실패하도록 주입
        before = sha(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            sb_state.migrate(str(self.db))
        with sqlite3.connect(self.db) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({'state_candidate', 'state_verification'} & names, 'partial migration must roll back: %s' % names)
        self.assertEqual(sha(self.db), before)
        with sqlite3.connect(self.db) as db:
            db.execute('DROP VIEW state_acceptance')
        out = sb_state.migrate(str(self.db))
        self.assertEqual(set(out['created']), {'state_candidate', 'state_verification', 'state_acceptance'})
        self.assertEqual(sb_state.migrate(str(self.db))['created'], [], 'second migrate is a no-op')

    def test_backup_api_copy_is_consistent_and_source_untouched(self):
        self.legacy_state(self.db); sb_state.migrate(str(self.db)); self.add_verified(self.db, 'k1', 'v1')
        holder = sqlite3.connect(self.db); holder.execute('BEGIN')  # 소스에 열린 연결이 있어도 backup 은 일관 복제
        try:
            copy = self.tmp / 'copy.db'; backup_copy(self.db, copy)
        finally:
            holder.rollback(); holder.close()
        with sqlite3.connect(copy) as c, sqlite3.connect(self.db) as s:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            for t in ('state', 'state_candidate', 'state_verification', 'state_acceptance'):
                self.assertEqual(c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0], s.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0])

    def test_old_code_reads_migrated_db_and_new_rows_survive(self):
        self.legacy_state(self.db); sb_state.migrate(str(self.db)); self.add_verified(self.db, 'deploy.target', 'staging-2')
        before = sha(self.db)
        data = self.old_code_heads(self.db)
        self.assertEqual(sorted(h['fact_key'] for h in data['heads']), ['deploy.target', 'legacy.key'])
        self.assertEqual(data['hist'][0]['body'], 'old')
        with sqlite3.connect(self.db) as db:  # 구버전 코드 읽기 후에도 신규 테이블·행 보존, 바이트 동일
            self.assertEqual(db.execute('SELECT COUNT(*) FROM state_acceptance').fetchone()[0], 1)
        self.assertEqual(sha(self.db), before)
        code = ("import sys; sys.path.insert(0, 'bin'); from sb_memory import state_put; "
                "print(state_put('alpha', 'legacy.key', 'newer-by-old-code', 'fact', 'legacy', 'w2', expected_version=1, db_path=sys.argv[1])['version'])")
        p = subprocess.run([PY, '-c', code, str(self.db)], cwd=str(BASELINE_ROOT), capture_output=True, text=True,
                           encoding='utf-8', env={**os.environ, 'SB_STATE_PILOT_SCOPES': 'alpha', 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual((p.returncode, p.stdout.strip()), (0, '2'), p.stderr)  # 구버전 쓰기 경로도 계속 동작
        new = sb_state.query('head', 'alpha', 'legacy.key')['items'][0]
        self.assertEqual((new['version'], new['verification_status']), (2, 'unverified'))  # 구코드 쓰기는 acceptance 없음 → 미검증 표시

    def test_operational_db_is_only_a_readonly_backup_source(self):
        # 운영 DB = $SB_HOME/state.db. 레거시 행이 든 운영 DB 를 임시 SB_HOME 에 만든다.
        operational = self.sb_home / 'state.db'
        self.sb_home.mkdir(parents=True)
        self.legacy_state(operational)
        before = sha(operational)
        copy = self.tmp / 'operational-copy.db'; backup_copy(operational, copy)
        out = sb_state.migrate(str(copy))
        self.assertEqual(set(out['created']) >= {'state_candidate'}, True)
        with sqlite3.connect(copy) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            legacy_rows = c.execute('SELECT COUNT(*) FROM state').fetchone()[0]
        heads = sb_state.query('head', 'alpha', db_path=str(copy))
        self.assertTrue(heads['items'] and all(i['verification_status'] == 'unverified' for i in heads['items']),
                        'legacy heads stay unverified')
        self.assertGreaterEqual(legacy_rows, 1)
        self.assertEqual(sha(operational), before, 'operational DB must not change')
        with self.assertRaises(sb_state.StateError):
            sb_state.migrate(str(operational))  # 승인 플래그 없는 운영 마이그레이션 거부
        with self.assertRaises(sb_state.StateError):  # 플래그 하나만으로는 부족 (env + 인자 둘 다 필요)
            sb_state.migrate(str(operational), allow_operational=True)
        self.assertEqual(sha(operational), before)

    # 구 운영판의 "flag-off staged hook == live hook == baseline hook" 패리티 시험은 kit 에 훅이 하나뿐이라
    # 의미가 없어 뺐다. 훅 동작은 test_memory_briefing 이 kit 훅(hooks/session_context.py)으로 검사한다.

if __name__ == '__main__':
    unittest.main()
