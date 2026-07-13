from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from select_fuzz.domain import ColumnMeta
from select_fuzz.oracle.canonical import (
    MYSQL_FLAG_SET,
    MYSQL_TYPE_BIT,
    MYSQL_TYPE_DOUBLE,
    MYSQL_TYPE_FLOAT,
    MYSQL_TYPE_GEOMETRY,
    MYSQL_TYPE_JSON,
    FloatCell,
    FloatTolerance,
    _bit_value,
    _canonical_json,
    _decimal_value,
    _json_object,
    _json_value,
    _set_value,
    _spatial_value,
    _typed_value,
    canonical_float_cell,
    canonical_group_value,
    float_cells_equal,
    tolerance_for,
)
from select_fuzz.oracle.errors import CanonicalizationError


def column(type_code: int, *, flags: int | None = None) -> ColumnMeta:
    return ColumnMeta("value", type_code, True, False, False, flags=flags)


def test_decimal_canonicalization_handles_special_zero_and_trailing_zero_values() -> None:
    assert _decimal_value(Decimal("NaN"))[0] == "decimal_nan"
    assert _decimal_value(Decimal("-Infinity")) == ("decimal_infinity", True)
    assert _decimal_value(Decimal("0.000")) == ("decimal", 0, (0,), 0)
    assert _decimal_value(Decimal("12.300")) == ("decimal", 0, (1, 2, 3), -1)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        3,
        Decimal("1.20"),
        "text",
        b"bytes",
        memoryview(b"view"),
        datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        date(2024, 1, 2),
        time(3, 4, 5, 6, tzinfo=timezone.utc),
        timedelta(days=1, seconds=2, microseconds=3),
        float("nan"),
        float("inf"),
        1.25,
        {"b": 2, "a": (1, None)},
        (1, "two"),
    ],
)
def test_typed_wire_values_have_stable_lossless_canonical_forms(value: object) -> None:
    assert _typed_value(value) is not None


def test_typed_wire_values_reject_ambiguous_and_unknown_types() -> None:
    with pytest.raises(CanonicalizationError, match="string keys"):
        _typed_value({1: "bad"})
    with pytest.raises(CanonicalizationError, match="unsupported wire value"):
        _typed_value([1, 2])


@pytest.mark.parametrize(
    "value",
    [
        {"key": [None, True, 1, Decimal("1.2"), 1.25, "text"]},
        (1, 2),
        [1, 2],
        None,
        True,
        2,
        Decimal("1.20"),
        1.25,
        "text",
    ],
)
def test_json_values_cover_every_permitted_json_scalar_and_container(value: object) -> None:
    assert _canonical_json(value) is not None


def test_json_values_reject_duplicate_keys_nonfinite_numbers_and_unknown_types() -> None:
    with pytest.raises(CanonicalizationError, match="duplicate keys"):
        _canonical_json(_json_object([("key", 1), ("key", 2)]))
    with pytest.raises(CanonicalizationError, match="keys must be strings"):
        _canonical_json({1: "bad"})
    with pytest.raises(CanonicalizationError, match="Decimal NaN"):
        _canonical_json(Decimal("NaN"))
    with pytest.raises(CanonicalizationError, match="NaN or infinity"):
        _canonical_json(float("inf"))
    with pytest.raises(CanonicalizationError, match="unsupported JSON"):
        _canonical_json({1, 2})


def test_json_wire_parser_accepts_utf8_and_rejects_bad_bytes_or_syntax() -> None:
    assert _json_value(b'{"value": 1}')[0] == "json"
    with pytest.raises(CanonicalizationError, match="must be UTF-8"):
        _json_value(b"\xff")
    with pytest.raises(CanonicalizationError, match="invalid JSON"):
        _json_value("{")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, ("bit", 1)),
        (3, ("bit", 3)),
        (memoryview(b"\x01"), ("bit", 1)),
        (b"\x01\x00", ("bit", 256)),
    ],
)
def test_bit_wire_values(value: object, expected: object) -> None:
    assert _bit_value(value) == expected


