#!/usr/bin/env python3
"""세컨브레인 자료 목록: 과거 에이전트 세션·사용자가 알려 준 폴더·링크를 등록하고 분류한다(셋업 2·3단계).

  sb sources detect [--json]                 과거 세션 위치·MCP 커넥터 이름 감지(작업 폴더·시각·건수·이름만, 저장 안 함)
  sb sources add LOCATION [--kind K] [--json]  폴더·회의 폴더·링크 등록 → $SB_HOME/sources.json
  sb sources list [--json]                   등록된 자료 목록
  sb sources remove ID|LOCATION [--json]     add 로 넣은 자료 빼기
  sb sources select ID [--off] [--json]      감지된 세션 소스를 적재 대상으로 고르기(고르기 전에는 skip)

원칙
- 세션 파일에서는 작업 폴더(cwd)·파일 시각·건수만 읽는다. 대화 본문 필드는 해석하지 않고 저장하지 않는다.
- 폴더는 파일 이름·확장자·크기만 본다(내용을 열지 않는다). 민감도를 모르는 자료는 기본으로 뺀다.
- 3단계 표본은 관측 id·판정만 남긴다(제목·본문 없음).
- MCP 커넥터는 설정 파일의 서버 이름만 읽는다(주소·명령·인자·env·토큰은 읽지도 저장하지도 않는다). 적재하지 않는다(live).
"""
import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import sb_config  # noqa: E402
import sb_scope  # noqa: E402
import sb_vault  # noqa: E402
import sync_automemory  # noqa: E402

SCHEMA = 1
DEFAULT_SEED = 20261116            # sb_setup.SAMPLE_SEED 와 같은 값
SAMPLE_N = 50
HEAD_BYTES = 65536                 # 세션 파일에서 cwd 를 찾을 때 읽는 앞부분 상한(본문 전체를 읽지 않는다)
# claude-mem 관측기가 남기는 자체 세션(사용자 작업이 아님). 다른 PC 보고서(11-16)에서 Claude 세션의 64% 였다
OBSERVER_SLUG_MARKERS = ('-claude-mem-observer-sessions', '-claude-mem-work-view')
# 작업 폴더 나누기(보고만 — 회수·적재는 그대로). 평가 복사본: 경로에 이 조각이 든 폴더, 또는 같은 이름 원본이 따로 있는 폴더
EVAL_MARKERS = ('sbeval', 'smoketest')
WIN_TEMP_PARTS = ('appdata', 'local', 'temp')   # 다른 OS 에서 읽은 윈도우 경로(C:\…\AppData\Local\Temp)도 임시로
# MCP 커넥터 이름 → 알려진 종류(앞에서부터 처음 맞는 것). 이름만 보고 정한다
# microsoft 를 google_drive 앞에 둔다 — 'onedrive' 에도 'drive' 가 들어 있다
MCP_CATEGORIES = (('slack', ('slack',)), ('atlassian', ('atlassian', 'confluence', 'jira')),
                  ('microsoft', ('outlook', 'teams', 'sharepoint', 'onedrive')),
                  ('google_drive', ('google-drive', 'google_drive', 'googledrive', 'gdrive', 'drive')),
                  ('notion', ('notion',)), ('github', ('github',)), ('gitlab', ('gitlab',)), ('linear', ('linear',)),
                  ('figma', ('figma',)), ('gmail', ('gmail',)), ('calendar', ('calendar', 'gcal')))
MCP_NAME_MAX = 80
CODEX_MCP_RE = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:"([^"\n]+)"|\'([^\'\n]+)\'|([A-Za-z0-9_-]+))', re.MULTILINE)
CLAUDE_FILES_PER_DIR = 5           # Claude 프로젝트 폴더는 cwd 하나에서 나온 슬러그라 앞 몇 개만 열어 본다
MAX_FILES = 200000                 # 폴더 하나에서 세는 파일 상한(넘으면 truncated)
OWN_EXT = {'.md', '.txt', '.docx', '.pptx', '.pdf', '.hwp', '.hwpx', '.html', '.htm'}
OWN_MIN, OWN_MAX = 1024, 20 * 1024 * 1024
JUNK_DIRS = {'node_modules', '.git', '.hg', '.svn', '__pycache__', '.cache', 'cache', 'caches', '.venv', 'venv',
             '.tox', '.mypy_cache', '.pytest_cache', '.idea', '.vscode', '.next', '.gradle', 'tmp', 'temp', '.tmp',
             '.trash', '$recycle.bin'}
JUNK_EXT = {'.dmg', '.pkg', '.exe', '.msi', '.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.7z', '.rar', '.iso',
            '.tmp', '.temp', '.swp', '.swo', '.part', '.crdownload', '.download', '.pyc', '.o', '.class', '.lock'}
JUNK_NAMES = {'.ds_store', 'thumbs.db', 'desktop.ini'}
# 파일·폴더 이름의 민감 신호. 이름은 NFC 로 맞춘 뒤 본다(맥 zip 해제본·옛 HFS+ 복사본은 NFD).
# 모르면 빼는 쪽이 원칙이라 넓게 잡는다 — own_output 이 sensitive 로 잘못 빠지는 것은 손해가 작다.
SENSITIVE_KO = ('세금', '계약', '급여', '주민', '계좌', '견적', '원천징수', '연봉', '여권', '신분증', '통장', '등본',
                '비밀번호', '비번', '패스워드', '인감', '세무', '부가세', '종합소득', '연말정산', '영수증', '청구서',
                '인보이스', '거래명세', '급여명세', '카드명세', '이력서', '경력증명', '재직증명', '성적증명', '졸업증명',
                '가족관계', '혼인', '진단서', '처방', '진료', '건강검진', '의료', '보험', '대출', '신용', '면담',
                '인사평가', '사업자등록')
# 영어: 오탐이 드문 단어는 붙여 쓴 이름·복수형까지 부분 문자열로(mycontract·passwords·salaryslip),
# 짧아서 다른 단어에 섞이는 것은 앞쪽 경계만(taxreturn 은 잡고 syntax·taxonomy·taxi 는 뺀다).
SENSITIVE_EN = re.compile(
    r'(invoice|contract|salary|salaries|payroll|payslip|passport|password|passwd|credential|secret|receipt|'
    r'private[_-]?key|id[_-]?rsa|api[_-]?keys?|access[_-]?keys?|apikey|'
    r'(?<![a-z])tax(?!on|i)|(?<![a-z])bank|(?<![a-z])ssn(?![a-z])|(?<![a-z])tokens?(?![a-z])|'
    r'(?<![a-z])resume(?![a-z]))')
SENSITIVE_EXT = {'.pem', '.key', '.p12', '.pfx', '.kdbx', '.env'}
EXT_RE = re.compile(r'^\.[a-z0-9]{1,10}$')     # 통계에 쓰는 확장자 꼴(아니면 (other) — 파일 이름 조각이 새지 않게)
# 링크에서 지우는 비밀값: userinfo(아이디:비밀번호@)와, 이름에 이 조각이 든 쿼리(sig·X-Amz-Signature·access_token 등)
SECRET_PARAM = re.compile(r'(sig|token|key|secret|pass|pwd|auth|credential|session|code|x-amz-|x-goog-)', re.IGNORECASE)
MEETING_NAMES = ('회의', '회의록', 'meeting', 'meetings', 'minutes', 'meetnote')
BODY_KEYS = ('text', 'content', 'prompt')
SESSION_KINDS = ('claude_sessions', 'codex_sessions', 'gemini_sessions', 'omo_sessions')
CWD_RE = re.compile(rb'"(?:cwd|directory)"\s*:\s*"((?:[^"\\]|\\.)*)"')
# 한 줄에서 이 키들 뒤에 나온 "cwd" 는 본문(도구 결과 안의 JSON 등)일 수 있어 쓰지 않는다
BODY_BEFORE_RE = re.compile(rb'"(?:content|message|text|output|prompt|result|toolUseResult|tool_result|summary|'
                            rb'base_instructions|instructions|parts)"\s*:')

