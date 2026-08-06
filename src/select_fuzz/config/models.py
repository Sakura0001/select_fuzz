"""Pydantic contracts for the fixed three-node test topology."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    """Reject misspelled settings instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class RunMode(StrEnum):
    """The independently packaged test modes."""

    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    FUZZ = "fuzz"


MAX_STATEMENT_TIMEOUT_SECONDS = 300.0
MAX_FUZZ_READER_WORKERS = 256


class NodeRole(StrEnum):
    """The only server roles accepted by the differential runner."""

    BASELINE = "baseline"
    CUSTOM_OFF = "custom_off"
    CUSTOM_ON = "custom_on"


_ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


class ServerEndpointConfig(StrictModel):
    """One secret-safe MySQL endpoint without a differential role."""

    host: str = Field(min_length=1)
    port: int = Field(default=3306, ge=1, le=65535)
    username_env: str = Field(default="SELECT_FUZZ_MYSQL_USER", pattern=_ENV_NAME_PATTERN)
    password_env: str = Field(default="SELECT_FUZZ_MYSQL_PASSWORD", pattern=_ENV_NAME_PATTERN)
    role_probe_sql: str | None = None
    role_probe_expected: str | None = None

    @field_validator("host")
    @classmethod
    def strip_host(cls, value: str) -> str:
        host = value.strip()
        if not host:
            raise ValueError("host must not be blank")
        return host

    @model_validator(mode="after")
    def require_complete_role_probe(self) -> Self:
        if (self.role_probe_sql is None) != (self.role_probe_expected is None):
            raise ValueError("role_probe_sql and role_probe_expected must be set together")
        return self


class NodeConfig(ServerEndpointConfig):
    """A concrete endpoint bound to one differential role."""

    role: NodeRole


