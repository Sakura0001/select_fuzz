"""Bounded business-like DML generation for the fuzz writer pool."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random


def _quote_identifier(value: str) -> str:
    if not value or not value.replace("_", "").isalnum() or value[0].isdigit():
        raise ValueError("identifier must be alphanumeric snake_case")
    return f"`{value}`"


@dataclass(frozen=True, slots=True)
class FuzzTable:
    name: str
    primary_key: str
    mutable_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FuzzDmlStatement:
    operation: str
    sql: str
    target_rows: int

    def with_target_rows(self, target_rows: int) -> FuzzDmlStatement:
        """Return the same bounded statement with a smaller source LIMIT."""

        if not 1 <= target_rows <= self.target_rows:
            raise ValueError("target_rows must be within the generated statement bound")
        if target_rows == self.target_rows:
            return self
        marker = f"LIMIT {self.target_rows}"
        if marker not in self.sql:
            raise ValueError("generated DML statement has no bounded LIMIT")
        return FuzzDmlStatement(
            self.operation,
            self.sql.replace(marker, f"LIMIT {target_rows}", 1),
            target_rows,
        )


class FuzzDmlGenerator:
    # Every materialized fuzz table starts with id=1.  Keeping that seed row
    # outside the delete predicate guarantees that a delete workload cannot
    # drain a table completely, while still allowing point deletes for every
    # other row.
    _protected_primary_key_value = 1

    def __init__(
        self,
        tables: tuple[FuzzTable, ...],
        *,
        batch_rows_min: int,
        batch_rows_max: int,
        delete_batch_rows_min: int,
        delete_batch_rows_max: int,
    ) -> None:
        if not tables:
            raise ValueError("at least one fuzz table is required")
        if not 1 <= batch_rows_min <= batch_rows_max:
            raise ValueError("invalid DML batch bounds")
        if not 1 <= delete_batch_rows_min <= delete_batch_rows_max <= 100:
            raise ValueError("delete batches must stay between one and 100 rows")
        self._tables = tables
        self._batch_rows_min = batch_rows_min
        self._batch_rows_max = batch_rows_max
        self._delete_min = delete_batch_rows_min
        self._delete_max = delete_batch_rows_max

    def generate(
        self,
        *,
        seed: int,
        known_high_watermark: int,
        insert_weight: int = 35,
        update_weight: int = 45,
        delete_weight: int = 10,
        upsert_weight: int = 10,
    ) -> FuzzDmlStatement:
        weights = (insert_weight, update_weight, delete_weight, upsert_weight)
        if any(weight < 0 for weight in weights) or sum(weights) != 100:
            raise ValueError("DML weights must be nonnegative and sum to 100")
        rng = random.Random(seed)
        ticket = rng.randrange(100)
        if ticket < insert_weight:
            return self.generate_insert(seed=seed, known_high_watermark=known_high_watermark)
        if ticket < insert_weight + update_weight:
            return self.generate_update(seed=seed, known_high_watermark=known_high_watermark)
        if ticket < insert_weight + update_weight + delete_weight:
            return self.generate_delete(seed=seed, known_high_watermark=known_high_watermark)
        return self.generate_upsert(seed=seed, known_high_watermark=known_high_watermark)

    def _batch(self, rng: random.Random) -> int:
        lower = self._batch_rows_min
        upper = self._batch_rows_max
        if lower == upper:
            return lower
        # Log-uniform selection keeps large batches reachable without making
        # every operation a maximum-sized transaction.
        return min(
            upper,
            max(
                lower,
                int(10 ** rng.uniform(math.log10(lower), math.log10(upper))),
            ),
        )

    def generate_insert(
        self,
        *,
        seed: int,
        known_high_watermark: int,
    ) -> FuzzDmlStatement:
        rng = random.Random(seed)
        table = rng.choice(self._tables)
        rows = self._batch(rng)
        start = rng.randint(1, max(1, known_high_watermark))
        table_name = _quote_identifier(table.name)
        sql = (
            f"INSERT INTO {table_name} (`tenant_id`,`amount`,`status`,`updated_at`,`payload`) "
            "SELECT MOD(`tenant_id` + 17, 1024) + 1, `amount` + 1, "
            "MOD(`status` + 1, 16), UTC_TIMESTAMP(6), "
            f"CONCAT(`payload`, '-i{seed % 10007}') FROM {table_name} "
            f"WHERE `id` >= {start} ORDER BY `id` LIMIT {rows}"
        )
        return FuzzDmlStatement("insert", sql, rows)

    def generate_update(
        self,
        *,
        seed: int,
        known_high_watermark: int,
    ) -> FuzzDmlStatement:
        rng = random.Random(seed)
        table = rng.choice(self._tables)
        rows = self._batch(rng)
        start = rng.randint(1, max(1, known_high_watermark))
        table_name = _quote_identifier(table.name)
        sql = (
            f"UPDATE {table_name} SET `amount` = `amount` + {rng.randint(1, 97)}, "
            "`status` = MOD(`status` + 1, 16), `updated_at` = UTC_TIMESTAMP(6), "
            f"`payload` = CONCAT('u{seed % 10007}-', RIGHT(`payload`, 220)) "
            f"WHERE `id` >= {start} ORDER BY `id` LIMIT {rows}"
        )
        return FuzzDmlStatement("update", sql, rows)

    def generate_upsert(
        self,
        *,
        seed: int,
        known_high_watermark: int,
    ) -> FuzzDmlStatement:
        rng = random.Random(seed)
        table = rng.choice(self._tables)
        rows = self._batch(rng)
        start = rng.randint(1, max(1, known_high_watermark))
        table_name = _quote_identifier(table.name)
        sql = (
            f"INSERT INTO {table_name} (`id`,`tenant_id`,`amount`,`status`,`updated_at`,`payload`) "
            "SELECT `id`, `tenant_id`, `amount`, `status`, UTC_TIMESTAMP(6), `payload` "
            f"FROM {table_name} WHERE `id` >= {start} ORDER BY `id` LIMIT {rows} "
            "ON DUPLICATE KEY UPDATE `amount` = VALUES(`amount`) + 1, "
            "`status` = MOD(VALUES(`status`) + 1, 16), "
            "`updated_at` = UTC_TIMESTAMP(6)"
        )
        return FuzzDmlStatement("upsert", sql, rows)

    def generate_delete(
        self,
        *,
        seed: int,
        known_high_watermark: int,
    ) -> FuzzDmlStatement:
        if known_high_watermark <= 0:
            raise ValueError("known_high_watermark must be positive")
        rng = random.Random(seed)
        table = rng.choice(self._tables)
        point_delete = rng.randrange(100) < 60
        target_rows = 1 if point_delete else rng.randint(self._delete_min, self._delete_max)
        start = rng.randint(
            self._protected_primary_key_value + 1,
            max(self._protected_primary_key_value + 1, known_high_watermark),
        )
        table_name = _quote_identifier(table.name)
        primary_key = _quote_identifier(table.primary_key)
        if point_delete:
            sql = (
                f"DELETE FROM {table_name} WHERE {primary_key} = {start} "
                f"AND {primary_key} <> {self._protected_primary_key_value} "
                "LIMIT 1"
            )
        else:
            sql = (
                f"DELETE FROM {table_name} WHERE {primary_key} >= {start} "
                f"AND {primary_key} <> {self._protected_primary_key_value} "
                f"ORDER BY {primary_key} LIMIT {target_rows}"
            )
        return FuzzDmlStatement("delete", sql, target_rows)


__all__ = ["FuzzDmlGenerator", "FuzzDmlStatement", "FuzzTable"]
