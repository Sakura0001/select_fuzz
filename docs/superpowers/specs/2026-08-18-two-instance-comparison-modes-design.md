# Two-Instance Comparison Modes Design

## Objective

Change `correctness` and `performance` from the fixed three-role, six-endpoint
topology to a real two-instance comparison. The two active roles are
`custom_off` and `custom_on`; `custom_off` is the only baseline. Each endpoint is
an independently writable MySQL instance whose PQ setting is configured before
Select Fuzz starts.

The `fuzz` mode keeps its existing primary/replica topology, routing, replication
barrier, long-lived worker connections, and `fuzz.target_role` behavior.

## Scope and Compatibility

The new comparison topology is intentionally incompatible with the old
`baseline`/`custom_off`/`custom_on` six-endpoint configuration. There is no
automatic compatibility mode and no hidden or duplicated `baseline`
measurement.

New correctness and performance artifacts contain exactly two result roles:
`custom_off` and `custom_on`. Historical three-role artifacts remain readable by
the artifact reader and HTML report generator. Replaying a historical
three-role finding with the new two-instance configuration is rejected with a
specific Chinese diagnostic because the required topology no longer exists.

## Configuration Contract

The effective mode after CLI overrides controls validation.

For `correctness` and `performance`, `nodes` must contain exactly two flat
endpoints with distinct host/port pairs and the exact roles `custom_off` and
`custom_on`:

```yaml
mode: correctness

nodes:
  - role: custom_off
    host: 192.168.1.10
    port: 3306
    username_env: SELECT_FUZZ_MYSQL_USER
    password_env: SELECT_FUZZ_MYSQL_PASSWORD
    role_probe_sql: null
    role_probe_expected: null

  - role: custom_on
    host: 192.168.1.11
    port: 3306
    username_env: SELECT_FUZZ_MYSQL_USER
    password_env: SELECT_FUZZ_MYSQL_PASSWORD
    role_probe_sql: null
    role_probe_expected: null
```

Comparison entries do not accept nested `primary` or `replica` endpoints.
`replica_parameters_file` must be `null` for comparison modes because there are
no replica sessions. PQ and other engine settings are server-side prerequisites;
the runner never enables or disables them.

For `fuzz`, the existing three-role `NodeTopologyConfig` shape remains valid and
unchanged. The selected role still has explicit `primary` and `replica`
endpoints. Fuzz may continue to use a shared proxy endpoint where its current
rules allow it.

`config/example.yaml` becomes the documented two-instance correctness and
performance template. `config/intranet-fuzz.example.yaml` remains the fuzz
template. Both templates, together with the replica-parameter example used by
fuzz, are included in the CentOS 7 offline bundle.

## Active Roles and Internal Boundaries

`NodeRole.BASELINE` may remain as a legacy data value needed to parse historical
artifacts and to avoid changing unrelated fuzz role values, but it is not an
active correctness or performance role. A single ordered comparison-role
definition, `(custom_off, custom_on)`, is shared by configuration validation,
doctor, setup, execution, oracles, artifacts, and replay. This prevents each
subsystem from independently hard-coding its own role set.

Two-node comparison execution uses pair-specific coordinators and validators.
It does not emulate a triad, issue the same query twice to `custom_off`, or copy a
measurement into a fake baseline slot.

## Doctor and Preflight

For correctness and performance, doctor probes exactly the two configured
endpoints. It verifies credentials, connection establishment, required runtime
capabilities, server version information, role probes when configured, and the
connection budget relevant to the selected mode.

Missing role probes remain warnings. Duplicate endpoints, missing or extra
roles, nested primary/replica comparison entries, and non-null replica session
parameters are fatal configuration errors with specific Chinese user-facing
messages. JSON keys, issue codes, roles, and machine-readable enum values remain
English.

Fuzz doctor retains its current selected-role primary/replica behavior.

## Correctness Data Flow

1. Generate one deterministic schema, index set, data set, and query stream from
   the run seed.
2. Apply the same database setup statements in lockstep to `custom_off` and
   `custom_on`. The database name may be identical because the endpoints are
   independent servers.
3. Treat setup status, errors, and affected-row differences as findings or
   infrastructure failures according to the existing classification rules.
4. Use `custom_off` for the baseline plain-`EXPLAIN` admission check.
5. Start each formal SELECT concurrently on both endpoints behind a two-party
   barrier.
6. Compare execution status, normalized errors, column metadata, and typed
   result rows between the two roles. Any difference is a
   `custom_off/custom_on` finding with complete reproduction SQL.
7. After each configured mutation interval, apply the same bounded DML
   transaction to both endpoints. Affected-row, commit, or resulting-state
   differences are findings.

