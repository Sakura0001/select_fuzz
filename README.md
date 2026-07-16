# Select Fuzz

Select Fuzz is a deterministic MySQL differential test product for three
primary/replica pairs:

- `baseline`: unmodified open-source MySQL 8.0.41;
- `custom_off`: the custom engine with parallel query disabled globally;
- `custom_on`: the custom engine with parallel query enabled globally.

Each role has one primary for setup/DML and one replica for SELECT/analysis.
It has separate correctness and performance modes. Correctness compares typed
results or normalized errors on all three replicas. Both modes use seed-reproducible
weighted random schemas, tables, ordinary MySQL types and ranges, indexes, data,
and table-reading SELECTs. Performance uses exactly one logical worker. Each
performance round fills its random tables once through bounded deterministic
stored procedures, waits for all replicas, and then executes the configured
number of distinct `EXPLAIN ANALYZE FORMAT=TREE` queries sequentially. Each query
is launched concurrently on the three replicas and reports regressions against
both baseline and `custom_off`. JSON, FULLTEXT, SPATIAL, and multivalue indexes
are excluded from the default fuzz scope.

## Install

Python 3.11 and Node.js are required. Credentials are resolved only from shell
environment variables and must never be written into YAML or Git.

```bash
UV_CACHE_DIR=.uv-cache uv sync --locked --all-groups
npm --prefix frontend ci
npm --prefix frontend run build
export SELECT_FUZZ_MYSQL_USER='<local user>'
export SELECT_FUZZ_MYSQL_PASSWORD='<set in shell only>'
cp config/example.yaml config/local.yaml
cp config/replica-parameters.example.yaml config/replica-parameters.yaml
```

Edit the six endpoints and optional role probes in the ignored
`config/local.yaml`, then point `replica_parameters_file` at the separate
replica parameter file. Only typed `SET SESSION` values are accepted there;
credentials remain environment-only. The three role pairs must have isolated,
comparable resources and working primary-to-replica replication.

## CLI

Always run `doctor` first. It probes all six distinct endpoints. Version and
configuration differences are reported but do not hard-gate startup; missing
runtime capabilities or required permissions remain fatal.

```bash
uv run select-fuzz doctor --mode correctness --config config/local.yaml
uv run select-fuzz run --mode correctness --config config/local.yaml --rounds 1
uv run select-fuzz run --mode performance --config config/local.yaml --rounds 1
```

Omit `--rounds` for continuous operation, or use `--duration-seconds` for a
bounded run. Correctness row range, worker count, queries per round, limits,
query-lane ratios, performance query count, data volume, and degradation ratio
are all YAML/CLI controlled.

Set `full_thread_sql_log: true` in YAML or pass `--full-thread-sql-log` to append
every statement executed by each logical fuzz worker to `sql/worker-NNN.sql`.
These files are append-only across rounds and are never reset by random DDL.

Every generated correctness or performance round also writes a directly
source-able script under `rounds/`. Both modes use exactly one
`rounds/<database>.sql`: only its opening header is comments, every later SQL
statement occupies one physical line, queries have no separating blank lines,
and each periodic DML transaction has one blank line before and after it. SQL is
appended only when attempted, in execution order. Run it with
`mysql < rounds/<case>.sql` or MySQL `SOURCE`.
True findings additionally contain a minimal `case.sql` and compact `case.diff`;
result bodies are retained only up to 100 rows and 64 KiB, otherwise only counts
and digests are stored.

Findings are appended and fsynced immediately. Rebuild a report or replay a
finding on a new retained database with:

```bash
uv run select-fuzz report --artifacts artifacts --output reports/latest.html
uv run select-fuzz replay --config config/local.yaml --artifacts artifacts \
  --finding '<case id>'
```

In correctness mode every worker independently triggers one deterministic
1–3-statement DML transaction after each ten completed logical queries. The
batch targets 12–50 rows with `INSERT:UPDATE:DELETE = 2:1:1`. All primaries run
the same statement in lockstep; identical semantic errors roll back and
continue, while any status/error/affected-row difference ends the current
database and preserves it. Successful commits update a replication marker and
SELECTs resume only after all replicas observe it (10-second default timeout).
Low-cardinality tables use an exact-cardinality insert fallback so a committed
batch still changes 12–50 actual rows; correctness configuration therefore
requires `min_rows_per_table >= 1`. Each transaction statement is appended
to `rounds/<database>.sql` before execution, including rollback/commit.
Any DDL, DML, transaction, replication, or SELECT inconsistency preserves the
database, emits a finding, and moves that worker to a fresh round database.
Performance materialization failures preserve the real database, failing setup
SQL, per-node outcomes or replica observations, and replica-parameter digest;
they also make the CLI exit nonzero.

