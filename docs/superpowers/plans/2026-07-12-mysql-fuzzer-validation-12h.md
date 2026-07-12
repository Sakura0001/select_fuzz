# MySQL SQL Coverage Discovery and 12-Hour Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the auditable source-discovery, feature-signature, reachability, checkpoint, local-cluster, and soak tooling required to run the continuous 12-hour MySQL 8.0.41 query-shape completion loop.

**Architecture:** The product performs safe source acquisition, offline SQL isolation, versioned feature-signature extraction, generator reachability checks, append-only gap accounting, and deterministic soak execution. Code changes remain outside the running product: the agent consumes a gap, writes a failing test, implements the smallest fix, runs gates, commits locally, and starts a new epoch.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, `httpx`, `sqlglot` only for defensive tokenization (the project AST remains authoritative), pytest, Hypothesis, psutil, MySQL 8.0.41/8.0.45.

---

## File Map

- `src/select_fuzz/research/models.py`: immutable source, signature, gap, and epoch domain objects.
- `src/select_fuzz/research/source_loader.py`: allowlisted HTTP/file acquisition, cache, hash, and rate limiting.
- `src/select_fuzz/research/extractor.py`: candidate query isolation without executing source text.
- `src/select_fuzz/research/signature.py`: canonical feature-signature extraction and serialization.
- `src/select_fuzz/research/capability.py`: static and directed dynamic generator reachability audit.
- `src/select_fuzz/research/ledger.py`: SQLite checkpoint plus append-only research events.
- `src/select_fuzz/research/loop.py`: continuous 12-hour epoch coordinator with freeze window.
- `src/select_fuzz/research/report.py`: coverage matrix, saturation, gaps, and source-manifest reports.
- `src/select_fuzz/local_cluster.py`: three-instance local MySQL lifecycle manager.
- `src/select_fuzz/soak.py`: deterministic scenario scheduling and resource telemetry.
- `scripts/run_12h_coverage_loop.py`: operator entry point used by the agent during acceptance.
- `docs/testing/12h-sql-coverage-runbook.md`: exact human/agent TDD loop and recovery rules.
- `tests/unit/research/*`: deterministic research component tests.
- `tests/integration/test_local_cluster.py`: opt-in real-MySQL cluster tests.
- `tests/soak/test_soak_smoke.py`: accelerated clock and fault-injection smoke tests.

### Task 1: Versioned research domain model

**Files:**
- Create: `src/select_fuzz/research/__init__.py`
- Create: `src/select_fuzz/research/models.py`
- Test: `tests/unit/research/test_models.py`

- [ ] **Step 1: Write the failing canonicalization test**

```python
from select_fuzz.research.models import FeatureSignature, SourceRecord


def test_feature_signature_key_is_order_independent() -> None:
    left = FeatureSignature(
        version="8.0.41",
        nodes=("window", "select", "cte"),
        requirements=("decimal", "unique_tiebreaker"),
    )
    right = FeatureSignature(
        version="8.0.41",
        nodes=("cte", "select", "window"),
        requirements=("unique_tiebreaker", "decimal"),
    )
    assert left.key == right.key


def test_source_record_rejects_unhashed_content() -> None:
    try:
        SourceRecord(
            url="https://dev.mysql.com/doc/refman/8.0/en/select.html",
            source_level=1,
            version_evidence="8.0.41",
            fetched_at="2026-07-12T00:00:00Z",
            sha256="bad",
        )
    except ValueError as exc:
        assert "sha256" in str(exc)
    else:
        raise AssertionError("invalid digest was accepted")
```

- [ ] **Step 2: Run the focused test and verify red**

Run: `uv run pytest -q tests/unit/research/test_models.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'select_fuzz.research'`.

- [ ] **Step 3: Implement immutable normalized models**

```python
# src/select_fuzz/research/models.py
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SourceRecord:
    url: str
    source_level: int
    version_evidence: str
    fetched_at: str
    sha256: str

    def __post_init__(self) -> None:
        if not 1 <= self.source_level <= 5:
            raise ValueError("source_level must be between 1 and 5")
        if not _DIGEST.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase 64-character digest")


@dataclass(frozen=True, slots=True)
class FeatureSignature:
    version: str
    nodes: tuple[str, ...]
    requirements: tuple[str, ...]

    @property
    def key(self) -> str:
        payload = {
            "nodes": sorted(set(self.nodes)),
            "requirements": sorted(set(self.requirements)),
            "version": self.version,
        }
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
```

