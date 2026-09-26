#!/usr/bin/env python3
"""세컨브레인 재부상 층 — 미결(열린 루프) 상태기계.

사용:
  loops.py list [--all]          열린 루프 목록 (OPEN.md도 갱신)
  loops.py brief [--ids]         오늘의 브리프 생성(읽기 전용, 상위 3건) → stdout
  loops.py record-exposure <id> [<id>...]  발송 성공 후 노출 기록·백오프 적용
  loops.py close <id> [메모]     완료 처리
  loops.py snooze <id> <일수>    N일 뒤로 미루기
  loops.py drop <id> [메모]      폐기(안 할 일로 확정)
  loops.py add "제목" [--project P] [--value high|medium|low] [--due YYYY-MM-DD] [--action "다음 행동"]
  loops.py set-action <id> "다음 행동" [--expected-action "현재값"]
                                 열린 미결의 next_action 갱신 (open/waiting_external 만, 충돌 검출)
  loops.py reopen <id> [메모]    done/dropped/stale → open 으로 되돌림 (자동 close 되돌리기용)
  loops.py stats                 상태 집계

원장 위치: $SB_HOME/loops/loops.jsonl (SB_LOOPS_PATH 로 변경).
백오프 1→2→3→5→8→13일, 6회 노출 시 stale 자동 퇴장, 미종료 상한 40건 경고.
"""
import argparse
import collections
import datetime
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sb_store import atomic_write_text, load_loops, locked_loops, loops_path, new_loop_id, validate_loop

BACKOFF = [1, 2, 3, 5, 8, 13]
OPEN_CAP = 40

def today(): return datetime.date.today()
def iso(d): return d.isoformat()
def open_md(): return os.path.join(os.path.dirname(loops_path()), 'OPEN.md')

def render(items):
    live = [i for i in items if i['status'] in ('open', 'waiting_external', 'snoozed')]
    live.sort(key=lambda i: ({'high':0,'medium':1,'low':2}.get(i.get('value'),3), i.get('date_opened','')))
    L = [f"# 열린 루프 — {iso(today())} 기준 {len(live)}건\n"]
    L.append("| id | 가치 | 상태 | 열린날 | 다음노출 | 내용 |")
    L.append("|---|---|---|---|---|---|")
    for i in live:
        L.append(f"| {i['id']} | {i.get('value','?')} | {i['status']} | {i.get('date_opened','?')} | {i.get('next_review','?')} | {i['title'][:60]} |")
    done = [i for i in items if i['status'] in ('done','dropped','stale')]
    L.append(f"\n종결 {len(done)}건 (done/dropped/stale). 전체 원장: loops.jsonl")
    atomic_write_text(open_md(), '\n'.join(L) + '\n')

def brief(ids: bool = False) -> str:
    items = load_loops()
    t = iso(today())
    # 결정 필요한 것만 문자 대상: 마감 임박(2일 내) 또는 high 가치 (2026-08-19: 매일 순환 노출 → 무시 습관 방지)
    soon = iso(today() + datetime.timedelta(days=2))
    elig = [i for i in items if i['status'] in ('open','waiting_external') and (i.get('next_review') or t) <= t
            and ((i.get('due') and i['due'] <= soon) or i.get('value') == 'high')]
    elig.sort(key=lambda i: (0 if (i.get('due') and i['due'] <= t) else 1,
                             {'high':0,'medium':1,'low':2}.get(i.get('value'),3),
                             i.get('last_exposed') or '1970', i.get('date_opened','')))
    top = elig[:3]
    if ids:
        return ' '.join(i['id'] for i in top)
    if not top:
        return ''  # 보낼 게 없으면 빈 문자열 → 호출자가 알림 생략
    n_open = len([i for i in items if i['status'] in ('open','waiting_external')])
    lines = [f"🧠 좋은 아침입니다. 미결 {n_open}건 중 오늘 볼 것 {len(top)}건입니다.", ""]
    for k, i in enumerate(top, 1):
        age = ""
        try:
            d0 = datetime.date.fromisoformat(i.get('date_opened', t))
            days = (today() - d0).days
            if days >= 3: age = f" ({days}일째 방치)"
        except (ValueError, TypeError):
            age = ""
        due = f" · 마감 {i['due']}" if i.get('due') else ""
        head = i['title'].split(',')[0].split('(')[0].strip()[:45]
        lines.append(f"{k}. {head}{age}{due}")
        body = i['title'][:110]
        lines.append(f"   상황: {body}")
        if i.get('next_action'):
            lines.append(f"   다음 한 수: {i['next_action'][:80]}")
        lines.append(f"   처리: loops.py close {i['id']} / snooze {i['id']} 7 / drop {i['id']}")
        lines.append("")
    if n_open > OPEN_CAP:
        lines.append(f"⚠ 미결 {n_open}건 — 상한 {OPEN_CAP} 초과, 정리 필요")
    return '\n'.join(lines)


