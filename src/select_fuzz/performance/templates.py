"""Built-in reproducible CPU-dense templates and materialization contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

from select_fuzz.performance.models import ScaleKnobs
from select_fuzz.performance.tree import Family, ShapeBoundary


@dataclass(frozen=True, slots=True)
class CpuDenseSetupManifest:
    template_id: str
    seed: int
    expected_row_count: int
    setup_statements: tuple[str, ...]


def _digit_source(alias: str) -> str:
    values = " UNION ALL ".join(
        f"SELECT {value}" if value else "SELECT 0 AS n" for value in range(10)
    )
    return f"({values}) AS {alias}"


def _setup_manifest(
    template_id: str,
    seed: int,
    scale: ScaleKnobs,
) -> CpuDenseSetupManifest:
    rows = scale.table_rows
    digits = max(1, len(str(rows - 1)))
    sources = " CROSS JOIN ".join(_digit_source(f"d{index}") for index in range(digits))
    terms = " + ".join(f"{10**index} * d{index}.n" for index in range(digits))
    insert = (
        "INSERT INTO cpu_data (id, v) "
        "SELECT n, MOD((n * 1103515245) + "
        f"{seed}, 2147483647) FROM (SELECT 1 + {terms} AS n FROM {sources}) AS seq "
        f"WHERE n <= {rows} ORDER BY n"
    )
    return CpuDenseSetupManifest(
        template_id=template_id,
        seed=seed,
        expected_row_count=rows,
        setup_statements=(
            "DROP TABLE IF EXISTS cpu_data",
            "CREATE TABLE cpu_data (id BIGINT NOT NULL PRIMARY KEY, "
            "v BIGINT NOT NULL) ENGINE=InnoDB",
            insert,
        ),
    )


def _case_id(case_id: str, round_number: int, query_number: int) -> str:
    if round_number <= 0 or query_number <= 0:
        raise ValueError("round and query numbers must be positive")
    return f"{case_id}_r{round_number}_q{query_number}"


@dataclass(frozen=True, slots=True)
class CpuDenseScanTemplate:
    seed: int
    case_id: str
    initial_scale: ScaleKnobs = ScaleKnobs()
    template_id: str = "cpu_dense_scan_aggregate_v1"
    boundary: ShapeBoundary = ShapeBoundary(required=frozenset({Family.SCAN, Family.AGGREGATE}))
    driver_family: Family = Family.SCAN

    def for_case(self, round_number: int, query_number: int) -> CpuDenseScanTemplate:
        return replace(
            self,
            seed=self.seed ^ (round_number << 32) ^ query_number,
            case_id=_case_id(self.case_id, round_number, query_number),
        )

    def target_rows(self, scale: ScaleKnobs) -> int:
        return scale.scan_rows

    def render(self, scale: ScaleKnobs) -> str:
        return (
            "SELECT SUM(((v * v) + id + (v DIV 7)) % 1000003) AS cpu_checksum "
            f"FROM cpu_data WHERE id <= {scale.scan_rows} ORDER BY 1"
        )

    def data_manifest(self, scale: ScaleKnobs) -> CpuDenseSetupManifest:
        return _setup_manifest(self.template_id, self.seed, scale)


@dataclass(frozen=True, slots=True)
class CpuDenseRangeSortTemplate:
    seed: int
    case_id: str
    initial_scale: ScaleKnobs = ScaleKnobs()
    template_id: str = "cpu_dense_range_sort_v1"
    boundary: ShapeBoundary = ShapeBoundary(required=frozenset({Family.SCAN, Family.SORT}))
    driver_family: Family = Family.SCAN

    def for_case(self, round_number: int, query_number: int) -> CpuDenseRangeSortTemplate:
        return replace(
            self,
            seed=self.seed ^ (round_number << 32) ^ query_number,
            case_id=_case_id(self.case_id, round_number, query_number),
        )

    def target_rows(self, scale: ScaleKnobs) -> int:
        return max(1, math.ceil(scale.table_rows * scale.range_selectivity))

    def render(self, scale: ScaleKnobs) -> str:
        selected = self.target_rows(scale)
        limit = min(selected, scale.sort_rows)
        return (
            "SELECT id, SHA2(CONCAT(v, REPEAT('x', "
            f"{scale.sort_key_bytes})), 256) AS sort_key FROM cpu_data "
            f"WHERE id <= {selected} ORDER BY 2, 1 LIMIT {limit}"
        )

    def data_manifest(self, scale: ScaleKnobs) -> CpuDenseSetupManifest:
        return _setup_manifest(self.template_id, self.seed, scale)


@dataclass(frozen=True, slots=True)
class CpuDenseJoinTemplate:
    seed: int
    case_id: str
    initial_scale: ScaleKnobs = ScaleKnobs()
    template_id: str = "cpu_dense_join_v1"
    boundary: ShapeBoundary = ShapeBoundary(
        required=frozenset({Family.SCAN, Family.JOIN, Family.AGGREGATE})
    )
    driver_family: Family = Family.JOIN

    def for_case(self, round_number: int, query_number: int) -> CpuDenseJoinTemplate:
        return replace(
            self,
            seed=self.seed ^ (round_number << 32) ^ query_number,
            case_id=_case_id(self.case_id, round_number, query_number),
        )

    def target_rows(self, scale: ScaleKnobs) -> int:
        return max(1, math.ceil(scale.join_probe_rows * scale.join_fanout))

    def render(self, scale: ScaleKnobs) -> str:
        fanout = max(1, math.ceil(scale.join_fanout))
        return (
            "SELECT SUM(MOD((a.v * b.v) + a.id + b.id, 1000003)) AS checksum "
            "FROM cpu_data AS a INNER JOIN cpu_data AS b ON b.id BETWEEN "
            f"MOD(a.v, {scale.join_build_rows}) + 1 AND LEAST({scale.join_build_rows}, "
            f"MOD(a.v, {scale.join_build_rows}) + {fanout}) "
            f"WHERE a.id <= {scale.join_probe_rows} ORDER BY 1"
        )

    def data_manifest(self, scale: ScaleKnobs) -> CpuDenseSetupManifest:
        return _setup_manifest(self.template_id, self.seed, scale)


@dataclass(frozen=True, slots=True)
class CpuDenseGroupSortTemplate:
    seed: int
    case_id: str
    initial_scale: ScaleKnobs = ScaleKnobs()
    template_id: str = "cpu_dense_group_sort_v1"
    boundary: ShapeBoundary = ShapeBoundary(
        required=frozenset({Family.SCAN, Family.AGGREGATE, Family.SORT})
    )
    driver_family: Family = Family.AGGREGATE

    def for_case(self, round_number: int, query_number: int) -> CpuDenseGroupSortTemplate:
        return replace(
            self,
            seed=self.seed ^ (round_number << 32) ^ query_number,
            case_id=_case_id(self.case_id, round_number, query_number),
        )

    def target_rows(self, scale: ScaleKnobs) -> int:
        return scale.aggregate_input_rows

    def render(self, scale: ScaleKnobs) -> str:
        return (
            f"SELECT MOD(v, {scale.aggregate_groups}) AS group_key, "
            "SUM(MOD((v * v) + id, 1000003)) AS checksum FROM cpu_data "
            f"WHERE id <= {scale.aggregate_input_rows} GROUP BY 1 ORDER BY 2, 1"
        )

    def data_manifest(self, scale: ScaleKnobs) -> CpuDenseSetupManifest:
        return _setup_manifest(self.template_id, self.seed, scale)


@dataclass(frozen=True, slots=True)
class CpuDenseWindowTemplate:
    seed: int
    case_id: str
    initial_scale: ScaleKnobs = ScaleKnobs()
    template_id: str = "cpu_dense_window_v1"
    boundary: ShapeBoundary = ShapeBoundary(
        required=frozenset({Family.SCAN, Family.WINDOW, Family.SORT})
    )
    driver_family: Family = Family.WINDOW

    def for_case(self, round_number: int, query_number: int) -> CpuDenseWindowTemplate:
        return replace(
            self,
            seed=self.seed ^ (round_number << 32) ^ query_number,
            case_id=_case_id(self.case_id, round_number, query_number),
        )

    def target_rows(self, scale: ScaleKnobs) -> int:
        return scale.sort_rows

    def render(self, scale: ScaleKnobs) -> str:
        partitions = max(1, math.ceil(scale.sort_rows / scale.window_partition_rows))
        return (
            "SELECT id, SUM(MOD((v * v) + id, 1000003)) OVER (PARTITION BY "
            f"MOD(id, {partitions}) ORDER BY id ROWS BETWEEN "
            f"{scale.window_frame_rows} PRECEDING AND CURRENT ROW) AS window_sum "
            f"FROM cpu_data WHERE id <= {scale.sort_rows} ORDER BY 2, 1"
        )

    def data_manifest(self, scale: ScaleKnobs) -> CpuDenseSetupManifest:
        return _setup_manifest(self.template_id, self.seed, scale)


__all__ = [
    "CpuDenseGroupSortTemplate",
    "CpuDenseJoinTemplate",
    "CpuDenseRangeSortTemplate",
    "CpuDenseScanTemplate",
    "CpuDenseSetupManifest",
    "CpuDenseWindowTemplate",
]
