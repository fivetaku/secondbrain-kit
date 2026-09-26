#!/usr/bin/env python3
"""Read-only automemory import. Run: python3 bin/sync_automemory.py --help."""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sb_config import IS_WINDOWS, sb_path
from sb_memory import build_provenance, content_hash, save_memory


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


def parse_memory_file(path: str) -> Dict[str, Any]:
    """Read single-line scalar fields and metadata.type; preserve the body verbatim.

    Matching outer quotes are removed. Full YAML (lists, multiline scalars,
    escapes, anchors and inline comments) is deliberately unsupported.
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
    return {**fields, 'body': body, 'title': fields['description'] or fields['name']}


def discover(root: str = None) -> List[Tuple[str, str]]:
    base = Path(os.path.abspath(os.path.expanduser(root or '~/.claude/projects')))
    return [(project_from_slug(path.parent.parent.name), str(path))
            for path in sorted(base.glob('*/memory/*.md'))
            if path.name != 'MEMORY.md' and path.is_file()]


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


def sync(root: str = None, state_path: str = None, dry_run: bool = False,
         only_project: Optional[str] = None) -> Dict[str, Any]:
    state = load_state(state_path)
    result = {'scanned': 0, 'saved': 0, 'unchanged': 0, 'skipped_dup': 0,
              'failed': 0, 'details': []}
    now = datetime.now(timezone.utc).isoformat()
    dirty = False
    for project, path in discover(root):
        if only_project is not None and project != only_project:
            continue
        result['scanned'] += 1
        detail = {'project': project, 'path': path}
        result['details'].append(detail)
        try:
            memory = parse_memory_file(path)
            digest = content_hash(memory['body'], memory['title'])
            previous = state.get(path, {})
            detail['hash'] = digest
            if previous.get('hash') == digest:
                result['unchanged'] += 1
                detail['action'] = 'unchanged'
                if not dry_run and 'deleted_at' in previous:
                    del previous['deleted_at']
                    dirty = True
                continue
            old_id = previous.get('obs_id')
            if dry_run:
                detail.update(action='would_save', supersedes=old_id)
                continue
            prov = build_provenance(
                source='claude-code-automemory', kind='automemory-sync',
                origin='sync_automemory', created_by='secondbrain-batch',
                sid=path + '#' + digest, content_hash=digest,
                verification='user-authored',
                extra={'type': memory['type'], 'name': memory['name'],
                       'path': path, 'supersedes': old_id})
            obs_id = save_memory(
                text='[자동 메모리 · type={} · file={}]\n{}'.format(
                    memory['type'], Path(path).name, memory['body']),
                title='[automemory] {} · {}'.format(project, memory['title']),
                project=project, prov=prov)
            if obs_id == -1:
                result['skipped_dup'] += 1
                detail['action'] = 'skipped_dup'
                continue
            supersedes = list(previous.get('supersedes', []))
            if old_id is not None and old_id not in supersedes:
                supersedes.append(old_id)
            state[path] = {'hash': digest, 'obs_id': obs_id, 'synced_at': now,
                           'supersedes': supersedes}
            dirty = True
            result['saved'] += 1
            detail.update(action='saved', obs_id=obs_id, supersedes=old_id)
        except (RuntimeError, OSError, UnicodeError) as exc:
            result['failed'] += 1
            detail.update(action='failed', error=str(exc))
    base = Path(os.path.abspath(os.path.expanduser(root or '~/.claude/projects')))
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
