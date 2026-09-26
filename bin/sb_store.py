"""Shared, atomic JSON Lines storage for the open-loop ledger (macOS/Linux/Windows)."""

import datetime
import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from typing import List, Dict, Iterator, Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sb_config  # noqa: E402
import sb_lock  # noqa: E402

VALID_STATUS = ('open', 'waiting_external', 'snoozed', 'done', 'dropped', 'stale')
VALID_VALUE = ('high', 'medium', 'low')


def loops_path() -> str:
    return os.environ.get('SB_LOOPS_PATH') or sb_config.sb_path('loops', 'loops.jsonl')


def validate_loop(item: Dict) -> None:
    """Reject invalid required fields, enum values, and populated dates."""
    if not isinstance(item, dict):
        raise ValueError('loop must be a JSON object')
    for field in ('id', 'title', 'status', 'date_opened'):
        if field not in item:
            raise ValueError('missing required field: ' + field)
        if not isinstance(item[field], str) or not item[field].strip():
            raise ValueError(field + ' must be a nonempty string')
    if item['status'] not in VALID_STATUS:
        raise ValueError('invalid status: ' + repr(item['status']))
    if 'value' in item and item['value'] not in VALID_VALUE:
        raise ValueError('invalid value: ' + repr(item['value']))
    for field in ('date_opened', 'next_review', 'due', 'last_exposed',
                  'stale_at', 'closed_at', 'reopened_at'):
        value = item.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise ValueError(field + ' must be YYYY-MM-DD')
        try:
            datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(field + ' must be a valid YYYY-MM-DD date') from exc


def load_loops(path: str = None) -> List[Dict]:
    """Read a ledger, failing with its line number instead of losing records."""
    target = loops_path() if path is None else path
    try:
        ledger = open(target, 'r', encoding='utf-8')
    except FileNotFoundError:
        return []
    items = []
    with ledger:
        for number, line in enumerate(ledger, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                validate_loop(item)
            except ValueError as exc:
                raise ValueError('{}: line {}: {}'.format(target, number, exc)) from exc
            items.append(item)
    return items


def _replace(source: str, target: str) -> None:
    """os.replace — Windows 에서는 다른 프로세스가 target 을 잠깐 열고 있으면 PermissionError 가 나므로 짧게 재시도."""
    if not sb_config.IS_WINDOWS:
        os.replace(source, target)
        return
    for attempt in range(20):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def atomic_write_text(path: str, text: str) -> None:
    """Flush a same-directory temporary file before atomically replacing path.

    임시 파일은 replace 전에 닫는다 — Windows 는 열린 파일을 rename 할 수 없다.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = None
    fd, temporary_path = tempfile.mkstemp(dir=directory, prefix='.tmp-', suffix='.jsonl')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as temporary:
            fchmod = getattr(os, 'fchmod', None)  # Windows(<3.13) 에는 없다 — 권한 보존은 POSIX 한정
            if mode is not None and fchmod is not None:
                fchmod(temporary.fileno(), mode)
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        _replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def save_loops(items: Iterable[Dict], path: str = None) -> None:
    """Validate and serialize the whole ledger before replacing its contents."""
    lines = []
    for item in items:
        validate_loop(item)
        lines.append(json.dumps(item, ensure_ascii=False, allow_nan=False) + '\n')
    atomic_write_text(loops_path() if path is None else path, ''.join(lines))


@contextmanager
def locked_loops(path: str = None) -> Iterator[List[Dict]]:
    """Serialize read-modify-write transactions; exceptions roll back changes."""
    target = loops_path() if path is None else path
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with open(target + '.lock', 'a', encoding='utf-8') as lock:
        try:
            sb_lock.lock_with_timeout(lock, sb_lock.LOCK_EX, timeout=10.0)
        except TimeoutError:
            raise TimeoutError('timed out waiting for ledger lock: ' + target)
        try:
            items = load_loops(target)
            yield items
            save_loops(items, target)
        finally:
            sb_lock.lock(lock, sb_lock.LOCK_UN)


def new_loop_id(existing_ids: Iterable[str]) -> str:
    existing = set(existing_ids)
    while True:
        candidate = 'L' + uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate
