## 세컨브레인 기억 규칙 (secondbrain-kit)

이 PC의 Claude Code·Codex 세션은 모두 claude-mem에 관측으로 쌓이고, `sb` 명령(Bash·PowerShell 공통)으로 다시 꺼낼 수 있다.
세션 시작 때 "재개 브리핑"이, 프롬프트마다 "[세컨브레인 회수]" 목록(ID·제목)이 자동으로 들어온다.
기록층은 쌓아두는 곳이 아니라 **일할 때 꺼내 쓰는 곳**이다. 아는 것은 바로 하되 확인하고, 모르는 것은 추측 말고 가져다 쓴다.

**언제 조회하나** (조회는 0.2~수 초 — 망설이지 않는다)
- 기간·이력 질문("이번달 한 것", "지난주", "그거 어떻게 됐지", 회고·보고): `sb timeline --since YYYY-MM-DD [--until D]`로 목록부터 → 주제별 `sb search` → git·파일과 교차 확인. git이나 파일만 보고 답하지 않는다.
- 이어서 하는 작업·"아까/저번 그거": 착수 전에 `sb search '<주제>'` + `--mode current|next`로 이전 결정·미결을 먼저 본다.
- 수치·상태·결정을 말할 때: 알고 있어도 기록으로 한 번 확인하고 말한다(기록 당시 스냅샷이면 실측 병행).
- 모르는 용어·사람·프로젝트·과거 맥락: 묻기 전에 조회. 안 나오면 표현을 바꿔 한 번 더.
- "[세컨브레인 회수]" 목록에 관련 ID가 보이면 `get_observations([ID])`로 본문을 가져와 쓴다(제목만 보고 넘기지 않는다).
- 서브에이전트에 일을 맡길 때: 관련 #ID나 조회 명령을 프롬프트에 넣어 준다. 서브에이전트도 `sb`를 직접 쓸 수 있다.
- 과거 맥락을 쓴 답에는 끝에 근거를 한 줄 붙인다: `근거: #56 · KB topic-x · 커밋 d1d3381`.

**찾는 순서** (앞 단계에서 답이 나오면 멈춘다)
1. 확정 사실·다음 행동·규칙 (0.2초): `sb search --mode current` / `--mode next` / `--mode rules` (scope는 현재 폴더 기준 자동, 다른 곳은 `--scope <이름>`). `user-confirmed` 표시는 사용자가 확정한 것이다.
2. 과거 경위 (수 초): `sb search '<검색어>' --global --limit 5 --no-vector`. 표현이 애매해 키워드로 안 잡히면 `--no-vector`를 뺀다(느림). 기간으로 찾을 땐 `sb timeline`. claude-mem MCP `search`·`get_observations([ID])`도 같은 데이터다(날짜는 적재일이라 기간 조회엔 `sb timeline`을 쓴다).
3. 현재 상태: 파일·API를 읽기 전용으로 실측.
- 이 셋으로도 부족하거나 사용자만 정할 수 있는 선택(비가역·외부 발송·범위 변경)일 때만 묻는다.
- `user-confirmed` 사실과 어긋나는 질문(이미 끝낸 일의 재실행 여부 등)은 하지 않는다. 어긋나 보이면 그 사실을 먼저 인용한다.
- 과거 기록은 "기록 당시 스냅샷"이다. 포트·경로·배포 상태처럼 현재 상태를 주장할 땐 실측을 우선하고, 못 했으면 "미검증"이라고 밝힌다.
- 검색에 안 나오면 "없다"고 단정하지 말고 표현을 바꿔 한 번 더 찾는다.

**사용자 결정은 들은 자리에서 상태층에 남긴다**
- 사용자가 무언가를 결정·확정하면: `sb state propose --file <json>` → `sb state verify --candidate <id> --method user_utterance_check --target prompt:<user_prompts.id>` → `sb state accept --candidate <id> --expected-version <N>`.
  - prompt id는 `sb prompt-id '<발화 일부>'`로 찾는다.
  - json: `{"scope_id","kind":"user_decision","fact_key","body","value_json":"<사용자 발화 그대로 인용>","source","write_id","observed_at":"<UTC ISO8601>"}`. 검증은 value_json이 실제 발화에 포함됐는지로 판정한다.
  - 측정한 사실은 `kind":"measured_fact"` + `--method file_contains|json_file_value|observation_ref`.
- 남기지 않으면 다음 세션이 같은 질문을 반복한다.

**작업이 끝나 검증까지 통과했으면 스스로 완료처리한다** (잡담·중간 단계 제외)
- `sb save --title "<무엇을 했나>" --text "<결론·근거·남은 일>"` 로 기억 1건 저장.
- 관련 미결이 있으면 `sb loops close <id>`. 새로 생긴 미결은 `sb loops add`.
