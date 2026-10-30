# secondbrain-kit — 에이전트 운영 매뉴얼

이 폴더는 **세컨브레인 기억 파이프라인의 설치본이자 관리 작업 공간**이다. 이 폴더에서 일하는 에이전트(Claude Code·Codex)는 이 문서를 기준으로 설치·점검·수리한다.
새 PC 세팅을 맡았다면 먼저 [docs/AGENT-CHECKLIST.md](docs/AGENT-CHECKLIST.md)를 순서대로 따른다.

## 시스템 한눈에

```
Claude Code 훅 ─┐   Codex 훅(hooks/codex_hook.py: exec 자동화 제외) ─┐
                └→ claude-mem worker(127.0.0.1:<포트>) → LLM이 도구 사용을 '관측'으로 요약
저장   ~/.claude-mem/claude-mem.db + ~/.claude-mem/chroma (bge-m3 via Ollama /v1)
파생   ~/.secondbrain/index/ko_fts.sqlite (한국어 색인), index/chroma-snapshot
상태   ~/.secondbrain/state.db (확정 사실: 제안→검증→채택), ~/.secondbrain/loops (미결), ~/.secondbrain/rules (규칙층, 생성물)
회수   bin/sb_recalld.py 상주 회수 서버(127.0.0.1:37791, 형태소 색인+벡터, 재순위는 패키지·모델이 있을 때만). 세션 시작·sb recall 때 뜨고 6시간 유휴면 종료
주입   hooks/session_context.py (세션 시작 브리핑·기록층 경고·못 알린 교정 후보), hooks/sb_recall.py (프롬프트별 ID·제목, 교정 감지 판정기 기동),
       hooks/sb_error_recall.py (Claude 도구 실패 시 같은 오류 기록), hooks/sb_correction.py (교정 판정, LLM 설정 없으면 꺼짐),
       hooks/recall_gate.py (Claude 질문 전·종료 전 회상 강제, 서브에이전트 안내)
규칙   ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md 의 <!-- secondbrain-kit:begin/end --> 블록 (원문: templates/memory-rules.md)
배치   bin/nightly.py (매일 05:07: [pii] → preflight → ko-index → automemory → rules → snapshot → health), bin/sb_fts_ko.py build (매시간)
```

키트가 거는 Claude Code 훅은 6종이다: SessionStart · UserPromptSubmit · PreToolUse(AskUserQuestion) · Stop · SubagentStart · PostToolUseFailure. PostToolUseFailure는 Claude Code 2.1.119 이상에서만 등록하므로 그보다 낮으면 5종이다(2.1.101 이전은 모르는 이벤트가 있으면 settings.json 전체를 무시한다). 키트는 도구 호출마다 뜨는 PostToolUse 훅을 두지 않는다(claude-mem 플러그인의 캡처 훅은 별개). Codex 쪽 키트 훅은 캡처(hooks/codex_hook.py)와 SessionStart·UserPromptSubmit이다.

야간 배치의 핵심 단계는 ko-index·automemory 둘이다. 이 둘이 실패할 때만 exit 1이고, 나머지(rules 포함)가 실패하면 status=partial, exit 0이다. pii는 마스킹 모듈이 있을 때만, consolidate는 `--with-consolidate`일 때만 돈다.

포트는 고정값이 아니다: claude-mem 기본 규칙은 `37700 + uid%100`(맥), 윈도우 37777. 실제 값은 `~/.claude-mem/worker.pid`의 `port`. 코드는 `bin/sb_config.worker_base_url()`로만 얻는다.

## 폴더 구성

