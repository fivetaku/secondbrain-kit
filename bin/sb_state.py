#!/usr/bin/env python3
"""L2 상태층 후보·검증·채택 CLI (memory-operations M1).

상태 DB 기본 위치: $SB_HOME/state.db (SB_STATE_DB 또는 --db 로 변경).
쓰기 허용 scope: SB_STATE_PILOT_SCOPES(쉼표 목록). 미설정·빈 값·'*' = 전 scope 허용.

  propose  --file request.json           후보만 저장 (head 불변). 같은 (scope, source, write_id) 재시도는 기존 결과 반환,
                                          payload 가 다르면 conflict.
  verify   --candidate ID --method M --target T [--evidence-ref R]
                                          등록된 읽기 전용 검사기만 실행. 기억 본문의 명령·URL 은 절대 실행하지 않는다.
                                          파일 target 은 '<절대경로>#L1-L3' (마지막 '#' 뒤가 줄 범위; C:\\x\\y.md#1-3 도 가능).
  accept   --candidate ID --expected-version N
                                          confirmed 검증 + 근거 조건 + 파일럿 gate 통과 시 한 트랜잭션으로 head 교체.
  head     --scope S [--fact-key K]      읽기 전용(mode=ro). DB/테이블을 만들지 않는다.
  history  --scope S --fact-key K         읽기 전용.
  migrate  --db PATH                      추가 테이블 생성. 운영 DB 는 명시 승인 플래그 없이는 거부.

종료 코드: 0 ok · 2 invalid · 3 stale_version · 4 busy · 5 conflict · 6 verification_failed · 7 schema_missing · 8 unavailable
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sb_config  # noqa: E402
from sb_scope import require_scope, _load_aliases, _norm_name, _norm_path, is_path_entry, path_within  # noqa: E402
from sb_memory import state_db_path, state_pilot_scopes, state_scope_allowed, STATE_MEMORY_KINDS  # noqa: E402

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
KINDS = ('measured_fact', 'user_decision', 'inference')
CANDIDATE_STATUS = ('proposed', 'accepted', 'rejected', 'conflict')
VERIFY_RESULTS = ('confirmed', 'contradicted', 'unknown', 'rejected')
MEASUREMENT_METHODS = ('file_contains', 'json_file_value', 'observation_ref')
DECISION_METHODS = ('user_utterance_check',)
EXIT = {'ok': 0, 'invalid': 2, 'stale_version': 3, 'busy': 4, 'conflict': 5,
        'verification_failed': 6, 'schema_missing': 7, 'unavailable': 8}

_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_candidate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    body TEXT NOT NULL,
    value_json TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('measured_fact','user_decision','inference')),
    source TEXT NOT NULL,
    write_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    logical_key TEXT NOT NULL,
    expected_version INTEGER,
    observed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('proposed','accepted','rejected','conflict')),
    recorded_at TEXT NOT NULL,
    UNIQUE(scope_id, source, write_id)
);
CREATE TABLE IF NOT EXISTS state_verification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES state_candidate(id),
    state_id INTEGER REFERENCES state(id),
    scope_id TEXT NOT NULL,
    method TEXT NOT NULL,
    target TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('confirmed','contradicted','unknown','rejected')),
    checked_at TEXT NOT NULL,
    evidence_ref TEXT,
    evidence_hash TEXT,
    detail TEXT,
    CHECK ((candidate_id IS NOT NULL) + (state_id IS NOT NULL) = 1)
);
-- scope 일관성: 대상 행의 scope_id 와 다르면 거부 (복합 FK 와 동등한 DB 제약)
CREATE TRIGGER IF NOT EXISTS state_verification_scope_candidate
BEFORE INSERT ON state_verification WHEN NEW.candidate_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'verification scope mismatch with candidate')
    WHERE (SELECT scope_id FROM state_candidate WHERE id = NEW.candidate_id) IS NOT NEW.scope_id;
END;
CREATE TRIGGER IF NOT EXISTS state_verification_scope_state
BEFORE INSERT ON state_verification WHEN NEW.state_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'verification scope mismatch with state')
    WHERE (SELECT scope_id FROM state WHERE id = NEW.state_id) IS NOT NEW.scope_id;
END;
CREATE TABLE IF NOT EXISTS state_acceptance (
    candidate_id INTEGER NOT NULL UNIQUE REFERENCES state_candidate(id),
    state_id INTEGER NOT NULL UNIQUE REFERENCES state(id),
    verification_id INTEGER NOT NULL REFERENCES state_verification(id),
    label TEXT NOT NULL CHECK (label IN ('user-confirmed','operationally measured')),
    accepted_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS state_acceptance_verification_match
BEFORE INSERT ON state_acceptance
BEGIN
    SELECT RAISE(ABORT, 'acceptance verification must be confirmed and belong to the candidate')
    WHERE NOT EXISTS (SELECT 1 FROM state_verification v WHERE v.id = NEW.verification_id
                      AND v.candidate_id = NEW.candidate_id AND v.result = 'confirmed');
END;
"""
EXTRA_TABLES = ('state_candidate', 'state_verification', 'state_acceptance')