# 3단계 소음 휴리스틱. 근거: obs-value-triage_20261109/FINDINGS.md — 메타데이터(type·길이) 규칙은 분리력이 약하고
# 내용 유형 transient_status(상태 확인·대기·재시도·단순 열람)만 소음 84%로 강하게 갈렸다 → 그 유형만 제목으로 잡고 나머지는 unjudged.
NOISE_TITLE = re.compile(
    r'(상태\s*(확인|점검|조회)|대기\s*중|기다리는\s*중|재시도|다시\s*시도|진행\s*중|실행\s*중|폴링|'
    r'파일\s*(읽기|열람)|읽어\s*봄|목록\s*(확인|조회|출력)|디렉(터|토)리\s*(목록|구조\s*확인)|출력\s*확인|'
    r'\bstatus\s+check|\bchecking\s+status|\bwaiting\b|\bpolling\b|\bretry(ing)?\b|\bretried\b|\bstill\s+running\b|'
    r'\bin\s+progress\b|^\s*read(ing)?\s+(the\s+)?file|\bdirectory\s+listing\b|\bfile\s+listing\b|\blisted\s+files\b)',
    re.IGNORECASE)
OK_TYPES = {'decision', 'bugfix', 'feature', 'refactor', 'fix', 'gotcha', 'pattern'}
OK_TITLE = re.compile(r'(원인|해결|결정|확정|절차|규칙|교훈|해법|우회|root\s*cause|\bfixed\b|\bdecid|\bdecision\b|'
                      r'\bprocedure\b|\bhow\s+to\b|\bworkaround\b|\bgotcha\b)', re.IGNORECASE)
NOISE_DUP_MAX, SCOPE_MISMATCH_MAX, UNJUDGED_MAX = 0.20, 0.05, 0.30
JUDGE_VALUES = ('ok', 'noise', 'dup')
# 이 판에는 관측 소음·중복을 자동으로 접는 도구가 없다 — 고칠 수 있는 것(판정 정정·넘김)만 안내한다
NOISE_HINT = ('소음+중복 %.1f%% — 이 키트 판에는 관측 소음·중복 자동 정리가 없습니다. 판정이 틀렸으면 '
              'sb setup judge --stage 3 로 고치고, 그대로 쓰려면 sb setup accept 3 --reason "<이유>"')


# ---------------------------------------------------------------- 경로·파일

def sources_path() -> Path:
    return sb_config.home() / 'sources.json'


def logs_dir() -> Path:
    return sb_config.home() / 'logs'


def private_dir(d: Path) -> Path:
    """본인만 여는 폴더. 새로 만들 때만 0700 으로 만든다(이미 있는 폴더 권한은 바꾸지 않는다)."""
    for x in (sb_config.home(), d):
        if not x.exists():
            x.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _empty() -> Dict[str, Any]:
    return {'version': SCHEMA, 'sources': []}


def load() -> Dict[str, Any]:
    """sources.json. 없으면 빈 목록. 깨졌으면 덮어쓰지 않고 멈춘다(사용자 기록 보호)."""
    p = sources_path()
    if not p.exists():
        return _empty()
    data = sb_vault.read_json_file(p)
    if not isinstance(data, dict) or not isinstance(data.get('sources', []), list):
        raise SystemExit('%s 를 읽을 수 없어 멈췄습니다 — 파일을 고치거나 옮긴 뒤 다시 하세요(덮어쓰지 않았습니다)'
                         % sb_vault._short(str(p)))
    doc = _empty()
    doc.update(data)
    doc['sources'] = [s for s in doc.get('sources') or [] if isinstance(s, dict)]
    return doc


def save(doc: Dict[str, Any]) -> Path:
    bad = body_keys(doc)
    if bad:   # 본문 필드가 섞여 들어오면 쓰지 않는다
        raise ValueError('sources.json 에 본문 필드를 쓰지 않습니다: %s' % ', '.join(sorted(set(bad))))
    out = {'version': SCHEMA, 'sources': list(doc.get('sources') or [])}
    p = sources_path()
    private_dir(p.parent)
    sb_vault._atomic_write(p, json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + '\n', private=True,
                           mode=0o600)
    return p


