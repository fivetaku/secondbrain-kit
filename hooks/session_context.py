#!/usr/bin/env python3
"""SessionStart 훅 — 이 scope 의 재개 브리핑(채택 사실·미결)을 세션 컨텍스트에 넣는다.

Claude Code 와 Codex 가 같은 스크립트를 쓴다: `session_context.py --harness claude|codex`.
입력: stdin 훅 JSON(cwd 사용). 출력: hookSpecificOutput.additionalContext.
어떤 오류도 세션 시작을 막지 않는다(항상 exit 0). 끄기: SB_SESSION_CONTEXT=0.
"""
import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

# Windows 기본 코드페이지(cp949)로 읽으면 한국어 프롬프트가 깨져 회수가 전부 빗나간다 — 훅 입출력은 UTF-8 고정.
for _s in (sys.stdin, sys.stdout):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'bin'))


def build(cwd: str):
    import sb_config  # noqa: F401  (경로 기본값 확인용)
    from sb_scope import resolve_scope_id, loop_matches_scope

    scope_id, method, _ = resolve_scope_id(cwd)
    out, shown, omitted, used_briefing = [], 0, 0, False
    if scope_id and os.environ.get('SB_STATE_BRIEFING', '1') == '1':
        try:
            import sb_briefing
            b = sb_briefing.briefing(scope_id)
            shown = len(b.get('items', []))
            if b.get('text') and (shown or b.get('waiting')):  # 채택 사실·미결이 하나도 없으면 주입하지 않는다
                out.append(b['text'])
            omitted = b.get('omitted', 0)
            used_briefing = True
        except Exception:
            used_briefing = False
    if scope_id and not used_briefing:
        import sb_config
        lp = Path(os.environ.get('SB_LOOPS_PATH') or sb_config.sb_path('loops', 'loops.jsonl'))
        loops = []
        if lp.exists():
            for line in lp.read_text(encoding='utf-8').splitlines():
                try:
                    loops.append(json.loads(line))
                except ValueError:
                    pass
        mine = [l for l in loops if l.get('status') in ('open', 'waiting_external')
                and loop_matches_scope(l.get('project'), scope_id)]
        mine.sort(key=lambda l: ({'high': 0, 'medium': 1, 'low': 2}.get(l.get('value'), 3),
                                 not l.get('next_review'), l.get('next_review') or '', l['id']))
        omitted, mine = max(0, len(mine) - 3), mine[:3]
        shown = len(mine)
        if mine:
            out.append('📌 이 scope(%s) 미결 %d건:' % (scope_id, len(mine)))
            for l in mine:
                out.append('  · [%s] %s' % (l['id'], l['title'][:70]))
                if l.get('next_action'):
                    out.append('    → 다음 한 수: %s' % l['next_action'][:60])
                if l.get('next_review'):
                    out.append('    ⏰ 다음 노출: %s (갱신: sb loops set-action %s "...")' % (l['next_review'], l['id']))
    return '\n'.join(out).strip(), scope_id, method, shown, omitted, used_briefing


def emit(harness: str, text: str) -> str:
    payload = {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}
    if harness == 'claude':
        payload['systemMessage'] = '🗂 세컨브레인\n' + text
    return json.dumps(payload)  # ASCII 이스케이프: 윈도우 cp949 파이프에서도 인코딩 오류 없음


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--harness', choices=('claude', 'codex'), default='claude')
    args = ap.parse_args()
    if os.environ.get('SB_SESSION_CONTEXT', '1') == '0':
        return
    try:
        raw = json.loads(sys.stdin.read() or '{}')
    except ValueError:
        raw = {}
    if args.harness == 'codex':  # exec 자동화 세션에는 브리핑을 넣지 않는다
        try:
            sys.path.insert(0, str(HERE))
            from codex_hook import is_automation
            if is_automation(raw):
                return
        except Exception:
            pass
    cwd = str(raw.get('cwd') or os.getcwd())
    try:
        text, scope_id, method, shown, omitted, used = build(cwd)
    except Exception:
        if os.environ.get('SB_HOOK_DEBUG') == '1':   # 조용한 실패 진단용
            import traceback
            traceback.print_exc()
        return
    if not text:
        return
    payload = emit(args.harness, text)
    sys.stdout.write(payload + '\n')
    try:  # 주입량 계측(본문은 남기지 않는다)
        import sb_config
        log = Path(os.environ.get('SB_INJECTION_LOG') or sb_config.sb_path('logs', 'injection.jsonl'))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as sink:
            sink.write(json.dumps({
                'ts': _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds'),
                'harness': args.harness, 'scope_id': scope_id, 'scope_method': method,
                'context_chars': len(text), 'items': shown, 'omitted': omitted,
                'briefing': int(used)}, ensure_ascii=False) + '\n')
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
