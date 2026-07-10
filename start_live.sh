#!/usr/bin/env bash
# robust_v2 前台启动入口；由 Docker、systemd、supervisor 或 tmux 负责进程托管。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi
if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_EXECUTABLE="$PYTHON_BIN"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_EXECUTABLE="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_EXECUTABLE="python3"
fi

cd "$ROOT_DIR"
exec "$PYTHON_EXECUTABLE" "$ROOT_DIR/robust_runner.py" daemon "$@"
