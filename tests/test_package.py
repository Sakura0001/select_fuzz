from typer.testing import CliRunner

from select_fuzz import __version__
from select_fuzz.cli import app


def test_package_and_cli_are_importable() -> None:
    assert __version__ == "0.1.0"
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "serve", "doctor", "replay", "report", "cleanup"):
        assert command in result.stdout


def test_run_command_requires_an_explicit_config_file() -> None:
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 2
    assert "Missing option '--config'" in result.output
