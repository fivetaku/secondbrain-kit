#!/usr/bin/env python3
"""상태·미결 점검 (memory-operations M4) — 읽기 전용, 자동 수정·종료·승격·명령 실행 없음.

  sb_audit.py --scope S --json
  sb_audit.py --all --scopes S,T --json        명시한 프로젝트 집합만, 항목마다 scope_id 보존

reason_codes (모두 '후보'이며 판정이 아니다):
  value_conflict            같은 scope/fact_key 의 미채택 후보 값이 head 와 다르다 (구조화 값 우선)
  verification_contradicted 후보의 최근 검증이 contradicted
  observed_at_unknown       head 의 관측 시각이 null
  stale_check               head 관측 시각이 SB_AUDIT_STALE_DAYS(기본 30)일보다 오래됨 → 재검증 후보
  unverified_head           acceptance 가 없는 legacy head
  inference_pending         승격되지 않은 추론 후보 (승격 대상이 아님)
  review_due                미결 next_review 가 지났다 → 재검토 후보 (미처리·완료의 증명이 아니다)
  missing_next_action       열린 미결에 다음 행동이 없다
  completion_candidate      head 본문이 'loop:<ID>' 로 명시 연결한 열린 미결 → 완료 후보 (제목 유사성은 쓰지 않는다)
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sb_search  # noqa: E402
import sb_state  # noqa: E402

STALE_DAYS = int(os.environ.get('SB_AUDIT_STALE_DAYS', '30'))
LOOP_LINK = re.compile(r'loop:(L[0-9a-zA-Z]+)')


def _now() -> datetime:
    raw = os.environ.get('SB_EVAL_NOW')
    if raw:
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _value(body: str, value_json: Any) -> str:
    if value_json:
        try:
            v = json.loads(value_json)
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        except ValueError:
            pass
    return (body or '').strip()


def audit_scope(scope_id: str) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    warnings: List[str] = []
    now = _now()
    heads = sb_state.query('head', scope_id)
    status = 'ok'
    if heads['status'] in ('unavailable', 'error', 'degraded'):
        warnings += heads['warnings'] or [heads['status']]
        status = 'degraded'
    head_rows: Dict[str, Dict[str, Any]] = {h['fact_key']: h for h in heads['items']}
    candidates: List[sqlite3.Row] = []
    if heads['status'] not in ('unavailable', 'error'):
        try:
            with closing(sb_state.ro_connect(None)) as db:
                if 'state_candidate' in sb_state.tables(db):
                    candidates = db.execute(
                        "SELECT c.*, (SELECT result FROM state_verification v WHERE v.candidate_id=c.id ORDER BY v.id DESC LIMIT 1) AS last_result "
                        "FROM state_candidate c WHERE c.scope_id=? AND c.status IN ('proposed','conflict','rejected') ORDER BY c.id",
                        (scope_id,)).fetchall()
                    for h in head_rows.values():
                        row = db.execute('SELECT value_json FROM state_candidate c JOIN state_acceptance a ON a.candidate_id=c.id WHERE a.state_id=?',
                                         (h['item_id'],)).fetchone()
                        h['_value'] = _value(h['body'], row['value_json'] if row else None)
                else:
                    warnings.append('candidate_table_missing')
        except (sqlite3.Error, sb_state.StateError) as exc:
            warnings.append('candidates_unreadable:' + str(exc)); status = 'degraded'
    loops = sb_search._next_items(scope_id)
    if loops['status'] == 'degraded':
        warnings += loops['warnings']; status = 'degraded'
    open_loops = {l['loop_id']: l for l in loops['items']}
    for key, h in head_rows.items():
        codes, refs = [], []
        if h['observed_at'] is None:
            codes.append('observed_at_unknown')
        else:
            try:
                age = (now - datetime.fromisoformat(h['observed_at'].replace('Z', '+00:00'))).days
                if age > STALE_DAYS:
                    codes.append('stale_check'); refs.append('observed_at=%s (%d일 전)' % (h['observed_at'], age))
            except ValueError:
                codes.append('observed_at_unknown')
        if h['verification_status'] != 'verified':
            codes.append('unverified_head')
        head_val = h.get('_value', _value(h['body'], None))
        for c in candidates:
            if c['fact_key'] != key or c['kind'] == 'inference':
                continue
            if c['last_result'] == 'contradicted':
                codes.append('verification_contradicted'); refs.append('candidate:%d' % c['id'])
            if _value(c['body'], c['value_json']) != head_val:
                if 'value_conflict' not in codes:
                    codes.append('value_conflict')
                refs.append('candidate:%d=%s' % (c['id'], _value(c['body'], c['value_json'])[:60]))
        for lid in LOOP_LINK.findall(h['body'] or ''):
            if lid in open_loops:
                items.append({'scope_id': scope_id, 'layer': 'L1', 'item_id': lid, 'loop_id': lid, 'fact_key': None,
                              'body': open_loops[lid]['body'], 'reason_codes': ['completion_candidate'],
                              'evidence_ref': 'state:%s/%s' % (scope_id, key), 'evidence_quote': (h['body'] or '')[:160],
                              'basis': 'head 본문의 명시 연결 loop:%s' % lid, 'observed_at': h['observed_at'], 'recorded_at': h['recorded_at'],
                              'verification_status': h['verification_status']})
        items.append({'scope_id': scope_id, 'layer': 'L2', 'item_id': h['item_id'], 'fact_key': key, 'body': h['body'],
                      'reason_codes': codes, 'evidence_ref': h['evidence_ref'], 'basis': '; '.join(refs) or None,
                      'observed_at': h['observed_at'], 'recorded_at': h['recorded_at'], 'verification_status': h['verification_status'],
                      'label': h.get('label')})
    for c in candidates:
        if c['kind'] == 'inference' and c['status'] == 'proposed':
            items.append({'scope_id': scope_id, 'layer': 'L2-candidate', 'item_id': c['id'], 'fact_key': c['fact_key'], 'body': c['body'],
                          'reason_codes': ['inference_pending'], 'evidence_ref': None, 'basis': '추론 후보는 근거 없이 승격되지 않는다',
                          'observed_at': c['observed_at'], 'recorded_at': c['recorded_at'], 'verification_status': 'candidate'})
    today = now.date()
    for lid, l in open_loops.items():
        codes, basis = [], []
        if l.get('next_review'):
            try:
                due = date.fromisoformat(l['next_review'][:10])
                if due < today:
                    codes.append('review_due'); basis.append('next_review=%s (%d일 경과) — 재검토 후보일 뿐 상태 판정이 아님' % (l['next_review'], (today - due).days))
            except ValueError:
                pass
        if not l.get('next_action') and l['status'] == 'open':
            codes.append('missing_next_action')
        items.append({'scope_id': scope_id, 'layer': 'L1', 'item_id': lid, 'loop_id': lid, 'fact_key': None, 'body': l['body'],
                      'reason_codes': codes, 'evidence_ref': l['evidence_ref'], 'basis': '; '.join(basis) or None,
                      'status': l['status'], 'value': l.get('value'), 'next_review': l.get('next_review'),
                      'observed_at': None, 'recorded_at': l.get('recorded_at'), 'verification_status': 'n/a'})
    flagged = [i for i in items if i['reason_codes']]
    if status == 'ok' and not items:
        status = 'empty'
    return {'schema_version': 1, 'query_mode': 'audit', 'scope': scope_id, 'status': status, 'items': items,
            'omitted': 0, 'warnings': warnings, 'heads_count': len(head_rows), 'flagged_count': len(flagged),
            'actions_taken': [], 'note': '점검 후보만 제시한다. 자동 종료·승격·명령 실행은 하지 않는다.'}


def portfolio(scopes: List[str]) -> Dict[str, Any]:
    per = [audit_scope(s) for s in scopes]
    order = ['error', 'unavailable', 'degraded', 'ok', 'empty']
    status = min((p['status'] for p in per), key=order.index)
    return {'schema_version': 1, 'query_mode': 'portfolio_audit', 'scope': {'all': True, 'scopes': scopes}, 'status': status,
            'items': [i for p in per for i in p['items']], 'omitted': 0, 'warnings': [w for p in per for w in p['warnings']],
            'heads_count': sum(p['heads_count'] for p in per), 'flagged_count': sum(p['flagged_count'] for p in per),
            'per_scope': {p['scope']: {'heads_count': p['heads_count'], 'flagged_count': p['flagged_count'], 'status': p['status']} for p in per},
            'actions_taken': []}


def main(argv: List[str]) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 파이프(cp949 등)에서도 한글·이모지 출력
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--scope'); ap.add_argument('--all', action='store_true'); ap.add_argument('--scopes'); ap.add_argument('--json', action='store_true')
    ns = ap.parse_args(argv)
    try:
        if ns.all:
            scopes = [s.strip() for s in (ns.scopes or '').split(',') if s.strip()]
            if not scopes:
                raise ValueError('--all requires --scopes with an explicit project list')
            out = portfolio(scopes)
        else:
            if ns.scope is not None and not ns.scope.strip():
                raise ValueError('scope_required')
            from sb_scope import resolve_scope_id
            out = audit_scope(ns.scope.strip() if ns.scope else resolve_scope_id(os.getcwd())[0])
    except ValueError as exc:
        print(json.dumps({'schema_version': 1, 'query_mode': 'audit', 'status': 'error', 'items': [], 'omitted': 0, 'warnings': [str(exc)]},
                         ensure_ascii=False))
        return 2
    if ns.json:
        print(json.dumps(out, ensure_ascii=False, allow_nan=False))
    else:
        print('[audit] scope=%s status=%s heads=%d flagged=%d' % (out['scope'], out['status'], out['heads_count'], out['flagged_count']))
        for it in out['items']:
            if it['reason_codes']:
                print(' -', it.get('fact_key') or it.get('loop_id'), ':', ','.join(it['reason_codes']), '|', (it.get('basis') or '')[:100])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
