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
                "MAX(ABS(`amount`)) AS maximum_amount "
                f"FROM `{left.name}` WHERE MOD(`tenant_id`, {modulus}) <= {tenant % modulus}"
            )
            tags = {"scan", "aggregate"}
        elif shape == 1:
            sql = (
                "SELECT COUNT(*) AS row_count, "
                "SUM(MOD((l.`amount` * r.`amount`) + l.`id` + r.`id`, 1000003)) "
                f"AS checksum FROM `{left.name}` AS l INNER JOIN `{right.name}` AS r "
                "ON r.`id` BETWEEN l.`id` AND l.`id` + 8 "
                f"WHERE l.`tenant_id` <= {tenant}"
            )
            tags = {"scan", "join", "aggregate"}
        elif shape == 2:
            sql = (
                "SELECT COUNT(*), SUM(group_total), MAX(group_total) FROM ("
                "SELECT `tenant_id`, SUM(`amount`) AS group_total "
                f"FROM `{left.name}` GROUP BY `tenant_id`"
                ") AS grouped_rows"
            )
            tags = {"scan", "aggregate", "group"}
        elif shape == 3:
            sql = (
                "SELECT `tenant_id`, COUNT(*) AS row_count, SUM(`amount`) AS group_total "
                f"FROM `{left.name}` GROUP BY `tenant_id` "
                "ORDER BY group_total DESC, `tenant_id` LIMIT 64"
            )
            tags = {"scan", "aggregate", "group", "sort"}
        else:
            sql = (
                "SELECT COUNT(*), SUM(l.`amount`) "
                f"FROM `{left.name}` AS l INNER JOIN (SELECT `tenant_id` "
                f"FROM `{right.name}` WHERE `status` <= {tenant % 16} "
                "GROUP BY `tenant_id`) AS matched_tenants "
                "ON matched_tenants.`tenant_id` = l.`tenant_id`"
            )
            tags = {"scan", "subquery", "join", "aggregate", "group"}
        return GeneratedQuery(sql, seed, self.name, frozenset(tags))


__all__ = ["LoadShapedQueryGenerator"]
