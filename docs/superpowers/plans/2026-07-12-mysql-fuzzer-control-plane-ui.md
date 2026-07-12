# MySQL Fuzzer Control Plane and UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a loopback-only FastAPI control plane and resilient React/TypeScript console that supervise runs, stream sequenced state, search durable findings/reports, and launch replay without implementing generator or performance internals.

**Architecture:** FastAPI is an adapter around injected core services from `select_fuzz.service`, `select_fuzz.artifacts`, and `select_fuzz.replay`; a child-process supervisor owns lifecycle transitions while an atomically rebuildable SQLite read index serves searches over JSONL facts. The SPA uses TanStack Router/Query, a sequence-aware SSE cache reconciler, local error boundaries, Recharts, and TanStack Virtual; FastAPI serves the production Vite bundle on `127.0.0.1` only.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, Uvicorn, SQLite, pytest/httpx/anyio; React 19, TypeScript, Vite, TanStack Query/Router/Virtual, Recharts, Vitest, React Testing Library, MSW, Playwright, axe-core.

---

## Boundaries and file map

The core already supplies `CorrectnessRunService.run(RunRequest, stop_event)`, `EventSink.publish(RunEvent)`, `FindingReader.iter_findings()/get()`, and `ReplayService.replay(case_id)`. `control_plane/ports.py` adapts those services and adds only process/read-side contracts; it must not import generator, oracle, or performance implementation modules.

Backend responsibilities:

- `src/select_fuzz/control_plane/contracts.py`: HTTP DTOs and stable enum values.
- `src/select_fuzz/control_plane/ports.py`: injected core/process/artifact protocols.
- `src/select_fuzz/control_plane/problems.py`: RFC 9457 responses and request IDs.
- `src/select_fuzz/control_plane/run_state.py`: legal lifecycle transitions and durable coordination.
- `src/select_fuzz/control_plane/supervisor.py`: child spawn, graceful stop, exit watch, restart recovery.
- `src/select_fuzz/control_plane/events.py`: monotonic event history, SSE resume, snapshot watermark.
- `src/select_fuzz/control_plane/read_index.py`: disposable SQLite projection rebuilt from facts.
- `src/select_fuzz/control_plane/routes/`: health, run, event, finding, report, artifact, replay routes.
- `src/select_fuzz/control_plane/security.py`, `static.py`, `app.py`, `server.py`: loopback policy, SPA fallback, composition, serving.

Frontend responsibilities:

- `frontend/src/api/`: generated OpenAPI types, problem-aware fetch client, SSE sequence recovery.
- `frontend/src/app/`: Query client, router, shell, route error boundaries.
- `frontend/src/components/`: four-state panels, metrics, charts, virtual lists, node/result views.
- `frontend/src/pages/`: overview, new run, run detail/history, findings/detail, replay, reports.
- `frontend/src/test/` and `frontend/e2e/`: MSW fixtures, Vitest/RTL, Playwright/axe failure matrix.

### Task 1: Bootstrap the API contract and app factory

**Files:**
- Modify: `pyproject.toml`
- Create: `src/select_fuzz/control_plane/__init__.py`
- Create: `src/select_fuzz/control_plane/contracts.py`
- Create: `src/select_fuzz/control_plane/ports.py`
- Create: `src/select_fuzz/control_plane/app.py`
- Test: `tests/control_plane/test_app.py`

- [ ] **Step 1: Write the failing factory and validation tests**

```python
def test_health_and_performance_worker_contract(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    accepted = client.post("/api/v1/runs", json={"mode": "performance"})
    assert accepted.status_code == 202
    assert accepted.json()["request"]["workers"] == 1
    response = client.post("/api/v1/runs", json={"mode": "performance", "workers": 2})
    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/workers"
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run pytest tests/control_plane/test_app.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'select_fuzz.control_plane'`.

- [ ] **Step 3: Add dependencies, DTOs, ports, and a dependency-injected factory**

```python
class RunCreate(BaseModel):
    mode: Literal["correctness", "performance"]
    seed: int | None = None
    workers: int | None = Field(default=None, ge=1, le=64)
    rounds: int | None = Field(default=None, ge=1)
    queries_per_round: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    coverage_strategy: Literal["debt", "random"] = "debt"

    @model_validator(mode="after")
    def apply_mode_defaults(self) -> "RunCreate":
        if self.workers is None:
            self.workers = 1 if self.mode == "performance" else 10
        if self.queries_per_round is None:
            self.queries_per_round = 100 if self.mode == "performance" else 1000
        if self.timeout_seconds is None:
            self.timeout_seconds = 60.0 if self.mode == "performance" else 15.0
        if self.mode == "performance" and self.workers != 1:
            raise PydanticCustomError("performance_workers", "performance mode requires workers=1")
        return self

@dataclass(frozen=True)
class ControlPlanePorts:
    runs: RunRepository
    launcher: WorkerLauncher
    facts: FactSource
    artifacts: ArtifactStore
    replay: ReplayServicePort
```

Add FastAPI/Uvicorn/Pydantic settings plus pytest/httpx dependencies to `pyproject.toml`; create `/api/v1/health` and a temporary validated `/api/v1/runs` route in `create_app(ports)`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `uv run pytest tests/control_plane/test_app.py -q`
Expected: `2 passed`; the performance request is `422` and no launcher call occurs.

