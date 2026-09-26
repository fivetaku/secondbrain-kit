#!/usr/bin/env python3
"""sb timeline — 기간으로 과거 작업 꺼내기 ("이번달에 한 것", "지난주", "9/10쯤").

claude-mem 의 created_at 은 "적재된 시각"이라 소급 적재(session-backfill)분은 전부 적재일로 몰린다.
그래서 날짜는 metadata.extra.session_date(실제 세션 날짜) → 제목의 [YYYY-MM-DD] → created_at 순으로 정한다.

  sb timeline --since 2026-09-01 [--until 2026-09-30] [--project P | --global] [--limit N] [--json]
  sb timeline --days 7            최근 7일
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sb_config  # noqa: E402

TITLE_DATE = re.compile(r'^\[(\d{4}-\d{2}-\d{2})')


def work_date(title, created_at, metadata):
    try:
        d = ((json.loads(metadata or '{}').get('extra') or {}).get('session_date') or '')
        if re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            return d
    except Exception:
        pass
    m = TITLE_DATE.match(title or '')
    if m:
        return m.group(1)
    return (created_at or '')[:10]


def clean_title(title):
    m = re.match(r'^\[\d{4}-\d{2}-\d{2}([^\]]*)\]\s*', title or '')
    if not m:
        return title or ''
    tag = m.group(1).strip(' ·')
    return ('[%s] ' % tag if tag else '') + (title or '')[m.end():]


def main(argv=None):
    ap = argparse.ArgumentParser(prog='sb timeline')
    ap.add_argument('--since')
    ap.add_argument('--until')
    ap.add_argument('--days', type=int)
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--project', help='claude-mem project (기본: 현재 폴더 이름)')
    g.add_argument('--global', dest='glob', action='store_true')
    ap.add_argument('--limit', type=int, default=300)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--with-memory', action='store_true',
                    help='[automemory] 동기화분(날짜 없는 상태 스냅샷)도 포함')
    a = ap.parse_args(argv)

    today = dt.date.today()
    since = a.since or ((today - dt.timedelta(days=a.days)).isoformat() if a.days else
                        today.replace(day=1).isoformat())
    until = a.until or today.isoformat()
    project = None if a.glob else (a.project or os.path.basename(os.path.normpath(os.getcwd())))

    con = sqlite3.connect('file:%s?mode=ro' % Path(sb_config.claude_mem_db()).as_posix(), uri=True, timeout=2)
    sql = 'SELECT id, project, type, title, created_at, metadata FROM observations'
    args = []
    if project:
        sql += ' WHERE project = ? OR merged_into_project = ?'
        args = [project, project]
    rows = []
    for oid, proj, typ, title, created, meta in con.execute(sql, args):
        if not a.with_memory and (title or '').startswith('[automemory]'):
            continue
        d = work_date(title, created, meta)
        if since <= d <= until:
            rows.append({'date': d, 'id': oid, 'project': proj, 'type': typ,
                         'title': clean_title(title)})
    rows.sort(key=lambda r: (r['date'], r['id']))
    rows = rows[-a.limit:]

    if a.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    print('[timeline] %s ~ %s  project=%s  items=%d  (본문: get_observations([ID]))'
          % (since, until, project or 'ALL', len(rows)))
    cur = None
    for r in rows:
        if r['date'] != cur:
            cur = r['date']
            print('\n## ' + cur)
        tail = '' if project else '  (%s)' % r['project']
        print('- #%d %s%s' % (r['id'], r['title'][:110], tail))
    return 0


if __name__ == '__main__':
    sys.exit(main())