def body_keys(obj: Any) -> List[str]:
    """중첩된 dict 어디에든 'text'·'content'·'prompt' 키가 있으면 그 이름들."""
    found: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in BODY_KEYS:
                found.append(k)
            found.extend(body_keys(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(body_keys(v))
    return found


def _sid(kind: str, location: str) -> str:
    return '%s-%s' % (kind, hashlib.sha256(('%s\0%s' % (kind, location)).encode('utf-8')).hexdigest()[:10])


def _date(epoch: Optional[float]) -> Optional[str]:
    return time.strftime('%Y-%m-%d', time.localtime(epoch)) if epoch else None


def _cmp(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(p)))


# ---------------------------------------------------------------- 세션 감지(작업 폴더·시각·건수만)

def _head_cwd(path: Path) -> Optional[str]:
    """파일 앞부분(최대 HEAD_BYTES)에서 "cwd"(OpenCode 는 "directory") 값만 정규식으로 뽑는다.
    JSON 전체를 해석하지 않아 메시지 본문 필드는 객체로 만들어지지도 않는다."""
    try:
        with open(path, 'rb') as f:
            head = f.read(HEAD_BYTES)
    except OSError:
        return None
    for line in head.split(b'\n'):
        m = CWD_RE.search(line)
        if not m or BODY_BEFORE_RE.search(line, 0, m.start()):
            continue
        try:
            v = json.loads(b'"' + m.group(1) + b'"')
        except ValueError:
            continue
        if isinstance(v, str) and v:
            return v
    return None


class _Acc:
    """한 세션 소스의 집계(파일 수·크기·기간·작업 폴더)."""

    def __init__(self):
        self.files = 0
        self.size = 0
        self.lo: Optional[float] = None
        self.hi: Optional[float] = None
        self.cwds: set = set()
        self.sessions: Dict[str, int] = {}   # 작업 폴더별 세션 파일 수(같은 이름 폴더 중 원본 고르기에 쓴다)
        self.compressed = 0
        self.excluded = 0            # 사용자 작업이 아닌 기록(관측기 세션 등)으로 뺀 파일 수

    def add(self, p: Path, cwd: Optional[str] = None) -> None:
        try:
            st = p.stat()
        except OSError:
            return
        self.files += 1
        self.size += st.st_size
        self.lo = st.st_mtime if self.lo is None else min(self.lo, st.st_mtime)
        self.hi = st.st_mtime if self.hi is None else max(self.hi, st.st_mtime)
        if cwd:
            self.note(cwd)

    def note(self, cwd: str, n: int = 1) -> None:
        self.cwds.add(cwd)
        self.sessions[cwd] = self.sessions.get(cwd, 0) + n

    def summary(self, git_cache: Dict[str, bool], classes: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """cwd_count·git_count 는 원본 작업 폴더만 센다. 평가 복사본·임시는 따로(있을 때만) 남긴다."""
        classes = classes or {}
        kinds = {c: classes.get(c, 'original') for c in self.cwds}
        cwds = sorted(c for c, k in kinds.items() if k == 'original')
        out = {'files': self.files, 'size': self.size, 'period': [_date(self.lo), _date(self.hi)],
               'cwd_count': len(cwds), 'git_count': sum(1 for c in cwds if _in_git(c, git_cache))}
        if self.compressed:
            out['compressed'] = self.compressed
        if self.excluded:
            out['excluded_observer'] = self.excluded
        for k, key in (('eval_copy', 'cwd_eval_copy'), ('temp', 'cwd_temp')):
            n = sum(1 for v in kinds.values() if v == k)
            if n:
                out[key] = n
        return out


def _parts(cwd: str) -> List[str]:
    """경로 구성요소(소문자·NFC). 윈도우 경로를 다른 OS 에서 읽어도 나뉘게 / 와 \\ 둘 다로 자른다."""
    return [_nfc(x).lower() for x in re.split(r'[\\/]+', cwd or '') if x and not x.endswith(':')]


def _is_temp_cwd(cwd: str, home_n: str, roots: List[str]) -> bool:
    """시스템 임시 폴더 아래인가. 홈이 임시 폴더 아래인 경우(격리 시험·일회용 계정) 그 홈 아래는 임시로 보지 않는다
    — sb_vault.classify_path 와 같은 규칙."""
    parts = _parts(cwd)
    n = len(WIN_TEMP_PARTS)
    if any(tuple(parts[i:i + n]) == WIN_TEMP_PARTS for i in range(len(parts) - n + 1)):
        return True
    raw = os.path.expanduser(cwd)
    if not os.path.isabs(raw):     # 다른 OS 경로를 지금 폴더 기준으로 풀면 엉뚱한 판정이 난다
        return False
    real = _cmp(raw)
    return any(sb_scope.path_within(real, r) and not (sb_scope.path_within(home_n, r) and sb_scope.path_within(real, home_n))
               for r in roots)


def _is_git_root(cwd: str) -> bool:
    raw = os.path.expanduser(cwd)
    return os.path.isabs(raw) and os.path.exists(os.path.join(raw, '.git'))


def classify_cwds(sessions: Dict[str, int], home: str) -> Dict[str, str]:
    """작업 폴더 → 'original' | 'eval_copy' | 'temp'. 경로 문자열·.git 존재·세션 수만 본다(폴더 내용은 열지 않는다).
    - temp: 시스템 임시 폴더(gettempdir·/tmp·/private/tmp·/var/folders·AppData/Local/Temp) 아래
    - eval_copy: 경로 구성요소에 'sbeval'('_sbeval')·'smoketest' 가 있거나, 같은 마지막 폴더 이름이 다른 부모 아래에도
      있을 때 원본(git 루트, 없으면 세션이 가장 많은 쪽)이 아닌 나머지. src·app 같은 흔한 이름은 묶지 않는다."""
    home_n = _cmp(home)
    roots = sorted({_cmp(r) for r in sync_automemory.temp_roots()})
    out: Dict[str, str] = {}
    groups: Dict[str, List[str]] = {}
    for cwd in sorted(sessions):
        parts = _parts(cwd)
        if _is_temp_cwd(cwd, home_n, roots):
            out[cwd] = 'temp'
        elif any(m in x for x in parts for m in EVAL_MARKERS):
            out[cwd] = 'eval_copy'
        else:
            out[cwd] = 'original'
            if parts and parts[-1] not in sb_vault.GENERIC_NAMES:
                groups.setdefault(parts[-1], []).append(cwd)
    def real(c: str) -> str:   # 같은 폴더의 다른 표기(심볼릭 링크·윈도우 대소문자)는 한 폴더로 본다
        return _cmp(c) if os.path.isabs(os.path.expanduser(c)) else c

    for members in groups.values():
        if len({real(c) for c in members}) < 2:
            continue
        gits = [c for c in members if _is_git_root(c)]
        keep = set(gits) if gits else {min(members, key=lambda c: (-sessions.get(c, 0), len(c), c))}
        kept = {real(c) for c in keep}
        for c in members:
            if real(c) not in kept:
                out[c] = 'eval_copy'
    return out


def _in_git(cwd: str, cache: Dict[str, bool]) -> bool:
    """cwd 또는 그 위 폴더에 .git 이 있는가(git 을 띄우지 않는다 — 폴더가 수백 개라도 빠르게)."""
    p = os.path.realpath(os.path.expanduser(cwd))
    seen = []
    hit = False
    while True:
        if p in cache:
            hit = cache[p]
            break
        seen.append(p)
        if os.path.exists(os.path.join(p, '.git')):
            hit = True
            break
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    for s in seen:
        cache[s] = hit
    return hit


def _files(root: Path, pattern: str) -> List[Path]:
    try:
        return sorted(p for p in root.glob(pattern) if p.is_file())
    except OSError:
        return []


def _claude(root: Path) -> Optional[_Acc]:
    projects = root / 'projects'
    if not projects.is_dir():
        return None
    acc = _Acc()
    for d in sorted(p for p in projects.iterdir() if p.is_dir()):
        if d.name.lower().endswith(OBSERVER_SLUG_MARKERS):   # 관측기 자체 세션은 세지 않는다
            acc.excluded += sum(1 for _ in _files(d, '*.jsonl'))
            continue
        cwd = None
        files = _files(d, '*.jsonl')
        for i, f in enumerate(files):
            if cwd is None and i < CLAUDE_FILES_PER_DIR:
                cwd = _head_cwd(f)
            acc.add(f)
        if cwd:
            acc.note(cwd, len(files))
    return acc


def _codex(root: Path) -> Optional[_Acc]:
    dirs = [root / 'sessions', root / 'archived_sessions']
    if not any(d.is_dir() for d in dirs):
        return None
    acc = _Acc()
    for d in dirs:
        if not d.is_dir():
            continue
        for f in _files(d, '**/*.jsonl'):
            acc.add(f, _head_cwd(f))
        for f in _files(d, '**/*.jsonl.zst'):   # 압축본은 개수만(풀지 않는다)
            acc.add(f)
            acc.compressed += 1
    return acc


def _antigravity(root: Path) -> Optional[_Acc]:
    """Antigravity(구글 IDE 에이전트) 대화: ~/.gemini/antigravity/conversations. 개수·기간만(본문·cwd 안 읽음)."""
    conv = root / 'antigravity' / 'conversations'
    if not conv.is_dir():
        return None
    acc = _Acc()
    for f in _files(conv, '**/*'):
        if f.is_file():
            acc.add(f)
    return acc if acc.files else None


def _gemini(root: Path) -> Optional[_Acc]:
    tmp = root / 'tmp'
    if not tmp.is_dir():
        return None
    by_name: Dict[str, str] = {}
    projects = sb_vault.read_json_file(root / 'projects.json')
    table = projects.get('projects') if isinstance(projects, dict) else None
    if isinstance(table, dict):   # 폴더 이름은 projects.json 의 짧은 이름이거나 경로의 sha256
        for path, name in table.items():
            if isinstance(path, str):
                by_name[hashlib.sha256(path.encode('utf-8')).hexdigest()] = path
                if isinstance(name, str):
                    by_name[name] = path
    acc = _Acc()
    for d in sorted(p for p in tmp.iterdir() if p.is_dir()):
        chats = [f for f in _files(d / 'chats', '*') if f.suffix in ('.json', '.jsonl')]
        for f in chats:
            acc.add(f)
        if chats and d.name in by_name:
            acc.note(by_name[d.name], len(chats))
    return acc if acc.files or any(tmp.iterdir()) else None


def _omo(roots: List[Tuple[Path, str]]) -> Tuple[Optional[_Acc], Optional[Path]]:
    found = None
    acc = _Acc()
    for root, pattern in roots:
        if not root.is_dir():
            continue
        found = found or root
        for f in _files(root, pattern):
            acc.add(f, _head_cwd(f))
    return (acc, found) if found else (None, None)


def detect_sessions(home: Optional[str] = None) -> List[Dict[str, Any]]:
    """과거 에이전트 세션 위치. 있는 곳만 돌려준다. home 을 주면 그 아래만 본다(환경변수 무시)."""
    h = Path(home).expanduser() if home else Path.home()
    env = (lambda k: None) if home else os.environ.get
    claude_root = Path(env('CLAUDE_CONFIG_DIR') or h / '.claude').expanduser()
    codex_root = Path(env('CODEX_HOME') or h / '.codex').expanduser()
    xdg = Path(env('XDG_DATA_HOME') or h / '.local' / 'share').expanduser()
    omo_acc, omo_root = _omo([(h / '.omo' / 'agent' / 'sessions', '*/*.jsonl'),
                              (xdg / 'opencode' / 'storage' / 'session', '**/*.json')])
    found = [('claude_sessions', claude_root, _claude(claude_root)),
             ('codex_sessions', codex_root, _codex(codex_root)),
             ('gemini_sessions', h / '.gemini', _gemini(h / '.gemini')),
             ('antigravity_sessions', h / '.gemini' / 'antigravity', _antigravity(h / '.gemini')),
             ('omo_sessions', omo_root, omo_acc)]
    git_cache: Dict[str, bool] = {}
    sessions: Dict[str, int] = {}     # 원본·복사본은 모든 에이전트 세션을 합쳐서 가른다
    for _, root, acc in found:
        if acc is not None and root is not None:
            for c, n in acc.sessions.items():
                sessions[c] = sessions.get(c, 0) + n
    classes = classify_cwds(sessions, str(h))
    out = []
    for kind, root, acc in found:
        if acc is None or root is None:
            continue
        loc = sb_vault._short(str(root))
        summary = acc.summary(git_cache, classes)
        # 본문 적재는 사용자가 고른 세션만(A4) — 감지만으로는 색인 대상이 아니다
        out.append({'id': _sid(kind, loc), 'kind': kind, 'location': loc, 'mode': 'skip', 'discovered_by': 'auto',
                    'summary': summary})
    return out


# ---------------------------------------------------------------- MCP 커넥터 감지(이름만)

def _server_names(table: Any) -> List[str]:
    """mcpServers 표의 하위 키 이름만. 값(주소·명령·env·토큰)은 보지 않는다."""
    return [k for k in table if isinstance(k, str) and k.strip()] if isinstance(table, dict) else []


def _json_mcp(path: Path, label: str, found: Dict[str, set], unreadable: List[str], flat_ok: bool = False) -> None:
    """JSON 설정의 mcpServers 이름. 파일이 없으면 조용히 넘어가고, 있는데 못 읽으면(주석 섞인 JSON 등) '확인 못 함'.
    flat_ok: 플러그인 .mcp.json 처럼 mcpServers 없이 서버 표가 맨 위에 있는 꼴도 받는다."""
    if not path.is_file():
        return
    data = sb_vault.read_json_file(path)
    if not isinstance(data, dict):
        unreadable.append(label)
        return
    names = _server_names(data.get('mcpServers'))
    if not names and flat_ok and 'mcpServers' not in data:
        names = [k for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)]
    for n in names:
        found.setdefault(n, set()).add(label)


def _plugin_mcp(claude_root: Path, settings: Any, found: Dict[str, set], unreadable: List[str]) -> None:
    """켜진 플러그인의 .mcp.json. installed_plugins.json 의 installPath(플러그인 캐시 안만 믿는다)를 보고,
    settings.json enabledPlugins 에서 false 인 플러그인은 뺀다."""
    reg = sb_vault.read_json_file(claude_root / 'plugins' / 'installed_plugins.json')
    plugins = reg.get('plugins') if isinstance(reg, dict) else None
    if not isinstance(plugins, dict):
        return
    enabled = settings.get('enabledPlugins') if isinstance(settings, dict) else None
    enabled = enabled if isinstance(enabled, dict) else {}
    cache = _cmp(str(claude_root / 'plugins' / 'cache'))
    for key in sorted(k for k in plugins if isinstance(k, str)):
        if enabled.get(key) is False:
            continue
        entries = plugins[key] if isinstance(plugins[key], list) else [plugins[key]]
        for e in entries:
            if not isinstance(e, dict) or not isinstance(e.get('installPath'), str) or not e['installPath']:
                continue
            ip = os.path.expanduser(e['installPath'])
            if not sb_scope.path_within(_cmp(ip), cache):
                continue
            _json_mcp(Path(ip) / '.mcp.json', 'claude_plugin', found, unreadable, flat_ok=True)


def _codex_mcp(path: Path, found: Dict[str, set], unreadable: List[str]) -> None:
    """config.toml 의 [mcp_servers.<이름>] 머리글만 정규식으로(표 안의 값은 읽지 않는다)."""
    if not path.is_file():
        return
    try:
        # utf-8-sig: 윈도우 메모장·PowerShell 이 앞에 붙이는 BOM 이 있으면 첫 머리글이 조용히 빠진다
        text = path.read_text(encoding='utf-8-sig', errors='replace')
    except OSError:
        unreadable.append('codex')
        return
    for m in CODEX_MCP_RE.finditer(text):
        n = next(g for g in m.groups() if g)
        found.setdefault(n, set()).add('codex')


def mcp_category(name: str) -> str:
    low = _nfc(name).lower()
    for cat, keys in MCP_CATEGORIES:
        if any(k in low for k in keys):
            return cat
    return 'other'


def _mcp_name(name: str) -> str:
    """저장용 이름: 줄바꿈·제어문자를 빼고 길이를 자른다. 이름이 링크 꼴이면 비밀값을 지운다."""
    n = ''.join(ch for ch in _nfc(name).strip() if ch.isprintable())
    if _is_link(n):
        n = redact_link(n)[0]
    return n[:MCP_NAME_MAX]


def detect_mcp(home: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """설정에 등록된 MCP 서버 이름 → (자료 항목 목록, 확인 못 한 설정 이름 목록). 적재하지 않는다(mode=live —
    필요할 때 에이전트가 직접 조회). home 을 주면 그 아래만 본다(환경변수 무시)."""
    h = Path(home).expanduser() if home else Path.home()
    env = (lambda k: None) if home else os.environ.get
    claude_root = Path(env('CLAUDE_CONFIG_DIR') or h / '.claude').expanduser()
    codex_root = Path(env('CODEX_HOME') or h / '.codex').expanduser()
    found: Dict[str, set] = {}
    unreadable: List[str] = []
    seen = set()
    for cj in (h / '.claude.json', claude_root / '.claude.json'):   # CLAUDE_CONFIG_DIR 를 쓰면 그 안에 생긴다
        if _cmp(str(cj)) in seen or not cj.is_file():
            continue
        seen.add(_cmp(str(cj)))
        data = sb_vault.read_json_file(cj)
        if not isinstance(data, dict):
            unreadable.append('claude')
            continue
        for n in _server_names(data.get('mcpServers')):
            found.setdefault(n, set()).add('claude')
        projects = data.get('projects')
        for proj in (projects.values() if isinstance(projects, dict) else []):
            for n in _server_names(proj.get('mcpServers') if isinstance(proj, dict) else None):
                found.setdefault(n, set()).add('claude')
    settings_path = claude_root / 'settings.json'
    settings = sb_vault.read_json_file(settings_path) if settings_path.is_file() else None
    if settings_path.is_file() and not isinstance(settings, dict):
        unreadable.append('claude_settings')
    for n in _server_names(settings.get('mcpServers') if isinstance(settings, dict) else None):
        found.setdefault(n, set()).add('claude')
    _plugin_mcp(claude_root, settings, found, unreadable)
    _codex_mcp(codex_root / 'config.toml', found, unreadable)
    _json_mcp(h / '.gemini' / 'settings.json', 'gemini', found, unreadable)
    _json_mcp(h / '.gemini' / 'antigravity' / 'mcp_config.json', 'antigravity', found, unreadable)
    merged: Dict[str, Dict[str, Any]] = {}
    for raw in sorted(found):
        name = _mcp_name(raw)
        if not name:
            continue
        loc = 'mcp:%s' % name
        src = merged.setdefault(loc, {'id': _sid('mcp', loc), 'kind': 'mcp', 'location': loc, 'mode': 'live',
                                      'discovered_by': 'auto', 'category': mcp_category(name),
                                      'summary': {'found_in': []}})
        src['summary']['found_in'] = sorted(set(src['summary']['found_in']) | found[raw])
    return [merged[k] for k in sorted(merged)], sorted(set(unreadable))


# ---------------------------------------------------------------- 폴더·링크 등록과 분류

def _is_link(location: str) -> bool:
    return bool(re.match(r'^[a-z][a-z0-9+.-]*://', location.strip(), re.IGNORECASE))


def redact_link(url: str) -> Tuple[str, int]:
    """링크에서 비밀값을 지운다: userinfo(아이디:비밀번호@), 서명·토큰형 쿼리 값, '='가 든 조각(#access_token=…).
    (지운 링크, 지운 개수). 서명이 빠져 열리지 않는 링크는 요청 때 사용자가 다시 인증한다."""
    parts = urllib.parse.urlsplit(url.strip())
    removed = 0
    netloc = parts.netloc
    if '@' in netloc:
        netloc = netloc.rsplit('@', 1)[1]
        removed += 1
    query = []
    for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if SECRET_PARAM.search(k):
            removed += 1
            continue
        query.append((k, v))
    fragment = parts.fragment
    if '=' in fragment:
        fragment = ''
        removed += 1
    if not removed:
        return url.strip(), 0
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, urllib.parse.urlencode(query), fragment)), removed


