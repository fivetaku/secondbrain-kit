"""sb_lock contention contract: two handles on one lock file, NB second lock must fail."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import sb_lock  # noqa: E402


@unittest.skipIf(os.name == 'nt', 'POSIX flock semantics (Windows treats SH as EX)')
class LockContentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'x.lock'

    def test_exclusive_blocks_second_nonblocking_exclusive_until_unlock(self):
        with self.path.open('a') as first, self.path.open('a') as second:
            sb_lock.lock(first, sb_lock.LOCK_EX | sb_lock.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                sb_lock.lock(second, sb_lock.LOCK_EX | sb_lock.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                sb_lock.lock(second, sb_lock.LOCK_SH | sb_lock.LOCK_NB)
            sb_lock.lock(first, sb_lock.LOCK_UN)
            sb_lock.lock(second, sb_lock.LOCK_EX | sb_lock.LOCK_NB)

    def test_shared_allows_shared_but_blocks_exclusive(self):
        with self.path.open('a') as first, self.path.open('a') as second:
            sb_lock.lock(first, sb_lock.LOCK_SH | sb_lock.LOCK_NB)
            sb_lock.lock(second, sb_lock.LOCK_SH | sb_lock.LOCK_NB)
            sb_lock.lock(second, sb_lock.LOCK_UN)
            with self.assertRaises(BlockingIOError):
                sb_lock.lock(second, sb_lock.LOCK_EX | sb_lock.LOCK_NB)

    def test_close_releases_and_timeout_raises(self):
        with self.path.open('a') as waiter:
            with self.path.open('a') as holder:
                sb_lock.lock(holder, sb_lock.LOCK_EX)
                with self.assertRaises(TimeoutError):
                    sb_lock.lock_with_timeout(waiter, sb_lock.LOCK_EX, timeout=0.1)
            sb_lock.lock_with_timeout(waiter, sb_lock.LOCK_EX, timeout=1)


if __name__ == '__main__':
    unittest.main()
