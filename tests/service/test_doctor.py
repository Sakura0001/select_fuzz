from __future__ import annotations

from select_fuzz.config import AppConfig, NodeConfig, NodePreflight, NodeRole
from select_fuzz.doctor import DoctorService


def _nodes() -> tuple[NodeConfig, ...]:
    return tuple(
        NodeConfig(role=role, host="127.0.0.1", port=33061 + index)
        for index, role in enumerate(NodeRole)
    )


def _config() -> AppConfig:
    return AppConfig(nodes=_nodes())


class _Probe:
    def __init__(self, snapshots: dict[NodeRole, NodePreflight]) -> None:
        self.snapshots = snapshots

    def probe(self, node: NodeConfig) -> NodePreflight:
        return self.snapshots[node.role]


def _snapshot(role: NodeRole, *, fingerprint: str = "same") -> NodePreflight:
    return NodePreflight(
        role=role,
        config_fingerprint=fingerprint,
        capabilities=frozenset({"mysql_8_0_41", "explain_analyze"}),
        permissions=frozenset(
            {
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "CREATE",
                "CREATE TEMPORARY TABLES",
            }
        ),
        role_probe_matches=None,
    )


def test_doctor_allows_configuration_and_missing_role_probe_warnings() -> None:
    snapshots = {role: _snapshot(role) for role in NodeRole}

    report = DoctorService(_config(), _Probe(snapshots)).run()

    assert report.can_start is True
    assert {issue.code for issue in report.warnings} == {"role_probe_missing"}


def test_doctor_configuration_difference_is_warning_not_fatal() -> None:
    snapshots = {
        role: _snapshot(role, fingerprint=f"fp-{role.value}") for role in NodeRole
    }

    report = DoctorService(_config(), _Probe(snapshots)).run()

    assert report.can_start is True
    assert "configuration_difference" in {issue.code for issue in report.warnings}


def test_doctor_does_not_gate_exact_version_but_missing_permission_is_fatal() -> None:
    snapshots = {role: _snapshot(role) for role in NodeRole}
    snapshots[NodeRole.CUSTOM_ON] = NodePreflight(
        role=NodeRole.CUSTOM_ON,
        config_fingerprint="same",
        capabilities=frozenset({"explain_analyze"}),
        permissions=frozenset({"SELECT"}),
    )

    report = DoctorService(_config(), _Probe(snapshots)).run()

    assert report.can_start is False
    assert {issue.code for issue in report.fatals} == {"missing_permission"}


def test_doctor_sanitizes_probe_exception_as_node_unavailable() -> None:
    class BrokenProbe:
        def probe(self, node: NodeConfig) -> NodePreflight:
            if node.role is NodeRole.CUSTOM_OFF:
                raise RuntimeError("password=must-not-leak")
            return _snapshot(node.role)

    report = DoctorService(_config(), BrokenProbe()).run()

    assert report.can_start is False
    issue = next(issue for issue in report.fatals if issue.code == "node_unavailable")
    assert issue.role is NodeRole.CUSTOM_OFF
    assert "password" not in issue.message


def test_doctor_probes_all_six_distinct_endpoints_and_only_warns_on_version_mismatch() -> None:
    config = AppConfig(
        nodes=tuple(
            {
                "role": role,
                "primary": {"host": "primary.example", "port": 33061 + index},
                "replica": {"host": "replica.example", "port": 33161 + index},
            }
            for index, role in enumerate(NodeRole)
        )
    )

    class SixProbe:
        def __init__(self) -> None:
            self.ports: list[int] = []

        def probe(self, node: NodeConfig) -> NodePreflight:
            self.ports.append(node.port)
            return NodePreflight(
                role=node.role,
                config_fingerprint=f"fp-{node.port}",
                capabilities={"explain_analyze"},
                permissions={
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "CREATE",
                    "CREATE TEMPORARY TABLES",
                },
                server_version="8.0.40" if node.port < 33100 else "8.4.0",
            )

    probe = SixProbe()
    report = DoctorService(config, probe).run()

    assert len(probe.ports) == 6
    assert report.can_start is True
    assert "version_mismatch" in {issue.code for issue in report.warnings}
