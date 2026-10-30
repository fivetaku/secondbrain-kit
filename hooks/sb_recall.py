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
MAX_ITEMS = int(os.environ.get("SB_RECALL_MAX", "3"))   # 2026-10-31: 5→3. 실제 프롬프트 40개 블라인드 평가에서 잡음 -39, 쓸모 있는 기록을 받은 프롬프트 수는 그대로
FETCH = int(os.environ.get("SB_RECALL_FETCH", "10"))    # 자기 세션·20분 내 기록을 거른 뒤에도 MAX_ITEMS 를 채우려고 넉넉히 받는다
LAST = {}   # 마지막 recalld 응답의 후보 순서·게이트 — 회수 방식 비교의 운영 기준선으로 남긴다
MAX_CHARS = 700
MIN_PROMPT = 12
# 세션별 주입 원장. 2026-10-21: 임시 폴더는 재부팅마다 지워져 같은 세션에 같은 기록이 다시 주입됐다 → SB_HOME 아래 영속 경로.
STATE_DIR = os.environ.get("SB_RECALL_STATE_DIR") or sb_config.sb_path("index", "sb-recall-state")
STATE_TTL_DAYS = 30
# 세션 중 교정 감지(선택): 교정처럼 보이는 발화만 hooks/sb_correction.py 가 LLM 으로 판정해 후보 대기열에 쌓고,
# 같은 세션의 다음 프롬프트에 한 번 알린다. LLM 설정(SB_CORRECTION_LLM_URL 또는 index/correction_llm.json)이 없으면 꺼진 것과 같다.
CORRECTION = re.compile(r"(아니[ 야라고]|그게 아니|틀렸|잘못|하지 ?마|하지말|말고|다시 해|왜 이렇게|그렇게 하지|내가 말한|말했잖|몇 번|또 이러|이러지 마|금지|앞으로는|항상 |절대 )")
CORRECTION_QUEUE = os.environ.get("SB_CORRECTION_QUEUE") or sb_config.sb_path("index", "correction_candidates.jsonl")
FAIL_NOTICE = ("[세컨브레인 회수 실패] 회수 서버(recalld·worker)가 응답하지 않거나 제때 답하지 못해 관련 기록을 확인하지 못했다. "
               "'관련 기록 없음'이 아니다. 과거 맥락이 필요하면 `sb search`로 직접 검색한다.")
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
# 정밀화(실사용 10일 로그): 판정 10회 중 약 9회가 오탐(붙여넣은 일정 속 날짜, '9~12월' 같은 시트·탭 이름,
# '최근 추세'·'요청했던 안으로' 같은 서술)이고, '왜 그렇게 가정했었지?' 같은 진짜 과거 질문은 놓쳤다.
#  - 붙여넣은 블록·URL·코드는 판정에서 뺀다
#  - 강한 신호(그 자체로 과거를 묻는 말)는 바로 인정, 약한 신호(날짜·최근·했던)는 의문형일 때만 인정
TIME_STRONG = re.compile(r"이번\s*(달|주|분기)\s*(에|동안)?\s*(한|했|진행)|지난\s*(달|주|번|분기)|저번|그동안|회고|"
                         r"어떻게\s*(됐|되었|진행)|히스토리|이력|경위|어디까지\s*(했|됐|진행)|했었|였었|"
                         r"(이었|였|했|했었|하기로\s*했)(지|더라|던가)\s*[?？!]|하기로\s*(했|한)\s*(거|것)")
TIME_WEAK = re.compile(r"요즘|최근|어제|그저께|오늘\s*한|월간|주간|\d{1,2}\s*월\s*(에|중)\s*(한|했)|\d{1,2}/\d{1,2}|했던|됐던")
QUESTION = re.compile(r"[?？]|(지|나|까|니|냐|더라|던가|됐어|했어)\s*[.!~]*\s*$")
PAST = re.compile(r"했|됐|되었|였|었|보냈|왔|받았|정했")
# 닫는 태그가 없는(잘린) 붙여넣기 블록은 끝까지 뺀다
_PASTED = re.compile(r"<pasted_content[^>]*>.*?(</pasted_content>|\Z)|```.*?(```|\Z)|https?://\S+", re.S)
TIME_PAT = TIME_STRONG   # 하위 호환(외부 참조용)


