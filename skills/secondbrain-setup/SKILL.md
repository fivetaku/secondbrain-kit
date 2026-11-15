---
name: secondbrain-setup
description: 세컨브레인(secondbrain-kit) 셋업을 대화로 진행한다 — 설치 점검 → 자료 등록 → 분류·필터 → 레이어 구성(승인) → 검증의 다섯 단계를 `sb setup` 단계 엔진으로 돌리고, 멈춘 단계의 승인·판정·넘김을 사용자에게 받아 기록한다. 트리거 — "세컨브레인 셋업", "세컨브레인 깔아 줘", "secondbrain setup", "셋업 이어서 해 줘", "sb setup", "/secondbrain-setup". 단, 기록 검색·회수는 `sb search`/`sb recall`, 볼트 구성만 다시 하려면 `sb vault`.
---

# 세컨브레인 셋업 (에이전트 진행판)

터미널 설치기와 **같은 단계 엔진**(`sb setup`)을 쓴다. 질문과 결과는 같은 파일(`~/.secondbrain/setup.json`, 판정 근거 `~/.secondbrain/logs/setup-<단계>-<시각>.json`)에 남는다. 엔진이 판정하고, 에이전트는 결과를 보여 주고 사람의 승인·판정을 받아 넘겨 줄 뿐이다. 단계를 건너뛰거나 판정을 바꿔 적지 않는다.

## 지킬 것

- **키 값·비밀번호·토큰은 대화로 받지 않는다.** 필요하면 사용자가 별도 터미널에서 넣게 안내한다.
- 사용자 대화 본문·관측 본문을 화면에 옮기거나 파일에 쓰지 않는다. 보여 주는 것은 단계 판정·수치·경로·해시뿐이다.
- 승인(`approve`)·넘김(`accept`)·판정(`judge`)은 **사용자가 이 대화에서 직접 말한 경우에만** 실행한다. 추측으로 대신 승인하지 않는다.
- 넘김에는 사용자가 말한 이유를 그대로 `--reason`에 적는다. 이유가 없으면 넘기지 않는다.
- 명령은 모두 `--json`으로 받아 읽는다. 입력을 기다리는 명령은 쓰지 않는다(`--non-interactive`).

## 순서

1. 현재 상태를 본다.
   ```
   sb setup status --json
   ```
   `answers`가 비어 있거나 `source`가 `default`뿐이면 2로, 아니면 3으로.

2. 첫 실행 질문을 대화로 묻는다(설치기 터미널 질문과 같은 항목). 사용자가 "기본값으로"라고 하면 묻지 않고 3으로.
   - 쓰는 에이전트 — 자동 감지값(`answers.agents.value`)을 보여 주고 맞는지 묻는다
   - 넣을 자료 위치 — 폴더·회의록 폴더·링크(쉼표 구분, 없으면 없음)
   - 요약 LLM 경로 — 자동값을 보여 주고 바꿀지 묻는다
   - 한국어 임베딩 사용 — y/n
   받은 답만 넘겨 기록한다(답하지 않은 항목은 빼면 자동값이 남는다).
   ```
   sb setup answer --agents claude,codex --sources "~/meetings,~/docs" --llm-route claude --embedding y --json
   ```

3. 단계를 돌린다.
   ```
   sb setup run --non-interactive --json
   ```
   결과의 `stages`(단계별 판정), `stopped_at`, `verdict`, `hint`와 `results[].metrics` 중 `pass:false`인 항목을 표로 보여 준다.

4. 멈춘 단계에 따라 처리한다.
   - `verdict: stop` (문턱 미달·오류) — `hint`의 고칠 명령을 보여 준다. 고친 뒤 3을 다시 한다. 사용자가 이유를 대며 넘기자고 하면:
     ```
     sb setup accept <단계> --reason "<사용자가 말한 이유>" --json
     ```
   - `verdict: pending`, 4단계(구성안 승인 대기) — 구성안 요약을 보여 준다(`sb vault plan` 출력 또는 hint의 plan 경로를 `sb vault status --json`으로 확인). 사용자가 승인하면 hint에 나온 해시 앞 12자로:
     ```
     sb setup approve <해시 앞 12자> --json
     sb setup run --non-interactive --json
     ```
   - `verdict: pending`, 3단계(표본 판정 대기) — 최근 표본(`~/.secondbrain/logs/setup-3-sample-<seed>.json`)의 id별 판정 요청을 보여 주고, 사용자(또는 사용자가 맡긴 에이전트)의 판정을 받는다. 본문은 보여 주지 않는다 — 필요하면 사용자가 `sb search`로 직접 본다.
     ```
     sb setup judge --stage 3 --judgments "123:ok,456:noise,789:dup" --json
     ```
   - `verdict: pending`, 5단계(검증 데이터 대기) — 세션을 몇 번 쓴 뒤 `sb setup run --stage 5`로 다시 하라고 안내하고 끝낸다.
   - hint가 `미구현`이면 이 키트 판에 그 단계가 아직 없다는 뜻이다. 사용자가 원하면 이유를 적어 넘긴다.

5. `complete: true`가 될 때까지 3~4를 반복한다. 끝나면 단계별 판정과 `fingerprint` 앞 12자를 보고한다.