| 경로 | 역할 |
|---|---|
| `bootstrap.sh` / `bootstrap.ps1` | GitHub 한 줄 설치 진입점(clone/pull → install) |
| `install.sh` / `install.ps1` | uv 확보 후 `installer/install.py` 실행 |
| `installer/install.py` | 설치·제거(`--uninstall`)·점검(`--doctor`). 모든 단계 멱등, 설정 파일은 `.sbkit-bak-<시각>` 백업. chroma-mcp 고정 도구(`uv tool install`, pydantic<2.14)도 여기서 깐다 |
| `installer/doctor.py` | 설치 상태 점검(읽기 전용) |
| `installer/codex_trust.py` | Codex 훅 신뢰 등록·목록·끄기·되돌리기 (app-server JSON-RPC) |
| `bin/sb.py` | `sb` 단일 CLI |
| `bin/sb_config.py`, `bin/sb_lock.py` | 경로·포트·실행파일 기본값 / 맥·윈도우 공용 파일 잠금 |
| `bin/sb_*.py`, `loops.py`, `consolidate.py`, `nightly.py`, `sync_automemory.py` | 저장·검색·상태층·배치 |
| `bin/sb_recalld.py` | 상주 회수 서버(`sb recall`, 회수 훅·오류 회수·`sb context`가 묻는 곳) |
| `bin/sb_rules.py` | 규칙층 생성(`sb rules`, 야간 rules 단계). feedback 자동 메모리 → `rules/<scope>.md`. 파일 이름 규칙은 `sb_search._rules_items`와 같아야 한다 |
| `bin/sb_context.py` | 외부 작업자에게 넘길 맥락 블록(`sb context`, 읽기 전용·마스킹) |
| `bin/sb_health.py` | 기록층 감시(`sb health`), 세션 시작 경고 줄과 자동 복구 |
| `hooks/` | 세션 훅 스크립트 |
| `templates/memory-rules.md` | 에이전트 규칙 블록 원문 |
| `tests/` | 단위·통합 테스트 |

## 자주 쓰는 명령

```sh
./install.sh --doctor                 # 전체 점검 (윈도우: .\install.ps1 --doctor)
./install.sh                          # 재설치/수리 (멱등)
./install.sh --uninstall              # 훅·규칙·스케줄 제거 (데이터 보존)
sb search --mode current              # 이 폴더 scope 의 확정 사실
sb search '<검색어>' --global --limit 5 --no-vector
sb recall '<검색어>'                   # 상주 회수 서버 경유 빠른 회수(꺼져 있으면 띄운다)
sb scope                              # 현재 폴더가 어떤 scope 인지
sb health [--fix]                     # 기록층 점검(고칠 수 있으면 고침)
sb rules --dry-run --json             # 규칙층이 어떻게 바뀔지 미리 보기
sb nightly                            # 야간 배치 수동 실행
python installer/codex_trust.py list  # Codex 훅 신뢰 상태 (venv 파이썬으로 실행)
```
venv 파이썬: 맥 `~/.secondbrain/.venv/bin/python`, 윈도우 `~/.secondbrain/.venv/Scripts/python.exe`.

## 로그·상태 파일 (문제 진단은 여기부터)

| 무엇 | 위치 |
|---|---|
| claude-mem worker 로그 | `~/.claude-mem/logs/claude-mem-<날짜>.log` (`INIT_COMPLETE`, `Project excluded` 검색) |
| claude-mem 훅 실패 | `~/.claude-mem/logs/runner-errors.log` |
| 관측 생성 LLM 상태 | `~/.claude-mem/observer-health.json` |
| 야간 배치 | `~/.secondbrain/logs/nightly.log`, `nightly_status.json` (status: ok/partial/failed/skipped) |
| 주입량 계측 | `~/.secondbrain/logs/injection.jsonl` (본문 없음) |
| 회수 활용·게이트 판정 | `~/.secondbrain/logs/recall.jsonl`, `logs/recall-gate.jsonl` |
| 오류 시점 회수 | `~/.secondbrain/logs/error_recall.jsonl` (서명 해시·기록 ID만) |
| 교정 후보 대기열 / 판정 LLM 설정 | `~/.secondbrain/index/correction_candidates.jsonl` / `index/correction_llm.json` |
| 기록층 점검 결과 | `~/.secondbrain/logs/health.json` |
| 저장 저널 | `~/.secondbrain/logs/memory_journal.jsonl` |
| 스케줄 로그(맥) | `~/.secondbrain/logs/<잡>.launchd.log` |

## 증상별 수리

