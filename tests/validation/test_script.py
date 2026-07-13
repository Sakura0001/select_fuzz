from __future__ import annotations

from pathlib import Path
import importlib.util
import subprocess
import sys

from select_fuzz.validation.runtime import ProductionValidationResult


_SPEC = importlib.util.spec_from_file_location(
    "validation_12h_script", Path("scripts/validation_12h.py")
)
assert _SPEC is not None and _SPEC.loader is not None
validation_script = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = validation_script
_SPEC.loader.exec_module(validation_script)


def test_validation_script_dry_run_is_offline_and_writes_reports(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/validation_12h.py",
            "--duration",
            "1s",
            "--checkpoint",
            "1s",
            "--freeze",
            "0s",
            "--dry-run",
            "--output",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "dry-run" in completed.stdout
    assert (tmp_path / "coverage.json").is_file()
    assert (tmp_path / "operator-runbook.json").is_file()


def test_non_dry_cli_assembles_production_runner_without_hidden_epoch_limit(
    tmp_path: Path, monkeypatch: object
) -> None:
    captured: list[object] = []

    def fake_runner(config: object, **kwargs: object) -> ProductionValidationResult:
        captured.append(config)
        raise RuntimeError("runner reached")

    monkeypatch.setattr(  # type: ignore[attr-defined]
        validation_script, "run_production_validation", fake_runner
    )
    try:
        validation_script.main(
            [
                "--duration",
                "2h",
                "--checkpoint",
                "10m",
                "--freeze",
                "5m",
                "--output",
                str(tmp_path),
                "--seed-url",
                "https://dev.mysql.com/doc/refman/8.0/en/select.html",
                "--fault-command",
                "connection_reset=python -c 'print(1)'",
                "--fault-probe",
                "connection_reset=python -c 'raise SystemExit(0)'",
                "--mysql-connection-probe",
                "python -c 'print(3)'",
            ]
        )
    except RuntimeError as exc:
        assert str(exc) == "runner reached"
    else:
        raise AssertionError("production runner was not invoked")
    config = captured[0]
    assert config.duration_s == 7200  # type: ignore[attr-defined]
    assert config.checkpoint_s == 600  # type: ignore[attr-defined]
    assert config.max_epochs is None  # type: ignore[attr-defined]
    assert config.fault_commands[0][0] == "connection_reset"  # type: ignore[attr-defined]
    assert config.fault_probe_commands[0][0] == "connection_reset"  # type: ignore[attr-defined]
    assert config.mysql_connection_probe_command[-1] == "print(3)"  # type: ignore[attr-defined]
