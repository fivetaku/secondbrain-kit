# 새 PC 세팅 체크리스트 (에이전트용)

사용자가 "이 PC에 세컨브레인 세팅해줘"라고 하면 이 순서대로 진행한다. 각 항목은 **명령으로 실측한 뒤** 체크한다. 추측으로 넘어가지 않는다.
끝나면 맨 아래 "보고 양식"으로 사용자에게 보고한다.

---

## A. 사전 점검 (설치 전, 읽기만)

- [ ] **OS 확인**: 맥 `sw_vers` / 윈도우 `[Environment]::OSVersion` → macOS 또는 Windows 10/11인가
- [ ] **하네스**: `claude --version`, `codex --version` 중 최소 하나. Codex는 0.128 이상
- [ ] **로그인**: Claude는 `claude` 실행 시 로그인 상태, Codex는 `codex login status`
- [ ] **Node 20.12+**: `node --version` — 없거나 낮으면 사용자에게 설치를 요청(맥 `brew install node`, 윈도우 `winget install OpenJS.NodeJS.LTS`)
- [ ] **git**: `git --version` — 윈도우는 Git for Windows 필수(Claude Code 훅이 Git Bash로 돈다)
- [ ] **관측 요약 LLM 결정**:
  - Claude 구독 있음 → 기본(`--provider claude`)
  - Codex만 있음 → 사용자에게 Gemini(무료 등급 가능) 또는 OpenRouter 키 중 선택을 물어본다. 키는 사용자가 `~/.claude-mem/settings.json`에 직접 넣게 한다(`CLAUDE_MEM_GEMINI_API_KEY` / `CLAUDE_MEM_OPENROUTER_API_KEY`). **키를 채팅으로 받지 않는다.**
- [ ] **기존 claude-mem 여부**: `~/.claude-mem/chroma` 가 이미 있으면 기존 사용자다 → 설치 후 D-4 확인 필수
- [ ] **AGENTS.override.md**: `~/.codex/AGENTS.override.md`가 있으면 규칙 블록이 무시된다 — 사용자에게 알린다
- [ ] **디스크**: 여유 3GB 이상

## B. 설치

- [ ] 한 줄 설치 실행
  - 맥: `curl -fsSL https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.sh | sh`
  - 윈도우: `irm https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.ps1 | iex`
  - 옵션(필요 시): 맥 `| sh -s -- --provider gemini`, 윈도우 `$env:SBKIT_ARGS='--provider gemini'` 먼저 설정
- [ ] 출력에 `[1]`~`[10]`이 모두 지나가고 `완료.`가 나왔는지
- [ ] 중간 실패 시: 실패한 단계 번호와 마지막 20줄을 확보 → "E. 실패 대응" 참고 → 고친 뒤 **같은 명령 재실행**(멱등)

## C. 설치 직후 점검

- [ ] `~/secondbrain-kit/install.sh --doctor` (윈도우 `~\secondbrain-kit\install.ps1 --doctor`)
- [ ] 아래가 모두 ✅인가
  - [ ] venv 패키지
  - [ ] claude-mem 설정 (mode=code--ko)
  - [ ] 벡터 임베딩 = `openai` (기존 사용자면 `ollama`도 정상)
  - [ ] Ollama bge-m3, `~/.chroma_env`
  - [ ] Claude 훅 4종, Claude claude-mem 플러그인 (Claude 쓰는 경우)
  - [ ] Codex 훅, **Codex 훅 신뢰 N개**, Codex claude-mem 플러그인 훅 = 꺼짐 (Codex 쓰는 경우)
  - [ ] 규칙 블록 (CLAUDE.md / AGENTS.md)
  - [ ] 상태층(state.db)
- [ ] 이 시점에 ⚠여도 되는 것: `claude-mem worker`(세션 열면 뜸), `캡처 … 아직 없음`, `sb 명령 PATH`(새 터미널에서 해결)

## D. 실제 동작 확인 (새 터미널에서)

