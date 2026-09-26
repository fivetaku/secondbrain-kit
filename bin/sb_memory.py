"""Provenance-aware claude-mem saves using only the Python standard library."""

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sb_config  # noqa: E402
from sb_scope import require_scope, resolve_scope_id  # noqa: E402  (M2: 단일 scope 해석기)

PROVENANCE_KINDS = ('manual', 'wrap-backfill', 'consolidation', 'automemory-sync', 'import')


def current_scope_id(cwd: Optional[str] = None) -> str:
    """호출자가 project 를 모를 때 쓰는 단일 해석 경로 — 폴더 이름 매칭 없음."""
    return resolve_scope_id(cwd)[0]


def build_provenance(source: str, kind: str, origin: str, created_by: str,
                     sid: Optional[str] = None, source_ids: Optional[List[Any]] = None,
                     content_hash: Optional[str] = None, verification: str = 'unreviewed',
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if kind not in PROVENANCE_KINDS:
        raise ValueError('invalid provenance kind: ' + kind)
    prov = {'source': source, 'kind': kind, 'origin': origin,
            'created_by': created_by, 'sid': sid, 'source_ids': source_ids,
            'content_hash': content_hash, 'verification': verification,
            'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'schema': 'sb-prov-1'}
    if extra is not None:
        prov['extra'] = extra
    return prov


def content_hash(text: str, title: str = '') -> str:
    """Hash an unambiguous UTF-8 JSON pair, without normalizing content."""
    data = json.dumps([text, title], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:16]


def dedup_key(prov: Dict[str, Any], text: str, title: str) -> str:
    return '{}:{}'.format(prov['source'], prov.get('sid') or content_hash(text, title))


def already_saved(key: str, db_path: str = None) -> bool:
    target = Path(db_path or os.environ.get('SB_CLAUDE_MEM_DB') or
                  sb_config.claude_mem_db()).expanduser().resolve()
    if not target.exists():
        return False
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
        return db.execute(
            "SELECT 1 FROM observations WHERE json_extract(metadata,'$.dedup_key') = ? LIMIT 1",
            (key,)).fetchone() is not None


def journal(event: Dict[str, Any], path: str = None) -> None:
    target = Path(path or os.environ.get('SB_MEM_JOURNAL') or
                  sb_config.sb_path('logs', 'memory_journal.jsonl')).expanduser()
    line = json.dumps({'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                       **event}, ensure_ascii=False, allow_nan=False) + '\n'
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('a', encoding='utf-8') as stream:
        stream.write(line)


def _http_post(url: str, payload: Dict[str, Any], timeout: int) -> Tuple[int, str]:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read().decode('utf-8', errors='replace')


_post = _http_post


def save_memory(text: str, title: str, project: str, prov: Dict[str, Any],
                base_url: str = None, db_path: str = None, retries: int = 2) -> int:
    if not text.strip() or not title.strip():
        raise ValueError('text and title must be nonempty')
    if type(retries) is not int or retries < 0:
        raise ValueError('retries must be a nonnegative integer')
    # M2 격리 규약: scope_id(project) 또는 명시적 'global' 없는 쓰기는 거부 — 생략은 에러다.
    project = require_scope(project)
    key = dedup_key(prov, text, title)
    event = {'dedup_key': key, 'project': project}
    if already_saved(key, db_path):
        journal({**event, 'event': 'skipped'})
        return -1
    payload = {'text': text, 'title': title, 'project': project,
               'metadata': {**prov, 'dedup_key': key}}
    # SB_MEM_BASE_URL 이 있으면 worker_base_url() 이 그대로 돌려준다(env 우선).
    url = (base_url or sb_config.worker_base_url()).rstrip('/') + '/api/memory/save'
    for attempt in range(retries + 1):
        try:
            status, body = _post(url, payload, 10)
            result = json.loads(body)
            if (status != 200 or not isinstance(result, dict) or
                    result.get('success') is not True or type(result.get('id')) is not int):
                raise ValueError('invalid memory-save response (HTTP {})'.format(status))
        except (OSError, HTTPException, ValueError) as exc:
            if attempt == retries:
                journal({**event, 'event': 'failed', 'attempts': attempt + 1,
                         'error': str(exc)})
                raise RuntimeError('memory save failed after {} attempts'.format(attempt + 1)) from exc
            time.sleep(0.5 * (2 ** attempt))
        else:
            journal({**event, 'event': 'saved', 'id': result['id'], 'attempts': attempt + 1})
            return result['id']
    raise RuntimeError('unreachable retry state')


# ---------------------------------------------------------------------------
# L2 현재 상태 층 (2026-09-16 memory-layer-refactor M4 파일럿)
#
# claude-mem `observations`(L0 이력)는 건드리지 않는다. 세컨브레인이 소유하는 별도 SQLite 에
# "지금 참인 것"을 scope_id + fact_key 단위의 버전 체인으로 둔다. head 는 항상 1건, 구 버전은 이력.
#  - memory_kind 는 내용 분류(fact/entry_card 등). provenance `kind`(생성 경로)와 분리한다.
#  - observed_at 은 nullable — 모르면 null 로 둔다(created_at/recorded_at 로 채우지 않는다).
#  - 교체는 같은 scope_id + fact_key 의 head 만. 다른 대상의 head 를 닫으려는 호출은 거부.
#  - 쓰기 허용 scope: SB_STATE_PILOT_SCOPES(쉼표 목록)로 제한할 수 있다. 미설정·빈 값·'*' = 전 scope 허용.
#    조회는 제한 없음.
#  - dedup 키 = scope_id + source + write_id (세션 sid 를 여러 건에 재사용하지 않는다).
# ---------------------------------------------------------------------------

STATE_MEMORY_KINDS = ('fact', 'entry_card', 'decision')
_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    is_head INTEGER NOT NULL DEFAULT 1,
    observation_ref TEXT,
    observed_at TEXT,
    recorded_at TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL,
    write_id TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    superseded_by INTEGER,
    UNIQUE(scope_id, fact_key, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS state_head_idx ON state(scope_id, fact_key) WHERE is_head = 1;
CREATE INDEX IF NOT EXISTS state_scope_idx ON state(scope_id, memory_kind, is_head);
"""


def state_db_path(db_path: str = None) -> Path:
    return Path(db_path or os.environ.get('SB_STATE_DB') or sb_config.sb_path('state.db')).expanduser()


def state_pilot_scopes() -> Tuple[str, ...]:
    """명시적으로 제한된 scope 목록. 빈 튜플 = 제한 없음(전 scope 허용). '*' 가 들어 있어도 전 scope 허용."""
    raw = os.environ.get('SB_STATE_PILOT_SCOPES') or ''
    return tuple(s.strip() for s in raw.split(',') if s.strip())


def state_scope_allowed(scope_id: str) -> bool:
    scopes = state_pilot_scopes()
    return not scopes or '*' in scopes or scope_id in scopes


def state_connect(db_path: str = None) -> sqlite3.Connection:
    target = state_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target), isolation_level=None)  # 명시적 BEGIN/COMMIT 로 원자성 제어
    db.row_factory = sqlite3.Row
    db.executescript(_STATE_SCHEMA)
    return db


def state_dedup_key(scope_id: str, source: str, write_id: str) -> str:
    if not (source and source.strip()) or not (write_id and write_id.strip()):
        raise ValueError('source and write_id must be nonempty (do not reuse a session sid across writes)')
    return '{}:{}:{}'.format(scope_id, source.strip(), write_id.strip())


def state_put(scope_id: str, fact_key: str, body: str, memory_kind: str, source: str, write_id: str,
              observation_ref: Optional[str] = None, observed_at: Optional[str] = None,
              expected_version: Optional[int] = None, db_path: str = None) -> Dict[str, Any]:
    """새 버전을 head 로 세우고 직전 head 를 같은 트랜잭션에서 내린다.

    반환: {'id', 'version', 'superseded': 이전 head id 또는 None, 'skipped': dedup 생략 여부}
    거부: scope 없음 / 파일럿 밖 scope / 빈 fact_key·body / 잘못된 memory_kind /
          expected_version 불일치(낙관적 잠금).
    """
    scope_id = require_scope(scope_id)
    if not state_scope_allowed(scope_id):
        raise ValueError('state layer pilot: scope {!r} not in SB_STATE_PILOT_SCOPES {}'.format(
            scope_id, state_pilot_scopes()))
    if memory_kind not in STATE_MEMORY_KINDS:
        raise ValueError('invalid memory_kind: {!r} (allowed: {})'.format(memory_kind, STATE_MEMORY_KINDS))
    if not (fact_key and fact_key.strip()) or not (body and body.strip()):
        raise ValueError('fact_key and body must be nonempty')
    fact_key = fact_key.strip()
    key = state_dedup_key(scope_id, source, write_id)
    recorded_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with closing(state_connect(db_path)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if db.execute('SELECT 1 FROM state WHERE dedup_key = ?', (key,)).fetchone():
                db.execute('ROLLBACK')
                return {'id': -1, 'version': None, 'superseded': None, 'skipped': True}
            head = db.execute('SELECT id, version FROM state WHERE scope_id = ? AND fact_key = ? AND is_head = 1',
                              (scope_id, fact_key)).fetchone()
            current_version = head['version'] if head else 0
            if expected_version is not None and expected_version != current_version:
                raise ValueError('version conflict for {}/{}: head is v{}, expected v{}'.format(
                    scope_id, fact_key, current_version, expected_version))
            if head:
                # 같은 scope_id + fact_key 의 head 만 내린다 — WHERE 절이 그 보증이다.
                # (head 유일 partial index 때문에 새 head insert 보다 먼저 내려야 한다; 같은 트랜잭션이라 원자적)
                changed = db.execute(
                    'UPDATE state SET is_head = 0 WHERE id = ? AND scope_id = ? AND fact_key = ? AND is_head = 1',
                    (head['id'], scope_id, fact_key)).rowcount
                if changed != 1:
                    raise ValueError('refused: previous head {} does not belong to {}/{}'.format(
                        head['id'], scope_id, fact_key))
            cur = db.execute(
                'INSERT INTO state(memory_kind, scope_id, fact_key, version, is_head, observation_ref, observed_at, '
                'recorded_at, body, source, write_id, dedup_key) VALUES (?,?,?,?,1,?,?,?,?,?,?,?)',
                (memory_kind, scope_id, fact_key, current_version + 1, observation_ref, observed_at,
                 recorded_at, body, source.strip(), write_id.strip(), key))
            new_id = cur.lastrowid
            if head:
                db.execute('UPDATE state SET superseded_by = ? WHERE id = ?', (new_id, head['id']))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
    return {'id': new_id, 'version': current_version + 1, 'superseded': head['id'] if head else None,
            'skipped': False}


def _rows(cursor) -> List[Dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


def state_head(scope_id: str, fact_key: Optional[str] = None, memory_kind: Optional[str] = None,
               db_path: str = None) -> List[Dict[str, Any]]:
    """현재 참인 것만(head). 다른 scope 의 head 는 돌려주지 않는다."""
    scope_id = require_scope(scope_id)
    sql, args = 'SELECT * FROM state WHERE is_head = 1 AND scope_id = ?', [scope_id]
    if fact_key:
        sql += ' AND fact_key = ?'; args.append(fact_key)
    if memory_kind:
        sql += ' AND memory_kind = ?'; args.append(memory_kind)
    sql += ' ORDER BY fact_key'
    with closing(state_connect(db_path)) as db:
        return _rows(db.execute(sql, args))


def state_history(scope_id: str, fact_key: str, db_path: str = None) -> List[Dict[str, Any]]:
    """전 버전(교체 체인 포함), 오래된 것부터."""
    scope_id = require_scope(scope_id)
    with closing(state_connect(db_path)) as db:
        return _rows(db.execute('SELECT * FROM state WHERE scope_id = ? AND fact_key = ? ORDER BY version',
                                (scope_id, fact_key)))
