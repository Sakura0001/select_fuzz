"""Fresh-process reachability re-audit after an external regression command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from select_fuzz.validation.generator_adapter import ProductionGeneratorAdapter
from select_fuzz.validation.models import (
    FeatureSignature,
    Reachability,
    ReachabilityResult,
)
from select_fuzz.validation.reachability import CapabilityAuditor


def _result_payload(result: ReachabilityResult) -> dict[str, object]:
    return {
        "signature_key": result.signature_key,
        "status": result.status.value,
        "reasons": list(result.reasons),
        "witness_seed": result.witness_seed,
        "witness_feature_id": result.witness_feature_id,
    }


def _decode_result(payload: Any) -> ReachabilityResult:
    if not isinstance(payload, dict):
        raise RuntimeError("isolated re-audit returned a non-object payload")
    try:
        return ReachabilityResult(
            signature_key=payload["signature_key"],
            status=Reachability(payload["status"]),
            reasons=tuple(payload["reasons"]),
            witness_seed=payload["witness_seed"],
            witness_feature_id=payload["witness_feature_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("isolated re-audit returned an invalid payload") from exc


def run_isolated_reaudit(
    signature: FeatureSignature,
    *,
    budget: int = 32,
    timeout_s: float = 60,
) -> ReachabilityResult:
    """Load the on-disk module graph in a child interpreter and audit there."""

    if budget <= 0 or timeout_s <= 0:
        raise ValueError("budget and timeout_s must be positive")
    request = json.dumps(
        {
            "version": signature.version,
            "nodes": list(signature.nodes),
            "requirements": list(signature.requirements),
            "budget": budget,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    env = os.environ.copy()
    import_paths = [path for path in sys.path if path]
    if env.get("PYTHONPATH"):
        import_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(import_paths)
    try:
        completed = subprocess.run(
            (sys.executable, "-m", "select_fuzz.validation.reaudit_worker"),
            input=request,
            text=True,
            shell=False,
            check=False,
            capture_output=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("isolated re-audit exceeded its deadline") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-1000:]
        raise RuntimeError(f"isolated re-audit failed with exit {completed.returncode}: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("isolated re-audit returned invalid JSON") from exc
    result = _decode_result(payload)
    if result.signature_key != signature.key:
        raise RuntimeError("isolated re-audit returned a mismatched signature")
    return result


def main() -> int:
    try:
        request = json.loads(sys.stdin.read(64 * 1024))
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        signature = FeatureSignature(
            version=request["version"],
            nodes=tuple(request["nodes"]),
            requirements=tuple(request["requirements"]),
        )
        budget = request["budget"]
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise ValueError("budget must be a positive integer")
        adapter = ProductionGeneratorAdapter()
        capability = adapter.find_capability(signature)
        result = CapabilityAuditor().audit(
            signature,
            capability,
            generator=adapter,
            budget=budget,
        )
    except (KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(_result_payload(result), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_isolated_reaudit"]
