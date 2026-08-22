"""Bounded JSON-safe exception evidence shared by every execution mode."""

from __future__ import annotations

from types import TracebackType


_TEXT_LIMIT = 4096
_CHAIN_LIMIT = 8
_TRACEBACK_FRAME_LIMIT = 32


def _bounded_text(value: object, limit: int = _TEXT_LIMIT) -> str:
    try:
        rendered = str(value)
    except Exception as error:  # pragma: no cover - hostile exception rendering
        rendered = f"<{type(value).__name__} str failed: {type(error).__name__}>"
    return rendered[:limit]


def _bounded_repr(value: object, limit: int = _TEXT_LIMIT) -> str:
    try:
        rendered = repr(value)
    except Exception as error:  # pragma: no cover - hostile exception rendering
        rendered = f"<{type(value).__name__} repr failed: {type(error).__name__}>"
    return rendered[:limit]


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_text(value: object) -> str | None:
    return _bounded_text(value) if isinstance(value, str) else None


def _exception_item(error: BaseException, relation: str) -> dict[str, object]:
    return {
        "module": type(error).__module__,
        "type": type(error).__name__,
        "message": _bounded_text(error),
        "repr": _bounded_repr(error),
        "args": tuple(_bounded_text(value) for value in error.args[:16]),
        "errno": _optional_int(getattr(error, "errno", None)),
        "sqlstate": _optional_text(getattr(error, "sqlstate", None)),
        "connector_message": _optional_text(getattr(error, "msg", None)),
        "relation": relation,
    }


def _traceback_frames(traceback_value: TracebackType | None) -> tuple[dict[str, object], ...]:
    frames: list[dict[str, object]] = []
    current = traceback_value
    while current is not None and len(frames) < _TRACEBACK_FRAME_LIMIT:
        frame = current.tb_frame
        frames.append(
            {
                "file": frame.f_code.co_filename,
                "line": current.tb_lineno,
                "function": frame.f_code.co_name,
            }
        )
        current = current.tb_next
    return tuple(frames)


def capture_exception_evidence(
    error: BaseException,
    failure_stage: str,
) -> dict[str, object]:
    """Capture bounded evidence without retaining live traceback frames."""

    if not failure_stage:
        raise ValueError("failure_stage must be nonempty")
    chain: list[dict[str, object]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    relation = "root"
    while current is not None and len(chain) < _CHAIN_LIMIT and id(current) not in seen:
        seen.add(id(current))
        chain.append(_exception_item(current, relation))
        if current.__cause__ is not None:
            current = current.__cause__
            relation = "cause"
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
            relation = "context"
        else:
            current = None
    return {
        "failure_stage": failure_stage,
        "exception": dict(chain[0]),
        "exception_chain": tuple(chain),
        "traceback_frames": _traceback_frames(error.__traceback__),
    }


def render_traceback_text(evidence: dict[str, object]) -> str:
    """Render captured frames and exception chain without retaining frames."""

    lines = ["Traceback (most recent call last):"]
    raw_frames = evidence.get("traceback_frames")
    if isinstance(raw_frames, (tuple, list)):
        for raw_frame in raw_frames[:_TRACEBACK_FRAME_LIMIT]:
            frame = raw_frame if isinstance(raw_frame, dict) else {}
            lines.append(
                f'  File "{_bounded_text(frame.get("file", ""))}", '
                f'line {frame.get("line", 0)}, in '
                f'{_bounded_text(frame.get("function", ""))}'
            )
    raw_chain = evidence.get("exception_chain")
    if isinstance(raw_chain, (tuple, list)):
        for raw_item in raw_chain[:_CHAIN_LIMIT]:
            item = raw_item if isinstance(raw_item, dict) else {}
            lines.append(
                f'{_bounded_text(item.get("module", ""))}.'
                f'{_bounded_text(item.get("type", "Exception"))}: '
                f'{_bounded_text(item.get("message", ""))}'
            )
    return "\n".join(lines)[:16_384]


__all__ = ["capture_exception_evidence", "render_traceback_text"]
