"""이식 가능한 파일 잠금 — POSIX 는 fcntl.flock, Windows 는 msvcrt.locking.

Windows 에는 공유 잠금이 없어 SH 도 배타 잠금으로 취급한다(동시 읽기가 직렬화될 뿐 안전성은 같다).
인터페이스는 fcntl 흉내: lock(f, EX|NB), lock(f, UN).
"""
import os
import time

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

if os.name == 'nt':
    import msvcrt

    def lock(stream, operation: int) -> None:
        fd = stream.fileno() if hasattr(stream, 'fileno') else stream
        os.lseek(fd, 0, os.SEEK_SET)
        if operation & LOCK_UN:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return
        mode = msvcrt.LK_NBLCK
        if operation & LOCK_NB:
            try:
                msvcrt.locking(fd, mode, 1)
            except OSError as exc:
                raise BlockingIOError(str(exc))
            return
        while True:  # LK_LOCK 은 10초 뒤 실패하므로 직접 반복
            try:
                msvcrt.locking(fd, mode, 1)
                return
            except OSError:
                time.sleep(0.05)
else:
    import fcntl

    _MAP = {LOCK_SH: fcntl.LOCK_SH, LOCK_EX: fcntl.LOCK_EX, LOCK_UN: fcntl.LOCK_UN}

    def lock(stream, operation: int) -> None:
        op = 0
        for k, v in _MAP.items():
            if operation & k:
                op |= v
        if operation & LOCK_NB:
            op |= fcntl.LOCK_NB
        fcntl.flock(stream.fileno() if hasattr(stream, 'fileno') else stream, op)


def lock_with_timeout(stream, operation: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock(stream, operation | LOCK_NB)
            return
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                raise TimeoutError('lock timeout')
            time.sleep(0.05)
