# 한국어 임베딩 (bge-m3)

## 왜 필요한가
claude-mem은 벡터 컬렉션(`cm__claude-mem`)을 만들 때 임베딩을 지정하지 않아 chroma-mcp 기본값(all-MiniLM, 영어 전용)이 된다. 원 PC에서 한국어 질의 15개 중 14개가 회수되지 않았고, bge-m3로 바꾼 뒤 회수율이 크게 올랐다.

## 키트의 방식
- 컬렉션을 **claude-mem이 처음 만들기 전에** 키트가 먼저 만든다. 임베딩은 chroma 내장 `openai` 임베딩 함수를 Ollama의 OpenAI 호환 엔드포인트(`http://127.0.0.1:11434/v1`, 모델 `bge-m3`, 1024차원)로 향하게 한다.
- 이후 claude-mem(chroma-mcp)은 컬렉션에 저장된 임베딩 설정을 그대로 복원해 쓴다. 임베딩 때문에 추가 패키지·shim을 둘 필요가 없어 macOS와 Windows가 같다(chroma-mcp 자체의 버전 고정은 아래 별도 항목).
- chroma-mcp는 홈 디렉토리의 `~/.chroma_env`에서 `CHROMA_OPENAI_API_KEY=ollama`를 읽는다(값은 Ollama가 무시하지만 변수는 있어야 한다).
- Ollama가 꺼져 있으면 벡터 저장·검색이 실패하고, 검색은 한국어 형태소 색인만으로 계속 동작한다.

## 입력 길이 한도 (num_batch)
- 2026-11-06 원 PC 실측(Ollama 0.40.13): `ollama ps`는 bge-m3 컨텍스트를 8192로 보여 주지만 러너가 `-c 8192 -b 2048 -ub 2048`로 떠서, `/api/embed`와 `/v1/embeddings` 모두 **2048토큰 뒤를 에러 없이 버린다**(꼬리만 다른 두 입력의 벡터가 같다). 한국어는 약 2.1자/토큰이라 약 4,300자부터 잘린다. `num_ctx`를 올려도 그대로다.
- 설치기는 `ollama pull bge-m3` 직후 모델에 `PARAMETER num_batch 8192`를 넣는다(`ollama create bge-m3`). 그러면 8192토큰(모델 한도)까지 반영된다. 2048토큰 이하 입력의 벡터는 바뀌지 않아(cos 1.0) 기존 컬렉션과 그대로 호환된다. 러너 메모리는 약 0.35GB 늘고 짧은 질의 지연은 같다.
- `ollama pull bge-m3`를 따로 실행하면 이름표가 원본으로 돌아가 다시 잘린다. `./install.sh --doctor`의 `bge-m3 입력 한도`가 ⚠이면 설치기를 다시 돌리거나 아래를 실행한다.
  ```
  printf 'FROM bge-m3\nPARAMETER num_batch 8192\n' > /tmp/bge-m3.Modelfile
  ollama create bge-m3 -f /tmp/bge-m3.Modelfile
  ```
- 이미 저장된 긴 기록(2048토큰 초과)은 예전처럼 앞부분만 반영된 벡터로 남는다. 새로 저장되는 기록부터 전체가 반영된다. 원 PC에서는 긴 문서 50건 시험에서 뒷부분 내용으로 찾을 때 MRR 0.426→0.555, 앞부분으로 찾을 때 0.739→0.716이었다.

## chroma-mcp 버전 고정
- 2026-10-12, uvx가 chroma-mcp 환경을 새로 풀면서 pydantic 2.14.1을 받았다. chroma-mcp 0.2.6이 고정한 mcp 1.6.0은 2.14에서 사라진 `pydantic._internal._typing_extra.eval_type_backport`를 import해 시작하자마자 죽었고, 벡터 동기화가 4시간 넘게 멈췄다. 2026-10-30에 고정 없이 다시 띄워도 같은 ImportError가 났다.
- claude-mem 워커는 자기 인자(`uvx --python 3.13 --with onnxruntime>=1.20 --with protobuf<7 --from chroma-mcp==0.2.6 chroma-mcp`)로 chroma-mcp를 띄우므로, 설치기가 자기 명령에 붙인 고정은 워커에 닿지 않는다. 그래서 설치기는 같은 인자에 `--with "pydantic<2.14"`를 더해 `uv tool install`로 도구를 깔아 둔다. uvx는 요구를 만족하는 설치 도구가 있으면 새로 풀지 않고 그 환경을 쓴다(uv 0.10 실측).
- 확인: `./install.sh --doctor`의 `chroma-mcp 기동`이 워커와 같은 인자로 실제로 띄워 본다. ❌에 ImportError가 보이면 설치기를 다시 돌린다. 밀린 벡터는 원인을 없앤 뒤 claude-mem 워커가 다시 떠야 채워진다(`sb health`의 chroma-pending).
- 한계: claude-mem을 올려 워커의 `--with` 목록이나 파이썬 버전이 설치 도구와 달라지면 uvx가 다시 풀어 고정이 빠질 수 있다. 이 경우는 doctor의 기동 점검으로만 잡힌다.
- 이 도구는 claude-mem 워커가 쓰므로 키트를 제거해도 남긴다. claude-mem까지 지운 뒤에 `uv tool uninstall chroma-mcp`로 지운다.

## 이미 claude-mem을 쓰던 PC (기본 임베딩 컬렉션이 있을 때)
설치기는 기존 컬렉션을 건드리지 않는다. 바꾸려면(미검증 절차 — 원본 백업 필수):
1. Claude Code·Codex 세션을 모두 닫고 `npx claude-mem stop`
2. `~/.claude-mem/chroma` 를 다른 이름으로 옮겨 백업하고, `~/.claude-mem/chroma-sync-state.json` 도 옮긴다
3. `./install.sh` 재실행 → 새 컬렉션이 bge-m3로 만들어진다
4. 세션을 열면 worker가 SQLite와 벡터의 차이를 채우는 backfill로 기존 관측을 다시 임베딩한다(관측 수에 비례해 시간이 걸린다)
5. `./install.sh --doctor` 에서 `벡터 임베딩 openai` 확인, `sb search '<예전 작업 키워드>' --global` 로 회수 확인

관측 원본(SQLite)은 이 과정에서 바뀌지 않는다. 문제가 생기면 백업한 `chroma` 폴더를 되돌리면 된다.
