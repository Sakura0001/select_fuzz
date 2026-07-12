from typer.testing import CliRunner

from select_fuzz import __version__
from select_fuzz.cli import app


def test_package_and_cli_are_importable() -> None:
    assert __version__ == "0.1.0"
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "serve", "doctor", "replay", "report", "cleanup"):
        assert command in result.stdout


def test_unimplemented_command_cannot_report_success() -> None:
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 2
    assert "not implemented" in result.stdout
