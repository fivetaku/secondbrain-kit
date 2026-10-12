#!/usr/bin/env python3
"""회수(검색) DOE 하네스 — 백엔드·질의형태·필터·주입정책 조합을 한 번에 평가한다.

단계
  collect  질의마다 백엔드×질의형태의 상위 N 결과를 한 번만 수집해 runs.jsonl 에 캐시한다(느린 부분).
  pool     조합별 상위 K 를 모아 판정할 (질의, 관측) 쌍을 pool.jsonl 로 뽑는다(사람/LLM 판정용).
  score    qrels(판정) + runs 로 모든 요인 조합의 지표를 계산해 doe.csv·effects.md 로 쓴다(빠름, 오프라인).

데이터(질의·판정·결과)는 개인 기록이므로 --dir(기본 ~/.secondbrain/local/eval)에만 둔다.

  python eval/retrieval_eval.py collect [--dir D] [--limit 30]
  python eval/retrieval_eval.py pool    [--dir D] [--k 5]
  python eval/retrieval_eval.py score   [--dir D]
"""
import argparse
import csv
import itertools
import json
import math
import os
import random
import re
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / 'bin'))
import sb_config  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass

DEFAULT_DIR = Path(sb_config.sb_path('local', 'eval'))
BACKENDS = ('ko_all', 'ko_nn', 'vec', 'worker')
QFORMS = ('raw', 'kw')


def load_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


# ── 백엔드 ─────────────────────────────────────────────────────────────
def _ko():
    import sb_fts_ko
    sb_fts_ko.ensure_current()
    return sb_fts_ko


def ko_tokens(ko, text, nouns_only):
    if not nouns_only:
        return ko.ko_query_tokens(text)
    ko._tokenizer()
    if ko._kiwi is None:
        return ko.ko_query_tokens(text)
    ident = [w.lower() for w in re.findall(r'[A-Za-z0-9_]+(?:[._/-][A-Za-z0-9_]+)*', text)]
    toks = ident + [t.form.lower() for t in ko._kiwi.tokenize(text)
                    if t.tag.startswith('NN') or t.tag in {'SL', 'SN', 'SH'}]
    return list(dict.fromkeys(toks))


def ko_search(ko, query, limit, nouns_only):
    toks = ko_tokens(ko, query, nouns_only)
    if not toks:
        return []
    match = ' OR '.join('"' + t.replace('"', '""') + '"' for t in toks)
    with closing(ko.open_index(create=False)) as conn:
        rows = conn.execute('SELECT obs_id, bm25(ko_fts) FROM ko_fts WHERE ko_fts MATCH ? '
                            'ORDER BY bm25(ko_fts), obs_id LIMIT ?', (match, limit)).fetchall()
    return [(int(r[0]), -float(r[1])) for r in rows]


def vec_search(query, limit):
    import sb_search
    hits, meta = sb_search.vector_search(query, sb_search.Scope('global', []), limit)
    if not meta.get('ok'):
        raise RuntimeError(meta.get('error'))
    return [(h.obs_id, h.score) for h in hits]


def worker_search(query, limit):
    base = sb_config.worker_base_url()
    url = base + '/api/search/observations?' + urllib.parse.urlencode({'query': query[:300], 'limit': str(limit)})
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.loads(r.read().decode())
    text = d['content'][0]['text'] if isinstance(d.get('content'), list) else ''
    ids = [int(m.group(1)) for m in re.finditer(r'^\| #(\d+) \|', text, re.M)]
    return [(i, 1.0 / (n + 1)) for n, i in enumerate(ids)]


def collect(args):
    d = Path(args.dir)
    queries = load_jsonl(d / 'queries.jsonl')
    ko = _ko()
    out, done = [], set()
    runs = d / 'runs.jsonl'
    if runs.exists() and not args.fresh:
        out = load_jsonl(runs)
        done = {(r['qid'], r['backend'], r['qform']) for r in out}
    # 워밍업(콜드 스타트는 latency 지표에서 따로 본다)
    vec_search('워밍업', 1)
    for n, q in enumerate(queries, 1):
        kw = ' '.join(ko_tokens(ko, q['query'], True)) or q['query']
        for backend, qform in itertools.product(BACKENDS, QFORMS):
            if backend.startswith('ko') and qform == 'kw':
                continue  # 형태소 추출이 곧 kw — 중복 수집 생략
            if (q['qid'], backend, qform) in done:
                continue
            text = q['query'] if qform == 'raw' else kw
            t = time.perf_counter()
            try:
                if backend == 'ko_all':
                    hits = ko_search(ko, text, args.limit, False)
                elif backend == 'ko_nn':
                    hits = ko_search(ko, text, args.limit, True)
                elif backend == 'vec':
                    hits = vec_search(text, args.limit)
                else:
                    hits = worker_search(text, args.limit)
                err = None
            except Exception as exc:  # noqa: BLE001
                hits, err = [], str(exc)[:200]
            out.append({'qid': q['qid'], 'backend': backend, 'qform': qform, 'text': text,
                        'ids': [h[0] for h in hits], 'scores': [round(h[1], 6) for h in hits],
                        'ms': round((time.perf_counter() - t) * 1000, 1), 'error': err})
        if n % 10 == 0:
            write_jsonl(runs, out)
            print('collect %d/%d' % (n, len(queries)), flush=True)
    write_jsonl(runs, out)
    errs = sum(1 for r in out if r['error'])
    print('runs=%d errors=%d -> %s' % (len(out), errs, runs))


