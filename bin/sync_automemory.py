#!/usr/bin/env python3
"""Read-only automemory import. Run: python3 bin/sync_automemory.py --help."""

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sb_config import IS_WINDOWS, sb_path
from sb_memory import build_provenance, content_hash, dedup_key, find_saved_id, save_memory


# 긴 메모리 파일을 나누는 글자 수. 파일 통째 1관측이면 벡터 1개로 뭉쳐 묻히고, Ollama /v1/embeddings 는 2048토큰 뒤를
# 오류 없이 버린다(2026-11-06 실측). 800자: 긴 문서 질문 30개 3위 안 4→13, 일반 관측 질문 40개 악화 0.
DEFAULT_CHUNK_CHARS = 800
_BLOCK_START = re.compile(r'(?m)^(?=#{1,4} |\*\*[^\n]{2,80}\*\*\s*$|\*\*[^\n]{2,40}\*\*[:：])|\n\s*\n')


def chunk_chars() -> int:
    """SB_AUTOMEM_CHUNK_CHARS. 0 이면 나누지 않는다. 숫자가 아니거나 음수면 기본값."""
    try:
        value = int(os.environ.get('SB_AUTOMEM_CHUNK_CHARS', DEFAULT_CHUNK_CHARS))
    except ValueError:
        return DEFAULT_CHUNK_CHARS
    return value if value >= 0 else DEFAULT_CHUNK_CHARS


def _blocks(body: str) -> List[str]:
    """제목·굵은 글씨 줄·빈 줄 경계로 자른 블록."""
    edges = [0] + [match.start() for match in _BLOCK_START.finditer(body)] + [len(body)]
    return [body[start:end] for start, end in zip(edges, edges[1:]) if body[start:end].strip()]


def _pieces(block: str, cap: int) -> List[str]:
    """cap 을 넘는 블록을 줄 단위로, 그래도 넘는 줄은 글자 단위로 자른다."""
    if len(block) <= cap:
        return [block]
    return [line[start:start + cap] for line in block.splitlines(keepends=True)
            for start in range(0, len(line), cap)]


def split_body(body: str, cap: int) -> List[str]:
    """본문을 cap 글자 이하 조각으로 나눈다. 겹침 없이 앞에서부터 블록을 채운다. cap 0 이나 짧은 본문은 그대로."""
    if cap <= 0 or len(body) <= cap:
        return [body]
    parts: List[str] = []
    current = ''
    for piece in (piece for block in _blocks(body) for piece in _pieces(block, cap)):
        if current and len(current) + len(piece) > cap:
            parts.append(current.strip('\r\n'))
            current = ''
        current += piece
    parts.append(current.strip('\r\n'))
    return [part for part in parts if part.strip()]


def _home_slug() -> str:
    """Claude Code 프로젝트 디렉토리 슬러그 규칙(경로 구분자·점 → '-')으로 홈 경로를 변환."""
    return re.sub(r'[^A-Za-z0-9]', '-', str(Path.home()))


def project_from_slug(slug: str, home_slug: Optional[str] = None) -> str:
    """'<home slug>-<project>' → '<project>'.

    POSIX slugs start with '-' ('/Users/kim' → '-Users-kim'); Windows slugs start with
    the drive ('C:\\Users\\kim' → 'C--Users-kim'), so a slug that begins with the home
    slug is handled too. Windows compares case-insensitively (drive-letter case varies).
    """
    home = home_slug if home_slug is not None else _home_slug()
    fold = str.lower if IS_WINDOWS else str
    folded, folded_home = fold(slug), fold(home)
    if not (slug.startswith('-') or (folded_home and folded.startswith(folded_home))):
        return slug
    if folded == folded_home:
        return 'home'
    index = folded.rfind(folded_home + '-')
    if index < 0:
        return slug
    return slug[index + len(home) + 1:] or 'home'


