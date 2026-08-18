# CentOS 7 portable runtime

This directory contains the builder for a self-contained `select-fuzz` bundle.
The bundle includes:

- CPython 3.11 built for the CentOS 7 / glibc 2.17 ABI;
- all runtime Python dependencies, including `mysql-connector-python`;
- the `select_fuzz` package and its bundled MySQL grammar/catalog files;
- one intranet configuration example for each of correctness, performance, and fuzz.

The target machine does not need Python, pip, or uv. A Linux x86_64 machine
with Docker can build the bundle without Python installed:

```bash
./python/build-centos7-bundle.sh
```

The output is written to `python/output/`. Copy the generated directory or
archive to the CentOS 7 host and run:

For correctness or performance, configure two independently writable instances.
`custom_off` must have PQ disabled and `custom_on` must have PQ enabled before
the run; Select Fuzz does not change that server-side setting and does not wait
for replication. Each template requires only its two `host` lines to be changed:

```bash
cd select-fuzz-centos7-x86_64
export SELECT_FUZZ_MYSQL_USER='<local user>'
export SELECT_FUZZ_MYSQL_PASSWORD='<set in shell only>'

cp config/intranet-correctness.example.yaml config/intranet-correctness.yaml
vi config/intranet-correctness.yaml
./select-fuzz doctor --mode correctness --config config/intranet-correctness.yaml
./select-fuzz run --mode correctness --config config/intranet-correctness.yaml \
  --rounds 64 --seed "$(date +%s)" --artifacts artifacts/correctness

cp config/intranet-performance.example.yaml config/intranet-performance.yaml
vi config/intranet-performance.yaml
./select-fuzz doctor --mode performance --config config/intranet-performance.yaml
./select-fuzz run --mode performance --config config/intranet-performance.yaml \
  --rounds 1 --seed "$(date +%s)" --artifacts artifacts/performance
```

For fuzz, use the separate primary/replica template:

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
