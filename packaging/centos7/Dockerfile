FROM quay.io/pypa/manylinux2014_x86_64

ARG PYTHON_TAG=cp311-cp311
ENV BUNDLE_PYTHON=/opt/python/${PYTHON_TAG}/bin/python

WORKDIR /src
COPY . /src

RUN set -eux; \
    bundle=/tmp/select-fuzz-centos7-x86_64; \
    rm -rf "$bundle" /out; \
    mkdir -p "$bundle"; \
    cp -a "/opt/python/${PYTHON_TAG}/." "$bundle/python"; \
    site="$bundle/python/lib/python3.11/site-packages"; \
    "$BUNDLE_PYTHON" -m pip install \
      --no-cache-dir \
      --only-binary=:all: \
      --target "$site" \
      'fastapi>=0.115,<1' \
      'mysql-connector-python>=9.0,<10' \
      'pydantic>=2.8,<3' \
      'PyYAML>=6.0,<7' \
      'typer>=0.12,<1' \
      'uvicorn>=0.30,<1'; \
    mkdir -p "$site/select_fuzz/data"; \
    cp -a /src/src/select_fuzz/. "$site/select_fuzz/"; \
    cp /src/catalog/mysql-8.0.41-query-shapes.yaml "$site/select_fuzz/data/"; \
    cp /src/catalog/mysql-8.0.41-select.grammar.yy "$site/select_fuzz/data/"; \
    mkdir -p "$bundle/config"; \
    cp /src/config/intranet-fuzz.example.yaml "$bundle/config/"; \
    cp /src/config/replica-parameters.example.yaml "$bundle/config/"; \
    cat > "$bundle/select-fuzz" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_ROOT="$ROOT_DIR/python"
export PYTHONHOME="$PYTHON_ROOT"
export PYTHONPATH="$PYTHON_ROOT/lib/python3.11/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$PYTHON_ROOT/lib:$PYTHON_ROOT/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

exec "$PYTHON_ROOT/bin/python3.11" -m select_fuzz "$@"
EOF
    chmod +x "$bundle/select-fuzz"; \
    "$bundle/select-fuzz" --help >/dev/null; \
    mkdir -p /out; \
    tar -czf /out/select-fuzz-centos7-x86_64.tar.gz \
      -C /tmp select-fuzz-centos7-x86_64