def parse_memory_file(path: str, legacy_type: bool = True) -> Dict[str, Any]:
    """Read single-line scalar fields and the type (metadata.type, else legacy top-level type);
    preserve the body verbatim.

    Matching outer quotes are removed. Full YAML (lists, multiline scalars,
    escapes, anchors and inline comments) is deliberately unsupported.
    legacy_type=False reproduces the old parser (metadata.type only) — sync uses it to tell
    which already-synced files were stored as type=unknown.
    """
    with open(path, encoding='utf-8') as stream:
        text = stream.read()
    lines = text.splitlines(keepends=True)
    fields = {'name': Path(path).stem, 'description': '', 'type': 'unknown'}
    body = text
    if lines and lines[0].strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if end is not None:
            body = ''.join(lines[end + 1:])
            metadata = False
            for line in lines[1:end]:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                indented = line[0].isspace()
                if not indented:
                    metadata = stripped == 'metadata:'
                key, separator, value = stripped.partition(':')
                if not separator:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if not indented and key in ('name', 'description'):
                    fields[key] = value
                if indented and metadata and key == 'type':
                    fields['type'] = value or 'unknown'
                # 예전 형식은 type 을 최상위에 둔다(metadata 블록 없음). 이 형식이 전부 unknown 으로
                # 들어가던 것을 교정한다(2026-10-14 운영 실측 41건). 둘 다 있으면 metadata.type 이 우선.
                if legacy_type and not indented and key == 'type' and fields['type'] == 'unknown':
                    fields['type'] = value or 'unknown'
    return {**fields, 'body': body, 'title': fields['description'] or fields['name']}


def excluded_projects() -> set:
    """~/.secondbrain/config/automemory_exclude.txt 의 프로젝트명(한 줄에 하나)은 가져오지 않는다."""
    path = Path(sb_path('config', 'automemory_exclude.txt'))
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.startswith('#')}


def project_map() -> Dict[str, str]:
    """~/.secondbrain/config/automemory_project_map.json {"슬러그 프로젝트명": "claude-mem 프로젝트명"}.
    claude-mem 은 cwd 폴더 이름을 project 로 쓰므로, 같은 폴더의 기록이 한 이름으로 모이게 맞춘다."""
    path = Path(sb_path('config', 'automemory_project_map.json'))
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def projects_root(root: Optional[str] = None) -> Path:
    """Claude Code 프로젝트 폴더(자동 메모리는 그 아래 <slug>/memory/*.md). sb_rules 도 같은 기준을 쓴다."""
    return Path(os.path.abspath(os.path.expanduser(root or '~/.claude/projects')))


def temp_roots() -> List[str]:
    """임시 폴더 뿌리. macOS 는 /tmp·/var/folders 의 실체가 /private 아래라 둘 다 둔다. 윈도우는 %TEMP%
    (gettempdir). sb_rules 의 임시 cwd 판정도 이 목록을 써서 두 곳의 기준이 갈리지 않게 한다."""
    roots = ['/tmp', '/var/folders', '/private/tmp', '/private/var/folders', tempfile.gettempdir()]
    real = os.path.realpath(roots[-1])   # 윈도우 8.3 짧은 이름·macOS 심볼릭 링크를 푼 모양도 함께 본다
    return roots + ([real] if real != roots[-1] else [])


def is_temp_slug(slug: str) -> bool:
    """임시 폴더에서 연 세션(테스트·일회성 실행)의 프로젝트 슬러그인가 — 그 자동 메모리는 가져오지 않는다.
    슬러그는 cwd 의 영숫자 아닌 글자를 '-' 로 바꾼 것이라 뿌리도 같은 규칙으로 바꿔 앞부분을 비교한다."""
    fold = str.lower if IS_WINDOWS else str   # 윈도우는 드라이브 문자·폴더 대소문자가 섞인다
    for root in temp_roots():
        prefix = fold(re.sub(r'[^A-Za-z0-9]', '-', root).rstrip('-'))
        if prefix and (fold(slug) == prefix or fold(slug).startswith(prefix + '-')):
            return True
    return False


def discover(root: str = None) -> List[Tuple[str, str]]:
    base = projects_root(root)
    skip = excluded_projects()
    mapping = project_map()
    return [(mapping.get(project_from_slug(path.parent.parent.name), project_from_slug(path.parent.parent.name)),
             str(path))
            for path in sorted(base.glob('*/memory/*.md'))
            if path.name != 'MEMORY.md' and path.is_file()
            and project_from_slug(path.parent.parent.name) not in skip
            and not is_temp_slug(path.parent.parent.name)]


def memory_hash(memory: Dict[str, Any]) -> str:
    """변경 감지·중복 키(sid)용 해시. type 도 넣는다 — 파싱 교정으로 type 만 바뀐 파일이 같은 sid 로 막히지 않고
    다시 올라가게(운영 실측: 교정 뒤에도 type=unknown 관측 58건이 그대로 남았다)."""
    return content_hash(memory['body'], '{}\n{}'.format(memory['title'], memory['type']))


