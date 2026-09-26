# 한국어 임베딩 (bge-m3)

## 왜 필요한가
claude-mem은 벡터 컬렉션(`cm__claude-mem`)을 만들 때 임베딩을 지정하지 않아 chroma-mcp 기본값(all-MiniLM, 영어 전용)이 된다. 원 PC에서 한국어 질의 15개 중 14개가 회수되지 않았고, bge-m3로 바꾼 뒤 회수율이 크게 올랐다.

## 키트의 방식
- 컬렉션을 **claude-mem이 처음 만들기 전에** 키트가 먼저 만든다. 임베딩은 chroma 내장 `openai` 임베딩 함수를 Ollama의 OpenAI 호환 엔드포인트(`http://127.0.0.1:11434/v1`, 모델 `bge-m3`, 1024차원)로 향하게 한다.
- 이후 claude-mem(chroma-mcp)은 컬렉션에 저장된 임베딩 설정을 그대로 복원해 쓴다. 추가 패키지·shim이 필요 없어 macOS와 Windows가 같다.
- chroma-mcp는 홈 디렉토리의 `~/.chroma_env`에서 `CHROMA_OPENAI_API_KEY=ollama`를 읽는다(값은 Ollama가 무시하지만 변수는 있어야 한다).
- Ollama가 꺼져 있으면 벡터 저장·검색이 실패하고, 검색은 한국어 형태소 색인만으로 계속 동작한다.

## 이미 claude-mem을 쓰던 PC (기본 임베딩 컬렉션이 있을 때)
설치기는 기존 컬렉션을 건드리지 않는다. 바꾸려면(미검증 절차 — 원본 백업 필수):
1. Claude Code·Codex 세션을 모두 닫고 `npx claude-mem stop`
2. `~/.claude-mem/chroma` 를 다른 이름으로 옮겨 백업하고, `~/.claude-mem/chroma-sync-state.json` 도 옮긴다
3. `./install.sh` 재실행 → 새 컬렉션이 bge-m3로 만들어진다
4. 세션을 열면 worker가 SQLite와 벡터의 차이를 채우는 backfill로 기존 관측을 다시 임베딩한다(관측 수에 비례해 시간이 걸린다)
5. `./install.sh --doctor` 에서 `벡터 임베딩 openai` 확인, `sb search '<예전 작업 키워드>' --global` 로 회수 확인

관측 원본(SQLite)은 이 과정에서 바뀌지 않는다. 문제가 생기면 백업한 `chroma` 폴더를 되돌리면 된다.