- [ ] **Step 4: Run the model tests and full unit suite**

Run: `uv run pytest -q tests/unit/research/test_models.py tests/unit`

Expected: all tests pass.

- [ ] **Step 5: Commit the domain model**

```bash
git add src/select_fuzz/research tests/unit/research/test_models.py
git commit -m "feat: add research coverage domain models"
```

### Task 2: Allowlisted acquisition and immutable source cache

**Files:**
- Create: `src/select_fuzz/research/source_loader.py`
- Create: `tests/fixtures/research/select-page.html`
- Test: `tests/unit/research/test_source_loader.py`

- [ ] **Step 1: Write failing file-cache and host-policy tests**

```python
from pathlib import Path

import pytest

from select_fuzz.research.source_loader import SourceLoader, SourcePolicyError


def test_loader_caches_content_by_sha256(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/research/select-page.html")
    loader = SourceLoader(cache_dir=tmp_path, allowed_hosts={"dev.mysql.com"})
    cached = loader.cache_bytes(fixture.read_bytes(), suffix=".html")
    assert cached.path.exists()
    assert cached.path.read_bytes() == fixture.read_bytes()
    assert cached.path.name.startswith(cached.sha256)


def test_loader_rejects_non_allowlisted_host(tmp_path: Path) -> None:
    loader = SourceLoader(cache_dir=tmp_path, allowed_hosts={"dev.mysql.com"})
    with pytest.raises(SourcePolicyError):
        loader.validate_url("https://example.com/query.sql")
```

- [ ] **Step 2: Run tests and confirm the missing loader failure**

Run: `uv run pytest -q tests/unit/research/test_source_loader.py`

Expected: import fails for `select_fuzz.research.source_loader`.

- [ ] **Step 3: Implement bounded cache and URL policy**

```python
# src/select_fuzz/research/source_loader.py
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse


class SourcePolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CachedSource:
    path: Path
    sha256: str


class SourceLoader:
    def __init__(self, cache_dir: Path, allowed_hosts: set[str], max_bytes: int = 2_000_000):
        self.cache_dir = cache_dir
        self.allowed_hosts = frozenset(allowed_hosts)
        self.max_bytes = max_bytes

    def validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise SourcePolicyError(f"source host is not allowlisted: {parsed.hostname}")

    def cache_bytes(self, content: bytes, suffix: str = ".bin") -> CachedSource:
        if len(content) > self.max_bytes:
            raise SourcePolicyError("source exceeds max_bytes")
        digest = sha256(content).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{digest}{suffix}"
        if not path.exists():
            path.write_bytes(content)
        return CachedSource(path=path, sha256=digest)
```

- [ ] **Step 4: Add an `httpx` adapter test with `MockTransport` and implement timeout, redirect, byte-limit, ETag, and minimum one-second host rate limiting**

Run: `uv run pytest -q tests/unit/research/test_source_loader.py`

Expected: host rejection, cache reuse, byte limit, timeout, and `304 Not Modified` tests all pass without internet access.

- [ ] **Step 5: Commit the source loader**

```bash
git add src/select_fuzz/research/source_loader.py tests/unit/research/test_source_loader.py tests/fixtures/research/select-page.html
git commit -m "feat: add safe SQL source acquisition cache"
```

### Task 3: Offline candidate isolation and query safety envelope

**Files:**
- Create: `src/select_fuzz/research/extractor.py`
- Test: `tests/unit/research/test_extractor.py`

- [ ] **Step 1: Write failing extraction safety tests**

```python
from select_fuzz.research.extractor import CandidateExtractor


def test_extractor_keeps_shape_but_never_marks_web_sql_executable() -> None:
    html = b"<pre>WITH c AS (SELECT 1) SELECT * FROM c;</pre>"
    candidates = CandidateExtractor().from_html(html)
    assert candidates[0].text.startswith("WITH c")
    assert candidates[0].executable is False


def test_extractor_rejects_multi_statement_and_side_effect_tokens() -> None:
    html = b"<code>SELECT 1; DROP TABLE users;</code><code>SELECT LOAD_FILE('/tmp/x')</code>"
    assert CandidateExtractor().from_html(html) == ()
```

