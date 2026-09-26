import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import sb_store


class LoopsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'loops.jsonl'
        self.env = mock.patch.dict(os.environ, {
            'SB_HOME': str(Path(self.temp.name) / 'sbhome'),
            'SB_LOOPS_PATH': str(self.path), 'LOOPS_DIR': self.temp.name, 'PYTHONIOENCODING': 'utf-8',
            'PYTHONDONTWRITEBYTECODE': '1',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def item(self, **changes):
        record = {'id': 'L12345678', 'title': '테스트 미결', 'status': 'open',
                  'date_opened': datetime.date.today().isoformat(),
                  'value': 'high', 'expose_count': 0}
        record.update(changes)
        return record

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(BIN / 'loops.py')] + list(args),
                              env=os.environ.copy(), capture_output=True, text=True, encoding='utf-8', timeout=20)

    def test_atomic_save_content_and_environment_path(self):
        records = [self.item(), self.item(id='L87654321', title='다른 제목')]
        sb_store.save_loops(iter(records))
        self.assertEqual(sb_store.loops_path(), str(self.path))
        self.assertEqual(sb_store.load_loops(), records)
        self.assertEqual([json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()], records)
        self.assertTrue(self.path.read_bytes().endswith(b'\n'))

    def test_failed_replace_preserves_existing_file_and_cleans_temp(self):
        sb_store.save_loops([self.item()])
        before = self.path.read_bytes()
        names = set(Path(self.temp.name).iterdir())
        with mock.patch.object(sb_store.os, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                sb_store.save_loops([self.item(title='changed')])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(set(Path(self.temp.name).iterdir()), names)

    def test_corrupt_line_raises_with_line_number(self):
        self.path.write_text(json.dumps(self.item()) + '\n{broken\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '2'):
            sb_store.load_loops()

    def test_lock_timeout_in_second_thread(self):
        results = []
        def contend():
            started = time.monotonic()
            try:
                with sb_store.locked_loops():
                    results.append(('acquired', time.monotonic() - started))
            except TimeoutError:
                results.append(('timeout', time.monotonic() - started))
        with sb_store.locked_loops():
            worker = threading.Thread(target=contend, daemon=True)
            worker.start()
            worker.join(timeout=12)
            alive = worker.is_alive()
        worker.join(timeout=2)
        self.assertFalse(alive, 'second lock did not time out within 12 seconds')
        self.assertEqual(results[0][0], 'timeout')
        self.assertGreaterEqual(results[0][1], 9.9)
        self.assertLess(results[0][1], 12)

    def test_exception_in_transaction_does_not_save(self):
        sb_store.save_loops([self.item()])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            with sb_store.locked_loops() as items:
                items[0]['title'] = 'changed'
                raise RuntimeError('abort')
        self.assertEqual(self.path.read_bytes(), before)
        with sb_store.locked_loops() as items:
            items[0]['title'] = 'committed'
        self.assertEqual(sb_store.load_loops()[0]['title'], 'committed')

    def test_id_collision_is_retried(self):
        values = [uuid.UUID('12345678-0000-0000-0000-000000000000'),
                  uuid.UUID('abcdefab-0000-0000-0000-000000000000')]
        with mock.patch.object(sb_store.uuid, 'uuid4', side_effect=values) as generate:
            result = sb_store.new_loop_id(iter(['L12345678']))
        self.assertEqual(result, 'Labcdefab')
        self.assertEqual(generate.call_count, 2)

    def test_validation_rejects_invalid_fields_and_dates(self):
        for field in ('id', 'title', 'status', 'date_opened'):
            record = self.item()
            del record[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                sb_store.validate_loop(record)
        for changes in ({'status': 'unknown'}, {'value': 'urgent'},
                        {'date_opened': '2026-02-30'}, {'due': '2026-9-05'},
                        {'next_review': 'tomorrow'}, {'closed_at': '2026-09-05T00:00:00'},
                        {'last_exposed': 20260905}, {'stale_at': ''},
                        {'reopened_at': '2026-13-01'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                sb_store.validate_loop(self.item(**changes))
        sb_store.validate_loop(self.item(due='2028-02-29'))

    def test_default_ledger_path_is_under_sb_home(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop('SB_LOOPS_PATH')
            self.assertEqual(sb_store.loops_path(),
                             str(Path(self.temp.name) / 'sbhome' / 'loops' / 'loops.jsonl'))

    def test_personal_commands_and_filters_removed(self):
        # Paseo 워크스페이스 연결(link·--workspace)과 개인 프로젝트 차단 목록(--force)은 kit 에 없다
        for args in (('link', 'L12345678', 'wks_x'), ('add', 't', '--workspace', 'wks_x'), ('add', 't', '--force')):
            with self.subTest(args=args):
                self.assertEqual(self.cli(*args).returncode, 2)
        result = self.cli('add', 'bench', '--project', 'benchmark-demo')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sb_store.load_loops()[0]['project'], 'benchmark-demo')

    def test_reopen_without_linear_field_does_not_add_it(self):
        sb_store.save_loops([self.item(status='done', closed_at='2026-01-01')])
        result = self.cli('reopen', 'L12345678')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('linear_closed', sb_store.load_loops()[0])

    def test_reopen_resets_exposure_and_linear_state(self):
        sb_store.save_loops([self.item(status='stale', expose_count=6,
                                      linear_closed=True, stale_at='2026-01-01')])
        result = self.cli('reopen', 'L12345678', '다시 진행')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('재개', result.stdout)
        record = sb_store.load_loops()[0]
        for field, value in {'status': 'open', 'expose_count': 0, 'linear_closed': False,
                             'reopen_pending': True, 'reopened_from': 'stale',
                             'reopen_note': '다시 진행',
                             'next_review': datetime.date.today().isoformat(),
                             'reopened_at': datetime.date.today().isoformat()}.items():
            self.assertEqual(record[field], value, field)
        self.assertNotIn('stale_at', record)

    def test_six_exposures_apply_backoff_and_stale(self):
        sb_store.save_loops([self.item()])
        for count in range(1, 7):
            result = self.cli('record-exposure', 'L12345678')
            self.assertEqual(result.returncode, 0, result.stderr)
            record = sb_store.load_loops()[0]
            self.assertEqual(record['expose_count'], count)
            self.assertEqual(record['last_exposed'], datetime.date.today().isoformat())
            if count < 6:
                expected = datetime.date.today() + datetime.timedelta(days=[1, 2, 3, 5, 8][count - 1])
                self.assertEqual(record['next_review'], expected.isoformat())
                self.assertEqual(record['status'], 'open')
        self.assertEqual(record['status'], 'stale')
        self.assertEqual(record['stale_at'], datetime.date.today().isoformat())

    def test_brief_and_ids_are_read_only_and_select_same_top_three(self):
        sb_store.save_loops([self.item(id='L{:08x}'.format(n)) for n in range(4)])
        before = (self.path.read_bytes(), self.path.stat().st_mtime_ns)
        result = self.cli('brief')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('미결 4건 중 오늘 볼 것 3건', result.stdout)
        ids = self.cli('brief', '--ids')
        self.assertEqual(ids.returncode, 0, ids.stderr)
        self.assertEqual(ids.stdout.strip(), 'L00000000 L00000001 L00000002')
        for lid in ids.stdout.split():
            self.assertIn(lid, result.stdout)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_invalid_arguments_exit_two_without_changing_ledger(self):
        sb_store.save_loops([self.item()])
        before = self.path.read_bytes()
        commands = [(), ('close',), ('drop',), ('snooze', 'L12345678'), ('add',),
                    ('link', 'L12345678'), ('reopen',), ('record-exposure',),
                    ('add', 'title', '--due'), ('add', 'title', '--due', '2026-02-30'),
                    ('add', 'title', '--value', 'urgent')]
        commands += [('snooze', 'L12345678', days) for days in ('0', '-1', '366', 'abc', '1.5')]
        for args in commands:
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertTrue(result.stdout or result.stderr)
                self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_ids_prevent_mutation(self):
        sb_store.save_loops([self.item(), self.item(title='duplicate')])
        before = self.path.read_bytes()
        for args in [('close', 'L12345678'), ('record-exposure', 'L12345678')]:
            result = self.cli(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.path.read_bytes(), before)

    def test_exposure_batch_is_atomic_and_duplicate_arguments_count_once(self):
        sb_store.save_loops([self.item(), self.item(id='L87654321')])
        before = self.path.read_bytes()
        result = self.cli('record-exposure', 'L12345678', 'missing')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)
        result = self.cli('record-exposure', 'L12345678', 'L87654321', 'L12345678')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item['expose_count'] for item in sb_store.load_loops()], [1, 1])

    def test_concurrent_cli_adds_do_not_lose_updates(self):
        processes = [subprocess.Popen(
            [sys.executable, '-B', str(BIN / 'loops.py'), 'add', 'loop ' + str(n)],
            env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
        ) for n in range(8)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        records = sb_store.load_loops()
        self.assertEqual({record['title'] for record in records}, {'loop ' + str(n) for n in range(8)})
        self.assertEqual(len({record['id'] for record in records}), 8)

    def test_cli_add_and_remaining_commands(self):
        result = self.cli('add', 'new loop', '--value', 'low', '--due', '2028-02-29')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = sb_store.load_loops()[0]
        self.assertRegex(record['id'], r'^L[0-9a-f]{8}$')
        for args, field, value in [
            (('snooze', record['id'], '365'), 'status', 'open'),
            (('close', record['id'], 'completed'), 'status', 'done'),
            (('drop', record['id'], 'discarded'), 'status', 'dropped'),
        ]:
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sb_store.load_loops()[0][field], value)


if __name__ == '__main__':
    unittest.main()
