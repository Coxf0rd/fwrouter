#!/bin/sh
set -eu

APP_ROOT="${1:-/opt/fwrouter-api}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${APP_ROOT}/.venv"
PIP_INSTALL_ARGS="${FWROUTER_PIP_INSTALL_ARGS:-}"

if [ ! -f "$APP_ROOT/pyproject.toml" ]; then
  echo "setup-python-env.sh: missing pyproject.toml in $APP_ROOT" >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "setup-python-env.sh: python command not found: $PYTHON_BIN" >&2
  exit 1
fi

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip wheel
# shellcheck disable=SC2086
"$VENV_DIR/bin/python" -m pip install $PIP_INSTALL_ARGS -e "$APP_ROOT"

echo "Prepared FWRouter Python environment: $VENV_DIR"