def clean_location(location: str) -> str:
    """저장·출력 전에 쓰는 자료 위치(링크면 비밀값을 지운 값)."""
    location = (location or '').strip()
    return redact_link(location)[0] if _is_link(location) else location


def _walk(root: Path) -> Iterator[Tuple[str, int, float, bool]]:
    """(상대 경로, 크기, 수정 시각, junk 폴더 아래인가). 심볼릭 링크는 따라가지 않는다."""
    if root.is_file():
        st = root.stat()
        yield root.name, st.st_size, st.st_mtime, False
        return
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        rel_dir = os.path.relpath(dirpath, str(root))
        parts = [] if rel_dir == os.curdir else rel_dir.split(os.sep)
        in_junk = any(x.lower() in JUNK_DIRS for x in parts)
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            yield os.path.join(*(parts + [name])), st.st_size, st.st_mtime, in_junk


def _nfc(s: str) -> str:
    return unicodedata.normalize('NFC', s)


def _sensitive(rel: str) -> bool:
    rel = _nfc(rel)
    low = rel.lower()
    if os.path.splitext(low)[1] in SENSITIVE_EXT or os.path.basename(low).startswith('.env'):
        return True
    return any(k in rel for k in SENSITIVE_KO) or bool(SENSITIVE_EN.search(low))


