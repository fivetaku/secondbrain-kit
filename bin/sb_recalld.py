#!/usr/bin/env python3
"""sb_recalld — 회수용 상주 검색 서버(로컬 전용).

왜: 회수 훅은 프롬프트마다 새 파이썬 프로세스로 뜬다. 한국어 형태소 분석기(Kiwi) 적재 ~2초 +
    색인 동기화 ~3초라 매번 직접 검색하면 프롬프트가 4~6초 늦어진다. 이 서버가 Kiwi·색인을 메모리에
    올려 두고 훅은 HTTP 로 ~20ms 에 묻는다(claude-mem 워커와 같은 구조).

평가 근거(eval/retrieval_eval.py, 2026-09-26, 질의 150·판정 1,820쌍):
  - 백엔드: 한국어 형태소 색인(명사만) nDCG@5 0.78 > claude-mem 워커 0.62 > 벡터 0.55
  - 주입 문턱: 질의 명사가 상위 문서에 3개 이상 일치, 또는 2개 이상 + 커버리지 60% 이상
    → 관련 결과 유지 98%, 무관 질문 주입률 100% → 30%

  GET /health                         {"ok":true,"max_id":…}
  GET /recall?q=…&project=…&limit=5   {"items":[…], "gate":{"matched","coverage","pass"}}

  python bin/sb_recalld.py serve [--port 37791] [--idle-hours 6]
  python bin/sb_recalld.py ensure     # 떠 있지 않으면 백그라운드로 띄운다
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sb_config  # noqa: E402

PORT = int(os.environ.get('SB_RECALLD_PORT', '37791'))
DB = ''
SYNC_EVERY = 20.0          # 색인 동기화 최소 간격(초)
FRESH_MINUTES = 20         # 이보다 새 실시간 관찰은 회상 대상에서 뺀다(훅의 enrich 와 같은 기준)
GATE_MATCHED, GATE_MATCHED_LOW, GATE_COVERAGE = 3, 2, 0.6


def gate_pass(matched: int, coverage: float) -> bool:
    """주입할 만큼 관련 있는가 — 질의 명사가 1위 문서에 3개 이상, 또는 2개 이상이면서 커버리지 60% 이상."""
    return matched >= GATE_MATCHED or (matched >= GATE_MATCHED_LOW and coverage >= GATE_COVERAGE)


def base_url(port=None):
    return 'http://127.0.0.1:%d' % (port or PORT)


def is_up(port=None, timeout=0.5):
    try:
        with urllib.request.urlopen(base_url(port) + '/health', timeout=timeout) as r:
            return json.loads(r.read().decode()).get('ok') is True
    except Exception:  # noqa: BLE001
        return False


def db_path():
    return str(Path(os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db()).resolve())


def ensure_running(port=None):
    """떠 있으면 True. 아니면 분리된 백그라운드 프로세스로 띄우고 False(이번 호출은 기다리지 않는다).
    DB·색인 경로를 환경변수로 바꾼 환경(테스트·임시 실행)에서는 자동으로 띄우지 않는다 —
    임시 DB 를 보는 서버가 공용 포트를 차지하는 사고를 막는다."""
    if is_up(port):
        return True
    if os.environ.get('SB_RECALLD_AUTOSTART', '1') == '0' or os.environ.get('SB_CLAUDE_MEM_DB') or os.environ.get('SB_KO_INDEX'):
        return False
    python = sys.executable
    if os.name == 'nt':   # 콘솔 창 없이
        pyw = Path(python).with_name('pythonw.exe')
        python = str(pyw) if pyw.exists() else python
    cmd = [python, str(Path(__file__).resolve()), 'serve', '--port', str(port or PORT)]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                  env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    if os.name == 'nt':
        kwargs['creationflags'] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except Exception:  # noqa: BLE001
        pass
    return False


class Engine:
    def __init__(self):
        import sb_fts_ko
        self.ko = sb_fts_ko
        self.ko._tokenizer()
        self.lock = threading.Lock()
        self.last_sync = 0.0
        self.sync(force=True)

    def sync(self, force=False):
        now = time.time()
        if force or now - self.last_sync >= SYNC_EVERY:
            with self.lock:
                if force or now - self.last_sync >= SYNC_EVERY:
                    try:
                        self.ko.ensure_current()
                    except Exception:  # noqa: BLE001
                        pass
                    self.last_sync = time.time()

    def nouns(self, text):
        ident = [w.lower() for w in re.findall(r'[A-Za-z0-9_]+(?:[._/-][A-Za-z0-9_]+)*', text)]
        kiwi = self.ko._kiwi
        if kiwi is None:
            return list(dict.fromkeys(self.ko.ko_query_tokens(text)))
        toks = ident + [t.form.lower() for t in kiwi.tokenize(text)
                        if t.tag.startswith('NN') or t.tag in {'SL', 'SN', 'SH'}]
        return list(dict.fromkeys(toks))

    def _search(self, conn, match, project, limit):
        sql = 'SELECT obs_id, bm25(ko_fts), project, body FROM ko_fts WHERE ko_fts MATCH ?'
        params = [match]
        if project:
            sql += ' AND project = ?'
            params.append(project)
        sql += ' ORDER BY bm25(ko_fts), obs_id LIMIT ?'
        return conn.execute(sql, [*params, limit]).fetchall()

    def recall(self, query, project, limit):
        self.sync()
        toks = self.nouns(query)
        if not toks:
            return {'items': [], 'gate': {'matched': 0, 'coverage': 0.0, 'pass': False}, 'tokens': []}
        match = ' OR '.join('"' + t.replace('"', '""') + '"' for t in toks)
        with closing(self.ko.open_index(create=False)) as conn:
            rows = self._search(conn, match, project, limit * 2) if project else []
            seen = {r[0] for r in rows}
            rows += [r for r in self._search(conn, match, None, limit * 2) if r[0] not in seen]
        meta = self._meta([r[0] for r in rows])
        # 판정 전에 거른다: 방금(20분 내) 생긴 실시간 관찰은 '회상'이 아니라 진행 중인 작업이다.
        # 판정 뒤에 거르면 판정은 그 기록으로 통과하고 주입은 무관한 기록이 되는 불일치가 생긴다(DOE S9).
        cutoff = (time.time() - FRESH_MINUTES * 60) * 1000
        rows = [r for r in rows if not (meta.get(r[0], {}).get('live') and meta[r[0]].get('epoch', 0) > cutoff)]
        rows = rows[:limit]
        matched, cov = 0, 0.0
        if rows:
            body = set((rows[0][3] or '').lower().split())
            matched = sum(1 for t in toks if t in body)
            cov = matched / len(toks)
        gate = gate_pass(matched, cov)
        items = []
        for oid, score, proj, _body in rows:
            m = meta.get(oid, {})
            items.append({'id': oid, 'score': round(-score, 3), 'project': proj or m.get('project', ''),
                          'title': m.get('title', ''), 'date': m.get('date', '')})
        return {'items': items, 'gate': {'matched': matched, 'coverage': round(cov, 2), 'pass': gate}, 'tokens': toks}

    @staticmethod
    def _meta(ids):
        if not ids:
            return {}
        con = sqlite3.connect('file:%s?mode=ro' % Path(os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db()).as_posix(), uri=True, timeout=2)
        out = {}
        with closing(con):
            q = ('SELECT id, project, title, created_at, metadata, created_at_epoch FROM observations WHERE id IN (%s)'
                 % ','.join('?' * len(ids)))
            for oid, proj, title, created, md, epoch in con.execute(q, ids):
                sd = re.search(r'"session_date":"(\d{4}-\d{2}-\d{2})"', md or '')
                td = re.match(r'\[(\d{4}-\d{2}-\d{2})', title or '')
                imported = any(k in (md or '') for k in ('"kind":"import"', '"kind":"automemory-sync"', 'backfill'))
                out[oid] = {'project': proj, 'title': title or '', 'epoch': epoch or 0, 'live': not imported,
                            'date': (sd or td).group(1) if (sd or td) else (created or '')[:10]}
        return out


def serve(port, idle_hours):
    global DB
    DB = db_path()
    engine = Engine()
    state = {'last': time.time()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401 — 조용히
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state['last'] = time.time()
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            if url.path == '/health':
                return self._send(200, {'ok': True, 'pid': os.getpid(), 'db': DB})
            if url.path == '/recall':
                try:
                    q = (qs.get('q') or [''])[0][:500]
                    project = (qs.get('project') or [''])[0]
                    limit = max(1, min(20, int((qs.get('limit') or ['5'])[0])))
                    return self._send(200, dict(engine.recall(q, project, limit), db=DB))
                except Exception as exc:  # noqa: BLE001
                    return self._send(500, {'error': str(exc)[:300]})
            return self._send(404, {'error': 'not found'})

    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError:
        return 0   # 이미 떠 있음(포트 사용 중)
    run = Path(sb_config.sb_path('run'))
    run.mkdir(parents=True, exist_ok=True)
    (run / 'recalld.json').write_text(json.dumps({'pid': os.getpid(), 'port': port, 'started': time.time()}), encoding='utf-8')

    def reaper():
        while True:
            time.sleep(60)
            if time.time() - state['last'] > idle_hours * 3600:
                httpd.shutdown()
                return
    threading.Thread(target=reaper, daemon=True).start()
    httpd.serve_forever()
    return 0


def query(text, project, limit, as_json, port=None):
    """sb recall — 상주 서버로 0.1초대 조회(없으면 띄우고 최대 15초 기다린다)."""
    if not ensure_running(port):
        deadline = time.time() + 15
        while time.time() < deadline and not is_up(port):
            time.sleep(0.5)
    url = base_url(port) + '/recall?' + urllib.parse.urlencode({'q': text, 'project': project, 'limit': str(limit)})
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.loads(r.read().decode('utf-8'))
    if as_json:
        print(json.dumps(d, ensure_ascii=False))
        return 0
    g = d['gate']
    print('[recall] project=%s 일치 명사 %d개·커버리지 %.0f%% → %s  (본문: get_observations([ID]) / 기간: sb timeline)'
          % (project or 'ALL', g['matched'], g['coverage'] * 100, '관련 있음' if g['pass'] else '약함(표현을 바꿔 다시)'))
    for it in d['items']:
        print('- #%d %s %s · %s' % (it['id'], it['date'], it['project'], it['title'][:110]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('serve', 'ensure', 'status', 'query'))
    ap.add_argument('text', nargs='?', default='')
    ap.add_argument('--project', help='기본: 현재 폴더 이름(claude-mem project)')
    ap.add_argument('--global', dest='glob', action='store_true')
    ap.add_argument('--limit', type=int, default=8)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--idle-hours', type=float, default=float(os.environ.get('SB_RECALLD_IDLE_HOURS', '6')))
    a = ap.parse_args()
    if a.cmd == 'query':
        project = '' if a.glob else (a.project or os.path.basename(os.path.normpath(os.getcwd())))
        return query(a.text, project, a.limit, a.json, a.port)
    if a.cmd == 'serve':
        return serve(a.port, a.idle_hours)
    if a.cmd == 'ensure':
        up = ensure_running(a.port)
        print('up' if up else 'starting')
        return 0
    print('up' if is_up(a.port) else 'down')
    return 0


if __name__ == '__main__':
    sys.exit(main())
