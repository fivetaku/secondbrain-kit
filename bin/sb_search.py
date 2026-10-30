#!/usr/bin/env python3
"""Read-only observation search. Run with .venv/bin/python bin/sb_search.py QUERY."""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Iterator

import sb_config
import sb_lock


@dataclass
class Scope:
    mode: str
    projects: List[str]


@dataclass
class Hit:
    obs_id: int
    score: float
    source: str


def _path(value: Optional[str], variable: str, default: str) -> Path:
    return Path(value or os.environ.get(variable) or default).expanduser().resolve()


def _db_path(value: Optional[str]) -> Path:
    return _path(value, 'SB_CLAUDE_MEM_DB', sb_config.claude_mem_db())


def _flock(stream: Any, operation: int, timeout: float = 10.0) -> None:
    """Bounded lock wait (sb_lock constants); closing the file releases it."""
    try:
        sb_lock.lock_with_timeout(stream, operation, timeout)
    except TimeoutError:
        raise TimeoutError('freshness lock timed out: ' + str(stream.name)) from None


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.3)
    connection.row_factory = sqlite3.Row
    return connection


def _projects(scope: Scope) -> List[str]:
    if scope.mode not in ('project', 'global', 'multi'):
        raise ValueError('scope.mode must be project, global, or multi')
    if scope.mode == 'global':
        return []
    projects = list(dict.fromkeys(scope.projects))
    if not projects or any(not project.strip() for project in projects):
        raise ValueError('project scope requires nonempty projects')
    if scope.mode == 'project' and len(projects) != 1:
        raise ValueError('project scope requires exactly one project')
    return projects


def fts_search(query: str, scope: Scope, limit: int, db_path: str = None) -> List[Hit]:
    """Quote literal tokens, OR them, and rank distinct observations with BM25."""
    projects = _projects(scope)
    if limit <= 0 or not query.strip():
        return []
    match = ' OR '.join('"' + token.replace('"', '""') + '"' for token in query.split())
    sql = ('SELECT o.id, bm25(observations_fts) AS score FROM observations_fts '
           'JOIN observations o ON o.id=observations_fts.rowid WHERE observations_fts MATCH ?')
    params = [match]
    if projects:
        sql += ' AND o.project IN (' + ','.join('?' for _ in projects) + ')'
        params.extend(projects)
    sql += ' ORDER BY score, o.id LIMIT ?'
    with closing(_connect(_db_path(db_path))) as connection:
        return [Hit(row['id'], -row['score'], 'fts')
                for row in connection.execute(sql, [*params, limit])]


_snapshot_options: ContextVar[Tuple[bool, bool]] = ContextVar('snapshot_options', default=(False, False))
_snapshot_info: ContextVar[Optional[Dict[str, Any]]] = ContextVar('snapshot_info', default=None)


def _manifest_entries(snapshot: Path, meta: Dict[str, Any]) -> Optional[List[Tuple[Path, int, int]]]:
    """snapshot_meta.json 의 files 목록을 (경로, 크기, mtime_ns) 로 푼다. 형식이 어긋나면 None."""
    files = meta.get('files')
    if not isinstance(files, dict) or 'chroma.sqlite3' not in files:
        return None
    entries = []
    for relative, value in files.items():
        if not isinstance(relative, str):
            return None
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or '..' in parts:
            return None
        if (not isinstance(value, list) or len(value) != 2
                or any(isinstance(number, bool) or not isinstance(number, int) for number in value)):
            return None
        entries.append((snapshot / relative, value[0], value[1]))
    return entries


def _file_manifest(snapshot: Path) -> Dict[str, List[int]]:
    """chroma.sqlite3 와 벡터 세그먼트 디렉터리 안 모든 파일의 [크기, mtime_ns].
    키는 '/' 구분 상대경로로 남겨 OS 가 달라도 같은 모양이 되게 한다."""
    files = {}
    for item in [snapshot / 'chroma.sqlite3'] + [path for directory in snapshot.iterdir() if directory.is_dir()
                                                 for path in directory.rglob('*') if path.is_file()]:
        stat = item.stat()
        files[item.relative_to(snapshot).as_posix()] = [stat.st_size, stat.st_mtime_ns]
    return files


def _restore_manifest_times(snapshot: Path) -> None:
    """Chroma 는 조회만 해도 chroma.sqlite3 를 제자리에서 다시 써 mtime 을 바꾼다(크기 동일, 2026-10-14 실측).
    우리 조회가 남긴 흔적이 다음 재사용 검증을 깨지 않도록, 크기가 같은 파일만 기록된 mtime 으로 되돌린다.
    크기가 달라졌으면 그대로 둬서 다음 검증이 재생성하게 한다."""
    try:
        meta = json.loads((snapshot / 'snapshot_meta.json').read_text(encoding='utf-8'))
        entries = _manifest_entries(snapshot, meta) if isinstance(meta, dict) else None
        for path, size, mtime_ns in entries or []:
            stat = path.stat()
            if stat.st_size == size and stat.st_mtime_ns != mtime_ns:
                os.utime(path, ns=(stat.st_atime_ns, mtime_ns))
    except (OSError, ValueError):
        pass  # 복원 실패(윈도우에서 다른 프로세스가 연 파일 등)는 다음 조회의 재생성 한 번으로 끝난다.


