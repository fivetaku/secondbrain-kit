#!/usr/bin/env python3
"""secondbrain-kit 상태 점검 — 설치가 실제로 동작하는지 한 번에 확인한다(읽기 전용).

  python installer/install.py --doctor     (또는 python installer/doctor.py)
✅ 정상 / ⚠ 동작은 하나 권장과 다름 / ❌ 고쳐야 함. ❌ 가 하나라도 있으면 exit 1.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import sys
import time
import urllib.request
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / 'bin'))
sys.path.insert(0, str(KIT / 'installer'))
import sb_config  # noqa: E402
import sb_claude_mem  # noqa: E402
import install  # noqa: E402  설치기와 같은 조건(Claude 버전 게이트·같은 역할 훅 판별)으로 점검한다

HOME = Path.home()
IS_WIN = os.name == 'nt'
VPY = sb_config.home() / '.venv' / ('Scripts/python.exe' if IS_WIN else 'bin/python')
rows = []


def rec(level: str, name: str, detail: str = '') -> None:
    rows.append((level, name, detail))


def _get(url: str, timeout: float = 3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


# 자식 파이썬 출력은 UTF-8 로 받는다 — 윈도우 cp949 파이프에서 한국어·이모지가 깨지거나 죽지 않게
# stdin 은 물려주지 않는다 — 설치기(12단계 셋업)가 터미널을 쥔 채 doctor 를 돌려도 자식이 입력을 기다리지 않게
UTF8 = dict(capture_output=True, text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL,
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
CHILD_TIMEOUT = 120      # 자식 파이썬 점검 하나의 상한(멈춘 자식이 설치·셋업 전체를 붙잡지 않게)
UVX_TIMEOUT = 300        # chroma-mcp 첫 기동은 패키지 내려받기로 오래 걸린다. sb setup 1단계는 더 짧게 바꿔 쓴다


def check_venv():
    if not VPY.exists():
        return rec('❌', 'venv', '%s 없음 — install.py 재실행' % VPY)
    r = subprocess.run([str(VPY), '-c', 'import chromadb, kiwipiepy, openai; print(chromadb.__version__)'],
                       timeout=CHILD_TIMEOUT, **UTF8)
    rec('✅' if r.returncode == 0 else '❌', 'venv 패키지', (r.stdout or r.stderr).strip()[-120:])


def check_worker():
    url = sb_config.worker_base_url()
    try:
        _get(url + '/api/health')
        rec('✅', 'claude-mem worker', url)
    except Exception as exc:
        rec('⚠', 'claude-mem worker', '%s 응답 없음(%s) — Claude/Codex 세션을 열면 자동 기동' % (url, type(exc).__name__))


def check_settings():
    s = sb_config.claude_mem_settings()
    if not s:
        return rec('❌', 'claude-mem 설정', '~/.claude-mem/settings.json 없음')
    rec('✅', 'claude-mem 설정', 'provider=%s model=%s mode=%s' % (
        s.get('CLAUDE_MEM_PROVIDER', 'claude'), s.get('CLAUDE_MEM_MODEL', '-'), s.get('CLAUDE_MEM_MODE', '-')))
    # 클라우드 동기화·원격 메모리 키가 채워져 있으면 기록이 이 PC 밖으로 나갈 수 있다(키 이름만 보고 값은 출력하지 않는다).
    remote = sorted(k for k, v in s.items() if isinstance(k, str) and k.startswith(REMOTE_KEY_PREFIXES) and str(v or '').strip())
    if remote:
        rec('⚠', 'claude-mem 원격 동기화', '설정 키 %d개가 채워져 있음(%s) — 키트는 기록이 이 PC 에만 있다고 가정한다. '
            '쓰지 않으면 claude-mem 설정에서 끄세요' % (len(remote), ', '.join(k.replace('CLAUDE_MEM_', '') for k in remote[:4])))


REMOTE_KEY_PREFIXES = ('CLAUDE_MEM_CLOUD_SYNC_', 'CLAUDE_MEM_PRO_MEMORY_')


def parse_num_batch(parameters: str) -> int:
    m = re.search(r'^num_batch\s+(\d+)', parameters or '', re.M)
    return int(m.group(1)) if m else 0


def embed_num_batch(model='bge-m3-ko'):
    """임베딩 모델 파라미터의 num_batch(없으면 0, 조회 실패면 None). Ollama 기본은 2048토큰에서 조용히 자른다."""
    try:
        req = urllib.request.Request('http://127.0.0.1:11434/api/show', data=json.dumps({'model': model}).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=5) as r:
            return parse_num_batch(json.loads(r.read().decode('utf-8')).get('parameters') or '')
    except Exception:
        return None


def check_embedding():
    db = sb_config.claude_mem_dir() / 'chroma' / 'chroma.sqlite3'
    if not db.exists():
        return rec('⚠', '벡터 컬렉션', '아직 없음(첫 관측 저장 때 생성)')
    try:
        con = sqlite3.connect('file:%s?mode=ro' % db.as_posix(), uri=True)
        row = con.execute("SELECT schema_str FROM collections WHERE name='cm__claude-mem'").fetchone()
        con.close()
    except sqlite3.Error as exc:
        return rec('⚠', '벡터 컬렉션', str(exc))
    text = (row or [''])[0] or ''
    name = 'openai' if '"name":"openai"' in text else 'ollama' if '"name":"ollama"' in text else 'default'
    mm = re.search(r'"model_name":"([^"]+)"', text)
    model = mm.group(1) if mm else ''
    if name == 'default':
        rec('⚠', '벡터 임베딩', 'default (영어 전용 — 한국어 검색 약함)')
    elif 'bge-m3' not in text:
        rec('⚠', '벡터 임베딩', name + ' 이지만 bge-m3 계열이 아님 — 키트 설정과 다른 기존 컬렉션(docs/embedding.md)')
    elif model == 'bge-m3':
        rec('✅', '벡터 임베딩', name + ' / bge-m3 (BGE-m3-ko 가 한국어 회수에 더 낫다 — 바꾸려면 재색인, docs/embedding.md)')
    else:
        rec('✅', '벡터 임베딩', name + ' / ' + (model or 'bge-m3 계열'))
    if name in ('openai', 'ollama'):
        model = model or 'bge-m3-ko'
        try:
            tags = json.loads(_get('http://127.0.0.1:11434/api/tags'))
            ok = any(m.get('name', '').split(':')[0] == model for m in tags.get('models', []))
            rec('✅' if ok else '❌', 'Ollama ' + model, '있음' if ok else '없음 — 설치기 재실행(4단계가 모델을 준비)')
            if ok:
                nb = embed_num_batch(model)
                if nb is not None:
                    rec('✅' if nb >= 8192 else '⚠', model + ' 입력 한도',
                        'num_batch %d (8192토큰)' % nb if nb >= 8192 else
                        'num_batch 미설정 — 2048토큰 넘는 기록은 앞부분만 임베딩됨. 설치기 재실행(docs/embedding.md)')
        except Exception:
            rec('❌', 'Ollama', '127.0.0.1:11434 응답 없음 — Ollama 앱/서비스를 켜세요')
        if name == 'openai':
            env = HOME / '.chroma_env'
            ok = env.exists() and 'CHROMA_OPENAI_API_KEY' in env.read_text(encoding='utf-8')
            rec('✅' if ok else '❌', '~/.chroma_env', '있음' if ok else 'CHROMA_OPENAI_API_KEY=ollama 줄 필요')


def venv_chromadb():
    """키트 venv 의 chromadb 버전(없거나 못 읽으면 None)."""
    if not VPY.exists():
        return None
    try:
        r = subprocess.run([str(VPY), '-c', 'import chromadb; print(chromadb.__version__)'], timeout=60, **UTF8)
    except Exception:  # noqa: BLE001
        return None
    return (r.stdout or '').strip() or None if r.returncode == 0 else None


def check_claude_mem_version():
    """설치된 claude-mem 이 키트 검증 범위 안인지, 워커가 고정한 chromadb 가 키트 venv 와 같은지(경고만, 차단 없음)."""
    root = sb_claude_mem.plugin_root()
    v = sb_claude_mem.installed_version(root)
    base = sb_claude_mem.plugin_base()
    if not v:
        broken = base.is_dir() and any(base.iterdir())
        return rec('❌' if broken else '⚠', 'claude-mem 버전',
                   '플러그인 폴더는 있으나 쓸 수 있는 워커(scripts/worker-service.cjs)가 없음 — install.py --reinstall-claude-mem'
                   if broken else '플러그인 캐시에서 설치판을 못 찾음 — install.py 재실행')
    lo, hi = sb_claude_mem.TESTED
    if sb_claude_mem.in_tested(v):
        rec('✅', 'claude-mem 버전', '%s (키트 검증 %s~%s, 권장 %s)' % (v, lo, hi, sb_claude_mem.RECOMMENDED))
    else:
        rec('⚠', 'claude-mem 버전', '%s — 키트 검증 범위(%s~%s) 밖. 아래 chroma-mcp 기동 결과를 확인하세요' % (v, lo, hi))
    withs, _, ok = sb_claude_mem.worker_chroma_spec(root)
    if not ok:
        rec('⚠', 'claude-mem 워커 인자', '%s 의 chroma-mcp 인자를 읽지 못함 — 고정 도구가 워커와 어긋날 수 있음(키트 업데이트 확인)' % v)
    want = sb_claude_mem.pinned_chromadb(withs) if ok else None
    have = venv_chromadb()
    if want and have and want != have:
        rec('⚠', 'chromadb 판 일치', '워커 %s / 키트 venv %s — 디스크 포맷이 갈릴 수 있음(requirements.txt 를 워커 판에 맞춘 키트로 갱신)'
            % (want, have))
    elif want and have:
        rec('✅', 'chromadb 판 일치', '%s' % have)


def check_chroma_mcp():
    """워커와 같은 인자로 chroma-mcp 를 --help 까지 띄워 본다(워커 prewarm 과 같은 확인).
    2026-10-12 pydantic 2.14 로 시작 즉시 ImportError 가 났는데, 워커는 재시도 차단기 뒤로 실패를 삼켜
    벡터 동기화가 4시간 넘게 조용히 멈췄다 — 로그를 보지 않으면 알 수 없어 여기서 직접 띄운다."""
    s = sb_config.claude_mem_settings() or {}
    if str(s.get('CLAUDE_MEM_CHROMA_ENABLED', 'true')).lower() == 'false':
        return rec('⚠', 'chroma-mcp 기동', 'CLAUDE_MEM_CHROMA_ENABLED=false — 벡터 동기화 꺼짐')
    uvx = shutil.which('uvx')
    if not uvx:
        return rec('❌', 'chroma-mcp 기동', 'uvx 없음 — 워커가 chroma-mcp 를 띄울 수 없어 벡터 동기화가 멈춤')
    py = sb_claude_mem.python_version(s)
    withs, ver, parsed = sb_claude_mem.worker_chroma_spec()
    # 워커처럼 가상환경 변수는 빼고 띄운다(uv run 으로 doctor 를 돌리면 VIRTUAL_ENV 가 잡혀 있다)
    env = {k: v for k, v in UTF8['env'].items()
           if k not in ('VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV')}
    try:
        r = subprocess.run([uvx, '--python', py] + sb_claude_mem.worker_args(withs, ver) + ['--help'], cwd=str(HOME), timeout=UVX_TIMEOUT,
                           **dict(UTF8, env=env))
    except subprocess.TimeoutExpired:
        return rec('⚠', 'chroma-mcp 기동', '%d초 안에 안 뜸(첫 실행은 패키지 내려받기로 오래 걸림) — 다시 확인' % UVX_TIMEOUT)
    out = (r.stderr or '') + (r.stdout or '')
    if r.returncode == 0:
        if not parsed:
            return rec('⚠', 'chroma-mcp 기동', '기본 인자로는 기동 — 설치된 워커 인자를 읽지 못함(worker-service.cjs 형식 변경?)')
        return rec('✅', 'chroma-mcp 기동', '워커와 같은 인자로 정상 기동')
    if 'eval_type_backport' in out:
        return rec('❌', 'chroma-mcp 기동', 'pydantic 2.14+ 와 mcp 1.6.0 비호환으로 시작 즉시 ImportError — 벡터 동기화 멈춤. '
                   'install.py 를 다시 실행해 고정 도구(워커 인자 + %s)를 까세요' % sb_claude_mem.PYDANTIC_PIN)
    tail = [x for x in out.strip().splitlines() if x.strip()]
    rec('❌', 'chroma-mcp 기동', 'rc=%d %s' % (r.returncode, (tail[-1] if tail else '')[:120]))


def _has_kit_hook(path: Path, event: str) -> bool:
    try:
        hooks = json.loads(path.read_text(encoding='utf-8')).get('hooks', {})
    except (OSError, ValueError):
        return False
    kit = str(KIT).replace('\\', '/')
    return any(kit in str(h.get('command', '')).replace('\\', '/') for g in hooks.get(event, []) for h in g.get('hooks', []))


# 이벤트별 키트 훅 스크립트 — 설치기 claude_hooks() 와 같은 구성
CLAUDE_HOOK_SCRIPTS = {'SessionStart': 'hooks/session_context.py', 'UserPromptSubmit': 'hooks/sb_recall.py',
                       'PreToolUse': 'hooks/recall_gate.py', 'Stop': 'hooks/recall_gate.py',
                       'SubagentStart': 'hooks/recall_gate.py', 'PostToolUseFailure': 'hooks/sb_error_recall.py'}


def claude_events() -> list:
    """점검할 Claude 훅 이벤트. PostToolUseFailure 는 설치기처럼 지원 버전(2.1.119+)에서만 본다."""
    events = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'Stop', 'SubagentStart']
    if install.claude_version() >= install.MIN_CLAUDE_FOR_FAILURE_HOOK:
        events.append('PostToolUseFailure')
    return events


def _claude_hook_status(path: Path, event: str) -> tuple:
    """(수준, 대신 충족한 사용자 훅 명령). ✅ 키트 훅이거나 같은 .py 스크립트(키트의 예전 형식 — 설치기가 조용히 건너뜀),
    ⚠ 이름만 겹치는 키트 밖 훅(.sh 래퍼 등)으로만 충족 — 다른 도구의 훅이면 키트 기능이 실제로는 안 돈다, ❌ 없음."""
    if _has_kit_hook(path, event):
        return '✅', ''
    try:
        hooks = json.loads(path.read_text(encoding='utf-8')).get('hooks', {})
    except (OSError, ValueError):
        return '❌', ''
    found = install._find_equivalent(hooks, event, install.py_cmd(CLAUDE_HOOK_SCRIPTS[event]))
    if not found:
        return '❌', ''
    return ('✅' if found[1] else '⚠'), found[0]


def check_recall_smoke():
    """등록만 보지 않고 실제로 돌려 본다 — 한국어 프롬프트가 깨지면 회수가 조용히 전부 빗나간다(윈도우 cp949)."""
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONIOENCODING', 'PYTHONUTF8')}
    env['SB_RECALL_GATE_DIR'] = tempfile.mkdtemp(prefix='sb-doctor-')
    payload = json.dumps({'prompt': '지난주에 한 작업 어떻게 됐지', 'session_id': 'sb-doctor-%d' % os.getpid(),
                          'cwd': str(HOME)}, ensure_ascii=False).encode('utf-8')
    try:
        t0 = time.time()
        r = subprocess.run([str(VPY if VPY.exists() else sys.executable), str(KIT / 'hooks' / 'sb_recall.py')],
                           input=payload, capture_output=True, timeout=30, env=env)
        ctx = json.loads(r.stdout.decode('utf-8') or '{}').get('hookSpecificOutput', {}).get('additionalContext', '')
    except Exception as e:  # noqa: BLE001
        return rec('❌', '회수 훅 실동작', '실행 실패: %s' % e)
    ok = 'sb timeline' in ctx
    took = time.time() - t0
    if took > 10:   # 훅 제한 시간(15초)에 가까우면 결과가 버려질 수 있다 — 프로세스 기동이 느린 PC
        rec('⚠', '회수 훅 소요 시간', '%.1f초 — 제한 15초에 근접(백신 검사·저전력 모드 확인)' % took)
    rec('✅' if ok else '❌', '회수 훅 실동작', '한국어 프롬프트 → 회수 주입 정상' if ok else '한국어 프롬프트에 주입 없음(인코딩 확인)')


def check_claude():
    if not shutil.which('claude'):
        return rec('⚠', 'Claude Code', '미설치 — 건너뜀')
    p = HOME / '.claude' / 'settings.json'
    events = claude_events()
    status = {e: _claude_hook_status(p, e) for e in events}
    missing = [e for e in events if status[e][0] == '❌']
    borrowed = ['%s(%s)' % (e, status[e][1]) for e in events if status[e][0] == '⚠']
    detail = []
    if missing:
        detail.append('누락: ' + ', '.join(missing))
    if borrowed:   # 키트 훅 대신 이름만 같은 사용자 훅이 걸려 있다 — 다른 도구의 것이면 지우고 install 재실행
        detail.append('키트 훅 대신 같은 이름의 사용자 훅: ' + '; '.join(borrowed))
    rec('❌' if missing else '⚠' if borrowed else '✅', 'Claude 훅', ' / '.join(detail) or '%d종 등록' % len(events))
    check_recall_smoke()
    cache = HOME / '.claude/plugins/cache/thedotmack/claude-mem'
    rec('✅' if cache.exists() else '❌', 'Claude claude-mem 플러그인', str(cache) if cache.exists() else '미설치')


def check_codex():
    codex = shutil.which('codex')
    if not codex:
        return rec('⚠', 'Codex', '미설치 — 건너뜀')
    p = HOME / '.codex' / 'hooks.json'
    missing = [e for e in ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop') if not _has_kit_hook(p, e)]
    rec('✅' if not missing else '❌', 'Codex 훅', '등록' if not missing else '누락: ' + ', '.join(missing))
    r = subprocess.run([str(VPY if VPY.exists() else sys.executable), str(KIT / 'installer' / 'codex_trust.py'),
                        'list', '--json'], timeout=60, **UTF8)
    try:
        hs = json.loads(r.stdout or '[]')
    except ValueError:
        return rec('❌', 'Codex 훅 신뢰', 'app-server 조회 실패: ' + (r.stderr or '').strip()[-100:])
    kit = str(KIT).replace('\\', '/')
    ours = [h for h in hs if kit in str(h.get('command') or '').replace('\\', '/')]
    bad = [h for h in ours if h.get('trustStatus') != 'trusted']
    plugin_on = [h for h in hs if 'claude-mem' in str(h.get('pluginId') or '') and h.get('enabled')]
    rec('✅' if ours and not bad else '❌', 'Codex 훅 신뢰', '%d개 신뢰됨' % len(ours) if ours and not bad else
        '%d/%d개 미신뢰 — python "%s" trust --match "%s"' % (len(bad), len(ours), KIT / 'installer' / 'codex_trust.py', kit))
    rec('✅' if not plugin_on else '⚠', 'Codex claude-mem 플러그인 훅',
        '꺼짐(우리 훅이 대신 캡처)' if not plugin_on else '켜져 있음 — exec 자동화까지 캡처됨')


def check_rules():
    for p in (HOME / '.claude' / 'CLAUDE.md', HOME / '.codex' / 'AGENTS.md'):
        if not p.parent.exists():
            continue
        ok = p.exists() and 'secondbrain-kit:begin' in p.read_text(encoding='utf-8')
        rec('✅' if ok else '❌', '규칙 블록 ' + p.parent.name + '/' + p.name, '있음' if ok else '없음')


def _answered_agents():
    """sb setup 첫 실행 답의 '쓰는 에이전트'(소문자 목록). 답이 없거나 못 읽으면 None — 그때는 예전처럼 모두 ⚠."""
    try:
        import sb_setup
        agents = sb_setup.flat_answers(sb_setup.load()).get('agents')
    except BaseException:  # noqa: BLE001 — 깨진 setup.json 은 load 가 SystemExit 을 던진다. 점검은 계속한다
        return None
    if not isinstance(agents, list):
        return None
    return [str(a).strip().lower() for a in agents if str(a).strip()]


def check_state_and_capture():
    r = subprocess.run([str(VPY if VPY.exists() else sys.executable), str(KIT / 'bin' / 'sb_search.py'),
                        '--mode', 'current', '--scope', 'global'], timeout=CHILD_TIMEOUT, **UTF8)
    rec('✅' if r.returncode in (0,) else '❌', '상태층(state.db)', (r.stdout or r.stderr).strip().splitlines()[0][:100]
        if (r.stdout or r.stderr).strip() else 'rc=%d' % r.returncode)
    db = Path(sb_config.claude_mem_db())
    if not db.exists():
        return rec('⚠', '캡처', 'claude-mem.db 없음(첫 세션 뒤 생성)')
    con = sqlite3.connect('file:%s?mode=ro' % db.as_posix(), uri=True)
    used = _answered_agents()
    for src in ('claude', 'codex'):
        row = con.execute('SELECT MAX(started_at_epoch), COUNT(*) FROM sdk_sessions WHERE platform_source=?', (src,)).fetchone()
        if row and row[1]:
            age_h = (time.time() * 1000 - (row[0] or 0)) / 3.6e6
            detail = '세션 %d개, 최근 %.0f시간 전' % (row[1], age_h)
            if age_h < 72:
                rec('✅', '캡처 %s' % src, detail)
            elif used is not None and src not in used:   # 셋업에서 안 쓴다고 답한 에이전트 — 끊김이 정상이다
                rec(INFO, '캡처 %s' % src, detail + ' — 셋업 답에서 쓰지 않는 에이전트(정보)')
            else:
                rec('⚠', '캡처 %s' % src, detail)
        elif used is not None and src not in used:
            rec(INFO, '캡처 %s' % src, '아직 없음 — 셋업 답에서 쓰지 않는 에이전트(정보)')
        else:
            rec('⚠', '캡처 %s' % src, '아직 없음 — 세션을 한 번 열고 도구를 써 보세요')
    con.close()


# Chroma 단일 쓰기 잠금 — claude-mem 워커는 Chroma 폴더에 잠금 파일을 두고, 다른 프로세스가 같은 폴더를 잡고 있으면
# 'Chroma data dir … is already owned by PID …; refusing to start a second writer' 로 기동을 거부한다(13.34.2 worker-service.cjs).
# 그 뒤에는 backoff·Batch add 실패가 이어지고 검색은 FTS 로 떨어진다 — 벡터 저장이 밀려 회수 품질이 떨어진다.
CHROMA_LOCK_FILE = '.claude-mem-chroma-writer.lock'
CHROMA_LOCK_PATTERNS = (b'is already owned by PID', b'refusing to start a second writer', b'chroma-mcp connection in backoff',
                        b'Batch add failed', b'falling back to FTS')
CHROMA_LOCK_DAYS = 7
CHROMA_LOCK_STRONG = 20            # 7일 합계가 이 이상이면 강한 경고 + 확인 방법
LOG_TAIL_BYTES = 20 * 1024 * 1024  # 파일당 끝에서 이만큼만 읽는다(큰 로그 대비)
INFO = 'ℹ'                         # 정보 — 판정에 영향 없음(❌ 가 아니므로 sb setup 1단계도 통과)


# 워커가 직접 찍은 경고·오류 줄의 머리(claude-mem 로거 형식 '[YYYY-MM-DD HH:MM:SS.mmm] [ERROR] '·'[WARN ] ').
# INFO 줄(훅·큐가 찍는 도구 입력)과 머리 없는 이어짐 줄(관측 본문·여러 줄 출력)은 문구가 들어 있어도 잠금 증상이 아니다.
_WORKER_PROBLEM_HEAD = re.compile(rb'\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?\] \[(?:ERROR|WARN) ?\]')


def _count_log_lines(data: bytes, patterns=CHROMA_LOCK_PATTERNS) -> int:
    """patterns 중 하나라도 든 워커 경고·오류 줄 수(한 줄에 둘 이상 있어도 1). INFO·훅 줄과 이어짐 줄은 뺀다."""
    starts = set()
    for p in patterns:
        i = data.find(p)
        while i >= 0:
            s = data.rfind(b'\n', 0, i) + 1
            if s not in starts and _WORKER_PROBLEM_HEAD.match(data, s, i) and b'] [HOOK' not in data[s:i]:
                starts.add(s)
            i = data.find(p, i + len(p))
    return len(starts)


def _read_tail(path: Path, limit=None) -> bytes:
    limit = limit or LOG_TAIL_BYTES
    with path.open('rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - limit))
        data = f.read(limit)
    if size > limit:   # 잘린 첫 줄은 버린다
        data = data[data.find(b'\n') + 1:]
    return data


def pid_alive(pid: int):
    """프로세스가 살아 있으면 True, 없으면 False, 판정 못 하면 None. 신호는 보내지 않는다(0 신호·조회만)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if IS_WIN:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False if k32.GetLastError() == 87 else None   # 87 = 잘못된 매개변수(그런 PID 없음)
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return None
                return code.value == 259   # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # 다른 사용자의 프로세스 — 살아 있다
        return True
    except OSError:
        return None
    return True


