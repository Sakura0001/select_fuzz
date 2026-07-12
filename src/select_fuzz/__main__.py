"""Allow ``python -m select_fuzz`` execution."""

from select_fuzz.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
