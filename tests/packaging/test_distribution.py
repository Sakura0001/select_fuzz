from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tarfile
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG_MEMBER = "select_fuzz/data/mysql-8.0.41-query-shapes.yaml"
GRAMMAR_MEMBER = "select_fuzz/data/mysql-8.0.22-select.grammar.yy"
LEGACY_GRAMMAR_MEMBER = "select_fuzz/data/mysql-8.0.41-select.grammar.yy"
FORBIDDEN_ANYWHERE = frozenset(
    {
        ".git",
        ".hypothesis",
        ".local",
        ".uv-cache",
        ".venv",
        "__pycache__",
    }
)
FORBIDDEN_WORKSPACE_ROOTS = frozenset({"artifacts", "reports"})


def _is_forbidden_sdist_member(path: Path) -> bool:
    # Every sdist member starts with hatchling's project-version directory.
    relative_parts = path.parts[1:]
    return bool(
        FORBIDDEN_ANYWHERE.intersection(relative_parts)
        or (
            relative_parts
            and relative_parts[0] in FORBIDDEN_WORKSPACE_ROOTS
        )
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


def test_wheel_contains_the_canonical_catalog_and_grammar_as_package_data(
    tmp_path: Path,
) -> None:
    wheel = _build_distribution(tmp_path, "wheel", "*.whl")

    with ZipFile(wheel) as archive:
        members = set(archive.namelist())
        assert CATALOG_MEMBER in members
        assert GRAMMAR_MEMBER in members
        assert LEGACY_GRAMMAR_MEMBER not in members
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
    assert completed.stdout.strip() == "64"


def test_sdist_is_reproducible_source_not_a_workspace_snapshot(tmp_path: Path) -> None:
    sdist = _build_distribution(tmp_path, "sdist", "*.tar.gz")

    with tarfile.open(sdist, mode="r:gz") as archive:
        members = [Path(member.name) for member in archive.getmembers()]

    assert any(path.name == "mysql-8.0.41-query-shapes.yaml" for path in members)
    assert not any(_is_forbidden_sdist_member(path) for path in members)