def _root_label(root: Path) -> str:
    """등록한 위치 자체(파일이면 그 폴더)의 이름 경로. 홈 아래면 홈 뒤 부분만 본다(계정 이름은 신호가 아니다)."""
    d = root if root.is_dir() else root.parent
    try:
        rel = d.resolve().relative_to(Path.home().resolve())
        parts = rel.parts
    except ValueError:
        parts = d.parts[1:]
    return os.sep.join(parts)


def _ext_key(rel: str) -> str:
    ext = os.path.splitext(rel)[1].lower()
    if not ext:
        return '(none)'
    return ext if EXT_RE.match(ext) else '(other)'


def classify_file(rel: str, size: int, in_junk: bool = False) -> Tuple[str, str]:
    """(분류, 이유). 분류 ∈ own_output | sensitive | junk | unknown. 모르면 unknown(제외)."""
    name = _nfc(os.path.basename(rel))
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if _sensitive(rel):
        return 'sensitive', 'sensitive_name'
    if in_junk or low in JUNK_NAMES or ext in JUNK_EXT or name.startswith('~$') or low.startswith('.~lock'):
        return 'junk', 'junk'
    if ext in OWN_EXT:
        if OWN_MIN <= size <= OWN_MAX:
            return 'own_output', 'own_output'
        return 'unknown', 'size_out_of_range'
    return 'unknown', 'unknown_ext'


def _scan(root: Path) -> Dict[str, Any]:
    by_ext: Dict[str, int] = {}
    root_sensitive = _sensitive(_root_label(root))   # 등록 폴더 이름 자체(~/docs/세금 등)
    counts = {'own_output': 0, 'sensitive': 0, 'junk': 0, 'unknown': 0}
    reasons: Dict[str, int] = {}
    files = size = 0
    lo = hi = None
    truncated = False
    for rel, sz, mt, in_junk in _walk(root):
        if files >= MAX_FILES:
            truncated = True
            break
        files += 1
        size += sz
        lo = mt if lo is None else min(lo, mt)
        hi = mt if hi is None else max(hi, mt)
        ext = _ext_key(rel)
        by_ext[ext] = by_ext.get(ext, 0) + 1
        cls, why = ('sensitive', 'sensitive_location') if root_sensitive else classify_file(rel, sz, in_junk)
        counts[cls] += 1
        if cls != 'own_output':
            reasons[why] = reasons.get(why, 0) + 1
    return {'files': files, 'size': size, 'by_ext': by_ext, 'period': [_date(lo), _date(hi)],
            'counts': counts, 'reasons': reasons, 'truncated': truncated}


def register(location: str, kind: Optional[str] = None) -> Dict[str, Any]:
    """폴더·회의 폴더·링크를 자료 항목으로 만든다(저장은 하지 않는다 — stage2·CLI add 가 저장)."""
    location = (location or '').strip()
    if not location:
        raise ValueError('자료 위치가 비었습니다')
    if _is_link(location):
        k = kind or 'link'
        clean, removed = redact_link(location)
        src = {'id': _sid(k, clean), 'kind': k, 'location': clean, 'mode': 'live', 'discovered_by': 'user'}
        if removed:
            src['redacted'] = removed       # 비밀값을 지웠다(값은 어디에도 남기지 않는다)
        return src
    p = Path(os.path.expanduser(location))
    p = Path(os.path.realpath(str(p))) if p.exists() else p
    loc = sb_vault._short(str(p))
    if kind is None:
        names = [_nfc(x).lower() for x in p.parts[-2:]]
        kind = 'meeting' if any(m in n for n in names for m in MEETING_NAMES) else 'folder'
    src: Dict[str, Any] = {'id': _sid(kind, loc), 'kind': kind, 'location': loc, 'discovered_by': 'user'}
    if not p.exists():
        src.update(mode='skip', missing=True,
                   summary={'files': 0, 'size': 0, 'by_ext': {}, 'period': [None, None], 'sensitive_hint': 0})
        return src
    scan = _scan(p)
    src['summary'] = {'files': scan['files'], 'size': scan['size'], 'by_ext': scan['by_ext'], 'period': scan['period'],
                      'sensitive_hint': scan['counts']['sensitive']}
    if scan['truncated']:
        src['summary']['truncated'] = True
    # 회의록은 파일 이름으로 민감도를 알 수 없다 — 민감도 표기를 보는 회의 적재기(M4)가 따로 고른다(A5·A12)
    src['mode'] = 'index' if scan['counts']['own_output'] and kind != 'meeting' else 'skip'
    return src