class NodeTopologyConfig(StrictModel):
    """The independently addressable primary and replica for one role."""

    role: NodeRole
    primary: ServerEndpointConfig
    replica: ServerEndpointConfig
    legacy_single_endpoint: bool = Field(default=False, exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_single_endpoint(cls, value: object) -> object:
        """Keep old programmatic/tests configs usable while preferring six endpoints."""

        if isinstance(value, NodeConfig):
            endpoint = value.model_dump(exclude={"role"})
            return {
                "role": value.role,
                "primary": endpoint,
                "replica": endpoint,
                "legacy_single_endpoint": True,
            }
        if isinstance(value, Mapping) and "primary" not in value and "replica" not in value:
            raw = dict(value)
            if "role" not in raw:
                return value
            role = raw.pop("role")
            return {
                "role": role,
                "primary": raw,
                "replica": raw,
                "legacy_single_endpoint": True,
            }
        return value

    def primary_node(self) -> NodeConfig:
        return NodeConfig(role=self.role, **self.primary.model_dump())

    def replica_node(self) -> NodeConfig:
        return NodeConfig(role=self.role, **self.replica.model_dump())


SessionVariableValue = bool | int | float | str
_SESSION_VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ReplicaSessionConfig(StrictModel):
    """SET SESSION values applied only to newly opened replica sessions."""

    session_variables: dict[str, SessionVariableValue] = Field(default_factory=dict)

    @field_validator("session_variables", mode="before")
    @classmethod
    def validate_session_variables(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("session_variables must be a mapping")
        for name, variable_value in value.items():
            if not isinstance(name, str) or _SESSION_VARIABLE_PATTERN.fullmatch(name) is None:
                raise ValueError("session variable names must be safe identifiers")
            if not isinstance(variable_value, (bool, int, float, str)):
                raise ValueError("session variable values must be scalar")
            if isinstance(variable_value, float) and not math.isfinite(variable_value):
                raise ValueError("session variable float values must be finite")
        return dict(value)


def _default_replica_sessions() -> dict[NodeRole, ReplicaSessionConfig]:
    return {role: ReplicaSessionConfig() for role in NodeRole}


class ReplicaParametersConfig(StrictModel):
    """Versioned, non-secret session parameters for all three replicas."""

    version: Literal[1] = 1
    replicas: dict[NodeRole, ReplicaSessionConfig] = Field(
        default_factory=_default_replica_sessions
    )

    @model_validator(mode="after")
    def require_all_roles(self) -> Self:
        if set(self.replicas) != set(NodeRole):
            raise ValueError(
                "replicas must contain baseline, custom_off, and custom_on exactly once"
            )
        return self

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(payload).hexdigest()


class CorrectnessConfig(StrictModel):
    """Defaults and safety ceilings for result differential testing."""

    workers: int = Field(default=10, ge=1, le=64)
    queries_per_round: int = Field(default=1000, ge=1)
    timeout_seconds: float = Field(default=15.0, gt=0, le=MAX_STATEMENT_TIMEOUT_SECONDS)
    row_limit: int = Field(default=10_000, ge=1)
    byte_limit: int = Field(default=32 * 1024 * 1024, ge=1)
    min_rows_per_table: int = Field(default=10, ge=1)
    max_rows_per_table: int = Field(default=500, ge=1)
    min_tables: int = Field(default=1, ge=1, le=64)
    max_tables: int = Field(default=8, ge=1, le=64)
    min_columns: int = Field(default=2, ge=2, le=1017)
    max_columns: int = Field(default=16, ge=2, le=1017)
    max_indexes_per_table: int = Field(default=8, ge=1, le=65)
    max_query_tables: int = Field(default=4, ge=1, le=64)
    query_grammar_path: str | None = None
    grammar_compatible_type_percent: int = Field(default=80, ge=0, le=100)
    explain_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=MAX_STATEMENT_TIMEOUT_SECONDS,
    )

    @model_validator(mode="after")
    def validate_correctness_bounds(self) -> Self:
        if self.min_rows_per_table > self.max_rows_per_table:
            raise ValueError("min_rows_per_table must not exceed max_rows_per_table")
        if self.min_tables > self.max_tables:
            raise ValueError("min_tables must not exceed max_tables")
        if self.min_columns > self.max_columns:
            raise ValueError("min_columns must not exceed max_columns")
        if self.max_query_tables > self.max_tables:
            raise ValueError("max_query_tables must not exceed max_tables")
        return self


class PerformanceConfig(StrictModel):
    """Calibration and alert policy for the single-worker performance lane."""

    workers: Literal[1] = 1
    queries_per_round: int = Field(default=100, ge=1)
    initial_table_rows: int = Field(default=100_000, ge=1)
    initial_table_rows_max: int = Field(default=1_000_000, ge=1)
    max_table_rows: int = Field(default=50_000_000, ge=1)
    max_total_rows: int = Field(default=100_000_000, ge=1)
    insert_batch_rows: int = Field(default=10_000, ge=1, le=1_000_000)
    min_tables: int = Field(default=1, ge=1, le=16)
    max_tables: int = Field(default=4, ge=1, le=16)
    min_columns: int = Field(default=3, ge=2, le=1017)
    max_columns: int = Field(default=10, ge=2, le=1017)
    max_indexes_per_table: int = Field(default=6, ge=1, le=65)
    max_query_tables: int = Field(default=4, ge=1, le=16)
    max_query_depth: int = Field(default=3, ge=1, le=16)
    # Accepted for compatibility with pre-shared-round configuration files.
    # Production performance runs no longer perform per-query scale calibration.
    max_calibration_rounds: int = Field(default=8, ge=1)
    calibration_runs_per_reference: Literal[3] = 3
    calibration_min_seconds: float = Field(default=5.0, gt=0)
    calibration_max_seconds: float = Field(default=12.0, gt=0)
    formal_timeout_seconds: float = Field(default=15.0, gt=0, le=MAX_STATEMENT_TIMEOUT_SECONDS)
    materialization_timeout_seconds: float = Field(
        default=300.0, gt=0, le=MAX_STATEMENT_TIMEOUT_SECONDS
    )
    regression_threshold: float = Field(default=0.20, ge=0)
    max_start_skew_ms: float = Field(default=100.0, ge=0)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.initial_table_rows > self.initial_table_rows_max:
            raise ValueError("initial_table_rows must not exceed initial_table_rows_max")
        if self.initial_table_rows > self.max_table_rows:
            raise ValueError("initial_table_rows must not exceed max_table_rows")
        if self.max_table_rows > self.max_total_rows:
            raise ValueError("max_table_rows must not exceed max_total_rows")
        if self.min_tables > self.max_tables:
            raise ValueError("min_tables must not exceed max_tables")
        if self.min_columns > self.max_columns:
            raise ValueError("min_columns must not exceed max_columns")
        if self.max_query_tables > self.max_tables:
            raise ValueError("max_query_tables must not exceed max_tables")
        if self.initial_table_rows * self.max_tables > self.max_total_rows:
            raise ValueError("initial_table_rows times max_tables must not exceed max_total_rows")
        if self.calibration_min_seconds > self.calibration_max_seconds:
            raise ValueError("calibration_min_seconds must not exceed calibration_max_seconds")
        return self


class FuzzConfig(StrictModel):
    """Bounded concurrent read/write fuzzing on one configured topology."""

    target_role: NodeRole = NodeRole.CUSTOM_ON
    databases: int = Field(default=1, ge=1, le=32)
    writer_threads_per_database: int = Field(default=2, ge=1, le=64)
    reader_threads_per_database: int = Field(default=6, ge=3, le=192)
    max_total_connections: int = Field(default=1024, ge=1, le=4096)
    initial_tables: int = Field(default=4, ge=1, le=16)
    initial_rows_per_table: int = Field(default=10_000, ge=20)
    max_rows_per_database: int = Field(default=10_000_000, ge=100)
    min_columns_per_table: int = Field(default=200, ge=50, le=500)
    max_columns_per_table: int = Field(default=500, ge=50, le=500)
    min_indexes_per_table: int = Field(default=4, ge=4, le=64)
    max_indexes_per_table: int = Field(default=12, ge=4, le=64)
    query_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=MAX_STATEMENT_TIMEOUT_SECONDS,
    )
    batch_rows_min: int = Field(default=100, ge=1)
    batch_rows_max: int = Field(default=100_000, ge=1, le=1_000_000)
    delete_batch_rows_min: int = Field(default=10, ge=1, le=100)
    delete_batch_rows_max: int = Field(default=100, ge=1, le=100)
    insert_weight: int = Field(default=35, ge=0, le=100)
    update_weight: int = Field(default=45, ge=0, le=100)
    delete_weight: int = Field(default=10, ge=0, le=100)
    upsert_weight: int = Field(default=10, ge=0, le=100)
    grammar_query_weight: Literal[50] = 50
    load_shaped_query_weight: Literal[50] = 50
    query_generator_processes: int = Field(default=0, ge=0, le=32)
    schema_refresh_interval_seconds: float = Field(default=1800.0, ge=0)
    connector_implementation: Literal["auto", "c", "python"] = "auto"
    control_connection_reserve: int = Field(default=8, ge=1, le=128)
    query_kill_grace_seconds: float = Field(default=1.0, gt=0, le=10)
    reconnect_initial_delay_seconds: float = Field(default=0.25, gt=0, le=30)
    reconnect_max_delay_seconds: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_fuzz_bounds(self) -> Self:
        if self.reader_threads_per_database % 3 != 0:
            raise ValueError("reader_threads_per_database must be divisible by 3")
        total_reader_workers = self.databases * self.reader_threads_per_database
        if total_reader_workers > MAX_FUZZ_READER_WORKERS:
            raise ValueError(
                "fuzz supports at most "
                f"{MAX_FUZZ_READER_WORKERS} total reader workers; "
                f"configured {total_reader_workers}"
            )
        if self.batch_rows_min > self.batch_rows_max:
            raise ValueError("batch_rows_min must not exceed batch_rows_max")
        if self.delete_batch_rows_min > self.delete_batch_rows_max:
            raise ValueError(
                "delete_batch_rows_min must not exceed delete_batch_rows_max"
            )
        if self.initial_rows_per_table * self.initial_tables > self.max_rows_per_database:
            raise ValueError(
                "initial_rows_per_table times initial_tables must not exceed "
                "max_rows_per_database"
            )
        if self.min_columns_per_table > self.max_columns_per_table:
            raise ValueError(
                "min_columns_per_table must not exceed max_columns_per_table"
            )
        if self.min_indexes_per_table > self.max_indexes_per_table:
            raise ValueError(
                "min_indexes_per_table must not exceed max_indexes_per_table"
            )
        if (
            self.insert_weight
            + self.update_weight
            + self.delete_weight
            + self.upsert_weight
            != 100
        ):
            raise ValueError("fuzz DML weights must sum to 100")
        if self.reconnect_initial_delay_seconds > self.reconnect_max_delay_seconds:
            raise ValueError(
                "reconnect_initial_delay_seconds must not exceed "
                "reconnect_max_delay_seconds"
            )
        return self


