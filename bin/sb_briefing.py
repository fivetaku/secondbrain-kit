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
import re
import sqlite3
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sb_config  # noqa: E402
import sb_search  # noqa: E402  (_next_items / _mode_envelope 재사용)
import sb_state  # noqa: E402

DEFAULT_CHARS, DEFAULT_BYTES = 2000, 8192
MAX_TRACKS, MAX_STATES = 3, 4
PRIORITY = {'high': 0, 'medium': 1, 'low': 2}
# 2026-10-31 재확인 주기(일). 데이터로 정한 값이 아니라 초기 기본값이다(_drafts/2026-10-30-optimal-methods.md 4절).
REVIEW_DAYS = (('deal.', 21), ('lead.', 21), ('ledger.', 21), ('bug.', 21), ('deadline.', 21), ('refund.', 21),
               ('tooling.', 45), ('kit.', 45))
DEFAULT_REVIEW_DAYS = 90
REVIEW_PER_BRIEFING = 2
LOOP_ID = re.compile(r'\bL[0-9a-f]{7,8}\b')


def _review_days(fact_key: str) -> int:
    return next((d for p, d in REVIEW_DAYS if fact_key.startswith(p)), DEFAULT_REVIEW_DAYS)


def _age_days(ts: Optional[str], today: date) -> Optional[int]:
    try:
        return (today - datetime.fromisoformat(str(ts).replace('Z', '+00:00')).date()).days
    except (TypeError, ValueError):
        return None


def _provenance(items: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """사용자 확인 사실의 값(value)과 근거 발화가 사람 입력인지. 읽기 전용, 실패하면 빈 결과.
    근거 프롬프트가 '<'로 시작하면(agent-message·cross-session·paseo-system) 사람이 친 말이 아니라 전달문이다."""
    out: Dict[int, Dict[str, Any]] = {}
    ids = [it['item_id'] for it in items if it.get('label') == 'user-confirmed' and it.get('item_id')]
    if not ids:
        return out
    try:
        st = sqlite3.connect('file:%s?mode=ro' % sb_state.state_db_path(None), uri=True, timeout=1)
        rows = st.execute('SELECT a.state_id, c.value_json, v.target FROM state_acceptance a '
                          'JOIN state_candidate c ON c.id = a.candidate_id JOIN state_verification v ON v.id = a.verification_id '
                          'WHERE a.state_id IN (%s)' % ','.join('?' * len(ids)), ids).fetchall()
        st.close()
        cm_path = os.path.expanduser(os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db())
        cm = sqlite3.connect('file:%s?mode=ro' % cm_path, uri=True, timeout=1) if os.path.exists(cm_path) else None
        for sid, value_json, target in rows:
            try:
                value = json.loads(value_json) if value_json else None
            except ValueError:
                value = value_json
            relay = False
            if cm is not None and str(target).startswith('prompt:') and str(target)[7:].isdigit():
                r = cm.execute('SELECT prompt_text FROM user_prompts WHERE id=?', (int(str(target)[7:]),)).fetchone()
                head = (r[0] or '').lstrip()[:300] if r else ''
                relay = head.startswith('<') or 'agent-message' in head or 'cross-session-message' in head
            out[sid] = {'value': value if isinstance(value, str) else None, 'relay': relay}
        if cm is not None:
            cm.close()
    except Exception:  # noqa: BLE001 — 표시 보강일 뿐, 실패해도 브리핑은 그대로 낸다
        return {}
    return out


def _closed_loops() -> Dict[str, str]:
    path = os.path.expanduser(os.environ.get('SB_LOOPS_PATH') or sb_config.sb_path('loops', 'loops.jsonl'))
    out: Dict[str, str] = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get('id') and r.get('status') and r['status'] != 'open':
                    out[r['id']] = r['status']
    except OSError:
        pass
    return out


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
    prov = _provenance(state['items'])
    closed = _closed_loops()
    review: List[Any] = []
    for it in state['items']:
        if it['verification_status'] != 'verified':
            cautions.append({'code': 'unverified_state', 'ref': it['fact_key'], 'text': '미검증 상태: %s' % it['fact_key']})
        p = prov.get(it.get('item_id'))
        if p:
            it['user_value'], it['relay'] = p['value'], p['relay']
        age = _age_days(it.get('checked_at'), today)
        it['age_days'] = age
        # 낡은 사실은 지우지 않는다 — 재확인 후보로만 올리고, 사용자에게 아직 맞는지 묻게 한다(2026-10-31)
        gone = [l for l in LOOP_ID.findall(str(it.get('body') or '')) if l in closed]
        if gone:
            review.append((0, 0, {'code': 'state_loop_closed', 'ref': it['fact_key'],
                                  'text': '재확인 후보: %s (연결된 미결 %s 닫힘) — 사용자에게 아직 맞는지 확인' % (it['fact_key'], gone[0])}))
        elif age is not None and age > _review_days(it['fact_key']):
            review.append((1, -age, {'code': 'state_review_due', 'ref': it['fact_key'],
                                     'text': '재확인 후보: %s (%d일 전 확인) — 사용자에게 아직 맞는지 확인' % (it['fact_key'], age)}))
    # 질문이 많으면 무시하게 된다 — 한 번에 두 건만 묻고 나머지는 수만 알린다
    review.sort(key=lambda r: (r[0], r[1]))
    cautions += [r[2] for r in review[:REVIEW_PER_BRIEFING]]
    if len(review) > REVIEW_PER_BRIEFING:
        cautions.append({'code': 'state_review_more', 'ref': 'state',
                         'text': '재확인 후보 %d건 더(한 번에 %d건씩)' % (len(review) - REVIEW_PER_BRIEFING, REVIEW_PER_BRIEFING)})
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
            age = it.get('age_days')
            when = (' · 확인 %d일 전' % age if age is not None else ' · 확인 %s' % it['checked_at'][:10]) if it.get('checked_at') else ''
            body = str(it['body']).replace('\n', ' ')
            if it.get('label') == 'user-confirmed' and it.get('user_value'):
                # '사용자 확인'은 사용자가 말한 값에만 붙인다. 본문은 에이전트가 정리한 맥락이라 따로 표시한다(2026-10-31).
                who = '전달문 근거·사람 발화 미확인' if it.get('relay') else '사용자 확인'
                lines.append('  · %s = 「%s」 (%s%s)%s' % (it['fact_key'], str(it['user_value']).replace('\n', ' ')[:50], who, when, mark))
                lines.append('    에이전트 정리: %s' % body[:60])
            else:
                label = ' · %s' % it['label'] if it.get('label') else ''
                lines.append('  · %s = %s%s%s%s' % (it['fact_key'], body[:80], label, when, mark))
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