def _legacy_unchanged(path: str, memory: Dict[str, Any], previous: Dict[str, Any]) -> bool:
    """type 칸이 없는 예전 상태 항목: 본문·제목이 같고 type 도 예전 파서가 본 값 그대로면 바뀐 게 없다.
    예전 해시는 type 을 몰라 이걸 안 보면 모든 사용자가 첫 동기화에 전 파일을 다시 저장한다.
    예전 파서(metadata.type 만)가 unknown 으로 본 파일이 지금 다른 type 이면 그것만 다시 올린다."""
    return ('type' not in previous and previous.get('hash') == content_hash(memory['body'], memory['title'])
            and parse_memory_file(path, legacy_type=False)['type'] == memory['type'])


def _state_path(path: Optional[str] = None) -> Path:
    return Path(path or os.environ.get('SB_AUTOMEM_STATE') or
                sb_path('index', 'automemory_state.json')).expanduser()


def load_state(path: str = None) -> Dict[str, Any]:
    target = _state_path(path)
    try:
        with target.open(encoding='utf-8') as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict):
        raise ValueError('automemory state must be an object')
    return state


def save_state(state: Dict[str, Any], path: str = None) -> None:
    target = _state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=str(target.parent), delete=False) as stream:
            temporary = stream.name
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


class PartialSave(RuntimeError):
    """조각 저장이 중간에 멈췄다. ids 는 이미 저장된 조각 — 다음 판으로 넘어가면 숨겨야 한다."""

    def __init__(self, ids: List[int], cause: Exception) -> None:
        super().__init__(str(cause))
        self.ids = ids


def _retire_pending(entry: Dict[str, Any]) -> bool:
    """실패로 남은 조각 중 현재 판이 쓰지 않는 것을 supersedes 로 옮긴다. 옮길 게 있었으면 True."""
    pending = entry.pop('pending', [])
    current = entry.get('obs_ids', [])
    supersedes = entry.get('supersedes', [])
    entry['supersedes'] = supersedes + [i for i in pending if i not in current and i not in supersedes]
    return bool(pending)


def _save_parts(project: str, path: str, memory: Dict[str, Any], digest: str, parts: List[str],
                old_id: Optional[int], reuse_whole: bool, chunk_size: int) -> List[int]:
    """파일 하나를 관측 1건 이상으로 저장하고 id 목록을 돌려준다. 통째 저장이 이미 있으면 빈 목록
    (분할을 끄거나 줄여 예전 통째 기록으로 돌아가는 reuse_whole 이면 그 id).
    조각 저장은 중간에 실패해도 다음 실행이 이어간다 — 이미 저장된 조각은 dedup_key 로 id 를 찾아 다시 쓴다."""
    total = len(parts)
    title = '[automemory] {} · {}'.format(project, memory['title'])
    ids: List[int] = []
    for number, part in enumerate(parts, 1):
        whole = total == 1
        # 조각 크기를 넣는다 — 같은 내용을 다른 크기로 나눠 개수만 같을 때 옛 조각을 다시 쓰지 않게
        sid = path + '#' + digest if whole else '{}#{}#{}:{}/{}'.format(path, digest, chunk_size, number, total)
        prov = build_provenance(
            source='claude-code-automemory', kind='automemory-sync',
            origin='sync_automemory', created_by='secondbrain-batch',
            sid=sid, content_hash=digest, verification='user-authored',
            extra={'type': memory['type'], 'name': memory['name'], 'path': path, 'supersedes': old_id})
        head = '[자동 메모리 · type={} · file={}'.format(memory['type'], Path(path).name)
        # 벡터에는 본문만 들어가 제목을 본문 첫머리에도 둔다
        text = head + ']\n' + part if whole else '{} · 조각 {}/{}]\n{}\n{}'.format(
            head, number, total, memory['title'], part)
        part_title = title if whole else '{} ({}/{})'.format(title, number, total)
        try:
            obs_id = save_memory(text=text, title=part_title, project=project, prov=prov)
            if obs_id == -1:
                if whole and not reuse_whole:
                    return []
                obs_id = find_saved_id(dedup_key(prov, text, part_title))
                if obs_id is None:
                    raise RuntimeError('saved part not found: {}'.format(part_title))
        except (RuntimeError, sqlite3.Error) as exc:
            raise PartialSave(ids, exc) from exc
        ids.append(obs_id)
    return ids