- [ ] **Step 5: Commit the contract slice**

```bash
git add pyproject.toml src/select_fuzz/control_plane tests/control_plane/test_app.py
git commit -m "feat: bootstrap control plane contract"
```

### Task 2: Return RFC 9457 problems and freeze OpenAPI

**Files:**
- Create: `src/select_fuzz/control_plane/problems.py`
- Modify: `src/select_fuzz/control_plane/app.py`
- Test: `tests/control_plane/test_problems.py`
- Test: `tests/control_plane/test_openapi.py`
- Create: `tests/control_plane/snapshots/openapi.json`

- [ ] **Step 1: Write failing tests for validation, missing resources, and request IDs**

```python
def test_validation_is_problem_json(client):
    response = client.post("/api/v1/runs", json={"mode": "performance", "workers": 3})
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() | {"status": 422, "type": "urn:mysql-fuzzer:problem:validation"} == response.json()
    assert response.headers["x-request-id"] == response.json()["request_id"]
```

- [ ] **Step 2: Verify the error envelope test fails**

Run: `uv run pytest tests/control_plane/test_problems.py -q`
Expected: FAIL because FastAPI emits `application/json` and `detail`.

- [ ] **Step 3: Add problem handlers and stable OpenAPI metadata**

```python
class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str
    errors: list[ProblemField] = []

def problem_response(problem: Problem) -> JSONResponse:
    return JSONResponse(problem.model_dump(), status_code=problem.status,
                        media_type="application/problem+json")
```

Map `RequestValidationError`, typed `ApiProblem`, and unexpected exceptions; expose schemas/examples under OpenAPI tag groups `system`, `runs`, `events`, `findings`, `reports`, and `replays`.

- [ ] **Step 4: Generate and compare the deterministic OpenAPI snapshot**

Run: `uv run pytest tests/control_plane/test_problems.py tests/control_plane/test_openapi.py -q`
Expected: `6 passed`; `openapi.json` is sorted and contains no memory addresses or absolute paths.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/control_plane tests/control_plane
git commit -m "feat: standardize API problems and OpenAPI"
```

### Task 3: Enforce durable run-state transitions and idempotency

**Files:**
- Create: `src/select_fuzz/control_plane/run_state.py`
- Modify: `src/select_fuzz/control_plane/contracts.py`
- Test: `tests/control_plane/test_run_state.py`

- [ ] **Step 1: Write table tests for every accepted and rejected transition**

```python
@pytest.mark.parametrize("source,target", [("queued", "starting"), ("running", "stopping"),
    ("stopping", "stopped"), ("recovering", "orphaned"), ("running", "completed")])
def test_legal_transitions(source, target):
    assert transition(source, target) == target

def test_terminal_state_cannot_restart():
    with pytest.raises(IllegalTransition):
        transition("failed", "running")
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/control_plane/test_run_state.py -q`
Expected: FAIL because `transition` does not exist.

- [ ] **Step 3: Implement the state graph and compare-and-set coordinator**

```python
ALLOWED = {
    "queued": {"starting", "stopping", "failed"},
    "starting": {"running", "stopping", "failed"},
    "running": {"stopping", "completed", "failed", "recovering"},
    "stopping": {"stopped", "failed"},
    "recovering": {"running", "stopping", "orphaned", "failed"},
    "stopped": set(), "completed": set(), "failed": set(), "orphaned": set(),
}

async def create_once(repo, request, key):
    existing = await repo.by_idempotency_key(key)
    if existing:
        if existing.request_fingerprint != request.fingerprint():
            raise IdempotencyConflict(key)
        return existing
    return await repo.insert(RunRecord.queued(request, key))
```

Use repository compare-and-set for state/version updates so two stop calls return the same `stopping` or terminal record.

- [ ] **Step 4: Verify transition, conflict, and concurrent-stop tests pass**

Run: `uv run pytest tests/control_plane/test_run_state.py -q`
Expected: `14 passed` including one `IdempotencyConflict` and one persisted transition event per state change.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/control_plane/run_state.py src/select_fuzz/control_plane/contracts.py tests/control_plane/test_run_state.py
git commit -m "feat: persist safe run lifecycle transitions"
```

### Task 4: Supervise child processes, stops, exits, and service recovery

**Files:**
- Create: `src/select_fuzz/control_plane/supervisor.py`
- Modify: `src/select_fuzz/control_plane/ports.py`
- Test: `tests/control_plane/test_supervisor.py`

- [ ] **Step 1: Write fake-process tests for start, TERM-to-KILL, clean exit, crash, and restart**

```python
async def test_unknown_live_run_becomes_recovering_then_orphaned(supervisor, repo):
    await repo.insert(running_record(pid=4242))
    await supervisor.recover()
    assert (await repo.get("run-1")).state == "orphaned"

async def test_stop_is_idempotent(supervisor, handle):
    first, second = await asyncio.gather(supervisor.stop("run-1"), supervisor.stop("run-1"))
    assert first.state == second.state == "stopped"
    assert handle.terminate_calls == 1
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/control_plane/test_supervisor.py -q`
Expected: FAIL with missing `RunSupervisor`.