# ── 조합 ───────────────────────────────────────────────────────────────
def rrf(lists, k):
    s = {}
    for ids in lists:
        for rank, i in enumerate(dict.fromkeys(ids), 1):
            s[i] = s.get(i, 0.0) + 1.0 / (k + rank)
    return sorted(s, key=lambda i: (-s[i], i)), s


RETRIEVERS = {
    'ko_all': ['ko_all'], 'ko_nn': ['ko_nn'], 'vec': ['vec'], 'worker': ['worker'],
    'ko_all+vec': ['ko_all', 'vec'], 'ko_nn+vec': ['ko_nn', 'vec'],
    'ko_nn+worker': ['ko_nn', 'worker'], 'ko_nn+vec+worker': ['ko_nn', 'vec', 'worker'],
}


def ranked(runs_by, qid, retriever, qform, k, exclude):
    lists = []
    for b in RETRIEVERS[retriever]:
        form = 'raw' if b.startswith('ko') else qform
        r = runs_by.get((qid, b, form))
        if r is None:
            return None
        lists.append([i for i in r['ids'] if i not in exclude])
    order, _ = rrf(lists, k)
    return order


def pool(args):
    d = Path(args.dir)
    queries = load_jsonl(d / 'queries.jsonl')
    runs_by = {(r['qid'], r['backend'], r['qform']): r for r in load_jsonl(d / 'runs.jsonl')}
    obs = _obs_index()
    pairs = []
    for q in queries:
        ids = set()
        for retriever, qform in itertools.product(RETRIEVERS, QFORMS):
            order = ranked(runs_by, q['qid'], retriever, qform, 60, set())
            if order:
                ids.update(order[:args.k])
        if q.get('target_id'):
            ids.add(q['target_id'])
        for i in sorted(ids):
            o = obs.get(i)
            if o:
                pairs.append({'qid': q['qid'], 'query': q['query'], 'obs_id': i,
                              'title': o['title'], 'snippet': (o['narrative'] or o['text'] or '')[:400]})
    write_jsonl(d / 'pool.jsonl', pairs)
    print('pool pairs=%d queries=%d -> %s' % (len(pairs), len(queries), d / 'pool.jsonl'))


def _obs_index():
    con = sqlite3.connect('file:%s?mode=ro' % Path(sb_config.claude_mem_db()).as_posix(), uri=True)
    con.row_factory = sqlite3.Row
    return {r['id']: dict(r) for r in con.execute('SELECT id, project, title, narrative, text, metadata FROM observations')}


# ── 지표 ───────────────────────────────────────────────────────────────
def dcg(gains):
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def metrics_for(order, rel, k=5):
    top = order[:k]
    gains = [rel.get(i, 0) for i in top]
    ideal = sorted(rel.values(), reverse=True)[:k]
    first = next((n for n, i in enumerate(order[:10], 1) if rel.get(i, 0) >= 1), None)
    first2 = next((n for n, i in enumerate(order[:10], 1) if rel.get(i, 0) >= 2), None)
    return {
        'hit1': 1.0 if order[:1] and rel.get(order[0], 0) >= 1 else 0.0,
        'hit5': 1.0 if any(g >= 1 for g in gains) else 0.0,
        'hit5_strict': 1.0 if any(g >= 2 for g in gains) else 0.0,
        'mrr': 1.0 / first if first else 0.0,
        'mrr_strict': 1.0 / first2 if first2 else 0.0,
        'ndcg5': dcg(gains) / dcg(ideal) if ideal and dcg(ideal) > 0 else 0.0,
        'noise5': sum(1 for g in gains if g == 0) / max(1, len(top)),
    }


def title_filter(order, query, obs, ko):
    toks = set(ko_tokens(ko, query, True))
    keep = []
    for i in order:
        title = (obs.get(i) or {}).get('title') or ''
        if any(t in title.lower() for t in toks):
            keep.append(i)
    return keep


