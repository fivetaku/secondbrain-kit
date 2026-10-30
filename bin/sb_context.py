#!/usr/bin/env python3
"""외부 워커(다른 에이전트·원격 봇 등)에 넘길 세컨브레인 맥락 블록을 만든다 — 읽기 전용.

층 순서: L2 확정 사실(current) → L1 다음 행동(next) → L3 규칙 제목 → 관련 기록(상주 회수 서버).
각 줄에 출처 ID를 붙이고 --max-bytes 안으로 자른다. 밖으로 나가는 글이므로 금액·이메일·전화번호·계좌류 숫자는 가린다.

  sb context --query "<작업 설명>" [--scope <scope_id>] [--project <claude-mem 프로젝트>] [--max-bytes 6000]

scope 를 안 주면 현재 폴더를 sb_scope 로 해석한다. 회수 서버(sb_recalld)가 꺼져 있어도 나머지 층으로 블록을 만들고,
조회에 실패한 층이 있으면 '없음'과 구분되게 한 줄 표시한다. 회수 서버 주소: SB_RECALLD_URL > sb_recalld 기본(포트 37791).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import sb_recalld  # noqa: E402
import sb_scope  # noqa: E402

# '원' 뒤에 한글이 붙으면 '3원칙'·'2원화' 같은 낱말이라 금액이 아니다. 금액 뒤에 흔히 오는 조사·의존어만 허용한다
# ('500만원을'·'1,200,000원에' 는 가린다).
WON = (r'원(?:(?![가-힣])|(?=은|이|을|과|으로|에|의|도|만|씩|짜리|까지|부터|정도|대|선|어치|가량|쯤|당|미만|초과|'
       r'내외|밖에|뿐|보다|처럼|인데|인지|입니다))')
# 8자리 날짜(YYYYMMDD)는 숫자 덩어리로 가리지 않는다
DATE8 = r'(?!(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?![\d,.-]))'
SOURCE_PREFIXES = ('observation', 'prompt')   # 확정 사실 줄의 '(출처 …)'에 실제로 붙는 ID 접두
MASKS = [
    (re.compile(r'\d(?:[\d,.]*\d)?\s?(?:천|만|억)\s?' + WON + r'|\d(?:[\d,]*\d)?\s?' + WON), '<amount>'),
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'), '<email>'),
    # 주민·법인등록번호(6-7). 계좌·전화 패턴보다 먼저 가린다
    (re.compile(r'(?<![\d-])\d{6}-\d{7}(?![\d-])'), '<id>'),
    # 4묶음 계좌·카드번호(예: 351-1234-5678-13, 4111-1111-1111-1111). 3묶음 계좌 패턴은 (?![\d-]) 때문에 이를 통째로 놓친다
    (re.compile(r'(?<![\d-])\d{2,6}(?:[- ]\d{2,8}){3}(?![\d-])'), '<account>'),
    # \b 는 뒤에 한글이 붙으면 경계로 안 잡혀 lookaround 를 쓴다. +82 국가번호·점 구분도 가린다
    (re.compile(r'(?:\+82[- ]?|(?<!\d)0)1[016789][-. ]?\d{3,4}[-. ]?\d{4}(?!\d)'), '<phone>'),
    # 계좌번호류. 날짜 YYYY-MM-DD 는 끝자리가 짧아 남는다
    (re.compile(r'(?<![\d-])\d{3,6}-\d{2,6}-\d{4,8}(?![\d-])'), '<account>'),
    # 남은 7자리 이상 숫자(금액·계좌류). 날짜(하이픈)·버전(점)에 붙은 숫자와, 이 블록이 실제로 내는 출처 ID 만 남긴다 —
    # 확정 사실 출처(sb_state 검증 대상 observation:N·prompt:N)와 관련 기록 줄 머리의 '- #N'(build 가 줄마다 mask 를
    # 부르므로 \A 가 줄 머리다). 받는 쪽이 출처를 되짚을 수 있어야 한다. 'acct:12345678' 같은 임의 라벨이나 본문 중간의
    # '#12345678' 은 계좌·주문번호일 수 있어 가린다
    (re.compile(r'(?<![\d.-])(?<!\A- #)' + ''.join('(?<!%s:)' % p for p in SOURCE_PREFIXES) + DATE8
                + r'\d[\d,]{5,}\d(?![\d.-])'), '<num>'),
]
RECALL_LIMIT = 6
# sb_search --mode 가 층 저장소를 아직 안 만든 새 사용자에게 내는 unavailable 경고 코드(상태층 DB·미결 원장·규칙 폴더 없음).
# 이것만이면 '조회 실패'가 아니라 '없음'이다. 상태층 테이블 없음·잠금 오류 같은 그 밖의 unavailable 은 실패로 둔다
NOT_YET_CREATED = frozenset({'state_db_missing', 'loops_file_missing', 'rules_dir_missing'})
UNAVAILABLE_EXIT = 8   # sb_search MODE_EXIT['unavailable']


def mask(text: str) -> str:
    for pattern, repl in MASKS:
        text = pattern.sub(repl, text)
    return text


def recalld_url() -> str:
    # 키트 훅(sb_error_recall)과 같은 규칙: SB_RECALLD_URL 이 있으면 그것, 없으면 sb_recalld 기본(SB_RECALLD_PORT, 37791)
    return (os.environ.get('SB_RECALLD_URL') or sb_recalld.base_url()).rstrip('/')


def mode(name: str, scope: str) -> Optional[List[Dict[str, Any]]]:
    """sb_search --mode 결과 items. 실패면 None(빈 결과 [] 와 구분, 아직 안 만든 층은 []).
    같은 파이썬으로 따로 띄운다 — 상태층 DB 잠금 등으로 멈춰도 시간 제한으로 끊고 나머지 층은 낸다."""
    try:
        proc = subprocess.run([sys.executable, str(BIN / 'sb_search.py'), '--mode', name, '--scope', scope, '--json'],
                              capture_output=True, encoding='utf-8', errors='replace', timeout=20,
                              env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        envelope = json.loads(proc.stdout)
        status, warnings = envelope.get('status'), envelope.get('warnings') or []
        # 설치 직후처럼 층 저장소를 아직 안 만든 것뿐이면 빈 결과다 — 새 사용자 블록에 실패 표시가 늘 붙지 않게
        if (status == 'unavailable' and proc.returncode == UNAVAILABLE_EXIT and warnings
                and set(warnings) <= NOT_YET_CREATED):
            return []
        # 그 밖의 error·unavailable(상태층 테이블 없음, 잠금 오류 등)이나 실패 종료코드는 '빈 결과'가 아니다 —
        # [] 로 내면 받는 쪽이 '사실·미결 없음'으로 오해한다
        if proc.returncode != 0 or status in ('error', 'unavailable'):
            return None
        return envelope.get('items', [])
    except (OSError, ValueError, AttributeError, TypeError, subprocess.SubprocessError):
        return None


def recall(query: str, project: str = '') -> Optional[List[Dict[str, Any]]]:
    """상주 회수 서버의 관련 기록. 확인 게이트를 못 넘으면 [], 서버 불통이면 None."""
    params = {'q': query[:300], 'limit': RECALL_LIMIT}
    if project:
        params['project'] = project
    try:
        with urllib.request.urlopen(recalld_url() + '/recall?' + urllib.parse.urlencode(params), timeout=3) as r:
            d = json.loads(r.read().decode('utf-8'))
        return d.get('items', []) if (d.get('gate') or {}).get('pass') else []
    except (OSError, ValueError, AttributeError):
        return None


def build(query: str, scope: str, max_bytes: int, project: str = '') -> str:
    lines = ['[세컨브레인 맥락 — 읽기 전용, 기록 시점 자료. scope={}]'.format(scope),
             '확정 사실이 아래 관련 기록과 충돌하면 확정 사실을 따르고, 현재 상태는 가능하면 다시 확인하라.']
    facts = mode('current', scope)
    if facts:
        lines.append('## 확정 사실(L2)')
        lines += ['- [{}] {} (출처 {}, 확인 {})'.format(f.get('fact_key'), (f.get('body') or '')[:240], f.get('evidence_ref'),
                                                (f.get('checked_at') or '')[:10]) for f in facts]
    nexts = mode('next', scope)
    if nexts:
        lines.append('## 다음 행동(L1)')
        lines += ['- {} {}'.format(n.get('item_id'), (n.get('body') or '')[:160]) for n in nexts]
    rules = mode('rules', scope)
    if rules:
        titles = re.findall(r'^## (.+)$', rules[0].get('body') or '', re.M)
        if titles:
            lines.append('## 규칙(L3, 제목만)')
            lines += ['- ' + t[:120] for t in titles[:15]]
    hits: Optional[List[Dict[str, Any]]] = []
    if query:   # 현재 프로젝트 기록을 먼저, 남는 자리만 전역
        local = (recall(query, project) or []) if project else []
        wide = recall(query)
        if wide is None and not local:
            hits = None
        else:
            seen = {h.get('id') for h in local}
            hits = (local + [h for h in wide or [] if h.get('id') not in seen])[:RECALL_LIMIT]
    if hits:
        lines.append('## 관련 기록(제목, 관측 ID)')
        lines += ['- #{} {} {} · {}'.format(h.get('id'), h.get('date', ''), h.get('project', ''), (h.get('title') or '')[:110])
                  for h in hits]
    if any(layer is None for layer in (facts, nexts, rules, hits)):
        lines.append('(일부 층 조회 실패 — 없는 것과 구분할 것)')
    out, size = [], 0
    for line in map(mask, lines):
        cost = len(line.encode('utf-8')) + 1
        if size + cost > max_bytes:
            out.append('(용량 한도로 이하 생략)')
            break
        out.append(line)
        size += cost
    return '\n'.join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog='sb context', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--query', default='', help='작업 설명 — 관련 기록 회수에 쓴다(없으면 관련 기록 층을 건너뛴다)')
    ap.add_argument('--scope', help='상태층 scope_id (기본: 현재 폴더를 sb_scope 로 해석)')
    ap.add_argument('--max-bytes', type=int, default=6000)
    ap.add_argument('--project', default=os.path.basename(os.getcwd()), help='claude-mem project 이름(기본: 현재 폴더 이름)')
    a = ap.parse_args(argv)
    print(build(a.query, a.scope or sb_scope.resolve_scope_id()[0], a.max_bytes, a.project))
    return 0


if __name__ == '__main__':
    sys.exit(main())
