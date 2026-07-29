# CentOS 7 portable runtime

This directory contains the builder for a self-contained `select-fuzz` bundle.
The bundle includes:

- CPython 3.11 built for the CentOS 7 / glibc 2.17 ABI;
- all runtime Python dependencies, including `mysql-connector-python`;
- the `select_fuzz` package and its bundled MySQL grammar/catalog files;
- the intranet fuzz configuration examples.

The target machine does not need Python, pip, or uv. A Linux x86_64 machine
with Docker can build the bundle without Python installed:

```bash
./python/build-centos7-bundle.sh
```

The output is written to `python/output/`. Copy the generated directory or
archive to the CentOS 7 host and run:

```bash
cd select-fuzz-centos7-x86_64
cp config/intranet-fuzz.example.yaml config/intranet-fuzz.yaml
export SELECT_FUZZ_MYSQL_USER=root
read -s SELECT_FUZZ_MYSQL_PASSWORD
export SELECT_FUZZ_MYSQL_PASSWORD
./select-fuzz doctor --mode fuzz --config config/intranet-fuzz.yaml
./select-fuzz run --mode fuzz --config config/intranet-fuzz.yaml \
  --duration-seconds 300 --full-thread-sql-log \
  --artifacts artifacts/intranet-fuzz
```

The generated binary bundle is architecture-specific. This builder targets
x86_64; an ARM64 CentOS 7 host needs a separate ARM64 build image and bundle.
