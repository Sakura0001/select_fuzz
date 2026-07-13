"""FastAPI factory around durable control-plane ports."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
import asyncio
import base64
import json
from pathlib import Path
from typing import Annotated
from threading import Lock

from fastapi import BackgroundTasks, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from select_fuzz.artifacts import ArtifactReader, ArtifactValidationError
from select_fuzz.api.artifacts import SafeArtifactStore
from select_fuzz.api.contracts import ReplayCreate, ReplayView, RunCreate, RunPage, RunView
from select_fuzz.api.events import EventBroker, EventHistoryExpired, encode_sse
from select_fuzz.api.problems import ApiProblem, api_problem_handler, response, validation_handler
from select_fuzz.api.read_index import ReadIndex
from select_fuzz.api.replays import ReplayExecutor, ReplayJobRunner
from select_fuzz.api.run_state import IdempotencyConflict, RunStore
from select_fuzz.api.security import LoopbackSecurityMiddleware
from select_fuzz.api.supervisor import ProcessSupervisor


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeError) as error:
        raise ApiProblem(422, "validation", "Validation failed", "Cursor is invalid.") from error
    if value < 0:
        raise ApiProblem(422, "validation", "Validation failed", "Cursor is invalid.")
    return value


class _DisabledReplay:
    async def execute(self, case_id: str) -> dict[str, object]:
        del case_id
        raise RuntimeError("replay executor is not configured")


def create_app(
    *,
    state_path: str | Path,
    artifact_root: str | Path,
    supervisor: ProcessSupervisor,
    replay_executor: ReplayExecutor | None = None,
    spa_dist: str | Path | None = None,
    snapshot_provider: Callable[[], Mapping[str, object]] | None = None,
) -> FastAPI:
    state = Path(state_path)
    artifact_path = Path(artifact_root)
    store = RunStore(state)
    supervisor.bind_store(store)
    artifacts = SafeArtifactStore(artifact_path)
    artifact_reader = ArtifactReader(artifact_path)
    events = EventBroker(state)
    event_binder = getattr(supervisor, "bind_event_publisher", None)
    if callable(event_binder):
        event_binder(events.publish)
    index = ReadIndex(state.with_name(state.stem + "-read.sqlite3"))
    facts_path = artifact_path / "events.jsonl"
    index_lock = Lock()

    def refresh_index() -> None:
        with index_lock:
            index.refresh(facts_path)

    index.rebuild(facts_path)
    replay_jobs = ReplayJobRunner(store, replay_executor or _DisabledReplay())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await supervisor.recover()
        yield

    app = FastAPI(title="select-fuzz control plane", version="1.0.0", lifespan=lifespan)
    app.state.run_store = store
    app.state.artifact_store = artifacts
    app.state.event_broker = events
    app.state.read_index = index
    app.state.supervisor = supervisor
    app.state.replay_jobs = replay_jobs
    app.add_middleware(LoopbackSecurityMiddleware)
    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(ApiProblem, api_problem_handler)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, _error: Exception) -> JSONResponse:
        return response(
            request,
            ApiProblem(
                500, "internal", "Internal server error",
                "The request failed. Consult local service logs with the request ID.",
            ),
        )

    @app.get("/api/v1/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/runs", response_model=RunView, status_code=202, tags=["runs"])
    async def create_run(
        body: RunCreate,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> RunView:
        if idempotency_key is None or not 8 <= len(idempotency_key) <= 128:
            raise ApiProblem(422, "validation", "Validation failed", "Idempotency-Key is required.")
        try:
            record, created = store.create_once(body, idempotency_key)
        except IdempotencyConflict as error:
            raise ApiProblem(
                409, "idempotency-conflict", "Idempotency conflict",
                "The key was already used with a different request.",
            ) from error
        if not created:
            return record
        try:
            record = await supervisor.start(record.id, body)
        except Exception as error:
            store.set_state(record.id, "failed")
            raise ApiProblem(503, "worker-start", "Worker unavailable", "The worker could not start.") from error
        return record

    @app.get("/api/v1/runs", response_model=RunPage, tags=["runs"])
    async def list_runs(
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        cursor: str | None = None,
    ) -> RunPage:
        offset = _offset(cursor)
        rows = store.list(limit=limit + 1, offset=offset)
        more = len(rows) > limit
        return RunPage(items=rows[:limit], next_cursor=_cursor(offset + limit) if more else None)

    @app.get("/api/v1/runs/{run_id}", response_model=RunView, tags=["runs"])
    async def get_run(run_id: str) -> RunView:
        record = store.get(run_id)
        if record is None:
            raise ApiProblem(404, "not-found", "Not found", "Run does not exist.")
        return record

    @app.post("/api/v1/runs/{run_id}/stop", response_model=RunView, tags=["runs"])
    async def stop_run(run_id: str) -> RunView:
        if store.get(run_id) is None:
            raise ApiProblem(404, "not-found", "Not found", "Run does not exist.")
        try:
            stopped = await supervisor.stop(run_id)
        except KeyError as error:
            raise ApiProblem(404, "not-found", "Not found", "Run does not exist.") from error
        return stopped

    @app.get("/api/v1/events", tags=["events"])
    async def stream_events(
        request: Request,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        if last_event_id is not None:
            try:
                after = int(last_event_id)
            except ValueError as error:
                raise ApiProblem(422, "validation", "Validation failed", "Last-Event-ID is invalid.") from error
        try:
            subscription = events.open_subscription(after)
        except EventHistoryExpired as error:
            raise ApiProblem(
                409, "event-history-expired", "Event history expired",
                "Refresh from /api/v1/snapshot before reconnecting.",
            ) from error

        async def body() -> AsyncIterator[bytes]:
            try:
                async for event in subscription:
                    if await request.is_disconnected():
                        return
                    yield b": heartbeat\n\n" if event is None else encode_sse(event)
            finally:
                await subscription.aclose()

        return StreamingResponse(
            body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/v1/snapshot", tags=["events"])
    async def snapshot() -> dict[str, object]:
        await asyncio.to_thread(refresh_index)
        base: dict[str, object] = {
            "sequence": events.sequence,
            "service": {"status": "ok"},
            "runs": [item.model_dump(mode="json") for item in store.list(limit=200)],
            "replays": [item.model_dump(mode="json") for item in store.list_replays()],
            "recent_findings": index.list_findings(limit=20),
            "nodes": [],
        }
        if snapshot_provider is not None:
            base.update(snapshot_provider())
            base["sequence"] = events.sequence
        return base

    @app.get("/api/v1/findings", tags=["findings"])
    async def list_findings(
        mode: str | None = None,
        severity: str | None = None,
        node: str | None = None,
        feature: str | None = None,
        errno: int | None = None,
        query: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        cursor: str | None = None,
    ) -> dict[str, object]:
        await asyncio.to_thread(refresh_index)
        offset = _offset(cursor)
        rows = index.list_findings(
            mode=mode, severity=severity, node=node, feature=feature, errno=errno,
            query=query, limit=limit + 1, offset=offset,
        )
        more = len(rows) > limit
        return {"items": rows[:limit], "next_cursor": _cursor(offset + limit) if more else None}

    @app.get("/api/v1/findings/{finding_id}", tags=["findings"])
    async def finding(finding_id: str) -> dict[str, object]:
        try:
            stored = artifact_reader.get_finding(finding_id)
        except ArtifactValidationError:
            if not artifacts.valid_id(finding_id):
                raise ApiProblem(404, "not-found", "Not found", "Finding does not exist.")
            performance_root = artifact_path / "performance_findings"
            candidates = [performance_root / finding_id / "manifest.json"]
            candidates.extend(
                path / "manifest.json"
                for path in performance_root.glob(f"{finding_id}_attempt_*")
                if path.is_dir()
            )
            decoded: list[dict[str, object]] = []
            for candidate in candidates:
                try:
                    value = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict) and value.get("case_id") == finding_id:
                    decoded.append(value)
            if not decoded:
                raise ApiProblem(
                    404, "not-found", "Not found", "Finding does not exist."
                )
            def diagnostic_attempt(item: dict[str, object]) -> int:
                value = item.get("diagnostic_attempt")
                return value if isinstance(value, int) and not isinstance(value, bool) else 0

            performance = max(decoded, key=diagnostic_attempt)
            if (
                performance.get("type")
                not in {"performance_alert", "performance_calibration_failure"}
            ):
                raise ApiProblem(
                    500,
                    "artifact-invalid",
                    "Invalid artifact",
                    "Performance finding artifact is invalid.",
                )
            return {
                "id": finding_id,
                "manifest": jsonable_encoder(performance),
                "reproduction": {
                    key: jsonable_encoder(performance.get(key))
                    for key in (
                        "sql",
                        "seed",
                        "database",
                        "scale",
                        "data_manifest",
                    )
                    if key in performance
                },
                "nodes": jsonable_encoder(performance.get("measurements", {})),
            }
        replay = dict(stored.replay_manifest)
        return {
            "id": stored.case_id,
            "manifest": jsonable_encoder(dict(stored.manifest)),
            "reproduction": {
                key: jsonable_encoder(replay.get(key))
                for key in ("setup_sql", "query_sql", "seeds", "databases", "query_limits")
                if key in replay
            },
            "nodes": {
                role.value: jsonable_encoder(dict(result)) for role, result in stored.results.items()
            },
        }

    @app.post("/api/v1/replays", response_model=ReplayView, status_code=202, tags=["replays"])
    async def create_replay(
        body: ReplayCreate,
        background: BackgroundTasks,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> ReplayView:
        if idempotency_key is None or not 8 <= len(idempotency_key) <= 128:
            raise ApiProblem(422, "validation", "Validation failed", "Idempotency-Key is required.")
        try:
            job, created = store.create_replay_once(body.case_id, idempotency_key)
        except IdempotencyConflict as error:
            raise ApiProblem(
                409, "idempotency-conflict", "Idempotency conflict",
                "The key was already used with a different replay.",
            ) from error
        if created:
            background.add_task(replay_jobs.run, job.id)
        return job

    @app.get("/api/v1/replays/jobs/{replay_id}", response_model=ReplayView, tags=["replays"])
    async def replay_job(replay_id: str) -> ReplayView:
        job = store.get_replay(replay_id)
        if job is None:
            raise ApiProblem(404, "not-found", "Not found", "Replay job does not exist.")
        return job

    @app.get("/api/v1/reports", tags=["reports"])
    async def reports() -> dict[str, object]:
        return {"items": artifacts.list_reports()}

    @app.get("/api/v1/reports/{report_id}", tags=["reports"])
    async def report(report_id: str) -> dict[str, str]:
        ref = artifacts.report(report_id)
        if ref is None:
            raise ApiProblem(404, "not-found", "Not found", "Report does not exist.")
        return {"id": report_id, "artifact_url": f"/api/v1/artifacts/{report_id}"}

    @app.get("/api/v1/artifacts/{artifact_id}", tags=["artifacts"])
    async def artifact(artifact_id: str) -> FileResponse:
        ref = artifacts.report(artifact_id)
        if ref is None:
            raise ApiProblem(404, "not-found", "Not found", "Artifact does not exist.")
        return FileResponse(ref.path, media_type=ref.media_type, filename=ref.filename)

    dist = None if spa_dist is None else Path(spa_dist).resolve()
    if dist is not None and (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="spa-assets")

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa_fallback(spa_path: str, request: Request) -> FileResponse:
        if spa_path.startswith("api/") or dist is None or not (dist / "index.html").is_file():
            raise ApiProblem(404, "not-found", "Not found", "Endpoint does not exist.")
        return FileResponse(dist / "index.html", media_type="text/html")

    return app