- [ ] **Step 2: Verify the tests fail before implementation**

Run: `uv run pytest -q tests/unit/research/test_extractor.py`

Expected: import failure for `CandidateExtractor`.

- [ ] **Step 3: Implement HTML code-block extraction, statement counting, and hard token denylist**

```python
_DENIED = {
    "DROP", "ALTER", "INSERT", "UPDATE", "DELETE", "REPLACE", "CALL",
    "LOAD_FILE", "SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK",
    "INTO", "FOR UPDATE", "FOR SHARE", "LOCK IN SHARE MODE",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    text: str
    executable: bool = False


class CandidateExtractor:
    def from_html(self, content: bytes) -> tuple[Candidate, ...]:
        blocks = _extract_pre_and_code(content.decode("utf-8", errors="replace"))
        accepted: list[Candidate] = []
        for block in blocks:
            statements = defensive_split(block)
            upper = block.upper()
            if len(statements) != 1 or any(token in upper for token in _DENIED):
                continue
            if not upper.lstrip().startswith(("SELECT", "WITH", "TABLE", "VALUES")):
                continue
            accepted.append(Candidate(text=statements[0], executable=False))
        return tuple(accepted)
```

- [ ] **Step 4: Add fuzz tests proving random HTML and comments never set `executable=True`**

Run: `uv run pytest -q tests/unit/research/test_extractor.py --hypothesis-show-statistics`

Expected: all examples pass; no source candidate is executable.

- [ ] **Step 5: Commit the isolation layer**

```bash
git add src/select_fuzz/research/extractor.py tests/unit/research/test_extractor.py
git commit -m "feat: isolate discovered SQL as non-executable candidates"
```

### Task 4: Feature-signature extraction

**Files:**
- Create: `src/select_fuzz/research/signature.py`
- Create: `tests/fixtures/research/signature_cases.yaml`
- Test: `tests/unit/research/test_signature.py`

- [ ] **Step 1: Add table-driven failing examples**

```yaml
- sql: "WITH c AS (SELECT a, ROW_NUMBER() OVER (PARTITION BY b ORDER BY id) rn FROM t) SELECT * FROM c"
  nodes: [cte, select, window, window_partition, window_order]
  requirements: [table, integer, unique_tiebreaker]
- sql: "SELECT a FROM t1 INTERSECT ALL SELECT a FROM t2"
  nodes: [select, set_intersect_all]
  requirements: [two_compatible_relations]
```

```python
def test_signature_cases() -> None:
    for case in load_yaml("tests/fixtures/research/signature_cases.yaml"):
        signature = SignatureExtractor(version="8.0.41").extract(case["sql"])
        assert set(case["nodes"]) <= set(signature.nodes)
        assert set(case["requirements"]) <= set(signature.requirements)
```

- [ ] **Step 2: Run and confirm `SignatureExtractor` is missing**

Run: `uv run pytest -q tests/unit/research/test_signature.py`

Expected: FAIL before implementation.

- [ ] **Step 3: Implement an explicit visitor that emits stable nodes for query blocks, CTE, set operators, joins, subqueries, grouping, windows, JSON, order/limit, functions, and type requirements**

```python
class SignatureExtractor:
    def __init__(self, version: str):
        self.version = version

    def extract(self, sql: str) -> FeatureSignature:
        tree = defensive_parse_one(sql, dialect="mysql")
        nodes: set[str] = {"select"}
        requirements: set[str] = set()
        visit_query_shape(tree, nodes, requirements)
        return FeatureSignature(
            version=self.version,
            nodes=tuple(sorted(nodes)),
            requirements=tuple(sorted(requirements)),
        )
```

- [ ] **Step 4: Add every official feature family from the design catalog to the YAML corpus and run tests**

Run: `uv run pytest -q tests/unit/research/test_signature.py`

Expected: every fixture has a stable key and no unclassified AST node.

