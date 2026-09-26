#!/bin/sh
# secondbrain-kit 설치 (macOS). uv 가 없으면 설치한 뒤 파이썬 설치기를 실행한다.
#   ./install.sh [--dry-run] [--no-embed] [--no-codex] [--no-schedule] [--with-consolidate]
#   ./install.sh --doctor | --uninstall
set -e
KIT="$(cd "$(dirname "$0")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv 설치 중 (https://astral.sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"; export PATH
fi
exec uv run --no-project --python 3.12 "$KIT/installer/install.py" "$@"
