#!/bin/sh
# secondbrain-kit one-line installer (macOS)
#   curl -fsSL https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.sh | sh
#   curl -fsSL .../bootstrap.sh | sh -s -- --no-codex        # 설치 옵션 전달
# 이미 받아둔 PC 에서 다시 실행하면 최신으로 갱신(git pull) 후 재설치한다(멱등).
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
    if ! git -C "$DIR" pull --ff-only; then
      # 원격 이력이 다시 쓰였을 때(2026-10-22 작성자 이메일 정리) — 로컬 변경은 stash 에 보관하고 원격 main 에 맞춘다
      echo "==> 원격 이력이 바뀌어 최신본으로 맞춥니다 (로컬 변경은 git stash 에 보관)"
      git -C "$DIR" stash push -u -m "sbkit-update-$(date +%Y%m%d%H%M%S)" >/dev/null 2>&1 || true
      git -C "$DIR" fetch origin main
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
