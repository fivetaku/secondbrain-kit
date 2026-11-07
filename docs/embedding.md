# 한국어 임베딩 (BGE-m3-ko)

## 왜 필요한가
claude-mem은 벡터 컬렉션(`cm__claude-mem`)을 만들 때 임베딩을 지정하지 않아 chroma-mcp 기본값(all-MiniLM, 영어 전용)이 된다. 원 PC에서 한국어 질의 15개 중 14개가 회수되지 않았고, bge-m3로 바꾼 뒤 회수율이 크게 올랐다.

## 왜 BGE-m3-ko인가 (2026-11-07)
- 원 PC 기록 1.2만 건 표본과 질의 140개(실제 프롬프트 40 + 기억 찾기 100)로 7개 모델을 비교했다. Claude·GPT 두 판정자가 모델 이름을 모른 채 채점했다(가중 kappa 0.71).
- `dragonkue/BGE-m3-ko`(bge-m3를 한국어로 추가 학습, Apache-2.0, 1024차원)가 bge-m3보다 nDCG@10 0.620→0.683, 상위 30 회수 91→95로 둘 다 유의하게 나았다. 운영 전체 81만 청크에서도 기억 찾기 100질의 1위 정답 25→31, 상위 10 60→70, 상위 30 78→82였다.
- 차원(1024)·구조(XLM-RoBERTa)가 bge-m3와 같아 Ollama로 그대로 서빙하고, 색인 크기·질의 지연도 같다. 공개 벤치마크(MTEB-ko)에서는 차이가 0.01 안팎이라 대화 기록형 데이터에서 차이가 더 크게 났다.

## 키트의 방식
- 설치기 4단계가 BGE-m3-ko GGUF(F16, 약 1.2GB)를 Hugging Face에서 **리비전·sha256 고정**으로 받아 `ollama create bge-m3-ko`(`PARAMETER num_batch 8192`)로 만든다. 출처는 두 곳이고(`CHKH01/BGE-m3-ko-GGUF`, `Neuwhufbox/BGE-m3-ko-gguf`) 앞이 실패하면 뒤를 받는다. 두 파일 모두 원 PC에서 llama.cpp로 직접 변환한 모델과 벡터가 같았다(cos 1.0).
- 받지 못하면(네트워크·디스크) `ollama pull bge-m3`로 내려가고 끝에 경고를 남긴다. 설치기를 다시 돌리면 다시 시도한다.
- 컬렉션을 **claude-mem이 처음 만들기 전에** 키트가 먼저 만든다. 임베딩은 chroma 내장 `openai` 임베딩 함수를 Ollama의 OpenAI 호환 엔드포인트(`http://127.0.0.1:11434/v1`, 모델 `bge-m3-ko`, 1024차원)로 향하게 한다.
- 이후 claude-mem(chroma-mcp)은 컬렉션에 저장된 임베딩 설정을 그대로 복원해 쓴다. 임베딩 때문에 추가 패키지·shim을 둘 필요가 없어 macOS와 Windows가 같다(chroma-mcp 자체의 버전 고정은 아래 별도 항목).
- chroma-mcp는 홈 디렉토리의 `~/.chroma_env`에서 `CHROMA_OPENAI_API_KEY=ollama`를 읽는다(값은 Ollama가 무시하지만 변수는 있어야 한다).
- Ollama가 꺼져 있으면 벡터 저장·검색이 실패하고, 검색은 한국어 형태소 색인만으로 계속 동작한다.
- 이미 bge-m3 컬렉션이 있는 PC는 그대로 bge-m3를 쓴다(모델을 바꾸면 전체 재색인이 필요). 바꾸는 방법은 아래 "BGE-m3-ko로 바꾸기".

