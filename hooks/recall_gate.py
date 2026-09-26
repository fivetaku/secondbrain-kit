#!/usr/bin/env python3
"""회상 게이트 본체 — 묻기 전·고치기 전에 우리 기억층을 먼저 보게 하는 하한선.

배경: 이미 끝난 일이 기억층에 있는데 조회 없이 "할까요"를 다시 묻는 사고를 막는다.
규칙(CLAUDE.md)만으로는 안 지켜져서 훅으로 내린 하한선이다.

갈래:
  PostToolUse(Bash|PowerShell|claude-mem MCP) → 실제 회상이면 .recalled(+.recalled_at) 마커 기록
  Stop                        → sb_recall 이 남긴 .needs(과거 맥락 질문) 이후 회상이 없으면 1회 block
  SubagentStart               → 서브에이전트에 기록층 사용법 주입(회수 주입을 못 받으므로)
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

# Windows 기본 코드페이지(cp949)로 읽으면 한국어 프롬프트가 깨져 회수가 전부 빗나간다 — 훅 입출력은 UTF-8 고정.
for _s in (sys.stdin, sys.stdout):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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

RECALL_PAT = ("sb search", "sb state", "sb timeline", "sb loops", "sb_search", "sb_state", "sb_timeline",
              "sb_briefing", "claude-mem", "claude_mem", "mcp-search", "observations_fts", "user_prompts_fts", "recall")

EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")

# Stop: 과거 맥락이 걸린 질문(sb_recall 이 .needs 마커를 남김)에 기억층을 안 보고 답을 끝내려 할 때 1회 되돌린다.
STOP_MSG = (
    "기억층 조회 없이 답을 끝내려 합니다. 이 질문은 기간·이력 질문이라 과거 기록 확인이 필요합니다.\n"
    "  - 기간 질문: sb timeline --since YYYY-MM-DD [--until D]\n"
    "  - 주제: sb search '<주제>' --global --limit 5  →  관련 ID는 get_observations([ID])\n"
    "  - 확정 사실: sb search --mode current\n"
    "조회 결과로 답을 확인·보강하고 근거(#ID·파일·커밋)를 붙여 다시 답하세요. "
    "정말 무관하면 한 줄로 이유를 밝히고 끝내도 됩니다(이번 한 번만 멈춥니다)."
)

# SubagentStart: 서브에이전트는 UserPromptSubmit 회수 주입을 못 받는다 — 사용법을 직접 넣는다.
SUBAGENT_MSG = (
    "[세컨브레인 — 서브에이전트용] 이 PC의 과거 작업 기록층을 자유롭게 써라(읽기 전용, 0.2~수 초).\n"
    "- 과거 경위: `sb search '<주제>' --global --limit 5` (Bash/PowerShell 모두 `sb`), 본문은 claude-mem MCP get_observations([ID])\n"
    "- 기간별 목록: `sb timeline --since YYYY-MM-DD [--until D]`\n"
    "- 확정 사실·미결·규칙: `sb search --mode current|next|rules`\n"
    "과제에 과거 결정·상태·수치가 걸리면 추측 말고 먼저 조회하고, 결과에 근거(#ID·파일)를 적는다. "
    "과거 기록은 기록 당시 스냅샷이라 현재 상태 주장은 실측을 우선한다."
)


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
        sys.stdout.write(json.dumps({"hookSpecificOutput": payload}))  # ASCII 이스케이프(윈도우 인코딩)

    def touch(suffix: str) -> None:
        try:
            gate.mkdir(parents=True, exist_ok=True)
            (gate / (key + suffix)).touch()
        except OSError:
            pass

    def mtime(suffix: str) -> float:
        try:
            return (gate / (key + suffix)).stat().st_mtime
        except OSError:
            return 0.0

    def recalled() -> None:
        claim(".recalled")
        touch(".recalled_at")   # 프롬프트 단위 판정용(Stop 게이트) — 매 회상마다 갱신

    event = str(job.get("hook_event_name") or job.get("hookEventName") or "")

    # ── 서브에이전트 시작: 기록층 사용법 주입 ───────────────────────
    if event == "SubagentStart":
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SubagentStart", "additionalContext": SUBAGENT_MSG}}, ensure_ascii=False))
        return

    # ── Stop: 과거 맥락 질문인데 이번 프롬프트 이후 회상이 없으면 1회 되돌림 ──
    if event == "Stop":
        if job.get("stop_hook_active") or os.environ.get("SB_STOP_GATE", "1") == "0":
            return
        needs = mtime(".needs")
        if needs and mtime(".recalled_at") < needs:
            try:
                (gate / (key + ".needs")).unlink()   # 프롬프트당 1회만
            except OSError:
                pass
            sys.stdout.write(json.dumps({"decision": "block", "reason": STOP_MSG}, ensure_ascii=False))
        return

    # ── 회상 마커 기록 ───────────────────────────────────────────────
    low = tool.lower()
    if "recall" in low or "mcp-search" in low or "mcp__plugin_claude-mem" in tool:
        recalled()
        return
    if tool in SHELL_TOOLS:
        command = str(tool_input.get("command") or "")
        if any(pat in command for pat in RECALL_PAT):
            recalled()
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
