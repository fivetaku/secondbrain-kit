#!/usr/bin/env python3
"""Codex → claude-mem 캡처 중계 훅 (자동화 세션 제외).

claude-mem Codex 플러그인의 훅은 명령을 바꿀 수 없어 `codex exec` 자동화 세션까지 전부 캡처한다
(실측: 14일간 Codex 세션 623개 중 515개가 exec). 그래서 플러그인 훅은 끄고, 이 스크립트가
같은 claude-mem 명령(`hook codex <event>`)을 대신 부르되 자동화 세션은 건너뛴다.

  codex_hook.py context|session-init|file-context|observation|summarize

자동화 판정(하나라도 참이면 건너뜀):
  1) 환경변수 CLAUDE_MEM_INTERNAL=1 또는 SB_CODEX_SKIP=1
  2) transcript_path 가 없음(ephemeral/원격 세션)
  3) transcript 첫 줄 session_meta.payload.originator == "codex_exec"
SB_CODEX_CAPTURE_EXEC=1 이면 exec 도 캡처한다. 판정은 session_id 별로 캐시한다.
항상 exit 0 (중계 대상의 실패도 세션을 막지 않는다).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

EVENTS = ('context', 'session-init', 'file-context', 'observation', 'summarize')
CACHE = Path(tempfile.gettempdir()) / 'sb-codex-hook'


def is_automation(payload: dict) -> bool:
    if os.environ.get('SB_CODEX_CAPTURE_EXEC') == '1':
        return False
    if os.environ.get('CLAUDE_MEM_INTERNAL') == '1' or os.environ.get('SB_CODEX_SKIP') == '1':
        return True
    sid = re.sub(r'[^\w-]', '_', str(payload.get('session_id') or ''))
    cached = CACHE / (sid + '.json') if sid else None
    if cached and cached.exists():
        try:
            return bool(json.loads(cached.read_text(encoding='utf-8'))['automation'])
        except (OSError, ValueError, KeyError):
            pass
    tp = payload.get('transcript_path')
    automation = True
    if tp:
        try:
            with open(tp, encoding='utf-8') as fh:
                first = json.loads(fh.readline() or '{}')
            originator = str((first.get('payload') or {}).get('originator') or '')
            automation = originator == 'codex_exec'
        except (OSError, ValueError):
            automation = False  # 파일을 못 읽으면 대화형으로 간주(캡처 누락보다 과캡처가 낫다)
    if cached:
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps({'automation': automation}), encoding='utf-8')
        except OSError:
            pass
    return automation


def _ver(p: Path):
    parts = re.findall(r'\d+', p.name)
    return tuple(int(x) for x in parts[:3]) if parts else (0,)


def plugin_root():
    """claude-mem 스크립트 위치: Codex 플러그인 캐시 → Claude 플러그인 캐시 → 마켓 클론."""
    home = Path.home()
    cands = []
    for base in (home / '.codex/plugins/cache/claude-mem-local/claude-mem',
                 home / '.claude/plugins/cache/thedotmack/claude-mem'):
        if base.is_dir():
            cands += sorted((d for d in base.iterdir() if d.is_dir() and d.name[:1].isdigit()
                             and not (d / '.orphaned_at').exists()), key=_ver, reverse=True)
    cands += [home / '.claude/plugins/marketplaces/thedotmack/plugin']
    for c in cands:
        root = c / 'plugin' if (c / 'plugin' / 'scripts').is_dir() else c
        if (root / 'scripts' / 'bun-runner.js').exists() and (root / 'scripts' / 'worker-service.cjs').exists():
            return root
    return None


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in EVENTS:
        return
    event = sys.argv[1]
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode('utf-8') or '{}')
    except ValueError:
        payload = {}
    if is_automation(payload):
        if event == 'context':  # Codex 는 SessionStart 에 빈 additionalContext 를 기대한다
            sys.stdout.write(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart',
                                                                'additionalContext': ''}}))
        return
    root = plugin_root()
    if not root:
        return
    env = dict(os.environ, CLAUDE_MEM_CODEX_HOOK='1')
    try:
        res = subprocess.run(['node', str(root / 'scripts' / 'bun-runner.js'),
                              str(root / 'scripts' / 'worker-service.cjs'), 'hook', 'codex', event],
                             input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                             timeout=110)
        sys.stdout.buffer.write(res.stdout)
    except (OSError, subprocess.SubprocessError):
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