def classify(source: Dict[str, Any]) -> Dict[str, Any]:
    """{'own_output','sensitive','junk','unknown'} 개수와 intake(결정·제외 이유별 수)."""
    kind = source.get('kind')
    empty = {'own_output': 0, 'sensitive': 0, 'junk': 0, 'unknown': 0}
    if source.get('mode') == 'live' or kind == 'link':
        return dict(empty, intake={'decision': 'live', 'index': 0, 'skip': 0, 'live': 1, 'reasons': {}})
    if kind in SESSION_KINDS:   # 본문 적재는 사용자가 고른 세션만(A4) — 고르기 전에는 모두 skip(모르면 제외)
        n = int((source.get('summary') or {}).get('files') or 0)
        if source.get('mode') == 'index' and source.get('selected_by') == 'user':
            return dict(empty, own_output=n, intake={'decision': 'index', 'index': n, 'skip': 0, 'live': 0, 'reasons': {}})
        return dict(empty, unknown=n, intake={'decision': 'skip', 'index': 0, 'skip': n, 'live': 0,
                                              'reasons': {'awaiting_user_choice': n} if n else {}})
    p = Path(os.path.expanduser(source.get('location') or ''))
    if not source.get('location') or not p.exists():
        return dict(empty, intake={'decision': 'skip', 'index': 0, 'skip': 0, 'live': 0, 'reasons': {'missing': 1}})
    scan = _scan(p)
    c = dict(scan['counts'])
    reasons = dict(scan['reasons'])
    if kind == 'meeting' and c['own_output']:   # 민감도 표기 없는 회의록은 private 으로 본다(A5·A12)
        reasons['meeting_no_sensitivity'] = c['own_output']
        c['unknown'] += c['own_output']
        c['own_output'] = 0
    skip = c['sensitive'] + c['junk'] + c['unknown']
    return dict(c, intake={'decision': 'index' if c['own_output'] else 'skip', 'index': c['own_output'], 'skip': skip,
                           'live': 0, 'reasons': reasons})


# ---------------------------------------------------------------- 2단계

def _metric(name: str, value: Any, threshold: str = '', ok: Optional[bool] = None, evidence: str = '') -> Dict[str, Any]:
    return {'name': name, 'value': value, 'threshold': threshold, 'pass': ok, 'evidence': evidence}


def _result(stage: int, verdict: str, metrics: List[Dict[str, Any]], hint: str = '') -> Dict[str, Any]:
    return {'stage': stage, 'name': {2: 'register', 3: 'classify'}[stage], 'verdict': verdict, 'metrics': metrics,
            'hint': hint}


def _answer_sources(answers: Dict[str, Any]) -> List[Tuple[str, Optional[str]]]:
    raw = (answers or {}).get('sources') or []
    if isinstance(raw, str):
        raw = [x for x in raw.split(',')]
    out = []
    for item in raw:
        if isinstance(item, dict) and item.get('location'):
            out.append((str(item['location']).strip(), item.get('kind')))
        elif isinstance(item, str) and item.strip():
            out.append((item.strip(), None))
    return out


def _mcp_evidence(mcp: List[Dict[str, Any]], unreadable: List[str]) -> str:
    cats: Dict[str, int] = {}
    for s in mcp:
        cats[s.get('category') or 'other'] = cats.get(s.get('category') or 'other', 0) + 1
    out = 'MCP 커넥터 %d개(적재 안 함, 필요할 때 에이전트가 직접 조회)' % len(mcp)
    if cats:
        out += ' — ' + ', '.join('%s=%d' % kv for kv in sorted(cats.items()))
    if unreadable:
        out += ' / 확인 못 함: ' + ', '.join(unreadable)
    return out