Performance `workers` is fixed at `1`. At the start of a round, setup runs once
on the three primaries and one marker wait confirms the three replicas. The
round then executes up to `performance.queries_per_round` different queries in
strict sequence. A performance finding preserves that database and the next
round starts with a fresh one. There is no periodic DML in performance mode.

The local React control plane is started with:

```bash
uv run select-fuzz serve --config config/local.yaml --artifacts artifacts
```

It binds to loopback only. The UI starts and stops both modes, streams events,
and exposes findings, reports, and replay status.

Databases are retained by default and are never cleaned automatically. The
`cleanup` command accepts only generated `sf_c_...` or `sf_p_...` managed IDs,
defaults to a non-mutating plan, and requires `--execute` to drop the explicit
database from the three primaries (replication removes it from replicas):

```bash
uv run select-fuzz cleanup --config config/local.yaml \
  --database '<exact managed database id>'
uv run select-fuzz cleanup --config config/local.yaml \
  --database '<exact managed database id>' --execute
```

## Regression and validation

The current query-generation boundary, exact coverage counts, explicit exclusions,
and remaining gaps are maintained in
[`docs/testing/query-generation-coverage-checklist.md`](docs/testing/query-generation-coverage-checklist.md).
It defines a closed MySQL 8.0.41 read-only subset; JSON/FULLTEXT/SPATIAL renderers remain
available for isolated tests but are excluded from the default production scope.

The controlled corpus stores generator seeds and expected tags—not copied web
SQL and not credentials:

```bash
uv run select-fuzz regression-seeds \
  --output tests/regression/seeds.json --seed 20260712
```

The online validation loop accepts only allowlisted official MySQL sources,
stores content-addressed evidence, never executes discovered SQL, audits each
shape against the typed generator, checkpoints progress, and emits a gap
report. A formal acceptance run must actually complete twelve hours; a dry run
does not satisfy that gate.

```bash
uv run python scripts/validation_12h.py \
  --duration 12h --checkpoint 30m --freeze 30m \
  --output artifacts/validation-12h \
  --seed-url https://dev.mysql.com/doc/refman/8.0/en/select.html
```

Interrupted validation runs resume from their SQLite checkpoint and immutable
source cache. Review `report/coverage.json`, `report/gaps.json`,
`report/source-manifest.json`, `report/index.html`, and the append-only
telemetry/fault logs before accepting the run. A fault schedule also needs
operator-provided `--fault-command`, matching `--fault-probe`, and an optional
`--mysql-connection-probe`; unconfigured or unrecovered scheduled faults fail
acceptance instead of being reported as successful.

Exact local three-node socket integration and a production-pipeline soak can be run
without storing a password:

```bash
SELECT_FUZZ_MYSQL_SOCKET_INTEGRATION=1 \
SELECT_FUZZ_MYSQL_SOCKETS=/tmp/baseline.sock,/tmp/custom-off.sock,/tmp/custom-on.sock \
uv run pytest -q tests/integration/test_mysql8041_*.py

PYTHONPATH=src uv run python scripts/run_mysql8041_socket_soak.py \
  --sockets /tmp/baseline.sock /tmp/custom-off.sock /tmp/custom-on.sock \
  --duration-seconds 1800 --queries-per-round 100 --workers 3 \
  --artifact-root /tmp/select-fuzz-mysql8041-soak \
  --run-id mysql8041-query-soak
```

Socket order is `baseline`, `custom_off`, `custom_on`. The soak validates exact
8.0.41 versions and retains generated databases for replay; use a fresh artifact root
for each acceptance run. Canonical round scripts are always written. Full per-worker
SQL is written only when `full_thread_sql_log` is enabled, keeping the default durable
artifact volume bounded.

## Development gates

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q --cov=select_fuzz --cov-branch \
  --cov-report=term --cov-report=json:coverage.json
uv run python scripts/check_coverage.py coverage.json \
  --min-lines 90 --min-branches 85
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test:coverage
npm --prefix frontend run build
npm --prefix frontend run e2e
git diff --check
```

Tests marked `mysql` require opt-in and environment-only credentials. The
legacy three-socket suite remains a generator/executor compatibility gate;
primary/replica routing and lag behavior require six configured cloud endpoints.