Correctness does not create the replication marker table and never invokes a
replication barrier.

## Performance Data Flow

1. Generate one deterministic performance schema, data manifest, and query set.
2. Materialize the identical manifest concurrently on `custom_off` and
   `custom_on`.
3. Verify both materializations before formal execution. A one-sided setup
   failure or evidence mismatch terminates the round without a performance
   verdict.
4. Start each `EXPLAIN ANALYZE FORMAT=TREE` concurrently on both endpoints behind
   a two-party barrier and retain the configured maximum-start-skew check.
5. Parse and record both plans and measurements.
6. Use `custom_off` as the only reference. Emit `VS_CUSTOM_OFF` when
   `custom_on/custom_off - 1` meets or exceeds
   `performance.regression_threshold`. An improvement or a ratio below the
   threshold passes.

A timeout, connection failure, invalid plan, materialization error, or excessive
start skew is classified as execution/infrastructure evidence. It must not be
converted into a performance regression conclusion.

Performance does not create replication markers, wait for replica visibility,
or emit `VS_BASELINE`.

## Connection Lifecycle and Sleep Expectations

Correctness and performance open connections for bounded setup, control, or
query tasks and close them immediately when the task finishes. They do not adopt
fuzz's long-lived reader/writer connection model. Timeout and cancellation paths
must close or abort the affected session before returning.

During execution, a connection may momentarily appear as `Sleep` between client
protocol operations, but its Sleep time must not grow while application progress
stops. After a run exits, neither comparison endpoint may retain a Select Fuzz
application connection. MySQL replication/system threads, when present in the
user's environment, are not counted as application Sleep connections.

## Artifacts, Reports, and Replay

New correctness finding manifests and result files contain exactly
`custom_off` and `custom_on`. Pairwise difference descriptions use
`custom_off/custom_on`. Pass records use `custom_off` as the canonical result
digest source.

New performance events contain exactly two measurements and two plan files.
They contain only the `VS_CUSTOM_OFF` comparison verdict. Configuration
fingerprints and diagnostic evidence are keyed by the two active roles.

The artifact reader and HTML report accept both the new two-role shape and the
historical three-role shape. New replay requires a two-role finding. A
historical three-role finding produces an explicit unsupported-topology error
instead of silently discarding the baseline result.

Existing top-level run summary fields remain stable so automation can continue
to parse rounds, queries, findings, rejected cases, over-budget cases, and stop
status.

## Error Handling

User-facing CLI and configuration diagnostics introduced or changed by this
feature are Chinese and include the concrete failure reason. Structured JSON
field names, stable issue codes, MySQL errno/sqlstate values, role identifiers,
and verdict enum values are not translated.

One endpoint failing must never be represented as a matching pair. Setup and DML
operations preserve both node results in the event/finding record. Query timeout
and connection-loss paths preserve the original MySQL exception details while
closing the connection. Performance infrastructure failures suppress regression
judgment.

## Verification Strategy

Implementation follows red-green-refactor test-driven development. Coverage
includes:

- mode-aware validation for exactly two flat, distinct comparison endpoints;
- rejection of the old six-endpoint and malformed two-endpoint shapes;
- unchanged fuzz topology validation and routing;
- two-endpoint doctor probe count, warnings, and fatal diagnostics;
- pair lockstep setup, DML, concurrent query dispatch, and cleanup;
- correctness pair oracle behavior for successes, normalized errors, metadata,
  result differences, timeouts, and resource limits;
- performance two-party start barrier, start-skew enforcement, materialization,
  timeout handling, and `VS_CUSTOM_OFF` threshold decisions;
- new two-role artifacts, report generation, replay, and historical report read
  compatibility;
- offline package contents and launcher smoke tests.

Final integration verification starts two independent MySQL 8.0.22 instances,
preconfigures one with PQ disabled and one with PQ enabled, and then:

1. runs doctor for correctness and performance;
2. completes a bounded correctness round;
3. completes a bounded performance round;
4. performs sustained bounded runs while sampling PROCESSLIST, Questions, and
   artifacts;
5. verifies there is no growing application Sleep population while progress is
   stalled;
6. verifies zero remaining Select Fuzz application connections after exit;
7. rebuilds the CentOS 7 x86_64 archive and runs the packaged launcher plus both
   doctor commands in a compatible Linux container.

## Non-Goals

- Automatically configuring or detecting the PQ switch.
- Supporting the old six-endpoint comparison configuration.
- Adding comparison-mode replication routing or replication waits.
- Changing fuzz worker topology, connection lifecycle, diagnostics, or schema
  refresh behavior.
- Fabricating a third measurement for compatibility with the former triad.
