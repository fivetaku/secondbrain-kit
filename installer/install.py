#!/usr/bin/env python3
"""secondbrain-kit 설치기 (macOS · Windows). 표준 라이브러리만 쓴다.

  python installer/install.py [--dry-run] [--no-embed] [--no-codex] [--no-schedule]
                              [--provider claude|gemini|openrouter] [--with-consolidate]
  python installer/install.py --uninstall
  python installer/install.py --doctor

하는 일 (각 단계는 멱등 — 여러 번 돌려도 같은 결과):
  1  사전 요구사항 확인 (node, git, uv, claude, codex, ollama)
  2  venv (~/.secondbrain/.venv) + requirements
  3  claude-mem 설치 (npx claude-mem install — bun/uv 자동 설치, 플러그인 등록까지)
  4  한국어 임베딩: ollama bge-m3 + Chroma 컬렉션 선생성(OpenAI 호환 EF → Ollama /v1)
  5  claude-mem settings.json 병합 (한국어 모드, 주입량)
  6  Claude Code 훅 (~/.claude/settings.json)
  7  Codex: claude-mem 플러그인 + 우리 훅 (~/.codex/hooks.json)
  8  에이전트 규칙 블록 (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md)
  9  sb 명령 (PATH)
  10 스케줄 (launchd / 작업 스케줄러)
설정 파일을 바꾸기 전에 <파일>.sbkit-bak-<시각> 백업을 남긴다.
"""
import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
HOME = Path.home()
IS_WIN = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'
SB_HOME = Path(os.environ.get('SB_HOME') or HOME / '.secondbrain')
VENV = SB_HOME / '.venv'
VPY = VENV / ('Scripts/python.exe' if IS_WIN else 'bin/python')
CM_DIR = HOME / '.claude-mem'
CLAUDE_MEM_VERSION = os.environ.get('SBKIT_CLAUDE_MEM_VERSION', '13.24.23')
CHROMA_UVX = ['--python', '3.13', '--with', 'onnxruntime>=1.20', '--with', 'protobuf<7',
              '--from', 'chroma-mcp==0.2.6']
MARK_BEGIN = '<!-- secondbrain-kit:begin -->'
MARK_END = '<!-- secondbrain-kit:end -->'
HOOK_TAG = 'secondbrain-kit'
STAMP = dt.datetime.now().strftime('%Y%m%d-%H%M%S')

DRY = False
LOG = []


# ── 공통 ─────────────────────────────────────────────────────────────
def say(msg: str) -> None:
    print(msg, flush=True)
    LOG.append(msg)


