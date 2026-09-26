#!/usr/bin/env python3
"""UserPromptSubmit hook: inject a compact index of related past work (titles + IDs only).

Design (2026-09-09, owner decision "알아서 가져다 쓰되 낭비는 말 것"):
  1. Gate cheaply — skip short/command-like prompts, slash commands, automation sessions.
  2. Query claude-mem worker /api/search (≈0.2s, global across projects).
  3. Inject only observation IDs + titles (≤ MAX_ITEMS lines, ≤ MAX_CHARS). Bodies are fetched
     by the model on demand via get_observations([id]).
  4. Dedupe per session — an ID shown once is not shown again in that session.
Fail-open: any error → no output, exit 0. Never blocks the prompt.
Opt-out: SB_RECALL=0 in the environment (used by nightly/automation callers).
"""
import json, os, re, sys, tempfile, time, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import sb_config  # noqa: E402

# Windows 기본 코드페이지(cp949)로 읽으면 한국어 프롬프트가 깨져 회수가 전부 빗나간다 — 훅 입출력은 UTF-8 고정.
for _s in (sys.stdin, sys.stdout):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WORKER = os.environ.get("SB_RECALL_WORKER") or sb_config.worker_base_url()
MAX_ITEMS = int(os.environ.get("SB_RECALL_MAX", "5"))
MAX_CHARS = 700
MIN_PROMPT = 12
STATE_DIR = os.path.join(tempfile.gettempdir(), "sb-recall")
# 추가로 건너뛸 프롬프트 접두어(자동화 루프 등). 예: SB_RECALL_SKIP_PREFIXES="[fablize,/loop"
SKIP_PREFIXES = tuple(x for x in os.environ.get("SB_RECALL_SKIP_PREFIXES", "").split(",") if x)
SHORT_CMDS = re.compile(r"^(ㄱ+|ㄴ+|ㅇ+|ok|okay|yes|no|응|네|아니|계속|진행|진행해|진행시켜라?|해줘|해|고고|다시|취소|중단|멈춰|스톱|stop|continue|go)[.!~ ]*$", re.I)


def gate(prompt: str) -> bool:
    if os.environ.get("CLAUDE_MEM_INTERNAL") == "1":   # claude-mem's own observer agent → recursion
        return False
    if os.environ.get("SB_RECALL", "1") == "0":
        return False
    p = prompt.strip()
    if len(p) < MIN_PROMPT or p.startswith("/") or p.startswith("<"):
        return False
    if SHORT_CMDS.match(p):
        return False
    if SKIP_PREFIXES and p.startswith(SKIP_PREFIXES):
        return False
    return True


def _rows(text: str):
    items, date = [], ""
    for line in text.splitlines():
        m = re.match(r"^### (.+)$", line)
        if m:
            date = m.group(1).strip(); continue
        m = re.match(r"^\| #(\d+) \| ([^|]+) \| ([^|]*) \| (.+?) \| ~?\d*\s*\|", line)
        if m:
            items.append({"id": int(m.group(1)), "date": date, "kind": m.group(3).strip(), "title": m.group(4).strip()})
    return items


def _get(path: str, params: dict):
    url = WORKER + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=3) as r:
        d = json.loads(r.read().decode())
    return d["content"][0]["text"] if isinstance(d.get("content"), list) else ""


def search(q: str, project: str):
    """Observation-only search. The worker picks semantic candidates then orders them by date,
    so keep limits tight: global top-5 + project-scoped top-3, merged (project hits first)."""
    q = q[:300]
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        if project:
            jobs.append(ex.submit(_get, "/api/search/observations", {"query": q, "limit": "3", "project": project}))
        jobs.append(ex.submit(_get, "/api/search/observations", {"query": q, "limit": str(MAX_ITEMS)}))
    items, errors = [], 0
    for j in jobs:
        try:
            items += _rows(j.result())
        except Exception:
            errors += 1
    if errors == len(jobs):
        raise RuntimeError("search failed")
    # Cross-project (global) hits must share at least one content token with the prompt;
    # the worker orders semantic candidates by date, so this cheap lexical check removes drifters.
    toks = {t.lower() for t in re.findall(r"[A-Za-z0-9]{2,}|[가-힣]{2,}", q)}
    n_proj = 3 if project else 0
    out, seen = [], set()
    for i, it in enumerate(items):
        if it["id"] in seen:
            continue
        if i >= n_proj:
            title = it["title"].lower()
            if not any(t in title for t in toks):
                continue
        seen.add(it["id"]); out.append(it)
    return out


