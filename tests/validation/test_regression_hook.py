from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from threading import Event
import time
import sys

import pytest

from select_fuzz.validation.loop import HookContext
from select_fuzz.validation.models import GapRecord, Reachability, ReachabilityResult
from select_fuzz.validation.regression_hook import ExternalRegressionHook


def _gap() -> GapRecord:
    return GapRecord("a" * 64, "P1", Reachability.GAP, ("missing",), datetime.now(UTC))


def _context(seconds: float = 5) -> HookContext:
    return HookContext(1, time.monotonic() + seconds, Event(), time.monotonic)


def test_configured_commands_run_without_shell_then_reaudit(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    calls: list[str] = []

    def reaudit(gap: GapRecord) -> ReachabilityResult:
        calls.append(gap.signature_key)
        return ReachabilityResult(
            gap.signature_key,
            Reachability.SUPPORTED,
            witness_seed=1,
            witness_feature_id="fixed",
        )

    hook = ExternalRegressionHook(
        commands=((sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),),
        timeout_s=3,
        audit_path=tmp_path / "regression.jsonl",
        reaudit=reaudit,
    )
    result = hook.run(_gap(), allow_code_change=True, context=_context())

    assert marker.exists()
    assert result.status is Reachability.SUPPORTED
    assert calls == ["a" * 64]
    audit = json.loads((tmp_path / "regression.jsonl").read_text().splitlines()[0])
    assert audit["shell"] is False
    assert "-c" not in json.dumps(audit)


def test_freeze_skips_commands_but_still_reaudits(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    hook = ExternalRegressionHook(
        commands=((sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"),),
        timeout_s=3,
        audit_path=tmp_path / "audit.jsonl",
        reaudit=lambda gap: ReachabilityResult(
            gap.signature_key, Reachability.GAP, ("still missing",)
        ),
    )
    result = hook.run(_gap(), allow_code_change=False, context=_context())
    assert not marker.exists()
    assert result.status is Reachability.GAP


def test_command_timeout_is_bounded_by_hook_and_global_deadline(tmp_path: Path) -> None:
    hook = ExternalRegressionHook(
        commands=((sys.executable, "-c", "import time; time.sleep(2)"),),
        timeout_s=0.05,
        audit_path=tmp_path / "audit.jsonl",
        reaudit=lambda gap: ReachabilityResult(gap.signature_key, Reachability.GAP, ("x",)),
    )
    with pytest.raises(TimeoutError):
        hook.run(_gap(), allow_code_change=True, context=_context())
