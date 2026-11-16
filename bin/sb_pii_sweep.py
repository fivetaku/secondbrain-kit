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
CHROMA_BACKUP_KEEP = int(os.environ.get('SB_PII_BACKUP_KEEP', '3'))   # 벡터 정리 전 Chroma 사본 보존 개수


def prune_chroma_backups(backups: Path, keep: int = None) -> int:
    """chroma-before-piisweep-* 사본을 최신 keep 개만 남긴다. 야간 pii 단계가 매일 Chroma 전체를 복사하므로
    제한이 없으면 디스크가 계속 찬다. 반환: 지운 수."""
    keep = CHROMA_BACKUP_KEEP if keep is None else keep
    if keep <= 0:
        return 0
    olds = sorted(backups.glob('chroma-before-piisweep-*'), key=lambda p: p.name, reverse=True)
    removed = 0
    for p in olds[keep:]:
        shutil.rmtree(p, ignore_errors=True)
        removed += 1
    return removed


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


# claude-mem 13.34+ 워커는 Chroma 폴더에 단일 쓰기 잠금 파일을 둔다(worker-service.cjs acquireChromaWriterLock,
# 15.18.3 도 같은 이름·필드). 다른 워커는 이 파일의 pid 가 살아 있으면 "already owned by PID" 로 연결을 거부한다.
WRITER_LOCK = '.claude-mem-chroma-writer.lock'
try:   # 워커가 잠금을 풀 때까지 기다리는 최대 초. 잘못된 값이면 기본값 — import 가 깨지면 recalld 의 SQLite 마스킹까지 멈춘다
    LOCK_WAIT = float(os.environ.get('SB_PII_CHROMA_WAIT', '30'))
except ValueError:
    LOCK_WAIT = 30.0


class ChromaBusy(RuntimeError):
    """살아 있는 다른 프로세스가 Chroma 쓰기 잠금을 쥐고 있어 원본을 열지 않았다."""


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5   # 권한 거부 = 프로세스는 있다
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _proc_name(raw: str) -> str:
    """claude-mem 의 프로세스 이름 정규화와 같게: 마지막 경로 조각(/·\\ 모두), .exe 제거(대소문자 무시), 소문자."""
    base = raw.strip().replace('\\', '/').rsplit('/', 1)[-1]
    if base.lower().endswith('.exe'):
        base = base[:-4]
    return base.lower()


