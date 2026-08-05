# Composite Index Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ordinary, unique, mixed-direction, prefix, and three-to-four-part composite BTREE indexes randomly reachable in fuzz, performance, and correctness modes while excluding FULLTEXT and SPATIAL from production generation.

**Architecture:** Add one representation-independent composite-index planner that returns immutable column/part blueprints under a byte budget. Each existing mode adapts those blueprints to its current DDL or `IndexDef` representation and uses its existing seeded RNG and index-count ceiling.

**Tech Stack:** Python 3.11+, frozen dataclasses, `random.Random`, Pydantic configuration, pytest, Ruff, mypy, MySQL 8.0.

## Global Constraints

- Composite coverage is seed-randomized, not mandatory per table or batch.
- The same seed must remain byte-stable.
- No new configuration fields are introduced.
- Existing `max_indexes_per_table` limits always win.
- Unique composite indexes include `id`; partitioned unique indexes also include every partition column.
- Prefixes and full parts must remain inside the effective InnoDB index-byte budget.
- Production fuzz, performance, and correctness output must contain no FULLTEXT or SPATIAL indexes.
- Existing worker topology, query routing, data generation, and fuzz schema-refresh semantics must not change.

---

### Task 1: Representation-independent composite index planner

**Files:**
- Create: `src/select_fuzz/generation/composite_indexes.py`
- Create: `tests/generation/test_composite_indexes.py`

**Interfaces:**
- Produces: `CompositeIndexFamily`, `CompositeColumn`, `CompositeIndexPartPlan`, `CompositeIndexPlan`, and `build_composite_index_candidates(columns, rng, index_byte_budget, identity_column="id", unique_required_columns=())`.
- Consumers: fuzz, performance, and correctness schema adapters in Tasks 2–4.

- [ ] **Step 1: Write failing planner tests**

```python
def test_planner_builds_all_composite_families_within_budget() -> None:
    columns = (
        CompositeColumn("id", "BIGINT UNSIGNED"),
        CompositeColumn("counter", "INT"),
        CompositeColumn("tenant_id", "BIGINT UNSIGNED"),
        CompositeColumn("payload", "VARCHAR(64)", charset_bytes=4),
        CompositeColumn("body", "TEXT", charset_bytes=4),
        CompositeColumn("token", "VARBINARY(32)", charset_bytes=1),
    )
    candidates = build_composite_index_candidates(
        columns,
        rng=random.Random(17),
        index_byte_budget=3072,
    )
    assert {candidate.family for candidate in candidates} == set(CompositeIndexFamily)
    assert all(len(candidate.parts) >= 2 for candidate in candidates)
    assert all(candidate.estimated_bytes <= 3072 for candidate in candidates)


def test_planner_is_deterministic_and_omits_unsafe_candidates() -> None:
    columns = (
        CompositeColumn("id", "BIGINT UNSIGNED"),
        CompositeColumn("document", "JSON"),
        CompositeColumn("location", "POINT"),
    )
    first = build_composite_index_candidates(columns, rng=random.Random(9), index_byte_budget=16)
    second = build_composite_index_candidates(columns, rng=random.Random(9), index_byte_budget=16)
    assert first == second == ()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `PYTHONPATH=src .venv/bin/pytest -q tests/generation/test_composite_indexes.py`

Expected: collection fails because `select_fuzz.generation.composite_indexes` does not exist.

- [ ] **Step 3: Implement the immutable planner**

Implement these exact contracts:

```python
class CompositeIndexFamily(StrEnum):
    ORDINARY = "ordinary"
    UNIQUE = "unique"
    MIXED_DIRECTION = "mixed_direction"
    PREFIX = "prefix"
    WIDE = "wide"


@dataclass(frozen=True, slots=True)
class CompositeColumn:
    name: str
    mysql_type: str
    charset_bytes: int = 1


@dataclass(frozen=True, slots=True)
class CompositeIndexPartPlan:
    column_name: str
    estimated_bytes: int
    prefix_length: int | None = None
    descending: bool = False