- [ ] **Step 5: Commit signature extraction**

```bash
git add src/select_fuzz/research/signature.py tests/unit/research/test_signature.py tests/fixtures/research/signature_cases.yaml
git commit -m "feat: extract versioned SQL feature signatures"
```

### Task 5: Generator capability and directed reachability audit

**Files:**
- Create: `src/select_fuzz/research/capability.py`
- Test: `tests/unit/research/test_capability.py`
- Test: `tests/integration/test_signature_reachability.py`

- [ ] **Step 1: Write failing static and dynamic reachability tests**

```python
def test_missing_rule_is_classified_missing() -> None:
    catalog = FakeCatalog(supported_nodes={"select", "cte"})
    target = FeatureSignature("8.0.41", ("select", "window"), ())
    result = CapabilityAuditor(catalog).audit_static(target)
    assert result.status == "missing"
    assert result.missing_nodes == ("window",)


def test_directed_generation_must_hit_target_signature() -> None:
    generator = FakeGenerator(outputs=["SELECT 1", "SELECT SUM(a) OVER () FROM t"])
    target = FeatureSignature("8.0.41", ("select", "window"), ())
    result = CapabilityAuditor(FakeCatalog.all()).audit_dynamic(target, generator, budget=2)
    assert result.status == "covered"
    assert result.seed is not None
```

- [ ] **Step 2: Run tests and observe missing auditor failure**

Run: `uv run pytest -q tests/unit/research/test_capability.py`

Expected: FAIL before implementation.

- [ ] **Step 3: Implement the six-state decision result and directed generation budget**

```python
CapabilityStatus = Literal[
    "covered", "latent/unreachable", "missing", "unsupported-8.0.41",
    "unsafe/out-of-scope", "indeterminate",
]


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    signature_key: str
    status: CapabilityStatus
    missing_nodes: tuple[str, ...] = ()
    seed: int | None = None
    evidence: tuple[str, ...] = ()
```

- [ ] **Step 4: Add an opt-in integration test that directs the real generator and validates only the re-synthesized SQL with the project read-only validator**

Run: `uv run pytest -q tests/integration/test_signature_reachability.py -m mysql`

Expected: known catalog signatures are `covered`; unsupported MySQL 8.0.41 syntax is classified and never executed.

- [ ] **Step 5: Commit capability auditing**

```bash
git add src/select_fuzz/research/capability.py tests/unit/research/test_capability.py tests/integration/test_signature_reachability.py
git commit -m "feat: audit generator feature reachability"
```

### Task 6: Transactional checkpoint and append-only gap ledger

**Files:**
- Create: `src/select_fuzz/research/ledger.py`
- Test: `tests/unit/research/test_ledger.py`

- [ ] **Step 1: Write crash-recovery and idempotency tests**

```python
def test_record_gap_is_idempotent(tmp_path: Path) -> None:
    ledger = ResearchLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    gap = GapRecord(signature_key="abc", priority="P1", status="missing")
    ledger.record_gap(gap)
    ledger.record_gap(gap)
    assert ledger.list_gaps() == [gap]
    assert count_jsonl_events(tmp_path / "events.jsonl", "gap_recorded") == 1


def test_corrupt_jsonl_tail_does_not_corrupt_sqlite_state(tmp_path: Path) -> None:
    ledger = ResearchLedger(tmp_path / "state.db", tmp_path / "events.jsonl")
    ledger.checkpoint(EpochCheckpoint(run_id="r1", epoch=2, source_cursor="u3"))
    with (tmp_path / "events.jsonl").open("ab") as stream:
        stream.write(b'{"partial"')
    assert ResearchLedger(tmp_path / "state.db", tmp_path / "events.jsonl").latest_epoch() == 2
```

- [ ] **Step 2: Run the ledger tests and verify red**

Run: `uv run pytest -q tests/unit/research/test_ledger.py`

Expected: FAIL before `ResearchLedger` exists.

- [ ] **Step 3: Implement SQLite WAL transactions, unique gap keys, fsynced JSONL events, and schema migrations**

