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


def undo(journal_path):
    j = json.loads(Path(journal_path).read_text(encoding='utf-8'))
    mapping = {int(i): c['from'] for i, c in j['changes'].items()}
    return apply(mapping, note='undo of %s' % journal_path)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='sb relabel')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--ids-file', help='JSON: [id,...] 또는 {"id": "env"|"work"} (값이 env 인 것만)')
    g.add_argument('--undo')
    ap.add_argument('--to', help='옮길 project 이름')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--note', default='')
    a = ap.parse_args(argv)
    if a.undo:
        undo(a.undo)
        return 0
    if not a.to:
        ap.error('--to 필요')
    data = json.loads(Path(a.ids_file).read_text(encoding='utf-8'))
    ids = [int(k) for k, v in data.items() if v == 'env'] if isinstance(data, dict) else [int(x) for x in data]
    apply({i: a.to for i in ids}, dry_run=a.dry_run, note=a.note)
    return 0


if __name__ == '__main__':
    sys.exit(main())
