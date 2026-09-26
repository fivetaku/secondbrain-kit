#!/usr/bin/env python3
"""scope_id 해석기 — 세컨브레인 전 층(loops·session_context·sb_memory·state)이 공유하는 단일 프로젝트 귀속 규칙.

우선순위 (memory-layer-refactor M2, 2026-09-16):
  1. realpath(cwd) 가 project_aliases.json 의 별칭/경로에 매칭되면 그 프로젝트 키
  2. git remote origin URL 이 있으면 정규화한 URL (동명 폴더·클론 위치 무관)
  3. 그 외에는 realpath 자체

절대 basename 으로 매칭하지 않는다 — /a/shared 와 /b/shared 는 다른 scope 다.

project_aliases.json 형식:
  { "<project_key>": ["<별칭 또는 절대경로>", ...], ... }
  값이 절대경로('/', '~', Windows 'C:\\...')면 경로(realpath 비교), 아니면 폴더 basename 별칭
  (단, 경로 항목이 하나도 없는 프로젝트에만 적용). Windows 에서는 경로 비교가 대소문자를 구분하지 않는다.
  기본 위치: $SB_HOME/config/project_aliases.json (SB_PROJECT_ALIASES 로 변경).

사용:
  sb_scope.py [<cwd>]          → scope_id 한 줄 출력
  sb_scope.py --json [<cwd>]   → {"scope_id":..., "method":..., "realpath":...}
"""
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sb_config  # noqa: E402

GLOBAL_SCOPE = 'global'


def aliases_path() -> str:
    return os.path.expanduser(os.environ.get('SB_PROJECT_ALIASES') or
                              sb_config.sb_path('config', 'project_aliases.json'))


def _load_aliases(path: Optional[str] = None) -> Dict[str, List[str]]:
    p = path or aliases_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding='utf-8') as source:
            data = json.load(source)
    except (OSError, ValueError):
        return {}
    return {k: (v if isinstance(v, list) else [v]) for k, v in data.items() if isinstance(k, str)}


def is_path_entry(entry) -> bool:
    """별칭 항목이 경로인가(이름 별칭이 아니라)."""
    return isinstance(entry, str) and sb_config.is_abs_like(entry)


def _norm_path(p: str) -> str:
    """비교용 경로 — realpath + normcase(Windows 는 대소문자·구분자 무시, POSIX 는 그대로)."""
    return os.path.normcase(os.path.realpath(os.path.expanduser(p)))


def path_within(child: str, root: str) -> bool:
    """child 가 root 와 같거나 그 하위인가 — 둘 다 _norm_path 를 거친 값이어야 한다."""
    if child == root:
        return True
    base = root if root.endswith(os.sep) else root + os.sep
    return child.startswith(base)


def _norm_name(s: str) -> str:
    return ''.join(s.lower().replace('_', '-').split())


def _git_remote(realpath: str) -> Optional[str]:
    try:
        r = subprocess.run(['git', '-C', realpath, 'config', '--get', 'remote.origin.url'],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    url = r.stdout.strip()
    if r.returncode != 0 or not url:
        return None
    return normalize_git_url(url)


def normalize_git_url(url: str) -> str:
    """git@host:org/repo.git · https://host/org/repo.git · ssh://git@host/org/repo → host/org/repo"""
    u = url.strip()
    u = re.sub(r'^[a-z+]+://', '', u)          # scheme
    u = re.sub(r'^[^@/]+@', '', u)             # user@
    u = u.replace(':', '/', 1) if re.match(r'^[^/]+:[^/]', u) else u
    u = re.sub(r'\.git/?$', '', u)
    return u.lower().rstrip('/')


def resolve_scope_id(cwd: Optional[str] = None, aliases: Optional[Dict[str, List[str]]] = None,
                     use_git: bool = True) -> Tuple[str, str, str]:
    """returns (scope_id, method, realpath). method ∈ alias-path | alias-name | git | realpath"""
    real = os.path.realpath(cwd or os.getcwd())
    real_cmp = os.path.normcase(real)
    table = _load_aliases() if aliases is None else aliases
    # 1) 경로 별칭 (가장 긴 prefix 우선 — 하위 폴더는 상위 프로젝트에 귀속)
    best: Tuple[int, Optional[str]] = (-1, None)
    name_only: Dict[str, List[str]] = {}
    for key, entries in table.items():
        paths = [_norm_path(e) for e in entries if is_path_entry(e)]
        names = [e for e in entries if isinstance(e, str) and not is_path_entry(e)]
        for p in paths:
            if path_within(real_cmp, p):
                if len(p) > best[0]:
                    best = (len(p), key)
        if not paths and names:
            name_only[key] = names
    if best[1]:
        return best[1], 'alias-path', real
    # 1b) 이름 별칭 — 경로 항목이 전혀 없는 레거시 프로젝트에만 (동명 충돌 시 미적용)
    base = _norm_name(os.path.basename(real))
    hits = [k for k, names in name_only.items() if base in {_norm_name(n) for n in names}]
    if len(hits) == 1:
        return hits[0], 'alias-name', real
    # 2) git remote
    if use_git:
        remote = _git_remote(real)
        if remote:
            return remote, 'git', real
    # 3) realpath
    return real, 'realpath', real


def require_scope(scope_id: Optional[str]) -> str:
    """쓰기 경로용: scope_id 가 비었으면 거부. 'global' 은 명시적 전역 의사표시로 허용."""
    if not scope_id or not str(scope_id).strip():
        raise ValueError('scope_id required (pass an explicit scope_id or "global"); omission is an error')
    return str(scope_id).strip()


def loop_matches_scope(loop_project: Optional[str], scope_id: str) -> bool:
    """미결의 project 값이 현재 scope 에 속하는가.

    허용: 정확 일치, 또는 표기 정규화(대소문자·'_'↔'-'·공백) 후 일치.
    금지: basename 폴백 — project 이름이 폴더 이름과 우연히 같다는 이유로 붙이지 않는다.
    레거시 이름 project 는 project_aliases.json 에 경로를 등록해 resolve_scope_id 가 그 키를
    돌려주게 만들어서 연결한다.
    """
    if not loop_project or not scope_id:
        return False
    return _norm_name(str(loop_project)) == _norm_name(str(scope_id))


def main(argv: List[str]) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 파이프(cp949 등)에서도 한글·이모지 출력
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    as_json = '--json' in argv
    args = [a for a in argv if a != '--json']
    scope_id, method, real = resolve_scope_id(args[0] if args else None)
    if as_json:
        print(json.dumps({'scope_id': scope_id, 'method': method, 'realpath': real}, ensure_ascii=False))
    else:
        print(scope_id)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
