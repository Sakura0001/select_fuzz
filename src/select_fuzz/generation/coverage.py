"""Thread-safe coverage debt accounting with atomic durable checkpoints."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
from threading import Lock
from typing import BinaryIO
from uuid import uuid4

if sys.platform == "win32":  # pragma: win32 cover
    import msvcrt
else:  # pragma: posix cover
    import fcntl

from select_fuzz.domain.values import SeedTree
from select_fuzz.generation.catalog import FeatureCatalog, FeatureSpec
from select_fuzz.generation.catalog_schema import Version


class CoverageCorruptError(ValueError):
    """A persisted coverage checkpoint violates its schema."""


class NoEnabledFeatureError(LookupError):
    """No catalog target is compatible with this run."""


class CoveragePlanExhaustedError(RuntimeError):
    """The deterministic debt batch ended before all reserved hits landed."""


def _lock_file(stream: BinaryIO) -> None:
    if sys.platform == "win32":  # pragma: win32 cover
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    else:  # pragma: posix cover
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_file(stream: BinaryIO) -> None:
    if sys.platform == "win32":  # pragma: win32 cover
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: posix cover
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _fsync_parent_directory(path: Path) -> None:
    """Durably flush rename metadata where the platform exposes directory fsync."""

    if sys.platform == "win32":  # pragma: win32 cover
        # Windows cannot open directories through os.open(). The replaced file was
        # already fsynced above; NTFS journals the atomic rename metadata.
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class CoverageLedger:
    """Monotonic in-memory counts with explicit atomic checkpoints."""

    def __init__(self, path: str | Path, counts: dict[str, int] | None = None):
        self.path = Path(path)
        self._counts: dict[str, int] = {}
        self._pending_deltas: dict[str, int] = {}
        for feature_id, hits in (counts or {}).items():
            self._validate_entry(feature_id, hits)
            self._counts[feature_id] = hits
        self._lock = Lock()
        self._checkpoint_lock = Lock()

    @staticmethod
    def _validate_entry(feature_id: object, hits: object) -> None:
        if not isinstance(feature_id, str) or not feature_id:
            raise TypeError("feature_id must be a non-empty string")
        if not isinstance(hits, int) or isinstance(hits, bool):
            raise TypeError("hits must be an integer")
        if hits < 0:
            raise ValueError("hits must be nonnegative")

    def record(self, feature_id: str, hits: int = 1) -> None:
        self._validate_entry(feature_id, hits)
        with self._lock:
            self._counts[feature_id] = self._counts.get(feature_id, 0) + hits
            self._pending_deltas[feature_id] = self._pending_deltas.get(feature_id, 0) + hits

    def hits(self, feature_id: str) -> int:
        with self._lock:
            return self._counts.get(feature_id, 0)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def checkpoint(self) -> None:
        with self._checkpoint_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.parent / f".{self.path.name}.lock"
            with lock_path.open("a+b") as process_lock:
                _lock_file(process_lock)
                try:
                    self._checkpoint_with_process_lock()
                finally:
                    _unlock_file(process_lock)

    def _checkpoint_with_process_lock(self) -> None:
        temporary = self.path.parent / f".{self.path.name}.{uuid4().hex}.tmp"
        with self._lock:
            pending_batch = self._pending_deltas
            self._pending_deltas = {}
        replaced = False
        try:
            persisted = type(self).load(self.path).snapshot() if self.path.exists() else {}
            merged = dict(persisted)
            for feature_id, hits in pending_batch.items():
                merged[feature_id] = merged.get(feature_id, 0) + hits
            payload = (
                json.dumps(
                    {"schema_version": 1, "counts": merged},
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            replaced = True
            _fsync_parent_directory(self.path.parent)
        except BaseException:
            with self._lock:
                if replaced:
                    self._reset_counts_from_checkpoint(merged)
                else:
                    for feature_id, hits in pending_batch.items():
                        self._pending_deltas[feature_id] = (
                            self._pending_deltas.get(feature_id, 0) + hits
                        )
            raise
        else:
            with self._lock:
                self._reset_counts_from_checkpoint(merged)
        finally:
            temporary.unlink(missing_ok=True)

    def _reset_counts_from_checkpoint(self, persisted: dict[str, int]) -> None:
        self._counts = dict(persisted)
        for feature_id, hits in self._pending_deltas.items():
            self._counts[feature_id] = self._counts.get(feature_id, 0) + hits

    @classmethod
    def load(cls, path: str | Path) -> CoverageLedger:
        checkpoint = Path(path)
        try:
            document = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CoverageCorruptError("unable to read coverage checkpoint") from error
        if not isinstance(document, dict) or set(document) != {"schema_version", "counts"}:
            raise CoverageCorruptError("invalid coverage checkpoint fields")
        if document["schema_version"] != 1 or not isinstance(document["counts"], dict):
            raise CoverageCorruptError("invalid coverage checkpoint schema")
        counts = document["counts"]
        try:
            return cls(checkpoint, counts=counts)
        except (TypeError, ValueError) as error:
            raise CoverageCorruptError("invalid coverage counts") from error


class CoverageScheduler:
    """Choose maximum-debt targets before applying catalog weights."""

    def __init__(
        self,
        *,
        catalog: FeatureCatalog,
        ledger: CoverageLedger,
        min_hits: int,
        version: Version,
        profiles: frozenset[str] | None = None,
        schedule_seed: int = 0,
        plan_start_ordinal: int = 0,
    ):
        if min_hits <= 0:
            raise ValueError("min_hits must be positive")
        self.catalog = catalog
        self.ledger = ledger
        self.min_hits = min_hits
        self.version = version
        self.profiles = profiles
        self.schedule_seed = schedule_seed
        if (
            not isinstance(plan_start_ordinal, int)
            or isinstance(plan_start_ordinal, bool)
            or plan_start_ordinal < 0
        ):
            raise ValueError("plan_start_ordinal must be a nonnegative integer")
        self.plan_start_ordinal = plan_start_ordinal
        self._enabled_specs = self._load_enabled()
        completed_cycles = min(
            self.ledger.hits(spec.feature_id) // self.min_hits for spec in self._enabled_specs
        )
        self.cycle_number = completed_cycles + 1
        self.cycle_target_hits = self.cycle_number * self.min_hits
        self._debt_plan = self._build_debt_plan()

    @property
    def planned_case_count(self) -> int:
        return len(self._debt_plan)

    @property
    def enabled_specs(self) -> tuple[FeatureSpec, ...]:
        """All compatible targets, including targets outside the current debt batch."""

        return self._enabled_specs

    @property
    def plan_end_ordinal(self) -> int:
        return self.plan_start_ordinal + self.planned_case_count

    def replan(self, *, start_case_ordinal: int) -> CoverageScheduler:
        """Build the next deterministic debt batch at a monotonic global ordinal."""

        if (
            not isinstance(start_case_ordinal, int)
            or isinstance(start_case_ordinal, bool)
            or start_case_ordinal < self.plan_end_ordinal
        ):
            raise ValueError("start_case_ordinal must be at least the current plan end ordinal")
        return type(self)(
            catalog=self.catalog,
            ledger=self.ledger,
            min_hits=self.min_hits,
            version=self.version,
            profiles=self.profiles,
            schedule_seed=self.schedule_seed,
            plan_start_ordinal=start_case_ordinal,
        )

    def _load_enabled(self) -> tuple[FeatureSpec, ...]:
        enabled = self.catalog.signature_targets(
            version=self.version,
            profiles=self.profiles,
        )
        if not enabled:
            raise NoEnabledFeatureError("no feature is enabled")
        return tuple(sorted(enabled, key=lambda spec: spec.feature_id))

    def _build_debt_plan(self) -> tuple[FeatureSpec, ...]:
        debts = {
            spec.feature_id: max(
                0,
                self.cycle_target_hits - self.ledger.hits(spec.feature_id),
            )
            for spec in self._enabled_specs
        }
        tree = SeedTree(self.schedule_seed)
        plan: list[FeatureSpec] = []
        for level in range(max(debts.values(), default=0)):
            level_specs = [spec for spec in self._enabled_specs if debts[spec.feature_id] > level]
            level_specs.sort(key=lambda spec: tree.derive("coverage", level, spec.feature_id))
            plan.extend(level_specs)
        return tuple(plan)

    def choose(self, *, case_ordinal: int) -> FeatureSpec:
        if not isinstance(case_ordinal, int) or isinstance(case_ordinal, bool) or case_ordinal < 0:
            raise ValueError("case_ordinal must be a nonnegative integer")
        batch_ordinal = case_ordinal - self.plan_start_ordinal
        if batch_ordinal < 0:
            raise ValueError("case_ordinal predates this coverage plan")
        if batch_ordinal < len(self._debt_plan):
            return self._debt_plan[batch_ordinal]
        if not self.cycle_complete():
            raise CoveragePlanExhaustedError(
                "coverage debt batch exhausted before all hits were recorded; replan"
            )

        weights = [max(1, round(spec.weight * 1000)) for spec in self._enabled_specs]
        total_weight = sum(weights)
        ticket = (
            SeedTree(self.schedule_seed).derive("coverage", "weighted", case_ordinal) % total_weight
        )
        for spec, weight in zip(self._enabled_specs, weights, strict=True):
            if ticket < weight:
                return spec
            ticket -= weight
        raise AssertionError("weighted coverage selection exhausted")  # pragma: no cover

    def cycle_complete(self) -> bool:
        return all(
            self.ledger.hits(spec.feature_id) >= self.cycle_target_hits
            for spec in self._enabled_specs
        )


class WeightedCoverageScheduler(CoverageScheduler):
    """Seeded weighted-random selection with coverage debt as a bounded bias.

    Unlike the exhaustive debt plan, every compatible target remains selectable
    for every case. Missing coverage increases probability without turning fuzz
    generation into a fixed enumeration.
    """

    def __init__(self, *args: object, max_debt_boost: float = 4.0, **kwargs: object) -> None:
        if (
            not isinstance(max_debt_boost, (int, float))
            or isinstance(max_debt_boost, bool)
            or not math.isfinite(max_debt_boost)
            or max_debt_boost < 0
        ):
            raise ValueError("max_debt_boost must be a finite nonnegative number")
        self.max_debt_boost = float(max_debt_boost)
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def choose(self, *, case_ordinal: int) -> FeatureSpec:
        if (
            not isinstance(case_ordinal, int)
            or isinstance(case_ordinal, bool)
            or case_ordinal < self.plan_start_ordinal
        ):
            raise ValueError("case_ordinal predates this coverage plan")
        weighted: list[tuple[FeatureSpec, int]] = []
        for spec in self.enabled_specs:
            debt_ratio = min(
                1.0,
                max(0, self.cycle_target_hits - self.ledger.hits(spec.feature_id))
                / self.min_hits,
            )
            effective = spec.weight * (1.0 + self.max_debt_boost * debt_ratio)
            weighted.append((spec, max(1, round(effective * 1000))))
        ticket = SeedTree(self.schedule_seed).derive(
            "coverage", "weighted_fuzz", case_ordinal
        ) % sum(weight for _, weight in weighted)
        for spec, weight in weighted:
            if ticket < weight:
                return spec
            ticket -= weight
        raise AssertionError("weighted fuzz selection exhausted")  # pragma: no cover