def _chroma_dir() -> Path:
    return Path(os.environ.get('SB_CHROMA_PATH') or sb_config.claude_mem_dir() / 'chroma')


def chroma_lock_owner():
    """(잠금 파일 있음, pid, 살아 있음). 잠금 파일 JSON 에서 pid 만 읽는다 — 다른 필드는 보지도 내보내지도 않는다."""
    p = _chroma_dir() / CHROMA_LOCK_FILE
    if not p.is_file():
        return False, None, None
    try:
        pid = json.loads(p.read_text(encoding='utf-8', errors='replace')).get('pid')
    except (OSError, ValueError, AttributeError):
        return True, None, None
    if not isinstance(pid, int) or isinstance(pid, bool):
        return True, None, None
    return True, pid, pid_alive(pid)


def _log_today():
    """오늘 로그 파일의 날짜. 워커는 파일 이름을 new Date().toISOString() 의 UTC 날짜로 정한다(줄 안의 시각은 현지 시각)
    — 현지 날짜로 고르면 UTC 보다 늦은 시간대의 저녁에는 가장 최근 파일을 놓친다."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date()


def check_chroma_lock(today=None):
    """최근 7일 claude-mem 워커 로그의 잠금 충돌·backoff 줄 수와 잠금 파일 주인 PID 생존 여부(읽기 전용)."""
    from datetime import timedelta
    logs = sb_config.claude_mem_dir() / 'logs'
    has, pid, alive = chroma_lock_owner()
    owner = ''
    if has:
        owner = (' · 잠금 파일 주인 PID %d %s' % (pid, '살아 있음' if alive else '없음(남은 잠금)' if alive is False else '확인 못 함')
                 if pid is not None else ' · 잠금 파일 pid 못 읽음')
    if not logs.is_dir():
        return rec(INFO, 'Chroma 잠금', '로그 없음(%s)%s' % (_short(logs), owner))
    today = today or _log_today()
    total, files = 0, 0
    for k in range(CHROMA_LOCK_DAYS):
        p = logs / ('claude-mem-%s.log' % (today - timedelta(days=k)).isoformat())
        try:
            data = _read_tail(p)
        except OSError:
            continue
        files += 1
        total += _count_log_lines(data)
    detail = '최근 %d일 로그 %d개에서 잠금 충돌·backoff 줄 %d건%s' % (CHROMA_LOCK_DAYS, files, total, owner)
    if total == 0:
        return rec('✅', 'Chroma 잠금', detail)
    if total < CHROMA_LOCK_STRONG:
        return rec('⚠', 'Chroma 잠금', detail)
    rec('⚠', 'Chroma 잠금', detail + ' — 워커와 다른 프로세스가 같은 Chroma 폴더를 잡고 있습니다. 확인: Claude·Codex 세션을 모두 닫고 '
        '남은 chroma-mcp/uvx 프로세스를 찾아 끄세요(%s). 그다음 `sb health --fix`(꺼진 워커를 띄움)나 새 세션으로 워커를 다시 띄웁니다'
        % ('tasklist | findstr /i "uvx chroma"' if IS_WIN else 'ps ax | grep -i chroma-mcp'))


def _short(p) -> str:
    """보고용 경로 — 홈은 ~ 로 줄인다(sb_vault._short 와 같은 규칙)."""
    h, s = os.path.expanduser('~'), str(p)
    return '~' + s[len(h):] if s == h or s.startswith(h + os.sep) else s


DISK_WARN_GB = 5.0   # 키트가 쌓는 사본·스냅샷·백업 합계 경고선(SB_DOCTOR_DISK_WARN_GB 로 바꿈)


def _tree_bytes(path: Path) -> int:
    """폴더(또는 파일) 크기 합. 심볼릭 링크는 따라가지 않는다."""
    total, stack = 0, [path]
    while stack:
        p = stack.pop()
        try:
            if p.is_symlink():
                continue
            if p.is_file():
                total += p.stat().st_size
                continue
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir():
                            stack.append(Path(e.path))
                        else:
                            total += e.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _claude_mem_backups() -> list:
    """claude-mem 폴더의 DB 백업(이름이나 담긴 폴더 이름에 backup·bak). 운영 DB(claude-mem.db 와 그 -wal·-shm·-journal)만 뺀다
    — claude-mem.db.bak-… 처럼 운영 DB 이름으로 시작하는 백업은 센다."""
    cm = sb_config.claude_mem_dir()
    live = {'claude-mem.db', 'claude-mem.db-wal', 'claude-mem.db-shm', 'claude-mem.db-journal'}
    out = []
    for p in list(cm.glob('*')) + list(cm.glob('backup*/*')) + list(cm.glob('bak*/*')):
        name = p.name.lower()
        if not p.is_file() or p.is_symlink() or '.db' not in name or (p.parent == cm and name in live):
            continue
        if any(t in name or t in p.parent.name.lower() for t in ('backup', 'bak')):
            out.append(p)
    return out


def _gb(n: int) -> str:
    return '%.1fGB' % (n / 1024 ** 3) if n >= 1024 ** 3 else '%dMB' % (n // 1024 ** 2)


def check_disk_usage():
    """키트·claude-mem 이 쌓는 사본·스냅샷·백업 크기(읽기 전용 — 자동으로 지우지 않는다)."""
    try:
        warn = float(os.environ.get('SB_DOCTOR_DISK_WARN_GB') or DISK_WARN_GB)
    except ValueError:
        warn = DISK_WARN_GB
    sb = sb_config.home()
    index = sb / 'index'
    snap = [(index / n, _tree_bytes(index / n)) for n in ('chroma-snapshot', 'chroma-recalld')]
    parts = [(_short(sb / 'local'), _tree_bytes(sb / 'local'))] + [(_short(p), n) for p, n in snap]
    parts.append((_short(index) + ' (그 밖)', max(0, _tree_bytes(index) - sum(n for _, n in snap))))
    backups = _claude_mem_backups()
    if backups:
        parts.append(('%s 백업 DB %d개' % (_short(sb_config.claude_mem_dir()), len(backups)), sum(_tree_bytes(p) for p in backups)))
    total = sum(n for _, n in parts)
    try:
        free = shutil.disk_usage(str(sb if sb.exists() else HOME)).free
    except OSError:
        free = None
    top = sorted((x for x in parts if x[1] > 0), key=lambda x: -x[1])[:3]
    names = ', '.join('%s %s' % (label, _gb(n)) for label, n in top)
    detail = '합계 %s%s%s' % (_gb(total), ', 여유 %s' % _gb(free) if free is not None else '', ' — 큰 것: ' + names if names else '')
    limit = warn * 1024 ** 3
    if total >= limit or (free is not None and free < limit):
        return rec('⚠', '디스크 사용', detail + ' — 정리 안내(자동 삭제 안 함): local 의 실험 사본·오래된 claude-mem 백업 DB 는 확인 뒤 '
                   '지우거나 옮기고, index/chroma-snapshot·chroma-recalld 는 지워도 다음 검색 때 다시 만들어집니다(회수 서버를 끈 뒤)')
    rec('✅', '디스크 사용', detail)


def check_sb():
    found = shutil.which('sb')
    rec('✅' if found else '⚠', 'sb 명령', found or 'PATH 에 없음(새 터미널에서 다시 확인)')
    try:   # 회수 상주 서버 — 세션 시작 훅이 띄운다(꺼져 있어도 다음 세션에서 자동 기동)
        import sb_recalld
        up = sb_recalld.is_up()
        rec('✅' if up else '⚠', '회수 상주 서버(sb_recalld)', sb_recalld.base_url() if up else '꺼짐 — 새 세션을 열거나 `sb recall` 한 번이면 뜸')
    except Exception as exc:  # noqa: BLE001
        rec('⚠', '회수 상주 서버(sb_recalld)', str(exc)[:80])
    if IS_WIN:   # Claude Code 의 Bash 도구(Git Bash)는 sb.cmd 를 `sb` 로 못 부른다
        sh = sb_config.home() / 'bin' / 'sb'
        rec('✅' if sh.is_file() else '❌', 'sb 명령(Git Bash)', str(sh) if sh.is_file() else '없음 — install 재실행')


# 점검 순서 — sb setup 1단계(bin/sb_setup.py)도 이 목록을 그대로 돌린다
CHECKS = (check_venv, check_worker, check_settings, check_embedding, check_claude_mem_version, check_chroma_mcp, check_claude,
          check_codex, check_rules, check_state_and_capture, check_sb, check_chroma_lock, check_disk_usage)


def main() -> int:
    try:   # 파이프·리디렉션 시 cp949 콘솔에서 ✅ 출력이 UnicodeEncodeError 로 죽는다
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    for fn in CHECKS:
        try:
            fn()
        except Exception as exc:
            rec('❌', fn.__name__, '%s: %s' % (type(exc).__name__, exc))
    width = max(len(r[1]) for r in rows)
    for level, name, detail in rows:
        print('%s %s  %s' % (level, name.ljust(width), detail))
    return 1 if any(r[0] == '❌' for r in rows) else 0


if __name__ == '__main__':
    sys.exit(main())
