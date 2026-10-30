#!/bin/sh
# secondbrain-kit one-line installer (macOS)
#   curl -fsSL https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.sh | sh
#   curl -fsSL .../bootstrap.sh | sh -s -- --no-codex        # 설치 옵션 전달
# 이미 받아둔 PC 에서 다시 실행하면 최신으로 갱신(fetch 후 빨리감기, 안 되면 로컬 커밋·변경을 보관하고 원격 main 으로) 후 재설치한다(멱등).
# 받기가 실패하면(네트워크) 로컬은 그대로 두고 지금 있는 키트로 설치한다.
set -e

main() {
  REPO="${SBKIT_REPO:-https://github.com/fivetaku/secondbrain-kit.git}"
  DIR="${SBKIT_DIR:-$HOME/secondbrain-kit}"
  if ! command -v git >/dev/null 2>&1; then
    echo "git 이 필요합니다: xcode-select --install  (또는 brew install git)" >&2
    exit 1
  fi
  if [ -d "$DIR/.git" ]; then
    echo "==> 키트 갱신: $DIR"
    # 받기 전 원격 위치 — 이력이 다시 쓰여도 '원래 원격에 있던 커밋'과 '사용자 로컬 커밋'을 가르려고 남긴다
    OLD=$(git -C "$DIR" rev-parse -q --verify refs/remotes/origin/main || true)
    if ! git -C "$DIR" fetch origin main; then
      # 네트워크 등으로 받기 자체가 실패하면 로컬은 손대지 않는다(이전엔 이때도 stash 부터 해 변경이 사라진 것처럼 보였다)
      echo "⚠ 원격에서 최신본을 받지 못했습니다(네트워크 확인). 로컬은 그대로 두고 지금 받아 둔 키트로 설치를 계속합니다." >&2
    elif ! git -C "$DIR" merge --ff-only origin/main; then
      # 원격 이력이 다시 쓰였을 때(2026-10-22 작성자 이메일 정리) 등 빨리감기가 안 되면 origin/main 으로 맞춘다
      echo "==> 빨리감기가 안 돼 원격 main 으로 맞춥니다"
      TS=$(date +%Y%m%d-%H%M%S)
      # 판정 결과를 변수로 받아 실패를 잡는다 — [ -n "$(…)" ] 안에선 set -e 가 듣지 않아, rev-list 가 실패하면(예: 작업 트리에
      # origin/main 이라는 파일이 있어 리비전·경로가 모호) '로컬 커밋 없음'으로 읽혀 백업 없이 reset --hard 했다(ps1 은 throw)
      if ! LOCAL=$(git -C "$DIR" rev-list -n 1 HEAD --not origin/main ${OLD:+"$OLD"}); then
        echo "원격에 없는 로컬 커밋을 판정하지 못해(git rev-list 실패) 아무것도 바꾸지 않고 멈춥니다: $DIR" >&2
        exit 1
      fi
      if [ -n "$LOCAL" ]; then
        git -C "$DIR" branch "sbkit-backup-$TS" HEAD
        echo "    원격에 없는 로컬 커밋을 브랜치 sbkit-backup-$TS 에 보관했습니다 (git -C \"$DIR\" log sbkit-backup-$TS)"
      fi
      # 변경 판정도 같은 이유로 변수로 받는다 — status 가 실패하면 '변경 없음'으로 읽혀 stash 없이 reset --hard 했다
      if ! CHANGES=$(git -C "$DIR" status --porcelain); then
        echo "커밋하지 않은 변경을 판정하지 못해(git status 실패) 아무것도 바꾸지 않고 멈춥니다: $DIR" >&2
        exit 1
      fi
      if [ -n "$CHANGES" ]; then
        if ! git -C "$DIR" stash push -u -m "sbkit-update-$TS" >/dev/null; then
          echo "로컬 변경을 보관하지 못해 아무것도 바꾸지 않고 멈춥니다: $DIR" >&2
          exit 1
        fi
        echo "    커밋하지 않은 변경은 git stash 에 보관했습니다 (git -C \"$DIR\" stash list)"
      fi
      git -C "$DIR" reset --hard origin/main
    fi
  else
    echo "==> 키트 받기: $REPO -> $DIR"
    git clone --depth 1 "$REPO" "$DIR"
  fi
  chmod +x "$DIR/install.sh"
  "$DIR/install.sh" "$@" </dev/null
}

main "$@"
