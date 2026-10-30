#!/usr/bin/env python3
"""회상 게이트 본체 — 묻기 전·고치기 전에 우리 기억층을 먼저 보게 하는 하한선.

배경: 이미 끝난 일이 기억층에 있는데 조회 없이 "할까요"를 다시 묻는 사고를 막는다.
규칙(CLAUDE.md)만으로는 안 지켜져서 훅으로 내린 하한선이다.

갈래:
  PostToolUse(Bash|PowerShell|claude-mem MCP) → 실제 회상이면 .recalled(+.recalled_at) 마커 기록
  Stop                        → sb_recall 이 남긴 .needs(과거 맥락 질문) 이후 회상이 없으면 1회 block
  SubagentStart               → 서브에이전트에 기록층 사용법 주입(회수 주입을 못 받으므로)
  PreToolUse(AskUserQuestion) → 마커 없으면 1차 deny(재시도는 통과). 질문은 나가는 순간이 사고다
  PreToolUse(Edit|Write)      → 마커 없으면 세션 1회 넛지만(차단 아님) — 2026-09-27 등록 해제(호출마다 기동 비용)
  회상 여부는 이제 대화기록(transcript_path)에서 판정한다. PostToolUse 마커는 폴백으로만 남김.

한계: 세션 단위로만 본다. 세션 초반의 무관한 회상 1회로 이후가 조용해진다.
      대상 단위 판정은 CLAUDE.md 회상 게이트(규범)가 맡는다.

IMPORTANT: 어떤 경로로도 exit 0. 차단 사고를 내지 않는다.
"""
import hashlib
import json
import os
import re
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

# 회상은 '실행'이어야 한다(2026-10-21). 글자 포함만 보던 이전 방식은 `cat hooks/recall_gate.py`·`grep -rn recall`·
# `ls ~/.claude-mem` 처럼 기록층을 언급만 한 명령도 회상으로 세서, 실제 조회 없이 게이트가 풀렸다.
# 명령 자리(첫머리·줄바꿈·; & | ( ` $( 뒤)에서 기록층 명령을 부르거나 claude-mem DB 를 sqlite3 로 조회할 때만 센다.
# 2026-10-30: 실행인데 빠지던 꼴을 다시 넣는다 — 공백 든 따옴표 경로("C:\a b\bin\sb_search.py", '/x y/sb_search.py')와
# timeout·env·time·nohup·uv run 래퍼 접두. 거꾸로 따옴표 안 문자열(echo "a; sb search")·커밋 메시지 줄바꿈 뒤
# 'sb search' 는 is_recall_command 가 따옴표 상태로 걸러 낸다.
_CMD = r"(?:^|[;&|(`\n]|\$\()\s*"                                    # 명령 자리
_ASSIGN = r"(?:[A-Za-z_]\w*=(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'])*\s+)*"  # 앞에 붙은 환경변수 지정(따옴표 값 허용)
_WRAP = (r"(?:(?:[^\s\"';&|]*/)?(?:timeout(?:\s+-[ks]\s+\S+|\s+-\S+)*\s+\d[\d.]*[smhd]?"   # timeout [-k 5] 30
         r"|env(?:\s+-u\s+\S+|\s+-\S+)*|time(?:\s+-\S+)*|nohup"
         r"|uv\s+run(?:\s+--?[\w-]+(?:=\S+|\s+[^\s\-;&|][^\s;&|]*)?)*)\s+" + _ASSIGN + r")*")   # uv run [--project x]


def _tok(name: str) -> str:
    """경로 접두가 붙을 수 있는 실행 파일 토큰 — 공백 든 따옴표 경로("…\\a b\\…", '…/a b/…') 또는 맨 경로."""
    return (r"(?:\"(?:[^\"\n]*[/\\])?" + name + r"\"|'(?:[^'\n]*[/\\])?" + name + r"'"
            r"|(?:[^\s\"';&|]*[/\\])?" + name + r")")