- [ ] **Step 3: Implement a lock-per-run supervisor over `WorkerLauncher`**

```python
async def stop(self, run_id: str) -> RunRecord:
    async with self._locks[run_id]:
        record = await self._states.request_stop(run_id)
        handle = self._children.get(run_id)
        if handle is None:
            return record
        await handle.terminate()
        try:
            await asyncio.wait_for(handle.wait(), self._grace_seconds)
        except TimeoutError:
            await handle.kill()
            await handle.wait()
        return await self._states.mark_stopped(run_id)
```

Pass a serialized `RunCreate` to the launcher unchanged; never place database passwords in argv, run records, or emitted events. On API startup mark formerly `starting/running/stopping` records `recovering`; only an attached, identity-verified child may return to `running`, otherwise mark `orphaned`.

- [ ] **Step 4: Run supervisor tests**

Run: `uv run pytest tests/control_plane/test_supervisor.py -q`
Expected: `10 passed`; fake argv contains run ID/config reference but no environment secret values.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/control_plane/supervisor.py src/select_fuzz/control_plane/ports.py tests/control_plane/test_supervisor.py
git commit -m "feat: supervise and recover run workers"
```

### Task 5: Expose idempotent run and supervisor APIs

**Files:**
- Create: `src/select_fuzz/control_plane/routes/__init__.py`
- Create: `src/select_fuzz/control_plane/routes/runs.py`
- Modify: `src/select_fuzz/control_plane/app.py`
- Test: `tests/control_plane/test_run_routes.py`

- [ ] **Step 1: Write failing HTTP tests for create/list/get/stop and key reuse**

```python
def test_create_and_stop_are_idempotent(client):
    headers = {"Idempotency-Key": "create-42"}
    one = client.post("/api/v1/runs", headers=headers,
                      json={"mode": "correctness", "workers": 10}).json()
    two = client.post("/api/v1/runs", headers=headers,
                      json={"mode": "correctness", "workers": 10}).json()
    assert one["id"] == two["id"]
    assert client.post(f"/api/v1/runs/{one['id']}/stop").status_code in {200, 202}
    assert client.post(f"/api/v1/runs/{one['id']}/stop").json()["state"] in {"stopping", "stopped"}
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/control_plane/test_run_routes.py -q`
Expected: FAIL with `404 Not Found`.

- [ ] **Step 3: Add thin routes with response models and pagination**

```python
@router.post("", response_model=RunView, status_code=202)
async def create_run(body: RunCreate, supervisor=Depends(get_supervisor),
                     idempotency_key: Annotated[str, Header(min_length=8, max_length=128)] = ""):
    if not idempotency_key:
        raise ApiProblem.validation("Idempotency-Key is required", "/headers/idempotency-key")
    return await supervisor.start(body, idempotency_key)
```

Implement `GET /runs`, `GET /runs/{run_id}`, and `POST /runs/{run_id}/stop`; map unknown IDs to `404`, key/body mismatch to `409`, and capacity/preflight rejection to `409` without starting a child.

- [ ] **Step 4: Verify the route matrix**

Run: `uv run pytest tests/control_plane/test_run_routes.py -q`
Expected: `12 passed`; OpenAPI shows `Idempotency-Key` and all typed responses.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/control_plane/routes src/select_fuzz/control_plane/app.py tests/control_plane/test_run_routes.py
git commit -m "feat: expose idempotent run controls"
```

### Task 6: Stream sequenced SSE and recover gaps from snapshots

**Files:**
- Create: `src/select_fuzz/control_plane/events.py`
- Create: `src/select_fuzz/control_plane/routes/events.py`
- Modify: `src/select_fuzz/control_plane/app.py`
- Test: `tests/control_plane/test_events.py`

- [ ] **Step 1: Write tests for IDs, resume precedence, heartbeat, and expired history**

```python
async def test_resume_after_sequence(client, event_log):
    await event_log.append("run.state", {"run_id": "r1", "state": "running"})
    await event_log.append("finding.created", {"finding_id": "f1"})
    async with client.stream("GET", "/api/v1/events?after=1") as response:
        chunk = await first_sse_event(response)
    assert chunk["id"] == "2"
    assert chunk["event"] == "finding.created"
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/control_plane/test_events.py -q`
Expected: FAIL because `/api/v1/events` is absent.

- [ ] **Step 3: Implement a persisted-sequence adapter and SSE framing**

```python
def encode_sse(event: EventEnvelope) -> bytes:
    payload = json.dumps(event.payload, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.sequence}\nevent: {event.kind}\ndata: {payload}\n\n".encode()

async def stream(after: int, source: EventSource):
    async for event in source.subscribe(after=after):
        yield encode_sse(event)
```

Use `Last-Event-ID` when present, otherwise `after`; emit `: heartbeat` every 15 seconds. Add `GET /api/v1/snapshot` returning `{sequence, service, nodes, runs, recent_findings}` from one read transaction. If history is pruned, return `409 urn:mysql-fuzzer:problem:event-history-expired` with `snapshot_url`.

- [ ] **Step 4: Verify event semantics**