| 증상 | 확인 | 조치 |
|---|---|---|
| 세션 기록이 안 쌓임(Claude) | worker 로그에 `INIT_COMPLETE` 없음, `runner-errors.log` | `~/.claude/settings.json`에 claude-mem 플러그인 활성(`enabledPlugins`), `npx claude-mem@13.24.23 repair` |
| 세션 기록이 안 쌓임(Codex) | `codex_trust.py list`에서 키트 훅이 `trusted`인지 | 미신뢰면 `codex_trust.py trust --match "<키트 경로>"`. exec 세션은 설계상 제외(`SB_CODEX_CAPTURE_EXEC=1`로 강제) |
| 특정 폴더만 안 쌓임 | 로그에 `Project excluded from tracking` | `~/.claude-mem/settings.json`의 `CLAUDE_MEM_EXCLUDED_PROJECTS` |
| 한국어 의미 검색이 안 됨 | doctor `벡터 임베딩`, `Ollama` | Ollama 실행, `ollama pull bge-m3`, `~/.chroma_env`에 `CHROMA_OPENAI_API_KEY=ollama` |
| 벡터 동기화 멈춤(새 기록이 의미 검색에 안 잡힘) | doctor `chroma-mcp 기동` ❌(eval_type_backport ImportError), `sb health`의 chroma-pending·chroma-log | `./install.sh` 재실행(chroma-mcp 고정 도구 설치). 밀린 대기분은 원인을 없앤 뒤 claude-mem 워커가 다시 떠야 채워진다 |
| 프롬프트별 회수가 자주 빈손 | doctor `회수 상주 서버`·`회수 훅 실동작`, `logs/recall.jsonl`의 실패 기록 | `sb recall '<아무 검색어>'`로 서버 기동 확인. 느린 PC면 `SB_RECALLD_TIMEOUT`(기본 3초)을 올린다 |
| 교정 감지 알림이 전혀 없음 | `index/correction_llm.json` 또는 `SB_CORRECTION_LLM_URL`·`SB_CORRECTION_MODEL` | 설정이 없으면 원래 꺼져 있다(정상). 켜는 법은 README "교정 감지" |
| 관측이 생성 안 됨 | `observer-health.json` 연속 실패 | provider 인증 확인(claude 로그인 / Gemini·OpenRouter 키), `CLAUDE_MEM_LLM_TIMEOUT_MS` 상향 |
| 브리핑이 안 뜸 | `sb search --mode current`, `sb search --mode next` | 확정 사실·미결이 없으면 원래 주입하지 않는다(정상) |
| 야간 배치 failed | `nightly_status.json`의 `failed_stages`, `nightly.log` | 해당 단계 명령을 수동 실행해 원인 확인 |

## 개발 규칙 (이 저장소를 고칠 때)

- **공개 저장소다.** 개인 경로·계정명·키·기록을 커밋하지 않는다. 테스트 픽스처도 `demo` 같은 중립 이름을 쓴다. 커밋 전 `git grep -n -i -E "Users/[a-z]+/|api[_-]?key\s*=" -- .`로 확인.
- 경로·포트·실행파일은 반드시 `sb_config`를 거친다(하드코딩 금지). 파일 잠금은 `sb_lock`(fcntl 직접 사용 금지).
- 맥·윈도우 양쪽을 생각한다: 경로 구분자, `os.pathsep`, 셸 차이, Codex `commandWindows`(윈도우 Codex는 `cmd.exe /C "<명령>"`으로 실행 — 역슬래시 경로, 공백 있을 때만 따옴표).
- 훅은 어떤 오류에도 exit 0 — 세션을 막지 않는다.
- Codex 훅 명령 문자열을 바꾸면 해시가 바뀌어 **재신뢰가 필요**하다(설치기가 처리). Codex hooks.json 항목은 기존 배열의 **맨 끝에만** 추가(신뢰 키가 인덱스 기반).
- 테스트: `~/.secondbrain/.venv/bin/python -m unittest discover -s tests -t .` (세션 환경변수 `SB_STATE_BRIEFING`이 새면 일부 테스트가 깨진다 — `env -u SB_STATE_BRIEFING`으로 실행).
- 사용자 데이터(`~/.claude-mem`, `~/.secondbrain`)를 지우거나 덮어쓰는 조작, 배포(push)는 사용자 확인 후에만.
