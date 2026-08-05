# Fuzz 20-row minimum design

## Goal

Allow a fuzz table to start with 20 rows so local concurrency diagnostics can
separate client scheduling behavior from large-data execution cost. Preserve
the existing primary-writer, primary-reader, and replica-reader topology.

## Configuration behavior

- Change only `fuzz.initial_rows_per_table` validation from a minimum of 100
  rows to a minimum of 20 rows.
- Continue rejecting values below 20.
- Keep the existing invariant that
  `initial_rows_per_table * initial_tables <= max_rows_per_database`.
- Do not change defaults, thread counts, DML weights, query generation,
  replication checks, schema retention, or refresh behavior.

The local diagnostic configuration will use:

```yaml
initial_rows_per_table: 20
max_rows_per_database: 2000
batch_rows_min: 1
batch_rows_max: 5
delete_batch_rows_min: 1
delete_batch_rows_max: 2
```

With eight initial tables, each database starts with 160 rows and retains room
for bounded DML growth.

## Verification

- Add a configuration test proving that 20 rows is accepted.
- Add a boundary test proving that 19 rows is rejected.
- Re-run the focused configuration and fuzz tests, then the relevant static
  checks.
- Validate the temporary configuration with `doctor`.
- Start a six-database, 600-second local primary/replica fuzz run and confirm
  all expected worker connections appear after generation becomes ready.

## Scope boundaries

This change does not alter generated SQL semantics, worker routing, connection
counts, query timeouts, index generation, or database cleanup behavior. The
local run creates a new retained six-database batch and does not delete prior
diagnostic databases.
