"""secondbrain-kit 공통 설정 — 경로·포트·실행 파일을 한 곳에서 정한다.

모든 스크립트는 여기서 기본값을 얻는다. 개별 환경변수(SB_KO_INDEX 등)는 여전히 우선한다.

  SB_HOME            데이터 루트 (기본 ~/.secondbrain)
  SB_CLAUDE_MEM_DIR  claude-mem 데이터 (기본 ~/.claude-mem)
  SB_MEM_BASE_URL    claude-mem worker URL (기본: claude-mem settings.json → 계산값)
  SB_CLAUDE_BIN      claude CLI (기본: PATH 탐색)
  SB_LLM_MODEL       배치 LLM 모델 (기본 sonnet)

표준 라이브러리만 쓴다(시스템 파이썬·venv 어디서든 import 가능해야 한다).
"""
import json
import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = os.name == 'nt'


def home() -> Path:
    return Path(os.environ.get('SB_HOME') or '~/.secondbrain').expanduser()


def sb_path(*parts: str) -> str:
    """SB_HOME 아래 경로(문자열). 기존 코드의 '~/secondbrain/...' 기본값 대체용."""
    return str(home().joinpath(*parts))


def claude_mem_dir() -> Path:
    return Path(os.environ.get('SB_CLAUDE_MEM_DIR') or '~/.claude-mem').expanduser()


def claude_mem_db() -> str:
    return str(claude_mem_dir() / 'claude-mem.db')


def claude_mem_settings() -> dict:
    try:
        return json.loads((claude_mem_dir() / 'settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _default_port() -> int:
    """claude-mem 기본 포트 규칙: 37700 + (uid % 100). Windows 는 uid 가 없어 37777."""
    getuid = getattr(os, 'getuid', None)
    return 37700 + (getuid() % 100) if getuid else 37777


def worker_base_url() -> str:
    env = os.environ.get('SB_MEM_BASE_URL')
    if env:
        return env.rstrip('/')
    s = claude_mem_settings()
    host = str(s.get('CLAUDE_MEM_WORKER_HOST') or '127.0.0.1')
    port = s.get('CLAUDE_MEM_WORKER_PORT')
    # worker.pid 가 실제 포트를 가장 정확히 안다
    try:
        pid = json.loads((claude_mem_dir() / 'worker.pid').read_text(encoding='utf-8'))
        port = pid.get('port') or port
    except (OSError, ValueError, AttributeError):
        pass
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = _default_port()
    return 'http://%s:%d' % (host, port)


def claude_bin() -> str:
    env = os.environ.get('SB_CLAUDE_BIN')
    if env:
        return env
    found = shutil.which('claude')
    if found:
        return found
    for cand in ('~/.local/bin/claude', '~/.claude/local/claude'):
        p = Path(cand).expanduser()
        if p.exists():
            return str(p)
    return 'claude'


def llm_model() -> str:
    return os.environ.get('SB_LLM_MODEL') or 'sonnet'


def python_exe() -> str:
    return sys.executable


def path_list_sep() -> str:
    return os.pathsep


def is_abs_like(entry: str) -> bool:
    """별칭·허용 루트 항목이 절대경로(또는 ~)인지 — Windows 드라이브 경로 포함."""
    return entry.startswith(('/', '~')) or os.path.isabs(entry)
