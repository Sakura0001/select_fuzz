"""Rebuildable static HTML reports."""

from __future__ import annotations

from collections.abc import Callable
from html import escape
import json
import os
from pathlib import Path
from uuid import uuid4

from select_fuzz.artifacts.reader import ArtifactReader


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:  # pragma: no cover - OS contract defense
            raise OSError("report write returned no progress")
        offset += written


def _fsync_directory(path: Path, fsync: Callable[[int], None]) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        fsync(descriptor)
    finally:
        os.close(descriptor)


class HtmlReportBuilder:
    def __init__(
        self,
        reader: ArtifactReader,
        *,
        fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        self._reader = reader
        self._fsync = fsync

    def render(self) -> str:
        events = self._reader.events()
        case_events = [
            event for event in events if event.get("type") in {"pass", "finding"}
        ]
        findings = sum(event.get("type") == "finding" for event in case_events)
        rows = []
        for index, event in enumerate(events, start=1):
            detail = json.dumps(
                event,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            rows.append(
                "<tr>"
                f"<td>{index}</td>"
                f"<td>{escape(str(event.get('type', 'unknown')))}</td>"
                f"<td>{escape(str(event.get('case_id', '')))}</td>"
                f"<td><code>{escape(detail)}</code></td>"
                "</tr>"
            )
        return (
            "<!doctype html><html lang=\"en\"><head>"
            "<meta charset=\"utf-8\">"
            "<meta http-equiv=\"Content-Security-Policy\" "
            "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Select Fuzz Report</title>"
            "<style>body{font-family:system-ui;margin:2rem}table{border-collapse:collapse;"
            "width:100%}th,td{border:1px solid #ccc;padding:.4rem;text-align:left}"
            "code{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body>"
            "<h1>Select Fuzz Report</h1>"
            f"<p>{len(case_events)} total cases · {findings} findings</p>"
            "<table><thead><tr><th>#</th><th>Type</th><th>Case</th><th>Event</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
            "</body></html>"
        )

    def write(self, path: str | Path) -> Path:
        destination = Path(path)
        payload = self.render().encode("utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.tmp-{uuid4().hex}"
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        try:
            _write_all(descriptor, payload)
            self._fsync(descriptor)
        except Exception:
            os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)
        try:
            os.replace(temporary, destination)
            _fsync_directory(destination.parent, self._fsync)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination


__all__ = ["HtmlReportBuilder"]