```python
class ResearchLedger:
    def record_gap(self, gap: GapRecord) -> None:
        with self._connect() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO gaps(signature_key, priority, status) VALUES (?, ?, ?)",
                (gap.signature_key, gap.priority, gap.status),
            ).rowcount
        if inserted:
            append_jsonl_fsync(self.events_path, {"type": "gap_recorded", **asdict(gap)})
```

- [ ] **Step 4: Run unit tests plus repeated kill-point simulation**

Run: `uv run pytest -q tests/unit/research/test_ledger.py -k 'ledger or corrupt or idempotent' --count=20`

Expected: state is identical across repetitions; only the incomplete JSONL tail is ignored.

- [ ] **Step 5: Commit the research ledger**

```bash
git add src/select_fuzz/research/ledger.py tests/unit/research/test_ledger.py
git commit -m "feat: add durable research gap ledger"
```

### Task 7: Continuous 12-hour epoch coordinator

**Files:**
- Create: `src/select_fuzz/research/loop.py`
- Create: `scripts/run_12h_coverage_loop.py`
- Test: `tests/unit/research/test_loop.py`

- [ ] **Step 1: Write failing clock-driven duration, checkpoint, and freeze tests**

```python
def test_loop_freezes_code_changes_for_final_thirty_minutes() -> None:
    clock = FakeClock(start=0)
    handler = RecordingGapHandler()
    loop = CoverageLoop(duration_s=12 * 3600, checkpoint_s=1800, freeze_s=1800, clock=clock)
    loop.run(source_queue=RepeatingQueue(), gap_handler=handler, max_iterations=24)
    assert handler.mutable_calls_before(11.5 * 3600) > 0
    assert handler.mutable_calls_after(11.5 * 3600) == 0
    assert handler.read_only_calls_after(11.5 * 3600) > 0


def test_downtime_does_not_count_toward_contiguous_duration() -> None:
    clock = FakeClock(start=0)
    loop = CoverageLoop(duration_s=100, clock=clock)
    loop.note_interruption(start=20, end=50)
    assert loop.deadline == 130
```

- [ ] **Step 2: Run the focused tests and verify red**

Run: `uv run pytest -q tests/unit/research/test_loop.py`

Expected: FAIL before `CoverageLoop` exists.

- [ ] **Step 3: Implement an injectable monotonic coordinator**

```python
class CoverageLoop:
    def run(self, source_queue: SourceQueue, gap_handler: GapHandler, max_iterations: int | None = None) -> RunSummary:
        while self.clock.monotonic() < self.deadline:
            if max_iterations is not None and self.iterations >= max_iterations:
                break
            candidate = source_queue.next()
            decision = self.audit(candidate)
            mutable = self.clock.monotonic() < self.deadline - self.freeze_s
            gap_handler.handle(decision, allow_code_change=mutable)
            self.maybe_checkpoint()
            self.iterations += 1
        return self.finish()
```

- [ ] **Step 4: Add CLI tests for `--duration 12h`, `--checkpoint 30m`, `--freeze 30m`, resume, and a one-minute dry-run**

Run: `uv run pytest -q tests/unit/research/test_loop.py && python scripts/run_12h_coverage_loop.py --duration 1m --dry-run`

Expected: tests pass; dry-run prints at least one checkpoint and exits 0 without network or code changes.

- [ ] **Step 5: Commit the loop coordinator**

```bash
git add src/select_fuzz/research/loop.py scripts/run_12h_coverage_loop.py tests/unit/research/test_loop.py
git commit -m "feat: coordinate continuous coverage discovery epochs"
```

### Task 8: Local three-instance MySQL manager

**Files:**
- Create: `src/select_fuzz/local_cluster.py`
- Create: `scripts/local_mysql_cluster.py`
- Test: `tests/unit/test_local_cluster.py`
- Test: `tests/integration/test_local_cluster.py`

- [ ] **Step 1: Write failing command-construction tests without launching MySQL**

```python
def test_cluster_uses_isolated_ports_sockets_and_datadirs(tmp_path: Path) -> None:
    cluster = LocalMySQLCluster(root=tmp_path, mysqld=Path("/opt/mysql/bin/mysqld"), base_port=33361)
    specs = cluster.specs()
    assert [spec.port for spec in specs] == [33361, 33362, 33363]
    assert len({spec.socket for spec in specs}) == 3
    assert len({spec.datadir for spec in specs}) == 3
    assert all("canary-secret" not in " ".join(spec.argv) for spec in specs)
```

