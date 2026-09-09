from typer.testing import CliRunner

from select_fuzz.pq.cli import _build_config, app


def test_cli_help_exposes_budgets_reference_and_tolerance():
    response = CliRunner().invoke(app, ["--help"], color=False)
    assert response.exit_code == 0
    for option in ("--timeout-seconds", "--duration-seconds", "--reference-host",
                   "--dop-sweep", "--decimal-absolute", "--max-attempts"):
        assert option in response.stdout
    assert "115.120.249.9" not in response.stdout


def test_cli_invalid_mode_fails_before_opening_database(monkeypatch):
    def fail(**kwargs):
        raise AssertionError("must validate before connecting")
    monkeypatch.setattr("mysql.connector.connect", fail)
    response = CliRunner().invoke(app, ["--mode", "invalid"])
    assert response.exit_code == 2


def test_build_config_still_supports_existing_arguments():
    cfg = _build_config(mode="fast", host="127.0.0.1", port=3306, user="root",
                        password="", database="pq_cli_test", queries=10,
                        rows=20, tables=2, seed=4, dop=2, repeats=3)
    assert cfg.dop_on == 2
    assert cfg.queries_per_round == 10


def test_cli_missing_serial_endpoint_fails_before_connecting_or_writing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.delenv("SELECT_FUZZ_SERIAL_HOST", raising=False)
    monkeypatch.setattr("mysql.connector.connect", lambda **kw: calls.append(kw))
    output = tmp_path / "artifacts"
    response = CliRunner().invoke(app, ["--artifacts", str(output)])
    assert response.exit_code == 2
    assert "--serial-host" in response.output
    assert not calls
    assert not output.exists()


def test_cli_serial_credentials_are_independent_and_read_from_environment(monkeypatch, tmp_path):
    from types import SimpleNamespace
    seen = []
    monkeypatch.setenv("SELECT_FUZZ_SERIAL_MYSQL_USER", "serial_test_user")
    monkeypatch.setenv("SELECT_FUZZ_SERIAL_MYSQL_PASSWORD", "serial-private-password")
    monkeypatch.setattr("select_fuzz.pq.fast.run_fast", lambda cfg, **kw: seen.append(cfg) or
                        SimpleNamespace(stats=SimpleNamespace(total_attempts=1, triggered=1,
                            executed=1, matches=1, mismatches=0, errors=0)))
    response = CliRunner().invoke(app, ["--serial-host", "127.0.0.2", "--serial-port", "3307",
                                        "--artifacts", str(tmp_path)])
    assert response.exit_code == 0, response.output
    assert len(seen) == 1
    assert seen[0].serial_endpoint.host == "127.0.0.2"
    assert seen[0].serial_endpoint.port == 3307
    assert seen[0].serial_endpoint.user == "serial_test_user"
    assert seen[0].serial_endpoint.password == "serial-private-password"
    assert "serial-private-password" not in response.output


def test_cli_dop_sweep_fails_before_connecting_or_writing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("mysql.connector.connect", lambda **kw: calls.append(kw))
    output = tmp_path / "artifacts"
    response = CliRunner().invoke(app, ["--mode", "performance", "--serial-host", "127.0.0.2",
        "--dop-sweep", "2,4,8", "--artifacts", str(output)])
    assert response.exit_code == 2
    assert "preconfigured" in response.output
    assert not calls
    assert not output.exists()
