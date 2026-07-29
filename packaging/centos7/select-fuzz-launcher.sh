#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_ROOT="$ROOT_DIR/python"
export PYTHONHOME="$PYTHON_ROOT"
export PYTHONPATH="$PYTHON_ROOT/lib/python3.11/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$PYTHON_ROOT/lib:$PYTHON_ROOT/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

exec "$PYTHON_ROOT/bin/python3.11" -m select_fuzz "$@"
