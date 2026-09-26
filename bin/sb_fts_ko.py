#!/usr/bin/env python3
"""Derived Korean FTS index. Run with .venv/bin/python bin/sb_fts_ko.py."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import List, Tuple, Optional, Dict, Any, Iterable, IO

import sb_config
import sb_lock

TOKENIZER_VERSION = 'kiwi-1'
_kiwi = None
_loaded = False


def _tokenizer() -> str:
    """Cache Kiwi availability; an unavailable import keeps using bigram-1."""
    global _kiwi, _loaded
    if not _loaded:
        try:
            from kiwipiepy import Kiwi
        except ImportError:
            _kiwi = None
        else:
            _kiwi = Kiwi()
        _loaded = True
    return TOKENIZER_VERSION if _kiwi is not None else 'bigram-1'


def tokenize_ko(text: str) -> List[str]:
    """Keep content morphemes and lowercase intact ASCII identifiers/paths."""
    identifiers = re.findall(
        r'(?:[A-Za-z]:[\\/]|[~/]|\.{1,2}/)?[A-Za-z0-9_]+'
        r'(?:[._/\\:+-][A-Za-z0-9_]+)*', text)
    tokens = [word.lower() for word in identifiers]
    _tokenizer()
    if _kiwi is None:
        for word in re.findall(r'[가-힣]+', text):
            tokens.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
    else:
        tokens.extend(token.form.lower() for token in _kiwi.tokenize(text)
                      if token.tag.startswith('NN')
                      or token.tag in {'VV', 'VA', 'SL', 'SN', 'SH'})
    return tokens


def ko_query_tokens(text: str) -> List[str]:
    """Deduplicate query terms while preserving their order."""
    return list(dict.fromkeys(tokenize_ko(text)))


def _path(index_path: Optional[str] = None) -> Path:
    return Path(index_path or os.environ.get('SB_KO_INDEX')
                or sb_config.sb_path('index', 'ko_fts.sqlite')).expanduser().resolve()


def _source_path(db_path: Optional[str] = None) -> Path:
    return Path(db_path or os.environ.get('SB_CLAUDE_MEM_DB')
                or sb_config.claude_mem_db()).expanduser().resolve()


def open_index(index_path: str = None, create: bool = True) -> "sqlite3.Connection":
    """Open a derived index; inspection mode never creates or writes it."""
    path = _path(index_path)
    source = _source_path()
    if path == source or (path.exists() and source.exists() and path.samefile(source)):
        raise ValueError('Source and index must be different files')
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path.as_uri() + ('?mode=rwc' if create else '?mode=ro'), uri=True)
    if create:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS ko_docs (obs_id INTEGER PRIMARY KEY, project TEXT, content_hash TEXT, tokenizer TEXT, indexed_at INTEGER)')
            conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS ko_fts USING fts5(obs_id UNINDEXED, project UNINDEXED, body)')
            conn.execute('CREATE TABLE IF NOT EXISTS ko_meta (key TEXT PRIMARY KEY, value TEXT)')
    return conn


def _body(row: sqlite3.Row) -> str:
    parts = [row[key] or '' for key in ('title', 'subtitle', 'narrative', 'text')]
    for key in ('facts', 'concepts'):
        raw = row[key]
        if raw:
            try:
                values = json.loads(raw)
            except (ValueError, TypeError):
                values = [raw]
            if isinstance(values, list):
                parts.extend(value for value in values if isinstance(value, str))
    return ' '.join(tokenize_ko('\n'.join(parts)))


def _remove(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    for obs_id in ids:
        conn.execute('DELETE FROM ko_fts WHERE rowid = ?', (obs_id,))
        conn.execute('DELETE FROM ko_docs WHERE obs_id = ?', (obs_id,))


def _flock(stream: IO[str], operation: int, timeout: float = 10.0) -> None:
    """Bound lock contention; callers close the file to release it.

    operation uses sb_lock constants (LOCK_SH/LOCK_EX). On Windows SH is EX.
    """
    try:
        sb_lock.lock_with_timeout(stream, operation, timeout)
    except TimeoutError:
        raise TimeoutError('freshness lock timed out: ' + str(stream.name)) from None


def _synchronize(index_path: Optional[str], db_path: Optional[str], full: bool,
                 batch: int, check_source: bool) -> Dict[str, Any]:
    start = time.monotonic()
    if batch <= 0:
        raise ValueError('batch must be positive')
    source = _source_path(db_path)
    target = _path(index_path)
    if source == target or (source.exists() and target.exists() and source.samefile(target)):
        raise ValueError('Source and index must be different files')
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / (target.name + '.lock')).open('a') as lock:
        _flock(lock, sb_lock.LOCK_EX)
        # Read the source only after acquiring the lock, including on cache hits.
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, \
                closing(open_index(str(target))) as dst:
            return _build_snapshot(src, dst, source, full, batch, check_source, start)


def _build_snapshot(src: sqlite3.Connection, dst: sqlite3.Connection, source: Path,
                    full: bool, batch: int, check_source: bool, start: float) -> Dict[str, Any]:
    tokenizer = _tokenizer()
    indexed = skipped = removed = max_id = 0
    src.row_factory = sqlite3.Row
    src.execute('BEGIN')
    meta = dict(dst.execute('SELECT key, value FROM ko_meta'))
    if check_source and meta.get('source_path', str(source)) != str(source):
        raise ValueError('Korean index belongs to a different source')
    previous = {row[0]: (row[1], row[2]) for row in dst.execute('SELECT obs_id, content_hash, tokenizer FROM ko_docs')}
    reset = full or meta.get('tokenizer') != tokenizer or meta.get('source_path') != str(source)
    # One derived transaction: failed or concurrent refreshes never expose a
    # partially refreshed index. batch controls fetch size, not publication.
    with dst:
        if reset:
            dst.execute('DELETE FROM ko_fts')
            dst.execute('DELETE FROM ko_docs')
            dst.execute('DELETE FROM ko_meta')
            previous = {}
        cursor = src.execute('SELECT id, project, title, subtitle, narrative, text, facts, concepts FROM observations ORDER BY id')
        while rows := cursor.fetchmany(batch):
            for row in rows:
                obs_id = row['id']
                max_id = max(max_id, obs_id)
                # Source hashes can be null or omit project/subtitle/facts. Hash
                # actual indexed fields instead; unchanged rows need no Kiwi work.
                digest = hashlib.sha256(json.dumps(tuple(row), ensure_ascii=False).encode()).hexdigest()
                if previous.pop(obs_id, None) == (digest, tokenizer):
                    skipped += 1
                    continue
                body = _body(row)
                _remove(dst, [obs_id])
                dst.execute('INSERT INTO ko_docs VALUES (?, ?, ?, ?, ?)',
                            (obs_id, row['project'], digest, tokenizer, int(time.time())))
                dst.execute('INSERT INTO ko_fts(rowid, obs_id, project, body) VALUES (?, ?, ?, ?)',
                            (obs_id, obs_id, row['project'], body))
                indexed += 1
        removed = len(previous)
        _remove(dst, previous)
        if reset or indexed or removed:
            dst.executemany('INSERT OR REPLACE INTO ko_meta VALUES (?, ?)',
                            [('max_id', str(max_id)), ('tokenizer', tokenizer),
                             ('source_path', str(source)), ('last_build', str(int(time.time())))])
    return {'indexed': indexed, 'skipped': skipped, 'removed': removed,
            'max_id': max_id, 'took_ms': round((time.monotonic() - start) * 1000)}


def build(index_path: str = None, db_path: str = None, full: bool = False, batch: int = 500) -> Dict[str, Any]:
    """Synchronize a read-only source snapshot, serializing with integrated search."""
    return _synchronize(index_path, db_path, full, batch, False)


def ensure_current(index_path: Optional[str] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Verify every indexed field at the first source read, without a TTL.

    The ledger has no durable change sequence. A read-only scan is deliberate:
    neither max(id), nullable producer hashes nor a new connection's data_version
    can detect all updates/deletes. Only changed rows are retokenized/written.
    Standalone search_ko/status remain pure read-only inspection operations.
    """
    return _synchronize(index_path, db_path, False, 500, True)


