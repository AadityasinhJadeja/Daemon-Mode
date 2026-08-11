#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

exec "$PYTHON_BIN" "$ROOT_DIR/tools/manage-local-runtime.py" install \
  --source-root "$ROOT_DIR" \
  --python-bin "$PYTHON_BIN" \
  "$@"