class StateError(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def payload(self) -> Dict[str, Any]:
        return {'schema_version': SCHEMA_VERSION, 'status': 'error', 'error': self.code,
                'message': self.message, **self.extra}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def logical_key(scope_id: str, source: str, write_id: str) -> str:
    """정규 JSON 배열 직렬화 — 콜론 구분자 충돌이 없다."""
    return json.dumps([scope_id, source, write_id], ensure_ascii=False, separators=(',', ':'))


def payload_hash(fact_key: str, body: str, value_json: Optional[str], kind: str,
                 observed_at: Optional[str]) -> str:
    data = json.dumps([fact_key, body, value_json, kind, observed_at], ensure_ascii=False,
                      separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def validate_timestamp(value: Optional[str], field: str) -> Optional[str]:
    """ISO8601 + 시간대 필수, 미래 금지. None 은 None 유지(대체 금지)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StateError('invalid', field + ' must be an ISO8601 string or null')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise StateError('invalid', field + ' is not ISO8601: ' + value)
    if parsed.tzinfo is None:
        raise StateError('invalid', field + ' must carry a timezone offset')
    if parsed > datetime.now(timezone.utc) + timedelta(seconds=60):
        raise StateError('invalid', field + ' is in the future')
    return value


def write_connect(db_path: Optional[str]) -> sqlite3.Connection:
    """쓰기 연결: busy_timeout 5000ms, foreign_keys ON, 명시적 트랜잭션. 스키마를 만들지 않는다."""
    target = state_db_path(db_path)
    if not target.exists():
        raise StateError('unavailable', 'state db does not exist: ' + str(target))
    db = sqlite3.connect(str(target), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout = %d' % BUSY_TIMEOUT_MS)
    db.execute('PRAGMA foreign_keys = ON')
    return db


def ro_connect(db_path: Optional[str]) -> sqlite3.Connection:
    target = state_db_path(db_path)
    if not target.exists():
        raise StateError('unavailable', 'state db does not exist: ' + str(target))
    db = sqlite3.connect(_ro_uri(target), uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    db.row_factory = sqlite3.Row
    return db


def _ro_uri(target: Path) -> str:
    """읽기 전용 SQLite URI — as_uri() 가 Windows 드라이브 경로(file:///C:/...)와 특수문자(?, #)를 처리한다."""
    return target.resolve().as_uri() + '?mode=ro'


def tables(db: sqlite3.Connection) -> set:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def require_extra_schema(db: sqlite3.Connection) -> None:
    missing = [t for t in ('state',) + EXTRA_TABLES if t not in tables(db)]
    if missing:
        raise StateError('schema_missing', 'tables missing: %s (run `sb_state.py migrate --db <copy>`; '
                         'operational migration needs approval)' % ', '.join(missing), missing=missing)


def _busy(exc: sqlite3.OperationalError) -> bool:
    return 'locked' in str(exc).lower() or 'busy' in str(exc).lower()


def migrate(db_path: str, allow_operational: bool = False) -> Dict[str, Any]:
    target = state_db_path(db_path)
    # 운영 DB = SB_HOME 아래 state.db (SB_STATE_DB 로 바꿔도 이 경로는 보호된다)
    operational = _norm_path(sb_config.sb_path('state.db'))
    if _norm_path(str(target)) == operational and not (allow_operational and
                                                 os.environ.get('SB_STATE_ALLOW_OPERATIONAL_MIGRATION') == '1'):
        raise StateError('invalid', 'refusing to migrate operational state db without explicit approval flag')
    target.parent.mkdir(parents=True, exist_ok=True)
    from sb_memory import _STATE_SCHEMA
    db = sqlite3.connect(str(target), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        db.execute('PRAGMA busy_timeout = %d' % BUSY_TIMEOUT_MS)
        before = tables(db)
        # executescript 는 문장 단위로 실행된다 — 원자성은 BEGIN/COMMIT 을 스크립트 안에 넣어 확보한다.
        db.executescript('BEGIN IMMEDIATE;' + _STATE_SCHEMA + _EXTRA_SCHEMA + 'COMMIT;')
        after = tables(db)
    finally:
        db.close()
    return {'schema_version': SCHEMA_VERSION, 'status': 'ok', 'db': str(target),
            'created': sorted(after - before)}


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------

def propose(request: Dict[str, Any], db_path: Optional[str] = None) -> Dict[str, Any]:
    if not isinstance(request, dict):
        raise StateError('invalid', 'request must be a JSON object')
    scope_id = require_scope(request.get('scope_id'))
    kind = request.get('kind')
    if kind not in KINDS:
        raise StateError('invalid', 'kind must be one of %s' % (KINDS,))
    fact_key = (request.get('fact_key') or '').strip()
    body = (request.get('body') or '').strip()
    source = (request.get('source') or '').strip()
    write_id = (request.get('write_id') or '').strip()
    if not fact_key or not body or not source or not write_id:
        raise StateError('invalid', 'fact_key, body, source, write_id must be nonempty')
    value_json = request.get('value_json')
    if value_json is not None:
        value_json = json.dumps(value_json, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    observed_at = validate_timestamp(request.get('observed_at'), 'observed_at')
    expected_version = request.get('expected_version')
    if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
        raise StateError('invalid', 'expected_version must be a nonnegative integer or null')
    phash = payload_hash(fact_key, body, value_json, kind, observed_at)
    key = logical_key(scope_id, source, write_id)
    with closing(write_connect(db_path)) as db:
        require_extra_schema(db)
        try:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM state_candidate WHERE scope_id=? AND source=? AND write_id=?',
                                  (scope_id, source, write_id)).fetchone()
            if existing:
                db.execute('ROLLBACK')
                if existing['payload_hash'] == phash:
                    return {'schema_version': SCHEMA_VERSION, 'status': 'ok', 'idempotent': True,
                            'candidate_id': existing['id'], 'candidate_status': existing['status'],
                            'logical_key': key}
                raise StateError('conflict', 'same logical key with different payload',
                                 candidate_id=existing['id'], logical_key=key)
            cur = db.execute(
                'INSERT INTO state_candidate(scope_id, fact_key, body, value_json, kind, source, write_id, '
                'payload_hash, logical_key, expected_version, observed_at, status, recorded_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (scope_id, fact_key, body, value_json, kind, source, write_id, phash, key,
                 expected_version, observed_at, 'proposed', utc_now()))
            db.execute('COMMIT')
        except sqlite3.OperationalError as exc:
            if db.in_transaction:
                db.execute('ROLLBACK')
            if _busy(exc):
                raise StateError('busy', 'database busy: ' + str(exc))
            raise
        except sqlite3.IntegrityError:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise StateError('conflict', 'concurrent insert for the same logical key', logical_key=key)
    return {'schema_version': SCHEMA_VERSION, 'status': 'ok', 'idempotent': False,
            'candidate_id': cur.lastrowid, 'candidate_status': 'proposed', 'logical_key': key}


# ---------------------------------------------------------------------------
# verify — registered read-only checkers only
# ---------------------------------------------------------------------------

def _allowed_roots() -> List[str]:
    """허용 루트(비교용 정규화 경로). SB_VERIFY_ALLOWED_ROOTS 는 os.pathsep(POSIX ':' · Windows ';') 구분."""
    raw = os.environ.get('SB_VERIFY_ALLOWED_ROOTS')
    roots = [_norm_path(p.strip()) for p in raw.split(os.pathsep) if p.strip()] if raw else []
    if not roots:
        roots = [_norm_path(str(sb_config.home()))]
        for entries in _load_aliases().values():
            roots += [_norm_path(e) for e in entries if is_path_entry(e)]
    return roots


_SPAN = re.compile(r'L?(\d+)(?:-L?(\d+))?')


def split_target(target: str) -> Tuple[str, str]:
    """'<경로>#<조각>' 을 마지막 '#' 에서 나눈다(경로 안의 '#' 과 Windows 드라이브 문자 보존).

    조각이 줄 범위(L1-L3·1-3) 나 JSON 포인터('/...')가 아니면 '#' 을 경로의 일부로 본다.
    """
    head, sep, frag = target.rpartition('#')
    if sep and (frag == '' or frag.startswith('/') or _SPAN.fullmatch(frag)):
        return head, frag
    return target, ''


def _check_path(target: str) -> Tuple[Path, Optional[Tuple[int, int]]]:
    path_part, span = split_target(target)
    path = Path(path_part).expanduser()
    if not path.is_absolute():
        raise StateError('invalid', 'verification target must be an absolute path')
    path = path.resolve()
    norm = os.path.normcase(str(path))
    if not any(path_within(norm, r) for r in _allowed_roots()):
        raise StateError('invalid', 'target outside allowed read-only roots: ' + str(path))
    lines = None
    if span:
        m = _SPAN.fullmatch(span)
        if not m:
            raise StateError('invalid', 'line span must look like L1-L3 or 1-3: ' + span)
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a < 1 or b < a:
            raise StateError('invalid', 'invalid line span: ' + span)
        lines = (a, b)
    return path, lines


def _read_region(path: Path, lines: Optional[Tuple[int, int]], limit: int = 200_000) -> str:
    data = path.read_bytes()[:limit].decode('utf-8', errors='replace')
    if lines:
        rows = data.splitlines()
        data = '\n'.join(rows[lines[0] - 1:lines[1]])
    return data


# 업무 폴더 세션에서 말한 도구 결정(예: "임베딩 서버 자동 시작 켜줘")을 도구 scope 에 남길 때만 켠다.
# 발화 포함 여부 검증은 그대로 하고, 세션 project ≠ scope 불일치만 허용한다(검증 detail 에 cross-scope 표기).
_CROSS_SCOPE = {'on': False}


def _scope_matches_project(project: Optional[str], scope_id: str) -> bool:
    if _CROSS_SCOPE['on'] and project:
        return True
    if not project:
        return False
    if _norm_name(project) == _norm_name(scope_id):
        return True
    for key, entries in _load_aliases().items():
        if _norm_name(key) == _norm_name(scope_id):
            return any(_norm_name(e) == _norm_name(project) for e in entries
                       if isinstance(e, str) and not is_path_entry(e))
    return False


def _expected_text(cand: sqlite3.Row) -> str:
    if cand['value_json'] is not None:
        try:
            v = json.loads(cand['value_json'])
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True,
                                                            separators=(',', ':'))
        except ValueError:
            pass
    return cand['body']


def check_file_contains(cand: sqlite3.Row, target: str) -> Tuple[str, str, str]:
    path, lines = _check_path(target)
    if not path.is_file():
        return 'unknown', '', 'target file missing'
    region = _read_region(path, lines)
    h = hashlib.sha256(region.encode('utf-8')).hexdigest()
    return ('confirmed' if _expected_text(cand) in region else 'contradicted'), h, 'file region compared'


def check_json_file_value(cand: sqlite3.Row, target: str) -> Tuple[str, str, str]:
    """target = /abs/file.json#/json/pointer ; 후보 value_json 과 정규화 비교."""
    path_part, pointer = split_target(target)
    path, _ = _check_path(path_part)
    if not path.is_file():
        return 'unknown', '', 'target file missing'
    try:
        doc = json.loads(path.read_bytes()[:200_000].decode('utf-8'))
    except ValueError:
        return 'unknown', '', 'target is not valid JSON'
    node: Any = doc
    for part in [p for p in pointer.split('/') if p]:
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return 'unknown', '', 'pointer not found'
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return 'unknown', '', 'pointer not found'
    actual = json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    h = hashlib.sha256(actual.encode('utf-8')).hexdigest()
    if cand['value_json'] is None:
        return 'rejected', h, 'candidate has no value_json to compare'
    return ('confirmed' if actual == cand['value_json'] else 'contradicted'), h, 'json value compared'


def _claude_mem_ro() -> sqlite3.Connection:
    target = Path(os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db()).expanduser()
    if not target.exists():
        raise StateError('unavailable', 'claude-mem db missing: ' + str(target))
    db = sqlite3.connect(_ro_uri(target), uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    db.row_factory = sqlite3.Row
    return db


def check_observation_ref(cand: sqlite3.Row, target: str) -> Tuple[str, str, str]:
    if not target.startswith('observation:') or not target[12:].isdigit():
        raise StateError('invalid', 'target must be observation:<id>')
    with closing(_claude_mem_ro()) as db:
        row = db.execute('SELECT id, project, title, narrative, text, facts FROM observations WHERE id=?',
                         (int(target[12:]),)).fetchone()
    if not row:
        return 'unknown', '', 'observation not found'
    if not _scope_matches_project(row['project'], cand['scope_id']):
        return 'rejected', '', 'observation project %r does not belong to scope %r' % (row['project'],
                                                                                        cand['scope_id'])
    content = '\n'.join(str(row[k] or '') for k in ('title', 'narrative', 'text', 'facts'))
    h = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return ('confirmed' if _expected_text(cand) in content else 'contradicted'), h, 'observation compared'


def check_user_utterance(cand: sqlite3.Row, target: str) -> Tuple[str, str, str]:
    """사용자 발화 근거: prompt:<id>(claude-mem user_prompts) 또는 허용 경로의 로컬 기록 구간."""
    if target.startswith('prompt:'):
        if not target[7:].isdigit():
            raise StateError('invalid', 'target must be prompt:<id> or an absolute path#lines')
        with closing(_claude_mem_ro()) as db:
            row = db.execute('SELECT p.prompt_text, s.project FROM user_prompts p '
                             'LEFT JOIN sdk_sessions s ON s.id = p.session_db_id WHERE p.id=?',
                             (int(target[7:]),)).fetchone()
        if not row:
            return 'unknown', '', 'prompt not found'
        # user_prompts 에는 project 가 없다 — 세션 귀속(sdk_sessions.project)으로 scope 를 대조한다.
        if row['project'] is None or not _scope_matches_project(row['project'], cand['scope_id']):
            return 'rejected', '', 'prompt session project %r does not belong to scope' % row['project']
        text = str(row['prompt_text'] or '')
    else:
        path, lines = _check_path(target)
        if not path.is_file():
            return 'unknown', '', 'record file missing'
        text = _read_region(path, lines)
    h = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return ('confirmed' if _expected_text(cand) in text else 'contradicted'), h, 'user utterance compared'


CHECKERS = {'file_contains': check_file_contains, 'json_file_value': check_json_file_value,
            'observation_ref': check_observation_ref, 'user_utterance_check': check_user_utterance}


def verify(candidate_id: int, method: str, target: str, evidence_ref: Optional[str] = None,
           db_path: Optional[str] = None, cross_scope: bool = False) -> Dict[str, Any]:
    _CROSS_SCOPE['on'] = bool(cross_scope)
    try:
        return _verify(candidate_id, method, target, evidence_ref, db_path, cross_scope)
    finally:
        _CROSS_SCOPE['on'] = False


def _verify(candidate_id: int, method: str, target: str, evidence_ref: Optional[str],
            db_path: Optional[str], cross_scope: bool) -> Dict[str, Any]:
    if method not in CHECKERS:
        raise StateError('invalid', 'unregistered verification method %r (allowed: %s)' % (
            method, sorted(CHECKERS)))
    if not target or not target.strip():
        raise StateError('invalid', 'target required')
    with closing(write_connect(db_path)) as db:
        require_extra_schema(db)
        cand = db.execute('SELECT * FROM state_candidate WHERE id=?', (candidate_id,)).fetchone()
        if not cand:
            raise StateError('invalid', 'candidate %s not found' % candidate_id)
        if cand['kind'] == 'inference':
            result, h, detail = 'rejected', '', 'inference candidates cannot be verified into state'
        elif cand['kind'] == 'user_decision' and method not in DECISION_METHODS:
            result, h, detail = 'rejected', '', 'user_decision requires user_utterance_check'
        elif cand['kind'] == 'measured_fact' and method not in MEASUREMENT_METHODS:
            result, h, detail = 'rejected', '', 'measured_fact requires a measurement checker'
        else:
            result, h, detail = CHECKERS[method](cand, target.strip())
            if cross_scope:
                detail = (detail or '') + ' (cross-scope: 발화 세션 project 와 scope 불일치 허용)'
        try:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute(
                'INSERT INTO state_verification(candidate_id, scope_id, method, target, result, checked_at, '
                'evidence_ref, evidence_hash, detail) VALUES (?,?,?,?,?,?,?,?,?)',
                (candidate_id, cand['scope_id'], method, target.strip(), result, utc_now(),
                 evidence_ref or target.strip(), h or None, detail))
            db.execute('COMMIT')
        except sqlite3.OperationalError as exc:
            if db.in_transaction:
                db.execute('ROLLBACK')
            if _busy(exc):
                raise StateError('busy', 'database busy: ' + str(exc))
            raise
    return {'schema_version': SCHEMA_VERSION, 'status': 'ok', 'verification_id': cur.lastrowid,
            'candidate_id': candidate_id, 'result': result, 'method': method, 'detail': detail,
            'evidence_hash': h or None}


# ---------------------------------------------------------------------------
# accept — one BEGIN IMMEDIATE transaction
# ---------------------------------------------------------------------------

def accept(candidate_id: int, expected_version: int, db_path: Optional[str] = None,
           memory_kind: str = 'fact', allow_older: bool = False) -> Dict[str, Any]:
    if type(expected_version) is not int or expected_version < 0:
        raise StateError('invalid', 'expected_version must be a nonnegative integer')
    if memory_kind not in STATE_MEMORY_KINDS:
        raise StateError('invalid', 'invalid memory_kind %r' % memory_kind)
    with closing(write_connect(db_path)) as db:
        require_extra_schema(db)
        try:
            db.execute('BEGIN IMMEDIATE')
            cand = db.execute('SELECT * FROM state_candidate WHERE id=?', (candidate_id,)).fetchone()
            if not cand:
                raise StateError('invalid', 'candidate %s not found' % candidate_id)
            prior = db.execute('SELECT * FROM state_acceptance WHERE candidate_id=?', (candidate_id,)).fetchone()
            if prior:
                st = db.execute('SELECT version FROM state WHERE id=?', (prior['state_id'],)).fetchone()
                db.execute('ROLLBACK')
                return {'schema_version': SCHEMA_VERSION, 'status': 'ok', 'idempotent': True,
                        'candidate_id': candidate_id, 'state_id': prior['state_id'],
                        'version': st['version'] if st else None, 'label': prior['label']}
            if cand['status'] != 'proposed':
                raise StateError('verification_failed', 'candidate status is %r, not proposed' % cand['status'])
            if not state_scope_allowed(cand['scope_id']):
                raise StateError('verification_failed', 'pilot gate: scope %r not in SB_STATE_PILOT_SCOPES %s' % (
                    cand['scope_id'], state_pilot_scopes()))
            if cand['kind'] == 'inference':
                raise StateError('verification_failed', 'inference candidates are never accepted into state')
            methods = DECISION_METHODS if cand['kind'] == 'user_decision' else MEASUREMENT_METHODS
            ver = db.execute(
                'SELECT * FROM state_verification WHERE candidate_id=? AND result=? AND method IN (%s) '
                'ORDER BY id DESC LIMIT 1' % ','.join('?' * len(methods)),
                (candidate_id, 'confirmed', *methods)).fetchone()
            if not ver:
                raise StateError('verification_failed', 'no confirmed verification of an eligible method for candidate',
                                 required_methods=list(methods))
            if cand['expected_version'] is not None and cand['expected_version'] != expected_version:
                raise StateError('invalid', 'expected_version %s disagrees with candidate.expected_version %s' % (
                    expected_version, cand['expected_version']))
            head = db.execute('SELECT id, version, observed_at FROM state WHERE scope_id=? AND fact_key=? AND is_head=1',
                              (cand['scope_id'], cand['fact_key'])).fetchone()
            current = head['version'] if head else 0
            if current != expected_version:
                raise StateError('stale_version', 'head is v%d, expected v%d' % (current, expected_version),
                                 head_version=current)
            # 늦게 저장된 옛 관측은 더 최근 관측 head 를 교체하지 못한다 (시각을 둘 다 알 때만 판정).
            if head and head['observed_at'] and cand['observed_at'] and not allow_older:
                if datetime.fromisoformat(cand['observed_at'].replace('Z', '+00:00')) < \
                        datetime.fromisoformat(head['observed_at'].replace('Z', '+00:00')):
                    raise StateError('verification_failed',
                                     'candidate observed_at %s is older than head observed_at %s '
                                     '(pass --allow-older to override explicitly)' % (
                                         cand['observed_at'], head['observed_at']),
                                     reason='stale_observation')
            if head:
                changed = db.execute('UPDATE state SET is_head=0 WHERE id=? AND scope_id=? AND fact_key=? AND is_head=1',
                                     (head['id'], cand['scope_id'], cand['fact_key'])).rowcount
                if changed != 1:
                    raise StateError('conflict', 'previous head does not belong to target')
            if os.environ.get('SB_STATE_FAIL_BEFORE_INSERT') == '1':  # 원자성 시험용 실패 주입
                raise RuntimeError('injected failure before INSERT')
            cur = db.execute(
                'INSERT INTO state(memory_kind, scope_id, fact_key, version, is_head, observation_ref, observed_at, '
                'recorded_at, body, source, write_id, dedup_key) VALUES (?,?,?,?,1,?,?,?,?,?,?,?)',
                (memory_kind, cand['scope_id'], cand['fact_key'], current + 1, ver['evidence_ref'],
                 cand['observed_at'], utc_now(), cand['body'], cand['source'], cand['write_id'],
                 cand['logical_key']))
            state_id = cur.lastrowid
            if head:
                db.execute('UPDATE state SET superseded_by=? WHERE id=?', (state_id, head['id']))
            label = 'user-confirmed' if cand['kind'] == 'user_decision' else 'operationally measured'
            db.execute('INSERT INTO state_acceptance(candidate_id, state_id, verification_id, label, accepted_at) '
                       'VALUES (?,?,?,?,?)', (candidate_id, state_id, ver['id'], label, utc_now()))
            db.execute("UPDATE state_candidate SET status='accepted' WHERE id=?", (candidate_id,))
            db.execute('COMMIT')
        except StateError:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        except sqlite3.OperationalError as exc:
            if db.in_transaction:
                db.execute('ROLLBACK')
            if _busy(exc):
                raise StateError('busy', 'database busy: ' + str(exc))
            raise
        except Exception:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
    return {'schema_version': SCHEMA_VERSION, 'status': 'ok', 'idempotent': False, 'candidate_id': candidate_id,
            'state_id': state_id, 'version': current + 1, 'superseded': head['id'] if head else None,
            'label': label}


# ---------------------------------------------------------------------------
# read-only queries
# ---------------------------------------------------------------------------

def _envelope(mode: str, scope: str, items: List[Dict[str, Any]], status: str, warnings: List[str],
              omitted: int = 0) -> Dict[str, Any]:
    return {'schema_version': SCHEMA_VERSION, 'query_mode': mode, 'scope': scope, 'status': status,
            'items': items, 'omitted': omitted, 'warnings': warnings}


def _item(row: sqlite3.Row, verification: Dict[str, Any]) -> Dict[str, Any]:
    return {'scope_id': row['scope_id'], 'layer': 'L2', 'item_id': row['id'], 'fact_key': row['fact_key'],
            'version': row['version'], 'is_head': bool(row['is_head']), 'body': row['body'],
            'evidence_ref': row['observation_ref'], 'observed_at': row['observed_at'],
            'recorded_at': row['recorded_at'], 'source': row['source'],
            'verification_status': verification['status'], 'label': verification.get('label'),
            'checked_at': verification.get('checked_at'), 'reason_codes': verification['reason_codes']}


def _verification_for(db: sqlite3.Connection, state_id: int, has_acceptance: bool) -> Dict[str, Any]:
    if not has_acceptance:
        return {'status': 'unverified', 'reason_codes': ['acceptance_table_missing']}
    row = db.execute('SELECT a.label, v.checked_at, v.result FROM state_acceptance a '
                     'JOIN state_verification v ON v.id = a.verification_id WHERE a.state_id=?',
                     (state_id,)).fetchone()
    if not row:
        return {'status': 'unverified', 'reason_codes': ['legacy_head_without_acceptance']}
    return {'status': 'verified', 'label': row['label'], 'checked_at': row['checked_at'],
            'reason_codes': ['accepted_with_%s' % row['result']]}


def query(mode: str, scope_id: str, fact_key: Optional[str] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
    if mode not in ('head', 'history'):
        raise StateError('invalid', 'mode must be head or history')
    scope_id = require_scope(scope_id)
    if mode == 'history' and not fact_key:
        raise StateError('invalid', 'history requires --fact-key')
    try:
        db = ro_connect(db_path)
    except StateError as exc:
        if exc.code == 'unavailable':
            return _envelope(mode, scope_id, [], 'unavailable', ['state_db_missing'])
        raise
    with closing(db):
        try:
            present = tables(db)
            if 'state' not in present:
                return _envelope(mode, scope_id, [], 'unavailable', ['state_table_missing'])
            has_acc = {'state_acceptance', 'state_verification'} <= present
            if mode == 'head':
                sql, args = 'SELECT * FROM state WHERE is_head=1 AND scope_id=?', [scope_id]
                if fact_key:
                    sql += ' AND fact_key=?'; args.append(fact_key)
                rows = db.execute(sql + ' ORDER BY fact_key', args).fetchall()
            else:
                rows = db.execute('SELECT * FROM state WHERE scope_id=? AND fact_key=? ORDER BY version',
                                  (scope_id, fact_key)).fetchall()
            items = [_item(r, _verification_for(db, r['id'], has_acc)) for r in rows]
        except sqlite3.OperationalError as exc:
            status = 'degraded' if _busy(exc) else 'error'
            return _envelope(mode, scope_id, [], status, ['sqlite:' + str(exc)])
    warnings = [] if has_acc else ['acceptance_table_missing: heads shown as unverified']
    return _envelope(mode, scope_id, items, 'ok' if items else 'empty', warnings)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 파이프(cp949 등)에서도 한글·이모지 출력
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(prog='sb_state.py', description=__doc__.splitlines()[0])
    ap.add_argument('--db', help='state db path (default SB_STATE_DB or $SB_HOME/state.db)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('propose'); p.add_argument('--file', required=True)
    v = sub.add_parser('verify'); v.add_argument('--candidate', type=int, required=True)
    v.add_argument('--method', required=True); v.add_argument('--target', required=True); v.add_argument('--evidence-ref')
    v.add_argument('--cross-scope', action='store_true',
                   help='발화·관측이 다른 project 세션에서 나왔어도 허용(업무 세션에서 말한 도구 결정 등)')
    a = sub.add_parser('accept'); a.add_argument('--candidate', type=int, required=True)
    a.add_argument('--expected-version', type=int, required=True); a.add_argument('--memory-kind', default='fact')
    a.add_argument('--allow-older', action='store_true', help='explicitly allow an older observed_at to replace the head')
    for name in ('head', 'history'):
        q = sub.add_parser(name); q.add_argument('--scope', required=True); q.add_argument('--fact-key')
    m = sub.add_parser('migrate'); m.add_argument('--db', dest='mdb', required=True)
    m.add_argument('--allow-operational', action='store_true')
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == 'propose':
            with open(ns.file, encoding='utf-8') as f:
                out = propose(json.load(f), ns.db)
        elif ns.cmd == 'verify':
            out = verify(ns.candidate, ns.method, ns.target, ns.evidence_ref, ns.db, ns.cross_scope)
        elif ns.cmd == 'accept':
            out = accept(ns.candidate, ns.expected_version, ns.db, ns.memory_kind, ns.allow_older)
        elif ns.cmd in ('head', 'history'):
            out = query(ns.cmd, ns.scope, ns.fact_key, ns.db)
        else:
            out = migrate(ns.mdb, ns.allow_operational)
    except StateError as exc:
        print(json.dumps(exc.payload(), ensure_ascii=False))
        return EXIT.get(exc.code, 2)
    except (OSError, ValueError) as exc:
        print(json.dumps({'schema_version': SCHEMA_VERSION, 'status': 'error', 'error': 'invalid',
                          'message': str(exc)}, ensure_ascii=False))
        return EXIT['invalid']
    print(json.dumps(out, ensure_ascii=False))
    if out.get('status') == 'unavailable':
        return EXIT['unavailable']
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
