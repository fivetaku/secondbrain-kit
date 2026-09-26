"""이식 가능한 파일 잠금 — POSIX 는 fcntl.flock, Windows 는 LockFileEx.

Windows 도 공유(SH)·배타(EX) 잠금, 같은 핸들 재잠금 no-op, SH↔EX 전환을 flock 과 같게 지원한다.
인터페이스는 fcntl 흉내: lock(f, EX|NB), lock(f, UN).
"""
import os
import time

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

if os.name == 'nt':
    # msvcrt.locking 은 공유 잠금이 없고, 같은 핸들로 다시 잠그면 자기 자신과 충돌한다
    # (flock 은 같은 fd 재잠금·SH↔EX 전환이 된다). LockFileEx 로 flock 의미를 맞춘다.
    import ctypes
    import msvcrt
    import threading
    import weakref
    from ctypes import wintypes

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_void_p), ('InternalHigh', ctypes.c_void_p),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]

    _k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _k32.LockFileEx.restype = wintypes.BOOL
    _k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(_OVERLAPPED)]
    _k32.UnlockFileEx.restype = wintypes.BOOL
    _EXCLUSIVE, _FAIL_IMMEDIATELY = 0x2, 0x1
    _LOCK_VIOLATION = 33          # ERROR_LOCK_VIOLATION
    # handle → (파일 객체 weakref, 모드). 잠근 채 닫힌 파일은 OS 가 잠금을 풀고 핸들 번호가 재사용되므로,
    # 같은 파일 객체가 아직 열려 있을 때만 "쥐고 있다"고 본다.
    _held = {}
    _held_guard = threading.Lock()

    def _unlock(handle) -> None:
        _k32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_OVERLAPPED()))

    def _current(handle, stream):
        entry = _held.get(handle)
        if entry is None:
            return None
        ref, mode = entry
        owner = ref() if ref is not None else None
        if owner is None or owner is not stream or getattr(owner, 'closed', False):
            del _held[handle]     # 낡은 기록 — 실제 잠금은 이미 풀렸다
            return None
        return mode

    def lock(stream, operation: int) -> None:
        fd = stream.fileno() if hasattr(stream, 'fileno') else stream
        handle = msvcrt.get_osfhandle(fd)
        if operation & LOCK_UN:
            with _held_guard:
                _held.pop(handle, None)
            _unlock(handle)
            return
        want = LOCK_EX if operation & LOCK_EX else LOCK_SH
        with _held_guard:
            have = _current(handle, stream)
            if have == want:
                return            # flock 처럼 같은 모드 재잠금은 no-op
            if have is not None:  # 모드 전환(flock 도 원자적이지 않다)
                _unlock(handle)
                del _held[handle]
        flags = (_EXCLUSIVE if want == LOCK_EX else 0) | (_FAIL_IMMEDIATELY if operation & LOCK_NB else 0)
        if not _k32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(_OVERLAPPED())):
            err = ctypes.get_last_error()
            if err == _LOCK_VIOLATION:
                raise BlockingIOError(err, 'lock held by another handle')
            raise ctypes.WinError(err)
        try:
            ref = weakref.ref(stream) if hasattr(stream, 'fileno') else None
        except TypeError:
            ref = None
        if ref is not None:       # 원시 fd 는 기록하지 않는다(재잠금 판정 불가 → 매번 실제로 잠근다)
            with _held_guard:
                _held[handle] = (ref, want)
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
