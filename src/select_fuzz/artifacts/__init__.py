"""Durable correctness artifacts and rebuildable reports."""

from select_fuzz.artifacts.bundle import (
    CaseBundleWriter,
    FindingRecord,
    PassRecord,
    artifact_cell_to_value,
    node_execution_to_artifact,
)
from select_fuzz.artifacts.jsonl import JsonlCorruptionError, JsonlWriter, read_jsonl
from select_fuzz.artifacts.query_log import WorkerQueryLogWriter
from select_fuzz.artifacts.reader import (
    ArtifactReader,
    ArtifactValidationError,
    StoredFinding,
)
from select_fuzz.artifacts.report import HtmlReportBuilder
from select_fuzz.artifacts.sql_script import (
    DEFAULT_SESSION_STATEMENTS,
    MAX_DIFF_BYTES,
    MAX_DIFF_ROWS,
    SourceableSqlWriter,
    WorkerSqlLogWriter,
    compact_result_summary,
    write_difference_summary,
    write_minimal_failure_script,
)

__all__ = [
    "ArtifactReader",
    "ArtifactValidationError",
    "CaseBundleWriter",
    "DEFAULT_SESSION_STATEMENTS",
    "FindingRecord",
    "HtmlReportBuilder",
    "JsonlCorruptionError",
    "JsonlWriter",
    "MAX_DIFF_BYTES",
    "MAX_DIFF_ROWS",
    "PassRecord",
    "StoredFinding",
    "SourceableSqlWriter",
    "WorkerQueryLogWriter",
    "WorkerSqlLogWriter",
    "artifact_cell_to_value",
    "compact_result_summary",
    "node_execution_to_artifact",
    "read_jsonl",
    "write_difference_summary",
    "write_minimal_failure_script",
]
