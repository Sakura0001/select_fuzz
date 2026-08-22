from __future__ import annotations

from select_fuzz.execution.evidence import capture_exception_evidence


class _PartialConnectorError(RuntimeError):
    def __init__(self) -> None:
        self.errno = 2013
        self.sqlstate = None
        self.msg = None
        super().__init__("raw connect failure")


def test_shared_evidence_preserves_partial_connector_fields() -> None:
    evidence = capture_exception_evidence(_PartialConnectorError(), "connect")

    assert evidence["failure_stage"] == "connect"
    assert evidence["exception"]["type"] == "_PartialConnectorError"
    assert evidence["exception"]["errno"] == 2013
    assert evidence["exception"]["sqlstate"] is None
    assert evidence["exception"]["connector_message"] is None
    assert evidence["exception"]["message"] == "raw connect failure"
