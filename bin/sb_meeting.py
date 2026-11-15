#!/usr/bin/env python3
"""회의록 입력 계약·민감도 게이트·고정 500자 구간 색인·회수 (sb meeting).

  sb meeting index --source PATH [--source PATH ...] [--index-dir D] [--no-vector]
  sb meeting search "<질문>" [--k 3] [--index-dir D] [--no-vector]
  sb meeting status [--index-dir D]

입력 계약 MeetingDoc = {id, title, date(YYYY-MM-DD), participants_count, sensitivity, source, text}.
변환기 3종: from_meetnote_json(미팅노트 내보내기 json), from_corpus_dir(transcript.txt + meta.md),
from_text(txt·md, front-matter 또는 같은 이름의 .meta.json).

민감도 게이트: sensitivity 가 'public' 일 때만 색인한다. 없음·빈 값·그 밖의 값은 private 으로 보고 거부한다.
민감도는 front-matter·사이드카·인자·중복 키가 말한 값 중 가장 엄격한 것을 쓴다(모두 public 일 때만 public).
색인: 전사를 겹침 없이 500자씩 자르고 형태소(sb_fts_ko.tokenize_ko)로 FTS5 에 넣는다(index/meeting_fts.sqlite).

색인 파일에 남는 것(전사 글자는 없다):
- meetings: 회의 id·제목·날짜·참석자 수·소스 위치·sha256. 요약은 저장하지 않고 카드를 붙일 때 소스에서 읽는다.
- chunks: 구간 위치(offset·length)·sha256.
- chunk_fts: 구간 형태소를 색인별 키로 해시한 토큰(blake2b 64비트, 정렬해 어순 없음). 이메일·7자리 이상
  숫자열(전화·계좌 꼴)은 토큰에서 뺀다. 해시는 사전 대입으로 흔한 낱말을 되돌릴 수 있으니 암호화가 아니다.
- 지운 회의의 페이지는 secure_delete 로 덮어쓴다.
검색 때 소스를 다시 읽어 구간을 잘라 돌려주며, 소스가 바뀌었거나(sha 불일치) public 이 아니게 됐으면 그 회의를 뺀다.
벡터(선택): Ollama bge-m3-ko 가 응답하고 numpy 가 있고 디스크 여유가 5GB 이상일 때만
index/meeting_vec.npy + meeting_vec_ids.json 을 만든다. 벡터는 원문에서 나온 데이터다(원문 복원은 어렵지만
내용 추정은 가능). 벡터를 못 만들면 옛 벡터 파일을 지운다. 검색 때 벡터가 있으면 벡터 순위를 쓰고
(FTS 는 벡터가 못 채운 자리만), 없으면 FTS 순위를 쓴다. --fusion rrf 로 RRF 결합을 고를 수 있다.
카드(회의 이름·날짜·요약)는 질문에 회의 이름이나 날짜가 나올 때만 붙인다.

표준 라이브러리 + (있으면) numpy·kiwipiepy. 파이썬 3.9 호환.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sb_config  # noqa: E402
import sb_fts_ko  # noqa: E402
import sb_lock  # noqa: E402

CHUNK_CHARS = 500
SCHEMA = 'sb-meeting-2'
FTS_NAME = 'meeting_fts.sqlite'
VEC_NAME = 'meeting_vec.npy'
VEC_IDS_NAME = 'meeting_vec_ids.json'
MIN_FREE_BYTES = 5 * 1024 ** 3
RRF_K = 60
CARD_SUMMARY_CHARS = 800
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
KINDS = ('meetnote', 'corpus', 'text')


# ---------------------------------------------------------------- 입력 계약

def _date10(value: Any) -> str:
    text = str(value or '').strip()
    m = re.match(r'^(\d{4})[-./](\d{1,2})[-./](\d{1,2})', text)
    if not m:
        return ''
    return '%s-%02d-%02d' % (m.group(1), int(m.group(2)), int(m.group(3)))


def _count(value: Any) -> Optional[int]:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _sensitivity(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


class MetaError(ValueError):
    """메타를 믿을 수 없어 문서를 거부한다. reason 은 거부 이유 집계 이름."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _sens_values(meta: Dict[str, Any]) -> List[Any]:
    """키 이름 대소문자와 상관없이 sensitivity 값을 모두 모은다."""
    return [v for k, v in meta.items() if str(k).strip().lower() == 'sensitivity']


def strictest_sensitivity(values: List[Any]) -> Optional[str]:
    """여러 곳(front-matter·사이드카·인자·중복 키)이 말한 민감도 중 가장 엄격한 것.

    하나도 없거나 하나라도 빈 값·null 이면 None(거부). 모두 public 일 때만 public.
    public 이 아닌 값이 섞이면 private 이 있으면 private, 없으면 처음 나온 그 값.
    """
    if not values:
        return None
    norm = [_sensitivity(v) for v in values]
    if any(v is None for v in norm):
        return None
    others = [v for v in norm if v != 'public']
    if not others:
        return 'public'
    return 'private' if 'private' in others else others[0]


