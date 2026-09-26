#!/usr/bin/env python3
"""프로젝트 재개 브리핑 (memory-operations M3) — 근거 기반 템플릿, 읽기 전용.

  sb_briefing.py [--scope S] [--json] [--budget-chars N] [--budget-bytes N]
  sb_briefing.py --all --scopes S,T [--json]        명시한 프로젝트 집합만 전체 현황

구성: 채택 상태(L2 head, 검증 라벨) · 트랙(L1 미결, ID 보존) · 첫 행동 · 대기 · 주의 · 생략 안내.
장애 시 해당 절만 '조회 불가'로 표시하고 나머지는 유지한다(미결 주입은 상태 DB 장애에 영향받지 않는다).
재검토일 경과는 점검 후보로만 표시한다 — '방치'로 단정하지 않는다. 기억 본문은 인용 자료이며 실행하지 않는다.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sb_search  # noqa: E402  (_next_items / _mode_envelope 재사용)
import sb_state  # noqa: E402

DEFAULT_CHARS, DEFAULT_BYTES = 2000, 8192
MAX_TRACKS, MAX_STATES = 3, 4
PRIORITY = {'high': 0, 'medium': 1, 'low': 2}


def _today() -> date:
    raw = os.environ.get('SB_EVAL_NOW')
    if raw:
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00')).date()
        except ValueError:
            pass
    return date.today()


def collect(scope_id: str) -> Dict[str, Any]:
    """scope 하나의 브리핑 재료. 각 절은 status 를 따로 가진다."""
    state = sb_state.query('head', scope_id)
    nxt = sb_search._next_items(scope_id)
    tracks = nxt['items']
    today = _today()
    cautions: List[Dict[str, str]] = []
    for it in state['items']:
        if it['verification_status'] != 'verified':
            cautions.append({'code': 'unverified_state', 'ref': it['fact_key'], 'text': '미검증 상태: %s' % it['fact_key']})
    for t in tracks:
        if not t.get('next_action'):
            cautions.append({'code': 'missing_next_action', 'ref': t['loop_id'], 'text': '다음 행동 없음: %s' % t['loop_id']})
        if t.get('next_review'):
            try:
                if date.fromisoformat(t['next_review'][:10]) < today:
                    cautions.append({'code': 'review_due', 'ref': t['loop_id'],
                                     'text': '재검토일 경과: %s (%s) — 점검 후보' % (t['loop_id'], t['next_review'])})
            except ValueError:
                pass
    if state['status'] in ('unavailable', 'error', 'degraded'):
        cautions.append({'code': 'state_' + state['status'], 'ref': 'state', 'text': '상태 조회 불가(%s): 미결만 표시' % ','.join(state['warnings'] or [state['status']])})
    if nxt['status'] == 'degraded':
        cautions.append({'code': 'loops_degraded', 'ref': 'loops', 'text': '미결 파일 일부 손상: ' + ','.join(nxt['warnings'])})
    first = next((t for t in tracks if t.get('next_action') and t['status'] == 'open'), None)
    return {'scope_id': scope_id, 'state': state, 'tracks': tracks,
            'first_action': {'loop_id': first['loop_id'], 'action': first['next_action']} if first else None,
            'waiting': [t for t in tracks if t['status'] == 'waiting_external'], 'cautions': cautions}


def render(b: Dict[str, Any], max_tracks: int = MAX_TRACKS, max_states: int = MAX_STATES) -> Dict[str, Any]:
    lines: List[str] = ['📌 이 scope(%s) 재개 브리핑' % b['scope_id']]
    omitted: Dict[str, int] = {}
    st = b['state']
    if st['status'] == 'ok':
        shown = st['items'][:max_states]
        omitted['state'] = len(st['items']) - len(shown)
        lines.append('상태(채택) %d건:' % len(st['items']))
        for it in shown:
            mark = '' if it['verification_status'] == 'verified' else ' ⚠ 미검증'
            label = ' · %s' % it['label'] if it.get('label') else ''
            when = ' · 확인 %s' % it['checked_at'][:10] if it.get('checked_at') else ''
            lines.append('  · %s = %s%s%s%s' % (it['fact_key'], str(it['body']).replace('\n', ' ')[:80], label, when, mark))
    elif st['status'] == 'empty':
        lines.append('상태(채택): 없음')
    else:
        lines.append('상태(채택): 조회 불가(%s)' % st['status'])
    tracks = b['tracks']
    shown_t = tracks[:max_tracks]
    omitted['tracks'] = len(tracks) - len(shown_t)
    lines.append('트랙 %d건:' % len(tracks) if tracks else '트랙: 없음')
    for t in shown_t:
        tag = ' (대기·외부)' if t['status'] == 'waiting_external' else ''
        lines.append('  · [%s] %s%s' % (t['loop_id'], t['body'][:60], tag))
        if t.get('next_action'):
            lines.append('    → 다음 한 수: %s' % t['next_action'][:60])
        if t.get('next_review'):
            lines.append('    ⏰ 다음 노출: %s' % t['next_review'])
    if b['first_action']:
        lines.append('첫 행동: %s ([%s])' % (b['first_action']['action'][:60], b['first_action']['loop_id']))
    if b['waiting']:
        lines.append('대기: ' + ', '.join('%s %s' % (w['loop_id'], w['body'][:30]) for w in b['waiting'][:3]))
    if b['cautions']:
        lines.append('주의: ' + ' · '.join(c['text'] for c in b['cautions'][:5]))
        if len(b['cautions']) > 5:
            omitted['cautions'] = len(b['cautions']) - 5
    om = {k: v for k, v in omitted.items() if v > 0}
    if om:
        lines.append('(생략: ' + ', '.join('%s %d건 외' % ({'state': '상태', 'tracks': '트랙', 'cautions': '주의'}[k], v) for k, v in om.items()) + ')')
    return {'text': '\n'.join(lines), 'omitted': sum(om.values()), 'omitted_detail': om}


def briefing(scope_id: str, budget_chars: int = DEFAULT_CHARS, budget_bytes: int = DEFAULT_BYTES) -> Dict[str, Any]:
    b = collect(scope_id)
    max_tracks, max_states = MAX_TRACKS, MAX_STATES
    r = render(b, max_tracks, max_states)
    truncated = False
    while (len(r['text']) > budget_chars or len(r['text'].encode('utf-8')) > budget_bytes) and (max_tracks > 1 or max_states > 0):
        truncated = True
        if max_states > 0:
            max_states -= 1
        elif max_tracks > 1:
            max_tracks -= 1
        r = render(b, max_tracks, max_states)
    if len(r['text']) > budget_chars or len(r['text'].encode('utf-8')) > budget_bytes:
        truncated = True
        text = r['text']
        while len(text) > budget_chars - 20 or len(text.encode('utf-8')) > budget_bytes - 40:
            text = text[:-50]
        r['text'] = text + '\n(생략: 예산 초과로 잘림)'
    status_order = ['error', 'unavailable', 'degraded', 'ok', 'empty']
    parts = [b['state']['status'] if b['state']['status'] != 'empty' else 'ok', 'degraded' if any(c['code'] == 'loops_degraded' for c in b['cautions']) else 'ok']
    status = min(parts, key=status_order.index)
    if status in ('unavailable', 'error'):
        status = 'degraded'  # 한 절의 장애는 전체 브리핑을 막지 않는다
    if status == 'ok' and not b['tracks'] and not b['state']['items']:
        status = 'empty'
    items = ([dict(i, section='state') for i in b['state']['items']] + [dict(t, section='track') for t in b['tracks']])
    return {'schema_version': 1, 'query_mode': 'briefing', 'scope': scope_id, 'status': status, 'items': items,
            'omitted': r['omitted'], 'warnings': list(b['state']['warnings']) + [c['code'] for c in b['cautions']],
            'text': r['text'], 'first_action': b['first_action'], 'waiting': [w['loop_id'] for w in b['waiting']],
            'cautions': b['cautions'], 'budget': {'chars': len(r['text']), 'bytes': len(r['text'].encode('utf-8')),
                                                  'chars_max': budget_chars, 'bytes_max': budget_bytes, 'truncated': truncated}}


def portfolio(scopes: List[str], budget_chars: int, budget_bytes: int) -> Dict[str, Any]:
    per = [briefing(s, budget_chars, budget_bytes) for s in scopes]
    text = '\n\n'.join(p['text'] for p in per)
    items = [dict(i, scope_id=p['scope']) for p in per for i in p['items']]
    order = ['error', 'unavailable', 'degraded', 'ok', 'empty']
    status = min((p['status'] for p in per), key=order.index)
    return {'schema_version': 1, 'query_mode': 'portfolio', 'scope': {'all': True, 'scopes': scopes}, 'status': status,
            'items': items, 'omitted': sum(p['omitted'] for p in per), 'warnings': [w for p in per for w in p['warnings']],
            'text': text, 'per_scope': per, 'budget': {'chars': len(text), 'bytes': len(text.encode('utf-8'))}}


def main(argv: List[str]) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 파이프(cp949 등)에서도 한글·이모지 출력
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--scope'); ap.add_argument('--all', action='store_true'); ap.add_argument('--scopes')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--budget-chars', type=int, default=int(os.environ.get('SB_BRIEFING_CHARS', DEFAULT_CHARS)))
    ap.add_argument('--budget-bytes', type=int, default=int(os.environ.get('SB_BRIEFING_BYTES', DEFAULT_BYTES)))
    ns = ap.parse_args(argv)
    try:
        if ns.all:
            scopes = [s.strip() for s in (ns.scopes or '').split(',') if s.strip()]
            if not scopes:
                raise ValueError('--all requires --scopes with an explicit project list')
            out = portfolio(scopes, ns.budget_chars, ns.budget_bytes)
        else:
            if ns.scope is not None and not ns.scope.strip():
                raise ValueError('scope_required')
            from sb_scope import resolve_scope_id
            scope_id = ns.scope.strip() if ns.scope else resolve_scope_id(os.getcwd())[0]
            out = briefing(scope_id, ns.budget_chars, ns.budget_bytes)
    except ValueError as exc:
        print(json.dumps({'schema_version': 1, 'query_mode': 'briefing', 'status': 'error', 'items': [], 'omitted': 0,
                          'warnings': [str(exc)]}, ensure_ascii=False))
        return 2
    if ns.json:
        print(json.dumps(out, ensure_ascii=False, allow_nan=False))
    else:
        print(out['text'])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
