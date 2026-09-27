#!/usr/bin/env python3
"""활용(행동) DOE — 헤드리스 claude -p 로 시나리오 × 조건을 돌려 에이전트가 기록층을 실제로 쓰는지 본다.

조건 요인은 훅 환경변수로 바꾼다(SB_RECALL_BODIES, SB_STOP_GATE 등 — conditions.json).
기록 오염 방지: 실험 cwd 는 claude-mem CLAUDE_MEM_EXCLUDED_PROJECTS 경로 패턴으로 제외해 둔다.

  python eval/behavior_eval.py run   --sandbox <cwd> [--dir D] [--model opus] [--parallel 2] [--only S1,S2]
  python eval/behavior_eval.py judge-pack [--dir D]    # 판정용 묶음(judge.jsonl) 생성
  python eval/behavior_eval.py report     [--dir D]    # grades.json 읽어 요인 효과 집계
"""
import argparse
import concurrent.futures as cf
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
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
RECALL_MARKERS = ('sb search', 'sb timeline', 'sb state', 'sb loops', 'get_observations', 'mcp-search', 'claude-mem')


def load(path):
    return json.load(open(path, encoding='utf-8'))


def run_one(sc, cond, rep, args):
    env = dict(os.environ)
    env.update({k: str(v) for k, v in cond['env'].items()})
    env['SB_PROJECT_ALIASES'] = str(Path(args.dir) / 'aliases.json')
    claude = shutil.which('claude') or 'claude'
    cmd = [claude, '-p', sc['prompt'], '--output-format', 'stream-json', '--verbose',
           '--model', args.model, '--permission-mode', 'bypassPermissions',
           '--disallowedTools', 'Edit', 'Write', 'NotebookEdit', '--max-turns', '30']
    t = time.time()
    try:
        p = subprocess.run(cmd, cwd=args.sandbox, env=env, capture_output=True, timeout=args.timeout,
                           encoding='utf-8', errors='replace')
        lines, err = p.stdout.splitlines(), p.stderr[-800:]
    except subprocess.TimeoutExpired as exc:
        lines, err = (exc.stdout or '').splitlines() if isinstance(exc.stdout, str) else [], 'timeout'
    tools, recall_calls, final, meta, hook_ctx, texts = [], 0, '', {}, [], []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get('type') == 'assistant':
            for block in ev.get('message', {}).get('content', []):
                if block.get('type') == 'text' and block.get('text'):
                    texts.append(block['text'])   # 헤드리스 result 는 마지막 메시지만 담는다 — 채점은 전체로
                if block.get('type') == 'tool_use':
                    name = block.get('name', '')
                    inp = json.dumps(block.get('input', {}), ensure_ascii=False)
                    tools.append({'name': name, 'input': inp[:300]})
                    if any(m in name or m in inp for m in RECALL_MARKERS):
                        recall_calls += 1
        elif ev.get('type') == 'result':
            final = ev.get('result') or ''
            meta = {k: ev.get(k) for k in ('duration_ms', 'num_turns', 'total_cost_usd', 'is_error')}
        elif ev.get('type') == 'system' and 'hook' in json.dumps(ev, ensure_ascii=False)[:200].lower():
            hook_ctx.append(json.dumps(ev, ensure_ascii=False)[:300])
    return {'sid': sc['sid'], 'cond': cond['cid'], 'rep': rep, 'prompt': sc['prompt'], 'final': final,
            'all_text': '\n\n'.join(texts) or final,
            'tools': tools, 'n_tools': len(tools), 'recall_calls': recall_calls, 'wall_s': round(time.time() - t, 1),
            'meta': meta, 'stderr': err, 'hooks': hook_ctx[:5]}