def _self_process_name() -> str:
    """claude-mem 이 이 프로세스를 볼 때 얻는 이름. 잠금의 processName 이 이것과 다르면 claude-mem 이 낡은 잠금으로 보고 지운다.
    sys.executable 의 실제 파일 이름(python3.12)은 macOS ps(python)·Linux comm(실행한 링크 이름)과 다를 수 있어
    claude-mem 과 같은 출처를 읽는다 — Linux /proc/self/comm, macOS ps -o comm=, Windows 실행 파일 이름(Get-Process 와 같음)."""
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(k32.GetCurrentProcess(), 0, buf, ctypes.byref(size)):
                return _proc_name(buf.value)
        except Exception:  # noqa: BLE001
            pass
        return _proc_name(sys.executable)
    try:
        return _proc_name(Path('/proc/self/comm').read_text(encoding='utf-8'))
    except OSError:
        pass
    try:
        r = subprocess.run(['ps', '-p', str(os.getpid()), '-o', 'comm='], capture_output=True, text=True, timeout=2)
        if r.returncode == 0 and r.stdout.strip():
            return _proc_name(r.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return _proc_name(sys.executable)


def claim_writer_lock(chroma: Path, wait: float = None):
    """워커가 잠금을 풀(또는 죽을) 때까지 기다린 뒤, 키트가 원본을 여는 동안 같은 잠금을 쥔다.
    그동안 훅이 새 워커를 띄워도 그 워커의 chroma-mcp 가 같은 폴더를 함께 쓰지 않는다(잠금을 보고 물러나 10초마다 재시도).
    pid·이름만 적고 startToken 은 비워 둔다 — claude-mem 은 그때 프로세스 이름으로 살아 있는지 본다
    (이름이 어긋나면 claude-mem 이 이 잠금을 낡은 것으로 보고 지운다 — 그때는 예전 동작과 같다).
    키트가 도중에 죽어 잠금이 남아도 pid 가 죽었으므로 claude-mem 이 지운다.
    반환: 잠금 파일 경로(해제는 release_writer_lock). 살아 있는 다른 프로세스가 계속 쥐고 있으면 ChromaBusy."""
    wait = LOCK_WAIT if wait is None else wait
    path = Path(chroma) / WRITER_LOCK
    record = {'pid': os.getpid(), 'ownerId': 'secondbrain-kit:%d:%d' % (os.getpid(), int(time.time() * 1000)),
              'dataDir': str(Path(chroma).resolve()), 'acquiredAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'processName': _self_process_name()}
    deadline = time.time() + max(0.0, wait)
    owner = None
    while True:
        try:
            with open(path, 'x', encoding='utf-8') as f:
                json.dump(record, f)
            return path
        except FileExistsError:
            pass
        try:
            owner = json.loads(path.read_text(encoding='utf-8')).get('pid')
        except (OSError, ValueError, AttributeError):
            owner = None   # 쓰는 중이거나 깨진 파일 — 잠깐 뒤 다시 본다
        if owner is not None and not _pid_alive(owner):
            try:   # 죽은 워커가 남긴 잠금(taskkill /F 는 정리할 틈이 없다) — claude-mem 도 같은 규칙으로 지운다
                path.unlink()
            except OSError:
                pass
            continue
        if time.time() >= deadline:
            raise ChromaBusy('Chroma 쓰기 잠금을 PID %s 가 쥐고 있음 — 벡터 정리를 건너뜀' % owner)
        time.sleep(0.5)


def release_writer_lock(path) -> None:
    """claim_writer_lock 이 만든 잠금만 지운다(그 사이 다른 프로세스가 바꿔 썼으면 그대로 둔다)."""
    if not path:
        return
    try:
        if json.loads(Path(path).read_text(encoding='utf-8')).get('pid') == os.getpid():
            Path(path).unlink()
    except (OSError, ValueError, AttributeError):
        pass


def sweep_chroma(mask, dry_run=False):
    """워커를 멈추고 벡터 문서를 정리한다. 반환: (삭제 문서 수, 고아 수, 보류 등록 수).
    워커가 쓰기 잠금을 풀지 않으면 원본을 열지 않고 ChromaBusy 를 낸다(두 프로세스가 같은 폴더에 쓰지 않게)."""
    cm = sb_config.claude_mem_dir()
    chroma = Path(os.environ.get('SB_CHROMA_PATH') or cm / 'chroma')
    with closing(sqlite3.connect('file:%s?mode=ro' % Path(_db()).as_posix(), uri=True)) as db:
        alive = {DOC_TYPE[k]: {r[0] for r in db.execute('SELECT id FROM %s' % t)} for k, (t, _) in TABLES.items()}
    lock = None
    if not dry_run:
        stamp = time.strftime('%Y%m%d-%H%M%S')
        (cm / 'backups').mkdir(exist_ok=True)
        shutil.copytree(chroma, cm / 'backups' / ('chroma-before-piisweep-' + stamp))
        prune_chroma_backups(cm / 'backups')
        try:   # 워커가 Chroma 를 쥐고 있으므로 멈춘다 — 다음 세션 훅이 다시 띄우고 보류분을 재임베딩한다
            wp = json.loads((cm / 'worker.pid').read_text(encoding='utf-8'))
            pid = wp.get('pid') if isinstance(wp, dict) else int(wp)
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
            else:
                os.kill(int(pid), 15)
        except Exception:  # noqa: BLE001
            pass
        # SIGTERM 뒤 정상 종료는 chroma-mcp 를 멈추고 잠금을 푸는 데 몇 초 걸린다(15.18.3 정상 종료 기한 10초).
        # 예전처럼 2초만 기다리고 열면 아직 살아 있는 chroma-mcp 와 함께 쓰게 된다.
        lock = claim_writer_lock(chroma)
    try:
        return _sweep_open(chroma, mask, alive, dry_run, cm)
    finally:
        release_writer_lock(lock)


def _sweep_open(chroma, mask, alive, dry_run, cm):
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
            try:
                deleted, orphans, pending = sweep_chroma(mask, a.dry_run)
                out['chroma'] = {'deleted': deleted, 'orphans': orphans, 'pending': pending}
            except ChromaBusy as e:   # SQLite 마스킹은 끝났다 — 벡터 정리만 다음 밤으로 미룬다
                out['chroma'] = {'skipped': str(e)}
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
