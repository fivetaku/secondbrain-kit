"""doctor 의 Chroma 잠금·디스크 점검, 쓰지 않는 에이전트의 캡처 끊김(정보 수준), sb setup 1단계의 ⚠ 통과.
모두 격리 홈(임시 폴더)의 가짜 로그·잠금 파일·희소 파일로 돈다 — 실제 claude-mem 폴더는 열지 않는다."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bin'))
import sb_setup  # noqa: E402


def load_doctor():
    spec = importlib.util.spec_from_file_location('sbkit_doctor_lockdisk', str(ROOT / 'installer' / 'doctor.py'))
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    return d


LOCK_LINE = '[{d} 10:00:00.000] [ERROR] [CHROMA_MCP] Chroma data dir /x is already owned by PID 4242; refusing to start a second writer\n'
BACKOFF_LINE = '[{d} 10:00:01.000] [WARN ] [CHROMA_MCP] chroma-mcp connection in backoff (9s remaining)\n'
FTS_LINE = '[{d} 10:00:02.000] [WARN ] [SEARCH] Chroma search failed, falling back to FTS\n'
HOOK_LINE = '[{d} 10:00:03.000] [INFO ] [HOOK  ] cmd: grep "Batch add failed" log\n'
OK_LINE = '[{d} 10:00:04.000] [INFO ] [WORKER] 정상 줄\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sb-doctor-ld-'))
        self.home = self.tmp / 'home'
        self.cm = self.home / '.claude-mem'
        self.sbhome = self.home / '.secondbrain'
        for p in (self.cm, self.sbhome):
            p.mkdir(parents=True)
        env = {'HOME': str(self.home), 'USERPROFILE': str(self.home), 'SB_HOME': str(self.sbhome),
               'SB_CLAUDE_MEM_DIR': str(self.cm)}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ('SB_CHROMA_PATH', 'SB_DOCTOR_DISK_WARN_GB'):
            os.environ.pop(k, None)
        self.d = load_doctor()
        del self.d.rows[:]

    def row(self, name):
        found = [r for r in self.d.rows if r[1] == name]
        self.assertEqual(len(found), 1, self.d.rows)
        return found[0]


class ChromaLockTests(Base):
    def write_log(self, day, lines):
        logs = self.cm / 'logs'
        logs.mkdir(exist_ok=True)
        (logs / ('claude-mem-%s.log' % day.isoformat())).write_text(''.join(x.format(d=day.isoformat()) for x in lines),
                                                                     encoding='utf-8')

    def lock(self, pid):
        c = self.cm / 'chroma'
        c.mkdir(exist_ok=True)
        (c / '.claude-mem-chroma-writer.lock').write_text(json.dumps({'pid': pid, 'ownerId': 'secret-owner', 'host': 'h'}),
                                                          encoding='utf-8')

    def test_no_logs_is_info_not_ok(self):
        self.d.check_chroma_lock()
        level, _, detail = self.row('Chroma 잠금')
        self.assertEqual(level, self.d.INFO)
        self.assertIn('로그 없음', detail)
        self.assertIn('~', detail)
        self.assertNotIn(str(self.home), detail)

    def test_clean_logs_ok(self):
        today = date(2026, 11, 16)
        self.write_log(today, [OK_LINE] * 5 + [HOOK_LINE])
        self.d.check_chroma_lock(today=today)
        level, _, detail = self.row('Chroma 잠금')
        self.assertEqual(level, '✅')
        self.assertIn('0건', detail)

    def test_counts_lines_once_and_ignores_hook_lines(self):
        today = date(2026, 11, 16)
        # 잠금 줄은 두 문구를 다 담지만 한 줄로 센다. 훅 줄은 뺀다. 8일 전 파일은 창 밖.
        self.write_log(today, [LOCK_LINE, BACKOFF_LINE, FTS_LINE, HOOK_LINE, OK_LINE])
        self.write_log(today - timedelta(days=6), [LOCK_LINE])
        self.write_log(today - timedelta(days=8), [LOCK_LINE] * 50)
        self.d.check_chroma_lock(today=today)
        level, _, detail = self.row('Chroma 잠금')
        self.assertEqual(level, '⚠')
        self.assertIn('줄 4건', detail)
        self.assertIn('로그 2개', detail)
        self.assertNotIn('같은 Chroma 폴더', detail)

    def test_many_lines_strong_warning_with_kit_commands_only(self):
        today = date(2026, 11, 16)
        self.write_log(today, [LOCK_LINE] * 15)
        self.write_log(today - timedelta(days=1), [BACKOFF_LINE] * 5)
        self.d.check_chroma_lock(today=today)
        level, _, detail = self.row('Chroma 잠금')
        self.assertEqual(level, '⚠')
        self.assertIn('20건', detail)
        self.assertIn('같은 Chroma 폴더', detail)
        self.assertIn('sb health --fix', detail)
        self.assertNotIn('npx', detail)
        self.assertNotIn('--restart-worker', detail)

    def test_reads_only_tail_of_big_log(self):
        today = date(2026, 11, 16)
        self.write_log(today, [LOCK_LINE] * 100 + [OK_LINE] * 100)
        with mock.patch.object(self.d, 'LOG_TAIL_BYTES', len(OK_LINE.format(d=today.isoformat()).encode()) * 100 + 10):
            self.d.check_chroma_lock(today=today)
        level, _, detail = self.row('Chroma 잠금')
        self.assertEqual(level, '✅', detail)   # 앞쪽 잠금 줄은 읽지 않았다(잘린 첫 줄도 버림)

    def test_tail_reader_drops_partial_first_line(self):
        p = self.tmp / 'x.log'
        p.write_bytes(b'aaaa\nbbbb\ncccc\n')
        self.assertEqual(self.d._read_tail(p, 7), b'cccc\n')
        self.assertEqual(self.d._read_tail(p, 100), b'aaaa\nbbbb\ncccc\n')

    def test_lock_owner_alive_and_dead_pid_only(self):
        today = date(2026, 11, 16)
        self.write_log(today, [OK_LINE])
        self.lock(os.getpid())
        self.d.check_chroma_lock(today=today)
        detail = self.row('Chroma 잠금')[2]
        self.assertIn('PID %d 살아 있음' % os.getpid(), detail)
        self.assertNotIn('secret-owner', detail)
        # 끝난 자식 프로세스 pid — 다시 쓰일 수는 있지만 바로는 아니다
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        self.lock(child.pid)
        del self.d.rows[:]
        self.d.check_chroma_lock(today=today)
        detail = self.row('Chroma 잠금')[2]
        self.assertIn('PID %d 없음' % child.pid, detail)
        self.assertNotIn('secret-owner', detail)

    def test_broken_lock_file(self):
        (self.cm / 'chroma').mkdir()
        (self.cm / 'chroma' / '.claude-mem-chroma-writer.lock').write_text('{bad', encoding='utf-8')
        self.d.check_chroma_lock()
        self.assertIn('pid 못 읽음', self.row('Chroma 잠금')[2])

    def test_pid_alive(self):
        self.assertTrue(self.d.pid_alive(os.getpid()))
        self.assertIsNone(self.d.pid_alive(0))
        self.assertIsNone(self.d.pid_alive(True))
        self.assertIsNone(self.d.pid_alive('12'))


class DiskUsageTests(Base):
    def sparse(self, path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as f:
            f.truncate(size)

    def test_small_home_ok(self):
        self.sparse(self.sbhome / 'local' / 'a.bin', 1024 * 1024)
        with mock.patch.object(self.d.shutil, 'disk_usage', return_value=mock.Mock(free=500 * 1024 ** 3)):
            self.d.check_disk_usage()
        level, _, detail = self.row('디스크 사용')
        self.assertEqual(level, '✅')
        self.assertIn('~/.secondbrain/local', detail)
        self.assertNotIn(str(self.home), detail)

    def test_big_total_warns_top3_and_excludes_live_db(self):
        mb = 1024 ** 2
        self.sparse(self.sbhome / 'local' / 'copy' / 'big.db', 400 * mb)
        self.sparse(self.sbhome / 'index' / 'chroma-snapshot' / 'chroma.sqlite3', 300 * mb)
        self.sparse(self.sbhome / 'index' / 'chroma-recalld' / 'chroma.sqlite3', 200 * mb)
        self.sparse(self.sbhome / 'index' / 'fts.db', 10 * mb)
        self.sparse(self.cm / 'claude-mem.db', 900 * mb)            # 운영 DB 자신은 세지 않는다
        self.sparse(self.cm / 'claude-mem.db-wal', 900 * mb)
        self.sparse(self.cm / 'claude-mem-backup-1.db', 50 * mb)
        self.sparse(self.cm / 'backups' / 'claude-mem-pre-1.db', 60 * mb)
        self.sparse(self.cm / 'notes.db', 70 * mb)                   # 백업 이름이 아니면 뺀다
        os.environ['SB_DOCTOR_DISK_WARN_GB'] = '0.9'
        with mock.patch.object(self.d.shutil, 'disk_usage', return_value=mock.Mock(free=500 * 1024 ** 3)):
            self.d.check_disk_usage()
        level, _, detail = self.row('디스크 사용')
        self.assertEqual(level, '⚠')
        self.assertIn('합계 1020MB', detail)       # 400+300+200+10+50+60
        self.assertIn('자동 삭제 안 함', detail)
        big = detail.split('큰 것: ')[1]
        self.assertEqual([x.split(' ')[0] for x in big.split(' — ')[0].split(', ')],
                         ['~/.secondbrain/local', '~/.secondbrain/index/chroma-snapshot', '~/.secondbrain/index/chroma-recalld'])
        self.assertTrue((self.cm / 'claude-mem-backup-1.db').exists())   # 지우지 않았다

    def test_low_free_space_warns(self):
        self.sparse(self.sbhome / 'local' / 'a.bin', 1024)
        with mock.patch.object(self.d.shutil, 'disk_usage', return_value=mock.Mock(free=1024 ** 3)):
            self.d.check_disk_usage()
        self.assertEqual(self.row('디스크 사용')[0], '⚠')

    def test_backup_names(self):
        for n in ('claude-mem.db', 'claude-mem.db-shm', 'a.db.bak', 'x-backup.db', 'plain.db', 'b.txt'):
            self.sparse(self.cm / n, 1)
        self.sparse(self.cm / 'backups' / 'old.db', 1)
        self.assertEqual(sorted(p.name for p in self.d._claude_mem_backups()), ['a.db.bak', 'old.db', 'x-backup.db'])


class CaptureAgentTests(Base):
    def make_db(self, ages_h):
        con = sqlite3.connect(str(self.cm / 'claude-mem.db'))
        con.execute('CREATE TABLE sdk_sessions (id INTEGER PRIMARY KEY, platform_source TEXT, started_at_epoch INTEGER)')
        for src, age in ages_h.items():
            con.execute('INSERT INTO sdk_sessions (platform_source, started_at_epoch) VALUES (?, ?)',
                        (src, int((time.time() - age * 3600) * 1000)))
        con.commit()
        con.close()

    def run_capture(self):
        with mock.patch.object(self.d.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')):
            self.d.check_state_and_capture()

    def answer(self, agents):
        sb_setup.set_answers({'agents': agents})

    def test_stale_unused_agent_is_info(self):
        self.make_db({'claude': 1, 'codex': 24 * 51})
        self.answer(['claude'])
        self.run_capture()
        self.assertEqual(self.row('캡처 claude')[0], '✅')
        level, _, detail = self.row('캡처 codex')
        self.assertEqual(level, self.d.INFO)
        self.assertIn('쓰지 않는', detail)

    def test_stale_used_agent_still_warns(self):
        self.make_db({'claude': 1, 'codex': 24 * 51})
        self.answer(['claude', 'Codex'])
        self.run_capture()
        self.assertEqual(self.row('캡처 codex')[0], '⚠')

    def test_no_answers_keeps_warning(self):
        self.make_db({'claude': 1, 'codex': 24 * 51})
        self.assertFalse(sb_setup.setup_path().exists())
        self.run_capture()
        self.assertEqual(self.row('캡처 codex')[0], '⚠')

    def test_broken_setup_json_keeps_warning(self):
        self.make_db({'codex': 24 * 51})
        sb_setup.setup_path().write_text('{broken', encoding='utf-8')
        self.run_capture()
        self.assertEqual(self.row('캡처 codex')[0], '⚠')


class Stage1LevelTests(Base):
    def test_stage1_passes_on_warning_and_info_stops_on_error(self):
        rows = [('✅', 'venv', 'ok'), ('⚠', '캡처 codex', '오래됨'), (self.d.INFO, 'Chroma 잠금', '로그 없음')]
        with mock.patch.object(sb_setup, '_doctor_rows', return_value=rows):
            res = sb_setup.stage1({})
        self.assertEqual(res['verdict'], 'pass')
        self.assertTrue(all(m['pass'] for m in res['metrics']))
        with mock.patch.object(sb_setup, '_doctor_rows', return_value=rows + [('❌', 'venv 패키지', '없음')]):
            self.assertEqual(sb_setup.stage1({})['verdict'], 'stop')

    def test_new_checks_in_checks_and_doctor_rows_runs_them(self):
        self.assertIn(self.d.check_chroma_lock, self.d.CHECKS)
        self.assertIn(self.d.check_disk_usage, self.d.CHECKS)
        d = self.d
        others = tuple(f for f in d.CHECKS if f not in (d.check_chroma_lock, d.check_disk_usage))
        names = {f.__name__ for f in others}
        fake = load_doctor()
        for n in names:
            setattr(fake, n, lambda: None)
        fake.CHECKS = tuple(getattr(fake, f.__name__) for f in d.CHECKS)
        with mock.patch.object(sb_setup, '_load_doctor', return_value=fake):
            t0 = time.time()
            rows = sb_setup._doctor_rows()
            took = time.time() - t0
        self.assertEqual({r[1] for r in rows}, {'Chroma 잠금', '디스크 사용'})
        self.assertLess(took, 2.0)


if __name__ == '__main__':
    unittest.main()