def run(args):
    d = Path(args.dir)
    scenarios = load(d / 'scenarios.json')
    conds = load(d / 'conditions.json')
    if args.only:
        keep = set(args.only.split(','))
        scenarios = [s for s in scenarios if s['sid'].split('-')[0] in keep or s['sid'] in keep]
    out_path = d / 'behavior_runs.jsonl'
    done = set()
    if out_path.exists():
        for line in open(out_path, encoding='utf-8'):
            r = json.loads(line)
            done.add((r['sid'], r['cond'], r['rep']))
    jobs = [(s, c, r) for r in range(args.reps) for c in conds for s in scenarios if (s['sid'], c['cid'], r) not in done]
    print('jobs', len(jobs), flush=True)
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as pool, open(out_path, 'a', encoding='utf-8') as sink:
        futs = {pool.submit(run_one, s, c, r, args): (s['sid'], c['cid'], r) for s, c, r in jobs}
        for n, fut in enumerate(cf.as_completed(futs), 1):
            res = fut.result()
            sink.write(json.dumps(res, ensure_ascii=False) + '\n')
            sink.flush()
            print('%d/%d %s %s recall=%d tools=%d %.0fs' % (n, len(jobs), res['sid'], res['cond'], res['recall_calls'],
                                                          res['n_tools'], res['wall_s']), flush=True)


def judge_pack(args):
    d = Path(args.dir)
    gold = {s['sid']: s for s in load(d / 'scenarios.json')}
    rows = [json.loads(l) for l in open(d / 'behavior_runs.jsonl', encoding='utf-8')]
    with open(d / 'judge.jsonl', 'w', encoding='utf-8') as f:
        for n, r in enumerate(rows):
            f.write(json.dumps({'rid': n, 'sid': r['sid'], 'prompt': r['prompt'], 'gold': gold[r['sid']]['gold'],
                                'answer': (r.get('all_text') or r['final'])[:6000]}, ensure_ascii=False) + '\n')
    print('judge items', len(rows))


def report(args):
    d = Path(args.dir)
    rows = [json.loads(l) for l in open(d / 'behavior_runs.jsonl', encoding='utf-8')]
    grades = {int(k): v for k, v in load(d / 'grades.json').items()}
    conds = {c['cid']: c for c in load(d / 'conditions.json')}
    for n, r in enumerate(rows):
        r['grade'] = grades.get(n, {}).get('score')
    lines = ['# 활용(행동) DOE 결과', '', '| 조건 | 설명 | 정답점수(0-2) 평균 | 기록 조회 호출 평균 | 조회 0회 비율 | 소요(s) | 비용($) | n |',
             '|---|---|---|---|---|---|---|---|']
    for cid, c in conds.items():
        sub = [r for r in rows if r['cond'] == cid and r['sid'] != 'S9-negative' and r['grade'] is not None]
        if not sub:
            continue
        lines.append('| %s | %s | %.2f | %.1f | %.0f%% | %.0f | %.3f | %d |' % (
            cid, c.get('desc', ''), statistics.mean(r['grade'] for r in sub),
            statistics.mean(r['recall_calls'] for r in sub), 100 * statistics.mean(r['recall_calls'] == 0 for r in sub),
            statistics.mean(r['wall_s'] for r in sub), statistics.mean((r['meta'] or {}).get('total_cost_usd') or 0 for r in sub), len(sub)))
    lines += ['', '## 시나리오별 점수 (조건 순서대로)', '']
    for sid in sorted({r['sid'] for r in rows}):
        cells = []
        for cid in conds:
            g = [r['grade'] for r in rows if r['sid'] == sid and r['cond'] == cid and r['grade'] is not None]
            k = [r['recall_calls'] for r in rows if r['sid'] == sid and r['cond'] == cid]
            cells.append('%s: %s (조회 %s)' % (cid, '/'.join(str(x) for x in g) or '-', '/'.join(str(x) for x in k) or '-'))
        lines.append('- **%s** — %s' % (sid, ' · '.join(cells)))
    (d / 'behavior_report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('stage', choices=('run', 'judge-pack', 'report'))
    ap.add_argument('--dir', default=str(DEFAULT_DIR))
    ap.add_argument('--sandbox')
    ap.add_argument('--model', default='opus')
    ap.add_argument('--parallel', type=int, default=2)
    ap.add_argument('--reps', type=int, default=1)
    ap.add_argument('--timeout', type=int, default=600)
    ap.add_argument('--only')
    args = ap.parse_args()
    {'run': run, 'judge-pack': judge_pack, 'report': report}[args.stage](args)


if __name__ == '__main__':
    main()
