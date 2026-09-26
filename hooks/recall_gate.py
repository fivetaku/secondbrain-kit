#!/usr/bin/env python3
"""회상 게이트 본체 — 묻기 전·고치기 전에 우리 기억층을 먼저 보게 하는 하한선.

배경: 이미 끝난 일이 기억층에 있는데 조회 없이 "할까요"를 다시 묻는 사고를 막는다.
규칙(CLAUDE.md)만으로는 안 지켜져서 훅으로 내린 하한선이다.

세 갈래:
  PostToolUse(Bash)           → 명령이 실제 회상이면 .recalled 마커 기록
  PreToolUse(AskUserQuestion) → 마커 없으면 1차 deny(재시도는 통과). 질문은 나가는 순간이 사고다
  PreToolUse(Edit|Write)      → 마커 없으면 세션 1회 넛지만(차단 아님)

한계: 세션 단위로만 본다. 세션 초반의 무관한 회상 1회로 이후가 조용해진다.
      대상 단위 판정은 CLAUDE.md 회상 게이트(규범)가 맡는다.

IMPORTANT: 어떤 경로로도 exit 0. 차단 사고를 내지 않는다.
"""
import hashlib
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import sb_config  # noqa: E402

SB = "sb search"

ASK_MSG = (
    "✋ 되묻기 전에 회상부터: 이 세션에서 기억층을 한 번도 안 봤습니다.\n"
    "  1) 확정 사실(0.2초): " + SB + " --mode current --scope <프로젝트>\n"
    "  2) 과거 경위: 같은 스크립트에 검색어를 주고 --global --limit 5, 또는 claude-mem 검색\n"
    "이미 사용자가 결정했거나 끝난 일을 다시 묻는 것을 막는 장치입니다. 확인 후에도 "
    "사용자만 정할 수 있는 것이면 그대로 다시 호출하세요 — 이번 한 번만 멈추고 재시도는 통과합니다."
)

EDIT_MSG = (
    "\U0001f4dd 이 세션에서 기억층 조회 없이 파일 수정에 들어갑니다. 이어지는 작업이면 "
    + SB + " --mode current --scope <프로젝트> 로 확정 사실을 먼저 확인하세요(0.2초). "
    "이미 고친 것을 다시 고치거나 사용자 결정을 덮는 것을 막는 리마인더입니다."
)

RECALL_PAT = ("sb search", "sb state", "sb_search", "sb_state", "sb_briefing", "claude-mem", "claude_mem",
              "mcp-search", "observations_fts", "user_prompts_fts", "recall")

EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        return
    job = json.loads(raw)
    if not isinstance(job, dict):
        return

    sid = str(job.get("session_id") or job.get("sessionId")
              or os.environ.get("CLAUDE_SESSION_ID") or "")
    if not sid:
        return  # 세션 식별 불가 — 공유 마커로 영구 침묵하느니 조용히 통과

    key = hashlib.sha256(sid.encode()).hexdigest()
    gate = pathlib.Path(os.environ.get(
        "SB_RECALL_GATE_DIR", sb_config.sb_path("logs", "recall-gate")))
    tool = str(job.get("tool_name") or "")
    tool_input = job.get("tool_input") or {}

    def prune() -> None:
        """마커가 새로 생길 때만 7일 지난 다른 세션 마커를 지운다."""
        import time
        cutoff = time.time() - 7 * 86400
        try:
            for fp in gate.iterdir():
                try:
                    if fp.stat().st_mtime < cutoff:
                        fp.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def claim(suffix: str) -> bool:
        """원자적 생성. 이미 있으면 False — 병렬 호출에도 한 번만 통과한다."""
        try:
            gate.mkdir(parents=True, exist_ok=True)
            os.close(os.open(str(gate / (key + suffix)),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            prune()
            return True
        except OSError:
            return False

    def has(suffix: str) -> bool:
        try:
            return (gate / (key + suffix)).exists()
        except OSError:
            return False

    def emit(payload: dict) -> None:
        payload["hookEventName"] = "PreToolUse"
        sys.stdout.write(json.dumps({"hookSpecificOutput": payload}, ensure_ascii=False))

    # ── 회상 마커 기록 ───────────────────────────────────────────────
    low = tool.lower()
    if "recall" in low or "mcp-search" in low or "mcp__plugin_claude-mem" in tool:
        claim(".recalled")
        return
    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        if any(pat in command for pat in RECALL_PAT):
            claim(".recalled")
        return

    if has(".recalled"):
        return

    # ── 되묻기 차단 (세션 1회) ───────────────────────────────────────
    if tool == "AskUserQuestion":
        if claim(".asked"):
            emit({"permissionDecision": "deny", "permissionDecisionReason": ASK_MSG})
        return

    # ── 파일 수정 넛지 (차단 아님, 세션 1회) ─────────────────────────
    if tool in EDIT_TOOLS and claim(".nudged"):
        emit({"additionalContext": EDIT_MSG})


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — 훅은 어떤 이유로도 작업을 막지 않는다
        pass
    sys.exit(0)
