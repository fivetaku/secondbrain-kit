# 기억층 평가 (저장 · 검색 · 활용)

에이전트가 기록층을 "쌓기만" 하는지 "꺼내 쓰는지"를 숫자로 확인하고, 설정을 실험계획(DOE)으로 고르기 위한 하네스입니다.
평가 데이터(질의·판정·결과)는 개인 기록이 담기므로 `~/.secondbrain/local/eval/`에만 두고 커밋하지 않습니다.

## 1. 저장 (write path)

| 시험 | 방법 | 합격 기준 |
|---|---|---|
| 왕복 도달 시간 | 고유 토큰으로 `sb save` → 원본 FTS·`sb_recalld`·워커·벡터에 잡힐 때까지 폴링 | 형태소 회수 ≤ 20초(동기화 주기) |
| 중복 저장 | 같은 제목·본문 2회 저장 | 두 번째는 `duplicate` |
| 저장 형태별 검색 성능 | 검색 DOE 결과를 정답 기록의 종류(세션 백필 / `sb save` / 메모리 동기화 / 실시간 관찰)로 나눠 봄 | 종류 간 격차가 크면 저장 규칙을 고친다 |

## 2. 검색 (`retrieval_eval.py`)

1. **정답 기록 표본**: 업무 기록에서 종류·월 층화 추출(기본 40건).
2. **질의 세트**: 정답 1건당 3형태 — `keyword`(명사 2~4개), `natural`(구어체·동의어), `vague`(맥락 의존·시점 표현). 무관 질의(`negative`) 30개는 주입 오탐을 재기 위함. LLM이 기록 본문만 보고 작성.
3. **수집(collect)**: 질의 × 백엔드(`ko_all` 형태소 명사+동사, `ko_nn` 명사만, `vec` 벡터, `worker` claude-mem 의미검색) × 질의형태(raw/kw) 상위 30을 한 번만 받아 캐시.
4. **판정(pool)**: 모든 조합의 상위 5 + 정답을 풀링해 (질의, 기록) 쌍마다 0/1/2 판정. 판정자는 어느 조합이 낸 결과인지 모른다.
5. **채점(score)**: 요인 조합을 오프라인으로 전수 계산.
   - 요인: `retriever`(단일·RRF 융합 8종) · `qform` · `rrf_k`(10/60) · `excl_tool` · `excl_auto` · `inject`(top5 / 제목 필터)
   - 지표: nDCG@5, Hit@1/5, MRR, 질의형태별 nDCG, **부정 질의 주입률**, 백엔드 지연 p50/p95
6. **주입 문턱**: 1위 문서의 질의 명사 일치 수·커버리지·bm25·벡터 유사도로 "관련 있을 때만 주입" 규칙을 격자 탐색.

```
python eval/retrieval_eval.py collect
python eval/retrieval_eval.py pool       # → pool.jsonl 판정 → qrels.json
python eval/retrieval_eval.py score      # → doe.csv, effects.md
```

## 3. 활용 (`behavior_eval.py`)

헤드리스 `claude -p`로 실제 에이전트를 돌려, 과거 맥락이 필요한 질문에 기록층을 조회해 맞게 답하는지 본다.

- **시나리오**: 기간 요약 / 상충 기록 / 결정 / 이어하기 / 수치 해석 / 작업 규칙 / 이력 / 모호 참조 / 무관 대조군. 각각 채점 기준(0~2점)을 둔다.
- **조건(요인)**: 훅 환경변수로 바꾼다. `SB_RECALL_BACKEND`(worker/recalld), `SB_RECALL_BODIES`(요지 주입 건수), `SB_STOP_GATE`.
- **측정**: 정답 점수(판정자 채점), 기록 조회 호출 수, 조회 0회 비율, 소요 시간, 비용.
- **오염 방지**: 실험 cwd를 claude-mem `CLAUDE_MEM_EXCLUDED_PROJECTS` 경로 패턴으로 제외하고, 폴더 이름만 업무 폴더와 같게 둔다(프로젝트 우선 회수 조건을 재현). scope 는 실험용 `SB_PROJECT_ALIASES` 로 맞춘다.

```
python eval/behavior_eval.py run --sandbox <실험 cwd> --parallel 2
python eval/behavior_eval.py judge-pack   # → judge.jsonl 채점 → grades.json
python eval/behavior_eval.py report
```
