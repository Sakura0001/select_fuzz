from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import json

import pytest

from select_fuzz.validation.source import OfficialSourceAcquirer


@pytest.mark.online
def test_opt_in_fetches_real_official_mysql_page(tmp_path: Path) -> None:
    if os.environ.get("SELECT_FUZZ_RUN_ONLINE_VALIDATION") != "1":
        pytest.skip("set SELECT_FUZZ_RUN_ONLINE_VALIDATION=1 for official-source gate")
    cached = OfficialSourceAcquirer(tmp_path).acquire(
        "https://dev.mysql.com/doc/refman/8.0/en/select.html"
    )
    assert cached.path.is_file()
    assert cached.source.url.startswith("https://dev.mysql.com/")
    assert len(cached.source.content_sha256) == 64


@pytest.mark.online
def test_opt_in_non_dry_cli_runs_real_online_epoch_and_report(tmp_path: Path) -> None:
    if os.environ.get("SELECT_FUZZ_RUN_ONLINE_VALIDATION") != "1":
        pytest.skip("set SELECT_FUZZ_RUN_ONLINE_VALIDATION=1 for official-source gate")
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/validation_12h.py",
            "--duration",
            "30s",
            "--checkpoint",
            "5s",
            "--freeze",
            "0s",
            "--max-epochs",
            "1",
            "--run-id",
            "online-cli-e2e",
            "--seed-url",
            "https://dev.mysql.com/doc/refman/8.0/en/group-by-modifiers.html",
            "--output",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((tmp_path / "report" / "source-manifest.json").read_text())
    assert manifest
    assert manifest[0]["url"].startswith("https://dev.mysql.com/")
    assert (tmp_path / "report" / "coverage.json").is_file()