# 기간·이력·상태 질문 — 제목 몇 줄로는 부족하니 기간 조회를 안내한다(2026-09-26).
TIME_PAT = re.compile(r"이번\s*(달|주|분기)|지난\s*(달|주|번|분기)|저번|요즘|최근|그동안|어제|그저께|오늘\s*한|월간|주간|회고|"
                      r"\d{1,2}\s*월\s*(에|달|중|한)|\d{1,2}/\d{1,2}|어떻게\s*됐|진행\s*상황|진행\s*상태|히스토리|이력|경위|했었|했던")
TIME_HINT = ("[세컨브레인 회수·기간/이력 질문] 답하기 전에 `sb timeline --since YYYY-MM-DD [--until D]`(세션 날짜 기준 목록)와 "
             "`sb search '<주제>' --global --limit 5`로 기록층을 먼저 훑고, git·파일 실측과 교차 확인한다. 답 끝에 근거(#ID·파일·커밋)를 적는다.")

# 상위 N건에 요지(facts/narrative 앞부분)를 붙인다. 0 = 제목만.
BODIES = int(os.environ.get("SB_RECALL_BODIES", "3") or 0)
BODY_CHARS = 220


def _bodies(ids):
    if not ids:
        return {}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{os.environ.get('SB_CLAUDE_MEM_DB') or sb_config.claude_mem_db()}?mode=ro", uri=True, timeout=1)
        rows = con.execute(f"SELECT id, facts, narrative, text FROM observations WHERE id IN ({','.join('?'*len(ids))})", ids).fetchall()
        con.close()
    except Exception:
        return {}
    out = {}
    for oid, facts, narrative, text in rows:
        src = narrative or text or ""
        try:   # facts 는 JSON 배열 문자열인 경우가 많다
            fl = json.loads(facts) if facts else None
            if isinstance(fl, list) and fl:
                src = " / ".join(str(x) for x in fl)
        except Exception:
            pass
        src = re.sub(r"\s+", " ", src).strip()
        if src:
            out[oid] = src[:BODY_CHARS] + ("…" if len(src) > BODY_CHARS else "")
    return out


# 검색 백엔드: worker(claude-mem 의미검색, 문턱 없음) | recalld(상주 형태소 색인 + 문턱)
BACKEND = os.environ.get("SB_RECALL_BACKEND", "recalld")


def recalld_search(prompt: str, project: str):
    """상주 서버 결과. 문턱 미달이면 [] (주입 안 함), 서버가 없으면 None(띄우고 폴백)."""
    try:
        import sb_recalld
        url = sb_recalld.base_url() + "/recall?" + urllib.parse.urlencode(
            {"q": prompt[:500], "project": project, "limit": str(MAX_ITEMS)})
        with urllib.request.urlopen(url, timeout=1.5) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        try:
            import sb_recalld
            sb_recalld.ensure_running()
        except Exception:
            pass
        return None
    if d.get("db") and d["db"] != sb_recalld.db_path():   # 다른 DB 를 보는 서버(테스트 등) — 쓰지 않는다
        return None
    if not (d.get("gate") or {}).get("pass"):
        return []
    return [{"id": it["id"], "date": it.get("date", ""), "kind": "", "title": it.get("title", ""),
             "project": it.get("project", "")} for it in d.get("items", [])]


FRESH_MINUTES = 20   # observations younger than this are "what is happening right now", not recall


def enrich(items, content_session_id: str):
    """One read-only SQLite pass: add date+project, drop this session's own observations and very fresh ones."""
    if not items:
        return items
    try:
        import sqlite3
        db = sb_config.claude_mem_db()
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        own = {r[0] for r in con.execute(
            "SELECT o.id FROM observations o JOIN sdk_sessions s ON s.memory_session_id=o.memory_session_id "
            "WHERE s.content_session_id=?", (content_session_id,))}
        ids = [it["id"] for it in items]
        meta = {r[0]: (r[1], r[2], r[3], r[4] or "") for r in con.execute(
            f"SELECT id, substr(created_at,1,10), project, created_at_epoch, metadata FROM observations WHERE id IN ({','.join('?'*len(ids))})", ids)}
        con.close()
    except Exception:
        return items
    cutoff = (time.time() - FRESH_MINUTES * 60) * 1000
    out = []
    for it in items:
        if it["id"] in own:
            continue
        d, proj, ep, md = meta.get(it["id"], ("", "", 0, ""))
        # 소급 적재(import/backfill/automemory-sync)는 created_at 이 적재 시각일 뿐 "지금 벌어지는 일"이 아니다.
        imported = any(k in md for k in ('"kind":"import"', '"kind":"automemory-sync"', 'backfill'))
        if ep and ep > cutoff and not imported:
            continue
        sd = re.search(r'"session_date":"(\d{4}-\d{2}-\d{2})"', md)
        if sd:
            d = sd.group(1)
        it["date"] = d[5:] if d else ""; it["project"] = proj or ""
        out.append(it)
    return out