@pytest.mark.parametrize("value", [-1, 2**64, b"", b"123456789", "1"])
def test_bit_wire_values_reject_out_of_range_or_wrong_representations(value: object) -> None:
    with pytest.raises(CanonicalizationError):
        _bit_value(value)


def test_spatial_wire_values_accept_bytes_mapping_tuple_and_memoryview() -> None:
    wkb = b"\x01\x01\x00\x00\x00"
    raw = (4326).to_bytes(4, "little") + wkb
    assert _spatial_value(raw) == ("spatial", 4326, wkb)
    assert _spatial_value({"srid": 4326, "wkb": memoryview(wkb)}) == (
        "spatial",
        4326,
        wkb,
    )
    assert _spatial_value((0, wkb)) == ("spatial", 0, wkb)
    assert _spatial_value(memoryview(raw)) == ("spatial", 4326, wkb)


@pytest.mark.parametrize(
    "value",
    [
        b"short",
        {"srid": 0},
        "geometry",
        (True, b"\x01\x01\x00\x00\x00"),
        (2**32, b"\x01\x01\x00\x00\x00"),
        (0, b"bad"),
        (0, b"\x02\x01\x00\x00\x00"),
    ],
)
def test_spatial_wire_values_reject_malformed_representations(value: object) -> None:
    with pytest.raises(CanonicalizationError):
        _spatial_value(value)


def test_set_and_type_dispatch_paths() -> None:
    assert _set_value({"a", b"b"})[0] == "set"
    with pytest.raises(CanonicalizationError, match="returned as a set"):
        _set_value(["a"])
    with pytest.raises(CanonicalizationError, match="strings or bytes"):
        _set_value({1})

    assert canonical_group_value(None, column(0)) == ("null",)
    assert canonical_group_value({"a"}, column(0, flags=MYSQL_FLAG_SET))[0] == "set"
    assert canonical_group_value(1, column(MYSQL_TYPE_BIT)) == ("bit", 1)
    assert canonical_group_value("1", column(MYSQL_TYPE_JSON))[0] == "json"
    wkb = b"\x01\x01\x00\x00\x00"
    assert canonical_group_value((0, wkb), column(MYSQL_TYPE_GEOMETRY))[0] == "spatial"
    assert canonical_group_value("plain", column(0)) == ("str", "plain")


def test_float_tolerances_cells_sorting_and_equality_paths() -> None:
    assert tolerance_for(column(MYSQL_TYPE_FLOAT)).absolute == 1e-6
    assert tolerance_for(column(MYSQL_TYPE_DOUBLE)).relative == 1e-9
    with pytest.raises(CanonicalizationError, match="not FLOAT"):
        tolerance_for(column(0))

    cells = [
        canonical_float_cell(None),
        canonical_float_cell(Decimal("1")),
        canonical_float_cell(float("nan")),
        canonical_float_cell(float("inf")),
        canonical_float_cell(float("-inf")),
        canonical_float_cell(1.0),
    ]
    assert [cell.kind for cell in sorted(cells, key=FloatCell.sort_key)] == [
        "null",
        "negative_infinity",
        "finite",
        "positive_infinity",
        "nan",
        "typed",
    ]

    tolerance = FloatTolerance(absolute=0.01, relative=0.01)
    assert not float_cells_equal(FloatCell("null"), FloatCell("finite", 0.0), tolerance)
    assert float_cells_equal(FloatCell("finite", 1.0), FloatCell("finite", 1.001), tolerance)
    assert float_cells_equal(
        FloatCell("typed", exact=("int", 1)),
        FloatCell("typed", exact=("int", 1)),
        tolerance,
    )
    assert float_cells_equal(FloatCell("nan"), FloatCell("nan"), tolerance)
