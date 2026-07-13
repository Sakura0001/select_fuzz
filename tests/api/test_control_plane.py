from __future__ import annotations

import gzip
import json
from pathlib import Path

from fastapi.testclient import TestClient

from select_fuzz.api.app import create_app
from select_fuzz.api.supervisor import InMemoryProcessSupervisor
from select_fuzz.api.security import require_loopback_bind


def _client(tmp_path: Path) -> tuple[TestClient, InMemoryProcessSupervisor]:
    supervisor = InMemoryProcessSupervisor()
    app = create_app(
        state_path=tmp_path / "state.sqlite3",
        artifact_root=tmp_path / "artifacts",
        supervisor=supervisor,
    )
    return TestClient(app, base_url="http://127.0.0.1"), supervisor


def test_health_create_list_detail_and_stop_are_durable(tmp_path: Path) -> None:
    client, supervisor = _client(tmp_path)
    assert client.get("/api/v1/health").json() == {"status": "ok"}

    response = client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": "correctness-42"},
        json={"mode": "correctness", "seed": 42},
    )
    assert response.status_code == 202
    run = response.json()
    assert run["request"]["workers"] == 10
    assert supervisor.started == [run["id"]]
    assert client.get("/api/v1/runs").json()["items"][0]["id"] == run["id"]
    assert client.get(f"/api/v1/runs/{run['id']}").json()["state"] == "running"

    stopped = client.post(f"/api/v1/runs/{run['id']}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"
    assert supervisor.stopped == [run["id"]]

    # A new app process reads the same persisted state.
    reopened = TestClient(
        create_app(
            state_path=tmp_path / "state.sqlite3",
            artifact_root=tmp_path / "artifacts",
            supervisor=InMemoryProcessSupervisor(),
        ),
        base_url="http://127.0.0.1",
    )
    assert reopened.get(f"/api/v1/runs/{run['id']}").json()["state"] == "stopped"


def test_validation_and_not_found_use_rfc9457(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    invalid = client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": "performance-2"},
        json={"mode": "performance", "workers": 2},
    )
    assert invalid.status_code == 422
    assert invalid.headers["content-type"].startswith("application/problem+json")
    assert invalid.json()["type"] == "urn:select-fuzz:problem:validation"
    assert invalid.json()["errors"][0]["pointer"] == "/body"
    assert invalid.headers["x-request-id"] == invalid.json()["request_id"]

    missing = client.get("/api/v1/runs/missing")
    assert missing.status_code == 404
    assert missing.json()["type"] == "urn:select-fuzz:problem:not-found"


def test_idempotency_reuses_identical_request_and_rejects_conflict(tmp_path: Path) -> None:
    client, supervisor = _client(tmp_path)
    headers = {"Idempotency-Key": "same-key-123"}
    first = client.post("/api/v1/runs", headers=headers, json={"mode": "correctness"})
    second = client.post("/api/v1/runs", headers=headers, json={"mode": "correctness"})
    assert first.json()["id"] == second.json()["id"]
    assert len(supervisor.started) == 1

    conflict = client.post(
        "/api/v1/runs", headers=headers, json={"mode": "correctness", "seed": 99}
    )
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")


def test_loopback_host_and_foreign_origin_are_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get("/api/v1/health", headers={"Host": "evil.example"}).status_code == 400
    rejected = client.post(
        "/api/v1/runs",
        headers={"Origin": "https://evil.example", "Idempotency-Key": "foreign-origin"},
        json={"mode": "correctness"},
    )
    assert rejected.status_code == 403
    assert rejected.headers["content-type"].startswith("application/problem+json")


def test_ipv6_loopback_and_same_origin_with_port_are_allowed(tmp_path: Path) -> None:
    assert require_loopback_bind("::1") == "::1"
    client, _ = _client(tmp_path)
    accepted = client.post(
        "/api/v1/runs",
        headers={
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Idempotency-Key": "same-origin-port",
        },
        json={"mode": "correctness"},
    )
    assert accepted.status_code == 202


def test_read_endpoints_never_accept_paths(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    root = tmp_path / "artifacts"
    finding = root / "findings" / "case-7"
    finding.mkdir(parents=True)
    (finding / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_id": "case-7",
                    "replay": {
                        "setup_sql": ["CREATE TABLE t(id INT)"],
                        "query_sql": "SELECT id FROM t ORDER BY 1",
                        "seeds": {"query": 7},
                        "databases": {
                            "baseline": "db_base", "custom_off": "db_off", "custom_on": "db_on"
                        },
                        "query_limits": {"timeout_seconds": 15, "row_limit": 10000, "byte_limit": 1024},
                    },
                "result_files": {
                    role: f"{role}.result.json.gz"
                    for role in ("baseline", "custom_off", "custom_on")
                },
            }
        ),
        encoding="utf-8",
    )
    for role in ("baseline", "custom_off", "custom_on"):
        with gzip.open(finding / f"{role}.result.json.gz", "wb") as stream:
            stream.write(json.dumps({"status": "success", "rows": [[1]]}).encode())
    (root / "reports").mkdir()
    (root / "reports" / "report-7.html").write_text("<h1>safe</h1>", encoding="utf-8")

    finding_detail = client.get("/api/v1/findings/case-7")
    assert finding_detail.status_code == 200
    assert set(finding_detail.json()["nodes"]) == {"baseline", "custom_off", "custom_on"}
    assert finding_detail.json()["reproduction"]["query_sql"].endswith("ORDER BY 1")
    assert client.get("/api/v1/reports/report-7").status_code == 200
    assert client.get("/api/v1/artifacts/report-7").status_code == 200
    assert client.get("/api/v1/artifacts/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_run_pagination_and_json_content_type(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    for index in range(3):
        assert client.post(
            "/api/v1/runs",
            headers={"Idempotency-Key": f"page-key-{index}"},
            json={"mode": "correctness", "seed": index},
        ).status_code == 202
    first = client.get("/api/v1/runs?limit=2").json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = client.get(f"/api/v1/runs?limit=2&cursor={first['next_cursor']}").json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert client.post(
        "/api/v1/runs",
        headers={"Idempotency-Key": "wrong-content", "Content-Type": "text/plain"},
        content='{"mode":"correctness"}',
    ).status_code == 415


def test_spa_fallback_never_swallows_api_404(tmp_path: Path) -> None:
    supervisor = InMemoryProcessSupervisor()
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<main>console</main>", encoding="utf-8")
    client = TestClient(
        create_app(
            state_path=tmp_path / "state.sqlite3",
            artifact_root=tmp_path / "artifacts",
            supervisor=supervisor,
            spa_dist=dist,
        ),
        base_url="http://127.0.0.1",
    )
    assert "console" in client.get("/runs/run-1").text
    missing = client.get("/api/v1/unknown")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")


def test_unexpected_errors_are_redacted_and_non_http_origin_is_rejected(tmp_path: Path) -> None:
    def broken_snapshot() -> dict[str, object]:
        raise RuntimeError("password=do-not-leak")

    app = create_app(
        state_path=tmp_path / "state.sqlite3",
        artifact_root=tmp_path / "artifacts",
        supervisor=InMemoryProcessSupervisor(),
        snapshot_provider=broken_snapshot,
    )
    client = TestClient(
        app, base_url="http://127.0.0.1", raise_server_exceptions=False
    )
    failed = client.get("/api/v1/snapshot")
    assert failed.status_code == 500
    assert failed.headers["content-type"].startswith("application/problem+json")
    assert "do-not-leak" not in failed.text
    origin = client.post(
        "/api/v1/runs",
        headers={
            "Origin": "file://127.0.0.1",
            "Idempotency-Key": "origin-scheme",
        },
        json={"mode": "correctness"},
    )
    assert origin.status_code == 403


def test_findings_are_served_from_paginated_read_index(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    records = []
    for sequence in range(1, 4):
        records.append(
            json.dumps(
                {
                    "sequence": sequence,
                    "kind": "finding.created",
                    "payload": {
                        "id": f"case-{sequence}", "run_id": "run-1",
                        "mode": "correctness", "severity": "high",
                        "node": "custom_on", "feature": "cte", "errno": 1064,
                        "occurred_at": f"2026-07-13T00:00:0{sequence}Z",
                    },
                }
            )
        )
    (root / "events.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")
    client = TestClient(
        create_app(
            state_path=tmp_path / "state.sqlite3",
            artifact_root=root,
            supervisor=InMemoryProcessSupervisor(),
        ),
        base_url="http://127.0.0.1",
    )
    first = client.get("/api/v1/findings?limit=2&severity=high").json()
    assert [item["id"] for item in first["items"]] == ["case-3", "case-2"]
    second = client.get(f"/api/v1/findings?limit=2&cursor={first['next_cursor']}").json()
    assert [item["id"] for item in second["items"]] == ["case-1"]


def test_performance_alert_detail_exposes_reproduction_and_measurements(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    finding = root / "performance_findings" / "perf-case-1"
    finding.mkdir(parents=True)
    (finding / "manifest.json").write_text(
        json.dumps(
            {
                "case_id": "perf-case-1",
                "type": "performance_alert",
                "sql": "SELECT 1 ORDER BY 1",
                "seed": 7,
                "database": "sf_p_case",
                "scale": {"table_rows": 100},
                "data_manifest": {"setup_statements": ["CREATE TABLE t(i INT)"]},
                "measurements": {"baseline": {"root_end_ms": 5000}},
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            state_path=tmp_path / "state.sqlite3",
            artifact_root=root,
            supervisor=InMemoryProcessSupervisor(),
        ),
        base_url="http://127.0.0.1",
    )

    response = client.get("/api/v1/findings/perf-case-1")

    assert response.status_code == 200
    assert response.json()["reproduction"]["sql"] == "SELECT 1 ORDER BY 1"
    assert response.json()["nodes"]["baseline"]["root_end_ms"] == 5000


def test_performance_calibration_failure_detail_resolves_latest_attempt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    finding = root / "performance_findings" / "bad-case_attempt_2"
    finding.mkdir(parents=True)
    (finding / "manifest.json").write_text(
        json.dumps(
            {
                "case_id": "bad-case",
                "type": "performance_calibration_failure",
                "run_id": "run-p",
                "occurred_at": "2026-07-13T00:00:00Z",
                "diagnostic_attempt": 2,
                "failure_category": "setup_mismatch",
                "sql": "SELECT 1 ORDER BY 1",
                "data_manifest": {"rows": 1},
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            state_path=tmp_path / "state.sqlite3",
            artifact_root=root,
            supervisor=InMemoryProcessSupervisor(),
        ),
        base_url="http://127.0.0.1",
    )

    response = client.get("/api/v1/findings/bad-case")

    assert response.status_code == 200
    assert response.json()["manifest"]["failure_category"] == "setup_mismatch"
