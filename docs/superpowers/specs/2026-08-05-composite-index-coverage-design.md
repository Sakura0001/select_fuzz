# Composite Index Coverage Design

## Goal

Extend the seeded schema generation used by concurrent fuzz, performance comparison, and three-node correctness comparison so each production mode can randomly generate safe composite BTREE indexes. FULLTEXT and SPATIAL indexes must not be selected by any production mode because the target engine does not support them.

## Scope

The production generators must make these five index families reachable from deterministic random seeds:

1. A non-unique two-column index, for example `(col_a, col_b)`.
2. A unique composite index, with `id` included as a stable uniqueness component.
3. A mixed-direction index, for example `(col_a ASC, col_b DESC)`.
4. A prefix composite index, for example `(varchar_col(prefix), numeric_col)`.
5. A wide composite index containing three or four index parts.

Coverage is probabilistic rather than mandatory per table or per batch. The same seed must remain byte-stable, while a bounded deterministic seed window must demonstrate that every family is reachable in every production mode.

This change does not add configuration fields, alter worker counts, change query routing, change fuzz batch replacement semantics, or change performance/correctness comparison behavior.

## Selected Architecture

Each mode keeps its current schema representation and generation entry point:

- Concurrent fuzz continues to build `FuzzIndexSpec` DDL in `select_fuzz.modes.fuzz.schema`.
- Performance comparison continues to build `IndexDef` values in `select_fuzz.performance.fuzz`.
- Three-node correctness comparison continues to use the shared `SchemaGenerator` in `select_fuzz.generation.schema`.

The implementations use the same policy for eligible columns, prefix sizing, physical index-byte budgets, uniqueness, and sort-direction selection. Small safety helpers may be shared where their inputs are representation-independent; the three generators are not forced through a new common schema model.

This preserves the distinct execution and data-generation semantics of each mode and avoids coupling the concurrent fuzz lifecycle to the differential generators.

## Candidate Selection

Each table generator builds a deterministic candidate pool and uses its existing seeded random source to choose an allowed subset. Candidate selection must obey the configured `max_indexes_per_table`; existing mandatory indexes and profile-specific indexes take priority when capacity is limited.

The following rules apply:

- Ordinary composite indexes use two compatible indexable columns.
- Unique composite indexes include the non-null `id` column. Existing initial-data and DML generation already keep `id` unique, so no new value-distribution algorithm is required.
- Mixed-direction candidates contain at least one ascending and one descending part.
- Prefix candidates use a legal prefix on character, binary, TEXT, or BLOB data and combine it with a full scalar column.
- Wide candidates contain three or four parts selected from safe fixed-width columns and safely prefixed variable-width columns.
- Duplicate index definitions and duplicate names are rejected before selection.
- A candidate is omitted when the table has too few compatible columns or insufficient byte budget. Omission is normal and is not a generation failure.

One physical index may exhibit more than one property, but reachability tests classify and prove the five properties independently.

## Index-Size Safety

All generated composite indexes must fit the existing effective InnoDB index-byte budget.

- Fixed-width parts contribute their modeled physical width.
- utf8mb4 character prefixes reserve four bytes per character.
- Binary prefixes reserve one byte per byte.
- Prefix length is reduced when necessary to leave room for later parts.
- A candidate is skipped if a legal prefix of at least one unit cannot fit.
- Functional expressions remain separate from prefix-index generation unless the existing mode already models a legal functional index.

The correctness generator continues to pass every manifest through `SchemaRules`. Fuzz and performance generation receive equivalent focused tests for rendered DDL and byte-budget boundaries.

## Mode-Specific Behavior

### Concurrent fuzz

The four current core index forms—primary, descending, unique payload, and functional—remain available. Composite candidates fill remaining randomly selected index capacity. When the configured maximum leaves no capacity after required indexes, the table remains valid and no composite candidate is forced.

Unique composites use `id` plus another eligible column. Prefix composites may use currently excluded LOB columns only with an explicit safe prefix. Query-generation workers continue to consume index names exactly as before.

### Performance comparison

The performance table plan expands its secondary-index candidate pool. Scalable data loading remains unchanged because every unique composite contains `id`. Composite choice must not increase setup SQL size as a function of row count.

Invisible-index randomization remains supported. FULLTEXT, SPATIAL, and multivalue index kinds remain outside the performance workload.

### Three-node correctness comparison

Regular, partitioned, temporary, and foreign-key profiles gain or retain composite BTREE candidates. A unique index on a partitioned table must include every partition column. Foreign-key indexes must retain legal referenced and referencing left prefixes.

The production target/profile selection excludes `fulltext_innodb` and `spatial_innodb`. Their low-level model and rule definitions may remain for isolated library tests, but production correctness materialization must never select or render those profiles. JSON multivalue behavior is unchanged by this work.

## Failure Handling

Generation must prefer omission over invalid SQL when an index cannot be safely constructed. Invalid index length, missing compatible columns, illegal prefix use, or partition uniqueness conflicts are generation defects and must be caught by tests or schema validation before a database connection executes the DDL.

Runtime error classification is unchanged. If a validated generated index is rejected by an authorized target engine, the existing mode records the database error as it does for other generated DDL.

## Verification

Automated tests must prove:

- Seed reproducibility in all three modes.
- Reachability of all five composite-index properties over fixed bounded seed windows.
- Compliance with `max_indexes_per_table` and physical index-byte budgets.
- Legal rendering of mixed ASC/DESC, prefixed, unique, and three-to-four-part indexes.
- Stable uniqueness during initial data generation and subsequent fuzz writes.
- Inclusion of all partition columns in partitioned unique indexes.
- Preservation of foreign-key left-prefix requirements.
- Absence of FULLTEXT and SPATIAL indexes from all three production-mode outputs.

After focused tests pass, run the full unit suite, Ruff, mypy, and `git diff --check`. Then start the local MySQL 8.0 service and execute reduced-size smoke runs for fuzz, performance, and correctness. Inspect `information_schema.statistics` to confirm actual index part count, order, collation direction, uniqueness, and prefix length. Stop the local service after validation and retain generated databases unless the user requests cleanup.

## Acceptance Criteria

The feature is accepted when all automated checks pass and local MySQL smoke evidence shows that each mode can materialize seeded composite indexes without changing its execution semantics. Production-mode output must contain no FULLTEXT or SPATIAL indexes.