@dataclass(frozen=True, slots=True)
class CompositeIndexPlan:
    family: CompositeIndexFamily
    parts: tuple[CompositeIndexPartPlan, ...]
    unique: bool = False

    @property
    def estimated_bytes(self) -> int:
        return sum(part.estimated_bytes for part in self.parts)
```

`build_composite_index_candidates` must validate a positive budget, ignore JSON/spatial columns, calculate fixed and declared variable widths, treat TEXT/BLOB as prefix-only, shuffle eligible copies with the supplied RNG, include `identity_column` in unique plans, append deduplicated `unique_required_columns`, and return only plans containing at least two distinct columns within budget.

- [ ] **Step 4: Run planner tests and verify GREEN**

Run: `PYTHONPATH=src .venv/bin/pytest -q tests/generation/test_composite_indexes.py`

Expected: all planner tests pass.

- [ ] **Step 5: Run focused static checks**

Run: `.venv/bin/ruff check src/select_fuzz/generation/composite_indexes.py tests/generation/test_composite_indexes.py`

Run: `.venv/bin/mypy src/select_fuzz/generation/composite_indexes.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit the planner**

```bash
git add src/select_fuzz/generation/composite_indexes.py tests/generation/test_composite_indexes.py
git commit -m "feat: add safe composite index planner"
```

### Task 2: Concurrent fuzz composite indexes

**Files:**
- Modify: `src/select_fuzz/modes/fuzz/schema.py`
- Modify: `tests/modes/fuzz/test_schema.py`

**Interfaces:**
- Consumes: `build_composite_index_candidates` from Task 1.
- Produces: deterministic `FuzzIndexSpec` DDL named by composite family.

- [ ] **Step 1: Add failing fuzz reachability and limit tests**

Add helpers that classify generated DDL by the `idx_comp_ordinary`, `uq_comp_unique`, `idx_comp_mixed_direction`, `idx_comp_prefix`, and `idx_comp_wide` name prefixes. Generate tables across a bounded seed range with `min_indexes_per_table=4` and `max_indexes_per_table=12` and assert:

```python
assert reached == set(CompositeIndexFamily)
assert all(len(spec.indexes) <= config.max_indexes_per_table for spec in specs)
assert build_table_specs(("fuzz_t0",), config, seed=seed) == build_table_specs(
    ("fuzz_t0",), config, seed=seed
)
assert "FULLTEXT" not in ddl
assert "SPATIAL" not in ddl
```

Also assert the mixed candidate renders one default-ascending part and one `DESC` part, the prefix candidate includes `(<positive integer>)`, and the wide candidate contains at least three comma-separated parts.

- [ ] **Step 2: Run the fuzz schema test and verify RED**

Run: `PYTHONPATH=src .venv/bin/pytest -q tests/modes/fuzz/test_schema.py`

Expected: the new reachability assertion fails because no `idx_comp_*` index exists.

- [ ] **Step 3: Adapt planner output to fuzz DDL**

In `build_table_specs`, convert each `FuzzColumnSpec` to `CompositeColumn`, using four charset bytes for character/TEXT types and one for binary/BLOB and scalar types. Build composite plans with a 3072-byte budget, shuffle them with the existing table RNG, and append as many as fit between the four existing core indexes and `target_indexes`. Render parts as:

```python
rendered = _quote_identifier(part.column_name)
if part.prefix_length is not None:
    rendered += f"({part.prefix_length})"
if part.descending:
    rendered += " DESC"
```

Use `UNIQUE KEY` only for `plan.unique`; fill remaining capacity with the existing random single-column indexes.

- [ ] **Step 4: Run fuzz tests and verify GREEN**

Run: `PYTHONPATH=src .venv/bin/pytest -q tests/modes/fuzz/test_schema.py tests/modes/fuzz/test_execution.py tests/modes/fuzz/test_service.py`

Expected: all selected fuzz tests pass.

- [ ] **Step 5: Commit fuzz integration**

