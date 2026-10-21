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
# 색인 동기화 최소 간격(초). 동기화는 원장 전수 대조라 기록 수에 비례해 느려지므로 요청 경로 밖(백그라운드)에서 돈다.
SYNC_EVERY = float(os.environ.get('SB_RECALLD_SYNC_EVERY', '20'))
FRESH_MINUTES = 20         # 이보다 새 실시간 관찰은 회상 대상에서 뺀다(훅의 enrich 와 같은 기준)
GATE_MATCHED, GATE_MATCHED_LOW, GATE_COVERAGE = 3, 2, 0.6
RARE_DF = 0.10             # 이 비율 미만의 기록에만 나오는 단어를 '드문 단어'로 본다
# 작업 지시·진행 질문에 흔한 일반 명사 — 판정에서 세지 않는다("원인 파악해서 수정해", "남은 이슈 개선해줘").
# DOE 재검증: 관련 유지 0.99·모호 0.96 그대로, 진행·지시 질문 주입 4/12 → 0/12, 무관 주입 0.33 → 0.27.
TASK_WORDS = frozenset('원인 파악 수정 확인 정리 진행 작업 문제 이슈 개선 상태 결과 내용 부분 방법 지금 다음 단계 남 뭐 '
                       '것 거 이거 그거 저거 해결 처리 검토 보고 테스트 다시 전체 관련 사항 요청 필요'.split())


def gate_pass(matched: int, coverage: float, rare: int = 1) -> bool:
    """주입할 만큼 관련 있는가 — 질의 명사가 1위 문서에 3개 이상, 또는 2개 이상이면서 커버리지 60% 이상.
    그리고 겹친 단어 중 드문 단어(전체 기록의 10% 미만에 나옴)가 1개 이상 — '진행·상태·이슈·개선'처럼
    흔한 단어만 겹친 진행 질문("남은 이슈 개선해줘")을 거른다(DOE: 관련 유지 98% 그대로, 진행 질문 주입 5→3/10)."""
    return rare >= 1 and (matched >= GATE_MATCHED or (matched >= GATE_MATCHED_LOW and coverage >= GATE_COVERAGE))


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


# 하이브리드(2026-10-20): 키워드 후보 + 벡터 후보를 점수 결합한 뒤 한국어 재순위기로 섞어 고친다.
# 실사용 프롬프트 200개(정답=그 프롬프트에서 기록된 관측) 평가 근거는 secondbrain RESEARCH/hybrid-search-quality_20261020_1600.
# 주입 여부 판정(gate)은 그대로 키워드 1위 문서로 한다 — DOE로 맞춘 주입률을 바꾸지 않기 위해서다.
HYBRID = os.environ.get('SB_RECALLD_HYBRID', '1') == '1'
CANDIDATES = 30            # 결합·재순위 후보 수
FUSE_NORM = os.environ.get('SB_RECALLD_FUSE_NORM', 'minmax')   # minmax | zero
LEX_WEIGHT = 0.3           # 점수 결합에서 키워드 몫(나머지는 벡터). 실사용 질의에서 0.2~0.3이 최고였다
VEC_REFRESH = float(os.environ.get('SB_RECALLD_VEC_REFRESH', '1800'))   # 벡터 스냅샷 갱신 간격(초)
RERANK_MODEL = os.environ.get('SB_RERANK_MODEL', 'nlpai-lab/KURE-Reranker-nano')


