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
    git -C "$DIR" pull --ff-only
  else
    echo "==> 키트 받기: $REPO -> $DIR"
    git clone --depth 1 "$REPO" "$DIR"
  fi
  chmod +x "$DIR/install.sh"
  "$DIR/install.sh" "$@" </dev/null
}

main "$@"