# 인터프리터 옵션: 값을 따로 받는 -X·-W(윈도우에서 흔한 python -X utf8 …\bin\sb_search.py)와 나머지 -옵션
_PY = r"(?:" + _tok(r"py(?:thon[\d.]*)?(?:\.exe)?") + r"\s+(?:-[XW]\s+[^\s;&|-][^\s;&|]*\s+|-\S+\s+)*)?"   # python·py 로 스크립트를 돌리는 경우
RECALL_RUN = re.compile(
    _CMD + _ASSIGN + _WRAP + _PY
    + r"(?:" + _tok(r"sb(?:\.cmd|\.ps1|\.py)?") + r"\s+(?:search|state|timeline|recall|loops)\b"
    + r"|" + _tok(r"(?:sb_search|sb_state|sb_timeline|sb_briefing)\.py\b")
    + r"|" + _tok(r"loops\.py") + r"\s+list\b)"
    + r"|\bsqlite3\b[^|;&\n]*claude-mem\.db")
# heredoc 본문은 실행이 아니라 데이터다 — git commit -m "$(cat <<'EOF' … EOF)" 의 메시지 줄이 명령 자리로 읽히지 않게 지운다.
_HEREDOC = re.compile(r"(<<-?[ \t]*([\"']?)(\w+)\2[^\n]*)\n.*?\n[ \t]*\3[ \t]*(?=\n|\Z)", re.S)


def _scan_quotes(s: str, i: int, end: int, q: str, esc: str = "\\"):
    """s[i:end] 를 셸처럼 훑어 end 위치가 어느 따옴표 안인지('"'·"'"·'') 이어서 센다(후보마다 처음부터 다시 훑지 않게).
    esc 는 이스케이프 문자 — bash 는 백슬래시(따옴표 밖 don\\'t 의 \\' 도 따옴표를 열지 않는다),
    PowerShell 은 백틱(백슬래시는 그냥 경로 구분자라 "C:\\a b\\" 의 끝 \\" 가 따옴표를 닫는다).
    작은따옴표 안에는 두 셸 모두 이스케이프가 없다."""
    while i < end:
        c = s[i]
        if c == esc and q != "'":
            i += 2
            continue
        if c in "\"'" and (not q or q == c):
            q = "" if q else c
        i += 1
    return i, q


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


AGENT_TOOLS = ("Agent", "Task")
AGENT_RECALL_PAT = ("sb timeline", "sb search", "sb recall", "get_observations", "sb state", "기록층")


def is_recall_command(command: str, powershell: bool = False) -> bool:
    """셸 명령이 기록층을 실제로 조회하나(RECALL_RUN).
    명령 자리(; 줄바꿈 등)가 따옴표 안이면 문자열일 뿐이라 세지 않는다 — 단 큰따옴표 안의 $( ` 는 실제 명령 치환이라 센다.
    한계(거짓 음성 쪽으로 기운다): bash -c "sb search x"·ssh host "sb …" 처럼 따옴표 안에서 실행되는 것과
    bash <<EOF 로 넘기는 heredoc 속 명령은 못 센다. 거짓 양성은 게이트를 조회 없이 풀어 버리므로 그쪽을 줄였다.
    powershell=True 면 따옴표를 PowerShell 규칙(백틱 이스케이프, 큰따옴표 안 명령 치환은 $( 만)으로 센다."""
    esc, subst = ("`", ("$(",)) if powershell else ("\\", ("$(", "`"))
    s = _HEREDOC.sub(r"\1", command or "")
    pos, i, q = 0, 0, ""
    while True:
        m = RECALL_RUN.search(s, pos)
        if not m:
            return False
        i, q = _scan_quotes(s, i, m.start(), q, esc)
        # 명령 자리 문자(; | 등)가 이스케이프됐거나(a\; · a`;) PowerShell 의 백틱(명령 치환이 아니라 이스케이프) — 문자일 뿐이다
        if i > m.start() or (powershell and s.startswith("`", m.start())):
            pos = m.start() + 1
            continue
        if not q or (q == '"' and s.startswith(subst, m.start())):
            return True
        pos = m.start() + 1