## 입력 길이 한도 (num_batch)
- 2026-11-06 원 PC 실측(Ollama 0.40.13): `ollama ps`는 bge-m3 컨텍스트를 8192로 보여 주지만 러너가 `-c 8192 -b 2048 -ub 2048`로 떠서, `/api/embed`와 `/v1/embeddings` 모두 **2048토큰 뒤를 에러 없이 버린다**(꼬리만 다른 두 입력의 벡터가 같다). 한국어는 약 2.1자/토큰이라 약 4,300자부터 잘린다. `num_ctx`를 올려도 그대로다. BGE-m3-ko도 같은 구조라 같다.
- 설치기는 모델을 만들 때 `PARAMETER num_batch 8192`를 넣는다. 그러면 8192토큰(모델 한도)까지 반영된다. 2048토큰 이하 입력의 벡터는 바뀌지 않는다(cos 1.0). 러너 메모리는 약 0.35GB 늘고 짧은 질의 지연은 같다.
- bge-m3를 쓰는 PC에서 `ollama pull bge-m3`를 따로 실행하면 이름표가 원본으로 돌아가 다시 잘린다. `./install.sh --doctor`의 `<모델> 입력 한도`가 ⚠이면 설치기를 다시 돌리거나 아래를 실행한다(`<모델>`은 bge-m3-ko 또는 bge-m3).
  ```
  printf 'FROM <모델>\nPARAMETER num_batch 8192\n' > /tmp/embed.Modelfile
  ollama create <모델> -f /tmp/embed.Modelfile
  ```

## BGE-m3-ko로 바꾸기 (이미 bge-m3·기본 임베딩 컬렉션이 있는 PC)
설치기는 기존 컬렉션을 건드리지 않는다. 바꾸려면(원본 백업 필수):
1. Claude Code·Codex 세션을 모두 닫고 `npx claude-mem stop`
2. `~/.claude-mem/chroma` 를 다른 이름으로 옮겨 백업하고, `~/.claude-mem/chroma-sync-state.json` 도 옮긴다. 워커의 backfill은 실제 벡터 유무가 아니라 이 파일의 "어디까지 넣었나" 표시를 보므로, 옮기지 않으면 기존 관측이 다시 들어가지 않는다.
3. `./install.sh` 재실행 → BGE-m3-ko를 준비하고 새 컬렉션을 만든다
4. 세션을 열면 worker가 backfill로 기존 관측을 다시 임베딩한다(관측 수에 비례해 시간이 걸린다. 수십만 청크면 몇 시간)
5. `./install.sh --doctor` 에서 `벡터 임베딩 openai / bge-m3-ko` 확인, `sb search '<예전 작업 키워드>' --global` 로 회수 확인

관측 원본(SQLite)은 이 과정에서 바뀌지 않는다. 문제가 생기면 백업한 `chroma` 폴더와 `chroma-sync-state.json`을 되돌리면 된다.

## chroma-mcp 버전 고정
- 2026-10-12, uvx가 chroma-mcp 환경을 새로 풀면서 pydantic 2.14.1을 받았다. chroma-mcp 0.2.6이 고정한 mcp 1.6.0은 2.14에서 사라진 `pydantic._internal._typing_extra.eval_type_backport`를 import해 시작하자마자 죽었고, 벡터 동기화가 4시간 넘게 멈췄다. 2026-10-30에 고정 없이 다시 띄워도 같은 ImportError가 났다.
- claude-mem 워커는 자기 인자(`uvx --python 3.13 --with onnxruntime>=1.20 --with protobuf<7 --from chroma-mcp==0.2.6 chroma-mcp`)로 chroma-mcp를 띄우므로, 설치기가 자기 명령에 붙인 고정은 워커에 닿지 않는다. 그래서 설치기는 같은 인자에 `--with "pydantic<2.14"`를 더해 `uv tool install`로 도구를 깔아 둔다. uvx는 요구를 만족하는 설치 도구가 있으면 새로 풀지 않고 그 환경을 쓴다(uv 0.10 실측).
- 확인: `./install.sh --doctor`의 `chroma-mcp 기동`이 워커와 같은 인자로 실제로 띄워 본다. ❌에 ImportError가 보이면 설치기를 다시 돌린다. 밀린 벡터는 원인을 없앤 뒤 claude-mem 워커가 다시 떠야 채워진다(`sb health`의 chroma-pending).
- 한계: claude-mem을 올려 워커의 `--with` 목록이나 파이썬 버전이 설치 도구와 달라지면 uvx가 다시 풀어 고정이 빠질 수 있다. 이 경우는 doctor의 기동 점검으로만 잡힌다.
- 이 도구는 claude-mem 워커가 쓰므로 키트를 제거해도 남긴다. claude-mem까지 지운 뒤에 `uv tool uninstall chroma-mcp`로 지운다.