def _clone_tree(source: Path, destination: Path) -> None:
    """세그먼트 디렉터리 복제. darwin 은 APFS clonefile(cp -c) 로 즉시 끝내고,
    실패하거나(APFS 아님 등) 다른 OS 면 일반 복사로 내려간다."""
    if sys.platform == 'darwin':
        try:
            subprocess.run(['cp', '-c', '-R', str(source), str(destination)], check=True,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except (OSError, subprocess.SubprocessError):
            shutil.rmtree(destination, ignore_errors=True)  # 반쯤 만들어진 복제본을 치우고 일반 복사로.
    shutil.copytree(source, destination)


def _snapshot_metadata(snapshot: Path, source: Path) -> Optional[Dict[str, Any]]:
    """Reject incomplete metadata and changed snapshot files without copying.

    수 GB 파일의 PRAGMA quick_check(실측 8~21초) 대신, 생성 때 기록한 files 목록의
    크기·mtime_ns 가 모두 그대로인지만 본다. files 가 없는 옛 메타는 None(한 번 재생성).
    """
    try:
        meta = json.loads((snapshot / 'snapshot_meta.json').read_text(encoding='utf-8'))
        if not isinstance(meta, dict) or meta.get('source_path') != str(source):
            return None
        for key in ('created_at_epoch', 'source_mtime', 'bytes'):
            value = meta.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                return None
        if meta['created_at_epoch'] > time.time():
            return None
        entries = _manifest_entries(snapshot, meta)
        if entries is None:
            return None
        for path, size, mtime_ns in entries:
            stat = path.stat()  # 없으면 OSError → None
            if stat.st_size != size or stat.st_mtime_ns != mtime_ns:
                return None
        with closing(_connect(snapshot / 'chroma.sqlite3')) as connection:
            # Chroma persists each vector segment in a directory bearing its id.
            segments = connection.execute("SELECT id FROM segments WHERE scope='VECTOR'").fetchall()
            if any(not (snapshot / row[0]).is_dir() for row in segments):
                return None
        return meta
    except (OSError, ValueError, OverflowError, sqlite3.Error):
        return None


def _chroma_generation(source: sqlite3.Connection, path: Path) -> str:
    """Committed log + per-segment watermarks, independent of ledger saves.

    Read in the same SQLite transaction as the backup. Queue max alone loses
    information when Chroma drains its log; metadata and vector segment offsets
    differ, so retain every watermark. Checkpoints do not change this token.
    Small catalog tables cover collection deletion/recreation/config changes.
    """
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    stat = (path / 'chroma.sqlite3').stat()
    generation: Dict[str, Any] = {'file': [stat.st_dev, stat.st_ino]}
    for table in ('collections', 'collection_metadata', 'segments', 'segment_metadata',
                  'tenants', 'databases', 'max_seq_id'):
        if table in tables:
            order = '1' if table == 'tenants' else '1, 2'
            generation[table] = [list(row) for row in source.execute('SELECT * FROM ' + table + ' ORDER BY ' + order)]
    if 'embeddings_queue' in tables:
        generation['queue'] = source.execute('SELECT MAX(seq_id) FROM embeddings_queue').fetchone()[0]
    encoded = json.dumps(generation, sort_keys=True, default=lambda value: value.hex())
    return hashlib.sha256(encoded.encode()).hexdigest()


@contextmanager
def _vector_snapshot(path: Path) -> Iterator[str]:
    """Cache SQLite backups and segment copies; pin readers through client.close().

    Acquire refresh before readers, and recheck source state after acquisition.
    Never serve stale data on contention. Reader pinning lasts through close().
    External HNSW writes remain outside SQLite's read transaction.
    """
    fresh, direct = _snapshot_options.get()
    if not (path / 'chroma.sqlite3').is_file():
        raise FileNotFoundError('Chroma index does not exist: ' + str(path))
    if direct:
        _snapshot_info.set({'reused': False, 'age_s': 0.0, 'path': str(path)})
        yield str(path)
        return
    snapshot = _path(None, 'SB_CHROMA_SNAPSHOT', sb_config.sb_path('index', 'chroma-snapshot'))
    if snapshot == path or path in snapshot.parents or snapshot in path.parents:
        raise ValueError('snapshot and source paths must not overlap')
    ttl = float(os.environ.get('SB_CHROMA_SNAPSHOT_TTL', '600'))
    if not math.isfinite(ttl) or ttl < 0:
        raise ValueError('snapshot TTL must be finite and nonnegative')
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    with (snapshot.parent / (snapshot.name + '.lock')).open('a') as lock, \
            (snapshot.parent / (snapshot.name + '.readers.lock')).open('a') as readers:
        _flock(lock, sb_lock.LOCK_EX)
        _flock(readers, sb_lock.LOCK_SH)
        meta = _snapshot_metadata(snapshot, path)
        with closing(_connect(path / 'chroma.sqlite3')) as source:
            source.execute('BEGIN')
            generation = _chroma_generation(source, path)
            if (meta is not None and meta.get('source_generation') == generation
                    and not fresh and time.time() - meta['created_at_epoch'] < ttl):
                source.rollback()
                _snapshot_info.set({'reused': True, 'age_s': max(0.0, time.time() - meta['created_at_epoch']),
                                    'path': str(snapshot)})
                yield str(snapshot)
                return
            sb_lock.lock(readers, sb_lock.LOCK_UN)
            # mkdtemp + rmtree(ignore_errors): on Windows a leftover handle must not
            # turn a successful publication into a cleanup exception.
            temporary = Path(tempfile.mkdtemp(prefix=snapshot.name + '-', dir=snapshot.parent))
            try:
                staging = temporary / 'new'
                staging.mkdir()
                with closing(sqlite3.connect(str(staging / 'chroma.sqlite3'))) as target:
                    source.backup(target)
                for item in path.iterdir():
                    if item.is_dir():
                        _clone_tree(item, staging / item.name)
                source.rollback()
                created = time.time()
                files = _file_manifest(staging)
                meta = dict(created_at_epoch=created, source_path=str(path),
                            source_generation=generation, source_mtime=path.stat().st_mtime,
                            bytes=sum(item.stat().st_size for item in staging.rglob('*') if item.is_file()),
                            files=files)
                (staging / 'snapshot_meta.json').write_text(json.dumps(meta), encoding='utf-8')
                _flock(readers, sb_lock.LOCK_EX)
                # Windows os.replace cannot overwrite a non-empty directory, so the
                # old generation is always moved aside first, then staging published.
                previous = temporary / 'old'
                if snapshot.exists():
                    os.replace(snapshot, previous)
                try:
                    os.replace(staging, snapshot)
                except OSError:
                    if previous.exists():
                        os.replace(previous, snapshot)
                    raise
            finally:
                # Delete the previous generation only after publication, with readers excluded.
                shutil.rmtree(temporary, ignore_errors=True)
        # Unlock before re-locking: msvcrt cannot downgrade EX->SH in place, and on
        # POSIX flock conversion is not atomic anyway; the refresh lock is still held.
        sb_lock.lock(readers, sb_lock.LOCK_UN)
        _flock(readers, sb_lock.LOCK_SH)
        _snapshot_info.set({'reused': False, 'age_s': max(0.0, time.time() - created),
                            'path': str(snapshot)})
        yield str(snapshot)


def vector_search(query: str, scope: Scope, limit: int, chroma_path: str = None) -> Tuple[List[Hit], Dict[str, Any]]:
    """Query a snapshot and give each observation only its best chunk score."""
    _snapshot_info.set(None)
    projects = _projects(scope)
    if limit <= 0 or not query.strip():
        return [], {'ok': True, 'error': None, 'chunks': 0}
    path = _path(chroma_path, 'SB_CHROMA_PATH', str(sb_config.claude_mem_dir() / 'chroma'))
    where = None if not projects else ({'project': projects[0]} if len(projects) == 1
                                      else {'project': {'$in': projects}})
    if _snapshot_options.get()[1]:
        print('warning: 원본 Chroma 직접 조회는 워커와 동시 접근 위험이 있습니다.', file=sys.stderr)
    for attempt in range(4):  # Initial attempt plus three short lock retries.
        try:
            # kit 설치의 컬렉션은 OpenAI 호환 EF(→ Ollama /v1)로 저장된다. 키 값은 Ollama 가 무시한다.
            os.environ.setdefault('CHROMA_OPENAI_API_KEY', 'ollama')
            import chromadb
            from chromadb.config import Settings
            import importlib
            importlib.import_module('sb_embedding').register()
            with _vector_snapshot(path) as snapshot:
                snapshot_stat = Path(snapshot).stat() if not _snapshot_options.get()[1] else None
                client = chromadb.PersistentClient(path=snapshot, settings=Settings(anonymized_telemetry=False))
                try:
                    collection = client.get_collection('cm__claude-mem')
                    # 관측이 아닌 청크(user_prompt 등, 실측 ~12%)는 아래 사후 필터로 빠지므로 그 몫만큼 후보를 더 받는다.
                    # sb_recalld.Vectors.search 와 같은 배수(9)로 맞춘다.
                    response = collection.query(query_texts=[query], n_results=limit * 9,
                                                where=where, include=['metadatas', 'distances'])
                finally:
                    # Release native handles before releasing the snapshot reader lock.
                    client.close()
                    if snapshot_stat is not None:
                        _restore_manifest_times(Path(snapshot))
                        # Chroma creates/removes journals even for a query; retain cache directory time.
                        os.utime(snapshot, ns=(snapshot_stat.st_atime_ns, snapshot_stat.st_mtime_ns))
            metadatas = (response.get('metadatas') or [[]])[0]
            distances = (response.get('distances') or [[]])[0]
            scores = {}
            for metadata, distance in zip(metadatas, distances):
                if not metadata or metadata.get('doc_type') != 'observation':
                    continue
                obs_id = metadata.get('sqlite_id')
                if isinstance(obs_id, bool) or not isinstance(obs_id, int) or distance is None:
                    continue
                score = 1.0 - float(distance)
                if math.isfinite(score):
                    scores[obs_id] = max(scores.get(obs_id, -math.inf), score)
            ranked = sorted(scores, key=lambda obs_id: (-scores[obs_id], obs_id))[:limit]
            return [Hit(obs_id, scores[obs_id], 'vector') for obs_id in ranked], {
                'ok': True, 'error': None, 'chunks': len(metadatas), 'snapshot': _snapshot_info.get()}
        except Exception as exc:  # Optional backend boundary: retain lexical results for any backend failure.
            if 'locked' in str(exc).lower() and attempt < 3:
                time.sleep(0.3)
                continue
            return [], {'ok': False, 'error': str(exc), 'chunks': 0, 'snapshot': _snapshot_info.get()}
    raise RuntimeError('unreachable retry state')


def rrf(rank_lists: List[List[int]], k: int = 60) -> Dict[int, float]:
    if k < 0:
        raise ValueError('k must be nonnegative')
    scores = {}
    for ranking in rank_lists:
        for rank, obs_id in enumerate(dict.fromkeys(ranking), 1):
            scores[obs_id] = scores.get(obs_id, 0.0) + 1.0 / (k + rank)
    return scores


def time_decay(score: float, created_at_epoch_ms: int, now_ms: int, rate_per_day: float) -> float:
    if not math.isfinite(rate_per_day) or rate_per_day < 0:
        raise ValueError('decay rate must be finite and nonnegative')
    return score * math.exp(-rate_per_day * max(0, now_ms - created_at_epoch_ms) / 86_400_000)


def rerank(query: str, candidates: List[Dict[str, Any]], top_n: int = 20) -> List[Hit]:
    if not candidates or top_n <= 0:
        return []
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    import numpy as np
    model = _reranker()
    pairs = [(query, '\n'.join(str(item.get(key) or '') for key in ('title', 'subtitle', 'narrative')))
             for item in candidates]
    scores = np.asarray(model.predict(pairs, batch_size=16)).reshape(-1)
    if len(scores) != len(candidates) or not np.isfinite(scores).all():
        raise ValueError('reranker returned invalid scores')
    # 재순위 순위와 들어온(융합) 순위를 RRF k=10 으로 섞는다(sb_recalld.Reranker.order 와 같은 식). 재순위 점수만 쓰면
    # 정확한 복합명사 질의에서 융합 순위보다 떨어져(2026-10-20 실측 MRR .576→.520), 섞은 쪽이 질의 세트 모두에서 기준 이상이었다.
    rerank_rank = np.empty(len(scores), dtype=int)
    rerank_rank[np.argsort(-scores, kind='stable')] = np.arange(1, len(scores) + 1)
    fused_rank = np.arange(1, len(scores) + 1)  # candidates arrive in fused order
    blended = 1 / (10 + fused_rank) + 1 / (10 + rerank_rank)
    order = np.argsort(-blended, kind='stable')[:top_n]
    return [Hit(int(candidates[int(index)].get('obs_id', candidates[int(index)].get('id'))),
                float(blended[index]), 'rerank') for index in order]


# 한국어 재순위기. sb_recalld.RERANK_MODEL 과 같은 모델·같은 환경변수로 맞춘다(상주 회수와 CLI 순위가 갈라지지 않게).
# 2026-10-20 실사용 프롬프트 200개 비교 MRR@10: 재순위 없음 .493, bge-reranker-base .462(중·영 전용),
# bge-reranker-v2-m3 .557, KURE-Reranker-nano .581(149M, 질의당 약 0.25초).
RERANK_MODEL = os.environ.get('SB_RERANK_MODEL', 'nlpai-lab/KURE-Reranker-nano')
_RERANKER = None


def _reranker():
    """한 프로세스에서 한 번만 적재한다. 모델은 미리 내려받아 둔 로컬 캐시에서만 읽는다(오프라인)."""
    global _RERANKER
    if _RERANKER is None:
        from sentence_transformers import CrossEncoder
        import torch
        device = 'mps' if torch.backends.mps.is_available() else 'cpu'
        _RERANKER = CrossEncoder(RERANK_MODEL, device=device, max_length=512, local_files_only=True)
    return _RERANKER


def hydrate(obs_ids: List[int], db_path: str = None) -> Dict[int, Dict[str, Any]]:
    if not obs_ids:
        return {}
    result = {}
    ids = list(dict.fromkeys(obs_ids))
    with closing(_connect(_db_path(db_path))) as connection:
        # title_norm_key 는 claude-mem 이 채우는 열이라 옛 원장·테스트 원장에는 없을 수 있다.
        columns = {row[1] for row in connection.execute('PRAGMA table_info(observations)')}
        norm = 'title_norm_key' if 'title_norm_key' in columns else 'NULL AS title_norm_key'
        for start in range(0, len(ids), 900):
            batch = ids[start:start + 900]
            sql = ('SELECT id, project, type, title, subtitle, narrative, created_at, '
                   'created_at_epoch, metadata, ' + norm + ' FROM observations WHERE id IN ('
                   + ','.join('?' for _ in batch) + ')')
            for row in connection.execute(sql, batch):
                item = dict(row)
                try:
                    metadata = json.loads(item['metadata'] or '{}')
                except (ValueError, TypeError):
                    metadata = {}
                item['metadata'] = metadata if isinstance(metadata, dict) else {}
                result[item['id']] = item
    return result


def _provenance(item: Dict[str, Any], key: str) -> Any:
    metadata = item['metadata']
    nested = metadata.get('prov')
    return metadata.get(key) or (nested.get(key) if isinstance(nested, dict) else None)


def _source_ids(item: Dict[str, Any]) -> List[int]:
    values = _provenance(item, 'source_ids')
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(int(value) for value in values
                              if not isinstance(value, bool) and str(value).isdigit()))


