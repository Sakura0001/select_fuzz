from __future__ import annotations

from pathlib import Path
from threading import Event

from typer.testing import CliRunner

from select_fuzz.cli import MODE_RUNNERS, app
from select_fuzz.cleanup import CleanupNodeResult, CleanupReport
from select_fuzz.config import NodeRole, PreflightIssue, PreflightReport
from select_fuzz.domain import RunRequest
from select_fuzz.service import RunSummary
from select_fuzz.replay import ReplayResult, ReplayStatus
from select_fuzz.oracle import OracleVerdict, QueryErrorDisposition


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_correctness_mode_is_registered_by_default() -> None:
    assert "correctness" in MODE_RUNNERS


class _Runner:
    def __init__(self) -> None:
        self.requests: list[RunRequest] = []
        self.stop_events: list[Event] = []

    def run(self, request: RunRequest, stop_event: Event) -> RunSummary:
        self.requests.append(request)
        self.stop_events.append(stop_event)
        return RunSummary(
            run_id=request.run_id,
            rounds_completed=request.rounds or 0,
            queries_completed=request.queries_per_round * (request.rounds or 0),
            findings=0,
            rejected=0,
            over_budget=0,
            stopped=False,
        )


def test_cli_dispatches_correctness_defaults(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    runner = _Runner()
    monkeypatch.setitem(MODE_RUNNERS, "correctness", lambda config, root: runner)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--mode",
            "correctness",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--rounds",
            "1",
            "--artifacts",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert runner.requests[0].workers == 10
    assert runner.requests[0].queries_per_round == 1000
    assert runner.requests[0].rounds == 1
    assert '"queries_completed":1000' in result.output


def test_run_cli_returns_nonzero_when_findings_were_preserved(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    class FindingRunner(_Runner):
        def run(self, request: RunRequest, stop_event: Event) -> RunSummary:
            summary = super().run(request, stop_event)
            return RunSummary(
                run_id=summary.run_id,
                rounds_completed=summary.rounds_completed,
                queries_completed=summary.queries_completed,
                findings=1,
                rejected=summary.rejected,
                over_budget=summary.over_budget,
                stopped=summary.stopped,
            )

    monkeypatch.setitem(
        MODE_RUNNERS,
        "correctness",
        lambda config, root: FindingRunner(),
    )
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--mode",
            "correctness",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--rounds",
            "1",
            "--artifacts",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert '"findings":1' in result.output


def test_cli_overrides_seed_workers_and_queries(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    runner = _Runner()
    monkeypatch.setitem(MODE_RUNNERS, "correctness", lambda config, root: runner)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--mode",
            "correctness",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--rounds",
            "2",
            "--seed",
            "99",
            "--workers",
            "3",
            "--queries-per-round",
            "7",
            "--artifacts",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    request = runner.requests[0]
    assert (request.seed, request.workers, request.queries_per_round) == (99, 3, 7)


def test_run_cli_sanitizes_runner_failures(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    class FailingRunner:
        def run(self, request: RunRequest, stop_event: Event) -> RunSummary:
            raise RuntimeError("must-not-leak-database-error-detail")

    monkeypatch.setitem(
        MODE_RUNNERS,
        "correctness",
        lambda config, root: FailingRunner(),
    )

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--mode",
            "correctness",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--rounds",
            "1",
            "--artifacts",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "run failed: RuntimeError" in result.output
    assert "must-not-leak" not in result.output
    assert "Traceback" not in result.output


def test_cli_rejects_unknown_mode_without_exposing_traceback(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--mode",
            "not-a-mode",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--rounds",
            "1",
            "--artifacts",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert "not-a-mode" in result.output or "Invalid" in result.output
    assert "Traceback" not in result.output


def test_doctor_cli_returns_zero_with_warnings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "select_fuzz.cli.DOCTOR_FACTORY",
        lambda config: type(
            "Doctor",
            (),
            {
                "run": lambda self: PreflightReport(
                    warnings=(PreflightIssue(code="configuration_difference", message="different"),)
                )
            },
        )(),
    )

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--mode",
            "correctness",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"configuration_difference"' in result.output


def test_doctor_cli_returns_one_for_fatal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "select_fuzz.cli.DOCTOR_FACTORY",
        lambda config: type(
            "Doctor",
            (),
            {
                "run": lambda self: PreflightReport(
                    fatals=(PreflightIssue(code="missing_capability", message="missing"),)
                )
            },
        )(),
    )

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--mode",
            "correctness",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
        ],
    )

    assert result.exit_code == 1
    assert '"missing_capability"' in result.output


def test_report_cli_builds_html_from_artifacts(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(
        '{"case_id":"case_1","type":"finding"}\n', encoding="utf-8"
    )
    output = tmp_path / "report.html"

    result = CliRunner().invoke(
        app,
        ["report", "--artifacts", str(tmp_path), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert output.exists()
    assert "Select Fuzz Report" in output.read_text(encoding="utf-8")
    assert str(output) in result.output


class _ReplayRunner:
    def replay(self, reference: str | Path) -> ReplayResult:
        return ReplayResult(
            case_id=str(reference),
            database="sf_c_20260713t120000_w0_r0_sabc_n123_q0",
            status=ReplayStatus.REPRODUCED,
            original_verdict=OracleVerdict.RESULT_MISMATCH.value,
            replay_verdict=OracleVerdict.RESULT_MISMATCH,
            executions=(),
        )


def test_replay_cli_dispatches_finding_and_emits_json(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "select_fuzz.cli.REPLAY_FACTORY",
        lambda config, root: _ReplayRunner(),
    )

    result = CliRunner().invoke(
        app,
        [
            "replay",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--artifacts",
            str(tmp_path),
            "--finding",
            "case_finding_1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"status":"reproduced"' in result.output
    assert '"case_id":"case_finding_1"' in result.output
    assert '"replay_verdict":"result_mismatch"' in result.output
    assert '"oracle_verdict":"result_mismatch"' in result.output


def test_replay_cli_emits_effective_generator_classification(  # type: ignore[no-untyped-def]
    monkeypatch, tmp_path: Path
) -> None:
    class GeneratorFindingRunner(_ReplayRunner):
        def replay(self, reference: str | Path) -> ReplayResult:
            result = super().replay(reference)
            return ReplayResult(
                case_id=result.case_id,
                database=result.database,
                status=ReplayStatus.REPRODUCED,
                original_verdict=(QueryErrorDisposition.UNEXPECTED_VALID_ERROR.value),
                replay_verdict=OracleVerdict.MATCH,
                executions=(),
                replay_classification=(QueryErrorDisposition.UNEXPECTED_VALID_ERROR.value),
            )

    monkeypatch.setattr(
        "select_fuzz.cli.REPLAY_FACTORY",
        lambda config, root: GeneratorFindingRunner(),
    )

    result = CliRunner().invoke(
        app,
        [
            "replay",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--artifacts",
            str(tmp_path),
            "--finding",
            "case_generator_1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"replay_verdict":"unexpected_valid_error"' in result.output
    assert '"oracle_verdict":"match"' in result.output
    assert '"replay_verdict":"match"' not in result.output


def test_replay_cli_returns_one_when_finding_no_longer_reproduces(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    class NotReproduced(_ReplayRunner):
        def replay(self, reference: str | Path) -> ReplayResult:
            result = super().replay(reference)
            return ReplayResult(
                case_id=result.case_id,
                database=result.database,
                status=ReplayStatus.NOT_REPRODUCED,
                original_verdict=result.original_verdict,
                replay_verdict=OracleVerdict.MATCH,
                executions=(),
            )

    monkeypatch.setattr(
        "select_fuzz.cli.REPLAY_FACTORY",
        lambda config, root: NotReproduced(),
    )

    result = CliRunner().invoke(
        app,
        [
            "replay",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--artifacts",
            str(tmp_path),
            "--finding",
            "case_finding_1",
        ],
    )

    assert result.exit_code == 1
    assert '"status":"not_reproduced"' in result.output


def test_regression_seeds_cli_writes_versioned_corpus(tmp_path: Path) -> None:
    output = tmp_path / "seeds.json"

    result = CliRunner().invoke(
        app,
        [
            "regression-seeds",
            "--output",
            str(output),
            "--seed",
            "20260712",
        ],
    )

    assert result.exit_code == 0, result.output
    assert output.exists()
    assert '"schema_version":1' in output.read_text(encoding="utf-8")


def test_serve_cli_builds_loopback_app_with_real_supervision(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    received: dict[str, object] = {}

    def fake_run(app, *, host: str, port: int, log_level: str) -> None:  # type: ignore[no-untyped-def]
        received.update(app=app, host=host, port=port, log_level=log_level)

    monkeypatch.setattr("select_fuzz.cli.uvicorn.run", fake_run)
    result = CliRunner().invoke(
        app,
        [
            "serve",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--state",
            str(tmp_path / "state.sqlite3"),
            "--spa-dist",
            str(dist),
            "--port",
            "8877",
        ],
    )

    assert result.exit_code == 0, result.output
    assert received["host"] == "127.0.0.1"
    assert received["port"] == 8877


def test_cleanup_cli_defaults_to_plan_and_requires_explicit_execute(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[tuple[str, ...], bool]] = []

    class Cleanup:
        def run(self, databases: tuple[str, ...], *, execute: bool = False) -> CleanupReport:
            calls.append((databases, execute))
            return CleanupReport(
                databases,
                execute,
                tuple(
                    CleanupNodeResult(database, role, execute)
                    for database in databases
                    for role in NodeRole
                ),
            )

    monkeypatch.setattr("select_fuzz.cli.CLEANUP_FACTORY", lambda config: Cleanup())
    managed = "sf_p_20260713t112233_w0_r1_s0123456789_nabcdef12_q0"
    planned = CliRunner().invoke(
        app,
        [
            "cleanup",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--database",
            managed,
        ],
    )
    executed = CliRunner().invoke(
        app,
        [
            "cleanup",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--database",
            managed,
            "--execute",
        ],
    )

    assert planned.exit_code == 0, planned.output
    assert '"execute":false' in planned.output
    assert executed.exit_code == 0, executed.output
    assert calls == [((managed,), False), ((managed,), True)]


def test_cleanup_cli_returns_one_for_sanitized_partial_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    managed = "sf_c_20260713t112233_w0_r1_s0123456789_nabcdef12_q0"

    class Cleanup:
        def run(self, databases: tuple[str, ...], *, execute: bool = False) -> CleanupReport:
            return CleanupReport(
                databases,
                execute,
                (CleanupNodeResult(databases[0], NodeRole.CUSTOM_ON, False, "RuntimeError"),),
            )

    monkeypatch.setattr("select_fuzz.cli.CLEANUP_FACTORY", lambda config: Cleanup())
    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--database",
            managed,
            "--execute",
        ],
    )

    assert result.exit_code == 1
    assert '"error_type":"RuntimeError"' in result.output
    assert "Traceback" not in result.output