- [ ] **D-1 Claude 캡처**: 아무 프로젝트 폴더(`/tmp`·Downloads 제외)에서 Claude Code를 켜고 파일 하나를 읽게 한다 → 세션 종료 → `--doctor`의 `캡처 claude` ✅
- [ ] **D-2 Codex 캡처**: 같은 방식으로 Codex **대화형**(TUI)에서 한 번 → `캡처 codex` ✅
  - `codex exec`로 시험하면 **설계상 기록되지 않는다**. 시험용 강제 기록: `SB_CODEX_CAPTURE_EXEC=1 codex exec "..."`
- [ ] **D-3 회수**: 그 폴더에서 새 세션을 열고 "방금 전에 뭐 했지?" → 에이전트가 `sb search` 또는 claude-mem 검색으로 찾아 답하는지
- [ ] **D-4 한국어 검색**: `sb search '<방금 작업 키워드(한국어)>' --global --limit 5` 에 결과가 나오는지. 기존 claude-mem 사용자인데 임베딩이 `default`면 `docs/embedding.md` 절차를 사용자에게 제안(자동 실행 금지 — 재색인 필요)
- [ ] **D-5 결정 기록**: 사용자에게 간단한 결정 하나(예: "이 프로젝트 기본 브랜치는 main")를 말하게 하고 → `sb prompt-id` → `sb state propose/verify/accept` → 새 세션에서 브리핑에 `user-confirmed`로 뜨는지
- [ ] **D-6 스케줄**: 맥 `launchctl list | grep secondbrain-kit` (koindex·nightly), 윈도우 `schtasks /Query /TN "secondbrain-kit\nightly"`

## E. 실패 대응

| 실패 위치 | 흔한 원인 | 조치 |
|---|---|---|
| [1] 필수 도구 없음 | node/git 미설치, Node 20.12 미만 | 사용자에게 설치 명령 안내 후 재실행 |
| [2] venv | uv 설치 직후 PATH 미반영 | 새 터미널에서 재실행 |
| [3] claude-mem | npm 네트워크, 권한 | `npx -y claude-mem@13.24.23 install --provider <p> --no-auto-start`를 직접 실행해 오류 확인 |
| [4] 임베딩 | Ollama 서버 미기동, 모델 다운로드 실패 | Ollama 앱 실행 → `ollama pull bge-m3` → 재실행. 급하면 `--no-embed` |
| [7] Codex | 신뢰 등록 실패 | venv 파이썬으로 `installer/codex_trust.py trust --match "<키트 경로>"` |
| 윈도우 전반 | PowerShell 실행 정책 | `powershell -ExecutionPolicy Bypass -File .\install.ps1` |
| 윈도우 훅 무반응 | Git Bash 없음, node가 시스템 PATH에 없음 | Git for Windows 설치, Node를 시스템 PATH로 재설치 후 새 창 |

되돌리기: `install.sh --uninstall` (데이터 보존). 각 설정 파일 옆 `*.sbkit-bak-<시각>`이 설치 전 원본이다.

## 하지 말 것

- 사용자 데이터(`~/.claude-mem`, `~/.secondbrain`) 삭제·덮어쓰기
- 기존 claude-mem 임베딩 컬렉션을 확인 없이 교체
- API 키를 채팅으로 받거나 명령에 평문으로 넣기
- Codex `hooks.json` 기존 항목 사이에 끼워 넣기(신뢰 키가 밀린다) — 설치기에 맡긴다

## 보고 양식

```
세팅 결과: 완료 / 일부 완료 / 실패
- 하네스: Claude Code <버전>, Codex <버전>
- 관측 요약 LLM: claude / gemini / openrouter
- doctor: ✅ N개, ⚠ N개(항목), ❌ N개(항목)
- 실동작: Claude 캡처 ✅/❌, Codex 캡처 ✅/❌, 한국어 검색 ✅/❌, 결정 기록 ✅/❌
- 남은 일: (사용자가 해야 할 것)
```