def stage2(answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """세션 감지 + 답에 있는 자료 등록 → sources.json. 소스가 0이어도 pass(기본 볼트)."""
    doc = load()
    mcp, mcp_unreadable = detect_mcp()
    auto = detect_sessions() + mcp
    chosen = {s['id'] for s in doc['sources'] if s.get('selected_by') == 'user' and s.get('mode') == 'index'}
    for s in auto:                      # 사용자가 고른 세션(sb sources select)은 다시 감지해도 고른 그대로
        if s['id'] in chosen:
            s.update(mode='index', selected_by='user')
    wanted = [(loc, kind, 'answer') for loc, kind in _answer_sources(answers or {})]
    # sb sources add 로 넣은 자료만 다시 잰다. 답에서 온 자료는 지금 답이 정본이다(답을 고치면 옛 위치는 빠진다)
    for s in doc['sources']:
        if s.get('discovered_by') == 'user' and s.get('added_via') == 'cli' and s.get('location'):
            wanted.append((s['location'], s.get('kind'), 'cli'))
    users: Dict[str, Dict[str, Any]] = {}
    for loc, kind, via in wanted:
        src = register(loc, kind)
        src['added_via'] = via
        if src['id'] not in users or via == 'cli':
            users[src['id']] = src
    sources = []
    for src in auto + [users[k] for k in sorted(users)]:
        cls = classify(src)
        src['intake'] = cls.pop('intake')
        src['classification'] = cls
        sources.append(src)
    path = save({'sources': sources})
    saved = sb_vault.read_json_file(path) or {}
    leaked = body_keys(saved)
    sess = [s for s in sources if s['kind'] in SESSION_KINDS]
    missing = [s['location'] for s in sources if s.get('missing')]
    sensitive = sum(int((s.get('classification') or {}).get('sensitive') or 0) for s in sources)
    ev = sb_vault._short(str(path))
    metrics = [
        _metric('sources', len(sources), '0 이상(0이면 기본 볼트)', True, ev),
        _metric('session_files', sum(s['summary']['files'] for s in sess), '', True,
                ', '.join('%s=%d' % (s['kind'], s['summary']['files']) for s in sess) or '세션 위치 없음'),
        _metric('session_cwds', sum(s['summary']['cwd_count'] for s in sess), '', True,
                'cwd 정규식만(본문 해석 없음) — 원본 작업 폴더만(평가 복사본·임시 제외)'),
        _metric('session_cwds_eval_copy', sum(int(s['summary'].get('cwd_eval_copy') or 0) for s in sess),
                '보고만(판정에 안 씀)', True, "평가 복사본('sbeval'·'smoketest'·같은 이름 원본이 따로 있는 폴더)"),
        _metric('session_cwds_temp', sum(int(s['summary'].get('cwd_temp') or 0) for s in sess),
                '보고만(판정에 안 씀)', True, '시스템 임시 폴더 아래'),
        _metric('mcp_connectors', len(mcp), '보고만(적재 안 함)', True, _mcp_evidence(mcp, mcp_unreadable)),
        _metric('sensitive_hints', sensitive, '보고만(민감 신호는 skip)', True, '파일 이름 기준'),
        _metric('missing_sources', len(missing), '0', not missing, ', '.join(missing)),
        _metric('body_fields_saved', len(leaked), '0', not leaked, "sources.json 에 'text'·'content'·'prompt' 키"),
    ]
    if leaked:
        return _result(2, 'stop', metrics, 'sources.json 에 본문 필드가 있습니다 — 키트를 갱신하세요')
    if missing:
        cli = [s['id'] for s in sources if s.get('missing') and s.get('added_via') == 'cli']
        how = ('sb setup answer --sources "<고친 위치들>" 뒤 sb setup run' +
               (' / sb sources add 로 넣은 것은 sb sources remove %s' % ' '.join(cli) if cli else ''))
        return _result(2, 'stop', metrics, '없는 자료 위치: %s — %s' % (', '.join(missing), how))
    return _result(2, 'pass', metrics)


# ---------------------------------------------------------------- 3단계 표본 판정

def _norm_title(t: Optional[str]) -> str:
    return re.sub(r'[\W\d_]+', '', (t or '').lower())


def heuristic(obs_type: Optional[str], title: Optional[str]) -> str:
    """ok | noise | unjudged. 제목·type 만 본다."""
    t = (obs_type or '').strip().lower()
    if t in OK_TYPES:
        return 'ok'
    title = title or ''
    if NOISE_TITLE.search(title):
        return 'noise'
    if OK_TITLE.search(title):
        return 'ok'
    return 'unjudged'


def _scope_key(v: str) -> Tuple[str, str]:
    """(비교용 전체 값, basename). 경로(~·절대)는 realpath·normcase, 이름은 소문자·공백 제거."""
    v = (v or '').strip()
    if v.startswith('~') or sb_config.is_abs_like(v):
        full = _cmp(v)
        return full, sb_scope._norm_name(os.path.basename(full.rstrip('\\/')))
    n = sb_scope._norm_name(v)
    return n, n


def scope_match(scope_id: str, project: str) -> bool:
    a, ab = _scope_key(scope_id)
    b, bb = _scope_key(project)
    return bool(a) and bool(b) and (a == b or ab == b or a == bb or (ab and ab == bb))


def sample_path(seed: int) -> Path:
    return logs_dir() / ('setup-3-sample-%s.json' % seed)


def _ratio(a: int, b: int) -> Optional[float]:
    return round(a / b, 4) if b else None


def evaluate(sample: Dict[str, Any]) -> Dict[str, Any]:
    """표본 파일(자동 판정 + 사람 판정 + scope 비교)로 3단계 판정을 다시 계산한다(DB 를 다시 읽지 않는다)."""
    ids = [str(i) for i in sample.get('ids') or []]
    auto = sample.get('auto') or {}
    judged = sample.get('judged') or {}
    scope = sample.get('scope') or {}
    final = {i: judged.get(i) or auto.get(i) or 'unjudged' for i in ids}
    n = len(ids)
    noise = sum(1 for v in final.values() if v == 'noise')
    dup = sum(1 for v in final.values() if v == 'dup')
    unjudged = sum(1 for v in final.values() if v == 'unjudged')
    judged_n = n - unjudged
    scoped = [scope.get(i) for i in ids if scope.get(i) in ('match', 'mismatch')]
    mismatch = sum(1 for v in scoped if v == 'mismatch')
    nd = _ratio(noise + dup, judged_n)
    nd_floor = _ratio(noise + dup, n)           # 판정 못 한 것을 모두 ok 로 볼 때(하한)
    sm = _ratio(mismatch, len(scoped))
    un = _ratio(unjudged, n)
    ev = sb_vault._short(sample.get('path') or str(sample_path(sample.get('seed'))))
    metrics = [
        _metric('sample_n', n, '%d' % SAMPLE_N, True, 'seed=%s, %s' % (sample.get('seed'), ev)),
        _metric('noise_dup_ratio', nd, '≤ %d%% (판정된 표본 기준)' % (NOISE_DUP_MAX * 100),
                None if nd is None else nd <= NOISE_DUP_MAX, 'noise=%d dup=%d judged=%d' % (noise, dup, judged_n)),
        _metric('scope_mismatch_ratio', sm, '≤ %d%%' % (SCOPE_MISMATCH_MAX * 100),
                None if sm is None else sm <= SCOPE_MISMATCH_MAX,
                'mismatch=%d / cwd 있는 표본 %d (resolve_scope_id use_git=False)' % (mismatch, len(scoped))),
        _metric('unjudged_ratio', un, '≤ %d%%' % (UNJUDGED_MAX * 100), None if un is None else un <= UNJUDGED_MAX,
                'unjudged=%d (제목·type 휴리스틱으로 판정 못 함)' % unjudged),
    ]
    if sm is not None and sm > SCOPE_MISMATCH_MAX:
        return _result(3, 'stop', metrics, 'scope 불일치 %.1f%% — sb vault analyze 로 별칭·꼬리표를 확인하세요' % (sm * 100))
    if nd_floor is not None and nd_floor > NOISE_DUP_MAX:
        return _result(3, 'stop', metrics, NOISE_HINT % (nd_floor * 100))
    if un is not None and un > UNJUDGED_MAX:
        return _result(3, 'pending', metrics, '표본 %d건을 판정해 주세요: sb setup judge --stage 3 --judgments ID:ok|noise|dup,…'
                       % unjudged)
    if nd is not None and nd > NOISE_DUP_MAX:
        return _result(3, 'stop', metrics, NOISE_HINT % (nd * 100))
    return _result(3, 'pass', metrics)


def _write_sample(sample: Dict[str, Any], path: Path) -> None:
    body = {k: v for k, v in sample.items() if k != 'path'}
    private_dir(path.parent)
    sb_vault._atomic_write(path, json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True) + '\n', private=True,
                           mode=0o600)


def _prev_sample_ids(seed: int, n: int) -> Optional[List[int]]:
    """같은 seed·n 으로 전에 뽑은 표본 id. 없거나 형식이 다르면 None."""
    p = sample_path(seed)
    prev = sb_vault.read_json_file(p) if p.exists() else None
    if not isinstance(prev, dict) or prev.get('seed') != seed or prev.get('n') != n:
        return None
    try:
        out = sorted(int(i) for i in prev.get('ids') or [])
    except (TypeError, ValueError):
        return None
    return out if len(out) == n and len(set(out)) == n else None


def stage3(seed: Optional[int] = None, n: int = SAMPLE_N, db: Optional[str] = None) -> Dict[str, Any]:
    """claude-mem 관측 seed 고정 무작위 n건으로 소음·중복·scope 불일치를 잰다(DB 는 읽기 전용)."""
    seed = DEFAULT_SEED if seed is None else seed
    db = db or sb_config.claude_mem_db()
    ev = sb_vault._short(str(db))
    if not Path(db).is_file():
        return _result(3, 'pass', [_metric('sample_n', 0, '%d 이상' % n, None, '데이터 부족: claude-mem.db 없음 (%s)' % ev)])
    try:
        with closing(sb_vault._ro(db)) as con:
            ids = [r[0] for r in con.execute('SELECT id FROM observations ORDER BY id')]
            if len(ids) < n:
                return _result(3, 'pass', [_metric('sample_n', len(ids), '%d 이상' % n, None,
                                                   '데이터 부족: 관측 %d건 < %d (%s)' % (len(ids), n, ev))])
            picked = sorted(random.Random(seed).sample(ids, n))
            prev_ids = _prev_sample_ids(seed, n)
            if prev_ids is not None and set(prev_ids) <= set(ids):
                picked = prev_ids        # 관측이 늘어도 같은 표본 — 사람이 준 판정이 사라지지 않게
            scols = {r[1] for r in con.execute('PRAGMA table_info(sdk_sessions)')}
            cwd_col = 'cwd' if 'cwd' in scols else 'NULL'
            cwd_of: Dict[str, Optional[str]] = {}
            aliases = sb_scope._load_aliases()
            auto: Dict[str, str] = {}
            scope: Dict[str, str] = {}
            for oid in picked:
                row = con.execute('SELECT memory_session_id, project, type, title FROM observations WHERE id=?',
                                  (oid,)).fetchone()
                sid, project, otype, title = row
                key = _norm_title(title)
                dup = False
                if key:
                    for other_id, other_title in con.execute(
                            'SELECT id, title FROM observations WHERE memory_session_id=? AND id<>?', (sid, oid)):
                        if _norm_title(other_title) == key:
                            dup = True
                            break
                auto[str(oid)] = 'dup' if dup else heuristic(otype, title)
                if sid not in cwd_of:
                    r = con.execute('SELECT %s FROM sdk_sessions WHERE memory_session_id=?' % cwd_col, (sid,)).fetchone()
                    cwd_of[sid] = r[0] if r else None
                cwd = cwd_of[sid]
                if not cwd:
                    scope[str(oid)] = 'no_cwd'
                else:
                    sc, _, _ = sb_scope.resolve_scope_id(cwd, aliases=aliases, use_git=False)
                    scope[str(oid)] = 'match' if scope_match(sc, project or '') else 'mismatch'
    except sqlite3.Error as e:
        return _result(3, 'stop', [_metric('db_read', type(e).__name__, '읽기 성공', False, str(e)[:200])],
                       'claude-mem.db 를 읽지 못했습니다 — sb health 로 확인하세요')
    path = sample_path(seed)
    prev = sb_vault.read_json_file(path) if path.exists() else None
    keep = {}
    if isinstance(prev, dict) and isinstance(prev.get('judged'), dict):   # 같은 표본에 준 사람 판정은 다시 돌려도 남긴다
        keep = {k: v for k, v in prev['judged'].items() if k in auto and v in JUDGE_VALUES}
    sample = {'seed': seed, 'n': n, 'ids': [str(i) for i in picked], 'auto': auto, 'judged': keep, 'scope': scope,
              'db': ev}
    _write_sample(sample, path)
    sample['path'] = str(path)
    return evaluate(sample)