Run: `uv run pytest tests/control_plane/test_events.py -q`
Expected: `9 passed`; resumed streams neither repeat sequence 1 nor skip sequence 2.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/control_plane/events.py src/select_fuzz/control_plane/routes/events.py src/select_fuzz/control_plane/app.py tests/control_plane/test_events.py
git commit -m "feat: add resumable sequenced event stream"
```

### Task 7: Build and query the disposable SQLite read index

**Files:**
- Create: `src/select_fuzz/control_plane/read_index.py`
- Test: `tests/control_plane/test_read_index.py`

- [ ] **Step 1: Write failing rebuild, incremental, filtering, and torn-source tests**

```python
def test_deleted_index_rebuilds_from_committed_facts(tmp_path, fact_source):
    index = ReadIndex(tmp_path / "read.sqlite3")
    index.rebuild(fact_source)
    assert index.get_finding("case-7").severity == "high"
    (tmp_path / "read.sqlite3").unlink()
    ReadIndex(tmp_path / "read.sqlite3").rebuild(fact_source)
    assert [row.id for row in index.list_findings(mode="correctness").items] == ["case-7"]
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/control_plane/test_read_index.py -q`
Expected: FAIL with missing `ReadIndex`.

- [ ] **Step 3: Add schema, idempotent projection, and atomic rebuild**

```sql
CREATE TABLE finding (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, mode TEXT NOT NULL,
  severity TEXT NOT NULL, node TEXT, feature TEXT, errno INTEGER,
  occurred_at TEXT NOT NULL, summary_json TEXT NOT NULL, sequence INTEGER NOT NULL
);
CREATE INDEX finding_filters ON finding(mode, severity, node, feature, errno, occurred_at DESC);
CREATE TABLE projection_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
```

Project only committed `run.*`, `finding.*`, `report.*`, and `replay.*` envelopes, record the sequence watermark, rebuild into `read.sqlite3.new`, `fsync`, then `os.replace`. Treat malformed/torn JSONL tails as uncommitted input through `FactSource`, not as an API error.

- [ ] **Step 4: Verify rebuild and query tests**

Run: `uv run pytest tests/control_plane/test_read_index.py -q`
Expected: `11 passed`; replaying the same facts leaves row counts unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/control_plane/read_index.py tests/control_plane/test_read_index.py
git commit -m "feat: add rebuildable control plane read index"
```

### Task 8: Serve finding search/detail and opaque artifacts

**Files:**
- Create: `src/select_fuzz/control_plane/routes/findings.py`
- Create: `src/select_fuzz/control_plane/routes/artifacts.py`
- Modify: `src/select_fuzz/control_plane/app.py`
- Test: `tests/control_plane/test_finding_routes.py`

- [ ] **Step 1: Write API tests for all filters, cursor order, full detail, and traversal rejection**

```python
def test_finding_filters_and_opaque_artifact(client):
    page = client.get("/api/v1/findings?mode=correctness&severity=high&node=custom_on&errno=1064").json()
    assert [item["id"] for item in page["items"]] == ["case-7"]
    detail = client.get("/api/v1/findings/case-7").json()
    assert set(detail["nodes"]) == {"baseline", "custom_off", "custom_on"}
    assert client.get("/api/v1/artifacts/../../etc/passwd").status_code == 404
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/control_plane/test_finding_routes.py -q`
Expected: FAIL with route `404`.

- [ ] **Step 3: Implement typed filters, detail DTO mapping, and artifact streaming**

```python
@router.get("/{finding_id}", response_model=FindingDetail)
async def finding(finding_id: str, index=Depends(get_index)):
    row = await index.get_finding(finding_id)
    if row is None:
        raise ApiProblem.not_found("finding", finding_id)
    return FindingDetail.from_projection(row)

@artifact_router.get("/{artifact_id}")
async def artifact(artifact_id: str, store=Depends(get_artifacts)):
    ref = await store.resolve_id(artifact_id)
    return StreamingResponse(ref.open_binary(), media_type=ref.media_type,
                             headers={"Content-Disposition": ref.content_disposition()})
```

Accept mode, severity, node, feature, errno, start/end ISO timestamps, run ID, query text, `limit<=200`, and opaque cursor. Never accept a filesystem path and never expose an absolute path in JSON.

- [ ] **Step 4: Verify routes and response redaction**

