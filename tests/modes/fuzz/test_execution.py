from contextlib import contextmanager
from dataclasses import dataclass

from select_fuzz.config import NodeConfig, NodeRole
from select_fuzz.modes.fuzz.execution import StreamingQueryExecutor


class _Cursor:
    columns = ()
    affected_rows = None

    def __init__(self) -> None:
        self._batches = [((1,), (2,)), ((3,),), ()]

    def fetchmany(self, size: int):  # type: ignore[no-untyped-def]
        assert size == 128
        return self._batches.pop(0)

    def warnings(self):  # type: ignore[no-untyped-def]
        return ()

    def close(self) -> None:
        return None


class _Session:
    def connection_id(self) -> int:
        return 7

    def is_alive(self) -> bool:
        return True

    def execute(self, sql: str) -> _Cursor:
        assert sql == "SELECT value FROM t"
        return _Cursor()

    def abort(self) -> None:
        return None

    def close(self) -> None:
        return None


@dataclass
class _Factory:
    @contextmanager
    def query_session(self, node: NodeConfig, database: str):  # type: ignore[no-untyped-def]
        del node, database
        yield _Session()

    control_session = query_session


def test_streaming_executor_discards_values_and_counts_rows() -> None:
    node = NodeConfig(role=NodeRole.CUSTOM_ON, host="127.0.0.1")
    result = StreamingQueryExecutor(_Factory()).execute(
        node,
        "sf_f_case",
        "SELECT value FROM t",
        timeout_seconds=5,
    )

    assert result.success
    assert result.rows_seen == 3
    assert result.error is None
