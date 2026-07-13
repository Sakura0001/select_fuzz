from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.doctor import MySQLDoctorProbe, _fetch_all, _permissions


class Cursor:
    def __init__(self, rows: tuple[tuple[object, ...], ...]) -> None:
        self.rows = rows
        self.sent = False
        self.closed = False

    def fetchmany(self, size: int) -> tuple[tuple[object, ...], ...]:
        assert size == 128
        if self.sent:
            return ()
        self.sent = True
        return self.rows

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    def execute(self, sql: str) -> Cursor:
        key = next(key for key in self.responses if sql.startswith(key))
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, tuple)
        return Cursor(value)


class Factory:
    def __init__(self, responses: dict[str, object]) -> None:
        self.session = Session(responses)

    @contextmanager
    def control_session(self, node: NodeConfig, database: str) -> Iterator[Session]:
        assert database == "information_schema"
        yield self.session


def node(*, probe: bool = False) -> NodeConfig:
    return NodeConfig(
        role=NodeRole.BASELINE,
        host="localhost",
        port=3306,
        role_probe_sql="SELECT role" if probe else None,
        role_probe_expected="on" if probe else None,
    )


def responses(
    *,
    version: object = "8.0.41",
    explain: object = (("plan",),),
    grants: object = (("GRANT SELECT, INSERT ON *.* TO user",),),
    role: object = (("on",),),
) -> dict[str, object]:
    return {
        "SELECT VERSION()": ((version, "comment"),),
        "EXPLAIN ANALYZE": explain,
        "SHOW GRANTS": grants,
        "SELECT role": role,
    }


def test_fetch_all_closes_cursor_after_multiple_fetches() -> None:
    cursor = Cursor(((1,), (2,)))

    class DirectSession:
        def execute(self, sql: str) -> Cursor:
            assert sql == "SELECT 1"
            return cursor

    assert _fetch_all(DirectSession(), "SELECT 1") == ((1,), (2,))
    assert cursor.closed


def test_permission_parser_skips_bad_grants_and_handles_all_privileges() -> None:
    assert _permissions(((), (1,), ("not a grant",), ("GRANT SELECT, CREATE ON *.* TO u",))) == {
        "SELECT",
        "CREATE",
    }
    assert _permissions((("GRANT ALL PRIVILEGES ON *.* TO u",),)) >= {"SELECT", "CREATE"}


def test_mysql_probe_collects_version_explain_grants_and_role_match() -> None:
    snapshot = MySQLDoctorProbe(Factory(responses())).probe(node(probe=True))
    assert snapshot.capabilities == {"mysql_8_0_41", "explain_analyze"}
    assert snapshot.permissions == {"SELECT", "INSERT"}
    assert snapshot.role_probe_matches is True


def test_mysql_probe_degrades_failed_optional_checks_without_hiding_node() -> None:
    snapshot = MySQLDoctorProbe(
        Factory(
            responses(
                version=b"8.0.41",
                explain=RuntimeError("unsupported"),
                grants=RuntimeError("denied"),
                role=RuntimeError("denied"),
            )
        )
    ).probe(node(probe=True))
    assert snapshot.capabilities == set()
    assert snapshot.permissions == set()
    assert snapshot.role_probe_matches is False


def test_mysql_probe_supports_no_role_probe_and_false_role_value() -> None:
    no_probe = MySQLDoctorProbe(Factory(responses(version="8.0.40"))).probe(node())
    assert no_probe.role_probe_matches is None
    mismatch = MySQLDoctorProbe(Factory(responses(role=(("off",),)))).probe(node(probe=True))
    assert mismatch.role_probe_matches is False


def test_mysql_probe_requires_exactly_one_nonempty_fingerprint_row() -> None:
    empty = responses()
    empty["SELECT VERSION()"] = ()
    with pytest.raises(RuntimeError, match="fingerprint probe"):
        MySQLDoctorProbe(Factory(empty)).probe(node())

    multiple = responses()
    multiple["SELECT VERSION()"] = (("8.0.41",), ("8.0.41",))
    with pytest.raises(RuntimeError, match="fingerprint probe"):
        MySQLDoctorProbe(Factory(multiple)).probe(node())