def is_history_question(prompt: str) -> bool:
    text = _PASTED.sub(" ", prompt)
    if TIME_STRONG.search(text):
        return True
    # 약한 신호는 '의문형 + 과거형'이 함께 있을 때만 — "9/22 이후 수정된 것만 걸어줄 수 있어?" 같은 요청은 제외
    return bool(TIME_WEAK.search(text) and QUESTION.search(text) and PAST.search(text))
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
# recalld 응답 대기(초). 2026-10-20 재순위기가 켜진 뒤 질의당 0.4~0.9초가 실측돼 1.5초면 느린 PC에서 버려졌다 → 3초.
# 훅 전체 제한(15초) 안에서 워커 폴백(3초)까지 들어간다. SB_RECALLD_TIMEOUT 으로 조정.
RECALLD_TIMEOUT_DEFAULT = 3.0


def recalld_timeout() -> float:
    try:
        v = float(os.environ.get("SB_RECALLD_TIMEOUT") or RECALLD_TIMEOUT_DEFAULT)
    except ValueError:
        return RECALLD_TIMEOUT_DEFAULT
    return v if v > 0 else RECALLD_TIMEOUT_DEFAULT


def recalld_search(prompt: str, project: str):
    """상주 서버 결과. 문턱 미달이면 [] (주입 안 함), 서버가 없으면 None(띄우고 폴백)."""
    try:
        import sb_recalld
        url = sb_recalld.base_url() + "/recall?" + urllib.parse.urlencode(
            {"q": prompt[:500], "project": project, "limit": str(FETCH)})
        with urllib.request.urlopen(url, timeout=recalld_timeout()) as r:
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
    LAST.update(gate=d.get("gate"), cand=[it["id"] for it in d.get("items", [])])
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
        it["date"] = d[5:] if d else ""; it["project"] = proj or ""; it["_d"] = d or ""
        out.append(it)
    return out


# ── 주입 잡음 줄이기(2026-10-05 감사: 주입의 약 78%가 안 쓰임) ──────────────────────────
# 정기 자동 실행(예: 아침 브리핑) 관측은 그 주제를 물을 때만 내보낸다. PC 마다 이름이 달라 환경변수로 바꾼다.
BRIEF_TITLE = re.compile(os.environ.get("SB_RECALL_AUTO_TITLE") or r"daily\s*brief|데일리\s*브리핑|daily\s*브리핑", re.I)
BRIEF_PROMPT = re.compile(os.environ.get("SB_RECALL_AUTO_PROMPT") or r"브리핑|brief|데일리|daily", re.I)
CROSS_MIN_SHARED = 2


def superseded_ids():
    """자동 메모리 동기화가 새 판으로 대체한 옛 관측 + 삭제된 메모리 파일의 관측 — 회수에서 뺀다."""
    try:
        st = json.load(open(sb_config.sb_path("index", "automemory_state.json"), encoding="utf-8"))
    except Exception:
        return set()
    out = set()
    for entry in st.values():
        if isinstance(entry, dict):
            out.update(i for i in entry.get("supersedes", []) if isinstance(i, int))
            if entry.get("deleted_at") and isinstance(entry.get("obs_id"), int):
                out.add(entry["obs_id"])
    return out


def _tokens(s: str):
    return {t.lower() for t in re.findall(r"[A-Za-z0-9]{2,}|[가-힣]{2,}", s or "")}


def refine(items, prompt: str, project: str):
    drop = superseded_ids()
    want_brief = bool(BRIEF_PROMPT.search(prompt))
    ptoks = _tokens(prompt)
    out = []
    for it in items:
        if it["id"] in drop:
            continue
        if not want_brief and BRIEF_TITLE.search(it.get("title", "")):
            continue
        proj = it.get("project") or ""
        if project and proj and proj != project and len(ptoks & _tokens(it.get("title", ""))) < CROSS_MIN_SHARED:
            continue
        out.append(it)
    # 같은 주제면 최신판이 먼저 — 상위 N건만 요지가 붙으므로 낡은 판이 요지를 차지하지 않게(2026-10-05)
    out.sort(key=lambda it: (bool(project) and it.get("project") == project, it.get("_d", "")), reverse=True)
    return out


