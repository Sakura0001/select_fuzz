# Run failure diagnostics design

## Goal

Make CLI failures actionable in the owner's authorized test environment. A
failed fuzz generation must show the underlying per-database error instead of
only `_GenerationBuildError`. Replica marker timeouts must identify replication
visibility as the failed phase.

## CLI behavior

The `run` command will print the exception type followed by its complete string
message:

```text
run failed: _GenerationBuildError: fuzz generation build failed: database[0]=TimeoutError: replica synchronization timeout after 300 seconds; database=sf_f_example; last probe error=Unknown database
```

This applies to every run mode and every exception. The owner explicitly
accepts raw exception text in this private test environment. Python tracebacks
remain suppressed by the CLI.

## Fuzz generation errors

Generation materialization will retain, for every failed database:

- database ordinal;
- generated database name;
- exception type;
- complete exception message.

`_GenerationBuildError` will render all failures rather than discarding all but
their types. The `fuzz_generation_failed` JSONL event will record the same
fields so terminal output and retained artifacts agree.

## Replica synchronization timeout

The replica marker wait will report:

- configured wait duration;
- generated database name;
- last replica probe exception type and message, when a probe raised;
- `replication marker not visible` when probes succeeded but the expected marker
  row never appeared.

The timeout remains a hard generation failure. The runner does not fall back to
an old generation, retry the whole batch, or delete any created database.

## Verification

- Update the CLI test to require raw exception text while continuing to require
  no traceback.
- Add materialization tests for a probe exception and for a visible connection
  whose marker row never reaches the expected value.
- Add service tests proving all database failure details appear in both
  `_GenerationBuildError` and `fuzz_generation_failed`.
- Run focused tests, static checks, and the relevant full regression gates.
- Combine the verified diagnostic behavior with the separately approved
  20-row fuzz startup configuration, then run a six-database, 600-second local
  primary/replica validation.

## Scope

This feature changes diagnostics only. It does not change query routing,
connection counts, SQL generation, DML weights, timeouts, refresh semantics, or
database retention.
