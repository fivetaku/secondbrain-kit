#!/usr/bin/env python3
"""sb pii-sweep — 저장된 기록에서 개인정보를 가린다(선택형 마스킹 모듈이 있을 때만).

왜: `sb save` 등 키트 저장 경로는 sb_memory 에서 가리지만, claude-mem 실시간 관찰기는 대화에 나온 이름을
    그대로 기록한다(2026-09-27 확인). 그래서 저장 뒤에 주기적으로 훑어서 가린다.

  sb pii-sweep                SQLite(관찰·세션 요약·프롬프트) 마스킹 — 상주 서버가 1시간마다 실행
  sb pii-sweep --chroma       + 벡터 문서 정리: 워커를 멈추고 마스킹이 필요한 문서·고아 문서를 지운 뒤
                              재임베딩 보류 목록에 올린다(다음 세션 시작 때 워커가 가려진 본문으로 다시 임베딩).
                              워커를 멈추므로 야간(nightly)에만 돌린다.
  --dry-run                   바꿀 건수만

마스킹 모듈: $SB_HOME/local/pii_mask.py 또는 SB_PII_MASK (mask(str)->str). 없으면 아무것도 하지 않는다.
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sb_config  # noqa: E402
import sb_memory  # noqa: E402

TABLES = {
    'observations': ('observations', ['title', 'subtitle', 'narrative', 'text', 'facts', 'concepts']),
    'summaries': ('session_summaries', ['request', 'investigated', 'learned', 'completed', 'next_steps', 'notes']),
    'prompts': ('user_prompts', ['prompt_text']),
}
DOC_TYPE = {'observations': 'observation', 'summaries': 'session_summary', 'prompts': 'user_prompt'}


def _db():
    return os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db()


def sweep_sqlite(mask, dry_run=False):
    changed = {k: [] for k in TABLES}
    with closing(sqlite3.connect(_db(), timeout=30)) as db:
        with db:
            for kind, (tbl, cols) in TABLES.items():
                for row in db.execute('SELECT id, %s FROM %s' % (','.join(cols), tbl)).fetchall():
                    new = [mask(v) if isinstance(v, str) else v for v in row[1:]]
                    if new != list(row[1:]):
                        changed[kind].append(row[0])
                        if not dry_run:
                            db.execute('UPDATE %s SET %s WHERE id=?' % (tbl, ','.join(c + '=?' for c in cols)),
                                       (*new, row[0]))
    return changed


def _projects(kind_ids):
    out = {}
    with closing(sqlite3.connect('file:%s?mode=ro' % Path(_db()).as_posix(), uri=True)) as db:
        for kind, ids in kind_ids.items():
            for rid in ids:
                if kind == 'prompts':
                    r = db.execute('SELECT s.project FROM user_prompts p LEFT JOIN sdk_sessions s '
                                   'ON s.id=p.session_db_id WHERE p.id=?', (rid,)).fetchone()
                else:
                    r = db.execute('SELECT project FROM %s WHERE id=?' % TABLES[kind][0], (rid,)).fetchone()
                if r and r[0]:
                    out.setdefault(kind, {})[rid] = r[0]
    return out


def sweep_chroma(mask, dry_run=False):
    """워커를 멈추고 벡터 문서를 정리한다. 반환: (삭제 문서 수, 고아 수, 보류 등록 수)."""
    cm = sb_config.claude_mem_dir()
    chroma = Path(os.environ.get('SB_CHROMA_PATH') or cm / 'chroma')
    with closing(sqlite3.connect('file:%s?mode=ro' % Path(_db()).as_posix(), uri=True)) as db:
        alive = {DOC_TYPE[k]: {r[0] for r in db.execute('SELECT id FROM %s' % t)} for k, (t, _) in TABLES.items()}
    if not dry_run:
        stamp = time.strftime('%Y%m%d-%H%M%S')
        (cm / 'backups').mkdir(exist_ok=True)
        shutil.copytree(chroma, cm / 'backups' / ('chroma-before-piisweep-' + stamp))
        try:   # 워커가 Chroma 를 쥐고 있으므로 멈춘다 — 다음 세션 훅이 다시 띄우고 보류분을 재임베딩한다
            wp = json.loads((cm / 'worker.pid').read_text(encoding='utf-8'))
            pid = wp.get('pid') if isinstance(wp, dict) else int(wp)
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
            else:
                os.kill(int(pid), 15)
            time.sleep(2)
        except Exception:  # noqa: BLE001
            pass
    os.environ.setdefault('CHROMA_OPENAI_API_KEY', 'ollama')
    import chromadb
    from chromadb.config import Settings
    import sb_embedding
    sb_embedding.register()
    client = chromadb.PersistentClient(path=str(chroma), settings=Settings(anonymized_telemetry=False))
    try:
        col = client.get_collection('cm__claude-mem')
        allg = col.get(include=['documents', 'metadatas'], limit=500000)
        leaked, orphans = {}, []
        kind_of = {v: k for k, v in DOC_TYPE.items()}
        for cid, doc, meta in zip(allg['ids'], allg['documents'], allg['metadatas']):
            meta = meta or {}
            dt, sid = meta.get('doc_type'), meta.get('sqlite_id')
            if dt in alive and sid not in alive[dt]:
                orphans.append(cid)
            elif dt in kind_of and doc and mask(doc) != doc:
                leaked.setdefault(kind_of[dt], set()).add(sid)
        to_delete = list(orphans)
        for kind, ids in leaked.items():
            got = col.get(where={'$and': [{'sqlite_id': {'$in': sorted(ids)}}, {'doc_type': DOC_TYPE[kind]}]}, include=[])
            to_delete += got['ids']
        if to_delete and not dry_run:
            for i in range(0, len(to_delete), 500):
                col.delete(ids=to_delete[i:i + 500])
    finally:
        if hasattr(client, 'close'):
            client.close()
    pend = _projects({k: sorted(v) for k, v in leaked.items()})
    n_pending = sum(len(v) for v in pend.values())
    if n_pending and not dry_run:
        sp = cm / 'chroma-sync-state.json'
        st = json.loads(sp.read_text(encoding='utf-8')) if sp.exists() else {}
        for kind, mp in pend.items():
            for rid, proj in mp.items():
                ent = st.setdefault(proj, {'observations': 0, 'summaries': 0, 'prompts': 0})
                p = ent.setdefault('pending', {})
                p[kind] = sorted(set(p.get(kind, [])) | {int(rid)})
        sp.write_text(json.dumps(st, indent=2), encoding='utf-8')
    return len(to_delete) - len(orphans), len(orphans), n_pending


def main(argv=None):
    ap = argparse.ArgumentParser(prog='sb pii-sweep')
    ap.add_argument('--chroma', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    mask = sb_memory._pii_masker()
    if mask is None:
        out = {'status': 'skipped', 'reason': 'no masking module'}
    else:
        changed = sweep_sqlite(mask, a.dry_run)
        out = {'status': 'ok', 'sqlite': {k: len(v) for k, v in changed.items()}}
        if a.chroma:
            deleted, orphans, pending = sweep_chroma(mask, a.dry_run)
            out['chroma'] = {'deleted': deleted, 'orphans': orphans, 'pending': pending}
        if any(changed.values()) and not a.dry_run:
            try:   # 형태소 색인도 가려진 본문으로
                import sb_fts_ko
                sb_fts_ko.build(full=True)
            except Exception:  # noqa: BLE001
                pass
    print(json.dumps(out, ensure_ascii=False) if a.json else out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