class AppConfig(StrictModel):
    """Top-level configuration with one node for every fixed role."""

    mode: RunMode = RunMode.CORRECTNESS
    nodes: tuple[NodeTopologyConfig, ...]
    replica_parameters_file: Path | None = None
    replica_parameters: ReplicaParametersConfig = Field(
        default_factory=ReplicaParametersConfig,
        exclude=True,
    )
    replica_sync_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    full_thread_sql_log: bool = False
    correctness: CorrectnessConfig = Field(default_factory=CorrectnessConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    fuzz: FuzzConfig = Field(default_factory=FuzzConfig)

    @model_validator(mode="after")
    def require_fixed_unique_topology(self) -> Self:
        if len(self.nodes) != len(NodeRole):
            raise ValueError("nodes must contain exactly three entries")
        roles = [node.role for node in self.nodes]
        if set(roles) != set(NodeRole) or len(roles) != len(set(roles)):
            raise ValueError("nodes must contain baseline, custom_off, and custom_on exactly once")

        # Fuzz mode deliberately targets one logical cluster through a routing
        # proxy.  The proxy can expose the same host/port for both the primary
        # and replica connection, and the other two fixed role entries are not
        # used by the fuzz runner.  Differential modes retain the strict
        # six-endpoint isolation contract below.
        if self.mode is RunMode.FUZZ:
            return self

        if any(
            not node.legacy_single_endpoint
            and (node.primary.host.casefold(), node.primary.port)
            == (node.replica.host.casefold(), node.replica.port)
            for node in self.nodes
        ):
            raise ValueError("explicit primary and replica endpoints must differ")
        endpoints_by_role = {
            (node.role, endpoint.host.casefold(), endpoint.port)
            for node in self.nodes
            for endpoint in (node.primary, node.replica)
        }
        endpoints = [(host, port) for _role, host, port in endpoints_by_role]
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("node host/port endpoints must be unique")
        return self

    def node_for(self, role: NodeRole) -> NodeConfig:
        """Return the primary endpoint for backwards-compatible callers."""

        return next(node.primary_node() for node in self.nodes if node.role is role)

    def replica_for(self, role: NodeRole) -> NodeConfig:
        return next(node.replica_node() for node in self.nodes if node.role is role)

    @property
    def primary_nodes(self) -> tuple[NodeConfig, ...]:
        return tuple(self.node_for(role) for role in NodeRole)

    @property
    def replica_nodes(self) -> tuple[NodeConfig, ...]:
        return tuple(self.replica_for(role) for role in NodeRole)

    def replica_session_variables(self, role: NodeRole) -> dict[str, SessionVariableValue]:
        return dict(self.replica_parameters.replicas[role].session_variables)

    @property
    def replica_parameters_sha256(self) -> str:
        return self.replica_parameters.sha256


class ResolvedCredentials(StrictModel):
    """Short-lived connector inputs whose values redact in every model rendering."""

    username: SecretStr
    password: SecretStr


class NodePreflight(StrictModel):
    """Sanitized facts collected by doctor probes for one node."""

    role: NodeRole
    config_fingerprint: str | None = None
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    role_probe_matches: bool | None = None
    server_version: str | None = None
    max_connections: int | None = Field(default=None, ge=1)


class PreflightIssue(StrictModel):
    """A stable policy result safe to publish in events and reports."""

    code: str
    message: str
    role: NodeRole | None = None


class PreflightReport(StrictModel):
    """Warnings permit startup; fatal issues do not."""

    warnings: tuple[PreflightIssue, ...] = ()
    fatals: tuple[PreflightIssue, ...] = ()

    @property
    def can_start(self) -> bool:
        return not self.fatals


def evaluate_preflight(
    snapshots: tuple[NodePreflight, ...],
    *,
    required_capabilities: set[str] | frozenset[str] = frozenset(),
    required_permissions: set[str] | frozenset[str] = frozenset(),
) -> PreflightReport:
    """Classify topology differences as warnings and missing access as fatal."""

    warnings: list[PreflightIssue] = []
    fatals: list[PreflightIssue] = []

    observed_roles = [snapshot.role for snapshot in snapshots]
    by_role = {snapshot.role: snapshot for snapshot in snapshots}
    for missing_role in sorted(set(NodeRole) - set(by_role), key=str):
        fatals.append(
            PreflightIssue(
                code="missing_node_observation",
                message=f"No preflight observation for role {missing_role.value}",
                role=missing_role,
            )
        )
    for duplicate_role in sorted(set(observed_roles), key=str):
        if observed_roles.count(duplicate_role) > 1:
            fatals.append(
                PreflightIssue(
                    code="duplicate_node_observation",
                    message=f"Multiple preflight observations for role {duplicate_role.value}",
                    role=duplicate_role,
                )
            )

    fingerprints = {
        snapshot.config_fingerprint
        for snapshot in snapshots
        if snapshot.config_fingerprint is not None
    }
    if len(fingerprints) > 1:
        warnings.append(
            PreflightIssue(
                code="configuration_difference",
                message="Server configuration fingerprints differ across roles",
            )
        )

    for snapshot in snapshots:
        if snapshot.config_fingerprint is None:
            warnings.append(
                PreflightIssue(
                    code="configuration_fingerprint_missing",
                    message=f"Configuration fingerprint is missing for {snapshot.role.value}",
                    role=snapshot.role,
                )
            )
        if snapshot.role_probe_matches is None:
            warnings.append(
                PreflightIssue(
                    code="role_probe_missing",
                    message=f"Role probe is not configured for {snapshot.role.value}",
                    role=snapshot.role,
                )
            )
        elif not snapshot.role_probe_matches:
            warnings.append(
                PreflightIssue(
                    code="role_probe_mismatch",
                    message=f"Role probe did not match for {snapshot.role.value}",
                    role=snapshot.role,
                )
            )

        missing_capabilities = sorted(required_capabilities - snapshot.capabilities)
        if missing_capabilities:
            fatals.append(
                PreflightIssue(
                    code="missing_capability",
                    message=(
                        f"{snapshot.role.value} lacks required capabilities: "
                        f"{', '.join(missing_capabilities)}"
                    ),
                    role=snapshot.role,
                )
            )

        missing_permissions = sorted(required_permissions - snapshot.permissions)
        if missing_permissions:
            fatals.append(
                PreflightIssue(
                    code="missing_permission",
                    message=(
                        f"{snapshot.role.value} lacks required permissions: "
                        f"{', '.join(missing_permissions)}"
                    ),
                    role=snapshot.role,
                )
            )

    return PreflightReport(warnings=tuple(warnings), fatals=tuple(fatals))