def score(args):
    d = Path(args.dir)
    queries = load_jsonl(d / 'queries.jsonl')
    runs = load_jsonl(d / 'runs.jsonl')
    runs_by = {(r['qid'], r['backend'], r['qform']): r for r in runs}
    qrels = json.load(open(d / 'qrels.json', encoding='utf-8'))
    obs = _obs_index()
    ko = _ko()
    tool = set(json.load(open(d / 'tool_meta_ids.json')))
    auto = {i for i, o in obs.items() if (o['title'] or '').startswith('[automemory]')}
    factors = {
        'retriever': list(RETRIEVERS), 'qform': list(QFORMS), 'rrf_k': [10, 60],
        'excl_tool': [0, 1], 'excl_auto': [0, 1], 'inject': ['top5', 'title_filter'],
    }
    rows = []
    for combo in itertools.product(*factors.values()):
        f = dict(zip(factors, combo))
        if len(RETRIEVERS[f['retriever']]) == 1 and f['rrf_k'] == 10:
            continue  # 단일 백엔드는 k 무관
        if f['retriever'].startswith('ko') and '+' not in f['retriever'] and f['qform'] == 'kw':
            continue
        exclude = (tool if f['excl_tool'] else set()) | (auto if f['excl_auto'] else set())
        per_q, neg = [], []
        for q in queries:
            order = ranked(runs_by, q['qid'], f['retriever'], f['qform'], f['rrf_k'], exclude)
            if order is None:
                continue
            if f['inject'] == 'title_filter':
                order = title_filter(order, q['query'], obs, ko)
            rel = {int(k): v for k, v in (qrels.get(q['qid']) or {}).items()}
            if q['style'] == 'negative':
                injected = order[:5]
                neg.append({'any': 1.0 if any(rel.get(i, 0) == 0 for i in injected) else 0.0,
                            'n': sum(1 for i in injected if rel.get(i, 0) == 0)})
            else:
                m = metrics_for(order, rel)
                m['style'] = q['style']
                per_q.append(m)
        if not per_q:
            continue
        row = dict(f)
        for key in ('hit1', 'hit5', 'hit5_strict', 'mrr', 'mrr_strict', 'ndcg5', 'noise5'):
            row[key] = round(statistics.mean(m[key] for m in per_q), 4)
        for style in ('keyword', 'natural', 'vague'):
            vals = [m['ndcg5'] for m in per_q if m['style'] == style]
            row['ndcg5_' + style] = round(statistics.mean(vals), 4) if vals else None
        row['neg_inject_rate'] = round(statistics.mean(x['any'] for x in neg), 4) if neg else None
        row['neg_noise_items'] = round(statistics.mean(x['n'] for x in neg), 3) if neg else None
        row['n_pos'], row['n_neg'] = len(per_q), len(neg)
        rows.append(row)
    rows.sort(key=lambda r: -r['ndcg5'])
    with open(d / 'doe.csv', 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    lat = {}
    for r in runs:
        lat.setdefault((r['backend'], r['qform']), []).append(r['ms'])
    report = ['# 회수 DOE 결과', '', '조합 %d개, 긍정 질의 %d, 부정 질의 %d' % (len(rows), rows[0]['n_pos'], rows[0]['n_neg']), '',
              '## 요인별 주효과 (nDCG@5 / Hit@5 / 부정질의 주입률, 조합 평균)', '']
    for name, levels in factors.items():
        report.append('- **%s**' % name)
        for lv in levels:
            sub = [r for r in rows if r[name] == lv]
            if sub:
                report.append('  - %s: nDCG@5 %.3f · Hit@5 %.3f · MRR %.3f · 부정주입 %.2f  (n=%d)' % (
                    lv, statistics.mean(r['ndcg5'] for r in sub), statistics.mean(r['hit5'] for r in sub),
                    statistics.mean(r['mrr'] for r in sub),
                    statistics.mean(r['neg_inject_rate'] or 0 for r in sub), len(sub)))
    report += ['', '## 상위 10개 조합', '', '| retriever | qform | k | excl_tool | excl_auto | inject | nDCG@5 | Hit@1 | Hit@5 | MRR | 부정주입 | kw/nat/vague nDCG |',
               '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for r in rows[:10]:
        report.append('| %s | %s | %s | %s | %s | %s | %.3f | %.3f | %.3f | %.3f | %s | %s/%s/%s |' % (
            r['retriever'], r['qform'], r['rrf_k'], r['excl_tool'], r['excl_auto'], r['inject'], r['ndcg5'],
            r['hit1'], r['hit5'], r['mrr'], r['neg_inject_rate'], r['ndcg5_keyword'], r['ndcg5_natural'], r['ndcg5_vague']))
    report += ['', '## 지연(ms, 워밍업 후 프로세스 내 호출)', '']
    for (b, f), v in sorted(lat.items()):
        v = sorted(v)
        report.append('- %s/%s: p50 %.0f · p95 %.0f' % (b, f, v[len(v) // 2], v[int(len(v) * .95) - 1]))
    (d / 'effects.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    print('\n'.join(report[:60]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('stage', choices=('collect', 'pool', 'score'))
    ap.add_argument('--dir', default=str(DEFAULT_DIR))
    ap.add_argument('--limit', type=int, default=30)
    ap.add_argument('--k', type=int, default=5)
    ap.add_argument('--fresh', action='store_true')
    args = ap.parse_args()
    {'collect': collect, 'pool': pool, 'score': score}[args.stage](args)


if __name__ == '__main__':
    main()