def judge(sample_path_: str, judgments: Dict[str, str]) -> Dict[str, Any]:
    """사람·에이전트 판정(id → ok|noise|dup)을 표본 파일에 합치고 판정을 다시 계산한다."""
    p = Path(sample_path_)
    sample = sb_vault.read_json_file(p)
    if not isinstance(sample, dict) or not isinstance(sample.get('ids'), list):
        raise ValueError('표본 파일을 읽을 수 없습니다: %s' % sb_vault._short(str(p)))
    ids = {str(i) for i in sample['ids']}
    clean: Dict[str, str] = {}
    for k, v in (judgments or {}).items():
        k, v = str(k).strip(), str(v).strip().lower()
        if k not in ids:
            raise ValueError('표본에 없는 id: %s' % k)
        if v not in JUDGE_VALUES:
            raise ValueError('판정 값은 ok|noise|dup 중 하나: %s=%s' % (k, v))
        clean[k] = v
    judged = dict(sample.get('judged') or {})
    judged.update(clean)
    sample['judged'] = judged
    _write_sample(sample, p)
    sample['path'] = str(p)
    return evaluate(sample)


# ---------------------------------------------------------------- CLI

def _add(location: str, kind: Optional[str]) -> Dict[str, Any]:
    src = register(location, kind)
    src['added_via'] = 'cli'
    cls = classify(src)
    src['intake'] = cls.pop('intake')
    src['classification'] = cls
    doc = load()
    doc['sources'] = [s for s in doc['sources'] if s.get('id') != src['id']] + [src]
    save(doc)
    return src


def _find(doc: Dict[str, Any], key: str) -> Dict[str, Any]:
    key = (key or '').strip()
    loc = clean_location(key)
    hits = [s for s in doc['sources'] if s.get('id') == key or s.get('location') == loc
            or (s.get('location') and not _is_link(loc) and s.get('kind') not in SESSION_KINDS
                and not _is_link(s['location']) and _cmp(s['location']) == _cmp(loc))]
    if not hits:
        raise ValueError('등록된 자료에 없습니다: %s (sb sources list 로 id 확인)' % loc)
    return hits[0]


def _remove(key: str) -> Dict[str, Any]:
    """sb sources add 로 넣은 자료를 뺀다. 답에서 온 자료는 sb setup answer --sources 로 고친다."""
    doc = load()
    src = _find(doc, key)
    if src.get('discovered_by') == 'auto':
        raise ValueError('자동 감지된 세션은 빼지 않습니다 — 고르지 않으면 그대로 skip 입니다')
    doc['sources'] = [s for s in doc['sources'] if s.get('id') != src['id']]
    save(doc)
    return src


def _select(key: str, on: bool = True) -> Dict[str, Any]:
    """감지된 세션 소스를 사용자가 적재 대상으로 고른다(A4). 고르기 전에는 skip."""
    doc = load()
    src = _find(doc, key)
    if src.get('kind') not in SESSION_KINDS:
        raise ValueError('세션 소스만 고를 수 있습니다: %s' % src.get('kind'))
    if on:
        src.update(mode='index', selected_by='user')
    else:
        src['mode'] = 'skip'
        src.pop('selected_by', None)
    cls = classify(src)
    src['intake'] = cls.pop('intake')
    src['classification'] = cls
    save(doc)
    return src


def _render(sources: List[Dict[str, Any]]) -> str:
    if not sources:
        return '등록된 자료가 없습니다(기본 볼트로 진행합니다)'
    lines = []
    for s in sources:
        sm = s.get('summary') or {}
        extra = ''
        if s['kind'] in SESSION_KINDS:
            extra = '파일 %s · 작업 폴더 %s · 기간 %s~%s' % (sm.get('files'), sm.get('cwd_count'), *(sm.get('period') or [None, None]))
            more = [('평가 복사본', sm.get('cwd_eval_copy')), ('임시', sm.get('cwd_temp')),
                    ('관측기 제외', sm.get('excluded_observer'))]
            extra += ''.join(' · %s %s' % (k, v) for k, v in more if v)
        elif s['kind'] == 'mcp':
            extra = '%s · %s(적재 안 함)' % (s.get('category'), ','.join(sm.get('found_in') or []))
        elif s.get('mode') != 'live':
            c = s.get('classification') or {}
            extra = '파일 %s · 넣을 것 %s · 민감 %s · 잡동사니 %s · 모름 %s' % (
                sm.get('files'), c.get('own_output'), c.get('sensitive'), c.get('junk'), c.get('unknown'))
        lines.append('%-16s %-6s %s  %s' % (s['kind'], s.get('mode'), s['location'], extra))
    return '\n'.join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(prog='sb sources', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('detect')
    d.add_argument('--json', action='store_true')
    a = sub.add_parser('add')
    a.add_argument('location')
    a.add_argument('--kind', choices=('folder', 'meeting', 'link'))
    a.add_argument('--json', action='store_true')
    ls = sub.add_parser('list')
    ls.add_argument('--json', action='store_true')
    rv = sub.add_parser('remove')
    rv.add_argument('key', help='id 또는 위치')
    rv.add_argument('--json', action='store_true')
    se = sub.add_parser('select')
    se.add_argument('key', help='세션 소스 id')
    se.add_argument('--off', action='store_true', help='고른 것을 취소')
    se.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    if args.cmd == 'detect':
        out = detect_sessions() + detect_mcp()[0]
    elif args.cmd in ('add', 'remove', 'select'):
        try:
            if args.cmd == 'add':
                out = [_add(args.location, args.kind)]
            elif args.cmd == 'remove':
                out = [_remove(args.key)]
            else:
                out = [_select(args.key, not args.off)]
        except ValueError as e:
            sys.stderr.write('%s\n' % e)
            return 2
    else:
        out = load()['sources']
    print(json.dumps(out, ensure_ascii=False, indent=1) if args.json else _render(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
