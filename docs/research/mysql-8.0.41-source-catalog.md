# MySQL 8.0.41 Official Query-Shape Catalog

This document records the provenance and maintenance rules for
`catalog/mysql-8.0.41-query-shapes.yaml`. The catalog stores structural
signatures only. It does not copy, retain, or execute query text found on web pages.

## Canonical model

The acceptance boundary is the parser and parse-tree implementation from the exact
`mysql-8.0.41` source tag:

- [MySQL 8.0.41 parser grammar](https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/sql_yacc.yy)
- [MySQL 8.0.41 parse-tree nodes](https://raw.githubusercontent.com/mysql/mysql-server/mysql-8.0.41/sql/parse_tree_nodes.h)

The catalog normalizes queries around this structural model:

```text
QueryExpression(
  WithClause?,
  QuerySpecification | ParenthesizedQuery | SetOperation,
  OrderBy?,
  Limit?
)
```

`QuerySpecification` owns projection, table references, filtering, grouping,
HAVING, and named windows. Table references own regular tables, explicit partition
selection, joins, derived tables, lateral derived tables, and `JSON_TABLE` table
functions. This is deliberately an AST vocabulary, not a text-template vocabulary.

## Schema v2 contract

Schema v2 is intentionally closed rather than extensible by accident. The validator
uses a duplicate-key-rejecting `yaml.SafeLoader` and exact key allowlists at the
catalog, source, feature, variant, and evidence levels. Unknown keys and unknown
categories fail validation instead of being ignored.

The checked-in snapshot contains 23 source records, 19 feature records, and 62
variant records. Every feature and variant has the same executable metadata shape:

- A stable `snake_case` ID and a strict category (feature only).
- A three-component `min_version` no newer than 8.0.41.
- Reviewed AST-node, semantic-guard, and compatible-profile enum IDs.
- An integer generation weight and nonempty evidence list.

The YAML contains no SQL templates, prose descriptions, or executable fragments in
these structural records. Locators are stable symbolic anchors such as grammar rule,
parse-tree class, release-note feature, or manual constraint identifiers. SQL text is
rendered later from internal ASTs; catalog data cannot bypass generator safety checks.

`guard_definitions` and `profile_definitions` are complete reviewed enums. Their
values must exactly equal the validator's allowlists, so adding a new behavior or
scene is a deliberate code-review change rather than an unvalidated YAML extension.

Production code also freezes the exact reviewed source, feature, and variant ID
manifests. A missing, renamed, or additional ID is rejected. Schema v2 has no implicit
extension namespace: an incompatible extension requires an explicit schema-version
bump and a corresponding validator review.

The complete canonical YAML data model is also hashed with sorted-key compact JSON.
The independent `REVIEWED_CATALOG_SHA256` lock catches otherwise schema-valid edits to
weights, hashes, locator patterns, evidence, versions, or ordering semantics. A review
that intentionally changes any catalog value must update this code-owned digest.

Loading is not generator support. `FeatureCatalog` round-trips all 62 reviewed variant
rows, while `capability_status` is derived from the internal generator registry.
Only explicitly registered variants are returned as scheduling targets; every other
loaded row is exposed as a `catalogued_gap`. The initial registry is intentionally
empty until renderers and their tests land, so this catalog cannot claim 62/62
generator reachability. Registration is necessary but not sufficient: the variant's
own evidence and its parent feature evidence must all reference `verified` source
locks. `refresh_required` evidence is reported as an evidence-lock gap and is never
scheduled.

## Initial coverage

The first slice covers the following independently measurable families:

- Basic and parenthesized query expressions, projection modifiers, filtering,
  grouping, HAVING, named windows, final ordering, limit, and offset.
- Comma, cross, inner, straight, left, right, natural, `ON`, and `USING` joins,
  including nested join trees and table index hints.
- Scalar, column, row, and table subqueries; correlated forms; `EXISTS`, `IN`,
  quantified comparisons, nullable inputs, and empty inputs.
- Regular and lateral derived tables, including dependency-direction restrictions.
- Single, multiple, reused, dependent, and recursive CTEs with bounded recursion.
- `UNION`, `INTERSECT`, and `EXCEPT`, their duplicate modes and precedence, and
  `SELECT`, `TABLE`, and `VALUES` query primaries.
- Global and grouped aggregates, HAVING, ROLLUP, and `GROUPING`.
- Ranking, navigation, value, and aggregate windows; inline and named windows;
  `ROWS` and `RANGE` frames; unsupported window constructs as negative mutations.
- All `JSON_TABLE` column forms and implicit correlation, plus safe JSON function
  families and multivalue-index predicates.
- Simple and searched `CASE`, control-flow functions, optimizer-hint scopes,
  explicit partition selection, deterministic operators, aggregate, full-text,
  and spatial expression families.

Each catalog item has a unique ID, a target-compatible minimum version, official
source references, and at least one structural AST, variant list, or semantic guard.

## Version evidence

The exact feature gates used by the initial slice come from official release notes:

| Version | Covered feature evidence |
| --- | --- |
| [8.0.1](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-1.html) | CTEs, recursive CTEs, `GROUPING`, descending indexes, join-order and index-merge hints |
| [8.0.2](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-2.html) | Window functions |
| [8.0.4](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-4.html) | `JSON_TABLE` |
| [8.0.13](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-13.html) | Functional indexes |
| [8.0.14](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-14.html) | Lateral derived tables and JSON aggregate windows |
| [8.0.17](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-17.html) | JSON multivalue indexes, overlap/member predicates, and JSON schema validation |
| [8.0.19](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-19.html) | `TABLE`, `VALUES`, and recursive-member limit |
| [8.0.20](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-20.html) | Index-level optimizer hints |
| [8.0.21](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-21.html) | `JSON_VALUE` |
| [8.0.22](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-22.html) | Parenthesized query expressions and derived-condition pushdown |
| [8.0.31](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-31.html) | `INTERSECT`, `EXCEPT`, and nested parenthesized-expression semantics |

For long-established syntax, `5.7.0` is used as a conservative compatibility floor,
not as a claim that the feature was first introduced in that release.

Each syntax feature and each of its variants independently cites an exact-tag parser
grammar or parse-tree locator. Every `min_version` newer than the conservative 5.7.0
floor also cites the release-note record for that exact version. A feature-level
citation does not satisfy a missing variant citation.

## Source integrity and snapshot policy

Every source record contains `kind`, `version`, URL, `hash_scope`, `lock_state`,
`content_sha256`, and `checked_at`. A verified digest is always calculated from
downloaded official content; it is never a hash of a URL or hand-authored summary.
`checked_at` records the review date, not a minute-level timestamp. The verifier pins
the reviewed representation headers and rejects redirects. Hashing then uses one of
two explicit scopes:

- `exact_source` is restricted to raw files under the immutable
  `mysql/mysql-server/mysql-8.0.41` tag. The catalog currently pins the parser grammar
  and parse-tree header with `response_bytes`; their exact downloaded bytes are hashed.
- `release_note` is restricted to an official version-specific MySQL release-note
  page and uses the version claimed by the evidence consumer. It uses
  `docs_body_text_v1`.
- `manual_snapshot` and `version_reference_snapshot` are rolling official pages.
  They also use `docs_body_text_v1`.

Raw `dev.mysql.com` HTML cannot be locked reproducibly because its page shell contains
per-request Akamai BOOMR values such as request IDs, timestamps, client ports, and
tokens. `docs_body_text_v1` therefore requires exactly one `<div id="docs-body">`,
rejects missing or duplicate bodies and non-MySQL/error/challenge titles, discards
script/style/noscript/SVG content, decodes character references, applies NFC, collapses
whitespace, and hashes UTF-8 visible text. Locator matching runs against the same
normalized body, so page chrome cannot satisfy evidence.

As of this snapshot, the two exact-tag sources plus the 8.0.19 and 8.0.41 release
notes are `verified`. The 8.0.41 normalized body has SHA-256
`a48124031d81275a43585468e5e26f8c2842729284f05671374b8dd3925d59f4`.
The other 19 documentation records are explicitly `refresh_required` with null hashes
because network permission was unavailable after the stable scope was introduced.
This is an intentional fail-closed state: the verifier refuses to report success until
those pages are fetched, inspected, and locked under `docs_body_text_v1`.

Only sources referenced by this catalog slice are retained. Adding unused pages does
not increase coverage; adding a feature or variant requires granular evidence and a
new verified source record when existing records are insufficient.

Each source also owns a strict `locators` mapping. Every symbolic evidence locator is
resolved to exactly one nonempty literal or bounded regular-expression pattern, and
unused locator definitions are rejected. The source-lock verifier downloads each
canonical URL once, derives the declared stable scope, hashes it, and verifies every
unique locator in that same inert text. It never sends downloaded content to MySQL, a
shell, or a query renderer.

Offline fixture tests inject byte fetchers. Real verification is deliberately opt-in:

```text
SELECT_FUZZ_RUN_ONLINE=1 uv run pytest -m online tests/catalog/test_catalog_source_lock.py
uv run python scripts/verify_catalog_sources.py catalog/mysql-8.0.41-query-shapes.yaml
uv run python scripts/verify_catalog_sources.py --refresh catalog/mysql-8.0.41-query-shapes.yaml
```

The CLI exits nonzero on a pending lock, download failure, redirect, invalid body/title,
hash drift, malformed locator, or missing literal/regex match. Reviewers should treat
normalized body hash drift as a source-change event, inspect the changed content, and
update the lock only after review. `--refresh` is read-only: it prints candidate scoped
digests and locator counts even for pending records. A reviewer must inspect the
candidate content, update `content_sha256` and `lock_state`, then update the canonical
catalog digest before normal verification can pass.

## MySQL 8.0.41 regression seeds

The [8.0.41 release notes](https://dev.mysql.com/doc/relnotes/mysql/8.0/en/news-8-0-41.html)
identify high-value structural seeds for the target release:

- Charset-sensitive view filtering over a union.
- A descending primary key combined with index-merge-eligible predicates.
- A filtered subquery eligible for index-subquery and materialization paths.
- Equal-operation union chains that exercise parser flattening.
- ROLLUP aggregates inside subqueries and row comparators.
- An antijoin with nullable key components and a build side sized to encourage spill.
- A distinct outer query with a negative membership subquery and a group-skip-scan
  candidate index.
- Optimizer-hint comment payloads containing hash or double-dash token sequences.

These entries describe syntax and plan intent separately. A generated query is a
syntax hit even if the optimizer chooses another plan; a `plan_confirmed` counter is
required before claiming physical-path coverage.

## Setup compatibility constraints

The catalog keeps regular, partitioned, temporary, foreign-key, full-text, spatial,
and JSON multivalue scenes as compatible profiles instead of arbitrarily mixing all
features.

Important guards are derived from the official [CREATE TABLE](https://dev.mysql.com/doc/refman/8.0/en/create-table.html),
[CREATE INDEX](https://dev.mysql.com/doc/refman/8.0/en/create-index.html),
[partition limitations](https://dev.mysql.com/doc/refman/8.0/en/partitioning-limitations.html),
[foreign-key rules](https://dev.mysql.com/doc/refman/8.0/en/create-table-foreign-keys.html),
[full-text restrictions](https://dev.mysql.com/doc/refman/8.0/en/fulltext-restrictions.html),
[spatial index rules](https://dev.mysql.com/doc/refman/8.0/en/spatial-index-optimization.html),
and [InnoDB limits](https://dev.mysql.com/doc/refman/8.0/en/innodb-limits.html):

- Every unique key on a partitioned table includes every partition-expression
  column. Partitioned InnoDB tables are kept separate from foreign-key, full-text,
  spatial-column, and temporary-table scenes.
- Temporary-table setup and queries remain on one session per node; only carrier
  database metadata and replay instructions survive session close.
- Foreign-key columns use compatible types, charset, collation, and leftmost index
  positions; prefix-indexed LOB columns and virtual generated targets are excluded.
- Full-text columns share charset and collation. Spatial indexes use one non-null,
  fixed-SRID spatial column. Multivalue indexes use one supported JSON array key part.
- Index part count, secondary index count, page-size-aware key bytes, row bytes, and
  actual LOB/JSON value sizes are checked before setup is emitted.

## Function discovery loop

The authoritative enumeration source is the official
[Built-In Functions and Operators Version Reference](https://dev.mysql.com/doc/mysqld-version-reference/en/built-in-functions.html).
Each discovery epoch should:

1. Import rows available in 8.0.41 using introduced, deprecated, removed, and series
   availability metadata.
2. Follow the official detail link and extract only argument, return-type, placement,
   and semantic constraints.
3. Classify the function as deterministic, denied, or configuration-gated.
4. Compare the structural signature with generator capabilities.
5. Record a gap and synthesize a safe internal AST when no generator path exists.
6. Run static safety, complexity, determinism, and version checks before any query is
   rendered or submitted.

Randomness, UUIDs, current clock values, session identity, server identity, row-state
information, stored/loadable functions, locks, waits, sleeps, and unordered aggregate
output are denied in both correctness and performance modes. Collation, ICU,
time-zone-table, and full-text-parser-dependent functions require matching capability
fingerprints on all three nodes.

## Contract and safety tests

`tests/catalog/test_official_catalog.py` treats the catalog as untrusted input. Its
positive contract checks required feature and variant IDs, exact category coverage,
enum membership, stable versions, source domains, SHA-256 shape, per-record evidence,
and exact-version release-note claims. Negative mutations prove rejection of:

- duplicate YAML keys;
- syntax newer than MySQL 8.0.41;
- SQL payloads in AST, identifier, or guard fields;
- unknown categories or schema keys; and
- variants with missing evidence.

The round-trip table test compares all 62 loaded production records across ID, family,
minimum version, profiles, guards, and evidence. Separate capability tests prove that
loaded rows remain gaps until their IDs are present in the generator registry. Source
lock fixture tests cover exact-byte hashing, literal and regex matches and misses,
inert hostile-looking SQL text, CLI exit status, and the gated real-source path.

These tests validate catalog admissibility and auditability. They do not claim that a
catalog entry has achieved runtime or physical-plan coverage; generator and execution
telemetry must count those separately.

## Known follow-up gaps

The initial slice intentionally leaves these for later catalog epochs:

- Views with explicit merge/temporary algorithms, generated columns, CHECK
  constraints, invisible columns, and generated invisible primary keys.
- Exhaustive function-by-function argument domains and every spatial subtype.
- A plan-signature catalog for index merge variants, skip scans, loose/group scans,
  hash join spill, materialization, and derived-condition pushdown.
- Exact error identities for every negative mutation under each supported SQL mode.

Those omissions are explicit gaps, not claims of complete coverage.
