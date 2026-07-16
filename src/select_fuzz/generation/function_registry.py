"""Closed registry of deterministic MySQL 8.0.41 scalar function signatures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_FUNCTION_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class FunctionFamily(StrEnum):
    MATH = "math"
    STRING = "string"
    TEMPORAL = "temporal"
    CONTROL = "control"
    ENCODING = "encoding"
    NETWORK = "network"


class FunctionArgument(StrEnum):
    NUMBER = "number"
    UNIT_NUMBER = "unit_number"
    INTEGER = "integer"
    INTEGER_TWO = "integer_two"
    INTEGER_THREE = "integer_three"
    BASE_SIXTEEN = "base_sixteen"
    TEXT = "text"
    TEXT_ALT = "text_alt"
    SQL_TEXT = "sql_text"
    SEPARATOR = "separator"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    PERIOD = "period"
    YEAR_NUMBER = "year_number"
    DAY_NUMBER = "day_number"
    SHA_BITS = "sha_bits"
    BASE64_TEXT = "base64_text"
    HEX_TEXT = "hex_text"
    IPV4_TEXT = "ipv4_text"
    IPV4_NUMBER = "ipv4_number"
    IPV6_TEXT = "ipv6_text"
    IPV6_BINARY = "ipv6_binary"


class FunctionResult(StrEnum):
    NUMERIC = "numeric"
    TEXT = "text"
    BINARY = "binary"
    TEMPORAL = "temporal"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class DeterministicFunctionSignature:
    signature_id: str
    family: FunctionFamily
    sql_name: str
    arguments: tuple[FunctionArgument, ...]
    result: FunctionResult
    null_argument_positions: frozenset[int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", tuple(self.arguments))
        if _IDENTIFIER.fullmatch(self.signature_id) is None:
            raise ValueError("signature_id must be a snake_case identifier")
        if not isinstance(self.family, FunctionFamily):
            raise TypeError("family must be a FunctionFamily")
        if _FUNCTION_NAME.fullmatch(self.sql_name) is None:
            raise ValueError("sql_name must be an unqualified uppercase function name")
        if any(not isinstance(argument, FunctionArgument) for argument in self.arguments):
            raise TypeError("arguments must use FunctionArgument recipes")
        if not isinstance(self.result, FunctionResult):
            raise TypeError("result must be a FunctionResult")
        if any(
            not isinstance(position, int)
            or isinstance(position, bool)
            or not 0 <= position < len(self.arguments)
            for position in self.null_argument_positions
        ):
            raise ValueError("null argument position is outside the function arity")


def _signature(
    family: FunctionFamily,
    sql_name: str,
    arguments: tuple[FunctionArgument, ...],
    result: FunctionResult,
    *,
    suffix: str | None = None,
) -> DeterministicFunctionSignature:
    identifier_suffix = suffix or str(len(arguments))
    signature_id = f"{family.value}_{sql_name.lower()}_{identifier_suffix}"
    return DeterministicFunctionSignature(
        signature_id=signature_id,
        family=family,
        sql_name=sql_name,
        arguments=arguments,
        result=result,
        null_argument_positions=frozenset(range(len(arguments))),
    )


N = FunctionArgument.NUMBER
U = FunctionArgument.UNIT_NUMBER
INT = FunctionArgument.INTEGER
INT2 = FunctionArgument.INTEGER_TWO
INT3 = FunctionArgument.INTEGER_THREE
B16 = FunctionArgument.BASE_SIXTEEN
T = FunctionArgument.TEXT
T2 = FunctionArgument.TEXT_ALT
SQL = FunctionArgument.SQL_TEXT
SEP = FunctionArgument.SEPARATOR
D = FunctionArgument.DATE
DT = FunctionArgument.DATETIME
TM = FunctionArgument.TIME
P = FunctionArgument.PERIOD
YR = FunctionArgument.YEAR_NUMBER
DAYS = FunctionArgument.DAY_NUMBER
SHA_BITS = FunctionArgument.SHA_BITS
B64 = FunctionArgument.BASE64_TEXT
HX = FunctionArgument.HEX_TEXT
IP4 = FunctionArgument.IPV4_TEXT
IP4N = FunctionArgument.IPV4_NUMBER
IP6 = FunctionArgument.IPV6_TEXT
IP6B = FunctionArgument.IPV6_BINARY
NUM = FunctionResult.NUMERIC
TXT = FunctionResult.TEXT
BIN = FunctionResult.BINARY
TEMP = FunctionResult.TEMPORAL
BOOL = FunctionResult.BOOLEAN


DETERMINISTIC_FUNCTION_SIGNATURES: tuple[DeterministicFunctionSignature, ...] = (
    # Numeric functions use bounded literals that stay inside every documented domain.
    _signature(FunctionFamily.MATH, "ABS", (N,), NUM),
    _signature(FunctionFamily.MATH, "ACOS", (U,), NUM),
    _signature(FunctionFamily.MATH, "ASIN", (U,), NUM),
    _signature(FunctionFamily.MATH, "ATAN", (N,), NUM),
    _signature(FunctionFamily.MATH, "ATAN", (N, INT2), NUM),
    _signature(FunctionFamily.MATH, "ATAN2", (N, INT2), NUM),
    _signature(FunctionFamily.MATH, "BIT_COUNT", (INT,), NUM),
    _signature(FunctionFamily.MATH, "CEIL", (N,), NUM),
    _signature(FunctionFamily.MATH, "CEILING", (N,), NUM),
    _signature(FunctionFamily.MATH, "CONV", (HX, B16, INT), TXT),
    _signature(FunctionFamily.MATH, "COS", (N,), NUM),
    _signature(FunctionFamily.MATH, "COT", (U,), NUM),
    _signature(FunctionFamily.MATH, "CRC32", (T,), NUM),
    _signature(FunctionFamily.MATH, "DEGREES", (N,), NUM),
    _signature(FunctionFamily.MATH, "EXP", (U,), NUM),
    _signature(FunctionFamily.MATH, "FLOOR", (N,), NUM),
    _signature(FunctionFamily.MATH, "LN", (INT2,), NUM),
    _signature(FunctionFamily.MATH, "LOG", (INT2,), NUM),
    _signature(FunctionFamily.MATH, "LOG", (INT2, INT3), NUM),
    _signature(FunctionFamily.MATH, "LOG10", (INT2,), NUM),
    _signature(FunctionFamily.MATH, "LOG2", (INT2,), NUM),
    _signature(FunctionFamily.MATH, "MOD", (INT, INT2), NUM),
    _signature(FunctionFamily.MATH, "PI", (), NUM, suffix="0"),
    _signature(FunctionFamily.MATH, "POW", (INT2, INT3), NUM),
    _signature(FunctionFamily.MATH, "POWER", (INT2, INT3), NUM),
    _signature(FunctionFamily.MATH, "RADIANS", (N,), NUM),
    _signature(FunctionFamily.MATH, "ROUND", (N,), NUM),
    _signature(FunctionFamily.MATH, "ROUND", (N, INT2), NUM),
    _signature(FunctionFamily.MATH, "SIGN", (N,), NUM),
    _signature(FunctionFamily.MATH, "SIN", (N,), NUM),
    _signature(FunctionFamily.MATH, "SQRT", (INT2,), NUM),
    _signature(FunctionFamily.MATH, "TAN", (N,), NUM),
    _signature(FunctionFamily.MATH, "TRUNCATE", (N, INT2), NUM),
    # String/binary signatures avoid locale-dependent forms and use ASCII fixtures.
    _signature(FunctionFamily.STRING, "ASCII", (T,), NUM),
    _signature(FunctionFamily.STRING, "BIN", (INT,), TXT),
    _signature(FunctionFamily.STRING, "BIT_LENGTH", (T,), NUM),
    _signature(FunctionFamily.STRING, "CHAR_LENGTH", (T,), NUM),
    _signature(FunctionFamily.STRING, "CHARACTER_LENGTH", (T,), NUM),
    _signature(FunctionFamily.STRING, "CONCAT", (T, T2), TXT),
    _signature(FunctionFamily.STRING, "CONCAT", (T, T2, SEP), TXT),
    _signature(FunctionFamily.STRING, "CONCAT_WS", (SEP, T, T2), TXT),
    _signature(FunctionFamily.STRING, "ELT", (INT2, T, T2), TXT),
    _signature(FunctionFamily.STRING, "EXPORT_SET", (INT, T, T2), TXT),
    _signature(FunctionFamily.STRING, "FIELD", (T, T2, T), NUM),
    _signature(FunctionFamily.STRING, "FIND_IN_SET", (T2, T), NUM),
    _signature(FunctionFamily.STRING, "FROM_BASE64", (B64,), BIN),
    _signature(FunctionFamily.STRING, "HEX", (T,), TXT),
    _signature(FunctionFamily.STRING, "INSTR", (T, T2), NUM),
    _signature(FunctionFamily.STRING, "LCASE", (T,), TXT),
    _signature(FunctionFamily.STRING, "LEFT", (T, INT3), TXT),
    _signature(FunctionFamily.STRING, "LENGTH", (T,), NUM),
    _signature(FunctionFamily.STRING, "LOCATE", (T2, T), NUM),
    _signature(FunctionFamily.STRING, "LOCATE", (T2, T, INT2), NUM),
    _signature(FunctionFamily.STRING, "LOWER", (T,), TXT),
    _signature(FunctionFamily.STRING, "LPAD", (T, INT, T2), TXT),
    _signature(FunctionFamily.STRING, "LTRIM", (T,), TXT),
    _signature(FunctionFamily.STRING, "MAKE_SET", (INT, T, T2), TXT),
    _signature(FunctionFamily.STRING, "MID", (T, INT2), TXT),
    _signature(FunctionFamily.STRING, "MID", (T, INT2, INT3), TXT),
    _signature(FunctionFamily.STRING, "OCT", (INT,), TXT),
    _signature(FunctionFamily.STRING, "OCTET_LENGTH", (T,), NUM),
    _signature(FunctionFamily.STRING, "ORD", (T,), NUM),
    _signature(FunctionFamily.STRING, "QUOTE", (T,), TXT),
    _signature(FunctionFamily.STRING, "REPEAT", (T, INT2), TXT),
    _signature(FunctionFamily.STRING, "REPLACE", (T, T2, SEP), TXT),
    _signature(FunctionFamily.STRING, "REVERSE", (T,), TXT),
    _signature(FunctionFamily.STRING, "RIGHT", (T, INT3), TXT),
    _signature(FunctionFamily.STRING, "RPAD", (T, INT, T2), TXT),
    _signature(FunctionFamily.STRING, "RTRIM", (T,), TXT),
    _signature(FunctionFamily.STRING, "SOUNDEX", (T,), TXT),
    _signature(FunctionFamily.STRING, "SPACE", (INT3,), TXT),
    _signature(FunctionFamily.STRING, "STRCMP", (T, T2), NUM),
    _signature(FunctionFamily.STRING, "SUBSTR", (T, INT2), TXT),
    _signature(FunctionFamily.STRING, "SUBSTR", (T, INT2, INT3), TXT),
    _signature(FunctionFamily.STRING, "SUBSTRING", (T, INT2), TXT),
    _signature(FunctionFamily.STRING, "SUBSTRING", (T, INT2, INT3), TXT),
    _signature(FunctionFamily.STRING, "SUBSTRING_INDEX", (T, SEP, INT2), TXT),
    _signature(FunctionFamily.STRING, "TO_BASE64", (T,), TXT),
    _signature(FunctionFamily.STRING, "TRIM", (T,), TXT),
    _signature(FunctionFamily.STRING, "UCASE", (T,), TXT),
    _signature(FunctionFamily.STRING, "UNHEX", (HX,), BIN),
    _signature(FunctionFamily.STRING, "UPPER", (T,), TXT),
    # Temporal functions always receive explicit values; no current-time form is present.
    _signature(FunctionFamily.TEMPORAL, "DATE", (DT,), TEMP),
    _signature(FunctionFamily.TEMPORAL, "DATEDIFF", (D, D), NUM),
    _signature(FunctionFamily.TEMPORAL, "DAY", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "DAYOFMONTH", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "DAYOFWEEK", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "DAYOFYEAR", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "FROM_DAYS", (DAYS,), TEMP),
    _signature(FunctionFamily.TEMPORAL, "HOUR", (TM,), NUM),
    _signature(FunctionFamily.TEMPORAL, "LAST_DAY", (D,), TEMP),
    _signature(FunctionFamily.TEMPORAL, "MAKEDATE", (YR, INT2), TEMP),
    _signature(FunctionFamily.TEMPORAL, "MAKETIME", (INT, INT2, INT3), TEMP),
    _signature(FunctionFamily.TEMPORAL, "MICROSECOND", (DT,), NUM),
    _signature(FunctionFamily.TEMPORAL, "MINUTE", (TM,), NUM),
    _signature(FunctionFamily.TEMPORAL, "MONTH", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "PERIOD_ADD", (P, INT2), NUM),
    _signature(FunctionFamily.TEMPORAL, "PERIOD_DIFF", (P, P), NUM),
    _signature(FunctionFamily.TEMPORAL, "QUARTER", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "SECOND", (TM,), NUM),
    _signature(FunctionFamily.TEMPORAL, "SEC_TO_TIME", (INT,), TEMP),
    _signature(FunctionFamily.TEMPORAL, "TIME", (DT,), TEMP),
    _signature(FunctionFamily.TEMPORAL, "TIME_TO_SEC", (TM,), NUM),
    _signature(FunctionFamily.TEMPORAL, "TIMEDIFF", (TM, TM), TEMP),
    _signature(FunctionFamily.TEMPORAL, "TIMESTAMP", (D,), TEMP),
    _signature(FunctionFamily.TEMPORAL, "TIMESTAMP", (D, TM), TEMP),
    _signature(FunctionFamily.TEMPORAL, "TO_DAYS", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "TO_SECONDS", (DT,), NUM),
    _signature(FunctionFamily.TEMPORAL, "WEEK", (D, INT3), NUM),
    _signature(FunctionFamily.TEMPORAL, "WEEKDAY", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "WEEKOFYEAR", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "YEAR", (D,), NUM),
    _signature(FunctionFamily.TEMPORAL, "YEARWEEK", (D, INT3), NUM),
    # Control-flow forms include a deterministic NULL fixture during generation.
    _signature(FunctionFamily.CONTROL, "COALESCE", (T, T2, SEP), TXT),
    _signature(FunctionFamily.CONTROL, "GREATEST", (INT, INT2, INT3), NUM),
    _signature(FunctionFamily.CONTROL, "IF", (INT, T, T2), TXT),
    _signature(FunctionFamily.CONTROL, "IFNULL", (T, T2), TXT),
    _signature(FunctionFamily.CONTROL, "ISNULL", (T,), BOOL),
    _signature(FunctionFamily.CONTROL, "LEAST", (INT, INT2, INT3), NUM),
    _signature(FunctionFamily.CONTROL, "NULLIF", (T, T2), TXT),
    # Hash/encoding functions have fixed payloads and no external state.
    _signature(FunctionFamily.ENCODING, "MD5", (T,), TXT),
    _signature(FunctionFamily.ENCODING, "SHA1", (T,), TXT),
    _signature(FunctionFamily.ENCODING, "SHA2", (T, SHA_BITS), TXT),
    _signature(FunctionFamily.ENCODING, "STATEMENT_DIGEST", (SQL,), TXT),
    _signature(FunctionFamily.ENCODING, "STATEMENT_DIGEST_TEXT", (SQL,), TXT),
    # Network conversion and classification use documentation-reserved addresses.
    _signature(FunctionFamily.NETWORK, "INET_ATON", (IP4,), NUM),
    _signature(FunctionFamily.NETWORK, "INET_NTOA", (IP4N,), TXT),
    _signature(FunctionFamily.NETWORK, "INET6_ATON", (IP6,), BIN),
    _signature(FunctionFamily.NETWORK, "INET6_NTOA", (IP6B,), TXT),
    _signature(FunctionFamily.NETWORK, "IS_IPV4", (IP4,), BOOL),
    _signature(FunctionFamily.NETWORK, "IS_IPV4_COMPAT", (IP6B,), BOOL),
    _signature(FunctionFamily.NETWORK, "IS_IPV4_MAPPED", (IP6B,), BOOL),
    _signature(FunctionFamily.NETWORK, "IS_IPV6", (IP6,), BOOL),
)


if len({item.signature_id for item in DETERMINISTIC_FUNCTION_SIGNATURES}) != len(
    DETERMINISTIC_FUNCTION_SIGNATURES
):  # pragma: no cover - import-time registry invariant
    raise RuntimeError("deterministic function signature IDs must be unique")


__all__ = [
    "DETERMINISTIC_FUNCTION_SIGNATURES",
    "DeterministicFunctionSignature",
    "FunctionArgument",
    "FunctionFamily",
    "FunctionResult",
]
