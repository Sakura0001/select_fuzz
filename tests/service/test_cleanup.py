from __future__ import annotations

from dataclasses import dataclass

import pytest

from select_fuzz.cleanup import CleanupService, ManagedDatabaseError, build_cleanup_service
from select_fuzz.config import AppConfig, NodeConfig, NodeRole


MANAGED = "sf_c_20260713t112233_w0_r1_s0123456789_nabcdef12_q0"


def _nodes() -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(role=role, host=f"{role.value}.test", port=3306 + ordinal)
        for ordinal, role in enumerate(NodeRole)
    )


@dataclass
class _Cursor:
    statements: list[str]

    def execute(self, sql: str) -> None:
        self.statements.append(sql)

    def close(self) -> None:
        return None


@dataclass
class _Connection:
    statements: list[str]

    def cursor(self) -> _Cursor:
        return _Cursor(self.statements)

    def close(self) -> None:
        return None


def test_cleanup_rejects_non_managed_name_before_connecting() -> None:
    connected = False

    def connect(node: NodeConfig) -> _Connection:
        nonlocal connected
        connected = True
        return _Connection([])

    with pytest.raises(ManagedDatabaseError):
        CleanupService(_nodes(), connect).run(("production",), execute=True)
    assert connected is False


def test_cleanup_is_dry_run_by_default_and_drops_explicit_name_on_all_nodes() -> None:
    statements: dict[NodeRole, list[str]] = {role: [] for role in NodeRole}

    def connect(node: NodeConfig) -> _Connection:
        return _Connection(statements[node.role])

    service = CleanupService(_nodes(), connect)
    planned = service.run((MANAGED,), execute=False)
    assert planned.execute is False
    assert all(not item.dropped for item in planned.nodes)
    assert all(not values for values in statements.values())

    executed = service.run((MANAGED,), execute=True)
    assert executed.success
    assert all(item.dropped for item in executed.nodes)
    assert all(values == [f"DROP DATABASE `{MANAGED}`"] for values in statements.values())


def test_cleanup_reports_sanitized_partial_node_failure() -> None:
    def connect(node: NodeConfig) -> _Connection:
        if node.role is NodeRole.CUSTOM_OFF:
            raise RuntimeError("secret host detail")
        return _Connection([])

    report = CleanupService(_nodes(), connect).run((MANAGED,), execute=True)
    assert not report.success
    failed = next(item for item in report.nodes if item.role is NodeRole.CUSTOM_OFF)
    assert failed.error_type == "RuntimeError"
    assert "secret" not in repr(report)


def test_cleanup_requires_a_name_and_deduplicates_explicit_ids() -> None:
    service = CleanupService(_nodes(), lambda node: _Connection([]))
    with pytest.raises(ManagedDatabaseError, match="at least one"):
        service.run((), execute=False)
    report = service.run((MANAGED, MANAGED), execute=False)
    assert report.databases == (MANAGED,)
    assert len(report.nodes) == 3


def test_production_cleanup_builder_resolves_credentials_without_persisting_them() -> None:
    calls: list[dict[str, object]] = []

    def connect(**kwargs: object) -> _Connection:
        calls.append(kwargs)
        return _Connection([])

    config = AppConfig(nodes=_nodes())
    service = build_cleanup_service(
        config,
        environ={
            "SELECT_FUZZ_MYSQL_USER": "local-user",
            "SELECT_FUZZ_MYSQL_PASSWORD": "ephemeral-secret",
        },
        connect=connect,
    )
    report = service.run((MANAGED,), execute=True)

    assert report.success
    assert len(calls) == 3
    assert calls[0]["user"] == "local-user"
    assert calls[0]["password"] == "ephemeral-secret"
    assert "ephemeral-secret" not in repr(report)
