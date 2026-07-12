from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tarfile
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG_MEMBER = "select_fuzz/data/mysql-8.0.41-query-shapes.yaml"
FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".hypothesis",
        ".local",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "reports",
    }
)


def _build_distribution(output: Path, target: str, suffix: str) -> Path:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "hatchling",
            "build",
            "--directory",
            str(output),
            "--target",
            target,
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return next(output.glob(suffix))


def test_wheel_contains_the_canonical_catalog_as_package_data(tmp_path: Path) -> None:
    wheel = _build_distribution(tmp_path, "wheel", "*.whl")

    with ZipFile(wheel) as archive:
        members = set(archive.namelist())
        assert CATALOG_MEMBER in members
        assert archive.read(CATALOG_MEMBER).startswith(b"schema_version: 2\n")


def test_installed_wheel_loads_its_own_canonical_catalog(tmp_path: Path) -> None:
    wheel = _build_distribution(tmp_path, "wheel", "*.whl")
    site_packages = tmp_path / "site-packages"
    with ZipFile(wheel) as archive:
        archive.extractall(site_packages)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from select_fuzz.generation.catalog import FeatureCatalog; "
                "print(len(FeatureCatalog.default()))"
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(site_packages)},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "58"


def test_sdist_is_reproducible_source_not_a_workspace_snapshot(tmp_path: Path) -> None:
    sdist = _build_distribution(tmp_path, "sdist", "*.tar.gz")

    with tarfile.open(sdist, mode="r:gz") as archive:
        members = [Path(member.name) for member in archive.getmembers()]

    assert any(path.name == "mysql-8.0.41-query-shapes.yaml" for path in members)
    assert not any(FORBIDDEN_PARTS.intersection(path.parts) for path in members)
