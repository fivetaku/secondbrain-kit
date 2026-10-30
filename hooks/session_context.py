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


CORRECTION_DAYS = 7
CORRECTION_MAX = 3


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def correction_lines(cwd: str) -> list:
    """세션 중 알림을 못 받고 끝난 교정 후보(hooks/sb_correction.py 대기열)를 다음 세션 시작에 한 번 보여 준다(2026-10-21).
    판정은 백그라운드라 세션이 먼저 끝나면 같은 세션 다음 프롬프트 알림(sb_recall)을 못 받는다.
    같은 폴더·최근 7일·최대 3건, 세션 중 이미 알린 것(*.corr.json notified)과 이미 브리핑한 것은 뺀다."""
    import glob
    import time
    import sb_config
    queue = os.environ.get('SB_CORRECTION_QUEUE') or sb_config.sb_path('index', 'correction_candidates.jsonl')
    if not os.path.exists(queue):
        return []
    state_dir = os.environ.get('SB_RECALL_STATE_DIR') or sb_config.sb_path('index', 'sb-recall-state')
    briefed_path = os.path.join(state_dir, 'corrections.briefed.json')
    seen = set()
    for fp in glob.glob(os.path.join(state_dir, '*.corr.json')):
        try:
            with open(fp, encoding='utf-8') as fh:
                seen |= set(json.load(fh).get('notified', []))
        except Exception:
            pass
    try:
        with open(briefed_path, encoding='utf-8') as fh:
            briefed = set(json.load(fh))
    except Exception:
        briefed = set()
    cut = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - CORRECTION_DAYS * 86400))  # 대기열 ts 와 같은 지역 시각
    with open(queue, encoding='utf-8', errors='replace') as fh:
        tail = fh.read().splitlines()[-500:]
    cands = []
    for line in tail:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or not r.get('cwd') or not r.get('rule'):   # 빈 cwd 는 realpath('')=현재 폴더라 오매칭된다
            continue
        key = r.get('ts', '') + '|' + r['rule'][:40]   # sb_recall.pending_corrections 와 같은 키
        if r.get('ts', '') >= cut and key not in seen and key not in briefed and _same_dir(r['cwd'], cwd):
            cands.append((key, r['rule']))
    if not cands:
        return []
    shown = cands[-CORRECTION_MAX:]
    out = ['📝 지난 세션에서 규칙으로 저장되지 않은 사용자 교정 %d건 — 맞으면 지금 규칙·메모리에 저장하고 아니면 무시:' % len(cands)]
    out += ['  · %s' % rule[:120] for _, rule in shown]
    try:
        import sb_lock
        os.makedirs(state_dir, exist_ok=True)
        # 같은 폴더 세션 둘이 동시에 시작하면 읽기·합치기·교체가 엇갈려 한쪽 키가 사라진다 — *.corr.json 과 같은 방식(<파일>.lock)으로 잠근다.
        # 훅이라 오래 기다리지 않는다: 못 잡으면(TimeoutError 도 OSError) 이번 기록만 건너뛴다 — 다음 세션에 같은 후보가 한 번 더 뜰 뿐.
        with open(briefed_path + '.lock', 'a') as lk:   # 닫으면 잠금도 풀린다
            sb_lock.lock_with_timeout(lk, sb_lock.LOCK_EX, 0.5)
            try:   # 잠근 뒤 다시 읽어 합친다 — 앞서 읽은 뒤 다른 세션이 써 넣은 키를 덮지 않게
                with open(briefed_path, encoding='utf-8') as fh:
                    briefed = set(json.load(fh))
            except Exception:
                pass
            # sb_recall 도 이 파일을 읽어 같은 세션 다음 프롬프트의 [교정 감지]에서 뺀다(resume·compact 이중 표시 방지) —
            # 쓰다 만 파일을 읽지 않게 임시 파일에 쓰고 통째로 바꾼다(교체 실패로 남은 .tmp 는 sb_recall 의 오래된 상태 파일 정리가 지운다).
            tmp = briefed_path + '.%d.tmp' % os.getpid()
            with open(tmp, 'w', encoding='utf-8') as fh:
                # 7일 창을 지난 키는 다시 뜰 일이 없으니 버린다(파일이 계속 커지지 않게)
                json.dump(sorted({k for k in briefed if k[:19] >= cut} | {k for k, _ in shown}), fh)
            try:
                os.replace(tmp, briefed_path)
            except PermissionError:   # 윈도우: sb_recall 이 마침 읽는 중이면 교체가 거부된다 — 잠깐 뒤 한 번만 더
                time.sleep(0.05)
                os.replace(tmp, briefed_path)
    except OSError:
        pass
    return out


def health_line() -> str:
    """기록층 건강 점검 — 문제가 있으면 경고 한 줄, 고칠 수 있으면 백그라운드 자동 복구(sb_health --fix)."""
    if os.environ.get('SB_HEALTH', '1') == '0':
        return ''
    import sb_health
    import sb_config
    # 훅은 읽기만 한다(health.json 은 --fix·CLI·야간 배치가 쓴다). 워커는 claude-mem 자체 훅이 지금 띄우는 중이라 빼고 본다.
    # 회수 자가 시험도 세션 시작엔 서버가 막 뜨는 중이라 빼고, 직전 점검(야간 배치·CLI)의 결과가 실패면 그걸 알린다.
    result = sb_health.run_checks(worker=False, recall=False)
    last = sb_health.load_last().get('recall') or {}
    if last.get('ok') is False and last.get('msg'):
        result['recall'] = {'ok': False, 'msg': last['msg'] + ' (직전 점검)', 'fix': last.get('fix')}
    line = sb_health.warning_line(result)
    fixable = any(isinstance(v, dict) and v.get('fix') for v in result.values())
    installed = Path(sb_config.sb_path('logs', 'nightly.log')).exists()   # 임시·테스트 SB_HOME 에서는 복구를 띄우지 않는다
    if line and fixable and installed and os.environ.get('SB_HEALTH_AUTOFIX', '1') != '0':
        sb_health.spawn_fix()
        line += ' (자동 복구 시작)'
    return line


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
    try:   # 회수 상주 서버를 미리 띄운다 — 첫 프롬프트부터 형태소 회수가 바로 되게(떠 있으면 확인만)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'bin'))
        import sb_recalld
        sb_recalld.ensure_running()
    except Exception:  # noqa: BLE001
        pass
    try:
        text, scope_id, method, shown, omitted, used = build(cwd)
    except Exception:
        if os.environ.get('SB_HOOK_DEBUG') == '1':   # 조용한 실패 진단용
            import traceback
            traceback.print_exc()
        return
    try:
        corr = correction_lines(cwd)
    except Exception:  # noqa: BLE001 — 교정 후보 표시 실패가 세션 시작을 막으면 안 된다
        corr = []
    if corr:
        text = (text + '\n\n' + '\n'.join(corr)).strip()
    try:
        warn = health_line()
    except Exception:  # noqa: BLE001 — 점검 실패가 세션 시작을 막으면 안 된다
        warn = ''
    if warn:
        text = (text + '\n' + warn).strip()
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