def sync(root: str = None, state_path: str = None, dry_run: bool = False,
         only_project: Optional[str] = None) -> Dict[str, Any]:
    state = load_state(state_path)
    result = {'scanned': 0, 'saved': 0, 'unchanged': 0, 'skipped_dup': 0,
              'failed': 0, 'details': []}
    now = datetime.now(timezone.utc).isoformat()
    dirty = False
    cap = chunk_chars()
    for project, path in discover(root):
        if only_project is not None and project != only_project:
            continue
        result['scanned'] += 1
        detail = {'project': project, 'path': path}
        result['details'].append(detail)
        try:
            memory = parse_memory_file(path)
            digest = memory_hash(memory)
            previous = state.get(path, {})
            detail['hash'] = digest
            legacy = _legacy_unchanged(path, memory, previous)
            parts = split_body(memory['body'], cap)
            split = cap if len(parts) > 1 else 0
            if (previous.get('hash') == digest or legacy) and previous.get('split', 0) == split:
                result['unchanged'] += 1
                detail['action'] = 'unchanged'
                if not dry_run and legacy:
                    previous.update(hash=digest, type=memory['type'])   # 상태만 새 해시로 옮긴다(재저장 없음)
                    dirty = True
                if not dry_run and 'deleted_at' in previous:
                    del previous['deleted_at']
                    dirty = True
                if not dry_run and _retire_pending(previous):
                    dirty = True
                continue
            old_id = previous.get('obs_id')
            if dry_run:
                detail.update(action='would_save', supersedes=old_id)
                continue
            resplit = previous.get('split', 0) != split
            obs_ids = _save_parts(project, path, memory, digest, parts, old_id, resplit, split)
            if not obs_ids:
                result['skipped_dup'] += 1
                detail['action'] = 'skipped_dup'
                continue
            supersedes = list(previous.get('supersedes', []))
            supersedes += [i for i in previous.get('obs_ids', [old_id]) if i is not None and i not in supersedes]
            supersedes = [i for i in supersedes if i not in obs_ids]   # 다시 쓰는 기록은 숨기지 않는다
            state[path] = {'hash': digest, 'type': memory['type'], 'obs_id': obs_ids[0], 'obs_ids': obs_ids,
                           'split': split, 'synced_at': now, 'supersedes': supersedes,
                           'pending': previous.get('pending', [])}
            _retire_pending(state[path])
            dirty = True
            result['saved'] += 1
            detail.update(action='saved', obs_id=obs_ids[0], parts=len(obs_ids), supersedes=old_id)
        except PartialSave as exc:
            if exc.ids:
                pending = previous.get('pending', [])
                state[path] = {**previous, 'pending': pending + [i for i in exc.ids if i not in pending]}
                dirty = True
            result['failed'] += 1
            detail.update(action='failed', error=str(exc))
        except (RuntimeError, OSError, UnicodeError, sqlite3.Error) as exc:
            result['failed'] += 1
            detail.update(action='failed', error=str(exc))
    base = projects_root(root)
    for path, entry in state.items():
        source = Path(path)
        if (source.parent.name != 'memory' or source.parent.parent.parent != base
                or source.name == 'MEMORY.md' or source.suffix != '.md'):
            continue
        project = project_from_slug(source.parent.parent.name)
        if only_project is not None and project != only_project:
            continue
        if not source.exists() and 'deleted_at' not in entry:
            result['details'].append({'project': project, 'path': path,
                                      'action': 'would_mark_deleted' if dry_run else 'deleted'})
            if not dry_run:
                entry['deleted_at'] = now
                _retire_pending(entry)
                dirty = True
    if dirty:
        save_state(state, state_path)
    return result


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--root')
    parser.add_argument('--project')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = sync(root=args.root, dry_run=args.dry_run, only_project=args.project)
    except (OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        else:
            print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(' '.join('{}={}'.format(key, result[key]) for key in
                       ('scanned', 'saved', 'unchanged', 'skipped_dup', 'failed')))
        for detail in result['details']:
            print('{}: {}{}'.format(detail['action'], detail['path'],
                                   ' — ' + detail['error'] if 'error' in detail else ''))
    return int(result['failed'] > 0)


if __name__ == '__main__':
    sys.exit(main())
