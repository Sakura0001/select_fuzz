#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_ROOT="$ROOT_DIR/python"
export PYTHONHOME="$PYTHON_ROOT"
export PYTHONPATH="$PYTHON_ROOT/lib/python3.11/site-packages${PYTHONPATH:+:$PYTHONPATH}"
# Use the OpenSSL libraries bundled with this runtime.  A host-level
# LD_LIBRARY_PATH (for example /opt/openssl3/lib) may contain an older
# libcrypto.so.3 whose symbol version is incompatible with _ssl.
unset LD_PRELOAD
export LD_LIBRARY_PATH="$PYTHON_ROOT/lib:$PYTHON_ROOT/lib64"

exec "$PYTHON_ROOT/bin/python3.11" -m select_fuzz "$@"