```bash
git add src/select_fuzz/modes/fuzz/schema.py tests/modes/fuzz/test_schema.py
git commit -m "feat: generate composite indexes in fuzz mode"
```

### Task 3: Performance composite indexes

**Files:**
- Modify: `src/select_fuzz/performance/fuzz.py`
- Modify: `tests/performance/test_fuzz.py`

**Interfaces:**
- Consumes: the Task 1 planner.
- Produces: BTREE `IndexDef` candidates that preserve scalable setup behavior.

- [ ] **Step 1: Add failing performance reachability tests**

Generate `PerformanceFuzzTemplate` instances over a fixed bounded seed range, classify non-primary index names by composite family, and assert all five families are reached. For each schema assert:

```python
assert all(len(table.indexes) <= 6 for table in case.schema.tables)
assert all(index.kind is IndexKind.BTREE for table in case.schema.tables for index in table.indexes if not index.primary)
assert all(
    index.parts[0].column_name == "id"
    for table in case.schema.tables
    for index in table.indexes
    if index.name.startswith("uq_comp_")
)
```

Retain the existing assertion that FULLTEXT, SPATIAL, and MULTIVALUE kinds never appear.

- [ ] **Step 2: Run the performance test and verify RED**

Run: `PYTHONPATH=src .venv/bin/pytest -q tests/performance/test_fuzz.py`

Expected: the new reachability assertion fails because composite family names do not exist.

- [ ] **Step 3: Add composite candidates to `_table_plan`**

Convert `ColumnDef` values to `CompositeColumn`, deriving charset width as one for ASCII/latin1/binary and four for utf8mb4. Translate part plans to `IndexPart` using `SortDirection.DESC` when requested and `SortDirection.ASC` otherwise. Mix translated composite candidates with the existing single/functional candidates before seeded selection under `max_indexes_per_table - 1`; keep invisible-index randomization.

- [ ] **Step 4: Run performance tests and verify GREEN**

Run: `PYTHONPATH=src .venv/bin/pytest -q tests/performance/test_fuzz.py tests/performance/test_materialization.py tests/performance/test_shared_round.py`

Expected: all selected performance tests pass.

- [ ] **Step 5: Commit performance integration**

```bash
git add src/select_fuzz/performance/fuzz.py tests/performance/test_fuzz.py
git commit -m "feat: generate composite indexes in performance mode"
```

### Task 4: Correctness composite indexes and production profile exclusions

**Files:**
- Modify: `src/select_fuzz/generation/schema.py`
- Modify: `src/select_fuzz/correctness.py`
- Modify: `tests/generation/test_schema.py`
- Modify: `tests/service/test_round_engine.py`

**Interfaces:**
- Consumes: the Task 1 planner.
- Produces: safe composite `IndexDef` candidates and `_production_schema_targets(catalog, replica_mode)` returning sanitized production `FeatureSpec` values.

- [ ] **Step 1: Add failing correctness reachability tests**

Extend the regular-schema reachability test to classify planner-derived index names and assert all five families are reachable over a bounded seed window. Add assertions that mixed candidates contain both sort directions, prefix candidates contain a non-null prefix, wide candidates contain three or four parts, and unique candidates begin with `id`.

For partitioned schemas, retain the existing invariant:

```python
assert partition_columns <= set(index.column_names)
```

for every unique index.

- [ ] **Step 2: Add a failing production profile-filter test**

Create a `FeatureCatalog` containing one `FeatureSpec` whose compatible profiles are regular, FULLTEXT, and SPATIAL. Call `_production_schema_targets(catalog, replica_mode=False)` and assert the returned target contains only `regular_innodb`. Also materialize a bounded seed range with `GeneratedRoundSource` and assert:

```python
assert materialized.schema.profile not in {
    SchemaProfile.FULLTEXT_INNODB,
    SchemaProfile.SPATIAL_INNODB,
}
assert all(
    index.kind not in {IndexKind.FULLTEXT, IndexKind.SPATIAL}
    for table in materialized.schema.tables
    for index in table.indexes
)
```

