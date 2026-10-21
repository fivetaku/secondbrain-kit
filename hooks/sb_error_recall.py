#!/usr/bin/env python3
"""PostToolUseFailure hook: when a tool call fails, look up past notes about the same error (2026-10-21, eval improvement #4).

Evidence: 44% of actionable tool errors repeat a signature already seen in another session, and for ~32% of sampled
repeated signatures a related cause/workaround note already existed before the repeat.
Randomized by session (hash of session_id) into 'on' and 'control' arms so the effect on repeats can be measured later;
both arms are logged to <SB_HOME>/logs/error_recall.jsonl (signature hash, ids, no error text).
Fail-open: any error -> no output, exit 0.
"""
import hashlib, json, re, time, urllib.parse, urllib.request
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import sb_config  # noqa: E402
import sb_lock  # noqa: E402


def _recalld_url() -> str:
    if os.environ.get("SB_RECALLD_URL"):
        return os.environ["SB_RECALLD_URL"]
    import sb_recalld
    return sb_recalld.base_url()
STATE_DIR = os.environ.get("SB_RECALL_STATE_DIR") or sb_config.sb_path("index", "sb-recall-state")
LOG = os.environ.get("SB_ERROR_RECALL_LOG") or sb_config.sb_path("logs", "error_recall.jsonl")
MAX_ITEMS = 2
NOT_ACTIONABLE = re.compile(r"(doesn't want to|was rejected|requires approval|haven't granted|permission to use|interrupted by user|user denied|wait for the user)", re.I)
MAX_LOG_BYTES = 2_000_000
OMO_FAILURE_RE = re.compile(
    r"(?i)(command not found|no such file or directory|traceback|syntaxerror|"
    r"\berror:|exit code [1-9]|exited with code [1-9]|tests? failed|build failed|assertionerror)")


def signature(text: str) -> str:
    t = re.sub(r"/[\w./-]+", "<path>", text)
    t = re.sub(r"\b[0-9a-f]{7,}\b", "<hex>", t)
    t = re.sub(r"\d+", "<n>", t)
    return re.sub(r"\s+", " ", t).strip().lower()[:160]


def arm(sid: str) -> str:
    forced = os.environ.get("SB_ERROR_RECALL_ARM")
    try:   # OmO 는 훅에 환경변수를 넘기지 않는다 — QA 동안만 파일로 강제(10분 지나면 무시)
        ov = os.path.join(STATE_DIR, "error_recall_arm.override")
        if not forced and time.time() - os.path.getmtime(ov) < 600:
            forced = open(ov).read().strip()
    except OSError:
        pass
    if forced in ("on", "control"):
        return forced
    return "on" if int(hashlib.sha256(sid.encode()).hexdigest(), 16) % 2 == 0 else "control"


def recall(query: str):
    url = _recalld_url() + "/recall?" + urllib.parse.urlencode({"q": query[:300], "limit": str(MAX_ITEMS)})
    with urllib.request.urlopen(url, timeout=3) as r:
        d = json.loads(r.read().decode())
    if not (d.get("gate") or {}).get("pass"):
        return []
    return [{"id": int(it["id"]), "title": it.get("title") or "", "date": it.get("date") or "", "project": it.get("project") or ""}
            for it in d.get("items", [])][:MAX_ITEMS]


def log(rec: dict):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > MAX_LOG_BYTES:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main():
    if os.environ.get("CLAUDE_MEM_INTERNAL") == "1" or os.environ.get("SB_ERROR_RECALL", "1") == "0":
        return
    raw = json.loads(sys.stdin.read() or "{}")
    event = "PostToolUseFailure"
    resp = raw.get("tool_response")
    if raw.get("hook_event_name") == "PostToolUse" and isinstance(resp, dict):   # OmO: 실패도 PostToolUse로 온다
        c = resp.get("content")
        text = c if isinstance(c, str) else "\n".join(b.get("text", "") for b in (c or []) if isinstance(b, dict))
        # OmO 코드 모드의 bash 는 종료 코드가 나빠도 is_error=false 로 온다(2026-10-21 실측) — fablize 와 같은 문구 기준으로도 실패를 본다.
        if str(raw.get("tool_name", "")).lower() in ("edit", "write", "apply_patch", "multiedit"):
            failed = bool(resp.get("is_error"))
        else:
            failed = bool(resp.get("is_error")) or bool(OMO_FAILURE_RE.search(text[:4000]))
        if not failed:
            return
        event = "PostToolUse"
        m = OMO_FAILURE_RE.search(text)
        raw["error"] = text[max(0, m.start() - 200):m.end() + 300] if m else text[:500]
    raw["_event"] = event
    err = str(raw.get("error") or "")
    if raw.get("is_interrupt") or len(err) < 20 or NOT_ACTIONABLE.search(err):
        return
    sid = str(raw.get("session_id") or "nosession")
    sig = signature(err)
    sig_hash = hashlib.sha256(sig.encode()).hexdigest()[:16]
    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, re.sub(r"[^\w-]", "_", sid) + ".err.json")
    lockf = open(state_path + ".lock", "a")
    sb_lock.lock_with_timeout(lockf, sb_lock.LOCK_EX, 5)
    try:
        run(raw, sid, err, sig_hash, state_path)
    finally:
        sb_lock.lock(lockf, sb_lock.LOCK_UN)
        lockf.close()


def run(raw, sid, err, sig_hash, state_path):
    try:
        st = json.load(open(state_path))
    except Exception:
        st = {"sigs": [], "ids": []}
    group = arm(sid)
    if sig_hash in st["sigs"]:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "sid": sid, "arm": group, "tool": raw.get("tool_name"), "sig": sig_hash, "repeat_in_session": True, "injected": False})
        return
    t0 = time.time()
    try:
        items = [it for it in recall(f"{raw.get('tool_name', '')} {err}") if it["id"] not in st["ids"]]
    except Exception as e:
        log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "sid": sid, "arm": group, "tool": raw.get("tool_name"), "sig": sig_hash, "error": type(e).__name__})
        return
    st["sigs"].append(sig_hash)
    if group == "on":
        st["ids"] += [it["id"] for it in items]
    try:
        json.dump(st, open(state_path, "w"))
    except OSError:
        pass
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "sid": sid, "arm": group, "tool": raw.get("tool_name"), "sig": sig_hash,
         "ids": [it["id"] for it in items], "injected": group == "on" and bool(items), "ms": round((time.time() - t0) * 1000)})
    if group != "on" or not items:
        return
    lines = "\n".join(f"- #{it['id']} {it['date'][5:]} {it['project'][:18]} · {it['title'][:80]}" for it in items)
    ctx = ("[세컨브레인 오류 회수] 방금 실패한 도구 호출과 비슷한 문제를 다룬 과거 기록이다. 원인·우회책이 있을 수 있으니 "
           "관련 있으면 get_observations([ID])로 본문을 보고, 아니면 무시한다.\n" + lines)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": raw.get("_event", "PostToolUseFailure"), "additionalContext": ctx}}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
