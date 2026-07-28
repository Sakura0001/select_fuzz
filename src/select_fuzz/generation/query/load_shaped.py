"""High-work, low-result SELECT shapes for sustained database load."""

from __future__ import annotations

import random

from select_fuzz.generation.query import GeneratedQuery, QueryGenerationContext
from select_fuzz.generation.query_grammar import GrammarSchema


class LoadShapedQueryGenerator:
    name = "load_shaped"

    def generate(
        self,
        context: QueryGenerationContext,
        *,
        seed: int,
    ) -> GeneratedQuery:
        if not isinstance(context.schema, GrammarSchema):
            raise ValueError("load-shaped query generation requires GrammarSchema")
        rng = random.Random(seed)
        tables = context.schema.tables
        left = rng.choice(tables)
        right = rng.choice(tables)
        tenant = rng.randrange(1, 1025)
        modulus = rng.choice((17, 31, 63, 127, 257))
        shape = rng.randrange(5)
        if shape == 0:
            sql = (
                "SELECT COUNT(*) AS row_count, "
                "SUM(MOD((`amount` * `amount`) + `id`, 1000003)) AS checksum, "
                "BIT_XOR(CRC32(`payload`)) AS payload_digest "
                f"FROM `{left.name}` WHERE MOD(`tenant_id`, {modulus}) <= {tenant % modulus}"
            )
            tags = {"scan", "aggregate"}
        elif shape == 1:
            sql = (
                "SELECT COUNT(*) AS row_count, "
                "SUM(MOD((l.`amount` * r.`amount`) + l.`id` + r.`id`, 1000003)) "
                f"AS checksum FROM `{left.name}` AS l JOIN `{right.name}` AS r "
                f"ON r.`tenant_id` = l.`tenant_id` AND MOD(r.`id`, {modulus}) = "
                f"MOD(l.`id`, {modulus}) WHERE l.`tenant_id` <= {tenant}"
            )
            tags = {"scan", "join", "aggregate"}
        elif shape == 2:
            sql = (
                "SELECT COUNT(*), SUM(group_total), MAX(group_total) FROM ("
                "SELECT `tenant_id`, SUM(`amount`) AS group_total "
                f"FROM `{left.name}` GROUP BY `tenant_id` HAVING COUNT(*) >= 1"
                ") AS grouped_rows"
            )
            tags = {"scan", "aggregate", "group"}
        elif shape == 3:
            sql = (
                "SELECT COUNT(*), SUM(window_value) FROM ("
                "SELECT SUM(`amount`) OVER (PARTITION BY `tenant_id` ORDER BY `id` "
                "ROWS BETWEEN 32 PRECEDING AND CURRENT ROW) AS window_value "
                f"FROM `{left.name}`"
                ") AS window_rows"
            )
            tags = {"scan", "window", "sort"}
        else:
            sql = (
                "SELECT COUNT(*), SUM(`amount`) FROM "
                f"`{left.name}` WHERE `tenant_id` IN (SELECT `tenant_id` "
                f"FROM `{right.name}` WHERE MOD(`status`, {modulus}) = {tenant % modulus})"
            )
            tags = {"scan", "subquery", "aggregate"}
        return GeneratedQuery(sql, seed, self.name, frozenset(tags))


__all__ = ["LoadShapedQueryGenerator"]