- [ ] **Step 3: Run correctness tests and verify RED**

Run: `PYTHONPATH=src .venv/bin/pytest -q tests/generation/test_schema.py tests/service/test_round_engine.py`

Expected: composite-family reachability and/or production profile exclusion fails before implementation.

- [ ] **Step 4: Add planner candidates to `SchemaGenerator._indexes_for`**

For the regular/partitioned/temporary candidate branch, build planner input from `ColumnDef` values and `_effective_index_budget(limits)`. Pass `partition_columns` as `unique_required_columns`, translate plans to `IndexDef`, remove duplicate part/direction/prefix/uniqueness signatures, then include them in the existing shuffled candidate selection. Existing mandatory foreign-key indexes and special low-level profile implementations remain intact.

- [ ] **Step 5: Exclude FULLTEXT and SPATIAL from correctness production selection**

Define a frozen production-profile set containing regular, partitioned, temporary, foreign-key, and JSON multivalue profile values. Implement `_production_schema_targets(catalog: FeatureCatalog, *, replica_mode: bool) -> tuple[FeatureSpec, ...]` by intersecting every enabled target's `compatible_profiles` with that set, removing temporary in replica mode, using `dataclasses.replace` to return sanitized targets, and dropping targets whose intersection is empty. Call this helper from `GeneratedRoundSource.materialize` before choosing `schema_target`.

- [ ] **Step 6: Run correctness tests and verify GREEN**

Run: `PYTHONPATH=src .venv/bin/pytest -q tests/generation/test_schema.py tests/service/test_round_engine.py tests/generation/test_data.py`

Expected: all selected correctness and data-generation tests pass.

- [ ] **Step 7: Commit correctness integration**

```bash
git add src/select_fuzz/generation/schema.py src/select_fuzz/correctness.py tests/generation/test_schema.py tests/service/test_round_engine.py
git commit -m "feat: extend correctness composite index coverage"
```

### Task 5: Full verification and local MySQL smoke runs

**Files:**
- Modify only files required by defects reproduced during verification.
- Write smoke artifacts under `/private/tmp`, not the repository.

**Interfaces:**
- Consumes: all preceding tasks.
- Produces: automated and live MySQL evidence for the acceptance criteria.

- [ ] **Step 1: Run focused composite-index tests**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/generation/test_composite_indexes.py \
  tests/modes/fuzz/test_schema.py \
  tests/performance/test_fuzz.py \
  tests/generation/test_schema.py \
  tests/service/test_round_engine.py
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the full automated suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`

Run: `.venv/bin/ruff check .`

Run: `.venv/bin/mypy src`

Run: `git diff --check`

Expected: every command exits 0; document any pre-existing skipped integration tests separately.

- [ ] **Step 3: Start local MySQL and run reduced fuzz smoke**

Use a `/private/tmp` configuration with one database, one writer, three readers, one table, 100 rows, 50–60 columns, 12 maximum indexes, a short duration, and a seed known from the reachability tests to select composite candidates. Keep the generated database.

- [ ] **Step 4: Run reduced performance and correctness smokes**

Use small table/row/query ceilings and seeds known to produce composites. Keep the generated databases and artifacts. Do not enable FULLTEXT or SPATIAL profiles.

- [ ] **Step 5: Query live index metadata**

Query `information_schema.statistics`, grouping by schema/table/index and ordering by `seq_in_index`. Record `non_unique`, `column_name`, `expression`, `collation`, and `sub_part`. Confirm each mode materialized at least one multi-part index and that production schemas contain no FULLTEXT or SPATIAL index types.

- [ ] **Step 6: Stop MySQL and report evidence**

Stop the local service if it was stopped before testing. Report artifact paths, retained database names, composite families observed, connection cleanup, test totals, lint/type results, and any limitation of the single-node local topology.

- [ ] **Step 7: Commit verification-only fixes if any**

Stage only files changed to fix reproduced failures and commit them with a message that names the verified defect. If verification required no code changes, do not create an empty commit.