def main():
    try:
        raw = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    prompt = str(raw.get("prompt") or "")
    if not gate(prompt):
        return
    if "codex" in sys.argv[1:]:   # Codex: exec 자동화 세션에는 주입하지 않는다
        try:
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from codex_hook import is_automation
            if is_automation(raw):
                return
        except Exception:
            pass
    sid = str(raw.get("session_id") or "nosession")
    # claude-mem 의 project 는 cwd 폴더 이름이다(sb_scope id 와 다른 이름공간 — 의도된 분리).
    project = os.path.basename(os.path.normpath(str(raw.get("cwd") or ""))) if raw.get("cwd") else ""
    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, re.sub(r"[^\w-]", "_", sid) + ".json")
    if not os.path.exists(state_path):   # 새 세션일 때만: 7일 지난 세션 상태 파일 정리
        cutoff = time.time() - 7 * 86400
        for name in os.listdir(STATE_DIR):
            fp = os.path.join(STATE_DIR, name)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
            except OSError:
                pass
    try:
        st = json.load(open(state_path, encoding="utf-8")); seen = set(st.get("ids", [])); last = st.get("last_prompt", "")
    except Exception:
        seen, last = set(), ""
    if prompt.strip() == last:      # identical re-submission → nothing new to add
        return
    t0 = time.time()
    items = None
    gated = False              # 관련도 문턱을 통과한 주입인가(Stop 게이트는 이때만 되돌린다)
    if BACKEND == "recalld":   # 상주 한국어 형태소 색인 + 관련도 문턱(eval/retrieval_eval.py 근거)
        items = recalld_search(prompt, project)
        gated = bool(items)
    if items is None:          # 서버가 아직 안 떴으면 이번만 워커로(서버는 백그라운드로 띄워 둔다)
        try:
            items = search(prompt, project)
        except Exception:
            items = []
    items = enrich(items, sid)
    fresh = [it for it in items if it["id"] not in seen][:MAX_ITEMS]
    hint = TIME_HINT if TIME_PAT.search(prompt) else ""
    if not fresh and not hint:
        return
    lines = [f"- #{it['id']} {it['date']} {it.get('project','')[:18]} · {it['title'][:70]}" for it in fresh]
    bodies = _bodies([it["id"] for it in fresh[:BODIES]]) if BODIES > 0 else {}
    if bodies:   # 상위 N건은 요지 한 줄을 같이 — 제목만으로는 관련 여부 판단·활용이 안 된다
        lines = [ln + (("\n    ↳ " + bodies[it["id"]]) if it["id"] in bodies else "") for ln, it in zip(lines, fresh)]
    body = "\n".join(lines)
    while len(body) > MAX_CHARS + BODY_CHARS * BODIES and len(lines) > 1:
        lines.pop(); body = "\n".join(lines)
    ctx = ("[세컨브레인 회수] 이 프롬프트와 관련 있을 수 있는 과거 기록"
           + ("(상위 %d건은 요지 포함)" % len(bodies) if bodies else "(제목만)")
           + ". 실제로 관련 있으면 get_observations([ID])로 본문을 가져오고, 아니면 무시한다.\n" + body) if fresh else ""
    if hint:
        ctx = (ctx + "\n" + hint).strip()
    try:
        json.dump({"ids": sorted(seen | {it["id"] for it in fresh}), "last_prompt": prompt.strip(), "updated": time.time()}, open(state_path, "w", encoding="utf-8"))
    except Exception:
        pass
    # recall_gate 의 Stop 게이트 기준 시각 — 기간·이력 질문에만 건다(회수 목록만 붙은 프롬프트는 제외: 매 답변 되돌림 방지)
    if hint:
        try:
            import hashlib, pathlib
            gate_dir = pathlib.Path(os.environ.get("SB_RECALL_GATE_DIR", sb_config.sb_path("logs", "recall-gate")))
            gate_dir.mkdir(parents=True, exist_ok=True)
            (gate_dir / (hashlib.sha256(sid.encode()).hexdigest() + ".needs")).touch()
        except Exception:
            pass
    if os.environ.get("SB_RECALL_DEBUG") == "1":
        sys.stderr.write(f"sb-recall: {len(fresh)} items in {time.time()-t0:.2f}s\n")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}))  # ASCII 이스케이프(윈도우 인코딩)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — 훅은 어떤 이유로도 세션을 막지 않는다
        pass
    sys.exit(0)
