"""Command-line entry point for Select Fuzz."""

from collections.abc import Callable

import typer

app = typer.Typer(
    name="select-fuzz",
    no_args_is_help=True,
    help="Differential correctness and performance testing for MySQL SELECT queries.",
)


def _pending_command(name: str) -> Callable[[], None]:
    """Build a typed placeholder while each command is wired in later slices."""

    def command() -> None:
        typer.echo(f"{name} is not implemented yet")
        raise typer.Exit(code=2)

    command.__name__ = name
    return command


for _command_name in ("run", "serve", "doctor", "replay", "report", "cleanup"):
    app.command(name=_command_name)(_pending_command(_command_name))
