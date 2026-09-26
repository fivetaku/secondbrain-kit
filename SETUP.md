# 다른 PC에 세컨브레인 세팅하기

이 키트는 **기록이 아니라 파이프라인**만 옮긴다. 새 PC는 빈 기억에서 시작해, 그 PC에서 하는 Claude Code·Codex 작업이 쌓이면서 기억이 자란다.

소요 시간: 약 10~20분 (모델 다운로드 1.2GB 포함)

> 에이전트(Claude Code·Codex)에게 맡기려면: "https://github.com/fivetaku/secondbrain-kit 의 docs/AGENT-CHECKLIST.md 대로 이 PC에 세팅해줘"

---

## 0단계. 준비물 확인

새 PC에 아래가 있어야 한다. 없으면 먼저 설치한다.

| 필요한 것 | 맥 | 윈도우 |
|---|---|---|
| Claude Code 또는 Codex (로그인까지) | https://claude.com/claude-code · `npm i -g @openai/codex` | 같음 |
| Node.js 20.12 이상 | `brew install node` | `winget install OpenJS.NodeJS.LTS` |
| git | `brew install git` (보통 이미 있음) | `winget install Git.Git` (**필수** — Claude Code가 Git Bash를 쓴다) |

- Homebrew가 없는 맥: https://brew.sh 의 한 줄 명령으로 먼저 설치.
- uv(파이썬 도구), Ollama(한국어 임베딩), bun은 **설치기가 알아서 깐다.**
- 관측 요약은 기본으로 **Claude 구독**을 쓴다. Claude 없이 Codex만 쓰는 PC라면 4단계의 `--provider` 참고.

## 1단계. 한 줄 설치

```sh
# 맥 (터미널)
curl -fsSL https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.sh | sh
```
```powershell
# 윈도우 (PowerShell)
irm https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.ps1 | iex
```

키트를 `~/secondbrain-kit`에 받은 뒤 설치까지 이어서 한다. 화면에 [1]~[10] 단계가 지나가고 `완료.`가 나오면 끝.
**이 폴더는 설치 후에도 지우지 말 것**(훅이 여기 스크립트를 직접 부른다).

## 2단계. (필요할 때) 옵션 주기

```sh
# 맥: sh -s -- 뒤에 옵션
curl -fsSL https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.sh | sh -s -- --no-codex
```
```powershell
# 윈도우: 환경변수로
$env:SBKIT_ARGS='--no-codex'; irm https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.ps1 | iex
```

- `--no-codex` : Codex는 연결하지 않음
- `--no-embed` : 한국어 임베딩 생략(디스크 절약, 대신 한국어 의미 검색이 약함)
- `--with-consolidate` : 매주 일요일 LLM으로 주간 요약 기억 생성(구독 사용량 소모)
- `--dry-run` : 실제로 바꾸지 않고 무엇을 할지만 보여줌

git 없이 받고 싶으면 GitHub 페이지의 Code → Download ZIP 으로 받아 홈에 풀고 `./install.sh`(윈도우 `.\install.ps1`)를 실행해도 된다.

## 3단계. 점검

```sh
~/secondbrain-kit/install.sh --doctor          # 윈도우: ~\secondbrain-kit\install.ps1 --doctor
```

`❌`가 없으면 된 것이다. 설치 직후엔 아래 두 개가 `⚠`인 게 정상이다:
- `캡처 — 아직 없음` → 4단계를 하면 사라진다
- `sb 명령 — PATH 에 없음` → 새 터미널을 열면 사라진다

## 4단계. 첫 사용 확인

1. **새 터미널**을 열고, 아무 프로젝트 폴더에서 Claude Code(또는 Codex)를 켠다.
2. 파일 하나 읽게 하는 정도의 작업을 시킨다.
3. 다시 `--doctor` → `캡처 claude` 또는 `캡처 codex`가 `✅`로 바뀌면 파이프라인이 도는 것이다.
4. 다음 날 같은 폴더에서 "어제 뭐 했었지?"를 물어보면, 에이전트가 `sb search`로 찾아 답한다.

Codex만 쓰는 PC(Claude 구독 없음)라면 설치 전에 관측 요약 LLM을 정한다:
```sh
./install.sh --provider gemini       # Gemini 키 필요(무료 등급 가능)
./install.sh --provider openrouter   # OpenRouter 키 필요
```
키는 `~/.claude-mem/settings.json`에 넣는다(`CLAUDE_MEM_GEMINI_API_KEY` 또는 `CLAUDE_MEM_OPENROUTER_API_KEY`).

---

## 설치되면 이 PC에 생기는 것

| 위치 | 내용 |
|---|---|
| `~/.claude-mem/` | 세션 기록 DB·벡터 (claude-mem) |
| `~/.secondbrain/` | 한국어 색인, 확정 사실(state.db), 미결, 로그, 파이썬 환경 |
| `~/.claude/settings.json` | 훅 4종 추가 (기존 훅은 그대로) |
| `~/.codex/hooks.json`, `config.toml` | Codex 캡처·주입 훅 + 신뢰 등록 |
| `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` | "세컨브레인 기억 규칙" 블록 추가 (기존 내용은 그대로) |
| 자동 작업 | 한국어 색인 매시간, 야간 정리 05:07 (맥 launchd / 윈도우 작업 스케줄러) |

바뀐 설정 파일은 모두 `<파일>.sbkit-bak-<시각>`으로 백업된다.

## 문제가 생기면

| 증상 | 해결 |
|---|---|
| `--doctor`에 `Codex 훅 신뢰 ❌` | `~/.secondbrain/.venv/bin/python installer/codex_trust.py trust --match "<키트 폴더 경로>"` (윈도우는 `.venv\Scripts\python.exe`) |
| `Ollama ❌` | Ollama 앱을 실행(맥: `brew services start ollama`), `ollama pull bge-m3` |
| `벡터 임베딩 ⚠ default` | 원래 claude-mem을 쓰던 PC다. `docs/embedding.md` 절차로 교체 |
| 세션 시작 브리핑이 안 뜸 | 정상일 수 있다 — 확정 사실·미결이 하나도 없으면 아무것도 넣지 않는다 |
| 전부 되돌리고 싶음 | `./install.sh --uninstall` (기록 데이터는 남김) |

## 업데이트

1단계의 한 줄 명령을 다시 실행하면 최신 키트로 갱신(git pull)하고 재설치한다. 여러 번 돌려도 중복되지 않는다.
