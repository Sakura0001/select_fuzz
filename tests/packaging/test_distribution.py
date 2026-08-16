from __future__ import annotations

from hashlib import sha256
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


def test_installed_wheel_loads_the_packaged_mysql_8022_grammar(tmp_path: Path) -> None:
    wheel = _build_distribution(tmp_path, "wheel", "*.whl")
    site_packages = tmp_path / "site-packages"
    with ZipFile(wheel) as archive:
        archive.extractall(site_packages)

    checkout_sha256 = sha256(
        (PROJECT_ROOT / "catalog" / "mysql-8.0.22-select.grammar.yy").read_bytes()
    ).hexdigest()
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "import select_fuzz.generation.query_grammar as module; "
                "from select_fuzz.generation.query_grammar import SelectGrammar; "
                "print(Path(module.__file__).resolve()); "
                "print(SelectGrammar.default().sha256)"
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
    module_path, grammar_sha256 = completed.stdout.splitlines()
    assert Path(module_path).is_relative_to(site_packages)
    assert grammar_sha256 == checkout_sha256


def test_installed_wheel_fails_closed_without_the_packaged_grammar(tmp_path: Path) -> None:
    wheel = _build_distribution(tmp_path, "wheel", "*.whl")
    site_packages = tmp_path / "site-packages"
    with ZipFile(wheel) as archive:
        archive.extractall(site_packages)
    (site_packages / GRAMMAR_MEMBER).unlink()

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from select_fuzz.generation.query_grammar import SelectGrammar; "
                "SelectGrammar.default()"
            ),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(site_packages)},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode != 0
    assert "canonical MySQL 8.0.22 SELECT grammar is unavailable" in completed.stderr


def test_sdist_is_reproducible_source_not_a_workspace_snapshot(tmp_path: Path) -> None:
    sdist = _build_distribution(tmp_path, "sdist", "*.tar.gz")

    with tarfile.open(sdist, mode="r:gz") as archive:
        members = [Path(member.name) for member in archive.getmembers()]

    assert any(path.name == "mysql-8.0.41-query-shapes.yaml" for path in members)
    assert not any(_is_forbidden_sdist_member(path) for path in members)
