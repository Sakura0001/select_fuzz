"""Offline isolation of query-shape candidates from untrusted documentation text."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re

from select_fuzz.generation.query_safety import ReadOnlyValidator, UnsafeQuery


class CandidateSafetyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OfflineCandidate:
    sql: str
    executable: bool = False

    def __post_init__(self) -> None:
        if self.executable:
            raise ValueError("network-derived candidates can never be executable")


class _CodeBlockParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tag: str | None = None
        self._parts: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"code", "pre"} and self._tag is None:
            self._tag = tag.lower()
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._tag is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._tag == tag.lower():
            self.blocks.append("".join(self._parts))
            self._tag = None
            self._parts = []


_DENIED_WORDS = frozenset(
    {
        "ALTER",
        "BENCHMARK",
        "CALL",
        "CREATE",
        "DELETE",
        "DO",
        "DROP",
        "DUMPFILE",
        "GET_LOCK",
        "HANDLER",
        "INSERT",
        "INTO",
        "LOAD_FILE",
        "LOCK",
        "OUTFILE",
        "RELEASE_LOCK",
        "RENAME",
        "REPLACE",
        "SET",
        "SLEEP",
        "TRUNCATE",
        "UPDATE",
    }
)
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*|;|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"")
_QUOTED = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"")


def _tokens(sql: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _TOKEN.finditer(sql))


class CandidateExtractor:
    """Accept a narrow read-only envelope without parsing or executing candidate SQL."""

    def from_html(self, content: bytes) -> tuple[OfflineCandidate, ...]:
        parser = _CodeBlockParser()
        parser.feed(content.decode("utf-8", errors="replace"))
        accepted: list[OfflineCandidate] = []
        for block in parser.blocks:
            try:
                accepted.append(self.from_text(block))
            except CandidateSafetyError:
                continue
        return tuple(accepted)

    def from_text(self, sql: str) -> OfflineCandidate:
        normalized = sql.strip()
        unquoted = _QUOTED.sub("''", normalized)
        if ":=" in unquoted or re.search(r"(?<![A-Za-z0-9_])@@?[A-Za-z_]", unquoted):
            raise CandidateSafetyError("candidate contains session variable access or assignment")
        tokens = _tokens(normalized)
        if not tokens:
            raise CandidateSafetyError("empty candidate")
        semicolons = [index for index, token in enumerate(tokens) if token == ";"]
        if semicolons:
            if len(semicolons) != 1 or semicolons[0] != len(tokens) - 1:
                raise CandidateSafetyError("candidate must contain exactly one statement")
            normalized = normalized.rstrip().removesuffix(";").rstrip()
            tokens = tokens[:-1]
        words = tuple(token.upper() for token in tokens if token != ";" and token[:1] not in "'\"")
        if not words or words[0] not in {"SELECT", "WITH", "TABLE", "VALUES"}:
            raise CandidateSafetyError("candidate is not a read-only query expression")
        denied = _DENIED_WORDS.intersection(words)
        if denied or any(
            phrase in " ".join(words)
            for phrase in ("FOR UPDATE", "FOR SHARE", "LOCK IN SHARE MODE")
        ):
            raise CandidateSafetyError("candidate contains denied side-effect syntax")
        try:
            ReadOnlyValidator().validate_text(normalized)
        except UnsafeQuery as exc:
            raise CandidateSafetyError(str(exc)) from exc
        return OfflineCandidate(sql=normalized, executable=False)


__all__ = ["CandidateExtractor", "CandidateSafetyError", "OfflineCandidate"]
