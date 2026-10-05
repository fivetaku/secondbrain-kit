#!/usr/bin/env python3
"""Cross-platform memory-only nightly batch (replaces nightly_brain.sh for the kit).

Stages, in order, each under an sb_run deadline:
  pii         sb_pii_sweep.py --chroma (opt-in: SB_NIGHTLY_PII=1)  900s
  preflight   sb_health.py ensure — claude-mem 워커·Ollama 기동      180s
  ko-index    sb_fts_ko.py build                                   900s  core
  automemory  sync_automemory.py --json                            600s  core
  snapshot    sb_search.py warmup --global --limit 1 --fresh       600s
  consolidate consolidate.py run --since-days 7 --json (opt-in)   7200s
  health      sb_health.py --quiet — 결과를 logs/health.json 에 남김  300s

Every stage runs even if an earlier one failed. Result goes to
SB_HOME/logs/nightly_status.json {date, finished_at, status ok|partial|failed,
core_failed, failed_stages, exit_code}; the human log is SB_HOME/logs/nightly.log.
Exit: 1 only when a core stage failed, 75 when another nightly holds the lock, else 0.

Tests/diagnostics may replace a stage command with SB_NIGHTLY_STAGE_OVERRIDE, a JSON object
{"<stage>": ["argv", ...]} or {"<stage>": {"argv": [...], "timeout": seconds}}.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import sb_config  # noqa: E402
import sb_run  # noqa: E402

CORE_STAGES = ('ko-index', 'automemory')


class Stage(NamedTuple):
    name: str
    argv: List[str]
    timeout: float
    quiet: bool = False  # discard stdout (snapshot prints search results)


def build_stages(with_consolidate: bool = False) -> List[Stage]:
    py = sys.executable
    stages = [
        Stage('ko-index', [py, str(BIN / 'sb_fts_ko.py'), 'build'], 900),
        Stage('automemory', [py, str(BIN / 'sync_automemory.py'), '--json'], 600),
        Stage('snapshot', [py, str(BIN / 'sb_search.py'), 'warmup', '--global',
                           '--limit', '1', '--fresh', '--json'], 600, quiet=True),
    ]
    # 동기화(automemory)가 워커에 저장하므로, 워커·Ollama 가 꺼져 있으면 먼저 띄운다.
    stages.insert(0, Stage('preflight', [py, str(BIN / 'sb_health.py'), 'ensure'], 180))
    if (os.environ.get('SB_NIGHTLY_PII') == '1' and os.environ.get('SB_PII_MASK') != '0'
            and Path(os.environ.get('SB_PII_MASK') or sb_config.sb_path('local', 'pii_mask.py')).is_file()):
        # 명시적으로 켠 PC만: 관찰기가 옮겨 적은 실명을 SQLite·벡터에서 가린다. 이 단계는 워커를 강제 종료하므로
        # 반드시 preflight(워커 재기동) 앞에 둔다 — 2026-09-28~10-02 automemory 연속 실패의 원인.
        stages.insert(0, Stage('pii', [py, str(BIN / 'sb_pii_sweep.py'), '--chroma', '--json'], 900))
    if with_consolidate:
        stages.append(Stage('consolidate', [py, str(BIN / 'consolidate.py'), 'run',
                                            '--since-days', os.environ.get('SB_CONS_SINCE_DAYS', '7'),
                                            '--json'],
                            float(os.environ.get('SB_CONS_TIMEOUT', '7200'))))
    stages.append(Stage('health', [py, str(BIN / 'sb_health.py'), '--quiet'], 300))
    return _apply_overrides(stages)


def _apply_overrides(stages: List[Stage]) -> List[Stage]:
    raw = os.environ.get('SB_NIGHTLY_STAGE_OVERRIDE')
    if not raw:
        return stages
    overrides = json.loads(raw)
    result = []
    for stage in stages:
        spec = overrides.get(stage.name)
        if isinstance(spec, list):
            stage = stage._replace(argv=[str(a) for a in spec])
        elif isinstance(spec, dict):
            stage = stage._replace(argv=[str(a) for a in spec.get('argv', stage.argv)],
                                   timeout=float(spec.get('timeout', stage.timeout)))
        result.append(stage)
    return result


def _child_env() -> Dict[str, str]:
    # UTF-8 stdio so Korean JSON output does not crash on a cp949/cp1252 Windows console/log.
    return {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}


def _write_status(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.nightly_status.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_stages(stages: Sequence[Stage], log) -> List[str]:
    failed = []
    for stage in stages:
        log.flush()
        code = sb_run.run_command(
            stage.argv, stage.timeout, env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if stage.quiet else log, stderr=log)
        action = 'timeout' if code == 124 else 'continue'
        print('stage={} exit={} action={}'.format(stage.name, code, action), file=log, flush=True)
        if code != 0:
            failed.append(stage.name)
    return failed


def summarize(day: str, failed: Sequence[str]) -> Dict[str, Any]:
    core_failed = any(name in CORE_STAGES for name in failed)
    status = 'ok' if not failed else ('failed' if core_failed else 'partial')
    return {'date': day,
            'finished_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'status': status, 'core_failed': core_failed,
            'failed_stages': list(failed), 'exit_code': 1 if core_failed else 0}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--with-consolidate', action='store_true',
                        help='also run the weekly consolidation (LLM) stage')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the stage plan; run nothing and write nothing')
    args = parser.parse_args(argv)
    try:
        stages = build_stages(args.with_consolidate)
    except (ValueError, TypeError, AttributeError) as error:
        print('nightly: bad SB_NIGHTLY_STAGE_OVERRIDE: {}'.format(error), file=sys.stderr)
        return 2
    if args.dry_run:
        for stage in stages:
            print('stage={} timeout={:g} core={} argv={}'.format(
                stage.name, stage.timeout, stage.name in CORE_STAGES,
                json.dumps(stage.argv, ensure_ascii=False)))
        return 0

    logs = Path(sb_config.sb_path('logs'))
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print('nightly: cannot create {}: {}'.format(logs, error), file=sys.stderr)
        return 1
    day = datetime.now().date().isoformat()
    source = Path(os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db())
    if not os.environ.get('SB_NIGHTLY_STAGE_OVERRIDE') and not source.exists():
        # 설치 직후 첫 세션 전: claude-mem DB 가 아직 없다 — 실패가 아니라 할 일이 없는 것
        result = dict(summarize(day, []), status='skipped', reason='claude_mem_db_missing')
        try:
            _write_status(logs / 'nightly_status.json', result)
        except OSError:
            pass
        return 0
    try:
        with sb_run.HeldLock(str(logs / 'nightly.lock')):
            with open(logs / 'nightly.log', 'a', encoding='utf-8') as log:
                print('== {} {} start'.format(day, datetime.now().strftime('%H:%M:%S')),
                      file=log, flush=True)
                failed = run_stages(stages, log)
                result = summarize(day, failed)
                print('== {} done status={} core_failed={} failed_stages=[{}] exit={}'.format(
                    day, result['status'], int(result['core_failed']),
                    ' '.join(failed), result['exit_code']), file=log, flush=True)
                try:
                    _write_status(logs / 'nightly_status.json', result)
                except OSError as error:
                    print('status-write failed: {}'.format(error), file=log, flush=True)
                return result['exit_code']
    except sb_run.LockBusy:
        print('already running', file=sys.stderr)
        return 75


if __name__ == '__main__':
    sys.exit(main())