- [ ] **Step 2: Run the unit test and verify red**

Run: `uv run pytest -q tests/unit/test_local_cluster.py`

Expected: FAIL before cluster manager implementation.

- [ ] **Step 3: Implement specs, initialize/start/stop/status, version gate, and environment-only credential lookup**

```python
@dataclass(frozen=True, slots=True)
class MySQLInstanceSpec:
    role: str
    port: int
    socket: Path
    datadir: Path
    pid_file: Path
    log_file: Path


class LocalMySQLCluster:
    def require_version(self, expected: str) -> None:
        actual = subprocess.run([str(self.mysqld), "--version"], check=True, text=True, capture_output=True).stdout
        if expected not in actual:
            raise VersionMismatch(f"expected {expected}, got {actual.strip()}")
```

- [ ] **Step 4: Run opt-in integration tests against `MYSQLD_BIN`**

Run: `MYSQLD_BIN=/opt/homebrew/bin/mysqld uv run pytest -q tests/integration/test_local_cluster.py -m mysql`

Expected on the discovered machine: three MySQL 8.0.45 instances start, answer `SELECT VERSION()`, use distinct runtime paths, and stop cleanly. Run the same test with an exact 8.0.41 binary before release; a mismatched binary must fail the `expected_version=8.0.41` gate.

- [ ] **Step 5: Commit local cluster support**

```bash
git add src/select_fuzz/local_cluster.py scripts/local_mysql_cluster.py tests/unit/test_local_cluster.py tests/integration/test_local_cluster.py
git commit -m "feat: manage isolated local MySQL test instances"
```

### Task 9: Soak telemetry and deterministic fault schedule

**Files:**
- Create: `src/select_fuzz/soak.py`
- Test: `tests/unit/test_soak.py`
- Test: `tests/soak/test_soak_smoke.py`

- [ ] **Step 1: Write failing resource-slope and fault-schedule tests**

```python
def test_fault_schedule_is_seed_reproducible() -> None:
    assert build_fault_schedule(seed=7, duration_s=3600) == build_fault_schedule(seed=7, duration_s=3600)


def test_linear_rss_growth_is_rejected() -> None:
    samples = [ResourceSample(t=i * 60, rss=100_000_000 + i * 10_000_000) for i in range(20)]
    verdict = ResourceTrendPolicy(max_growth_ratio=0.20).evaluate(samples)
    assert verdict.status == "failed"
    assert "rss" in verdict.reasons
```

- [ ] **Step 2: Run tests and verify red**

Run: `uv run pytest -q tests/unit/test_soak.py`

Expected: FAIL before soak models exist.

- [ ] **Step 3: Implement psutil sampling, JSONL metrics, seeded fault schedule, recovery timers, and acceptance policy**

```python
@dataclass(frozen=True, slots=True)
class ResourceSample:
    t: float
    rss: int
    threads: int
    fds: int
    mysql_connections: int


class SoakMonitor:
    def sample(self) -> ResourceSample:
        process = psutil.Process(self.pid)
        return ResourceSample(
            t=time.monotonic(),
            rss=process.memory_info().rss,
            threads=process.num_threads(),
            fds=process.num_fds(),
            mysql_connections=self.connection_probe(),
        )
```

- [ ] **Step 4: Run a ten-minute accelerated smoke with connection reset, worker termination, report write failure, and recovery assertions**

Run: `uv run pytest -q tests/soak/test_soak_smoke.py --soak-duration=600 -m soak`

Expected: injected events are classified, the coordinator resumes, and no active query remains after cancellation grace.

- [ ] **Step 5: Commit soak telemetry**

```bash
git add src/select_fuzz/soak.py tests/unit/test_soak.py tests/soak/test_soak_smoke.py
git commit -m "test: add soak telemetry and deterministic faults"
```

### Task 10: Coverage reports and operator runbook

**Files:**
- Create: `src/select_fuzz/research/report.py`
- Create: `docs/testing/12h-sql-coverage-runbook.md`
- Test: `tests/unit/research/test_report.py`

