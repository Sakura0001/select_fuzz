"""Audited operator-configured regression commands followed by mandatory re-audit."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import subprocess

from select_fuzz.validation.loop import HookContext
from select_fuzz.validation.models import GapRecord, ReachabilityResult


class ExternalRegressionHook:
    def __init__(
        self,
        *,
        commands: tuple[tuple[str, ...], ...],
        timeout_s: float,
        audit_path: Path,
        reaudit: Callable[[GapRecord], ReachabilityResult],
        env: Mapping[str, str] | None = None,
    ) -> None:
        if timeout_s <= 0 or any(not command or not all(command) for command in commands):
            raise ValueError("commands must be non-empty argv tuples and timeout_s positive")
        self.commands = commands
        self.timeout_s = timeout_s
        self.audit_path = audit_path
        self.reaudit = reaudit
        self.env = None if env is None else dict(env)
        audit_path.parent.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        gap: GapRecord,
        *,
        allow_code_change: bool,
        context: HookContext,
    ) -> ReachabilityResult:
        if allow_code_change:
            for index, command in enumerate(self.commands):
                if not context.active():
                    raise TimeoutError("validation deadline reached before regression command")
                timeout = min(self.timeout_s, context.remaining_s)
                try:
                    completed = subprocess.run(
                        command,
                        shell=False,
                        check=False,
                        capture_output=True,
                        timeout=timeout,
                        env=self.env,
                    )
                except subprocess.TimeoutExpired as exc:
                    self._audit(gap, index, command, "timeout", None)
                    raise TimeoutError("regression command exceeded its deadline") from exc
                status = "passed" if completed.returncode == 0 else "failed"
                self._audit(gap, index, command, status, completed.returncode)
                if completed.returncode != 0:
                    raise RuntimeError(
                        f"regression command {index} failed with exit {completed.returncode}"
                    )
        else:
            self._audit(gap, -1, (), "freeze_skipped", None)
        if not context.active():
            raise TimeoutError("validation deadline reached before re-audit")
        return self.reaudit(gap)

    def _audit(
        self,
        gap: GapRecord,
        index: int,
        command: tuple[str, ...],
        status: str,
        returncode: int | None,
    ) -> None:
        encoded = json.dumps(command, separators=(",", ":")).encode()
        payload = {
            "type": "regression_command",
            "signature_key": gap.signature_key,
            "command_index": index,
            "argv_sha256": sha256(encoded).hexdigest(),
            "executable": None if not command else Path(command[0]).name,
            "shell": False,
            "status": status,
            "returncode": returncode,
        }
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        with self.audit_path.open("ab", buffering=0) as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.write(line)
                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


__all__ = ["ExternalRegressionHook"]