def _doc(meta: Dict[str, Any], text: str, source: str, fallback_id: str,
         sens_values: Optional[List[Any]] = None) -> Dict[str, Any]:
    if sens_values is None:
        sens_values = _sens_values(meta)
    return {
        'id': str(meta.get('id') or meta.get('meeting_id') or meta.get('id8') or fallback_id).strip(),
        'title': str(meta.get('title') or '').strip(),
        'date': _date10(meta.get('date') or meta.get('created_at')),
        'participants_count': _count(meta.get('participants_count', meta.get('participants'))),
        'sensitivity': strictest_sensitivity(sens_values),
        'source': source,
        'text': text or '',
        'summary': str(meta.get('summary') or '').strip(),
    }


def from_meetnote_json(path) -> Dict[str, Any]:
    """미팅노트 내보내기 json(id, title, created_at, transcription_text, participants).

    11-04 시험 사본 꼴(date, text)도 받는다. sensitivity 필드가 없으면 None.
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('meetnote json must be an object')
    text = data.get('transcription_text')
    if text is None:
        text = data.get('text')
    return _doc(data, str(text or ''), 'meetnote', path.stem)


def _parse_kv_lines(text: str) -> Tuple[Dict[str, str], List[str]]:
    """meta.md 머리의 `- key: value` 묶음만 읽는다(키는 소문자, 같은 키는 앞의 값).

    맨 앞의 빈 줄·제목(#) 줄을 건너뛴 뒤 이어지는 글머리표 묶음까지만 보고, 다른 줄(다음 제목·본문)이
    나오면 멈춘다. 본문 글머리표의 `- sensitivity: ...` 는 메타가 아니다. 민감도 값은 모두 따로 돌려준다.
    """
    out: Dict[str, str] = {}
    sens: List[str] = []
    started = False
    for line in text.splitlines():
        if not line.strip():
            continue
        m = re.match(r'^\s*-\s*([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$', line)
        if m:
            started = True
            key = m.group(1).lower()
            if key == 'sensitivity':
                sens.append(m.group(2))
            out.setdefault(key, m.group(2))
            continue
        if not started and line.lstrip().startswith('#'):
            continue
        break
    return out, sens


def from_corpus_dir(path) -> Dict[str, Any]:
    """회의 폴더 하나: transcript.txt + meta.md(`- key: value` 줄)."""
    path = Path(path)
    text = (path / 'transcript.txt').read_text(encoding='utf-8-sig')
    meta_path = path / 'meta.md'
    if meta_path.exists():
        meta, sens = _parse_kv_lines(meta_path.read_text(encoding='utf-8-sig'))
    else:
        meta, sens = {}, []
    return _doc(meta, text, 'corpus', path.name, sens)


TEXT_SUFFIXES = ('.txt', '.md')


def _sidecars(path: Path) -> List[Path]:
    """이 파일에 붙는 사이드카 중 있는 것 [정확한 이름(a.md.meta.json), 줄기 이름(a.meta.json)].

    줄기 이름 사이드카는 같은 폴더에 줄기가 같은 다른 txt·md 가 있으면 어느 파일 것인지 알 수 없어
    MetaError('ambiguous_meta') 로 거부한다.
    """
    out = []
    exact = path.with_name(path.name + '.meta.json')
    if exact.is_file():
        out.append(exact)
    stem = path.with_suffix('.meta.json')
    if stem.is_file():
        lower = path.stem.lower()
        siblings = [c for c in path.parent.iterdir()
                    if c.name != path.name and c.suffix.lower() in TEXT_SUFFIXES and c.stem.lower() == lower]
        if siblings:
            raise MetaError('ambiguous_meta')
        out.append(stem)
    return out


def _load_json_meta(path: Path) -> Dict[str, Any]:
    loaded = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(loaded, dict):
        raise MetaError('invalid_meta')
    return loaded


def _front_matter(raw: str) -> Tuple[Dict[str, str], List[str], str]:
    """머리 front-matter(키는 소문자, 같은 키는 앞의 값), 민감도 값 전부, 본문."""
    m = re.match(r'^﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)', raw, re.S)
    if not m:
        return {}, [], raw
    meta: Dict[str, str] = {}
    sens: List[str] = []
    for line in m.group(1).splitlines():
        kv = re.match(r'^\s*([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$', line)
        if kv:
            key, value = kv.group(1).lower(), kv.group(2).strip('"\'')
            if key == 'sensitivity':
                sens.append(value)
            meta.setdefault(key, value)
    return meta, sens, raw[m.end():]


def from_text(path, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """일반 txt·md. 메타 필드 우선순위: 인자 meta > 정확한 이름 사이드카 > 줄기 이름 사이드카 > front-matter.

    민감도만은 우선순위가 아니라 모든 곳의 값 중 가장 엄격한 것을 쓴다(strictest_sensitivity).
    """
    path = Path(path)
    head, sens, body = _front_matter(path.read_text(encoding='utf-8-sig'))
    merged: Dict[str, Any] = dict(head)
    sides = [_load_json_meta(side) for side in _sidecars(path)]
    for loaded in reversed(sides):
        merged.update(loaded)
        sens.extend(_sens_values(loaded))
    if meta:
        merged.update(meta)
        sens.extend(_sens_values(meta))
    return _doc(merged, body, 'text', path.stem, sens)


def load_doc(kind: str, path, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if kind == 'meetnote':
        return from_meetnote_json(path)
    if kind == 'corpus':
        return from_corpus_dir(path)
    if kind == 'text':
        return from_text(path, meta)
    raise ValueError('unknown source kind: %s' % kind)


def discover(source) -> List[Tuple[str, Path]]:
    """소스 경로에서 (변환기 종류, 경로) 목록. 폴더는 한 단계 아래까지 본다."""
    source = Path(source).expanduser()
    if source.is_file():
        if source.name.endswith('.meta.json'):
            return []
        if source.suffix.lower() == '.json':
            return [('meetnote', source)]
        if source.suffix.lower() in ('.txt', '.md'):
            return [('text', source)]
        return []
    if not source.is_dir():
        raise FileNotFoundError(str(source))
    if (source / 'transcript.txt').is_file():
        return [('corpus', source)]
    found: List[Tuple[str, Path]] = []
    for child in sorted(source.iterdir()):
        if child.name.startswith('.'):
            continue
        if child.is_dir():
            if (child / 'transcript.txt').is_file():
                found.append(('corpus', child))
        else:
            found.extend(discover(child))
    return found


def gate(doc: Dict[str, Any]) -> Optional[str]:
    """None 이면 색인 허용. 아니면 거부 이유."""
    sens = doc.get('sensitivity')
    if sens is None:
        return 'missing_sensitivity'
    if sens != 'public':
        return 'private' if sens == 'private' else 'other_sensitivity'
    if not doc.get('id') or not doc.get('text', '').strip() or not DATE_RE.match(doc.get('date') or ''):
        return 'invalid'
    return None


def chunk_text(text: str, n: int = CHUNK_CHARS) -> List[Tuple[int, str]]:
    """겹침 없는 고정 n자 구간 [(offset, text)]."""
    if n <= 0:
        raise ValueError('chunk size must be positive')
    return [(i, text[i:i + n]) for i in range(0, len(text), n)]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------- 저장소

def default_index_dir() -> Path:
    return Path(sb_config.sb_path('index'))


def _paths(index_dir) -> Dict[str, Path]:
    base = Path(index_dir or default_index_dir()).expanduser()
    return {'dir': base, 'fts': base / FTS_NAME, 'vec': base / VEC_NAME, 'ids': base / VEC_IDS_NAME,
            'lock': base / 'meeting_index.lock'}


def _connect(path: Path, mode: str) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + '?mode=' + mode, uri=True)


def _schema_of(path: Path) -> Optional[str]:
    try:
        with closing(_connect(path, 'ro')) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


OLD_SCHEMAS = ('sb-meeting-1',)


def _open(path: Path, create: bool) -> sqlite3.Connection:
    if create and path.exists():
        found = _schema_of(path)
        if found in OLD_SCHEMAS:
            # 옛 꼴(형태소 평문·요약 저장)은 파생 색인이라 지우고 새로 만든다(원문은 소스에 있다).
            for leftover in (path, path.with_name(path.name + '-wal'), path.with_name(path.name + '-shm'),
                             path.with_name(path.name + '-journal')):
                if leftover.exists():
                    leftover.unlink()
        elif found != SCHEMA:
            raise ValueError('not a %s index: %s' % (SCHEMA, path))
    conn = _connect(path, 'rwc' if create else 'ro')
    if create:
        # 지운 회의의 페이지를 0 으로 덮어 파일에 남지 않게 한다.
        conn.execute('PRAGMA secure_delete = ON')
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS meetings (meeting_id TEXT PRIMARY KEY, title TEXT, date TEXT,'
                         ' participants_count INTEGER, source TEXT, source_kind TEXT, source_path TEXT,'
                         ' source_root TEXT, meta_override TEXT, text_sha TEXT, chunks INTEGER,'
                         ' indexed_at INTEGER)')
            conn.execute('CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, meeting_id TEXT, seq INTEGER,'
                         ' offset INTEGER, length INTEGER, chunk_sha TEXT, UNIQUE(meeting_id, seq))')
            conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(body)')
            conn.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (SCHEMA,))
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('token_key', ?)", (os.urandom(16).hex(),))
    return conn


# 이메일, 7자리 이상 숫자열(전화·계좌·주민번호 꼴, 숫자 사이 공백·점·하이픈 허용)은 색인 토큰에서 뺀다.
_PII_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*'
                     r'|(?<![0-9])[0-9](?:[ .\-]?[0-9]){6,}(?![0-9])')


def _token_key(conn: sqlite3.Connection) -> bytes:
    row = conn.execute("SELECT value FROM meta WHERE key = 'token_key'").fetchone()
    return bytes.fromhex(row[0]) if row else b''


def hash_tokens(tokens: Iterable[str], key: bytes) -> List[str]:
    """형태소 토큰을 색인별 키로 해시한다(blake2b 64비트). FTS 파일에 형태소가 평문으로 남지 않는다."""
    return ['h' + hashlib.blake2b(t.encode('utf-8'), key=key, digest_size=8).hexdigest() for t in tokens]


def index_body(text: str, key: bytes) -> str:
    """FTS 에 넣는 본문: 개인정보 꼴을 지운 글의 해시 토큰을 정렬한 묶음.

    정렬해 어순을 버려도 단일 토큰 OR 질의와 bm25(토큰 빈도·구간 길이)는 그대로다.
    """
    return ' '.join(sorted(hash_tokens(sb_fts_ko.tokenize_ko(_PII_RE.sub(' ', text)), key)))


def _delete_meeting(conn: sqlite3.Connection, meeting_id: str) -> None:
    ids = [r[0] for r in conn.execute('SELECT id FROM chunks WHERE meeting_id = ?', (meeting_id,))]
    for cid in ids:
        conn.execute('DELETE FROM chunk_fts WHERE rowid = ?', (cid,))
    conn.execute('DELETE FROM chunks WHERE meeting_id = ?', (meeting_id,))
    conn.execute('DELETE FROM meetings WHERE meeting_id = ?', (meeting_id,))


# ---------------------------------------------------------------- 벡터(선택)

def _ollama_url() -> str:
    return (os.environ.get('SB_OLLAMA_URL') or 'http://127.0.0.1:11434').rstrip('/')


def _embed_model() -> str:
    return os.environ.get('SB_MEETING_EMBED_MODEL') or 'bge-m3-ko'


def ollama_ready(timeout: float = 3.0) -> bool:
    model = _embed_model()
    try:
        with urllib.request.urlopen(_ollama_url() + '/api/tags', timeout=timeout) as resp:
            names = [m.get('name', '') for m in json.loads(resp.read()).get('models', [])]
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return any(name == model or name.split(':')[0] == model for name in names)


def ollama_embed(texts: List[str], batch: int = 16, timeout: float = 120.0) -> List[List[float]]:
    out: List[List[float]] = []
    for i in range(0, len(texts), batch):
        body = json.dumps({'model': _embed_model(), 'input': texts[i:i + batch]}).encode('utf-8')
        req = urllib.request.Request(_ollama_url() + '/api/embed', data=body,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            vectors = json.loads(resp.read())['embeddings']
        if len(vectors) != len(texts[i:i + batch]):
            raise ValueError('embedding count mismatch')
        out.extend(vectors)
    return out


def _numpy():
    try:
        import numpy
    except ImportError:
        return None
    return numpy


def _vector_skip_reason(index_dir: Path, embed: Optional[Callable], no_vector: bool) -> Optional[str]:
    if no_vector:
        return 'disabled'
    if _numpy() is None:
        return 'numpy_missing'
    if shutil.disk_usage(str(index_dir)).free < MIN_FREE_BYTES:
        return 'disk_below_5gb'
    if embed is None and not ollama_ready():
        return 'ollama_unavailable'
    return None


def _atomic_write_bytes(path: Path, writer: Callable[[Any], None]) -> None:
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'wb') as fh:
        writer(fh)
    os.replace(tmp, path)


def _build_vectors(conn: sqlite3.Connection, paths: Dict[str, Path], texts: Dict[int, str],
                   embed: Callable[[List[str]], List[List[float]]]) -> Dict[str, Any]:
    """현재 chunks 전체에 대한 벡터. 같은 chunk_sha·모델의 기존 벡터는 다시 쓴다."""
    np = _numpy()
    model = _embed_model()
    cache: Dict[str, Any] = {}
    if paths['vec'].exists() and paths['ids'].exists():
        try:
            old_ids = json.loads(paths['ids'].read_text(encoding='utf-8'))
            old = np.load(str(paths['vec']))
            if old_ids.get('model') == model and len(old_ids.get('chunks', [])) == len(old):
                for entry, vec in zip(old_ids['chunks'], old):
                    cache[entry['chunk_sha']] = vec
        except (OSError, ValueError, KeyError):
            cache = {}
    rows = list(conn.execute('SELECT id, meeting_id, seq, chunk_sha FROM chunks ORDER BY id'))
    missing = [r for r in rows if r[3] not in cache]
    need_text = [texts[r[0]] for r in missing]
    if need_text:
        fresh = np.asarray(embed(need_text), dtype=np.float32)
        norms = np.linalg.norm(fresh, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        fresh = fresh / norms
        for r, vec in zip(missing, fresh):
            cache[r[3]] = vec
    if rows:
        matrix = np.stack([np.asarray(cache[r[3]], dtype=np.float32) for r in rows])
    else:
        matrix = np.zeros((0, 0), dtype=np.float32)
    build_id = dict(conn.execute('SELECT key, value FROM meta')).get('build_id', '')
    ids = {'schema': SCHEMA, 'model': model, 'build_id': build_id,
           'chunks': [{'meeting_id': r[1], 'seq': r[2], 'chunk_sha': r[3]} for r in rows]}
    _atomic_write_bytes(paths['vec'], lambda fh: np.save(fh, matrix))
    _atomic_write_bytes(paths['ids'], lambda fh: fh.write(json.dumps(ids, ensure_ascii=False).encode('utf-8')))
    return {'vectors': len(rows), 'embedded': len(missing), 'reused': len(rows) - len(missing), 'model': model}


# ---------------------------------------------------------------- 색인

def _same_path(a: str, b: str) -> bool:
    """같은 파일·폴더인가. 대소문자를 가리지 않는 파일 시스템에서 대소문자만 다른 경로도 같다."""
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def index(sources: Iterable, index_dir=None, no_vector: bool = False,
          embed: Optional[Callable[[List[str]], List[List[float]]]] = None,
          text_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """소스마다 그 소스에서 온 회의를 통째로 다시 만든다(다른 소스의 회의는 그대로).

    거부된 회의는 이전에 색인됐더라도 지운다(같은 소스 파일에서 온 것만). 다른 소스가 이미 쓰는 회의 id 는
    덮어쓰지 않고 id_conflict 로 거부한다. text_meta: {txt 경로: meta} (from_text 의 meta 인자).
    """
    start = time.monotonic()
    paths = _paths(index_dir)
    paths['dir'].mkdir(parents=True, exist_ok=True)
    text_meta = {str(Path(k).expanduser().resolve()): v for k, v in (text_meta or {}).items()}
    rejected: Dict[str, int] = {}
    seen = 0
    accepted: List[Tuple[Dict[str, Any], str, Path, Path, Optional[Dict[str, Any]]]] = []
    reject_ids: List[Tuple[str, str]] = []
    roots: List[str] = []
    for source in sources:
        root = Path(source).expanduser().resolve()
        roots.append(str(root))
        for kind, path in discover(root):
            seen += 1
            meta = text_meta.get(str(path.resolve())) if kind == 'text' else None
            try:
                doc = load_doc(kind, path, meta)
            except MetaError as exc:
                rejected[exc.reason] = rejected.get(exc.reason, 0) + 1
                continue
            except (OSError, ValueError, UnicodeDecodeError):
                rejected['unreadable'] = rejected.get('unreadable', 0) + 1
                continue
            reason = gate(doc)
            if reason is None and any(a[0]['id'] == doc['id'] for a in accepted):
                reason = 'duplicate_id'
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                if doc.get('id'):
                    reject_ids.append((doc['id'], str(path.resolve())))
                continue
            accepted.append((doc, kind, path.resolve(), root, meta))
    texts: Dict[int, str] = {}
    chunks_total = 0
    with (paths['lock']).open('a') as lock:
        sb_lock.lock_with_timeout(lock, sb_lock.LOCK_EX, 30.0)
        with closing(_open(paths['fts'], True)) as conn:
            with conn:
                stored_roots = {r[0] for r in conn.execute('SELECT DISTINCT source_root FROM meetings')}
                for root in roots:
                    for old_root in [r for r in stored_roots if _same_path(r, root)]:
                        for (mid,) in list(conn.execute('SELECT meeting_id FROM meetings WHERE source_root = ?',
                                                        (old_root,))):
                            _delete_meeting(conn, mid)
                for mid, rpath in reject_ids:
                    row = conn.execute('SELECT source_path FROM meetings WHERE meeting_id = ?', (mid,)).fetchone()
                    if row is not None and _same_path(row[0], rpath):
                        _delete_meeting(conn, mid)
                key = _token_key(conn)
                indexed = 0
                for doc, kind, path, root, meta in accepted:
                    owner = conn.execute('SELECT source_path FROM meetings WHERE meeting_id = ?',
                                         (doc['id'],)).fetchone()
                    if owner is not None and not _same_path(owner[0], str(path)):
                        rejected['id_conflict'] = rejected.get('id_conflict', 0) + 1
                        continue
                    _delete_meeting(conn, doc['id'])
                    indexed += 1
                    pieces = chunk_text(doc['text'])
                    conn.execute('INSERT INTO meetings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                        doc['id'], doc['title'], doc['date'], doc['participants_count'], doc['source'], kind,
                        str(path), str(root), json.dumps(meta, ensure_ascii=False) if meta else None,
                        _sha(doc['text']), len(pieces), int(time.time())))
                    for seq, (offset, piece) in enumerate(pieces):
                        cur = conn.execute('INSERT INTO chunks(meeting_id, seq, offset, length, chunk_sha)'
                                           ' VALUES (?,?,?,?,?)', (doc['id'], seq, offset, len(piece), _sha(piece)))
                        conn.execute('INSERT INTO chunk_fts(rowid, body) VALUES (?, ?)',
                                     (cur.lastrowid, index_body(piece, key)))
                        texts[cur.lastrowid] = piece
                    chunks_total += len(pieces)
                build_id = '%d-%s' % (time.time_ns(), _sha(json.dumps(roots))[:8])
                last = {'seen': seen, 'indexed': indexed, 'chunks': chunks_total, 'rejected': rejected}
                conn.executemany('INSERT OR REPLACE INTO meta VALUES (?, ?)', [
                    ('schema', SCHEMA), ('tokenizer', sb_fts_ko._tokenizer()), ('chunk_chars', str(CHUNK_CHARS)),
                    ('build_id', build_id), ('last_build', str(int(time.time()))),
                    ('last_run', json.dumps(last, ensure_ascii=False))])
            skip = _vector_skip_reason(paths['dir'], embed, no_vector)
            vector: Dict[str, Any] = {'built': False, 'skipped': skip}
            if skip is None:
                # 다른 소스에서 온(이번에 다시 읽지 않은) 구간의 글자는 소스에서 다시 읽는다.
                others = [r[0] for r in conn.execute('SELECT id FROM chunks') if r[0] not in texts]
                texts.update(_chunk_texts(conn, others, {}))
                try:
                    info = _build_vectors(conn, paths, texts, embed or ollama_embed)
                    vector = {'built': True, 'skipped': None, **info}
                except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                    vector = {'built': False, 'skipped': 'embed_failed: %s' % type(exc).__name__}
            if not vector['built']:
                # 옛 벡터는 지금 색인과 맞지 않고(지운 회의의 구간도 들어 있을 수 있다) 검색도 쓰지 않으니 지운다.
                for name in ('vec', 'ids'):
                    if paths[name].exists():
                        paths[name].unlink()
    return {'seen': seen, 'indexed': indexed, 'chunks': chunks_total, 'rejected': rejected,
            'vector': vector, 'index': str(paths['fts']), 'took_ms': round((time.monotonic() - start) * 1000)}


# ---------------------------------------------------------------- 회수

_MEETING_COLS = ('meeting_id', 'source_kind', 'source_path', 'meta_override', 'text_sha')


def _meeting_row(conn: sqlite3.Connection, meeting_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute('SELECT %s FROM meetings WHERE meeting_id = ?' % ', '.join(_MEETING_COLS),
                       (meeting_id,)).fetchone()
    return dict(zip(_MEETING_COLS, row)) if row else None


def _reload(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """소스를 다시 읽어 sha·민감도를 확인한다. 어긋나면 None."""
    if row is None:
        return None
    meta = json.loads(row['meta_override']) if row['meta_override'] else None
    try:
        doc = load_doc(row['source_kind'], row['source_path'], meta)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if gate(doc) is not None or _sha(doc['text']) != row['text_sha']:
        return None
    return doc


def _doc_for(conn: sqlite3.Connection, meeting_id: str, docs: Dict[str, Optional[Dict[str, Any]]]
             ) -> Optional[Dict[str, Any]]:
    if meeting_id not in docs:
        docs[meeting_id] = _reload(_meeting_row(conn, meeting_id))
    return docs[meeting_id]


def _chunk_texts(conn: sqlite3.Connection, chunk_ids: List[int], docs: Dict[str, Optional[Dict[str, Any]]]
                 ) -> Dict[int, str]:
    """구간 id → 소스에서 다시 자른 글자. 검증 실패한 회의의 구간은 빠진다."""
    out = {}
    for cid in chunk_ids:
        row = conn.execute('SELECT meeting_id, offset, length FROM chunks WHERE id = ?', (cid,)).fetchone()
        if row is None:
            continue
        doc = _doc_for(conn, row[0], docs)
        if doc is not None:
            out[cid] = doc['text'][row[1]:row[1] + row[2]]
    return out


def _fts_rank(conn: sqlite3.Connection, query: str, limit: int) -> List[int]:
    tokens = list(dict.fromkeys(hash_tokens(sb_fts_ko.ko_query_tokens(query), _token_key(conn))))
    if not tokens:
        return []
    match = ' OR '.join('"' + t + '"' for t in tokens)
    return [r[0] for r in conn.execute('SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ?'
                                       ' ORDER BY bm25(chunk_fts), rowid LIMIT ?', (match, limit))]


def _vec_rank(conn: sqlite3.Connection, paths: Dict[str, Path], query: str, limit: int,
              embed: Optional[Callable]) -> Optional[List[int]]:
    np = _numpy()
    if np is None or not paths['vec'].exists() or not paths['ids'].exists():
        return None
    try:
        ids = json.loads(paths['ids'].read_text(encoding='utf-8'))
        matrix = np.load(str(paths['vec']))
    except (OSError, ValueError):
        return None
    meta = dict(conn.execute('SELECT key, value FROM meta'))
    if ids.get('build_id') != meta.get('build_id') or ids.get('model') != _embed_model():
        return None
    if len(ids.get('chunks', [])) != len(matrix) or len(matrix) == 0:
        return None
    if embed is None:
        if not ollama_ready():
            return None
        embed = ollama_embed
    try:
        qv = np.asarray(embed([query])[0], dtype=np.float32)
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        return None
    if qv.shape[0] != matrix.shape[1]:
        return None
    qv = qv / (np.linalg.norm(qv) or 1.0)
    order = np.argsort(-(matrix @ qv))[:limit]
    key_to_id = {(m, s): (cid, sha) for cid, m, s, sha in
                 conn.execute('SELECT id, meeting_id, seq, chunk_sha FROM chunks')}
    out = []
    for i in order:
        entry = ids['chunks'][int(i)]
        hit = key_to_id.get((entry['meeting_id'], entry['seq']))
        if hit and hit[1] == entry['chunk_sha']:
            out.append(hit[0])
    return out


def rrf(rankings: List[List[int]], k: int = RRF_K) -> List[int]:
    scores: Dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, 1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda c: (-scores[c], c))


def _norm(text: str) -> str:
    return re.sub(r'[\s\[\]()<>"\'「」『』·,.:!?~-]+', '', text or '').lower()


def card_matches(query: str, title: str, date: str) -> bool:
    """회의 이름(공백 무시 전체 일치)이나 날짜(YYYY-MM-DD 류 또는 M월 D일)가 질문에 있으면 True."""
    nt = _norm(title)
    if len(nt) >= 2 and nt in _norm(query):
        return True
    if not DATE_RE.match(date or ''):
        return False
    y, mo, d = int(date[:4]), int(date[5:7]), int(date[8:10])
    for m in re.finditer(r'(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})', query):
        if (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (y, mo, d):
            return True
    for m in re.finditer(r'(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일', query):
        if (int(m.group(1)), int(m.group(2))) == (mo, d):
            return True
    return False


FUSIONS = ('vector', 'rrf')


def search(query: str, k: int = 3, index_dir=None, no_vector: bool = False,
           embed: Optional[Callable[[List[str]], List[List[float]]]] = None,
           fusion: str = 'vector') -> Dict[str, Any]:
    """상위 구간 k개 + 각 구간의 앞뒤 구간.

    fusion='vector'(기본): 벡터가 있으면 벡터 순위를 쓰고 FTS 순위는 그 뒤에 잇는다(벡터가 못 채운 자리만).
    벡터가 없으면 FTS 순위. fusion='rrf': 벡터와 FTS 를 RRF 로 합친다(파일럿에서 벡터 단독보다 낮아 실험용).
    """
    if fusion not in FUSIONS:
        raise ValueError('unknown fusion: %s' % fusion)
    paths = _paths(index_dir)
    empty = {'query_mode': 'none', 'hits': [], 'cards': [], 'dropped_meetings': 0}
    if k <= 0 or not paths['fts'].exists():
        return empty
    with closing(_open(paths['fts'], False)) as conn:
        pool = max(k * 10, 50)
        fts = _fts_rank(conn, query, pool)
        vec = None if no_vector else _vec_rank(conn, paths, query, pool, embed)
        if not vec:
            order, mode = fts, 'fts'
        elif fusion == 'rrf':
            order, mode = rrf([fts, vec]), 'fts+vector'
        else:
            seen_ids = set(vec)
            order, mode = vec + [c for c in fts if c not in seen_ids], 'vector'
        docs: Dict[str, Optional[Dict[str, Any]]] = {}
        hits = []
        for cid in order:
            if len(hits) >= k:
                break
            row = conn.execute('SELECT c.meeting_id, c.seq, m.title, m.date, m.chunks FROM chunks c JOIN meetings m'
                               ' ON m.meeting_id = c.meeting_id WHERE c.id = ?', (cid,)).fetchone()
            if row is None:
                continue
            mid, seq, title, date, nchunks = row
            seqs = [s for s in (seq - 1, seq, seq + 1) if 0 <= s < nchunks]
            ids = [r[0] for r in conn.execute(
                'SELECT id FROM chunks WHERE meeting_id = ? AND seq IN (%s) ORDER BY seq' % ','.join('?' * len(seqs)),
                [mid, *seqs])]
            texts = _chunk_texts(conn, ids, docs)
            if docs.get(mid) is None or len(texts) != len(ids):
                continue
            hits.append({'meeting_id': mid, 'date': date, 'title': title, 'seq': seq, 'seqs': seqs,
                         'text': ''.join(texts[i] for i in ids)})
        cards = []
        for mid, title, date in conn.execute(
                'SELECT meeting_id, title, date FROM meetings ORDER BY date, meeting_id').fetchall():
            if not card_matches(query, title, date):
                continue
            doc = _doc_for(conn, mid, docs)
            if doc is not None:
                # 요약은 색인에 두지 않고 소스에서 읽는다.
                cards.append({'meeting_id': mid, 'title': title, 'date': date,
                              'summary': doc['summary'][:CARD_SUMMARY_CHARS]})
        dropped = sum(1 for v in docs.values() if v is None)
    return {'query_mode': mode, 'hits': hits, 'cards': cards, 'dropped_meetings': dropped}


def status(index_dir=None) -> Dict[str, Any]:
    """읽기 전용. 없는 색인을 만들지 않는다."""
    paths = _paths(index_dir)
    if not paths['fts'].exists():
        return {'exists': False, 'index': str(paths['fts']), 'meetings': 0, 'chunks': 0, 'vector': None}
    with closing(_open(paths['fts'], False)) as conn:
        meta = dict(conn.execute('SELECT key, value FROM meta'))
        out = {'exists': True, 'index': str(paths['fts']),
               'meetings': conn.execute('SELECT count(*) FROM meetings').fetchone()[0],
               'chunks': conn.execute('SELECT count(*) FROM chunks').fetchone()[0],
               'tokenizer': meta.get('tokenizer'), 'chunk_chars': int(meta.get('chunk_chars') or CHUNK_CHARS),
               'last_build': int(meta['last_build']) if 'last_build' in meta else None,
               'last_run': json.loads(meta['last_run']) if 'last_run' in meta else None}
    vector = None
    if paths['ids'].exists():
        try:
            ids = json.loads(paths['ids'].read_text(encoding='utf-8'))
            vector = {'model': ids.get('model'), 'vectors': len(ids.get('chunks', [])),
                      'current': ids.get('build_id') == meta.get('build_id')}
        except (OSError, ValueError):
            vector = {'error': 'unreadable'}
    out['vector'] = vector
    return out


# ---------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog='sb meeting', description='회의록 색인·회수')
    sub = parser.add_subparsers(dest='command', required=True)
    p_index = sub.add_parser('index', help='소스의 public 회의를 색인')
    p_index.add_argument('--source', action='append', required=True,
                         help='미팅노트 json·회의 폴더·txt/md 파일 또는 그 묶음 폴더(여러 번 가능)')
    p_index.add_argument('--index-dir', help='색인 폴더(기본 SB_HOME/index)')
    p_index.add_argument('--no-vector', action='store_true')
    p_search = sub.add_parser('search', help='질문으로 구간 회수')
    p_search.add_argument('query')
    p_search.add_argument('--k', type=int, default=3)
    p_search.add_argument('--index-dir')
    p_search.add_argument('--no-vector', action='store_true')
    p_search.add_argument('--fusion', choices=FUSIONS, default='vector',
                          help='벡터가 있을 때 순위 결합(기본 vector: 벡터 우선, rrf: FTS 와 RRF)')
    p_status = sub.add_parser('status', help='색인 상태(읽기 전용)')
    p_status.add_argument('--index-dir')
    args = parser.parse_args(argv)
    try:
        if args.command == 'index':
            result = index(args.source, args.index_dir, no_vector=args.no_vector)
        elif args.command == 'search':
            result = search(args.query, args.k, args.index_dir, no_vector=args.no_vector, fusion=args.fusion)
        else:
            result = status(args.index_dir)
    except (sqlite3.Error, OSError, ValueError, TimeoutError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