def _is_recall_tool_name(name: str) -> bool:
    low = (name or "").lower()
    return "recall" in low or "mcp-search" in low or "claude-mem" in low or "claude_mem" in low


def _is_recall_tool(name: str, inp) -> bool:
    if _is_recall_tool_name(name):
        return True
    if name in SHELL_TOOLS:
        return is_recall_command(str((inp or {}).get("command") or ""), powershell=name == "PowerShell")
    if name in AGENT_TOOLS:   # 기록층 조회를 맡긴 서브에이전트 — 2026-10-05 오탐(에이전트 결과 전달 답을 되돌림) 보정
        prompt = str((inp or {}).get("prompt") or "")
        return any(pat in prompt for pat in AGENT_RECALL_PAT)
    return False


def _is_notification(content) -> bool:
    """사용자 발화가 아니라 백그라운드 작업 알림·시스템 메시지인 턴."""
    texts = [content] if isinstance(content, str) else [
        b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text"]
    head = " ".join(t.lstrip()[:200] for t in texts)
    return "<task-notification>" in head or "[SYSTEM NOTIFICATION" in head


def gate_log(event: dict) -> None:
    """게이트 판정 기록 — 효과 측정용(차단 뒤 답이 실제로 바뀌었는지는 다음 턴 회상 여부로 본다)."""
    try:
        import time
        path = pathlib.Path(sb_config.sb_path("logs", "recall-gate.jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def transcript_recall(path, since_last_prompt=True, max_bytes=4_000_000):
    """대화기록(JSONL)에 회상 도구 호출이 있었나. since_last_prompt=True 면 마지막 사용자 프롬프트 이후만.
    반환 True/False, 읽을 수 없으면 None(마커 방식 폴백).
    도구 호출마다 훅을 띄워 마커를 찍던 방식을 대체한다 — 이 PC는 프로세스 기동만 1.6~9초라 호출당 비용이 컸다."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return None
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        if ev.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and \
                        _is_recall_tool(block.get("name", ""), block.get("input")):
                    return True
        elif ev.get("type") == "user" and since_last_prompt and not ev.get("isMeta"):
            if isinstance(content, str) or (isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "text" for b in content)):
                if _is_notification(content):
                    return "notification"   # 사용자 질문이 아니라 알림 턴 — 게이트 대상 아님
                return False   # 마지막 사용자 프롬프트까지 거슬러 올라갔는데 회상 없음
    return False


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
        if not needs:
            return
        seen = transcript_recall(job.get("transcript_path"), since_last_prompt=True)
        if seen == "notification":
            gate_log({"event": "stop", "decision": "skip_notification", "sid": key[:12]})
            return
        if seen is None:   # 대화기록을 못 읽으면 예전 마커 방식
            seen = mtime(".recalled_at") >= needs
        try:
            (gate / (key + ".needs")).unlink()   # 프롬프트당 1회만 판정
        except OSError:
            pass
        gate_log({"event": "stop", "decision": "pass" if seen else "block", "sid": key[:12]})
        if not seen:
            sys.stdout.write(json.dumps({"decision": "block", "reason": STOP_MSG}, ensure_ascii=False))
        return

    # ── 회상 마커 기록 ───────────────────────────────────────────────
    if _is_recall_tool_name(tool):
        recalled()
        return
    if tool in SHELL_TOOLS:
        if is_recall_command(str(tool_input.get("command") or ""), powershell=tool == "PowerShell"):
            recalled()
        return

    if has(".recalled"):
        return
    if tool == "AskUserQuestion" and transcript_recall(job.get("transcript_path"), since_last_prompt=False):
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
