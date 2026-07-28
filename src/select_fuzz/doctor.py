"""Three-node connectivity, version, capability, permission, and role probes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
from typing import Protocol

from select_fuzz.config import (
    AppConfig,
    NodeConfig,
    NodePreflight,
    NodeRole,
    PreflightIssue,
    PreflightReport,
    evaluate_preflight,
)
from select_fuzz.domain import stable_fingerprint
from select_fuzz.execution import ConnectionFactory, MySQLConnectorFactory, QuerySession


REQUIRED_CORRECTNESS_CAPABILITIES = frozenset({"explain_analyze"})
REQUIRED_CORRECTNESS_PERMISSIONS = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE"}
)


class NodeDoctorProbe(Protocol):
    def probe(self, node: NodeConfig) -> NodePreflight: ...


def _fetch_all(session: QuerySession, sql: str) -> tuple[tuple[object, ...], ...]:
    cursor = session.execute(sql)
    rows: list[tuple[object, ...]] = []
    try:
        while True:
            batch = cursor.fetchmany(128)
            if not batch:
                break
            rows.extend(tuple(row) for row in batch)
    finally:
        cursor.close()
    return tuple(rows)


def _permissions(grant_rows: tuple[tuple[object, ...], ...]) -> frozenset[str]:
    permissions: set[str] = set()
    for row in grant_rows:
        if not row or not isinstance(row[0], str):
            continue
        grant = row[0].upper()
        if "ALL PRIVILEGES" in grant:
            return REQUIRED_CORRECTNESS_PERMISSIONS
        match = re.match(r"GRANT\s+(.+?)\s+ON\s+", grant)
        if match is None:
            continue
        permissions.update(part.strip() for part in match.group(1).split(","))
    return frozenset(permissions)


class MySQLDoctorProbe:
    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    def probe(self, node: NodeConfig) -> NodePreflight:
        with self._factory.control_session(node, "information_schema") as session:
            fingerprint_rows = _fetch_all(
                session,
                "SELECT VERSION(), @@version_comment, @@sql_mode, @@optimizer_switch, "
                "@@transaction_isolation, @@character_set_server, @@collation_server, "
                "@@innodb_page_size, @@lower_case_table_names",
            )
            if len(fingerprint_rows) != 1 or not fingerprint_rows[0]:
                raise RuntimeError("fingerprint probe returned no row")
            version = fingerprint_rows[0][0]
            capabilities: set[str] = set()
            if isinstance(version, str) and version.startswith("8.0.41"):
                capabilities.add("mysql_8_0_41")
            try:
                _fetch_all(session, "EXPLAIN ANALYZE SELECT 1")
            except Exception:
                pass
            else:
                capabilities.add("explain_analyze")
            try:
                grants = _fetch_all(session, "SHOW GRANTS")
            except Exception:
                grants = ()
            role_probe_matches: bool | None = None
            if node.role_probe_sql is not None:
                try:
                    role_rows = _fetch_all(session, node.role_probe_sql)
                    role_probe_matches = bool(
                        role_rows
                        and role_rows[0]
                        and str(role_rows[0][0]) == node.role_probe_expected
                    )
                except Exception:
                    role_probe_matches = False
            return NodePreflight(
                role=node.role,
                config_fingerprint=stable_fingerprint(fingerprint_rows[0]),
                capabilities=frozenset(capabilities),
                permissions=_permissions(grants),
                role_probe_matches=role_probe_matches,
                server_version=version if isinstance(version, str) else None,
            )


class DoctorService:
    def __init__(self, config: AppConfig, probe: NodeDoctorProbe) -> None:
        self._config = config
        self._probe = probe

    def run(self) -> PreflightReport:
        snapshots: list[NodePreflight] = []
        replica_snapshots: list[NodePreflight] = []
        failures: list[PreflightIssue] = []
        roles = (
            (self._config.fuzz.target_role,)
            if self._config.mode.value == "fuzz"
            else tuple(NodeRole)
        )
        jobs: dict[tuple[NodeRole, str], NodeConfig] = {
            (role, "primary"): self._config.node_for(role) for role in roles
        }
        for role in roles:
            primary = self._config.node_for(role)
            replica = self._config.replica_for(role)
            if (primary.host.casefold(), primary.port) != (
                replica.host.casefold(),
                replica.port,
            ):
                jobs[(role, "replica")] = replica
        with ThreadPoolExecutor(
            max_workers=len(jobs), thread_name_prefix="sf-doctor"
        ) as pool:
            futures = {
                identity: pool.submit(self._probe.probe, node)
                for identity, node in jobs.items()
            }
            for (role, endpoint_kind), future in futures.items():
                try:
                    snapshot = future.result()
                    if snapshot.role is not role:
                        raise ValueError("probe returned the wrong role")
                    target = snapshots if endpoint_kind == "primary" else replica_snapshots
                    target.append(snapshot)
                    if (
                        self._config.mode.value == "fuzz"
                        and endpoint_kind == "primary"
                        and self._config.node_for(role).host.casefold()
                        == self._config.replica_for(role).host.casefold()
                        and self._config.node_for(role).port
                        == self._config.replica_for(role).port
                    ):
                        # A routing proxy represents both logical sides with
                        # one endpoint. Reuse the probe for the replica
                        # permission check instead of opening a duplicate job.
                        replica_snapshots.append(snapshot)
                except Exception as error:
                    failures.append(
                        PreflightIssue(
                            code="node_unavailable",
                            message=(
                                f"Node {role.value} {endpoint_kind} probe failed: "
                                f"{type(error).__name__}"
                            ),
                            role=role,
                        )
                    )
        evaluated = evaluate_preflight(
            tuple(snapshots),
            required_capabilities=frozenset(),
            required_permissions=REQUIRED_CORRECTNESS_PERMISSIONS,
        )
        replica_evaluated = (
            evaluate_preflight(
                tuple(replica_snapshots),
                required_capabilities=(
                    REQUIRED_CORRECTNESS_CAPABILITIES
                    if self._config.mode.value == "performance"
                    else frozenset()
                ),
                required_permissions=frozenset({"SELECT"}),
            )
            if replica_snapshots
            else PreflightReport()
        )
        selected_roles = set(roles)

        def retain_selected_role_observations(
            report: PreflightReport,
        ) -> tuple[PreflightIssue, ...]:
            return tuple(
                issue
                for issue in report.fatals
                if issue.code != "missing_node_observation"
                or issue.role in selected_roles
            )

        failed_roles = {issue.role for issue in failures}
        retained_fatals = tuple(
            issue
            for issue in (
                *retain_selected_role_observations(evaluated),
                *retain_selected_role_observations(replica_evaluated),
            )
            if not (
                issue.code == "missing_node_observation" and issue.role in failed_roles
            )
        )
        versions = {
            snapshot.server_version
            for snapshot in (*snapshots, *replica_snapshots)
            if snapshot.server_version is not None
        }
        version_warnings = (
            (
                PreflightIssue(
                    code="version_mismatch",
                    message="MySQL versions differ across configured primary/replica endpoints",
                ),
            )
            if len(versions) > 1
            else ()
        )
        warnings = tuple(
            {
                (issue.code, issue.role, issue.message): issue
                for issue in (
                    *evaluated.warnings,
                    *replica_evaluated.warnings,
                    *version_warnings,
                )
            }.values()
        )
        return PreflightReport(
            warnings=warnings,
            fatals=(*failures, *retained_fatals),
        )


def build_doctor(config: AppConfig) -> DoctorService:
    return DoctorService(config, MySQLDoctorProbe(MySQLConnectorFactory()))


__all__ = [
    "DoctorService",
    "MySQLDoctorProbe",
    "NodeDoctorProbe",
    "REQUIRED_CORRECTNESS_CAPABILITIES",
    "REQUIRED_CORRECTNESS_PERMISSIONS",
    "build_doctor",
]
