FROM quay.io/pypa/manylinux2014_x86_64

ARG PYTHON_TAG=cp311-cp311
ENV BUNDLE_PYTHON=/opt/python/${PYTHON_TAG}/bin/python

WORKDIR /src
COPY . /src
COPY packaging/centos7/select-fuzz-launcher.sh /tmp/select-fuzz-launcher.sh

RUN set -eux; \
    bundle=/tmp/select-fuzz-centos7-x86_64; \
    rm -rf "$bundle" /out; \
    mkdir -p "$bundle"; \
    cp -a "/opt/python/${PYTHON_TAG}/." "$bundle/python"; \
    ssl_module=$("$BUNDLE_PYTHON" -c 'import _ssl; print(_ssl.__file__)'); \
    for library in libssl.so.3 libcrypto.so.3; do \
      resolved=$(ldd "$ssl_module" | awk -v library="$library" '$1 == library { print $3; exit }'); \
      test -n "$resolved"; \
      resolved=$(readlink -f "$resolved"); \
      cp -a "$resolved" "$bundle/python/lib/$(basename "$resolved")"; \
      if [ "$(basename "$resolved")" != "$library" ]; then \
        ln -sf "$(basename "$resolved")" "$bundle/python/lib/$library"; \
      fi; \
    done; \
    sqlite_module=$("$BUNDLE_PYTHON" -c 'import _sqlite3; print(_sqlite3.__file__)'); \
    library=libsqlite3.so.0; \
    resolved=$({ ldd "$sqlite_module"; ldd "$BUNDLE_PYTHON"; } | awk -v library="$library" '$1 == library && index($3, "/") == 1 { print $3; exit } $1 == library && index($2, "/") == 1 { print $2; exit }'); \
    if [ -z "$resolved" ]; then \
      resolved=$(find /opt/_internal -name "$library" -print -quit); \
    fi; \
    test -n "$resolved"; \
    resolved=$(readlink -f "$resolved"); \
    cp -a "$resolved" "$bundle/python/lib/$(basename "$resolved")"; \
    if [ "$(basename "$resolved")" != "$library" ]; then \
      ln -sf "$(basename "$resolved")" "$bundle/python/lib/$library"; \
    fi; \
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
    cp /tmp/select-fuzz-launcher.sh "$bundle/select-fuzz"; \
    chmod +x "$bundle/select-fuzz"; \
    "$bundle/select-fuzz" --help >/dev/null; \
    mkdir -p /out; \
    tar -czf /out/select-fuzz-centos7-x86_64.tar.gz \
      -C /tmp select-fuzz-centos7-x86_64
