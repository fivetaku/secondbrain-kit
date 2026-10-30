# secondbrain-kit

Claude Code와 Codex 세션에서 쌓인 작업 기록을 **다음 세션의 에이전트가 알아서 꺼내 쓰게** 만드는 로컬 메모리 시스템.
[claude-mem](https://github.com/thedotmack/claude-mem)이 세션을 기록하고, 이 키트가 그 위에 한국어 검색·확정 사실 관리·자동 주입·에이전트 규칙을 얹는다. 모든 데이터는 내 PC에만 있다.

## 무엇이 달라지나

| 시점 | 에이전트가 받는 것 |
|---|---|
| 세션 시작 | 이 폴더(scope)의 **재개 브리핑** — 사용자가 확정한 사실, 열린 미결과 다음 행동 |
| 세션 시작 | claude-mem의 최근 작업 타임라인(ID·제목 25건) |
| 매 프롬프트 | 관련 있어 보이는 과거 기록 **ID·제목 최대 5줄**, 상위 3건에는 요지 앞부분 (전문은 필요할 때 에이전트가 가져감) |
| 도구 호출이 실패했을 때 (Claude Code 2.1.119 이상) | 같은 오류를 다룬 과거 기록 최대 2건 — [오류 시점 회수](#오류-시점-회수) |
| 사용자가 교정했을 때 (선택, LLM 설정이 있을 때만) | 오래 지킬 규칙으로 보이는 교정 후보. 다음 프롬프트에 알리고, 그 세션에서 못 알렸으면 다음 세션 시작 브리핑에 — [교정 감지](#교정-감지-선택) |
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
        ~/.secondbrain/rules     scope별 규칙층 — Claude 자동 메모리 중 feedback 을 야간에 모은 생성물
[회수]  sb_recalld  상주 회수 서버(127.0.0.1:37791) — 색인을 메모리에 올려 두고 훅의 질의에 바로 답한다
[주입]  세션 시작 브리핑 · 프롬프트별 회수 · 오류 시점 회수 · 교정 감지(선택) · 회상 게이트
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

단계별 안내·문제 해결은 [SETUP.md](SETUP.md). 에이전트에게 세팅을 맡길 때는 "[docs/AGENT-CHECKLIST.md](docs/AGENT-CHECKLIST.md) 따라서 세팅해줘"라고 하면 된다(운영 매뉴얼: [AGENTS.md](AGENTS.md)).

설치 옵션(`sh -s -- <옵션>` / 윈도우는 `$env:SBKIT_ARGS`): `--no-embed`(한국어 임베딩 생략) · `--no-codex` · `--no-schedule` · `--with-consolidate`(주간 LLM 통합 요약) · `--provider gemini|openrouter` · `--lang en` · `--dry-run`

설치가 하는 일 (모두 멱등, 바꾸는 설정 파일은 `.sbkit-bak-<시각>`으로 백업):
1. `~/.secondbrain/.venv` 생성, 상태층 DB 초기화
2. `npx claude-mem install` — Claude Code 플러그인 등록, bun/uv 설치. 이어서 chroma-mcp 고정 도구 설치(`uv tool install … --with "pydantic<2.14" chroma-mcp==0.2.6`) — 2026-10-12에 uvx가 pydantic 2.14를 받아 오자 chroma-mcp 0.2.6이 시작하자마자 ImportError로 죽고 벡터 동기화가 4시간 넘게 멈췄다. claude-mem 워커는 자기 인자로 chroma-mcp를 띄우지만, 조건에 맞는 설치 도구가 있으면 uvx가 그것을 다시 쓰므로 고정이 워커에도 걸린다([docs/embedding.md](docs/embedding.md#chroma-mcp-버전-고정))
3. `ollama pull bge-m3`, Chroma 컬렉션을 다국어 임베딩으로 선생성, `~/.chroma_env`
4. claude-mem 설정(한국어 관측 모드, 주입량) — 이미 있는 값은 건드리지 않음
5. Claude Code 훅 6종 (`~/.claude/settings.json`, 기존 훅은 보존): SessionStart · UserPromptSubmit · PreToolUse(AskUserQuestion) · Stop · SubagentStart · PostToolUseFailure. PostToolUseFailure(오류 시점 회수)는 Claude Code 2.1.119 이상에서만 걸고, 그보다 낮으면 5종이다 — 2.1.101 이전 버전은 모르는 이벤트가 하나라도 있으면 settings.json 전체를 무시하기 때문이다. 같은 역할의 사용자 훅이 이미 있으면 그 이벤트는 건너뛴다
6. Codex: claude-mem 플러그인(검색 MCP·스킬) 설치, 플러그인 훅은 끄고 키트의 캡처 훅으로 대체(`codex exec` 자동화 제외), **훅 신뢰 등록**까지 자동. 키트 훅은 SessionStart(브리핑)·UserPromptSubmit(회수)이고, 오류 시점 회수·회상 게이트는 Claude 전용이다
7. 규칙 블록 삽입, `sb` 명령 설치, 스케줄 등록(한국어 색인 매시, 야간 정리 05:07)

상주 회수 서버(`sb_recalld`)는 스케줄로 띄우지 않는다. 세션 시작 훅·`sb recall`·`sb health --fix`가 꺼져 있으면 띄우고, 6시간 질의가 없으면 스스로 꺼진다.

설치 뒤: `./install.sh --doctor` 로 전 항목 점검 → 새 Claude/Codex 세션을 열고 아무 도구나 한 번 쓰면 캡처가 시작된다.

## 매일 쓰는 명령 (`sb`)

```sh
sb search --mode current            # 이 폴더의 확정 사실
sb search --mode next               # 이 폴더의 미결
sb search --mode rules              # 이 폴더의 규칙층(rules/<scope>.md)
sb recall '배포 실패 원인'            # 빠른 회수(상주 서버) + 관련도 판정
sb search '배포 실패 원인' --global   # 과거 경위 검색(벡터 포함, 느림)
sb timeline --since 2026-10-01      # 기간별 과거 작업(세션 날짜 기준)
sb rules [--dry-run] [--json]       # 규칙층 다시 만들기(야간 배치가 매일 돈다)
sb context --query '<작업 설명>'      # 다른 에이전트에 넘길 맥락 블록
sb save --title "무엇을 했나" --text "결론·근거·남은 일"
sb loops add|list|close <id>
sb prompt-id '<내가 한 말 일부>'      # 결정 검증용 프롬프트 ID
sb scope                            # 이 폴더가 어떤 scope로 인식되는지
sb health [--fix]                   # 기록층이 막힌 곳이 없는지 점검(고칠 수 있으면 고침)
```

에이전트는 규칙 블록을 보고 이 명령들을 스스로 쓴다. 사람이 직접 칠 일은 드물다.

## 기록층 감시 (`sb health`)

기록이 들어가는 길이나 꺼내 쓰는 길이 막혀도 겉으로는 아무 일 없어 보인다(회수는 계속 되지만 새 기록이 안 쌓이거나, 낡은 판만 나온다). 그래서 막힌 곳을 알려 주고 스스로 고친다.

| 점검 | 실패하면 |
|---|---|
| claude-mem 워커, 관찰기 프록시(`~/.claude-mem/.env`의 `ANTHROPIC_BASE_URL`이 있을 때) | 새 기록이 생기지 않는다 |
| Ollama(설치기가 bge-m3 임베딩을 켠 PC만) | 의미 검색에 새 기록이 안 잡힌다 |
| 야간 배치 결과·신선도 | 메모리 동기화·색인이 밀린다 |
| 메모리 동기화 대기 · 벡터 미반영 · 한국어 색인 지연 | 회수 결과가 낡은 판에 머문다 |
| 훅 대기열 정체·만료 | 세션 기록이 유실된다 |
| Chroma 대기분(`~/.claude-mem/chroma-sync-state.json`의 pending 합계, 200건 이상) | 의미 검색에 새 기록이 안 잡힌다. 원인을 없앤 뒤 claude-mem 워커를 재시작해야 채워진다(자동 복구 없음) |
| 워커 로그의 Chroma 장애 줄(최근 6시간 20건 이상) | 벡터 동기화가 계속 실패하고 있다. 이미 끝난 장애가 이틀 내내 경고로 뜨지 않게 최근 6시간만 본다 |
| 회수 자가 시험(최근 기록을 제목으로 회수해 되돌아오는지. 회수 서버가 꺼져 있으면 `--fix`가 띄운다) | 프롬프트별 회수 주입이 빈손이 된다 |
| 상태층에 안 올라간 `[…·결정]` 저장 수 | 다음 세션이 같은 질문을 반복한다 |

- 세션 시작 브리핑에 `⚠ 기록층 점검: …` 한 줄이 붙고, 고칠 수 있으면 백그라운드로 `sb health --fix`가 돈다. 야간 배치 마지막 단계도 결과를 `~/.secondbrain/logs/health.json`에 남긴다.
- 벡터 미반영이 문턱 이상이고 서비스(워커·임베딩 서버)가 살아 있으면 `--fix`가 하루 1회 워커를 재시작해 백필한다(워커는 시작할 때만 백필한다). 재시작 전후 워커 pid가 바뀌어야 `vector-backfill:ok`, 그대로면 `noop`이다. 상태는 `$SB_HOME/logs/worker-restart.json`(날짜·결과).
- `sb health`는 최근 7일 회수 활용(주입 수·빈 결과·확인 게이트 판정)도 보여 준다. 원자료는 `logs/recall.jsonl`, `logs/recall-gate.jsonl`.
- 끄기: `SB_HEALTH=0`(세션 시작 점검 전부. `sb health`·야간 health 단계는 그대로 돈다) · `SB_HEALTH_AUTOFIX=0`(자동 복구만) · `SB_WORKER_AUTOSTART` / `SB_OLLAMA_AUTOSTART` / `SB_PROXY_AUTOSTART=0`(개별 기동)

## 회수 서버·오류 회수·교정 감지

### 상주 회수 서버 (`sb_recalld`)

회수 훅은 프롬프트마다 새 파이썬 프로세스로 뜬다. 한국어 형태소 분석기(Kiwi) 적재와 색인 동기화에 몇 초가 걸려 매번 직접 검색하면 프롬프트가 4~6초 늦어진다. 그래서 상주 서버가 색인을 메모리에 올려 두고, 훅은 로컬 HTTP(`127.0.0.1:37791`)로 묻는다.

- 형태소 색인과 벡터 스냅샷을 합친 뒤 관련도 문턱을 넘는 결과만 주입한다(질의 명사가 1위 기록에 3개 이상, 또는 2개 이상이면서 질의의 60% 이상 일치. 겹친 단어 중 드문 단어가 하나는 있어야 한다).
- 재순위(선택): venv에 `sentence-transformers`가 있고 재순위 모델(기본 `nlpai-lab/KURE-Reranker-nano`)이 로컬 캐시에 있을 때만 켜진다. 키트는 둘 다 설치하지 않으며, 없으면 합친 순위를 그대로 쓴다.
- 기동·종료: 세션 시작 훅·`sb recall`·`sb health --fix`가 꺼져 있으면 띄운다. 6시간 질의가 없으면 스스로 꺼진다. doctor의 `회수 상주 서버 ⚠ 꺼짐`은 새 세션을 열면 사라진다.
- 대기 시간: 프롬프트별 회수는 서버 응답을 3초(`SB_RECALLD_TIMEOUT`) 기다린다. 2026-10-20 재순위를 켠 뒤 질의당 0.4~0.9초가 실측돼, 예전 1.5초로는 느린 PC에서 결과가 버려졌다. 서버가 아직 안 떴거나 답하지 않으면 그 프롬프트만 claude-mem 워커 검색으로 대신한다. 둘 다 실패하면 '관련 기록 없음'과 구분되는 `[세컨브레인 회수 실패]` 안내를 넣는다.

### 오류 시점 회수

Claude Code에서 도구 호출이 실패하면(PostToolUseFailure, Claude Code 2.1.119 이상) `hooks/sb_error_recall.py`가 오류 문구에서 경로·숫자·해시를 걷어낸 서명으로 회수 서버에 묻고, 같은 문제를 다룬 과거 기록을 최대 2건 붙인다. 원 PC 실측에서 조치가 필요한 도구 오류의 44%가 다른 세션에서 이미 나온 서명이었다.

- 권한 거절·사용자 중단처럼 고칠 것이 없는 실패와 짧은 오류는 건너뛴다. 한 세션에서 같은 서명은 한 번만, 이미 보여 준 기록은 다시 넣지 않는다.
- 회수 서버가 꺼져 있으면 아무것도 붙이지 않는다(3초 제한, 오류 시 조용히 끝남).
- 기록: `~/.secondbrain/logs/error_recall.jsonl`(서명 해시와 기록 ID만, 오류 본문 없음). 끄기 `SB_ERROR_RECALL=0`.

### 교정 감지 (선택)

사용자 발화가 교정처럼 보이면("아니야", "하지 마", "앞으로는" 같은 표현) 회수 훅이 `hooks/sb_correction.py`를 백그라운드로 띄운다. 판정기는 OpenAI 호환 LLM에 "오래 지킬 규칙을 담은 교정인가"를 묻고, 그렇다면 후보 대기열(`~/.secondbrain/index/correction_candidates.jsonl`)에 쌓는다. 같은 세션의 다음 프롬프트에 `[교정 감지]`로 최대 3건 알리고, 세션이 끝나 못 알린 후보는 같은 폴더의 다음 세션 시작 브리핑에 한 번 보여 준다(최근 7일, 최대 3건).

**기본은 꺼져 있다.** LLM 주소와 모델이 둘 다 있어야 판정하고, 없으면 판정기가 아무것도 하지 않는다. 켜려면 둘 중 하나를 설정한다(환경변수가 파일보다 우선).

- 파일 `~/.secondbrain/index/correction_llm.json`: `{"url": "<OpenAI 호환 /chat/completions 전체 주소>", "model": "<모델 이름>", "key": "<필요할 때만>"}`
- 환경변수 `SB_CORRECTION_LLM_URL`, `SB_CORRECTION_MODEL`, `SB_CORRECTION_LLM_KEY`

키는 명령줄에 쓰지 말고 파일에 둔다. 판정은 교정 표현이 들어간 발화에서만 LLM을 한 번 부르고, 결과는 대기열에만 쓴다(claude-mem·상태층에는 쓰지 않는다). 후보를 규칙으로 남길지는 에이전트가 사용자와 정한다. 끄기 `SB_CORRECTION=0`.

## 규칙층과 맥락 블록

- **규칙층 (`sb rules`)**: Claude Code 자동 메모리 중 `type: feedback`(사용자 교정·선호) 파일을 scope별 `~/.secondbrain/rules/<scope>.md`로 모은다. `sb search --mode rules`가 이것을 읽고, 야간 배치의 `rules` 단계가 매일 다시 만든다(실패해도 배치는 partial일 뿐 핵심 실패가 아니다). 정본은 원래 메모리 파일이고 rules 파일은 생성물이다 — 첫 줄 표식이 있는 파일만 덮어쓰거나 지우고, 손으로 쓴 파일은 그대로 둔다. 파일당 약 19KB, 규칙 하나는 800자까지 옮긴다(`--mode rules`가 앞 20KB만 읽는다). scope는 그 프로젝트 세션 기록에 남은 실제 작업 폴더로 정한다. Claude Code가 오래된 대화 기록을 정리해 세션 기록이 없어진 프로젝트는 전에 만든 rules 파일(규칙마다 붙은 `출처:` 줄)의 scope를 이어 써서 규칙이 사라지지 않게 하고, 그런 파일도 없을 때만 건너뛴다. 임시 폴더에서 연 프로젝트·수집 제외 프로젝트는 건너뛰고, Claude Code 프로젝트 폴더 자체가 안 보이면(`no_source`) 기존 생성물을 지우지 않는다. 바꾸기 전에 `sb rules --dry-run --json`으로 확인할 수 있다.
- **맥락 블록 (`sb context`)**: 다른 에이전트나 외부 작업자에게 일을 넘길 때 프롬프트 앞에 붙일 블록을 만든다(읽기 전용). 순서는 확정 사실 → 다음 행동 → 규칙 제목 → 관련 기록(회수 서버)이고, 줄마다 출처 ID를 붙여 `--max-bytes`(기본 6000) 안으로 자른다. 밖으로 나가는 글이라 금액·이메일·전화번호·계좌류 숫자는 가리되, 이 블록이 내는 출처 ID(관련 기록 줄 머리의 `#관측번호`, 확정 사실 출처의 `observation:N`·`prompt:N`)·날짜·'3원칙' 같은 낱말은 남긴다(`acct:12345678`처럼 다른 라벨 뒤나 본문 중간 `#` 뒤의 긴 숫자는 가린다). 회수 서버가 꺼져 있어도 나머지 층으로 만들고, 조회에 실패한 층(`sb search --mode`가 `error`·`unavailable`을 내거나 실패 종료한 경우)은 '없음'과 구분해 표시한다. 다만 설치 직후 상태층 DB·미결 원장·규칙 폴더를 아직 만들지 않은 것(`state_db_missing`·`loops_file_missing`·`rules_dir_missing`)뿐이면 '없음'으로 본다. `sb context --query '<작업 설명>' [--scope S] [--project P] [--max-bytes N]`

## 설정 (환경변수)

훅에 적용하려면 Claude Code는 `~/.claude/settings.json`의 `env`, Codex는 Codex를 띄우는 셸 환경에 둔다. 스케줄 작업(맥 launchd)은 설치기가 넣은 `SB_HOME`·`PATH`·`SB_RECALL=0`만 받는다. 아래 값은 모두 코드의 기본값이다.

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `SB_HOME` | `~/.secondbrain` | 키트 데이터 폴더(색인·상태층·미결·규칙층·로그·venv). 설치기가 `~/.claude/settings.json`의 `env`에 적는다. 바꾸려면 그 값으로 설치기를 다시 돌린다 |
| **회수** | | |
| `SB_RECALL` | `1` | `0`이면 프롬프트별 회수를 끈다(자동화·배치용) |
| `SB_RECALL_MAX` | `5` | 회수 목록 최대 줄 수 |
| `SB_RECALL_BODIES` | `3` | 요지를 붙일 상위 건수. `0`이면 제목만 |
| `SB_RECALL_BACKEND` | `recalld` | `worker`면 상주 서버 대신 claude-mem 워커 의미 검색(관련도 문턱 없음) |
| `SB_RECALL_SKIP_PREFIXES` | 없음 | 회수를 건너뛸 프롬프트 접두어, 쉼표로 구분(자동화 루프 등) |
| `SB_RECALL_AUTO_TITLE`, `SB_RECALL_AUTO_PROMPT` | `daily brief`·`데일리 브리핑` 류 / `브리핑`·`brief`·`데일리`·`daily` | 정규식. 정기 자동 실행(아침 브리핑 등) 관측의 제목 패턴 / 그 주제를 묻는 프롬프트 패턴. 그 주제를 물을 때만 회수 목록에 넣는다 |
| `SB_RECALLD_TIMEOUT` | `3` | 프롬프트별 회수가 상주 서버 응답을 기다리는 초. 느린 PC에서 회수가 자주 빈손이면 올린다(훅 제한 15초). 0 이하나 숫자가 아니면 3 |
| `SB_RECALLD_PORT` | `37791` | 상주 회수 서버 포트 |
| `SB_RECALLD_URL` | 없음 | 오류 시점 회수와 `sb context`가 쓸 회수 서버 주소(없으면 `http://127.0.0.1:<SB_RECALLD_PORT>`). 프롬프트별 회수는 이 값을 쓰지 않는다 |
| `SB_RECALLD_IDLE_HOURS` | `6` | 이 시간 동안 질의가 없으면 서버가 스스로 꺼진다 |
| `SB_RECALLD_AUTOSTART` | `1` | `0`이면 훅·명령이 서버를 자동으로 띄우지 않는다 |
| `SB_RECALLD_RERANK` | `1` | `0`이면 재순위를 끈다(재순위 패키지·모델이 있을 때만 의미가 있다) |
| `SB_RERANK_MODEL` | `nlpai-lab/KURE-Reranker-nano` | 재순위 모델. 로컬 캐시에 있어야 한다(내려받지 않는다) |
| **오류 회수·교정 감지** | | |
| `SB_ERROR_RECALL` | `1` | `0`이면 오류 시점 회수를 끈다 |
| `SB_ERROR_RECALL_AB` | 없음 | `1`이면 세션 해시로 절반은 회수하지 않는 대조군으로 둔다(효과 측정용) |
| `SB_CORRECTION` | `1` | `0`이면 교정 판정기를 띄우지 않는다 |
| `SB_CORRECTION_LLM_URL`, `SB_CORRECTION_MODEL`, `SB_CORRECTION_LLM_KEY` | 없음 | 교정 판정 LLM(OpenAI 호환 `/chat/completions` 전체 주소·모델·키). 주소와 모델이 없으면 교정 감지는 꺼진 것과 같다. `index/correction_llm.json`보다 우선 |
| **세션 시작·회상 게이트** | | |
| `SB_SESSION_CONTEXT` | `1` | `0`이면 세션 시작 훅 전체를 끈다 |
| `SB_STATE_BRIEFING` | `1` | `0`이면 브리핑의 확정 사실·미결 부분을 뺀다 |
| `SB_STOP_GATE` | `1` | `0`이면 Stop 게이트(과거 맥락 질문에 회상 없이 끝내면 1회 되돌림)를 끈다 |
| **기록층 감시** | | |
| `SB_HEALTH` | `1` | `0`이면 세션 시작 때 기록층 점검을 하지 않는다 |
| `SB_HEALTH_AUTOFIX` | `1` | `0`이면 점검만 하고 자동 복구는 띄우지 않는다 |
| `SB_WORKER_AUTOSTART`, `SB_OLLAMA_AUTOSTART`, `SB_PROXY_AUTOSTART` | `1` | `0`이면 복구 때 그 서비스(claude-mem 워커·Ollama·관찰기 프록시)를 띄우지 않는다 |
| `SB_HEALTH_PENDING_WARN` | `200` | Chroma 대기분 경고 문턱(건) |
| `SB_HEALTH_CHROMA_LOG_WARN` | `20` | 최근 6시간 Chroma 장애 로그 경고 문턱(줄) |
| `SB_HEALTH_VECTOR_WARN`, `SB_HEALTH_KO_WARN`, `SB_HEALTH_DECISION_WARN` | `30`, `50`, `3` | 벡터 미반영·한국어 색인 지연·상태층 미등록 결정 저장의 경고 문턱(건) |
| **야간 배치·LLM** | | |
| `SB_NIGHTLY_PII` | 켜짐 | `0`이면 마스킹 모듈이 있어도 야간 실명 정리 단계를 끈다 |
| `SB_PII_BACKUP_KEEP` | `3` | 야간 실명 정리가 벡터를 고치기 전에 만드는 Chroma 사본의 보존 개수 |
| `SB_CONS_SINCE_DAYS`, `SB_CONS_TIMEOUT` | `7`, `7200` | `sb nightly --with-consolidate`로 돌리는 주간 통합 단계의 대상 일수와 제한 시간(초) |
| `SB_LLM_MODEL` | `sonnet` | 주간 통합 요약에 쓰는 claude CLI 모델 |
| `SB_LLM_FALLBACK_MODEL` | 없음 | 주 모델이 과부하일 때 claude CLI가 바꿔 쓸 모델(`--fallback-model`) |
| `SB_LLM_RETRIES` | `3` | LLM 호출이 실패(429·네트워크)할 때 최대 호출 횟수(첫 호출 포함). 응답 형식 오류는 따로 1회만 다시 부른다 |
| `SB_LLM_RETRY_WAIT` | `60` | 재시도 첫 대기(초). 실패할 때마다 두 배로 늘린다 |
| `SB_CLAUDE_BIN` | PATH에서 찾음 | claude CLI 경로 |
| **기타** | | |
| `SB_RULES_DIR` | `$SB_HOME/rules` | 규칙층 폴더(`sb rules`가 쓰고 `sb search --mode rules`가 읽는다) |
| `SB_PROJECT_ALIASES` | `$SB_HOME/config/project_aliases.json` | scope 별칭 파일 위치 |
| `SB_OLLAMA_URL` | `http://127.0.0.1:11434` | 기록층 감시가 확인하는 Ollama 주소 |
| `SB_RELABEL_KEEP` | `5` | 재분류할 때 만드는 DB 백업의 보존 개수 |
| `SB_SAVE_ALLOW_EXCLUDED` | 없음 | `1`이면 수집 제외 프로젝트에도 `sb save`를 허용한다(기본은 차단) |
| `SB_CODEX_CAPTURE_EXEC` | 없음 | `1`이면 `codex exec` 자동화 세션도 캡처한다 |
| `SB_CODEX_SKIP` | 없음 | `1`이면 그 Codex 세션을 캡처하지 않는다 |

파일로 두는 설정: `relabel_auto.json`의 `"extra_env_pattern"` — 이 PC에서만 쓰는 도구·프로그램 이름(업무 기록에서 자동 분리할 어휘).

**scope 이름 바꾸기**: 기본 scope는 폴더 경로(또는 git remote)다. 짧은 이름을 쓰려면 `~/.secondbrain/config/project_aliases.json`에 `{"myapp": ["/Users/me/code/myapp"]}` 형태로 등록한다.

## Codex 관련 참고

- Codex는 해시로 신뢰한 훅만 실행한다. 설치기가 신뢰 등록까지 하지만, 훅 명령이 바뀌거나 키트 위치를 옮기면 다시 필요하다: `python installer/codex_trust.py trust --match "<키트 경로>"` (현황: `... list`)
- `codex exec` 자동화 세션은 캡처·주입하지 않는다(판정: 세션 로그 첫 줄의 originator). 자동화도 기록하려면 `SB_CODEX_CAPTURE_EXEC=1`.
- 전역 `~/.codex/AGENTS.override.md`가 있으면 `AGENTS.md`의 규칙 블록이 무시된다.

## 제거

```sh
./install.sh --uninstall     # 훅·규칙 블록·스케줄·sb 제거, 꺼 두었던 claude-mem Codex 훅 복원. 데이터(~/.secondbrain, ~/.claude-mem)는 남김
claude plugin uninstall claude-mem@thedotmack
codex plugin remove claude-mem@claude-mem-local
```

## 이미 claude-mem을 쓰고 있다면

기존 벡터 컬렉션이 기본(영어) 임베딩이면 설치기는 **자동으로 바꾸지 않고** 경고만 한다. 교체 절차는 [docs/embedding.md](docs/embedding.md).

## 검증 상태

- macOS: 전체 테스트 423개 통과(스킵 4, 2026-10-30), 격리 홈에서 실제 설치·재설치(멱등)·제거·doctor 확인, 실제 Codex 세션 캡처와 exec 제외 확인.
- Windows: 코드 경로(잠금·프로세스 종료·경로·스케줄러·Codex `commandWindows`)는 작성했지만 **실기기 미검증**. 처음 설치하는 Windows에서는 `--doctor` 결과를 꼭 확인할 것. 사용자 폴더 이름에 공백이 있으면 Codex는 2026-07 이후 버전(0.15x)이어야 훅이 돈다.
