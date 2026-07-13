"""Pydantic contracts for the fixed three-node test topology."""

from __future__ import annotations

from enum import StrEnum
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
    """The two intentionally separate test modes."""

    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"


MAX_STATEMENT_TIMEOUT_SECONDS = 300.0


class NodeRole(StrEnum):
    """The only server roles accepted by the differential runner."""

    BASELINE = "baseline"
    CUSTOM_OFF = "custom_off"
    CUSTOM_ON = "custom_on"


_ENV_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


class NodeConfig(StrictModel):
    """Connection metadata containing references to credentials, never credentials."""

    role: NodeRole
    host: str = Field(min_length=1)
    port: int = Field(default=3306, ge=1, le=65535)
    username_env: str = Field(
        default="SELECT_FUZZ_MYSQL_USER", pattern=_ENV_NAME_PATTERN
    )
    password_env: str = Field(
        default="SELECT_FUZZ_MYSQL_PASSWORD", pattern=_ENV_NAME_PATTERN
    )
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


class CorrectnessConfig(StrictModel):
    """Defaults and safety ceilings for result differential testing."""

    workers: int = Field(default=10, ge=1, le=64)
    queries_per_round: int = Field(default=1000, ge=1)
    timeout_seconds: float = Field(
        default=15.0, gt=0, le=MAX_STATEMENT_TIMEOUT_SECONDS
    )
    row_limit: int = Field(default=10_000, ge=1)
    byte_limit: int = Field(default=32 * 1024 * 1024, ge=1)
    min_rows_per_table: int = Field(default=10, ge=1)
    max_rows_per_table: int = Field(default=500, ge=1)
    free_random_rate: float = Field(default=0.05, ge=0, le=1)
    negative_mutation_rate: float = Field(default=0.05, ge=0, le=1)

    @model_validator(mode="after")
    def reserve_space_for_coverage_driven_queries(self) -> Self:
        if self.free_random_rate + self.negative_mutation_rate > 1:
            raise ValueError("free_random_rate and negative_mutation_rate must sum to at most 1")
        if self.min_rows_per_table > self.max_rows_per_table:
            raise ValueError(
                "min_rows_per_table must not exceed max_rows_per_table"
            )
        return self


class PerformanceConfig(StrictModel):
    """Calibration and alert policy for the single-worker performance lane."""

    workers: Literal[1] = 1
    queries_per_round: int = Field(default=100, ge=1)
    initial_table_rows: int = Field(default=100_000, ge=1)
    max_table_rows: int = Field(default=50_000_000, ge=1)
    max_calibration_rounds: int = Field(default=8, ge=1)
    calibration_runs_per_reference: Literal[3] = 3
    calibration_min_seconds: float = Field(default=5.0, gt=0)
    calibration_max_seconds: float = Field(default=12.0, gt=0)
    formal_timeout_seconds: float = Field(
        default=15.0, gt=0, le=MAX_STATEMENT_TIMEOUT_SECONDS
    )
    regression_threshold: float = Field(default=0.20, ge=0)
    max_start_skew_ms: float = Field(default=100.0, ge=0)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.initial_table_rows > self.max_table_rows:
            raise ValueError("initial_table_rows must not exceed max_table_rows")
        if self.calibration_min_seconds > self.calibration_max_seconds:
            raise ValueError("calibration_min_seconds must not exceed calibration_max_seconds")
        if self.calibration_max_seconds > self.formal_timeout_seconds:
            raise ValueError("calibration_max_seconds must not exceed formal_timeout_seconds")
        return self


class AppConfig(StrictModel):
    """Top-level configuration with one node for every fixed role."""

    mode: RunMode = RunMode.CORRECTNESS
    nodes: tuple[NodeConfig, ...]
    correctness: CorrectnessConfig = Field(default_factory=CorrectnessConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)

    @model_validator(mode="after")
    def require_fixed_unique_topology(self) -> Self:
        if len(self.nodes) != len(NodeRole):
            raise ValueError("nodes must contain exactly three entries")
        roles = [node.role for node in self.nodes]
        if set(roles) != set(NodeRole) or len(roles) != len(set(roles)):
            raise ValueError("nodes must contain baseline, custom_off, and custom_on exactly once")
        endpoints = [(node.host.casefold(), node.port) for node in self.nodes]
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("node host/port endpoints must be unique")
        return self

    def node_for(self, role: NodeRole) -> NodeConfig:
        """Return the configured node for a fixed role."""

        return next(node for node in self.nodes if node.role is role)


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
