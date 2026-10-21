#!/usr/bin/env python3
"""Background classifier for in-session user corrections (2026-10-21, memory-daemon eval improvement #3).

sb-recall.py spawns this detached when a prompt matches the correction regex. It asks a small model whether the
message is a correction that implies a lasting rule; durable ones are appended to the candidate queue, and
sb-recall.py surfaces them on the next prompt of the same session so the agent can persist the rule right away.
Evidence: 12 durable corrections in a sample of 80 regex hits, only 1 ever reached rules/memory.
Never raises; never blocks a session. Queue only, no writes to claude-mem or state.db.
"""
import hashlib, json, time, urllib.request
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import sb_config  # noqa: E402
import sb_lock  # noqa: E402


QUEUE = os.environ.get("SB_CORRECTION_QUEUE") or sb_config.sb_path("index", "correction_candidates.jsonl")
def _llm_conf() -> dict:
    """OpenAI 호환 엔드포인트. 환경변수 또는 <SB_HOME>/index/correction_llm.json({"url","model","key"}). 없으면 판정하지 않는다."""
    try:
        conf = json.load(open(sb_config.sb_path("index", "correction_llm.json"), encoding="utf-8"))
    except Exception:
        conf = {}
    return {"url": os.environ.get("SB_CORRECTION_LLM_URL") or conf.get("url") or "",
            "model": os.environ.get("SB_CORRECTION_MODEL") or conf.get("model") or "",
            "key": os.environ.get("SB_CORRECTION_LLM_KEY") or conf.get("key") or ""}
MAX_QUEUE_BYTES = 1_000_000
PROMPT = ("You classify one user message sent to an AI coding assistant during a work session.\nMessage:\n<<<{p}>>>\n"
          'Return JSON {{"is_correction":bool,"durable_rule":bool,"rule":"<reusable rule in Korean or empty>"}}. '
          "is_correction = the user corrects or rejects the assistant's previous behavior or output. "
          "durable_rule = it implies a lasting preference or rule for future work beyond this task.")


def classify(prompt: str, conf: dict) -> dict:
    body = {"model": conf["model"], "reasoning_effort": "low", "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": PROMPT.format(p=prompt[:800])}]}
    headers = {"Content-Type": "application/json"}
    if conf["key"]:
        headers["Authorization"] = "Bearer " + conf["key"]
    req = urllib.request.Request(conf["url"], data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        content = json.loads(r.read())["choices"][0]["message"]["content"] or ""
    return json.JSONDecoder().raw_decode(content[content.find("{"):])[0]


def main():
    ev = json.loads(sys.stdin.read() or "{}")
    conf = _llm_conf()
    if not (conf["url"] and conf["model"]):
        return
    v = classify(ev.get("prompt", ""), conf)
    if not (v.get("is_correction") and v.get("durable_rule") and (v.get("rule") or "").strip()):
        return
    os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
    if os.path.exists(QUEUE) and os.path.getsize(QUEUE) > MAX_QUEUE_BYTES:
        os.replace(QUEUE, QUEUE + ".1")
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "sid": ev.get("sid"), "cwd": ev.get("cwd"),
           "prompt_sha": hashlib.sha256(ev.get("prompt", "").strip().encode()).hexdigest()[:16],
           "rule": v["rule"].strip()[:300], "model": conf["model"]}
    with open(QUEUE, "a", encoding="utf-8") as f:
        sb_lock.lock_with_timeout(f, sb_lock.LOCK_EX, 5)
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sb_lock.lock(f, sb_lock.LOCK_UN)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if os.environ.get("SB_RECALL_DEBUG") == "1":
            sys.stderr.write(f"sb-correction: {type(e).__name__}: {e}\n")
