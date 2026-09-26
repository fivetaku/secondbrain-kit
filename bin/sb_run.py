#!/usr/bin/env python3
"""Run batch commands with a portable lock/deadline, or check a JSON plan.

Exit codes: child's code on normal exit, 124 on timeout, 75 when the lock is busy,
126/127 when the command cannot be started, 130 on Ctrl-C.
POSIX kills the whole process group (SIGTERM, then SIGKILL after 5s);
Windows starts a new process group and kills the tree with `taskkill /T /F`.
"""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sb_lock  # noqa: E402
from sb_config import IS_WINDOWS  # noqa: E402

GRACE_SECONDS = 5.0


def check_json(path: str, require_array: bool, min_len: int) -> int:
    try:
        with open(path, encoding="utf-8") as source:
            value = json.load(source)
        if require_array or min_len:
            if not isinstance(value, list):
                raise ValueError("expected a JSON array")
            if len(value) < min_len:
                raise ValueError("array length must be >= {}".format(min_len))
    except (OSError, ValueError, UnicodeError) as error:
        print("check-json: {}: {}".format(path, error), file=sys.stderr)
        return 1
    return 0


def _popen_kwargs() -> Dict[str, Any]:
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def stop_group(process: subprocess.Popen) -> None:
    """Stop the child and all of its descendants."""
    if IS_WINDOWS:
        # taskkill /T walks the tree from the PID; /F forces termination.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        try:
            process.wait(timeout=GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    # Do not use leader.wait(timeout=5): the leader can exit before its children.
    time.sleep(GRACE_SECONDS)
    process.poll()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(command: Sequence[str], timeout: Optional[float], **popen: Any) -> int:
    """Inherit output streams (unless overridden) and isolate descendants."""
    try:
        process = subprocess.Popen(list(command), **_popen_kwargs(), **popen)
    except OSError as error:
        print("run: {}".format(error), file=sys.stderr)
        return 127 if isinstance(error, FileNotFoundError) else 126
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print("timeout after {} seconds".format(timeout), file=sys.stderr)
        stop_group(process)
        return 124
    except KeyboardInterrupt:
        stop_group(process)
        return 130
    return code if code >= 0 else 128 - code


class LockBusy(Exception):
    pass


class HeldLock:
    """Context manager: non-blocking exclusive lock on a file that is never unlinked."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.stream = None

    def __enter__(self) -> "HeldLock":
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Keep the inode: unlinking a lock file lets concurrent runs bypass it.
        self.stream = open(self.path, "a+")
        try:
            sb_lock.lock(self.stream, sb_lock.LOCK_EX | sb_lock.LOCK_NB)
        except BlockingIOError as error:  # sb_lock maps Windows lock conflicts to this too
            self.stream.close()
            self.stream = None
            raise LockBusy(str(error))
        except OSError:
            self.stream.close()
            self.stream = None
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.stream is not None:
            try:
                sb_lock.lock(self.stream, sb_lock.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--check-json")
    parser.add_argument("--require-array", action="store_true")
    parser.add_argument("--min-len", type=int, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.min_len < 0:
        parser.error("--min-len must be nonnegative")
    if args.timeout is not None and (
        not math.isfinite(args.timeout) or args.timeout <= 0
    ):
        parser.error("--timeout must be finite and positive")
    if args.check_json is not None:
        if args.command or args.lock or args.timeout is not None:
            parser.error("--check-json cannot be combined with command execution")
        return check_json(args.check_json, args.require_array, args.min_len)
    if args.require_array or args.min_len:
        parser.error("JSON options require --check-json")
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if args.lock is None:
        return run_command(command, args.timeout)
    try:
        with HeldLock(args.lock):
            return run_command(command, args.timeout)
    except LockBusy:
        print("already running", file=sys.stderr)
        return 75
    except OSError as error:
        print("lock: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
