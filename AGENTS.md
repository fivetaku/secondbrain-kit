# secondbrain-kit — 에이전트 운영 매뉴얼

이 폴더는 **세컨브레인 기억 파이프라인의 설치본이자 관리 작업 공간**이다. 이 폴더에서 일하는 에이전트(Claude Code·Codex)는 이 문서를 기준으로 설치·점검·수리한다.
새 PC 세팅을 맡았다면 먼저 [docs/AGENT-CHECKLIST.md](docs/AGENT-CHECKLIST.md)를 순서대로 따른다.

## 시스템 한눈에

```
Claude Code 훅 ─┐   Codex 훅(hooks/codex_hook.py: exec 자동화 제외) ─┐
                └→ claude-mem worker(127.0.0.1:<포트>) → LLM이 도구 사용을 '관측'으로 요약
저장   ~/.claude-mem/claude-mem.db + ~/.claude-mem/chroma (bge-m3 via Ollama /v1)
파생   ~/.secondbrain/index/ko_fts.sqlite (한국어 색인), index/chroma-snapshot
상태   ~/.secondbrain/state.db (확정 사실: 제안→검증→채택), ~/.secondbrain/loops (미결)
주입   hooks/session_context.py (세션 시작 브리핑), hooks/sb_recall.py (프롬프트별 ID·제목), hooks/recall_gate.py (Claude 질문 전 회상 강제)
규칙   ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md 의 <!-- secondbrain-kit:begin/end --> 블록 (원문: templates/memory-rules.md)
배치   bin/nightly.py (매일 05:07: 색인·auto-memory 동기화·스냅샷), bin/sb_fts_ko.py build (매시간)
```

포트는 고정값이 아니다: claude-mem 기본 규칙은 `37700 + uid%100`(맥), 윈도우 37777. 실제 값은 `~/.claude-mem/worker.pid`의 `port`. 코드는 `bin/sb_config.worker_base_url()`로만 얻는다.

## 폴더 구성

| 경로 | 역할 |
|---|---|
| `bootstrap.sh` / `bootstrap.ps1` | GitHub 한 줄 설치 진입점(clone/pull → install) |
| `install.sh` / `install.ps1` | uv 확보 후 `installer/install.py` 실행 |
| `installer/install.py` | 설치·제거(`--uninstall`)·점검(`--doctor`). 모든 단계 멱등, 설정 파일은 `.sbkit-bak-<시각>` 백업 |
| `installer/doctor.py` | 설치 상태 점검(읽기 전용) |
| `installer/codex_trust.py` | Codex 훅 신뢰 등록·목록·끄기·되돌리기 (app-server JSON-RPC) |
| `bin/sb.py` | `sb` 단일 CLI |
| `bin/sb_config.py`, `bin/sb_lock.py` | 경로·포트·실행파일 기본값 / 맥·윈도우 공용 파일 잠금 |
| `bin/sb_*.py`, `loops.py`, `consolidate.py`, `nightly.py`, `sync_automemory.py` | 저장·검색·상태층·배치 |
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
sb scope                              # 현재 폴더가 어떤 scope 인지
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
| 저장 저널 | `~/.secondbrain/logs/memory_journal.jsonl` |
| 스케줄 로그(맥) | `~/.secondbrain/logs/<잡>.launchd.log` |

## 증상별 수리

| 증상 | 확인 | 조치 |
|---|---|---|
| 세션 기록이 안 쌓임(Claude) | worker 로그에 `INIT_COMPLETE` 없음, `runner-errors.log` | `~/.claude/settings.json`에 claude-mem 플러그인 활성(`enabledPlugins`), `npx claude-mem@13.24.23 repair` |
| 세션 기록이 안 쌓임(Codex) | `codex_trust.py list`에서 키트 훅이 `trusted`인지 | 미신뢰면 `codex_trust.py trust --match "<키트 경로>"`. exec 세션은 설계상 제외(`SB_CODEX_CAPTURE_EXEC=1`로 강제) |
| 특정 폴더만 안 쌓임 | 로그에 `Project excluded from tracking` | `~/.claude-mem/settings.json`의 `CLAUDE_MEM_EXCLUDED_PROJECTS` |
| 한국어 의미 검색이 안 됨 | doctor `벡터 임베딩`, `Ollama` | Ollama 실행, `ollama pull bge-m3`, `~/.chroma_env`에 `CHROMA_OPENAI_API_KEY=ollama` |
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
