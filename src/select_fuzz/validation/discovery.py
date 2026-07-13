"""Persistent official-source frontier seeded by the checked-in MySQL catalog."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from threading import Event
from collections.abc import Callable, Iterable
from urllib.parse import urldefrag, urljoin

import yaml

from select_fuzz.validation.ledger import ValidationLedger
from select_fuzz.validation.models import is_official_source_url


OFFICIAL_DIRECTORY_ROOTS = (
    "https://dev.mysql.com/doc/refman/8.0/en/sql-statements.html",
    "https://dev.mysql.com/doc/refman/8.0/en/built-in-function-reference.html",
    "https://dev.mysql.com/doc/mysqld-version-reference/en/built-in-functions.html",
)


@dataclass(frozen=True, slots=True)
class QueuedSource:
    url: str
    attempt: int


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value)


def extract_official_links(content: bytes, *, base_url: str) -> tuple[str, ...]:
    parser = _LinkParser()
    parser.feed(content.decode("utf-8", errors="replace"))
    resolved = (urldefrag(urljoin(base_url, link)).url for link in parser.links)
    accepted = {url for url in resolved if is_official_source_url(url)}
    return tuple(sorted(accepted))


def _catalog_urls(path: Path | None) -> tuple[str, ...]:
    if path is None:
        packaged = resources.files("select_fuzz").joinpath(
            "data", "mysql-8.0.41-query-shapes.yaml"
        )
        if packaged.is_file():
            with resources.as_file(packaged) as packaged_path:
                document = yaml.safe_load(packaged_path.read_text())
        else:
            checkout = (
                Path(__file__).resolve().parents[3]
                / "catalog"
                / "mysql-8.0.41-query-shapes.yaml"
            )
            if not checkout.is_file():
                return ()
            document = yaml.safe_load(checkout.read_text())
    else:
        document = yaml.safe_load(path.read_text())
    sources = document.get("sources", []) if isinstance(document, dict) else []
    return tuple(
        source["url"]
        for source in sources
        if isinstance(source, dict)
        and isinstance(source.get("url"), str)
        and is_official_source_url(source["url"])
    )


class PersistentSourceDiscovery:
    def __init__(self, ledger: ValidationLedger, *, catalog_path: Path | None = None) -> None:
        self.ledger = ledger
        self.catalog_path = catalog_path
        self.ledger.recover_claimed_sources()

    def seed(self) -> int:
        inserted = 0
        for url in (*_catalog_urls(self.catalog_path), *OFFICIAL_DIRECTORY_ROOTS):
            inserted += self.ledger.enqueue_source(url, discovered_from="seed_catalog")
        return inserted

    def add_links(self, links: Iterable[str], *, discovered_from: str) -> int:
        inserted = 0
        for url in links:
            if is_official_source_url(url):
                inserted += self.ledger.enqueue_source(url, discovered_from=discovered_from)
        return inserted

    def next(
        self,
        *,
        deadline_monotonic: float,
        monotonic: Callable[[], float],
        stop_event: Event,
    ) -> QueuedSource | None:
        if stop_event.is_set() or monotonic() >= deadline_monotonic:
            return None
        claimed = self.ledger.claim_source()
        return None if claimed is None else QueuedSource(*claimed)

    def complete(self, url: str) -> None:
        self.ledger.complete_source(url)

    def retry(self, url: str, *, error: str) -> None:
        self.ledger.retry_source(url, error=error)


__all__ = [
    "OFFICIAL_DIRECTORY_ROOTS",
    "PersistentSourceDiscovery",
    "QueuedSource",
    "extract_official_links",
]
