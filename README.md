# secondbrain-kit

Claude Code와 Codex 세션에서 쌓인 작업 기록을 **다음 세션의 에이전트가 알아서 꺼내 쓰게** 만드는 로컬 메모리 시스템.
[claude-mem](https://github.com/thedotmack/claude-mem)이 세션을 기록하고, 이 키트가 그 위에 한국어 검색·확정 사실 관리·자동 주입·에이전트 규칙을 얹는다. 모든 데이터는 내 PC에만 있다.

## 무엇이 달라지나

| 시점 | 에이전트가 받는 것 |
|---|---|
| 세션 시작 | 이 폴더(scope)의 **재개 브리핑** — 사용자가 확정한 사실, 열린 미결과 다음 행동 |
| 세션 시작 | claude-mem의 최근 작업 타임라인(ID·제목 25건) |
| 매 프롬프트 | 관련 있어 보이는 과거 기록 **ID·제목 최대 5줄** (본문은 필요할 때 에이전트가 가져감) |
| 질문하기 직전 (Claude) | 이 세션에서 기억을 한 번도 안 찾아봤으면 질문을 한 번 멈추고 먼저 찾게 함 |
| 작업을 마칠 때 | 규칙에 따라 결론을 `sb save`로 남기고, 사용자 결정은 상태층에 확정 사실로 기록 |

## 구조

```
[캡처]  Claude Code 훅 ─┐          Codex 훅(자동화 exec 제외) ─┐
                        └→ claude-mem worker → LLM이 도구 사용을 '관측'으로 요약
[저장]  ~/.claude-mem/claude-mem.db (SQLite) + Chroma 벡터(bge-m3, 다국어)
[파생]  ~/.secondbrain/index  한국어 형태소 색인(Kiwi) · 벡터 스냅샷
[상태]  ~/.secondbrain/state.db  "지금 참인 사실" — 제안 → 검증 → 채택 (근거가 있어야 채택)
        ~/.secondbrain/loops     미결
[주입]  세션 시작 브리핑 · 프롬프트별 회수 · 회상 게이트
[규칙]  ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md 의 "세컨브레인 기억 규칙" 블록
```

- **관측 로그(claude-mem)** 는 "그때 무슨 일이 있었나", **상태층** 은 "지금 무엇이 참인가"를 맡는다. 사용자 결정은 실제 발화를 인용해 검증된 뒤에만 `user-confirmed`로 채택된다.
- 검색은 한국어 형태소 색인과 벡터를 RRF로 합친다. claude-mem 기본 임베딩(영어 전용 MiniLM)은 한국어 회수율이 거의 0이라, 설치 때 Ollama `bge-m3`로 컬렉션을 미리 만든다.

## 요구사항

- macOS 또는 Windows 10/11
- Claude Code 또는 Codex CLI(0.128+) 중 하나 이상 — 로그인된 상태
- Node.js 20.12+, git (Windows는 Git for Windows 필수 — Claude Code가 Git Bash로 훅을 돌린다)
- 디스크 약 3GB(모델 1.2GB + 파이썬 패키지), 설치 중 인터넷
- 관측 요약용 LLM: 기본은 **Claude 구독**(claude CLI 로그인). Claude가 없으면 `--provider gemini`(무료 키 가능) 또는 `--provider openrouter` — 키는 설치 전에 `~/.claude-mem/settings.json`에 넣어 둔다.

uv, Ollama, bun은 없으면 설치기가 설치한다.

## 설치 (한 줄)

```sh
curl -fsSL https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.sh | sh      # macOS
```
```powershell
irm https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.ps1 | iex           # Windows
```

단계별 안내·문제 해결은 [SETUP.md](SETUP.md).

설치 옵션(`sh -s -- <옵션>` / 윈도우는 `$env:SBKIT_ARGS`): `--no-embed`(한국어 임베딩 생략) · `--no-codex` · `--no-schedule` · `--with-consolidate`(주간 LLM 통합 요약) · `--provider gemini|openrouter` · `--lang en` · `--dry-run`

설치가 하는 일 (모두 멱등, 바꾸는 설정 파일은 `.sbkit-bak-<시각>`으로 백업):
1. `~/.secondbrain/.venv` 생성, 상태층 DB 초기화
2. `npx claude-mem install` — Claude Code 플러그인 등록, bun/uv 설치
3. `ollama pull bge-m3`, Chroma 컬렉션을 다국어 임베딩으로 선생성, `~/.chroma_env`
4. claude-mem 설정(한국어 관측 모드, 주입량) — 이미 있는 값은 건드리지 않음
5. Claude Code 훅 4종 (`~/.claude/settings.json`) — 기존 훅은 보존
6. Codex: claude-mem 플러그인(검색 MCP·스킬) 설치, 플러그인 훅은 끄고 키트의 캡처 훅으로 대체(`codex exec` 자동화 제외), **훅 신뢰 등록**까지 자동
7. 규칙 블록 삽입, `sb` 명령 설치, 스케줄 등록(한국어 색인 매시, 야간 정리 05:07)

설치 뒤: `./install.sh --doctor` 로 전 항목 점검 → 새 Claude/Codex 세션을 열고 아무 도구나 한 번 쓰면 캡처가 시작된다.

## 매일 쓰는 명령 (`sb`)

```sh
sb search --mode current            # 이 폴더의 확정 사실
sb search --mode next               # 이 폴더의 미결
sb search '배포 실패 원인' --global   # 과거 경위 검색
sb save --title "무엇을 했나" --text "결론·근거·남은 일"
sb loops add|list|close <id>
sb prompt-id '<내가 한 말 일부>'      # 결정 검증용 프롬프트 ID
sb scope                            # 이 폴더가 어떤 scope로 인식되는지
```

에이전트는 규칙 블록을 보고 이 명령들을 스스로 쓴다. 사람이 직접 칠 일은 드물다.

**scope 이름 바꾸기**: 기본 scope는 폴더 경로(또는 git remote)다. 짧은 이름을 쓰려면 `~/.secondbrain/config/project_aliases.json`에 `{"myapp": ["/Users/me/code/myapp"]}` 형태로 등록한다.

## Codex 관련 참고

- Codex는 해시로 신뢰한 훅만 실행한다. 설치기가 신뢰 등록까지 하지만, 훅 명령이 바뀌거나 키트 위치를 옮기면 다시 필요하다: `python installer/codex_trust.py trust --match "<키트 경로>"` (현황: `... list`)
- `codex exec` 자동화 세션은 캡처·주입하지 않는다(판정: 세션 로그 첫 줄의 originator). 자동화도 기록하려면 `SB_CODEX_CAPTURE_EXEC=1`.
- 전역 `~/.codex/AGENTS.override.md`가 있으면 `AGENTS.md`의 규칙 블록이 무시된다.

## 제거

```sh
./install.sh --uninstall     # 훅·규칙 블록·스케줄·sb 제거. 데이터(~/.secondbrain, ~/.claude-mem)는 남김
claude plugin uninstall claude-mem@thedotmack
codex plugin remove claude-mem@claude-mem-local
```

## 이미 claude-mem을 쓰고 있다면

기존 벡터 컬렉션이 기본(영어) 임베딩이면 설치기는 **자동으로 바꾸지 않고** 경고만 한다. 교체 절차는 [docs/embedding.md](docs/embedding.md).

## 검증 상태

- macOS: 전체 테스트 259개 통과, 격리 홈에서 실제 설치·재설치(멱등)·제거·doctor 확인, 실제 Codex 세션 캡처와 exec 제외 확인.
- Windows: 코드 경로(잠금·프로세스 종료·경로·스케줄러·Codex `commandWindows`)는 작성했지만 **실기기 미검증**. 처음 설치하는 Windows에서는 `--doctor` 결과를 꼭 확인할 것.