def find(items: List[Dict], lid: str) -> Dict:
    matches = [i for i in items if i['id'] == lid]
    if len(matches) > 1:
        raise ValueError(f"id {lid} 중복 ({len(matches)}건)")
    if not matches:
        raise ValueError(f"id {lid} 없음")
    return matches[0]


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = cli.add_subparsers(dest='command', required=True)
    commands.add_parser('list').add_argument('--all', action='store_true')
    commands.add_parser('brief').add_argument('--ids', action='store_true')
    commands.add_parser('stats')
    for command in ('close', 'drop', 'reopen'):
        sub = commands.add_parser(command)
        sub.add_argument('id')
        sub.add_argument('note', nargs='*')
    sub = commands.add_parser('snooze')
    sub.add_argument('id')
    sub.add_argument('days', type=int)
    sub = commands.add_parser('set-action', help='열린 미결의 다음 행동(next_action) 갱신 (open/waiting_external 만)')
    sub.add_argument('id')
    sub.add_argument('action')
    sub.add_argument('--expected-action', default=None,
                     help='현재 next_action 이 이 값과 다르면 충돌로 거부 (낙관적 잠금)')
    commands.add_parser('record-exposure').add_argument('ids', nargs='+')
    sub = commands.add_parser('add')
    sub.add_argument('title')
    for option in ('project', 'value', 'due', 'action'):
        sub.add_argument('--' + option)
    return cli


def mutate(items: List[Dict], args: argparse.Namespace) -> str:
    t = iso(today())
    cmd = args.command
    if cmd == 'add':
        it = {'id': new_loop_id(i['id'] for i in items), 'title': args.title, 'status': 'open',
              'date_opened': t, 'next_review': t, 'expose_count': 0, 'value': 'medium', 'source': 'manual'}
        for option, field in [('project', 'project'), ('value', 'value'), ('due', 'due'),
                              ('action', 'next_action')]:
            if getattr(args, option) is not None:
                it[field] = getattr(args, option)
        validate_loop(it)
        items.append(it)
        return f"추가: {it['id']}"
    if cmd == 'record-exposure':
        selected = [find(items, lid) for lid in dict.fromkeys(args.ids)]
        for i in selected:
            i['expose_count'] = i.get('expose_count', 0) + 1
            i['last_exposed'] = t
            if i['expose_count'] >= 6:
                i['status'] = 'stale'
                i['stale_at'] = t
            else:
                i['next_review'] = iso(today() + datetime.timedelta(days=BACKOFF[i['expose_count'] - 1]))
        return "노출 기록: " + ' '.join(i['id'] for i in selected)
    i = find(items, args.id)
    if cmd == 'set-action':
        # 2026-09-16 memory-layer-refactor M2: 다음 행동은 미결(트랙) 단위로 귀속·갱신한다.
        # 다른 미결을 건드리지 않고, 완료 처리(close)와도 분리된 갱신 경로다.
        if i['status'] not in ('open', 'waiting_external'):
            raise ValueError(f"거부: {i['id']} 는 {i['status']} 상태 — 열린 미결(open/waiting_external)만 다음 행동을 갱신할 수 있음")
        action = (args.action or '').strip()
        if not action:
            raise ValueError('다음 행동은 비어 있을 수 없음')
        if args.expected_action is not None and i.get('next_action', '') != args.expected_action:
            raise ValueError(f"충돌: {i['id']} 의 현재 next_action 이 --expected-action 과 다름 (다시 읽고 갱신하세요). "
                             f"현재: {i.get('next_action', '')[:80]!r}")
        i['next_action'] = action
        i['action_updated_at'] = t
        validate_loop(i)
        return f"다음 행동 갱신: {i['id']} → {action[:60]}"
    if cmd in ('close', 'drop'):
        i['status'] = 'done' if cmd == 'close' else 'dropped'
        i['closed_at'] = t
        if args.note:
            i['close_note'] = ' '.join(args.note)
        return f"{'닫음' if cmd == 'close' else '폐기'}: {i['title'][:60]}"
    if cmd == 'snooze':
        if not 1 <= args.days <= 365:
            raise ValueError('일수는 1~365 정수여야 함')
        i['status'] = 'open'
        i['next_review'] = iso(today() + datetime.timedelta(days=args.days))
        return f"{args.days}일 뒤 재노출: {i['title'][:60]}"
    if cmd == 'reopen':
        prev = i['status']
        i.update(status='open', next_review=t, reopened_at=t, reopened_from=prev,
                 expose_count=0, reopen_pending=True)
        if 'linear_closed' in i:  # 외부 보드 동기화가 남긴 수동 필드 — 있으면 되돌리기만 한다(API 호출 없음)
            i['linear_closed'] = False
        i.pop('stale_at', None)
        if args.note:
            i['reopen_note'] = ' '.join(args.note)
        return f"재개({prev}→open): {i['title'][:60]}"
    raise ValueError(f"알 수 없는 변경 명령: {cmd}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows 파이프(cp949 등)에서도 한글·이모지 출력
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    cli = parser()
    args = cli.parse_args()
    try:
        if args.command == 'brief':
            print(brief(args.ids))
        elif args.command == 'list':
            items = load_loops()
            render(items)
            live = [i for i in items if i['status'] in ('open', 'waiting_external', 'snoozed') or args.all]
            for i in live:
                print(f"{i['id']} [{i.get('value','?')}/{i['status']}] {i['title'][:70]}")
            print(f"-- {len(live)}건. 상세: {open_md()}")
        elif args.command == 'stats':
            items = load_loops()
            print(dict(collections.Counter(i['status'] for i in items)), f"/ 총 {len(items)}")
        else:
            with locked_loops() as items:
                message = mutate(items, args)
            render(items)
            print(message)
    except (ValueError, OSError) as exc:
        cli.error(str(exc))


if __name__ == '__main__':
    main()
