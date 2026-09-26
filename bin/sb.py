#!/usr/bin/env python3
"""sb — secondbrain-kit 단일 진입점. 에이전트 규칙(CLAUDE.md/AGENTS.md)은 이 명령만 안내한다.

  sb search '<질의>' [--global] [--limit N] [--no-vector]   과거 경위 검색(L0)
  sb search --mode current|next|rules|history [--scope S]    확정 사실·미결·규칙(L1~L3)
  sb recall '<질의>' [--global] [--json]                     빠른 회수(상주 서버, ~0.3초) + 관련도 판정
  sb timeline --since YYYY-MM-DD [--until D] [--days N] [--global]  기간별 과거 작업(세션 날짜 기준)
  sb state propose|verify|accept|head|history ...             상태층(사실 채택)
  sb save --title T --text X [--project P]                    기억 1건 저장(claude-mem 관측)
  sb loops add|list|close|snooze|drop|reopen|set-action ...   미결
  sb scope [경로]                                             이 폴더의 scope 확인
  sb prompt-id '<발화 일부>'                                  상태층 검증용 user_prompts.id 찾기
  sb index                                                    한국어 색인 증분 갱신
  sb nightly [--with-consolidate]                             야간 배치 수동 실행
  sb consolidate ...                                          주간 통합 수동 실행
"""
import os
import runpy
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))

ROUTES = {
    'search': 'sb_search.py', 'state': 'sb_state.py', 'loops': 'loops.py',
    'nightly': 'nightly.py', 'consolidate': 'consolidate.py', 'audit': 'sb_audit.py',
    'automemory': 'sync_automemory.py', 'timeline': 'sb_timeline.py', 'relabel': 'sb_relabel.py',
}


def _save(argv):
    import argparse
    import json
    import sb_memory
    from sb_scope import resolve_scope_id
    ap = argparse.ArgumentParser(prog='sb save')
    ap.add_argument('--title', required=True)
    ap.add_argument('--text', required=True)
    ap.add_argument('--project', help='claude-mem project (기본: 현재 폴더 이름)')
    ap.add_argument('--kind', default='manual')
    a = ap.parse_args(argv)
    project = a.project or os.path.basename(os.path.normpath(os.getcwd()))
    prov = sb_memory.build_provenance(source='sb-cli', kind=a.kind, origin='sb save',
                                      created_by='agent', content_hash=sb_memory.content_hash(a.text, a.title))
    obs_id = sb_memory.save_memory(a.text, a.title, project, prov)
    print(json.dumps({'status': 'ok' if obs_id and obs_id > 0 else 'duplicate', 'id': obs_id,
                      'project': project, 'scope': resolve_scope_id(os.getcwd())[0]}, ensure_ascii=False))


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help', 'help'):
        print(__doc__)
        return 0
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == 'save':
        _save(argv)
        return 0
    if cmd == 'scope':
        import json
        from sb_scope import resolve_scope_id
        sid, method, real = resolve_scope_id(argv[0] if argv else os.getcwd())
        print(json.dumps({'scope_id': sid, 'method': method, 'path': real}, ensure_ascii=False))
        return 0
    if cmd == 'prompt-id':
        import json
        import sqlite3
        import sb_config
        if not argv:
            sys.stderr.write('usage: sb prompt-id "<발화 일부>"\n')
            return 2
        con = sqlite3.connect('file:%s?mode=ro' % Path(sb_config.claude_mem_db()).as_posix(), uri=True)
        rows = con.execute('SELECT id, created_at, substr(prompt_text,1,120) FROM user_prompts '
                           'WHERE prompt_text LIKE ? ORDER BY id DESC LIMIT 5', ('%' + argv[0] + '%',)).fetchall()
        print(json.dumps([{'id': r[0], 'created_at': r[1], 'text': r[2]} for r in rows], ensure_ascii=False))
        return 0
    if cmd == 'recall':   # 상주 서버 경유 빠른 회수 (sb_recalld.py query)
        sys.argv = [str(BIN / 'sb_recalld.py'), 'query'] + argv
        runpy.run_path(str(BIN / 'sb_recalld.py'), run_name='__main__')
        return 0
    if cmd == 'index':
        cmd, argv = 'sb_fts_ko.py', ['build'] + argv
        script = cmd
    else:
        script = ROUTES.get(cmd)
    if not script:
        sys.stderr.write('unknown command: %s\n' % cmd)
        return 2
    sys.argv = [str(BIN / script)] + argv
    runpy.run_path(str(BIN / script), run_name='__main__')
    return 0


if __name__ == '__main__':
    sys.exit(main())
