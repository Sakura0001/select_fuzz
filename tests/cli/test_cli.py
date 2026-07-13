from __future__ import annotations

from pathlib import Path
from threading import Event

from typer.testing import CliRunner

from select_fuzz.cli import MODE_RUNNERS, app
from select_fuzz.config import PreflightIssue, PreflightReport
from select_fuzz.domain import RunRequest
from select_fuzz.service import RunSummary
from select_fuzz.replay import ReplayResult, ReplayStatus
from select_fuzz.oracle import OracleVerdict


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


def test_cli_rejects_unregistered_mode_without_exposing_traceback(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--mode",
            "performance",
            "--config",
            str(PROJECT_ROOT / "config" / "example.yaml"),
            "--rounds",
            "1",
            "--artifacts",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert "not registered" in result.output
    assert "Traceback" not in result.output


def test_doctor_cli_returns_zero_with_warnings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "select_fuzz.cli.DOCTOR_FACTORY",
        lambda config: type(
            "Doctor",
            (),
            {
                "run": lambda self: PreflightReport(
                    warnings=(
                        PreflightIssue(code="configuration_difference", message="different"),
                    )
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
