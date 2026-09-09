"""Configuration for the PQ hardening modes."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import math
import re
from typing import Literal


def validate_database(database: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", database):
        raise ValueError("database must be a simple SQL identifier (at most 64 characters)")
    if database.lower() in {"mysql", "sys", "information_schema", "performance_schema"}:
        raise ValueError("a dedicated test database is required")
    return database


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A single authorized database endpoint."""

    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = field(default="", repr=False)
    database: str = "pq_hardening"

    def __post_init__(self) -> None:
        if not self.host.strip() or any(c.isspace() for c in self.host):
            raise ValueError("host must identify one explicitly configured test endpoint")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")

    def as_kwargs(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
        }


@dataclass(frozen=True, slots=True)
class Tolerance:
    """Engineering tolerances; the source spec does not prescribe epsilon.

    Decimal tolerance applies only to explicitly declared expression columns.
    Accepted numerical differences remain visible as precision observations.
    """

    float_absolute: float = 1e-6
    float_relative: float = 1e-5
    double_absolute: float = 1e-12
    double_relative: float = 1e-9
    decimal_absolute: Decimal = Decimal("1e-9")
    decimal_relative: Decimal = Decimal("0")
    multiset_budget: int = 1_000_000

    def __post_init__(self) -> None:
        for name in ("float_absolute", "float_relative", "double_absolute", "double_relative"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        for name in ("decimal_absolute", "decimal_relative"):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
            object.__setattr__(self, name, value)
        if self.multiset_budget < 1:
            raise ValueError("multiset_budget must be positive")


@dataclass(frozen=True, slots=True)
class PQConfig:
    """Top-level configuration for a PQ hardening run."""

    endpoint: Endpoint
    database: str = "pq_hardening"
    dop_on: int = 4  # Expected setting only; execution DOP comes from EXPLAIN.
    dop_off: int = 0  # Serial intent; every serial plan is checked independently.
    rounds: int = 1
    queries_per_round: int = 200
    max_regenerations: int = 12
    seed: int = 1
    query_timeout_seconds: float = 60.0
    row_limit: int = 50_000
    tolerance: Tolerance = field(default_factory=Tolerance)
    mode: Literal["fast", "compare", "performance"] = "fast"
    perf_repeats: int = 5
    materialize_rows: int = 20_000
    materialize_tables: int = 4
    perf_dops: tuple[int, ...] = ()
    perf_warmups: int = 1
    max_duration_seconds: float = 600.0
    serial_endpoint: Endpoint | None = None

    def __post_init__(self) -> None:
        validate_database(self.database)
        if self.mode not in {"fast", "compare", "performance"}:
            raise ValueError("mode must be fast, compare or performance")
        if not 1 <= self.dop_on <= 256 or self.dop_off != 0:
            raise ValueError("dop_on must be 1..256 and dop_off must be 0")
        validate_performance_dops(self.perf_dops, expected_dop=self.dop_on)
        for name in ("rounds", "queries_per_round", "max_regenerations", "row_limit",
                     "perf_repeats", "materialize_rows", "materialize_tables"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.perf_warmups, int) or self.perf_warmups < 0:
            raise ValueError("perf_warmups must be a nonnegative integer")
        for name in ("query_timeout_seconds", "max_duration_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")


def validate_performance_dops(dops: tuple[int, ...], *, expected_dop: int) -> None:
    """Accept only the one preconfigured expectation, never a parameter sweep."""
    if any(not 1 <= d <= 256 for d in dops):
        raise ValueError("perf_dops must contain DOP values in 1..256")
    if dops and dops != (expected_dop,):
        raise ValueError(
            "DOP is preconfigured; automatic DOP sweeps are disabled. "
            "Use separate runs after externally configuring each DOP; "
            "perf_dops may only contain the expected dop_on value."
        )


def require_serial_endpoint(config: PQConfig) -> Endpoint:
    """Fail before opening a session or creating data without a serial arm."""
    if config.serial_endpoint is None:
        raise ValueError(
            "serial_endpoint (--serial-host) is required for a preconfigured serial "
            "comparison; the harness never changes database runtime parameters"
        )
    return config.serial_endpoint


__all__ = ["Endpoint", "PQConfig", "Tolerance", "require_serial_endpoint",
           "validate_performance_dops"]
