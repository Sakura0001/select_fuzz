#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_ROOT="$ROOT_DIR/python"
export PYTHONHOME="$PYTHON_ROOT"
export PYTHONPATH="$PYTHON_ROOT/lib/python3.11/site-packages${PYTHONPATH:+:$PYTHONPATH}"
# Use the runtime libraries bundled with this Python build. A host-level
# LD_LIBRARY_PATH or LD_PRELOAD may contain older OpenSSL or SQLite libraries
# whose symbol versions are incompatible with the bundled extension modules.
export LD_LIBRARY_PATH="$PYTHON_ROOT/lib:$PYTHON_ROOT/lib64"
bundled_preload=
for library in libsqlite3.so.0 libssl.so.3 libcrypto.so.3; do
    candidate="$PYTHON_ROOT/lib/$library"
    if [ -f "$candidate" ]; then
        bundled_preload="${bundled_preload:+$bundled_preload:}$candidate"
    fi
done
if [ -n "$bundled_preload" ]; then
    export LD_PRELOAD="$bundled_preload"
else
    unset LD_PRELOAD
fi

exec "$PYTHON_ROOT/bin/python3.11" -m select_fuzz "$@"
