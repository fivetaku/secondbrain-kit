#!/usr/bin/env python3
"""claude-mem 버전과 chroma-mcp 실행 인자의 기준값을 한 곳에 둔다(설치기·doctor·sb health 가 같이 쓴다).

claude-mem 워커는 벡터 동기화용 chroma-mcp 를 자기 인자로 띄운다(worker-service.cjs 의 buildLauncherPrefix):
  uvx --python <py> --with <요구 1> --with <요구 2> ... --from chroma-mcp==<버전> chroma-mcp
이 인자는 claude-mem 버전마다 다르다(2026-11-15, npm 18개 버전 비교):
  13.24.x~13.28.x  onnxruntime>=1.20, protobuf<7
  13.29.0~13.34.x  + chromadb==1.5.9
  13.35.0 이상     + pydantic<2.14 (본가가 직접 고정)
키트는 이 값을 복사해 두지 않고, 설치된 플러그인의 worker-service.cjs 에서 읽는다. 읽지 못하면 아래 기본값을 쓰고
ok=False 를 돌려준다 — 경고는 부르는 쪽이 낸다.

pydantic<2.14 는 일부러 고정하는 값이다: chroma-mcp 0.2.6 이 고정한 mcp 1.6.0 이 pydantic 2.14 에서 사라진
_typing_extra.eval_type_backport 를 import 해 시작 즉시 죽는다(2026-10-12 벡터 동기화 4시간+ 정지).

  python bin/sb_claude_mem.py          → 설치판·검증 범위·워커 인자 요약(JSON)
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# 설치기가 새로 깔 때 쓰는 권장판(오너 맥 실가동판). 바꾸면 TESTED·tests/fixtures/claude-mem 도 함께 본다.
RECOMMENDED = '13.34.2'
# 키트와 함께 돌려 본 범위(양끝 포함) — 13.24.23 은 예전 설치기 기본판, 13.34.2 는 오너 맥 실가동판.
# 밖이면 doctor·sb health 가 경고만 한다(차단 없음). 워커 인자 파서 자체는 13.24.23~15.18.3 의 npm 패키지 21개에서
# 같은 구조로 읽혔다(2026-11-15) — 그래도 캡처·관측기·벡터 동기화까지 함께 돌려 본 범위는 여기까지다.
# 이 범위 밖에서 읽기 경로만 확인한 판은 READ_CHECKED 에 따로 적는다.
TESTED = ('13.24.23', '13.34.2')
# TESTED 밖이지만 키트의 '읽기 경로'만 확인한 판(정확한 판 번호만, 범위 아님). 캡처·관측기·벡터 동기화는 확인하지 않았으므로
# TESTED 를 넓히지 않고 doctor·sb health 경고 문구만 정확하게 한다.
#   15.18.3 (2026-11-16): npm 패키지의 SessionStore 마이그레이션으로 격리 홈에 빈 DB(schema 75)를 만들고 FTS 정의를
#   더해 키트의 DB 읽기 25곳(sb_search·sb_health·consolidate·sb_vault·sb_sources·sb_pii_sweep·sb_fts_ko·훅·doctor 등)을
#   돌려 모두 통과. restart 명령·/api/health pid·기동 백필·/api/memory/save 본문 형식은 정적 대조로 확인.
READ_CHECKED = ('15.18.3',)
PYDANTIC_PIN = 'pydantic<2.14'
# 파싱 실패 시 기본값 = 권장판의 워커 인자
FALLBACK_WITH = ['onnxruntime>=1.20', 'protobuf<7', 'chromadb==1.5.9']
FALLBACK_CHROMA_MCP = '0.2.6'
DEFAULT_PYTHON = '3.13'


def vkey(v: str) -> Tuple[int, ...]:
    """'13.34.2' → (13, 34, 2, 1). 끝 자리는 안정판 1, 사전판(-beta 등) 0 — 같은 숫자면 안정판이 높다. 못 읽으면 ()."""
    raw = str(v).strip().lstrip('v')
    base = re.split(r'[-+]', raw)[0]
    try:
        nums = tuple(int(x) for x in base.split('.'))
    except ValueError:
        return ()
    return nums + (0 if '-' in raw else 1,)


def in_tested(v: Optional[str]) -> bool:
    k = vkey(v or '')
    return bool(k) and vkey(TESTED[0])[:-1] <= k[:-1] <= vkey(TESTED[1])[:-1]


def read_checked(v: Optional[str]) -> bool:
    """TESTED 밖이지만 키트 읽기 경로만 확인한 판인가(READ_CHECKED 와 같은 판 — 사전판은 따로 보지 않는다)."""
    k = vkey(v or '')
    return bool(k) and any(k == vkey(r) for r in READ_CHECKED)


def plugin_base() -> Path:
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude').expanduser() / 'plugins' / 'cache' / 'thedotmack' / 'claude-mem'


def _usable(d: Path) -> bool:
    return d.is_dir() and not (d / '.orphaned_at').exists() and (d / 'scripts' / 'worker-service.cjs').is_file()


def active_root() -> Optional[Path]:
    """Claude Code 플러그인 등록(installed_plugins.json)이 가리키는 claude-mem 설치 경로. 없거나 못 쓰면 None."""
    reg = plugin_base().parents[2] / 'installed_plugins.json'
    try:
        data = json.loads(reg.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    cache = os.path.realpath(plugin_base())
    found = []
    plugins = data.get('plugins') if isinstance(data, dict) else None
    if not isinstance(plugins, dict):   # 형식이 다른 등록 파일은 없는 것으로 보고 캐시에서 고른다
        return None
    for key, entries in plugins.items():
        if not str(key).startswith('claude-mem@'):
            continue
        for e in entries if isinstance(entries, list) else [entries]:
            if not isinstance(e, dict):
                continue
            p = Path(str(e.get('installPath') or '')).expanduser()
            # 플러그인 캐시 안의 경로만 믿는다 — 등록 파일이 엉뚱한 곳을 가리키면 쓰지 않는다
            if p.name and _usable(p) and os.path.realpath(p).startswith(cache + os.sep):
                found.append(p)
    if len(found) > 1:   # 여러 판이 등록돼 있으면 가장 높은 판(같은 숫자면 안정판)
        found.sort(key=lambda d: vkey(d.name))
    return found[-1] if found else None


def plugin_root() -> Optional[Path]:
    """실제로 쓰이는 claude-mem 플러그인 폴더. 등록 정보가 가리키는 판이 먼저이고, 없으면 캐시에서 가장 높은
    판(고아 표시·scripts 없는 폴더 제외, 같은 숫자면 안정판)."""
    act = active_root()
    if act:
        return act
    base = plugin_base()
    best = None
    for d in sorted(base.glob('*')) if base.is_dir() else []:
        if not _usable(d):
            continue
        key = vkey(d.name)
        if not key:
            continue
        if best is None or key > best[0]:
            best = (key, d)
    return best[1] if best else None


def installed_version(root: Optional[Path] = None) -> Optional[str]:
    root = root or plugin_root()
    return root.name if root else None


_PREFIX = re.compile(r'static buildLauncherPrefix\(\w+\)\{(.{0,1200}?)\}(?=static\b|\}|$)', re.S)
_VER = r'([0-9]+(?:\.[0-9]+)*(?:[-+.][0-9A-Za-z.]+)?)'


def _defs(text: str, name: str, pattern: str) -> List[str]:
    return re.findall(r'(?<![\w$.])' + re.escape(name) + pattern, text)


def parse_worker_spec(text: str) -> Tuple[List[str], str, bool]:
    """worker-service.cjs 본문에서 (워커 --with 목록, chroma-mcp 버전, 읽기 성공).
    모호하면(메서드가 둘 이상, 변수 정의가 둘 이상, 정의를 못 찾음) 기본값과 False — 틀린 값을 성공으로 내지 않는다.
    npm 18개 판(13.24.23~15.18.3) 모두 메서드 1개·정의 1개씩이었다(2026-11-15 실측)."""
    fail = (list(FALLBACK_WITH), FALLBACK_CHROMA_MCP, False)
    text = text or ''
    found = _PREFIX.findall(text)
    if len(found) != 1:
        return fail
    body = found[0]
    # 본문에 return·chroma-mcp 지정·배열 펼치기가 각각 하나여야 한다(분기마다 다른 인자를 돌려주면 모른다)
    if len(re.findall(r'\breturn\b', body)) != 1 or len(re.findall(r'chroma-mcp==', body)) != 1 \
            or len(re.findall(r'\.flatMap\(', body)) > 1:
        return fail
    vm = re.search(r'chroma-mcp==\$\{([\w$]+)\}', body)
    if vm:
        vals = _defs(text, vm.group(1), r'="' + _VER + '"')
        if len(_defs(text, vm.group(1), r'=')) != 1 or len(vals) != 1:
            return fail
        ver = vals[0]
    else:
        lm = re.search(r'chroma-mcp==' + _VER + r'["`]', body)
        if not lm:
            return fail
        ver = lm.group(1)
    fm = re.search(r'([\w$]+)\.flatMap\(', body)
    if fm:
        arrs = _defs(text, fm.group(1), r'=\[((?:"[^"\\]*",?)*)\]')
        if len(_defs(text, fm.group(1), r'=')) != 1 or len(arrs) != 1:
            return fail
        if _defs(text, fm.group(1), r'\.(?:push|unshift|splice|concat|length\s*=)\b'):
            return fail   # 정의 뒤에 배열을 바꾸는 코드가 있으면 실제 실행 값을 알 수 없다
        withs = re.findall(r'"([^"\\]*)"', arrs[0])
    else:
        # 압축기가 상수를 인라인한 꼴만 받는다: return["--python",<변수>,("--with","…",)*"--from","chroma-mcp==…","chroma-mcp"]
        # 주석·함수 호출·펼치기·템플릿 같은 다른 토큰이 하나라도 있으면 실제 값을 알 수 없으므로 실패로 본다.
        dm = re.fullmatch(r'\s*return\s*\[\s*"--python"\s*,\s*[\w$]+\s*,((?:\s*"--with"\s*,\s*"[^"\\]*"\s*,)+)'
                          r'\s*"--from"\s*,\s*"chroma-mcp==' + _VER + r'"\s*,\s*"chroma-mcp"\s*\]\s*;?\s*', body)
        if not dm:
            return fail
        withs = re.findall(r'"--with"\s*,\s*"([^"\\]*)"', dm.group(1))
    if not vkey(ver):
        return fail
    return withs, ver, True


def worker_chroma_spec(root: Optional[Path] = None) -> Tuple[List[str], str, bool]:
    """설치된 워커의 (--with 목록, chroma-mcp 버전, 읽기 성공). 플러그인이 없거나 못 읽으면 기본값과 False."""
    root = root or plugin_root()
    if not root:
        return list(FALLBACK_WITH), FALLBACK_CHROMA_MCP, False
    try:
        text = (root / 'scripts' / 'worker-service.cjs').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return list(FALLBACK_WITH), FALLBACK_CHROMA_MCP, False
    return parse_worker_spec(text)


def python_version(settings: Optional[dict] = None) -> str:
    """워커가 쓰는 파이썬 버전 — 워커와 같은 규칙(환경변수 > claude-mem 설정 > 3.13)."""
    s = settings or {}
    v = os.environ.get('CLAUDE_MEM_PYTHON_VERSION') or s.get('CLAUDE_MEM_PYTHON_VERSION') or DEFAULT_PYTHON
    return str(v).strip() or DEFAULT_PYTHON   # 설정 JSON 의 숫자(3.12)도 문자열로


def worker_args(withs: List[str], chroma_ver: str) -> List[str]:
    """워커와 같은 uvx 인자(--python 은 부르는 쪽이 앞에 붙인다)."""
    out: List[str] = []
    for w in withs:
        out += ['--with', w]
    return out + ['--from', 'chroma-mcp==%s' % chroma_ver, 'chroma-mcp']


def _pydantic_safe(req: str) -> bool:
    """이 요구 하나만으로 pydantic 2.14 미만이 보장되는가(보수적으로 해석 — 모르면 False).
    환경 표시(; python_version<"3.13" 같은)가 붙은 요구는 지금 파이썬에서 적용되는지 알 수 없어 보장으로 치지 않는다
    — 그때는 pydantic 고정을 따로 더한다."""
    if ';' in req:
        return False
    r = re.sub(r'\s+', '', req)
    m = re.match(r'pydantic(?:\[[^\]]*\])?(.*)$', r, re.I)
    if not m:
        return False
    for spec in m.group(1).split(','):
        # 숫자로만 된 판(2.13, 2.13.5)만 읽는다. rc·post·dev 같은 꼬리나 ~=(호환 판, 2.14 이상도 허용할 수 있음)는
        # 2.14 미만을 보장한다고 보지 않는다.
        mm = re.fullmatch(r'(<=|<|==)(\d+)\.(\d+)(?:\.(\d+))?', spec)
        if not mm:
            continue
        op, major, minor = mm.group(1), int(mm.group(2)), int(mm.group(3))
        patch = int(mm.group(4) or 0)
        if op == '<' and (major, minor, patch) <= (2, 14, 0):
            return True
        if op in ('<=', '==') and (major, minor) < (2, 14):
            return True
    return False


def tool_requirements(withs: List[str]) -> List[str]:
    """고정 도구에 넣을 요구 = 워커 요구 전부 + pydantic 고정(워커 요구가 이미 2.14 미만을 보장할 때만 생략).
    uvx 는 이름이 같은 도구가 워커의 모든 --with 를 이미 만족할 때만 그 도구를 재사용한다(2026-11-15 격리 실측).
    하나라도 빠지면 경고 없이 새 환경을 풀고 pydantic 최신판을 받는다. 워커가 pydantic>=2.14 를 요구하면
    두 요구가 충돌해 uv tool install 이 실패하고, 설치기는 경고를 남긴다."""
    reqs = list(withs)
    if not any(_pydantic_safe(w) for w in reqs):
        reqs.append(PYDANTIC_PIN)
    return reqs


def pinned_chromadb(withs: List[str]) -> Optional[str]:
    for w in withs:
        m = re.match(r'chromadb==([\w.+-]+)$', re.sub(r'\s+', '', w.split(';')[0]))
        if m:
            return m.group(1)
    return None


def summary() -> dict:
    root = plugin_root()
    withs, ver, ok = worker_chroma_spec(root)
    v = installed_version(root)
    return {'installed': v, 'recommended': RECOMMENDED, 'tested': list(TESTED), 'in_tested': in_tested(v),
            'read_checked': read_checked(v),
            'worker_with': withs, 'chroma_mcp': ver, 'parsed': ok, 'tool_requirements': tool_requirements(withs)}


if __name__ == '__main__':
    print(json.dumps(summary(), ensure_ascii=False, indent=1))
    sys.exit(0)