def recall_log(event: dict) -> None:
    try:
        path = Path(sb_config.sb_path("logs", "recall.jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def hashlib_sid(sid: str) -> str:
    import hashlib
    return hashlib.sha256(sid.encode()).hexdigest()[:12]


def prompt_hash(p: str) -> str:
    import hashlib
    return hashlib.sha256(p.strip().encode("utf-8")).hexdigest()[:16]


class _Locked:
    """세션 원장 파일 하나에 대한 잠금 — 동시 훅 실행이 원장을 덮어쓰지 않게."""

    def __init__(self, path: str):
        self.path = path + ".lock"
        self.f = None

    def __enter__(self):
        import sb_lock
        self.f = open(self.path, "a")
        sb_lock.lock_with_timeout(self.f, sb_lock.LOCK_EX, 3)
        return self

    def __exit__(self, *exc):
        import sb_lock
        if self.f:
            sb_lock.lock(self.f, sb_lock.LOCK_UN)
            self.f.close()
        return False


def spawn_correction_check(sid: str, prompt: str, cwd: str) -> None:
    if os.environ.get("SB_CORRECTION", "1") == "0" or not CORRECTION.search(prompt):
        return
    import subprocess
    script = str(Path(__file__).resolve().parent / "sb_correction.py")
    try:
        flags = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008 | 0x00000200}
        p = subprocess.Popen([sys.executable, script], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **flags)
        if p.stdin:
            p.stdin.write(json.dumps({"sid": sid, "prompt": prompt[:800], "cwd": cwd}, ensure_ascii=False).encode("utf-8"))
            p.stdin.close()
    except Exception:
        pass


def pending_corrections(sid: str, notified: set):
    out = []
    try:
        with open(CORRECTION_QUEUE, "rb") as fb:
            fb.seek(0, os.SEEK_END)
            fb.seek(max(0, fb.tell() - 262144))
            tail = fb.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in tail:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        key = r.get("ts", "") + "|" + r.get("rule", "")[:40]
        if r.get("sid") == sid and key not in notified:
            out.append((key, r.get("rule", "")))
    return out


def briefed_corrections() -> set:
    """세션 시작 브리핑(hooks/session_context.py)이 이미 보여 준 교정 후보 키(2026-10-30).
    같은 세션 ID 로 이어지는 시작(resume·compact)에서 브리핑에 뜬 후보가 다음 프롬프트 [교정 감지]에 또 뜨지 않게 뺀다.
    이 훅은 읽기만 하고 쓰는 쪽은 session_context 하나라 두 훅이 같은 파일을 다투지 않는다."""
    try:
        with open(os.path.join(STATE_DIR, "corrections.briefed.json"), encoding="utf-8") as fh:
            return set(json.load(fh))
    except Exception:
        return set()


def correction_note(sid: str, prompt: str, cwd: str) -> str:
    """교정 판정 작업을 프롬프트당 한 번 띄우고, 이 세션에 쌓인 미통지 후보를 최대 3건 알린다(보여 준 것만 통지 완료)."""
    path = os.path.join(STATE_DIR, re.sub(r"[^\w-]", "_", sid) + ".corr.json")
    note = ""
    try:
        with _Locked(path):
            try:
                cst = json.load(open(path, encoding="utf-8"))
            except Exception:
                cst = {"notified": [], "checked": []}
            ph = prompt_hash(prompt)
            p = prompt.strip()
            if p and not p.startswith(("/", "<")) and ph not in cst["checked"]:
                cst["checked"] = (cst["checked"] + [ph])[-200:]
                spawn_correction_check(sid, p, cwd)
            shown = pending_corrections(sid, set(cst["notified"]) | briefed_corrections())[:3]
            if shown:
                note = ("[교정 감지] 이 세션의 사용자 교정이 지속 규칙으로 보인다. 맞으면 지금 규칙·메모리에 저장하고, 아니면 무시한다.\n"
                        + "\n".join("- " + rule for _, rule in shown))
                cst["notified"] = cst["notified"] + [k for k, _ in shown]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cst, fh)
    except Exception:
        pass
    return note