- [ ] **Step 1: Write failing saturation and manifest-report tests**

```python
def test_report_exposes_saturation_and_unresolved_gaps(tmp_path: Path) -> None:
    report = build_research_report(
        signatures_per_checkpoint=[10, 5, 2, 1, 0],
        gaps=[GapRecord("a", "P1", "missing")],
        sources=[fixture_source("https://dev.mysql.com/doc/refman/8.0/en/select.html")],
    )
    assert report.saturation.new_signatures_last_checkpoint == 0
    assert report.unresolved_by_priority == {"P1": 1}
    assert report.source_manifest[0].url.startswith("https://dev.mysql.com/")
```

- [ ] **Step 2: Run tests and verify red**

Run: `uv run pytest -q tests/unit/research/test_report.py`

Expected: FAIL before report builder exists.

- [ ] **Step 3: Implement JSON, Markdown, and HTML report models from the ledger**

```python
def build_research_report(...: object) -> ResearchReport:
    return ResearchReport(
        generated_at=utc_now_iso(),
        source_manifest=tuple(sorted(sources, key=lambda item: item.url)),
        status_counts=Counter(result.status for result in capability_results),
        unresolved_by_priority=Counter(gap.priority for gap in gaps if gap.status != "covered"),
        saturation=calculate_saturation(signatures_per_checkpoint),
    )
```

- [ ] **Step 4: Write the exact 12-hour runbook**

The runbook must contain these non-optional commands and gates:

```bash
git status --short
uv run python -m select_fuzz doctor --mode correctness --config config/local-8041.yaml
uv run python scripts/run_12h_coverage_loop.py --duration 12h --checkpoint 30m --freeze 30m
uv run pytest -q
git diff --check
```

For every `missing` signature it must require: create isolated `codex/gap-<id>` worktree, add a failing reachability test, prove red, implement, prove focused green, run full gates, commit only related files, push when a remote exists, drain/restart soak at the new commit, and append the commit/checkpoint mapping. Raw web SQL is never executed.

- [ ] **Step 5: Run report tests and a dry-run report build**

Run: `uv run pytest -q tests/unit/research/test_report.py && python scripts/run_12h_coverage_loop.py --duration 1m --dry-run --report-dir /tmp/select-fuzz-research-dry-run`

Expected: tests pass and the directory contains `source-manifest.json`, `coverage-matrix.json`, `gaps.json`, `summary.md`, and `index.html`.

- [ ] **Step 6: Commit reports and runbook**

```bash
git add src/select_fuzz/research/report.py tests/unit/research/test_report.py docs/testing/12h-sql-coverage-runbook.md
git commit -m "docs: add active SQL coverage completion runbook"
```

### Task 11: Validation plan release gate

**Files:**
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Create: `tests/regression/research_signatures.json`

- [ ] **Step 1: Add marker and command tests for research, MySQL integration, and soak suites**

Run: `uv run pytest --collect-only -q`

Expected: unit, `mysql`, `soak`, and regression suites are all collected without unknown-marker warnings.

- [ ] **Step 2: Run the complete non-network gate**

Run: `ruff check . && mypy src && pytest -q --cov=select_fuzz --cov-report=term-missing --cov-fail-under=90 && npm --prefix frontend test -- --run && npm --prefix frontend run build`

Expected: every command exits 0; Python coverage is at least 90%.

- [ ] **Step 3: Run the local MySQL and accelerated soak gates**

Run: `uv run pytest -q -m mysql tests/integration && pytest -q -m soak tests/soak --soak-duration=600`

Expected: three local instances are isolated and cleaned up; the accelerated soak recovers from every injected fault.

- [ ] **Step 4: Freeze the initial signature regression corpus**

Run: `uv run python scripts/run_12h_coverage_loop.py --duration 5m --dry-run --write-regression tests/regression/research_signatures.json`

Expected: the corpus contains stable signature keys, source evidence, and capability classifications, but no copied executable web SQL.

- [ ] **Step 5: Commit the validation gate**

```bash
git add pyproject.toml .github/workflows/ci.yml tests/regression/research_signatures.json
git commit -m "ci: enforce SQL coverage validation gates"
```