Run: `uv run pytest tests/control_plane/test_finding_routes.py -q`
Expected: `13 passed`; secret sentinel and artifact root are absent from response bytes.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/control_plane/routes src/select_fuzz/control_plane/app.py tests/control_plane/test_finding_routes.py
git commit -m "feat: expose finding and artifact read APIs"
```

### Task 9: Expose reports and idempotent replay jobs

**Files:**
- Create: `src/select_fuzz/control_plane/routes/reports.py`
- Create: `src/select_fuzz/control_plane/routes/replays.py`
- Modify: `src/select_fuzz/control_plane/contracts.py`
- Modify: `src/select_fuzz/control_plane/app.py`
- Test: `tests/control_plane/test_report_replay_routes.py`

- [ ] **Step 1: Write failing list/detail and replay lifecycle tests**

```python
def test_replay_key_reuses_job(client):
    body = {"case_id": "case-7"}
    headers = {"Idempotency-Key": "replay-case-7"}
    first = client.post("/api/v1/replays", json=body, headers=headers)
    second = client.post("/api/v1/replays", json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/control_plane/test_report_replay_routes.py -q`
Expected: FAIL because report and replay routers are missing.

- [ ] **Step 3: Add read-only report endpoints and async replay dispatch**

```python
class ReplayView(BaseModel):
    id: str
    case_id: str
    state: Literal["queued", "running", "reproduced", "not_reproduced", "failed"]
    nodes: dict[Literal["baseline", "custom_off", "custom_on"], ReplayNodeView]
    sequence: int

@router.post("", response_model=ReplayView, status_code=202)
async def replay(body: ReplayCreate, key: Annotated[str, Header(alias="Idempotency-Key")], service=Depends(get_replay)):
    return await service.start_once(body.case_id, key)
```

Implement `GET /reports`, `GET /reports/{report_id}`, `POST /replays`, and `GET /replays/{replay_id}`. Adapt `select_fuzz.replay.ReplayService.replay(case_id)` behind the port, persist each state, publish sequences, and keep original node errors/results when replay fails.

- [ ] **Step 4: Verify all report/replay outcomes**

Run: `uv run pytest tests/control_plane/test_report_replay_routes.py -q`
Expected: `12 passed` across reproduced, not reproduced, failed, unknown case, and key/body conflict.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/control_plane tests/control_plane/test_report_replay_routes.py
git commit -m "feat: add report and replay APIs"
```

### Task 10: Enforce loopback serving and host the production SPA

**Files:**
- Create: `src/select_fuzz/control_plane/security.py`
- Create: `src/select_fuzz/control_plane/static.py`
- Create: `src/select_fuzz/control_plane/server.py`
- Modify: `src/select_fuzz/cli.py`
- Modify: `src/select_fuzz/control_plane/app.py`
- Test: `tests/control_plane/test_security_static.py`

- [ ] **Step 1: Write failing security/static tests**

```python
def test_non_loopback_bind_is_rejected():
    with pytest.raises(ValueError, match="loopback"):
        ServerSettings(host="0.0.0.0")

def test_cross_origin_mutation_is_forbidden(client):
    response = client.post("/api/v1/runs", headers={"Origin": "https://evil.example"}, json={"mode": "correctness"})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/control_plane/test_security_static.py -q`
Expected: FAIL because unsafe hosts are accepted.

- [ ] **Step 3: Add loopback/Host/Origin policy and SPA fallback**

```python
@field_validator("host")
@classmethod
def loopback_only(cls, value: str) -> str:
    if ipaddress.ip_address(value).is_loopback is False:
        raise ValueError("control plane host must be loopback")
    return value
```

Allow `127.0.0.1` and `localhost` Host headers only; reject foreign `Origin` on mutating methods, require JSON bodies, configure no permissive CORS, and add CSP/frame/referrer headers. Serve hashed files from `frontend/dist/assets`; unknown non-API GETs return `index.html`, while unknown `/api/v1/*` remains problem `404`. Wire `select-fuzz serve` to Uvicorn with host fixed to `127.0.0.1` and optional browser open.

- [ ] **Step 4: Verify security and fallback behavior**

Run: `uv run pytest tests/control_plane/test_security_static.py -q`
Expected: `10 passed`; `Host: evil.example` is `400`, `/findings/case-7` serves the SPA, and `/api/v1/nope` is `404` problem JSON.

- [ ] **Step 5: Commit**

```bash
git add src/select_fuzz/control_plane src/select_fuzz/cli.py tests/control_plane/test_security_static.py
git commit -m "feat: secure loopback server and SPA hosting"
```

### Task 11: Bootstrap the typed React app, router, and local state panels

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/vitest.config.ts`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/schema.d.ts`
- Create: `frontend/src/app/queryClient.ts`
- Create: `frontend/src/app/router.tsx`
- Create: `frontend/src/app/AppShell.tsx`
- Create: `frontend/src/components/AsyncPanel.tsx`
- Test: `frontend/src/components/AsyncPanel.test.tsx`

- [ ] **Step 1: Write failing RTL tests for loading, empty, data, stale, and error states**

```tsx
it.each(["loading", "empty", "data", "stale", "error"] as const)("renders %s locally", (state) => {
  render(<AsyncPanel state={state} onRetry={vi.fn()}>{state === "data" ? "rows" : null}</AsyncPanel>);
  expect(screen.getByTestId(`panel-${state}`)).toBeInTheDocument();
});
```

- [ ] **Step 2: Install and verify RED**

Run: `cd frontend && npm install && npm test -- --run src/components/AsyncPanel.test.tsx`
Expected: FAIL because `AsyncPanel` is missing.

- [ ] **Step 3: Add Vite/React, typed fetch, Query client, and code-based routes**

```tsx
export function AsyncPanel({state, onRetry, children}: Props) {
  if (state === "loading") return <section data-testid="panel-loading" aria-busy="true">Loading…</section>;
  if (state === "empty") return <section data-testid="panel-empty">No data</section>;
  if (state === "error") return <section data-testid="panel-error" role="alert">Load failed <button onClick={onRetry}>Retry</button></section>;
  return <section data-testid={`panel-${state}`} aria-live="polite">{state === "stale" && <p>Showing saved data while reconnecting</p>}{children}</section>;
}
```

The fetch client parses `application/problem+json` into `ApiProblem`, sends same-origin JSON, and never turns one rejected query into an app-wide error. Define routes for `/`, `/runs`, `/runs/new`, `/runs/$runId`, `/findings`, `/findings/$findingId`, `/replays/$replayId`, and `/reports`.

- [ ] **Step 4: Generate API types and verify GREEN**

Run: `uv run python -m select_fuzz.control_plane.openapi > /tmp/mysql-fuzzer-openapi.json && cd frontend && npm run api:generate && npm test -- --run`
Expected: generated `schema.d.ts` is updated and all component tests pass.

- [ ] **Step 5: Commit**

```bash
git add web package-lock.json
git commit -m "feat: bootstrap resilient typed web console"
```

### Task 12: Implement overview, new-run, and run-history pages

**Files:**
- Create: `frontend/src/pages/OverviewPage.tsx`
- Create: `frontend/src/pages/NewRunPage.tsx`
- Create: `frontend/src/pages/RunsPage.tsx`
- Create: `frontend/src/components/NodeStatusGrid.tsx`
- Create: `frontend/src/components/MetricCard.tsx`
- Test: `frontend/src/pages/OverviewPage.test.tsx`
- Test: `frontend/src/pages/NewRunPage.test.tsx`

- [ ] **Step 1: Write MSW-backed page tests for node failure isolation and mode-aware forms**

```tsx
it("forces one worker for performance without losing entered options", async () => {
  renderRoute("/runs/new");
  await userEvent.selectOptions(screen.getByLabelText("Mode"), "performance");
  expect(screen.getByLabelText("Workers")).toHaveValue(1);
  expect(screen.getByLabelText("Workers")).toBeDisabled();
  await userEvent.click(screen.getByRole("button", {name: "Start run"}));
  expect(await screen.findByText("starting")).toBeVisible();
});
```

- [ ] **Step 2: Verify RED**

Run: `cd frontend && npm test -- --run src/pages/OverviewPage.test.tsx src/pages/NewRunPage.test.tsx`
Expected: FAIL because the pages are route stubs.

- [ ] **Step 3: Implement independent dashboard queries and validated create form**

```tsx
const createRun = useMutation({
  mutationFn: (body: RunCreate) => api.post("/runs", body, {"Idempotency-Key": crypto.randomUUID()}),
  onSuccess: (run) => navigate({to: "/runs/$runId", params: {runId: run.id}}),
});
```

Overview shows service, all three named nodes, active tasks, throughput, coverage debt, and recent findings; each card owns its loading/empty/stale/error state. History supports state/mode filters and makes `recovering` and `orphaned` visually distinct from completed.

- [ ] **Step 4: Run page tests**

Run: `cd frontend && npm test -- --run src/pages/OverviewPage.test.tsx src/pages/NewRunPage.test.tsx`
Expected: `11 passed`, including an unreachable node card while throughput and findings remain visible.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages frontend/src/components
git commit -m "feat: add dashboard and run creation flows"
```

### Task 13: Reconcile run details through sequenced SSE and charts

**Files:**
- Create: `frontend/src/api/eventStream.ts`
- Create: `frontend/src/api/sequenceStore.ts`
- Create: `frontend/src/pages/RunDetailPage.tsx`
- Create: `frontend/src/components/CoverageChart.tsx`
- Create: `frontend/src/components/EventTimeline.tsx`
- Test: `frontend/src/api/eventStream.test.ts`
- Test: `frontend/src/pages/RunDetailPage.test.tsx`

- [ ] **Step 1: Write tests for duplicate suppression, gaps, snapshot replacement, refresh resume, and stop**

```ts
it("rebuilds on a sequence gap and resumes after the snapshot watermark", async () => {
  stream.accept({sequence: 40, kind: "run.state", payload: {state: "running"}});
  stream.accept({sequence: 42, kind: "run.metric", payload: {qps: 9}});
  expect(snapshot).toHaveBeenCalledOnce();
  await recovered;
  expect(connect).toHaveBeenLastCalledWith(47);
  expect(cache.getQueryData(["run", "r1"])).toMatchObject({state: "stopping"});
});
```

- [ ] **Step 2: Verify RED**

Run: `cd frontend && npm test -- --run src/api/eventStream.test.ts src/pages/RunDetailPage.test.tsx`
Expected: FAIL because the sequence reconciler is missing.

- [ ] **Step 3: Implement the strict sequence machine and run page**

```ts
if (event.sequence <= this.lastSequence) return;
if (event.sequence !== this.lastSequence + 1) {
  this.source.close();
  const snapshot = await this.api.snapshot();
  this.cache.applySnapshot(snapshot);
  this.lastSequence = snapshot.sequence;
  sessionStorage.setItem("event-sequence", String(snapshot.sequence));
  this.connect(snapshot.sequence);
  return;
}
this.apply(event);
this.lastSequence = event.sequence;
```

Show state, node health/fingerprints, event timeline, throughput and coverage charts with adjacent numeric tables, stop confirmation, and `TIMING_UNRELIABLE`/`cache_state_unverified` badges when present. On refresh, fetch the run before opening SSE; mark cached data stale during reconnect.

- [ ] **Step 4: Verify event and page tests**

Run: `cd frontend && npm test -- --run src/api/eventStream.test.ts src/pages/RunDetailPage.test.tsx`
Expected: `14 passed`; duplicate sequence 40 changes no counter and gap 41 triggers exactly one snapshot.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api frontend/src/pages/RunDetailPage.tsx frontend/src/components
git commit -m "feat: recover live run state from sequenced events"
```

### Task 14: Add virtualized finding search and complete anomaly detail

**Files:**
- Create: `frontend/src/pages/FindingsPage.tsx`
- Create: `frontend/src/pages/FindingDetailPage.tsx`
- Create: `frontend/src/components/FindingVirtualList.tsx`
- Create: `frontend/src/components/NodeOutcomeTabs.tsx`
- Create: `frontend/src/components/SqlBlock.tsx`
- Test: `frontend/src/pages/FindingsPage.test.tsx`
- Test: `frontend/src/pages/FindingDetailPage.test.tsx`

- [ ] **Step 1: Write tests for URL-backed filters, virtual scrolling, and three-node details**

```tsx
it("keeps filters in the URL and renders only the virtual window", async () => {
  renderRoute("/findings?severity=high&node=custom_on&errno=1064");
  expect(await screen.findByText("case-0001")).toBeVisible();
  expect(screen.queryByText("case-0900")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Severity")).toHaveValue("high");
});
```

- [ ] **Step 2: Verify RED**

Run: `cd frontend && npm test -- --run src/pages/FindingsPage.test.tsx src/pages/FindingDetailPage.test.tsx`
Expected: FAIL because search/detail components are absent.

- [ ] **Step 3: Implement cursor search, TanStack Virtual rows, and detail sections**

```tsx
const virtualizer = useVirtualizer({count: rows.length, getScrollElement: () => parentRef.current,
  estimateSize: () => 52, overscan: 8});
return <div ref={parentRef} role="region" aria-label="Findings">
  <div style={{height: virtualizer.getTotalSize(), position: "relative"}}>
    {virtualizer.getVirtualItems().map((item) => <FindingRow key={rows[item.index].id} row={rows[item.index]} item={item}/>)}
  </div>
</div>;
```

Filters cover mode, severity, node, SQL feature, errno, time range, and query text. Detail renders SQL/seed/database, first difference/statistics, baseline/custom_off/custom_on result-or-error and plans, warnings, configuration fingerprints, and opaque artifact links; one failed tab must not hide the others.

- [ ] **Step 4: Verify page behavior**

Run: `cd frontend && npm test -- --run src/pages/FindingsPage.test.tsx src/pages/FindingDetailPage.test.tsx`
Expected: `15 passed`; a 32,000-row fixture mounts fewer than 80 row elements.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/FindingsPage.tsx frontend/src/pages/FindingDetailPage.tsx frontend/src/components
git commit -m "feat: add scalable finding investigation UI"
```

### Task 15: Complete replay and report workflows

**Files:**
- Create: `frontend/src/pages/ReplayPage.tsx`
- Create: `frontend/src/pages/ReportsPage.tsx`
- Create: `frontend/src/components/ReplayNodeGrid.tsx`
- Test: `frontend/src/pages/ReplayPage.test.tsx`
- Test: `frontend/src/pages/ReportsPage.test.tsx`

- [ ] **Step 1: Write tests for replay transitions and report partial failure**

```tsx
it.each(["reproduced", "not_reproduced", "failed"])("renders %s replay", async (state) => {
  server.use(replayScenario(state));
  renderRoute("/replays/replay-7");
  expect(await screen.findByTestId(`replay-${state}`)).toBeVisible();
  expect(screen.getAllByRole("region", {name: /node/i})).toHaveLength(3);
});
```

- [ ] **Step 2: Verify RED**

Run: `cd frontend && npm test -- --run src/pages/ReplayPage.test.tsx src/pages/ReportsPage.test.tsx`
Expected: FAIL because both routes are stubs.

- [ ] **Step 3: Implement replay start/status and report history**

```tsx
const replay = useMutation({
  mutationFn: (caseId: string) => api.post("/replays", {case_id: caseId},
    {"Idempotency-Key": `replay-${caseId}-${crypto.randomUUID()}`}),
  onSuccess: (job) => navigate({to: "/replays/$replayId", params: {replayId: job.id}}),
});
```

Replay shows queued/running terminal state, per-node result/error, and a clear reproduced verdict; terminal failures keep diagnostics and retry starts a new key. Reports show mode/time/run filters, JSONL/HTML/replay-bundle links, durability watermark, and local error states.

- [ ] **Step 4: Run workflow tests**

Run: `cd frontend && npm test -- --run src/pages/ReplayPage.test.tsx src/pages/ReportsPage.test.tsx`
Expected: `10 passed`; a failed report request does not remove a running replay panel.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages frontend/src/components/ReplayNodeGrid.tsx
git commit -m "feat: add replay and report workflows"
```

### Task 16: Enforce component coverage, accessibility, and fault-state branches

**Files:**
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/test/server.ts`
- Create: `frontend/src/test/fixtures.ts`
- Create: `frontend/src/test/faultMatrix.test.tsx`
- Modify: `frontend/vitest.config.ts`
- Modify: `frontend/package.json`

- [ ] **Step 1: Add a failing fault/accessibility matrix**

```tsx
it.each([
  ["overview nodes 503", "/", "Node status unavailable"],
  ["finding 404", "/findings/missing", "Finding not found"],
  ["orphaned run", "/runs/orphaned", "orphaned"],
  ["replay failed", "/replays/failed", "Replay failed"],
])("isolates %s", async (_name, route, message) => {
  const {container} = renderRoute(route);
  expect(await screen.findByText(message)).toBeVisible();
  expect(await axe(container)).toHaveNoViolations();
});
```

- [ ] **Step 2: Run coverage and verify RED**

Run: `cd frontend && npm run test:coverage`
Expected: FAIL until all route branches and the configured 85% branch threshold are met.

- [ ] **Step 3: Add deterministic MSW fixtures and accessible fallbacks**

Configure fake timers only inside the SSE tests; reset handlers and Query cache after every test. Add labels, focus restoration after dialogs, keyboard tab activation, table alternatives for charts, visible focus, reduced-motion CSS, and route-scoped error boundaries for every fault in the matrix.

- [ ] **Step 4: Verify unit/component quality gates**

Run: `cd frontend && npm run typecheck && npm run lint && npm run test:coverage`
Expected: all commands exit 0; statements/lines/functions and branches are each at least 85%, with zero axe violations.

- [ ] **Step 5: Commit**

```bash
git add web
git commit -m "test: cover console accessibility and fault states"
```

### Task 17: Prove backend/frontend recovery with Playwright and production build

**Files:**
- Create: `tests/control_plane/e2e_app.py`
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/control-plane.spec.ts`
- Create: `frontend/e2e/faults.spec.ts`
- Create: `frontend/e2e/accessibility.spec.ts`
- Modify: `frontend/package.json`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing end-to-end journeys and failure injections**

```ts
test("start, refresh-resume, gap-rebuild, stop, finding, replay, report", async ({page}) => {
  await page.goto("/runs/new");
  await page.getByRole("button", {name: "Start run"}).click();
  await expect(page.getByText("running")).toBeVisible();
  await page.reload();
  await injectEventGap(page, 8, 10);
  await expect(page.getByText("Snapshot restored at sequence 10")).toBeVisible();
  await page.getByRole("button", {name: "Stop run"}).click();
  await page.getByRole("link", {name: "case-7"}).click();
  await page.getByRole("button", {name: "Replay"}).click();
  await expect(page.getByText("Reproduced")).toBeVisible();
});
```

- [ ] **Step 2: Run Playwright and verify RED**

Run: `cd frontend && npx playwright install chromium && npm run e2e`
Expected: FAIL before the fixture app exposes deterministic SSE/fault controls.

- [ ] **Step 3: Add a fake-core FastAPI fixture and full fault matrix**

`tests/control_plane/e2e_app.py` must inject in-memory ports, never MySQL. Cover startup preflight rejection, node disconnect/recovery, run `recovering/orphaned`, API 500, network offline/stale cache, empty lists, SSE duplicate/gap/pruned history, stop escalation, finding detail partial node error, replay not reproduced/failed, artifact 404, and report API failure. Run axe on every page at desktop and narrow viewport.

- [ ] **Step 4: Run all production gates and inspect the build**

Run: `uv run pytest tests/control_plane -q && cd frontend && npm run typecheck && npm run test:coverage && npm run build && npm run e2e`
Expected: backend tests pass, frontend branch coverage is at least 85%, Vite emits `frontend/dist/index.html` plus hashed assets, all Playwright journeys pass, and axe reports zero serious/critical violations.

Run: `uv run python -m select_fuzz.control_plane.openapi > /tmp/openapi.json && cd frontend && npm run api:check`
Expected: exit 0 with `schema.d.ts is current`.

- [ ] **Step 5: Commit the verified end-to-end slice**

```bash
git add .gitignore tests/control_plane frontend
git commit -m "test: verify control plane UI end to end"
```

## Final verification checklist

- [ ] Run `git status --short` and confirm only control-plane/UI files from this plan are present.
- [ ] Run `uv run pytest tests/control_plane -q`; expected: all backend unit/integration tests pass.
- [ ] Run `cd frontend && npm run typecheck && npm run lint && npm run test:coverage && npm run build && npm run e2e`; expected: all commands exit 0 and branch coverage is at least 85%.
- [ ] Run `rg -n "password|token|secret" tests/control_plane frontend/dist`; expected: only test field labels, never credential values or environment contents.
- [ ] Start `select-fuzz serve --host 127.0.0.1 --port <port>`, then run `lsof -nP -iTCP:<port> -sTCP:LISTEN`; expected: the listener is `127.0.0.1:<port>` and never `0.0.0.0:<port>` or a non-loopback address.
- [ ] Review `/api/v1/openapi.json`; expected: every mutating endpoint documents idempotency/error responses and every schema uses the same state/sequence names as the SPA.
- [ ] Push only after the implementation commits above and the full gate pass: `git push`; expected: the current `codex/...` branch updates successfully, or the implementer records the exact remote/network/authentication blocker.