class Vectors:
    """상주 벡터 검색. 전용 스냅샷(index/chroma-recalld)을 열어 둔 채 질의마다 0.1초대로 묻는다.
    원본은 claude-mem 워커가 쓰는 중이라 직접 열지 않는다. 갱신 중(수 초)에는 None 을 돌려 키워드만 쓰게 한다.
    doc_type 은 where 로 걸지 않고 사후에 거른다 — Chroma 1.5.9 로컬은 where 일치 ID 를 전부 꺼내
    필터를 만들어(chroma #4089) 전체의 88%에 걸리는 이 필터가 질의당 2~12초였다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.col = self.client = self.stack = None
        self.opened = 0.0
        self.error = None
        threading.Thread(target=self._open, daemon=True).start()

    def _open(self):
        import contextlib
        with self.lock:
            self._close()
            try:
                os.environ['SB_CHROMA_SNAPSHOT'] = sb_config.sb_path('index', 'chroma-recalld')
                os.environ['SB_CHROMA_SNAPSHOT_TTL'] = '0'   # 열 때마다 원본 기준으로 새로 뜬다
                os.environ.setdefault('CHROMA_OPENAI_API_KEY', 'ollama')
                import importlib
                import chromadb
                from chromadb.config import Settings
                import sb_search
                importlib.import_module('sb_embedding').register()
                stack = contextlib.ExitStack()
                source = Path(os.environ.get('SB_CHROMA_PATH') or (sb_config.claude_mem_dir() / 'chroma'))
                snap = stack.enter_context(sb_search._vector_snapshot(source))
                client = chromadb.PersistentClient(path=snap, settings=Settings(anonymized_telemetry=False))
                col = client.get_collection('cm__claude-mem')
                col.query(query_texts=['warmup'], n_results=1)   # 모델·HNSW 적재
                self.stack, self.client, self.col = stack, client, col
                self.opened, self.error = time.time(), None
            except Exception as exc:  # noqa: BLE001 — 벡터는 선택 기능: 실패하면 키워드만 쓴다
                self.error = str(exc)[:300]
                self._close()
                self.opened = time.time()

    def _close(self):
        for close in (getattr(self.client, 'close', None), getattr(self.stack, 'close', None)):
            try:
                if close:
                    close()
            except Exception:  # noqa: BLE001
                pass
        self.col = self.client = self.stack = None

    def search(self, query, n):
        if time.time() - self.opened > VEC_REFRESH and not self.lock.locked():
            threading.Thread(target=self._open, daemon=True).start()
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if self.col is None:
                return None
            r = self.col.query(query_texts=[query], n_results=n * 9, include=['metadatas', 'distances'])
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)[:300]
            return None
        finally:
            self.lock.release()
        best = {}
        for md, d in zip((r.get('metadatas') or [[]])[0], (r.get('distances') or [[]])[0]):
            oid = (md or {}).get('sqlite_id')
            # bool 은 int 의 하위형이라 True 가 1번 관측으로 섞인다 — sb_search.vector_search 와 같은 이유로 뺀다.
            if ((md or {}).get('doc_type') == 'observation' and isinstance(oid, int) and not isinstance(oid, bool)
                    and d is not None):
                best[oid] = max(best.get(oid, -9.0), 1.0 - float(d))
        return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


class Reranker:
    # 2026-10-21: ThreadingHTTPServer 가 요청마다 스레드를 띄워 mps 위에서 predict 가 동시에 돌면
    # Metal 'failed assertion _status < MTLCommandBufferStatusCommitted' 로 프로세스가 죽었다(동시 16요청 재현).
    predict_lock = threading.Lock()
    """한국어 재순위기(KURE-Reranker-nano, 149M). 후보 30개 질의당 약 0.25초(mps).
    재순위 순위와 들어온 결합 순위를 RRF k=10 으로 섞는다 — 재순위 점수만 쓰면 정확한 복합명사 질의에서
    결합 순위보다 떨어졌다(MRR .576→.520). 적재 전·실패 시 None(결합 순위 그대로)."""

    def __init__(self):
        self.model = None
        self.error = None
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            os.environ.setdefault('HF_HUB_OFFLINE', '1')
            from sentence_transformers import CrossEncoder
            import torch
            device = 'mps' if torch.backends.mps.is_available() else 'cpu'
            self.model = CrossEncoder(RERANK_MODEL, device=device, max_length=512, local_files_only=True)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)[:300]

    def order(self, query, ids, texts):
        if self.model is None or len(ids) < 2:
            return None
        with self.predict_lock:
            scores = self.model.predict([(query, t) for t in texts], batch_size=16)
        rank = {i: r for r, i in enumerate(sorted(range(len(ids)), key=lambda i: -float(scores[i])), 1)}
        blended = [1 / (11 + i) + 1 / (10 + rank[i]) for i in range(len(ids))]   # ids 는 결합 순위대로 들어온다
        return [ids[i] for i in sorted(range(len(ids)), key=lambda i: -blended[i])]


def fuse(lexical, vector, lex_weight=LEX_WEIGHT):
    """점수 결합(이론 최소 0 정규화). lexical·vector: [(id, 점수)] 높을수록 관련."""
    def norm(pairs):
        # 2026-10-21: 최소 0 정규화는 벡터 후보 30개가 모두 0.9 안팎이 돼 키워드에서만 잡힌 1~3위 기록이 30위 밖으로 밀렸다.
        # 후보 안 최소~최대로 펴면 A/B 30과제 원본 top-5 16→22, 실사용 60턴 정답 재현율 .329→.305(-2건). SB_RECALLD_FUSE_NORM=zero 로 되돌린다.
        if FUSE_NORM == 'zero':
            top = max((s for _, s in pairs), default=0)
            return {i: (s / top if top > 0 else 0.0) for i, s in pairs}
        if not pairs:
            return {}
        lo, hi = min(s for _, s in pairs), max(s for _, s in pairs)
        return {i: ((s - lo) / (hi - lo) if hi > lo else 1.0) for i, s in pairs}
    ln, vn = norm(lexical), norm(vector)
    score = {i: lex_weight * ln.get(i, 0.0) + (1 - lex_weight) * vn.get(i, 0.0) for i in set(ln) | set(vn)}
    return sorted(score, key=lambda i: (-score[i], i))


class Engine:
    def __init__(self):
        import sb_fts_ko
        self.ko = sb_fts_ko
        self.ko._tokenizer()
        self.lock = threading.Lock()
        self.last_sync = 0.0
        self.df, self.n_docs = {}, 1
        self.sync(force=True)
        self.vectors = Vectors() if HYBRID else None
        self.reranker = Reranker() if HYBRID and os.environ.get('SB_RECALLD_RERANK', '1') == '1' else None

    def sync(self, force=False):
        """force=True(기동 시)만 요청 전에 끝낸다. 평소에는 백그라운드 스레드 하나로 돌리고 즉시 돌아간다."""
        if force:
            return self._sync_now(force=True)
        if time.time() - self.last_sync < SYNC_EVERY or self.lock.locked():
            return
        threading.Thread(target=self._sync_now, daemon=True).start()

    def _sync_now(self, force=False):
        if not self.lock.acquire(blocking=force):
            return
        try:
            if force or time.time() - self.last_sync >= SYNC_EVERY:
                try:
                    r = self.ko.ensure_current()
                    if force or not self.df or (isinstance(r, dict) and (r.get('indexed') or r.get('removed'))):
                        self._load_df()
                except Exception:  # noqa: BLE001
                    pass
                self.last_sync = time.time()
        finally:
            self.lock.release()

    def _load_df(self):
        """단어별 문서 빈도 — 드문 단어 판정용(기록 수백~수천 건이라 적재 시 한 번 계산)."""
        from collections import Counter
        df = Counter()
        n = 0
        with closing(self.ko.open_index(create=False)) as conn:
            for (body,) in conn.execute('SELECT body FROM ko_fts'):
                n += 1
                df.update(set((body or '').lower().split()))
        self.df, self.n_docs = df, max(1, n)

    def nouns(self, text):
        ident = [w.lower() for w in re.findall(r'[A-Za-z0-9_]+(?:[._/-][A-Za-z0-9_]+)*', text)]
        kiwi = self.ko._kiwi
        if kiwi is None:
            return list(dict.fromkeys(self.ko.ko_query_tokens(text)))
        toks = ident + [t.form.lower() for t in kiwi.tokenize(text)
                        if (t.tag.startswith('NN') and t.tag != 'NNB') or t.tag in {'SL', 'SN', 'SH'}]   # NNB: 의존명사(거·것·수)
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
        width = max(limit * 2, CANDIDATES) if HYBRID else limit * 2
        with closing(self.ko.open_index(create=False)) as conn:
            rows = self._search(conn, match, project, width) if project else []
            seen = {r[0] for r in rows}
            rows += [r for r in self._search(conn, match, None, width) if r[0] not in seen]
        lexical = rows
        ranked = self._hybrid(query, lexical) if HYBRID else None
        if ranked:   # 보여줄 순서만 하이브리드 순서로 바꾸고, 판정은 아래에서 키워드 순서로 한다
            by_id = {r[0]: r for r in lexical}
            rows = [by_id.get(i, (i, 0.0, '', '')) for i in ranked]
        meta = self._meta(list(dict.fromkeys([r[0] for r in rows] + [r[0] for r in lexical])))
        # 판정 전에 거른다: 방금(20분 내) 생긴 실시간 관찰은 '회상'이 아니라 진행 중인 작업이다.
        # 판정 뒤에 거르면 판정은 그 기록으로 통과하고 주입은 무관한 기록이 되는 불일치가 생긴다(DOE S9).
        cutoff = (time.time() - FRESH_MINUTES * 60) * 1000
        dead = superseded_ids()   # 새 판으로 대체된 자동 메모리 옛 관측은 회수하지 않는다(2026-10-05)

        def keep(r):
            m = meta.get(r[0], {})
            return not (m.get('live') and m.get('epoch', 0) > cutoff) and r[0] not in dead
        rows = [r for r in rows if keep(r)][:limit]
        judge = next((r for r in lexical if keep(r)), None)
        matched, cov, rare = 0, 0.0, 0
        counted = [t for t in toks if t not in TASK_WORDS]   # 판정은 주제 단어로만(검색에는 모두 씀)
        if judge and counted:
            body = set((judge[3] or '').lower().split())
            hit = [t for t in counted if t in body]
            matched = len(hit)
            rare = (sum(1 for t in hit if self.df.get(t, 0) / self.n_docs < RARE_DF)
                    if self.n_docs >= 50 else matched)   # 기록이 적으면 빈도 비율이 무의미
            cov = matched / len(counted)
        gate = gate_pass(matched, cov, rare)
        items = []
        for oid, score, proj, _body in rows:
            m = meta.get(oid, {})
            items.append({'id': oid, 'score': round(-score, 3), 'project': proj or m.get('project', ''),
                          'title': m.get('title', ''), 'date': m.get('date', '')})
        return {'items': items, 'gate': {'matched': matched, 'coverage': round(cov, 2), 'pass': gate}, 'tokens': toks}

    def _hybrid(self, query, lexical):
        """키워드 후보 + 벡터 후보 → 점수 결합 → 재순위 섞기. 벡터가 아직 없거나 실패하면 None(키워드 순서 유지)."""
        vec = self.vectors.search(query, CANDIDATES) if self.vectors else None
        if not vec:
            return None
        order = fuse([(r[0], -r[1]) for r in lexical[:CANDIDATES]], vec)[:CANDIDATES]
        texts = self._texts(order)
        ids = [i for i in order if i in texts]
        try:
            reordered = self.reranker.order(query, ids, [texts[i] for i in ids]) if self.reranker else None
        except Exception:  # noqa: BLE001
            reordered = None
        return reordered or ids

    @staticmethod
    def _texts(ids):
        if not ids:
            return {}
        con = sqlite3.connect('file:%s?mode=ro' % Path(os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db()).as_posix(), uri=True, timeout=2)
        with closing(con):
            q = 'SELECT id, title, subtitle, narrative FROM observations WHERE id IN (%s)' % ','.join('?' * len(ids))
            return {r[0]: '\n'.join(x or '' for x in r[1:]) for r in con.execute(q, ids)}

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


def superseded_ids():
    """automemory_state.json 의 supersedes(옛 판) + 삭제된 메모리 파일의 관측 id."""
    try:
        with open(sb_config.sb_path('index', 'automemory_state.json'), encoding='utf-8') as f:
            st = json.load(f)
    except (OSError, ValueError):
        return set()
    out = set()
    for entry in st.values():
        if isinstance(entry, dict):
            out.update(i for i in entry.get('supersedes', []) if isinstance(i, int))
            if entry.get('deleted_at') and isinstance(entry.get('obs_id'), int):
                out.add(entry['obs_id'])
    return out


def serve(port, idle_hours):
    global DB
    DB = db_path()
    engine = None              # 포트를 먼저 잡고 나서 적재한다 — 이미 떠 있으면 Kiwi 적재 없이 바로 끝낸다
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
            if url.path == '/shutdown':   # 로컬 전용(127.0.0.1 바인드) — sb_recalld.py stop
                self._send(200, {'ok': True, 'pid': os.getpid()})
                threading.Thread(target=httpd.shutdown, daemon=True).start()
                return
            if url.path == '/health':
                v, rr = getattr(engine, 'vectors', None), getattr(engine, 'reranker', None)
                return self._send(200, {'ok': True, 'pid': os.getpid(), 'db': DB, 'hybrid': HYBRID,
                                        'vectors': bool(v and v.col is not None), 'vectors_error': v and v.error,
                                        'reranker': bool(rr and rr.model is not None), 'reranker_error': rr and rr.error})
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
    engine = Engine()          # 요청은 적재가 끝난 뒤부터 처리된다(serve_forever 전)
    run = Path(sb_config.sb_path('run'))
    run.mkdir(parents=True, exist_ok=True)
    (run / 'recalld.json').write_text(json.dumps({'pid': os.getpid(), 'port': port, 'started': time.time()}), encoding='utf-8')

    def reaper():
        last_relabel = 0.0
        while True:
            time.sleep(60)
            if time.time() - state['last'] > idle_hours * 3600:
                httpd.shutdown()
                return
            if time.time() - last_relabel >= 3600:   # 한 시간마다: 업무 폴더에 섞인 도구 작업 기록 자동 분리(설정 있을 때만)
                last_relabel = time.time()
                try:
                    import contextlib
                    import io
                    import sb_relabel
                    cfg = sb_relabel.auto_config()
                    if cfg and cfg.get('work_projects'):
                        with contextlib.redirect_stdout(io.StringIO()):
                            sb_relabel.auto_env(cfg['work_projects'], cfg.get('to') or 'secondbrain-kit', since_hours=24,
                                                extra_pattern=cfg.get('extra_env_pattern'))
                except Exception:  # noqa: BLE001
                    pass
                try:   # 실시간 관찰기가 옮겨 적은 실명 가리기(SQLite만 — 벡터는 야간 pii 단계)
                    import sb_memory
                    import sb_pii_sweep
                    mask = sb_memory._pii_masker()
                    if mask is not None and any(sb_pii_sweep.sweep_sqlite(mask).values()):
                        engine.ko.build(full=True)
                except Exception:  # noqa: BLE001
                    pass
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
    # 보여주는 순서: 현재 프로젝트 먼저, 그 안에서 최신판 먼저 — 낡은 판이 맨 위에 오던 문제(2026-10-05).
    # 관련도 판정은 서버가 점수 1위로 이미 끝냈다.
    items = sorted(d['items'], key=lambda it: (bool(project) and it.get('project') == project, it.get('date') or ''),
                   reverse=True)
    for it in items:
        print('- #%d %s %s · %s' % (it['id'], it['date'], it['project'], it['title'][:110]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('serve', 'ensure', 'status', 'query', 'stop'))
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
    if a.cmd == 'stop':
        try:
            urllib.request.urlopen(base_url(a.port) + '/shutdown', timeout=3).read()
            print('stopped')
        except Exception:  # noqa: BLE001
            print('not running')
        return 0
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
