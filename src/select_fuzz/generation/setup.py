"""Immutable schema-plus-data setup bundles shared by all execution roles."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Mapping

from select_fuzz.generation.data import DataBundle, DataGenerator, DataScenario
from select_fuzz.generation.schema import SchemaManifest


@dataclass(frozen=True, slots=True)
class SetupBundle:
    schema: SchemaManifest
    data: DataBundle
    statements: tuple[str, ...]
    requires_same_session: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "statements", tuple(self.statements))
        schema_sha256 = sha256(self.schema.canonical_bytes()).hexdigest()
        if schema_sha256 != self.data.schema_sha256:
            raise ValueError("setup schema and data bundle have different identities")
        if self.requires_same_session != self.schema.requires_same_session:
            raise ValueError("setup session scope must match the schema manifest")

    @property
    def payload(self) -> Mapping[str, bytes]:
        return self.data.payload

    @property
    def payload_sha256(self) -> str:
        return self.data.payload_sha256

    def canonical_bytes(self) -> bytes:
        document = {
            "data_sha256": sha256(self.data.canonical_bytes()).hexdigest(),
            "requires_same_session": self.requires_same_session,
            "schema_sha256": sha256(self.schema.canonical_bytes()).hexdigest(),
            "statements": self.statements,
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class SetupBundleBuilder:
    def __init__(self, data_generator: DataGenerator | None = None) -> None:
        self.data_generator = data_generator or DataGenerator()

    def build(
        self,
        schema: SchemaManifest,
        *,
        seed: int,
        rows_per_table: int | Mapping[str, int],
        scenario: DataScenario = DataScenario.MIXED,
    ) -> SetupBundle:
        data = self.data_generator.generate(
            schema,
            seed=seed,
            rows_per_table=rows_per_table,
            scenario=scenario,
        )
        tables = {table.name: table for table in schema.tables}
        statements = (
            "SET time_zone = '+00:00';",
            *(tables[name].render() for name in data.table_order),
            *data.inserts_sql,
        )
        return SetupBundle(
            schema=schema,
            data=data,
            statements=statements,
            requires_same_session=schema.requires_same_session,
        )


__all__ = ["SetupBundle", "SetupBundleBuilder"]
