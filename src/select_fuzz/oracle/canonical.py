"""Lossless typed canonical forms used by the correctness oracle."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TypeAlias

from select_fuzz.domain.models import ColumnMeta
from select_fuzz.oracle.errors import CanonicalizationError


MYSQL_TYPE_FLOAT = 4
MYSQL_TYPE_DOUBLE = 5
MYSQL_TYPE_BIT = 16
MYSQL_TYPE_JSON = 245
MYSQL_TYPE_GEOMETRY = 255
MYSQL_FLAG_SET = 1 << 11
FLOAT_TYPE_CODES = frozenset({MYSQL_TYPE_FLOAT, MYSQL_TYPE_DOUBLE})

CanonicalValue: TypeAlias = object


@dataclass(frozen=True, slots=True)
class FloatTolerance:
    absolute: float
    relative: float


@dataclass(frozen=True, slots=True)
class FloatCell:
    kind: str
    value: float | None = None
    exact: CanonicalValue | None = None

    def sort_key(self) -> tuple[int, float, str]:
        kind_order = {
            "null": 0,
            "negative_infinity": 1,
            "finite": 2,
            "positive_infinity": 3,
            "nan": 4,
            "typed": 5,
        }
        if self.kind == "finite" and self.value is not None:
            return kind_order[self.kind], self.value, ""
        return kind_order[self.kind], 0.0, repr(self.exact)


@dataclass(frozen=True, slots=True)
class _JsonObject:
    pairs: tuple[tuple[str, object], ...]


def is_float_column(column: ColumnMeta) -> bool:
    return column.type_code in FLOAT_TYPE_CODES


def tolerance_for(column: ColumnMeta) -> FloatTolerance:
    if column.type_code == MYSQL_TYPE_FLOAT:
        return FloatTolerance(absolute=1e-6, relative=1e-5)
    if column.type_code == MYSQL_TYPE_DOUBLE:
        return FloatTolerance(absolute=1e-12, relative=1e-9)
    raise CanonicalizationError(f"column {column.name!r} is not FLOAT or DOUBLE")


def _decimal_value(value: Decimal) -> CanonicalValue:
    if value.is_nan():
        return "decimal_nan", value.as_tuple()
    if value.is_infinite():
        return "decimal_infinity", value.is_signed()
    decimal_tuple = value.as_tuple()
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):  # pragma: no cover - finite invariant
        raise CanonicalizationError("finite Decimal has a non-integer exponent")
    digits = list(decimal_tuple.digits)
    if not any(digits):
        return "decimal", 0, (0,), 0
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return "decimal", decimal_tuple.sign, tuple(digits), exponent


def _offset_seconds(value: datetime | time) -> float | None:
    offset = value.utcoffset()
    return None if offset is None else offset.total_seconds()


def _typed_value(value: object) -> CanonicalValue:
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, int):
        return "int", value
    if isinstance(value, Decimal):
        return _decimal_value(value)
    if isinstance(value, str):
        return "str", value
    if isinstance(value, bytes):
        return "bytes", value
    if isinstance(value, memoryview):
        return "bytes", value.tobytes()
    if isinstance(value, datetime):
        return (
            "datetime",
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            value.fold,
            _offset_seconds(value),
            value.tzname(),
        )
    if isinstance(value, date):
        return "date", value.year, value.month, value.day
    if isinstance(value, time):
        return (
            "time",
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            value.fold,
            _offset_seconds(value),
            value.tzname(),
        )
    if isinstance(value, timedelta):
        return "timedelta", value.days, value.seconds, value.microseconds
    if isinstance(value, float):
        if math.isnan(value):
            return ("float_nan",)
        if math.isinf(value):
            return "float_infinity", math.copysign(1.0, value)
        return "float", value.hex()
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError("mapping wire values require string keys")
        return (
            "mapping",
            tuple(sorted((key, _typed_value(child)) for key, child in value.items())),
        )
    if isinstance(value, tuple):
        return "tuple", tuple(_typed_value(child) for child in value)
    raise CanonicalizationError(
        f"unsupported wire value type: {type(value).__module__}.{type(value).__qualname__}"
    )


def _json_object(pairs: list[tuple[str, object]]) -> _JsonObject:
    return _JsonObject(tuple(pairs))


def _canonical_json(value: object) -> CanonicalValue:
    if isinstance(value, _JsonObject):
        keys = [key for key, _ in value.pairs]
        if len(keys) != len(set(keys)):
            raise CanonicalizationError("JSON objects with duplicate keys are ambiguous")
        return (
            "json_object",
            tuple(sorted((key, _canonical_json(child)) for key, child in value.pairs)),
        )
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError("JSON object keys must be strings")
        return (
            "json_object",
            tuple(sorted((key, _canonical_json(child)) for key, child in value.items())),
        )
    if isinstance(value, tuple):
        return "json_array", tuple(_canonical_json(child) for child in value)
    if isinstance(value, list):
        return "json_array", tuple(_canonical_json(child) for child in value)
    if value is None:
        return ("json_null",)
    if isinstance(value, bool):
        return "json_bool", value
    if isinstance(value, int):
        return "json_int", value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalizationError("JSON does not permit Decimal NaN or infinity")
        return "json_decimal", _decimal_value(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("JSON does not permit NaN or infinity")
        return "json_float", value.hex()
    if isinstance(value, str):
        return "json_string", value
    raise CanonicalizationError(f"unsupported JSON value type: {type(value).__qualname__}")


def _json_value(value: object) -> CanonicalValue:
    parsed = value
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CanonicalizationError("JSON bytes must be UTF-8") from error
        parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(
                value,
                parse_float=Decimal,
                parse_int=int,
                object_pairs_hook=_json_object,
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise CanonicalizationError("invalid JSON wire value") from error
    return "json", _canonical_json(parsed)


def _bit_value(value: object) -> CanonicalValue:
    if isinstance(value, bool):
        return "bit", int(value)
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
            raise CanonicalizationError("BIT values must fit MySQL BIT(64)")
        return "bit", value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        if not 1 <= len(value) <= 8:
            raise CanonicalizationError("BIT byte values must contain 1 to 8 bytes")
        return "bit", int.from_bytes(value, byteorder="big", signed=False)
    raise CanonicalizationError("BIT values must be int or bytes")


def _spatial_value(value: object) -> CanonicalValue:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        if len(value) < 9:
            raise CanonicalizationError(
                "spatial bytes require a four-byte SRID and five-byte WKB header"
            )
        srid = int.from_bytes(value[:4], byteorder="little", signed=False)
        wkb = value[4:]
    elif isinstance(value, Mapping):
        if set(value) != {"srid", "wkb"}:
            raise CanonicalizationError("spatial mappings require srid and wkb")
        srid = value["srid"]
        wkb = value["wkb"]
    elif isinstance(value, tuple) and len(value) == 2:
        srid, wkb = value
    else:
        raise CanonicalizationError("unsupported spatial wire value")
    if not isinstance(srid, int) or isinstance(srid, bool) or not 0 <= srid <= 0xFFFFFFFF:
        raise CanonicalizationError("spatial SRID must be an unsigned 32-bit integer")
    if isinstance(wkb, memoryview):
        wkb = wkb.tobytes()
    if not isinstance(wkb, bytes) or len(wkb) < 5:
        raise CanonicalizationError("spatial WKB must contain a five-byte header")
    if wkb[0] not in {0, 1}:
        raise CanonicalizationError("spatial WKB has an invalid byte-order marker")
    return "spatial", srid, wkb


def _set_value(value: object) -> CanonicalValue:
    if not isinstance(value, (set, frozenset)):
        raise CanonicalizationError("SET values must be returned as a set")
    if not all(isinstance(member, (str, bytes)) for member in value):
        raise CanonicalizationError("SET members must be strings or bytes")
    members = tuple(sorted((_typed_value(member) for member in value), key=repr))
    return "set", members


def canonical_group_value(value: object, column: ColumnMeta) -> CanonicalValue:
    if value is None:
        return ("null",)
    if column.flags is not None and column.flags & MYSQL_FLAG_SET:
        return _set_value(value)
    if column.type_code == MYSQL_TYPE_BIT:
        return _bit_value(value)
    if column.type_code == MYSQL_TYPE_JSON:
        return _json_value(value)
    if column.type_code == MYSQL_TYPE_GEOMETRY:
        return _spatial_value(value)
    return _typed_value(value)


def canonical_float_cell(value: object) -> FloatCell:
    if value is None:
        return FloatCell("null")
    if not isinstance(value, float):
        return FloatCell("typed", exact=_typed_value(value))
    if math.isnan(value):
        return FloatCell("nan")
    if math.isinf(value):
        return FloatCell("positive_infinity" if value > 0 else "negative_infinity")
    return FloatCell("finite", value=value)


def float_cells_equal(
    left: FloatCell,
    right: FloatCell,
    tolerance: FloatTolerance,
) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind == "finite":
        if left.value is None or right.value is None:  # pragma: no cover - invariant
            return False
        return math.isclose(
            left.value,
            right.value,
            rel_tol=tolerance.relative,
            abs_tol=tolerance.absolute,
        )
    if left.kind == "typed":
        return left.exact == right.exact
    return True
