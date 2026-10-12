#!/usr/bin/env python3
"""sb relabel — 관측을 다른 claude-mem 프로젝트로 옮긴다(되돌리기 가능).

왜: claude-mem 은 cwd 폴더 이름을 project 로 쓴다. 업무 폴더에서 도구·환경 작업(세컨브레인 수리,
    계정 설정 등)을 하면 그 기록이 업무 프로젝트에 섞여, 업무 질문의 회수 1순위를 도구 기록이 차지한다
    (2026-09-26 평가에서 확인). 회수는 프로젝트 우선이므로 제자리로 옮기면 오염이 풀린다.

  sb relabel --ids-file ids.json --to secondbrain-kit [--dry-run]
  sb relabel --undo <journal.json>

- 옮기기 전 DB 를 SQLite backup API 로 복사하고, 되돌리기용 journal(id→원래 project)을 남긴다.
- 한국어 색인(ko_fts)의 project 열도 맞춘다. Chroma 메타데이터는 워커가 쓰는 중이라 건드리지 않는다
  (sb_recalld/형태소 회수는 SQLite·ko 색인만 쓴다).
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sb_config  # noqa: E402


def _ko_index():
    try:
        import sb_fts_ko
        return Path(sb_fts_ko._path())   # SB_KO_INDEX 우선
    except Exception:  # noqa: BLE001
        return Path(sb_config.sb_path('index', 'ko_fts.sqlite'))


def apply(mapping, dry_run=False, note=''):
    """mapping: {obs_id: new_project}. 반환: journal 경로."""
    db = Path(os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db())
    stamp = time.strftime('%Y%m%d-%H%M%S')
    logs = Path(sb_config.sb_path('logs', 'relabel'))
    logs.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(db), timeout=30)) as con:
        ids = list(mapping)
        before = {}
        for start in range(0, len(ids), 900):
            batch = ids[start:start + 900]
            q = 'SELECT id, project FROM observations WHERE id IN (%s)' % ','.join('?' * len(batch))
            before.update({r[0]: r[1] for r in con.execute(q, batch)})
        changes = {i: (before[i], p) for i, p in mapping.items() if i in before and before[i] != p}
        print('대상 %d건 중 변경 %d건 (없는 id %d)' % (len(mapping), len(changes), len(set(mapping) - set(before))))
        if dry_run or not changes:
            return None
        backup = db.with_name('%s.bak-%s-relabel%s' % (db.stem, stamp, db.suffix))
        with closing(sqlite3.connect(str(backup))) as dst:
            con.backup(dst)
        journal = logs / ('relabel-%s.json' % stamp)
        journal.write_text(json.dumps({'note': note, 'backup': str(backup),
                                       'changes': {str(i): {'from': a, 'to': b} for i, (a, b) in changes.items()}},
                                      ensure_ascii=False, indent=1), encoding='utf-8')
        with con:
            con.executemany('UPDATE observations SET project=? WHERE id=?', [(b, i) for i, (a, b) in changes.items()])
    _sync_ko({i: b for i, (a, b) in changes.items()})
    prune_backups(db)
    print('완료. 백업 %s · journal %s' % (backup, journal))
    return journal


def _sync_ko(new_projects):
    idx = _ko_index()
    if not idx.exists() or not new_projects:
        return
    try:
        import sb_fts_ko
        with closing(sb_fts_ko.open_index(create=True)) as conn, conn:   # create=False 는 읽기 전용
            for i, p in new_projects.items():
                conn.execute('UPDATE ko_docs SET project=? WHERE obs_id=?', (p, i))
                row = conn.execute('SELECT body FROM ko_fts WHERE obs_id=?', (i,)).fetchone()
                if row is None:
                    continue
                conn.execute('DELETE FROM ko_fts WHERE obs_id=?', (i,))
                conn.execute('INSERT INTO ko_fts(rowid, obs_id, project, body) VALUES (?,?,?,?)', (i, i, p, row[0]))
    except Exception as exc:  # noqa: BLE001
        print('ko 색인 갱신 실패(다음 전체 재색인 때 맞춰짐): %s' % exc, file=sys.stderr)


# 도구·환경 작업 기록 판별(자동 분리용). 2026-09-27 LLM 판정 251건 대비 정밀도 1.00 · 재현율 0.83 —
# 오분류가 없어야 자동으로 돌릴 수 있으므로 재현율보다 정밀도를 택했다. 애매한 건 업무에 남는다.
ENV_PATTERN = (r'\bsb (search|recall|timeline|save|relabel|state|loops)|sb_recall|sb_recalld|recall_gate|session_context|'
               r'secondbrain|세컨브레인|claude-mem|teamclaude|회수 훅|회상 게이트|recalld|DoE|관련도 판정|qrels|'
               r'test_\w+\.py|unittest|pytest|키트|kit\b|doctor|settings\.json 훅|훅 (타임아웃|등록)|chroma|임베딩|ko_fts|'
               r'Kiwi|PR #\d|이슈 #\d|\bsb\.py|sb_\w+\.py|\bsb command|insane-router|[Rr]ecall gate|Search API|SessionStart 훅|'
               # 보강: 실사용에서 업무 프로젝트에 남아 있던 도구·PC 환경 기록의 제목 어휘
               r'플러그인|plugin|PC 성능|성능 저하|시작 프로그램|Ollama|'
               r'훅 (경량화|이식|실행|구조)|[Hh]ook (파일|구조)|회수 서버|기록층 (점검|감사|복구)|automemory 동기화|야간 배치|'
               r'벡터 (동기화|백필)|behavior_eval|sbeval|평가 하네스|PII 마스킹|이름 마스킹|relabel|관찰기|팀클로드|[Tt]eam[Cc]laude|'
               r'sb health')
# 주의: 'PII'·'메모리 시스템'·'기록층' 단독은 업무 쪽 기록(데이터 동기화·회고)까지 잡아 빼서 넣지 않았다.
# PC 마다 다른 어휘(설치된 보안 프로그램 이름 등)는 relabel_auto.json 의 "extra_env_pattern" 에 둔다.

RELABEL_KEEP = int(os.environ.get('SB_RELABEL_KEEP', '5'))   # 재분류 DB 백업 보존 개수


def prune_backups(db: Path, keep: int = None) -> int:
    """재분류 백업(<db>.bak-*-relabel.db)을 최신 keep 개만 남긴다. 자동 분리가 매시간 돌면 DB 전체 사본이
    시간당 하나씩 쌓인다(실사용 10일에 31개·약 290MB). 반환: 지운 수."""
    keep = RELABEL_KEEP if keep is None else keep
    if keep <= 0:
        return 0
    olds = sorted(db.parent.glob('%s.bak-*-relabel%s' % (db.stem, db.suffix)), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for p in olds[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def auto_config():
    """~/.secondbrain/config/relabel_auto.json {"work_projects": [...], "to": "secondbrain-kit"} — 있을 때만 자동 분리."""
    p = Path(sb_config.sb_path('config', 'relabel_auto.json'))
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding='utf-8'))


def auto_env(work_projects, to, since_hours=72, dry_run=False, extra_pattern=None):
    """업무 프로젝트의 실시간 관찰 중 도구·환경 작업으로 판별된 것을 to 로 옮긴다. 반환: 옮긴 수."""
    import re
    pat = re.compile(ENV_PATTERN + ('|' + extra_pattern if extra_pattern else ''), re.I)
    db = Path(os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db())
    cutoff = int((time.time() - since_hours * 3600) * 1000)
    with closing(sqlite3.connect('file:%s?mode=ro' % db.as_posix(), uri=True, timeout=10)) as con:
        q = ('SELECT id, title, narrative, text, metadata FROM observations WHERE project IN (%s) '
             'AND created_at_epoch >= ?' % ','.join('?' * len(work_projects)))
        rows = con.execute(q, [*work_projects, cutoff]).fetchall()
    ids = []
    for oid, title, narrative, text, md in rows:
        md = md or ''
        if any(k in md for k in ('"kind":"import"', '"kind":"automemory-sync"', 'backfill')):
            continue   # 가져온 기록·메모리 동기화분은 사람이 쓴 업무 기록 — 건드리지 않는다
        if pat.search('%s %s' % (title or '', (narrative or text or '')[:400])):
            ids.append(oid)
    if not ids:
        return 0
    apply({i: to for i in ids}, dry_run=dry_run, note='auto-env')
    return len(ids)


def undo(journal_path):
    j = json.loads(Path(journal_path).read_text(encoding='utf-8'))
    mapping = {int(i): c['from'] for i, c in j['changes'].items()}
    return apply(mapping, note='undo of %s' % journal_path)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='sb relabel')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--ids-file', help='JSON: [id,...] 또는 {"id": "env"|"work"} (값이 env 인 것만)')
    g.add_argument('--undo')
    g.add_argument('--auto-env', action='store_true', help='설정(relabel_auto.json) 또는 --from/--to 로 도구 기록 자동 분리')
    ap.add_argument('--from', dest='src', help='--auto-env 대상 업무 project(쉼표 구분)')
    ap.add_argument('--since-hours', type=float, default=72)
    ap.add_argument('--to', help='옮길 project 이름')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--note', default='')
    a = ap.parse_args(argv)
    if a.undo:
        undo(a.undo)
        return 0
    if a.auto_env:
        cfg = auto_config() or {}
        src = a.src.split(',') if a.src else cfg.get('work_projects') or []
        to = a.to or cfg.get('to') or 'secondbrain-kit'
        if not src:
            ap.error('--from 또는 relabel_auto.json 의 work_projects 필요')
        print('자동 분리 %d건' % auto_env(src, to, a.since_hours, a.dry_run, cfg.get('extra_env_pattern')))
        return 0
    if not a.to:
        ap.error('--to 필요')
    data = json.loads(Path(a.ids_file).read_text(encoding='utf-8'))
    ids = [int(k) for k, v in data.items() if v == 'env'] if isinstance(data, dict) else [int(x) for x in data]
    apply({i: a.to for i in ids}, dry_run=a.dry_run, note=a.note)
    return 0


if __name__ == '__main__':
    sys.exit(main())
