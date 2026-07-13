# Select Fuzz

Select Fuzz is a deterministic MySQL 8.0.41 differential test product for a
three-node topology:

- `baseline`: unmodified open-source MySQL 8.0.41;
- `custom_off`: the custom engine with parallel query disabled globally;
- `custom_on`: the custom engine with parallel query enabled globally.

It has separate correctness and performance modes. Correctness compares typed
results or normalized errors on all three nodes. Performance uses only
`EXPLAIN ANALYZE FORMAT=TREE`, calibrates CPU-dense work to a bounded execution
window, and reports configurable regressions against both baseline and
`custom_off`. Every round uses a fresh retained database for reproduction.

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
```

Edit only the three endpoints and optional role probes in the ignored
`config/local.yaml`. The three servers must have identical resources and
isolated CPU, memory, and storage.

## CLI

Always run `doctor` first. Configuration differences are warnings; missing
MySQL 8.0.41 capabilities or required permissions are fatal.

```bash
uv run select-fuzz doctor --mode correctness --config config/local.yaml
uv run select-fuzz run --mode correctness --config config/local.yaml --rounds 1
uv run select-fuzz run --mode performance --config config/local.yaml --rounds 1
```

Omit `--rounds` for continuous operation, or use `--duration-seconds` for a
bounded run. Correctness row range, worker count, queries per round, limits,
query-lane ratios, performance degradation ratio, and calibration scale are
all YAML/CLI controlled.

Findings are appended and fsynced immediately. Rebuild a report or replay a
finding on a new retained database with:

```bash
uv run select-fuzz report --artifacts artifacts --output reports/latest.html
uv run select-fuzz replay --config config/local.yaml --artifacts artifacts \
  --finding '<case id>'
```

The local React control plane is started with:

```bash
uv run select-fuzz serve --config config/local.yaml --artifacts artifacts
```

It binds to loopback only. The UI starts and stops both modes, streams events,
and exposes findings, reports, and replay status.

Databases are retained by default and are never cleaned automatically. The
`cleanup` command accepts only generated `sf_c_...` or `sf_p_...` managed IDs,
defaults to a non-mutating plan, and requires `--execute` to drop the explicit
database from all three nodes:

```bash
uv run select-fuzz cleanup --config config/local.yaml \
  --database '<exact managed database id>'
uv run select-fuzz cleanup --config config/local.yaml \
  --database '<exact managed database id>' --execute
```

## Regression and validation

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

Tests marked `mysql` require opt-in, environment-only credentials and exact
three-node MySQL 8.0.41 endpoints. A local MySQL 8.0.45 service is smoke-only
and cannot satisfy the release gate.