def _epoch(item: Dict[str, Any]) -> Optional[int]:
    try:
        if item.get('created_at'):
            date = datetime.fromisoformat(item['created_at'].replace('Z', '+00:00'))
            if date.tzinfo is not None:
                return int(date.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        pass  # Malformed ISO timestamps can still have a valid epoch column.
    try:
        value = item.get('created_at_epoch')
        return int(value) if value is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


# 근사 중복 접기: 띄어쓰기·문장부호·끝말("기록됨"/"기록함")만 다른 제목을 하나로 본다.
# 같은 일을 표현만 바꿔 여러 번 기록한 관측이 상위를 채워 다른 결과를 밀어내는 것을 막는다.
_TITLE_ENDINGS = sorted(('됨', '함', '했다', '한다', '됐다', '된다', '했음', '하였다', '다'), key=len, reverse=True)
_TITLE_NOISE = re.compile(r'[\W_]+')


def normalize_title(title: Optional[str]) -> str:
    """소문자화 → 공백·문장부호 제거 → 끝말 하나만 떼기. 빈 결과면 '' (접지 않는다)."""
    text = _TITLE_NOISE.sub('', (title or '').lower())
    for ending in _TITLE_ENDINGS:
        if text.endswith(ending) and len(text) > len(ending):
            return text[:-len(ending)]
    return text


def fold_duplicates(ordered: List[int], items: Dict[int, Dict[str, Any]]) -> List[Tuple[int, int, int]]:
    """순위 목록을 중복 묶음으로 접는다. 반환: (대표 id, 묶음 첫 순위 id, 접힌 수).

    정규화 제목이 같거나 title_norm_key 가 같으면 같은 묶음. 묶음은 가장 앞 순위 자리에
    남고, 대표는 묶음에서 가장 최근(created_at) 관측이다.
    """
    groups: List[List[int]] = []
    owner: Dict[Tuple[str, str], int] = {}
    for obs_id in ordered:
        item = items[obs_id]
        keys = [('title', normalize_title(item.get('title')))]
        if item.get('title_norm_key'):
            keys.append(('norm', str(item['title_norm_key'])))
        keys = [key for key in keys if key[1]]
        index = next((owner[key] for key in keys if key in owner), None)
        if index is None:
            index = len(groups)
            groups.append([])
        groups[index].append(obs_id)
        for key in keys:
            owner.setdefault(key, index)
    folded = []
    for members in groups:
        newest = max(members, key=lambda obs_id: (_epoch(items[obs_id]) or 0, obs_id))
        folded.append((newest, members[0], len(members) - 1))
    return folded


def _lexical(query: str, scope: Scope, limit: int, db_path: Optional[str]) -> Tuple[List[Hit], str]:
    # A custom ledger needs its own explicit lexical path, even when selected
    # through the environment. Never create/refresh the default production index.
    source = _db_path(db_path)
    default_source = Path(sb_config.claude_mem_db()).expanduser().resolve()
    index = _path(None, 'SB_KO_INDEX', sb_config.sb_path('index', 'ko_fts.sqlite'))
    if source == _db_path(None) and (source == default_source or os.environ.get('SB_KO_INDEX')):
        try:
            from sb_fts_ko import ensure_current, search_ko
        except ImportError:
            # Optional backend absence retains the existing FTS5-only contract.
            return fts_search(query, scope, limit, db_path), 'fts5'
        try:
            ensure_current(index_path=str(index), db_path=str(source))
            pairs = search_ko(query, limit, projects=_projects(scope) or None, index_path=str(index))
            return [Hit(obs_id, score, 'fts') for obs_id, score in dict(pairs).items()], 'ko'
        except (OSError, sqlite3.Error, ValueError) as exc:
            print('Korean index unavailable: ' + str(exc), file=sys.stderr)
    return fts_search(query, scope, limit, db_path), 'fts5'


# 근사 중복 접기 재수집의 후보 수 상한. 예전엔 4배씩 두 번 늘려 벡터 n_results 가 limit×432(limit 20 이면 8,640 청크)까지
# 커지고 재순위도 회차마다 다시 돌았다. 첫 수집(limit×3)이 이보다 크면 재수집하지 않는다.
REFETCH_MAX_CANDIDATES = 200


def _search(query: str, scope: Scope, limit: int, use_rerank: bool, decay_rate: float,
            db_path: Optional[str], chroma_path: Optional[str], no_vector: bool) -> Dict[str, Any]:
    started = time.perf_counter()
    if limit < 0 or not math.isfinite(decay_rate) or decay_rate < 0:
        raise ValueError('limit and decay must be nonnegative; decay must be finite')
    count = limit * 3
    cap = max(count, REFETCH_MAX_CANDIDATES)
    vectors, vector_meta = [], {'ok': False, 'error': 'disabled'}
    # 근사 중복을 접고도 limit 개를 채우도록, 모자라고 후보가 더 있으면 후보 수를 4배(상한 cap)로 늘려 최대 2회 다시 모은다.
    for attempt in range(3):
        lexical, backend = _lexical(query, scope, count, db_path) if count and query.strip() else ([], 'fts5')
        # 벡터가 한 번 실패했으면(Ollama 꺼짐·잠금 등) 재수집 때 다시 불러도 같은 실패라 그 비용만 든다 — 건너뛴다.
        if not no_vector and (attempt == 0 or vector_meta['ok']):
            vectors, vector_meta = vector_search(query, scope, count, chroma_path)
        ranks, scores, items, ordered = _fuse(scope, decay_rate, db_path, lexical, vectors)
        # 재순위는 상위 count 개만 받아 그 안에서 순서를 바꾸므로, 채움 판단도 그 범위를 접어서 한다.
        pool = ordered[:count] if use_rerank else ordered
        exhausted = len(lexical) < count and len(vectors) < count
        if len(fold_duplicates(pool, items)) >= limit or exhausted or attempt == 2 or count >= cap:
            break
        count = min(count * 4, cap)
    rerank_error = None
    reranked = False
    # 재순위(질의당 수백 ms)는 재수집 회차마다가 아니라 최종 후보에 한 번만 돌린다.
    if use_rerank and ordered:
        try:
            # 접은 뒤에도 limit 개가 남도록 재순위는 후보 전체(count)를 돌려받는다.
            hits = rerank(query, [dict(items[obs_id], obs_id=obs_id) for obs_id in ordered[:count]], count)
            ordered = [hit.obs_id for hit in hits]
            scores.update({hit.obs_id: hit.score for hit in hits})
            reranked = True
        except Exception as exc:  # Optional offline model boundary; retain fused ranking.
            rerank_error = str(exc)
    # 묶음 자리(첫 순위)는 그대로 두고 대표(최신)의 점수를 그 자리 점수로 보여 순서가 흐트러지지 않게 한다.
    folded = fold_duplicates(ordered, items)
    for newest, first, _ in folded:
        scores[newest] = scores[first]
    results = []
    for obs_id, _, folded_count in folded[:limit]:
        item = items[obs_id]
        sources = [name for name, ranking in zip(('fts', 'vector'), ranks) if obs_id in ranking]
        kind = _provenance(item, 'kind') or 'observation'
        result = {'obs_id': obs_id, 'score': scores[obs_id], 'sources': sources + (['rerank'] if reranked else []),
                  'rank_fts': ranks[0].get(obs_id), 'rank_vector': ranks[1].get(obs_id),
                  'title': item['title'], 'project': item['project'], 'created_at': item['created_at'],
                  'snippet': (item['narrative'] or '')[:200], 'kind': kind, 'folded': folded_count}
        if kind == 'consolidation':
            result['derived_from'] = _source_ids(item)
        results.append(result)
    meta = {'vector_ok': vector_meta['ok'], 'vector_error': vector_meta['error'],
            'snapshot': vector_meta.get('snapshot'),
            'fts_backend': backend, 'took_ms': (time.perf_counter() - started) * 1000}
    if use_rerank:
        meta.update(rerank_ok=reranked, rerank_error=rerank_error)
    return {'query': query, 'scope': asdict(scope), 'results': results, 'meta': meta}


def _fuse(scope: Scope, decay_rate: float, db_path: Optional[str], lexical: List[Hit], vectors: List[Hit]):
    """RRF 융합 → 감쇠 → 순위 목록. 재순위·근사 중복 접기·결과 조립은 호출자가 한다."""
    projects = _projects(scope)
    ranks = [{hit.obs_id: rank for rank, hit in enumerate(hits, 1)} for hits in (lexical, vectors)]
    scores = rrf([list(ranking) for ranking in ranks])
    items = hydrate(list(scores), db_path)
    items = {key: item for key, item in items.items() if not projects or item['project'] in projects}
    if decay_rate > 0:
        originals = hydrate([source_id for item in items.values() for source_id in _source_ids(item)], db_path)
        now = int(time.time() * 1000)
        for obs_id, item in items.items():
            dates = [_epoch(originals[source_id]) for source_id in _source_ids(item) if source_id in originals]
            dates = [date for date in dates if date is not None]
            created = min(dates) if dates else _epoch(item)
            if created is not None:
                scores[obs_id] = time_decay(scores[obs_id], created, now, decay_rate)
    ordered = sorted(items, key=lambda obs_id: (-scores[obs_id], obs_id))
    return ranks, scores, items, ordered


def search(query: str, scope: Scope, limit: int = 20, use_rerank: bool = False, decay_rate: float = 0.0,
           db_path: str = None, chroma_path: str = None) -> Dict[str, Any]:
    return _search(query, scope, limit, use_rerank, decay_rate, db_path, chroma_path, False)


# ---------------------------------------------------------------------------
# memory-operations M2: 목적별 scope 조회 (opt-in --mode). 기존 인자·출력은 그대로 둔다.
#   current  → L2 head (sb_state.query)      history → --fact-key 면 L2 이력, 아니면 L0 관측(scope 선필터 FTS)
#   next     → L1 loops (scope 일치만)        rules   → SB_RULES_DIR/<scope>.md (읽기 전용)
# 응답 봉투: schema_version, query_mode, scope, status(ok/empty/degraded/unavailable/error), items, omitted, warnings
# scope 미지정 시 sb_scope.resolve_scope_id(cwd) — 폴더 basename 매칭 없음. 빈 scope 는 error.
# current 가 비어도 L0 이력을 현재 답으로 내지 않는다.
# ---------------------------------------------------------------------------
MODES = ('current', 'history', 'next', 'rules')
MODE_EXIT = {'ok': 0, 'empty': 0, 'degraded': 0, 'unavailable': 8, 'error': 2}


def _mode_envelope(mode: str, scope: Any, items: List[Dict[str, Any]], status: str, warnings: List[str],
                   omitted: int = 0) -> Dict[str, Any]:
    return {'schema_version': 1, 'query_mode': mode, 'scope': scope, 'status': status, 'items': items,
            'omitted': omitted, 'warnings': warnings}


def _resolve_scopes(args) -> List[str]:
    if args.scopes is not None:
        scopes = [s.strip() for s in args.scopes.split(',')]
        if not scopes or any(not s for s in scopes):
            raise ValueError('scope_required')
        return list(dict.fromkeys(scopes))
    if args.scope is not None:
        if not args.scope.strip():
            raise ValueError('scope_required')
        return [args.scope.strip()]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sb_scope import resolve_scope_id
    return [resolve_scope_id(os.getcwd())[0]]


def _l0_history(query: str, scope_id: str, limit: int) -> Dict[str, Any]:
    if not query or not query.strip():
        return _mode_envelope('history', scope_id, [], 'error', ['query_required'])
    db = _db_path(None)
    if not db.exists():
        return _mode_envelope('history', scope_id, [], 'unavailable', ['claude_mem_db_missing'])
    try:
        hits = fts_search(query, Scope('project', [scope_id]), limit, None)  # WHERE project=? 가 LIMIT 앞에 적용된다
        rows = hydrate([h.obs_id for h in hits], None)
    except sqlite3.OperationalError as exc:
        status = 'degraded' if 'locked' in str(exc).lower() else 'error'
        return _mode_envelope('history', scope_id, [], status, ['sqlite:' + str(exc)])
    items = []
    for h in hits:
        r = rows.get(h.obs_id)
        if not r or r['project'] != scope_id:
            continue  # 방어: scope 밖 행은 결과에 넣지 않는다
        items.append({'scope_id': r['project'], 'layer': 'L0', 'item_id': r['id'], 'fact_key': None,
                      'body': ((r['title'] or '') + ' — ' + (r['narrative'] or ''))[:400],
                      'evidence_ref': 'observation:%d' % r['id'], 'observed_at': r['created_at'], 'recorded_at': r['created_at'],
                      'verification_status': 'historical', 'reason_codes': ['l0_observation'], 'score': h.score})
    return _mode_envelope('history', scope_id, items, 'ok' if items else 'empty', [])


def _next_items(scope_id: str) -> Dict[str, Any]:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sb_scope import loop_matches_scope
    path = Path(os.environ.get('SB_LOOPS_PATH') or sb_config.sb_path('loops', 'loops.jsonl')).expanduser()
    if not path.exists():
        return _mode_envelope('next', scope_id, [], 'unavailable', ['loops_file_missing'])
    warnings, items = [], []
    with open(path, encoding='utf-8') as source:
        for n, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                l = json.loads(line)
            except ValueError:
                warnings.append('malformed_line:%d' % n)
                continue
            if l.get('status') not in ('open', 'waiting_external') or not loop_matches_scope(l.get('project'), scope_id):
                continue
            codes = [l['status']] + (['missing_next_action'] if not l.get('next_action') else [])
            items.append({'scope_id': scope_id, 'layer': 'L1', 'item_id': l['id'], 'loop_id': l['id'], 'fact_key': None,
                          'body': l.get('title', ''), 'next_action': l.get('next_action'), 'next_review': l.get('next_review'),
                          'status': l['status'], 'value': l.get('value'), 'evidence_ref': 'loop:' + l['id'],
                          'observed_at': None, 'recorded_at': l.get('created_at'), 'verification_status': 'n/a',
                          'reason_codes': codes})
    items.sort(key=lambda i: ({'high': 0, 'medium': 1, 'low': 2}.get(i.get('value'), 3), not i.get('next_review'),
                              i.get('next_review') or '', i['item_id']))
    status = 'degraded' if warnings else ('ok' if items else 'empty')
    return _mode_envelope('next', scope_id, items, status, warnings)


def _rules_items(scope_id: str) -> Dict[str, Any]:
    base = Path(os.environ.get('SB_RULES_DIR') or sb_config.sb_path('rules')).expanduser().resolve()
    name = scope_id.replace('/', '__').replace('\\', '__').replace('..', '_') + '.md'
    path = (base / name)
    if not base.exists():
        return _mode_envelope('rules', scope_id, [], 'unavailable', ['rules_dir_missing'])
    if not str(path.resolve()).startswith(str(base) + os.sep):
        return _mode_envelope('rules', scope_id, [], 'error', ['rules_path_outside_dir'])
    if not path.is_file():
        return _mode_envelope('rules', scope_id, [], 'empty', [])
    text = path.read_bytes()[:20000].decode('utf-8', errors='replace')
    item = {'scope_id': scope_id, 'layer': 'L3', 'item_id': name, 'fact_key': None, 'body': text,
            'evidence_ref': str(path), 'observed_at': None, 'recorded_at': None, 'verification_status': 'document',
            'reason_codes': ['rules_document']}
    return _mode_envelope('rules', scope_id, [item], 'ok', [])


def run_mode(args) -> Dict[str, Any]:
    try:
        scopes = _resolve_scopes(args)
    except ValueError as exc:
        return _mode_envelope(args.mode, args.scope, [], 'error', [str(exc)])
    if args.mode == 'history' and not args.fact_key and len(scopes) != 1:
        return _mode_envelope('history', scopes, [], 'error', ['history_requires_single_scope'])
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sb_state
    envelopes = []
    for scope_id in scopes:
        if args.mode == 'current':
            envelopes.append(dict(sb_state.query('head', scope_id, args.fact_key or None), query_mode='current'))
        elif args.mode == 'history':
            if args.fact_key:
                envelopes.append(sb_state.query('history', scope_id, args.fact_key))
            else:
                envelopes.append(_l0_history(args.query or '', scope_id, args.limit))
        elif args.mode == 'next':
            envelopes.append(_next_items(scope_id))
        else:
            envelopes.append(_rules_items(scope_id))
    if len(envelopes) == 1:
        return envelopes[0]
    order = ['error', 'unavailable', 'degraded', 'ok', 'empty']
    status = min((e['status'] for e in envelopes), key=order.index)
    if status in ('ok', 'empty'):
        status = 'ok' if any(e['items'] for e in envelopes) else 'empty'
    return _mode_envelope(args.mode, {'all': True, 'scopes': scopes}, [i for e in envelopes for i in e['items']], status,
                          [w for e in envelopes for w in e['warnings']], sum(e.get('omitted', 0) for e in envelopes))


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query', nargs='?')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--project')
    group.add_argument('--global', dest='global_scope', action='store_true')
    group.add_argument('--projects')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--rerank', action='store_true')
    parser.add_argument('--decay', type=float, default=0.0)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--no-vector', action='store_true')
    snapshots = parser.add_mutually_exclusive_group()
    snapshots.add_argument('--fresh', action='store_true', help='Force a Chroma snapshot copy even when the source generation is unchanged')
    snapshots.add_argument('--no-snapshot', action='store_true', help='Query original Chroma directly (concurrent worker access risk)')
    parser.add_argument('--mode', choices=MODES, help='opt-in purpose-scoped query (memory-operations M2)')
    parser.add_argument('--scope', help='explicit scope_id for --mode (default: sb_scope of cwd; "global" allowed)')
    parser.add_argument('--scopes', help='explicit comma list for --mode current/next/rules across projects (labelled per scope)')
    parser.add_argument('--fact-key', dest='fact_key')
    args = parser.parse_args(argv)
    if args.mode:
        try:
            envelope = run_mode(args)
        except (ValueError, OSError, sqlite3.Error) as exc:
            envelope = _mode_envelope(args.mode, args.scope, [], 'error', [type(exc).__name__ + ': ' + str(exc)])
        if args.json:
            print(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
        else:
            print('[%s] scope=%s status=%s items=%d' % (envelope['query_mode'], envelope['scope'], envelope['status'], len(envelope['items'])))
            for it in envelope['items']:
                print(' -', it.get('fact_key') or it.get('loop_id') or it.get('item_id'), '|', str(it.get('body', ''))[:120].replace('\n', ' '),
                      '|', it.get('verification_status'))
            for w in envelope['warnings']:
                print(' ! ' + w)
        return MODE_EXIT.get(envelope['status'], 2)
    if args.query is None:
        parser.error('the following arguments are required: query')
    scope = Scope('global', []) if args.global_scope else (
        Scope('multi', [project.strip() for project in args.projects.split(',')]) if args.projects is not None
        # Default L0 filter is the cwd basename on purpose: claude-mem stores project
        # names as folder basenames, not sb_scope ids (--mode uses sb_scope instead).
        else Scope('project', [args.project if args.project is not None else Path.cwd().name]))
    token = _snapshot_options.set((args.fresh, args.no_snapshot))
    try:
        result = _search(args.query, scope, args.limit, args.rerank, args.decay, None, None, args.no_vector)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print('sb_search: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        _snapshot_options.reset(token)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    else:
        print('순위  점수     출처          프로젝트  날짜        제목')
        for rank, item in enumerate(result['results'], 1):
            fields = [','.join(item['sources']), item['project'], str(item['created_at'] or '')[:10], item['title'] or '']
            print(f"{rank:>4}  {item['score']:.3f}  " + '  '.join(' '.join(str(field).split()) for field in fields))
        if result['meta']['vector_error'] and not args.no_vector:
            print('vector: ' + result['meta']['vector_error'], file=sys.stderr)
        if result['meta'].get('rerank_error'):
            print('rerank: ' + result['meta']['rerank_error'], file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
