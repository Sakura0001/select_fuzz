"""Opaque, traversal-safe artifact lookups."""

from __future__ import annotations

from dataclasses import dataclass
import mimetypes
from pathlib import Path
import re


_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    path: Path
    media_type: str
    filename: str


class SafeArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def valid_id(value: str) -> bool:
        return _OPAQUE_ID.fullmatch(value) is not None

    def finding_manifest(self, finding_id: str) -> Path | None:
        if not self.valid_id(finding_id):
            return None
        candidate = self.root / "findings" / finding_id / "manifest.json"
        return candidate if candidate.is_file() else None

    def report(self, report_id: str) -> ArtifactRef | None:
        if not self.valid_id(report_id):
            return None
        reports = self.root / "reports"
        for suffix in (".html", ".json", ".jsonl"):
            candidate = reports / f"{report_id}{suffix}"
            if candidate.is_file():
                media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
                return ArtifactRef(candidate, media_type, candidate.name)
        return None

    def list_reports(self) -> list[dict[str, str]]:
        reports = self.root / "reports"
        if not reports.is_dir():
            return []
        return [
            {"id": path.stem, "filename": path.name}
            for path in sorted(reports.iterdir(), reverse=True)
            if path.is_file() and self.valid_id(path.stem)
        ]