def run(cmd, check=True, capture=False, env=None, cwd=None):
    shown = ' '.join(str(c) for c in cmd)
    if DRY:
        say('  [dry-run] ' + shown)
        return subprocess.CompletedProcess(cmd, 0, '', '')
    say('  $ ' + shown)
    return subprocess.run([str(c) for c in cmd], check=check, text=True, env=env, cwd=cwd,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def which(name: str):
    return shutil.which(name)


def posix(p) -> str:
    """훅 명령에 넣을 경로. Windows 도 / 로 쓴다(Git Bash·cmd 모두 받아들인다)."""
    return str(p).replace('\\', '/')


def backup(path: Path) -> None:
    if path.exists() and not DRY:
        shutil.copy2(path, path.with_name(path.name + '.sbkit-bak-' + STAMP))


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    if DRY:
        say('  [dry-run] write ' + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def py_cmd(script: str, *args: str) -> str:
    parts = ['"%s"' % posix(VPY), '"%s"' % posix(KIT / script)] + list(args)
    return ' '.join(parts)


# ── 1 사전 요구사항 ─────────────────────────────────────────────────
def check_prereqs(args) -> dict:
    say('\n[1] 사전 요구사항')
    found = {n: which(n) for n in ('node', 'npx', 'git', 'uv', 'uvx', 'claude', 'codex', 'ollama')}
    for n, p in found.items():
        say('  %-7s %s' % (n, p or '없음'))
    missing = [n for n in ('node', 'npx', 'git') if not found[n]]
    if not found['claude'] and not found['codex']:
        missing.append('claude 또는 codex')
    if missing:
        hint = ('winget install OpenJS.NodeJS.LTS Git.Git' if IS_WIN else 'brew install node git')
        raise SystemExit('필수 도구 없음: %s\n  설치 예: %s' % (', '.join(missing), hint))
    if found['node']:
        v = run(['node', '--version'], capture=True, check=False).stdout.strip().lstrip('v') or '0'
        major, minor = (int(x) for x in (v.split('.') + ['0', '0'])[:2])
        if not DRY and (major, minor) < (20, 12):
            raise SystemExit('Node 20.12 이상이 필요합니다 (현재 %s)' % v)
    if not found['uv']:
        say('  uv 설치')
        if IS_WIN:
            run(['powershell', '-ExecutionPolicy', 'ByPass', '-c', 'irm https://astral.sh/uv/install.ps1 | iex'])
        else:
            run(['sh', '-c', 'curl -LsSf https://astral.sh/uv/install.sh | sh'])
        found['uv'] = which('uv') or str(HOME / ('.local/bin/uv.exe' if IS_WIN else '.local/bin/uv'))
        found['uvx'] = which('uvx') or str(Path(found['uv']).with_name('uvx.exe' if IS_WIN else 'uvx'))
    if not found['ollama'] and find_ollama() != 'ollama':  # 설치돼 있지만 PATH 에 없는 경우
        found['ollama'] = find_ollama()
    if not args.no_embed and not found['ollama']:
        say('  ollama 설치')
        if IS_WIN:
            run(['winget', 'install', '-e', '--id', 'Ollama.Ollama', '--accept-source-agreements',
                 '--accept-package-agreements'])
        elif which('brew'):
            run(['brew', 'install', 'ollama'])
            run(['brew', 'services', 'start', 'ollama'], check=False)
        else:
            raise SystemExit('ollama 가 필요합니다: https://ollama.com/download (또는 --no-embed)')
        found['ollama'] = find_ollama()
    return found


def find_ollama() -> str:
    """설치 직후엔 PATH 가 갱신되지 않은 창일 수 있다 — 기본 설치 위치도 본다."""
    cands = [which('ollama')]
    if IS_WIN:
        local = os.environ.get('LOCALAPPDATA', str(HOME / 'AppData' / 'Local'))
        cands.append(str(Path(local) / 'Programs' / 'Ollama' / 'ollama.exe'))
    else:
        cands += ['/opt/homebrew/bin/ollama', '/usr/local/bin/ollama',
                  '/Applications/Ollama.app/Contents/Resources/ollama']
    for c in cands:
        if c and Path(c).exists():
            return c
    return 'ollama'


def ensure_ollama_running(exe: str) -> None:
    """11434 가 응답할 때까지 최대 60초 기다리고, 안 뜨면 ollama serve 를 백그라운드로 띄운다."""
    import time
    import urllib.request

    def up() -> bool:
        try:
            urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=2).close()
            return True
        except Exception:
            return False
    if DRY or up():
        return
    say('  Ollama 서버 기동')
    flags = {'creationflags': 0x00000008 | 0x00000200} if IS_WIN else {'start_new_session': True}
    subprocess.Popen([exe, 'serve'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
    for _ in range(60):
        if up():
            return
        time.sleep(1)
    raise SystemExit('Ollama 서버가 뜨지 않습니다. Ollama 앱을 직접 실행한 뒤 다시 설치하세요 (또는 --no-embed).')


# ── 2 venv ──────────────────────────────────────────────────────────
def setup_venv(tools) -> None:
    say('\n[2] venv ' + str(VENV))
    uv = tools['uv'] or 'uv'
    if not VPY.exists():
        run([uv, 'venv', str(VENV), '--python', '3.12'])
    run([uv, 'pip', 'install', '--python', str(VPY), '-r', str(KIT / 'requirements.txt')])
    for d in ('logs', 'index', 'config', 'loops', 'rules'):
        if not DRY:
            (SB_HOME / d).mkdir(parents=True, exist_ok=True)
    aliases = SB_HOME / 'config' / 'project_aliases.json'
    if not aliases.exists() and not DRY:
        aliases.write_text('{}\n', encoding='utf-8')
    # 상태층 DB 스키마(운영 DB 보호 장치를 명시적으로 통과)
    env = dict(os.environ, SB_HOME=str(SB_HOME), SB_STATE_ALLOW_OPERATIONAL_MIGRATION='1')
    run([str(VPY), str(KIT / 'bin' / 'sb_state.py'), 'migrate', '--db', str(SB_HOME / 'state.db'),
         '--allow-operational'], env=env)


# ── 3 claude-mem ────────────────────────────────────────────────────
def install_claude_mem(args, tools) -> None:
    say('\n[3] claude-mem %s' % CLAUDE_MEM_VERSION)
    installed = (HOME / '.claude/plugins/cache/thedotmack/claude-mem').exists()
    if installed and not args.reinstall_claude_mem:
        say('  이미 설치됨 — 건너뜀 (--reinstall-claude-mem 으로 재실행)')
        return
    cmd = ['npx', '-y', 'claude-mem@%s' % CLAUDE_MEM_VERSION, 'install', '--provider', args.provider,
           '--no-auto-start']
    run(cmd if not IS_WIN else ['cmd', '/c'] + cmd)


# ── 4 임베딩 ────────────────────────────────────────────────────────
PRECREATE = textwrap.dedent('''
    import os, sys, chromadb
    from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
    os.environ.pop("OPENAI_API_KEY", None); os.environ["CHROMA_OPENAI_API_KEY"] = "ollama"
    c = chromadb.PersistentClient(path=sys.argv[1])
    ef = OpenAIEmbeddingFunction(model_name="bge-m3", api_base="http://127.0.0.1:11434/v1",
                                 api_key_env_var="CHROMA_OPENAI_API_KEY")
    col = c.get_or_create_collection("cm__claude-mem", embedding_function=ef,
                                     metadata={"hnsw:space": "cosine"})
    print("collection ok", col.count())
''')


def chroma_ef_name(chroma_dir: Path) -> str:
    db = chroma_dir / 'chroma.sqlite3'
    if not db.exists():
        return ''
    import sqlite3
    try:
        con = sqlite3.connect('file:%s?mode=ro' % db.as_posix(), uri=True)
        row = con.execute("SELECT schema_str FROM collections WHERE name='cm__claude-mem'").fetchone()
        con.close()
    except sqlite3.Error:
        return '?'
    if not row or not row[0]:
        return ''
    m = re.search(r'"embedding_function":\{"type":"known","name":"([^"]+)"', row[0])
    return m.group(1) if m else 'default'


def setup_embedding(args, tools) -> None:
    say('\n[4] 한국어 임베딩 (Ollama bge-m3)')
    if args.no_embed:
        say('  --no-embed: 건너뜀 (claude-mem 기본 영어 임베딩 사용)')
        return
    ensure_ollama_running(tools['ollama'] or 'ollama')
    run([tools['ollama'] or 'ollama', 'pull', 'bge-m3'])
    env_file = HOME / '.chroma_env'
    line = 'CHROMA_OPENAI_API_KEY=ollama'
    text = env_file.read_text(encoding='utf-8') if env_file.exists() else ''
    if line not in text and not DRY:
        env_file.write_text(text + ('' if text.endswith('\n') or not text else '\n') + line + '\n', encoding='utf-8')
    chroma = CM_DIR / 'chroma'
    ef = chroma_ef_name(chroma)
    if ef in ('openai', 'ollama'):
        say('  컬렉션이 이미 다국어 임베딩(%s) — 유지' % ef)
        return
    if ef:
        say('  ⚠ 기존 컬렉션이 %s 임베딩입니다. 교체는 재색인이 필요해 자동으로 하지 않습니다(docs/embedding.md).' % ef)
        return
    if worker_alive():
        run(['npx', '-y', 'claude-mem@%s' % CLAUDE_MEM_VERSION, 'stop'], check=False)
    tmp = SB_HOME / 'index' / 'precreate_chroma.py'
    if not DRY:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(PRECREATE, encoding='utf-8')
    env = dict(os.environ)
    env.pop('OPENAI_API_KEY', None)
    run([tools['uvx'] or 'uvx'] + CHROMA_UVX + ['python', str(tmp), str(chroma)], env=env)
    say('  컬렉션 ef=%s' % (chroma_ef_name(chroma) or '(dry-run)'))


def worker_alive() -> bool:
    """이 설치 대상(CM_DIR)의 worker 가 떠 있는지. worker.pid 가 없으면 남의 worker 로 보고 건드리지 않는다
    (같은 uid 면 포트가 같아 health 응답만으로는 구분이 안 된다)."""
    if not (CM_DIR / 'worker.pid').exists():
        return False
    sys.path.insert(0, str(KIT / 'bin'))
    import urllib.request
    import sb_config
    try:
        with urllib.request.urlopen(sb_config.worker_base_url() + '/api/health', timeout=2):
            return True
    except Exception:
        return False


# ── 5 claude-mem 설정 ───────────────────────────────────────────────
CM_SETTINGS = {
    'CLAUDE_MEM_MODE': 'code--ko',
    'CLAUDE_MEM_CONTEXT_OBSERVATIONS': '25',
    'CLAUDE_MEM_CONTEXT_SESSION_COUNT': '3',
    'CLAUDE_MEM_CONTEXT_FULL_COUNT': '0',
    'CLAUDE_MEM_CONTEXT_SHOW_LAST_SUMMARY': 'false',
    'CLAUDE_MEM_CONTEXT_SHOW_LAST_MESSAGE': 'false',
    'CLAUDE_MEM_FOLDER_CLAUDEMD_ENABLED': 'false',
}


def merge_claude_mem_settings(args) -> None:
    say('\n[5] claude-mem 설정')
    path = CM_DIR / 'settings.json'
    cur = read_json(path)
    changed = {k: v for k, v in CM_SETTINGS.items() if k not in cur or args.force_settings}
    if args.lang != 'ko':
        changed.pop('CLAUDE_MEM_MODE', None)
    cur.update(changed)
    excl = [x for x in str(cur.get('CLAUDE_MEM_EXCLUDED_PROJECTS', '')).split(',') if x]
    memdir = str(HOME / '.codex' / 'memories')  # Codex 자체 메모리 정리 에이전트
    if memdir not in excl:
        cur['CLAUDE_MEM_EXCLUDED_PROJECTS'] = ','.join(excl + [memdir])
        changed['CLAUDE_MEM_EXCLUDED_PROJECTS'] = '+codex/memories'
    if not changed:
        say('  변경 없음 (사용자 값 유지. 덮어쓰려면 --force-settings)')
        return
    write_json(path, cur)
    say('  적용: ' + ', '.join(sorted(changed)))


# ── 6·7 훅 ──────────────────────────────────────────────────────────
def _strip_ours(hooks: dict) -> dict:
    """이전 설치가 넣은 우리 훅(명령에 kit 경로 포함)을 뺀다 — 재설치 멱등."""
    kit = posix(KIT)
    out = {}
    for event, groups in (hooks or {}).items():
        kept = []
        for g in groups:
            hs = [h for h in g.get('hooks', []) if kit not in posix(h.get('command', ''))]
            if hs:
                kept.append(dict(g, hooks=hs))
        if kept:
            out[event] = kept
    return out


def _add(hooks: dict, event: str, matcher, command: str, timeout: int) -> None:
    group = {'hooks': [{'type': 'command', 'command': command, 'timeout': timeout}]}
    if matcher is not None:
        group['matcher'] = matcher
    hooks.setdefault(event, []).append(group)


def claude_hooks(args) -> None:
    say('\n[6] Claude Code 훅')
    path = HOME / '.claude' / 'settings.json'
    cur = read_json(path)
    hooks = _strip_ours(cur.get('hooks', {}))
    _add(hooks, 'SessionStart', 'startup|resume|clear|compact',
         py_cmd('hooks/session_context.py', '--harness', 'claude'), 15)
    _add(hooks, 'UserPromptSubmit', None, py_cmd('hooks/sb_recall.py'), 8)
    gate = py_cmd('hooks/recall_gate.py')
    _add(hooks, 'PreToolUse', 'AskUserQuestion', gate, 5)
    _add(hooks, 'PreToolUse', 'Edit|Write|NotebookEdit', gate, 5)
    _add(hooks, 'PostToolUse', 'Bash|PowerShell', gate, 5)
    _add(hooks, 'PostToolUse', 'mcp__plugin_claude-mem.*|mcp__.*mcp-search.*', gate, 5)
    _add(hooks, 'Stop', None, gate, 5)            # 과거 맥락 질문에 회상 없이 끝내면 1회 되돌림
    _add(hooks, 'SubagentStart', None, gate, 5)   # 서브에이전트에 기록층 사용법 주입
    cur['hooks'] = hooks
    env = cur.setdefault('env', {})
    env.setdefault('SB_HOME', str(SB_HOME))
    write_json(path, cur)


CODEX_CAPTURE = [  # (이벤트, matcher, codex_hook 인자, timeout) — claude-mem 플러그인 훅과 같은 구성
    ('SessionStart', 'startup|resume|clear|compact|fork', 'context', 20),
    ('UserPromptSubmit', None, 'session-init', 20),
    ('PreToolUse', '^Bash$|^mcp__.+__(read|view|cat)(_file|_files)?$', 'file-context', 30),
    ('PostToolUse', '.*', 'observation', 120),
    ('Stop', None, 'summarize', 60),
]


def _codex_add(hooks: dict, event: str, matcher, script: str, args: list, timeout: int) -> None:
    """Codex 훅 항목. 신뢰 키가 인덱스 기반이라 기존 배열의 맨 끝에만 붙인다(_strip_ours 뒤라 멱등)."""
    cmd = py_cmd(script, *args)
    win = '%s %s %s' % (posix(VPY), posix(KIT / script), ' '.join(args))  # PowerShell: 따옴표 없는 토큰
    h = {'type': 'command', 'command': cmd, 'timeout': timeout}
    if ' ' not in posix(VPY) + posix(KIT):
        h['commandWindows'] = win.strip()
    group = {'hooks': [h]}
    if matcher is not None:
        group['matcher'] = matcher
    hooks.setdefault(event, []).append(group)


def codex_setup(args, tools) -> None:
    say('\n[7] Codex')
    if args.no_codex or not tools.get('codex'):
        say('  건너뜀 (codex 없음 또는 --no-codex)')
        return
    codex = tools['codex']
    # 7-1 claude-mem 플러그인: MCP 검색·스킬용. 플러그인 훅은 exec 자동화까지 잡아 끈다(7-3).
    clone = HOME / '.claude/plugins/marketplaces/thedotmack'
    source = str(clone) if (clone / '.agents/plugins/marketplace.json').exists() else 'thedotmack/claude-mem'
    run([codex, 'plugin', 'marketplace', 'add', source], check=False)
    run([codex, 'plugin', 'add', 'claude-mem@claude-mem-local'], check=False)
    # 7-2 캡처(자동화 제외) + 주입 훅
    path = HOME / '.codex' / 'hooks.json'
    cur = read_json(path)
    hooks = _strip_ours(cur.get('hooks', {}))
    for event, matcher, arg, timeout in CODEX_CAPTURE:
        _codex_add(hooks, event, matcher, 'hooks/codex_hook.py', [arg], timeout)
    _codex_add(hooks, 'SessionStart', 'startup|resume|clear|compact|fork', 'hooks/session_context.py',
               ['--harness', 'codex'], 15)
    _codex_add(hooks, 'UserPromptSubmit', None, 'hooks/sb_recall.py', ['codex'], 8)
    cur['hooks'] = hooks
    write_json(path, cur)
    # 7-3 신뢰 등록(해시) + 플러그인 훅 끄기 — Codex 는 신뢰되지 않은 훅을 실행하지 않는다
    trust = [str(VPY), str(KIT / 'installer' / 'codex_trust.py')]
    if DRY:
        say('  [dry-run] codex_trust trust/disable')
        return
    run(trust + ['disable', '--plugin', 'claude-mem', '--codex', codex], check=False)
    run(trust + ['trust', '--match', posix(KIT), '--codex', codex], check=False)
    if IS_WIN:
        run(trust + ['trust', '--match', str(KIT), '--codex', codex], check=False)


# ── 8 규칙 블록 ─────────────────────────────────────────────────────
def upsert_block(path: Path) -> None:
    body = (KIT / 'templates' / 'memory-rules.md').read_text(encoding='utf-8').strip()
    block = '%s\n%s\n%s' % (MARK_BEGIN, body, MARK_END)
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    if MARK_BEGIN in text and MARK_END in text:
        new = re.sub(re.escape(MARK_BEGIN) + r'.*?' + re.escape(MARK_END), lambda _m: block, text, flags=re.S)
    else:
        new = text + ('\n\n' if text.strip() else '') + block + '\n'
    if new == text:
        say('  %s 변경 없음' % path)
        return
    if DRY:
        say('  [dry-run] update ' + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    path.write_text(new, encoding='utf-8')
    say('  %s 갱신' % path)


def rules(args, tools) -> None:
    say('\n[8] 에이전트 규칙')
    if tools.get('claude'):
        upsert_block(HOME / '.claude' / 'CLAUDE.md')
    if tools.get('codex') and not args.no_codex:
        upsert_block(HOME / '.codex' / 'AGENTS.md')


# ── 9 sb 명령 ───────────────────────────────────────────────────────
def sb_launcher() -> None:
    say('\n[9] sb 명령')
    bindir = SB_HOME / 'bin'
    if DRY:
        say('  [dry-run] %s/sb' % bindir)
        return
    bindir.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        (bindir / 'sb.cmd').write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (VPY, KIT / 'bin' / 'sb.py'), encoding='utf-8')
        # Git Bash(Claude Code 의 Bash 도구)는 .cmd 를 `sb` 로 못 부른다 — sh 런처를 같이 둔다.
        (bindir / 'sb').write_text('#!/bin/sh\nexport PYTHONIOENCODING=utf-8\nexec "%s" "%s" "$@"\n'
                                   % (Path(VPY).as_posix(), (KIT / 'bin' / 'sb.py').as_posix()), encoding='utf-8', newline='\n')
        user_path = subprocess.run(['powershell', '-NoProfile', '-c',
                                    "[Environment]::GetEnvironmentVariable('Path','User')"],
                                   text=True, capture_output=True).stdout.strip()
        if str(bindir).lower() not in user_path.lower():
            run(['powershell', '-NoProfile', '-c',
                 "[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';%s', 'User')" % bindir])
            say('  PATH 에 %s 추가 — 새 터미널부터 적용' % bindir)
    else:
        sb = bindir / 'sb'
        sb.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (VPY, KIT / 'bin' / 'sb.py'), encoding='utf-8')
        sb.chmod(0o755)
        link = HOME / '.local' / 'bin' / 'sb'
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(sb)
        if str(link.parent) not in os.environ.get('PATH', '').split(os.pathsep):
            say('  ⚠ ~/.local/bin 이 PATH 에 없습니다. 셸 설정에 추가하세요: export PATH="$HOME/.local/bin:$PATH"')


# ── 10 스케줄 ───────────────────────────────────────────────────────
JOBS = [  # (이름, 인자, 스케줄)
    ('koindex', ['bin/sb_fts_ko.py', 'build'], {'interval': 3600}),
    ('nightly', ['bin/nightly.py'], {'hour': 5, 'minute': 7}),
]


def schedule(args) -> None:
    say('\n[10] 스케줄')
    if args.no_schedule:
        say('  건너뜀')
        return
    jobs = list(JOBS)
    if args.with_consolidate:
        jobs.append(('consolidate', ['bin/consolidate.py', 'run', '--since-days', '7'],
                     {'weekday': 0, 'hour': 6, 'minute': 13}))
    for name, argv, when in jobs:
        (_launchd if IS_MAC else _schtasks if IS_WIN else _cron_hint)(name, argv, when)


def _launchd(name, argv, when) -> None:
    label = 'com.secondbrain-kit.' + name
    plist = HOME / 'Library' / 'LaunchAgents' / (label + '.plist')
    prog = ''.join('<string>%s</string>' % s for s in [str(VPY)] + [str(KIT / argv[0])] + argv[1:])
    if 'interval' in when:
        sched = '<key>StartInterval</key><integer>%d</integer>' % when['interval']
    else:
        items = ''.join('<key>%s</key><integer>%d</integer>' % (k.capitalize(), v) for k, v in when.items())
        sched = '<key>StartCalendarInterval</key><dict>%s</dict>' % items
    log = SB_HOME / 'logs' / (name + '.launchd.log')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
           '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
           '<key>Label</key><string>%s</string><key>ProgramArguments</key><array>%s</array>%s'
           '<key>EnvironmentVariables</key><dict><key>SB_HOME</key><string>%s</string>'
           '<key>SB_RECALL</key><string>0</string><key>PATH</key><string>%s</string></dict>'
           '<key>Nice</key><integer>5</integer>'
           '<key>StandardOutPath</key><string>%s</string><key>StandardErrorPath</key><string>%s</string>'
           '</dict></plist>\n') % (label, prog, sched, SB_HOME,
                                   os.pathsep.join([str(HOME / '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin',
                                                    '/usr/bin', '/bin']), log, log)
    if DRY:
        say('  [dry-run] %s' % plist)
        return
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(xml, encoding='utf-8')
    uid = str(os.getuid())
    subprocess.run(['launchctl', 'bootout', 'gui/%s/%s' % (uid, label)], capture_output=True)
    run(['launchctl', 'bootstrap', 'gui/' + uid, str(plist)])


def _schtasks(name, argv, when) -> None:
    task = 'secondbrain-kit\\' + name
    cmd = '"%s" "%s" %s' % (VPY, KIT / argv[0], ' '.join(argv[1:]))
    if 'interval' in when:
        sched = ['/SC', 'HOURLY', '/MO', str(max(1, when['interval'] // 3600))]
    elif 'weekday' in when:
        sched = ['/SC', 'WEEKLY', '/D', 'SUN', '/ST', '%02d:%02d' % (when['hour'], when['minute'])]
    else:
        sched = ['/SC', 'DAILY', '/ST', '%02d:%02d' % (when['hour'], when['minute'])]
    run(['schtasks', '/Create', '/F', '/TN', task, '/TR', cmd] + sched)


def _cron_hint(name, argv, when) -> None:
    say('  (이 OS 는 자동 등록 미지원) 수동 cron: %s %s' % (VPY, ' '.join([str(KIT / argv[0])] + argv[1:])))


# ── 제거 ────────────────────────────────────────────────────────────
def uninstall() -> None:
    say('제거: 훅·규칙 블록·스케줄·sb 명령 (데이터 ~/.secondbrain, ~/.claude-mem 은 남긴다)')
    for path in (HOME / '.claude' / 'settings.json', HOME / '.codex' / 'hooks.json'):
        cur = read_json(path)
        if cur.get('hooks'):
            cur['hooks'] = _strip_ours(cur['hooks'])
            write_json(path, cur)
    for path in (HOME / '.claude' / 'CLAUDE.md', HOME / '.codex' / 'AGENTS.md'):
        if path.exists():
            t = path.read_text(encoding='utf-8')
            n = re.sub(r'\n*' + re.escape(MARK_BEGIN) + r'.*?' + re.escape(MARK_END) + r'\n?', '\n', t, flags=re.S)
            if n != t and not DRY:
                backup(path)
                path.write_text(n, encoding='utf-8')
    for name in ('koindex', 'nightly', 'consolidate'):
        if IS_MAC:
            label = 'com.secondbrain-kit.' + name
            subprocess.run(['launchctl', 'bootout', 'gui/%d/%s' % (os.getuid(), label)], capture_output=True)
            p = HOME / 'Library' / 'LaunchAgents' / (label + '.plist')
            if p.exists() and not DRY:
                p.unlink()
        elif IS_WIN:
            subprocess.run(['schtasks', '/Delete', '/F', '/TN', 'secondbrain-kit\\' + name], capture_output=True)
    link = HOME / '.local' / 'bin' / 'sb'
    if link.is_symlink() and not DRY:
        link.unlink()
    say('claude-mem 플러그인 자체 제거: claude plugin uninstall claude-mem@thedotmack / codex plugin remove claude-mem@thedotmack')


def main() -> None:
    global DRY
    ap = argparse.ArgumentParser(description='secondbrain-kit installer')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--uninstall', action='store_true')
    ap.add_argument('--doctor', action='store_true')
    ap.add_argument('--no-embed', action='store_true', help='Ollama 한국어 임베딩 생략')
    ap.add_argument('--no-codex', action='store_true')
    ap.add_argument('--no-schedule', action='store_true')
    ap.add_argument('--with-consolidate', action='store_true', help='주간 LLM 통합 요약 스케줄 추가')
    ap.add_argument('--provider', default='claude', choices=('claude', 'gemini', 'openrouter'))
    ap.add_argument('--lang', default='ko', choices=('ko', 'en'))
    ap.add_argument('--force-settings', action='store_true')
    ap.add_argument('--reinstall-claude-mem', action='store_true')
    args = ap.parse_args()
    DRY = args.dry_run
    if args.doctor:
        sys.path.insert(0, str(KIT / 'installer'))
        import doctor
        sys.exit(doctor.main())
    if args.uninstall:
        uninstall()
        return
    say('secondbrain-kit 설치 — %s %s, SB_HOME=%s' % (platform.system(), platform.machine(), SB_HOME))
    tools = check_prereqs(args)
    setup_venv(tools)
    install_claude_mem(args, tools)
    setup_embedding(args, tools)
    merge_claude_mem_settings(args)
    if tools.get('claude'):
        claude_hooks(args)
    codex_setup(args, tools)
    rules(args, tools)
    sb_launcher()
    schedule(args)
    say('\n완료. 확인: python installer/install.py --doctor  (새 세션을 열면 재개 브리핑이 뜹니다)')


if __name__ == '__main__':
    main()
