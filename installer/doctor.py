#!/usr/bin/env python3
"""secondbrain-kit 상태 점검 — 설치가 실제로 동작하는지 한 번에 확인한다(읽기 전용).

  python installer/install.py --doctor     (또는 python installer/doctor.py)
✅ 정상 / ⚠ 동작은 하나 권장과 다름 / ❌ 고쳐야 함. ❌ 가 하나라도 있으면 exit 1.
"""
import json
import os
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
import sb_config  # noqa: E402

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
UTF8 = dict(capture_output=True, text=True, encoding='utf-8', errors='replace',
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})


def check_venv():
    if not VPY.exists():
        return rec('❌', 'venv', '%s 없음 — install.py 재실행' % VPY)
    r = subprocess.run([str(VPY), '-c', 'import chromadb, kiwipiepy, openai; print(chromadb.__version__)'], **UTF8)
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
    if name == 'default':
        rec('⚠', '벡터 임베딩', 'default (영어 전용 — 한국어 검색 약함)')
    elif 'bge-m3' not in text:
        rec('⚠', '벡터 임베딩', name + ' 이지만 bge-m3 가 아님 — 키트 설정과 다른 기존 컬렉션(docs/embedding.md)')
    else:
        rec('✅', '벡터 임베딩', name + ' / bge-m3')
    if name in ('openai', 'ollama'):
        try:
            tags = json.loads(_get('http://127.0.0.1:11434/api/tags'))
            ok = any(m.get('name', '').startswith('bge-m3') for m in tags.get('models', []))
            rec('✅' if ok else '❌', 'Ollama bge-m3', '있음' if ok else 'ollama pull bge-m3 필요')
        except Exception:
            rec('❌', 'Ollama', '127.0.0.1:11434 응답 없음 — Ollama 앱/서비스를 켜세요')
        if name == 'openai':
            env = HOME / '.chroma_env'
            ok = env.exists() and 'CHROMA_OPENAI_API_KEY' in env.read_text(encoding='utf-8')
            rec('✅' if ok else '❌', '~/.chroma_env', '있음' if ok else 'CHROMA_OPENAI_API_KEY=ollama 줄 필요')


def _has_kit_hook(path: Path, event: str) -> bool:
    try:
        hooks = json.loads(path.read_text(encoding='utf-8')).get('hooks', {})
    except (OSError, ValueError):
        return False
    kit = str(KIT).replace('\\', '/')
    return any(kit in str(h.get('command', '')).replace('\\', '/') for g in hooks.get(event, []) for h in g.get('hooks', []))


def check_recall_smoke():
    """등록만 보지 않고 실제로 돌려 본다 — 한국어 프롬프트가 깨지면 회수가 조용히 전부 빗나간다(윈도우 cp949)."""
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONIOENCODING', 'PYTHONUTF8')}
    env['SB_RECALL_GATE_DIR'] = tempfile.mkdtemp(prefix='sb-doctor-')
    payload = json.dumps({'prompt': '지난주에 한 작업 어떻게 됐지', 'session_id': 'sb-doctor-%d' % os.getpid(),
                          'cwd': str(HOME)}, ensure_ascii=False).encode('utf-8')
    try:
        r = subprocess.run([str(VPY if VPY.exists() else sys.executable), str(KIT / 'hooks' / 'sb_recall.py')],
                           input=payload, capture_output=True, timeout=30, env=env)
        ctx = json.loads(r.stdout.decode('utf-8') or '{}').get('hookSpecificOutput', {}).get('additionalContext', '')
    except Exception as e:  # noqa: BLE001
        return rec('❌', '회수 훅 실동작', '실행 실패: %s' % e)
    ok = 'sb timeline' in ctx
    rec('✅' if ok else '❌', '회수 훅 실동작', '한국어 프롬프트 → 회수 주입 정상' if ok else '한국어 프롬프트에 주입 없음(인코딩 확인)')


def check_claude():
    if not shutil.which('claude'):
        return rec('⚠', 'Claude Code', '미설치 — 건너뜀')
    p = HOME / '.claude' / 'settings.json'
    events = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SubagentStart')
    missing = [e for e in events if not _has_kit_hook(p, e)]
    rec('✅' if not missing else '❌', 'Claude 훅', '%d종 등록' % len(events) if not missing else '누락: ' + ', '.join(missing))
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
        '%d/%d개 미신뢰 — python installer/codex_trust.py trust --match "%s"' % (len(bad), len(ours), kit))
    rec('✅' if not plugin_on else '⚠', 'Codex claude-mem 플러그인 훅',
        '꺼짐(우리 훅이 대신 캡처)' if not plugin_on else '켜져 있음 — exec 자동화까지 캡처됨')


def check_rules():
    for p in (HOME / '.claude' / 'CLAUDE.md', HOME / '.codex' / 'AGENTS.md'):
        if not p.parent.exists():
            continue
        ok = p.exists() and 'secondbrain-kit:begin' in p.read_text(encoding='utf-8')
        rec('✅' if ok else '❌', '규칙 블록 ' + p.parent.name + '/' + p.name, '있음' if ok else '없음')


def check_state_and_capture():
    r = subprocess.run([str(VPY if VPY.exists() else sys.executable), str(KIT / 'bin' / 'sb_search.py'),
                        '--mode', 'current', '--scope', 'global'], **UTF8)
    rec('✅' if r.returncode in (0,) else '❌', '상태층(state.db)', (r.stdout or r.stderr).strip().splitlines()[0][:100]
        if (r.stdout or r.stderr).strip() else 'rc=%d' % r.returncode)
    db = Path(sb_config.claude_mem_db())
    if not db.exists():
        return rec('⚠', '캡처', 'claude-mem.db 없음(첫 세션 뒤 생성)')
    con = sqlite3.connect('file:%s?mode=ro' % db.as_posix(), uri=True)
    for src in ('claude', 'codex'):
        row = con.execute('SELECT MAX(started_at_epoch), COUNT(*) FROM sdk_sessions WHERE platform_source=?', (src,)).fetchone()
        if row and row[1]:
            age_h = (time.time() * 1000 - (row[0] or 0)) / 3.6e6
            rec('✅' if age_h < 72 else '⚠', '캡처 %s' % src, '세션 %d개, 최근 %.0f시간 전' % (row[1], age_h))
        else:
            rec('⚠', '캡처 %s' % src, '아직 없음 — 세션을 한 번 열고 도구를 써 보세요')
    con.close()


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


def main() -> int:
    try:   # 파이프·리디렉션 시 cp949 콘솔에서 ✅ 출력이 UnicodeEncodeError 로 죽는다
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    for fn in (check_venv, check_worker, check_settings, check_embedding, check_claude, check_codex,
               check_rules, check_state_and_capture, check_sb):
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
