import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Final
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import sb_lock  # noqa: E402
import sb_run  # noqa: E402

RUNNER: Final = Path(__file__).resolve().parents[1] / "bin" / "sb_run.py"
PYTHON: Final = sys.executable
POSIX: Final = os.name != "nt"


def py(code: str) -> list:
    return [PYTHON, "-c", code]


class SbRunTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sb-run-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(RUNNER), *arguments],
            capture_output=True, text=True, timeout=15, check=False,
        )

    def test_lock_contention_returns_75_without_running_command(self) -> None:
        lock = self.root / "nightly.lock"
        with lock.open("a+") as held:
            sb_lock.lock(held, sb_lock.LOCK_EX | sb_lock.LOCK_NB)
            result = self.run_cli(
                "--lock", str(lock), "--", *py("print('ran')"),
            )
        self.assertEqual(result.returncode, 75)
        self.assertIn("already running", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_lock_released_after_command_exits(self) -> None:
        lock = self.root / "sub" / "nightly.lock"
        result = self.run_cli("--lock", str(lock), "--", *py("raise SystemExit(0)"))
        self.assertEqual(result.returncode, 0, result.stderr)
        with lock.open("a+") as held:
            sb_lock.lock(held, sb_lock.LOCK_EX | sb_lock.LOCK_NB)
            sb_lock.lock(held, sb_lock.LOCK_UN)

    def test_timeout_returns_124(self) -> None:
        result = self.run_cli("--timeout", "1", "--", *py("import time; time.sleep(5)"))
        self.assertEqual(result.returncode, 124, result.stderr)

    @unittest.skipUnless(POSIX, "process-group signal semantics are POSIX-only")
    def test_timeout_kills_term_resistant_descendant_after_leader_exits(self) -> None:
        child = (
            "import os, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print(os.getpid(), flush=True); time.sleep(30)"
        )
        command = shlex.quote(PYTHON) + " -c " + shlex.quote(child) + " & wait"
        with subprocess.Popen(
            [PYTHON, str(RUNNER), "--timeout", "1", "--", "/bin/sh", "-c", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as process:
            self.assertIsNotNone(process.stdout)
            assert process.stdout is not None
            descendant = 0
            try:
                self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                descendant = int(process.stdout.readline().strip())
                started = time.monotonic()
                _, stderr = process.communicate(timeout=12)
                self.assertEqual(process.returncode, 124, stderr)
                self.assertGreaterEqual(time.monotonic() - started, 4.5)
                state = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(descendant)],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                if descendant:
                    try:
                        os.kill(descendant, signal.SIGKILL)
                    except ProcessLookupError:
                        descendant = 0
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_windows_kill_path_uses_taskkill_tree(self) -> None:
        """Exercise the Windows branch with a fake process (no real taskkill here)."""
        calls = []

        class FakeProcess:
            pid = 4321

            def wait(self, timeout=None):
                return 1

            def kill(self):
                raise AssertionError("kill() should not be needed")

        original_run, original_flag = sb_run.subprocess.run, sb_run.IS_WINDOWS
        sb_run.subprocess.run = lambda args, **kw: calls.append(args)
        sb_run.IS_WINDOWS = True
        try:
            sb_run.stop_group(FakeProcess())
            kwargs = sb_run._popen_kwargs()
        finally:
            sb_run.subprocess.run, sb_run.IS_WINDOWS = original_run, original_flag
        self.assertEqual(calls, [["taskkill", "/T", "/F", "/PID", "4321"]])
        self.assertIn("creationflags", kwargs)
        self.assertNotIn("start_new_session", kwargs)

    def test_missing_command_returns_127(self) -> None:
        result = self.run_cli("--", str(self.root / "does-not-exist"))
        self.assertEqual(result.returncode, 127)

    def test_child_exit_code_passes_through(self) -> None:
        result = self.run_cli("--", *py("raise SystemExit(3)"))
        self.assertEqual(result.returncode, 3)

    def test_stdout_and_stderr_pass_through(self) -> None:
        result = self.run_cli(
            "--", *py("import sys; print('output'); print('error', file=sys.stderr)"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.replace("\r\n", "\n"), "output\n")
        self.assertEqual(result.stderr.replace("\r\n", "\n"), "error\n")

    def test_check_json_accepts_nonempty_array(self) -> None:
        plan = self.root / "plan.json"
        plan.write_text('[{"source_observation_ids": [12]}]', encoding="utf-8")
        result = self.run_cli("--check-json", str(plan), "--require-array", "--min-len", "1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_json_rejects_invalid_inputs(self) -> None:
        cases = (
            ("missing", None), ("malformed", b"["), ("empty", b"[]"),
            ("object", b"{}"), ("scalar", b"1"), ("invalid-utf8", b"[\xff]"),
        )
        for name, payload in cases:
            with self.subTest(name=name):
                plan = self.root / (name + ".json")
                if payload is not None:
                    plan.write_bytes(payload)
                result = self.run_cli(
                    "--check-json", str(plan), "--require-array", "--min-len", "1",
                )
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stderr.strip())


if __name__ == "__main__":
    unittest.main()