def emit(ctx: str) -> None:
    if ctx:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}))  # ASCII 이스케이프(윈도우 인코딩)


def main():
    try:
        raw = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    prompt = str(raw.get("prompt") or "")
    if "codex" in sys.argv[1:]:   # Codex: exec 자동화 세션에는 주입·교정 판정을 하지 않는다
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from codex_hook import is_automation
            if is_automation(raw):
                return
        except Exception:
            pass
    sid = str(raw.get("session_id") or "nosession")
    os.makedirs(STATE_DIR, exist_ok=True)
    internal = os.environ.get("CLAUDE_MEM_INTERNAL") == "1" or os.environ.get("SB_RECALL", "1") == "0"
    corr = "" if internal else correction_note(sid, prompt, str(raw.get("cwd") or ""))
    if not gate(prompt):
        emit(corr)
        return
    # claude-mem 의 project 는 cwd 폴더 이름이다(sb_scope id 와 다른 이름공간 — 의도된 분리).
    project = os.path.basename(os.path.normpath(str(raw.get("cwd") or ""))) if raw.get("cwd") else ""
    state_path = os.path.join(STATE_DIR, re.sub(r"[^\w-]", "_", sid) + ".json")
    if not os.path.exists(state_path):   # 새 세션일 때만: 오래된 세션 상태 파일 정리
        cutoff = time.time() - STATE_TTL_DAYS * 86400
        for name in os.listdir(STATE_DIR):
            fp = os.path.join(STATE_DIR, name)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
            except OSError:
                pass
    lk = _Locked(state_path)
    try:
        lk.__enter__()
    except Exception:
        lk = None
    try:
        _main_locked(raw, prompt, sid, project, state_path, corr)
    finally:
        if lk:
            lk.__exit__()


def _main_locked(raw, prompt, sid, project, state_path, corr):
    ph = prompt_hash(prompt)
    try:
        st = json.load(open(state_path, encoding="utf-8")); seen = set(st.get("ids", []))
        last = st.get("last_prompt_sha") or (prompt_hash(st["last_prompt"]) if st.get("last_prompt") else "")
    except Exception:
        seen, last = set(), ""
    if ph == last:      # identical re-submission → nothing new to add
        emit(corr)
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
        except Exception as exc:
            # 두 백엔드 모두 실패 — '관련 기록 없음'과 구분되게 알리고 기록한다(프롬프트는 남기지 않음).
            recall_log({"sid": hashlib_sid(sid), "project": project, "failed": type(exc).__name__,
                        "ms": int((time.time() - t0) * 1000)})
            sys.stderr.write(f"sb-recall: recall unavailable ({type(exc).__name__})\n")
            emit("\n\n".join(x for x in (corr, FAIL_NOTICE) if x))
            return
    items = refine(enrich(items, sid), prompt, project)
    fresh = [it for it in items if it["id"] not in seen][:MAX_ITEMS]
    hint = TIME_HINT if is_history_question(prompt) else ""
    recall_log({"sid": hashlib_sid(sid), "project": project, "ids": [it["id"] for it in fresh],
                "hint": bool(hint), "gated": gated, "ms": int((time.time() - t0) * 1000),
                "prompt_sha": hashlib_sid(prompt.strip()), "cand": LAST.get("cand", []), "gate": LAST.get("gate"),
                "kept": [it["id"] for it in items], "max": MAX_ITEMS})
    if not fresh and not hint:
        emit(corr)
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
    if corr:
        ctx = corr + "\n\n" + ctx
    try:
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump({"ids": sorted(seen | {it["id"] for it in fresh}), "last_prompt_sha": ph, "updated": time.time()}, fh)
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
    emit(ctx)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — 훅은 어떤 이유로도 세션을 막지 않는다
        pass
    sys.exit(0)