def search_ko(query: str, limit: int, projects: Optional[List[str]] = None, index_path: str = None) -> List[Tuple[int, float]]:
    """Return observation IDs and ascending FTS5 bm25 scores."""
    if limit <= 0 or not _path(index_path).exists():
        return []
    tokens = ko_query_tokens(query)
    if not tokens:
        return []
    match = ' OR '.join('"' + token.replace('"', '""') + '"' for token in tokens)
    params = [match]
    sql = 'SELECT obs_id, bm25(ko_fts) FROM ko_fts WHERE ko_fts MATCH ?'
    if projects:
        sql += ' AND project IN (' + ','.join('?' for _ in projects) + ')'
        params.extend(projects)
    sql += ' ORDER BY bm25(ko_fts), obs_id LIMIT ?'
    with closing(open_index(index_path, create=False)) as conn:
        return [(int(row[0]), float(row[1])) for row in conn.execute(sql, [*params, limit])]


def status(index_path: str = None) -> Dict[str, Any]:
    """Inspect index metadata without creating a missing database."""
    if not _path(index_path).exists():
        return {'exists': False, 'documents': 0, 'max_id': 0, 'tokenizer': None, 'last_build': None}
    with closing(open_index(index_path, create=False)) as conn:
        meta = dict(conn.execute('SELECT key, value FROM ko_meta'))
        return {'exists': True, 'documents': conn.execute('SELECT count(*) FROM ko_docs').fetchone()[0],
                'max_id': int(meta.get('max_id', 0)), 'tokenizer': meta.get('tokenizer'),
                'last_build': int(meta['last_build']) if 'last_build' in meta else None}


def main(argv: List[str] = None) -> int:
    """Expose build, status, search and tokens commands as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    builder = commands.add_parser('build')
    builder.add_argument('--full', action='store_true')
    commands.add_parser('status')
    search = commands.add_parser('search')
    search.add_argument('query')
    search.add_argument('--limit', type=int, default=20)
    search.add_argument('--projects')
    commands.add_parser('tokens').add_argument('text')
    args = parser.parse_args(argv)
    try:
        if args.command == 'build':
            result = build(full=args.full)
        elif args.command == 'status':
            result = status()
        elif args.command == 'search':
            result = search_ko(args.query, args.limit, [p for p in (args.projects or '').split(',') if p])
        else:
            result = tokenize_ko(args.text)
        print(json.dumps(result, ensure_ascii=False))
    except (sqlite3.Error, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
