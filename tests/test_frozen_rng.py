from __future__ import annotations

import ast
from pathlib import Path

import pytest

from select_fuzz.sqlgen.rng import FrozenRandomV1


def test_v1_rng_sha256_counter_stream_固定向量覆盖完整公共接口() -> None:
    rng = FrozenRandomV1(123)

    assert rng.randbelow(10) == 1
    assert rng.random() == 0.3722146194161218
    assert rng.choice(("a", "b", "c")) == "b"
    assert rng.randint(-2, 4) == 1
    assert rng.sample(["a", "b", "c", "d", "e"], 3) == ["e", "b", "c"]
    assert rng.choices(["x", "y", "z"], weights=[1, 3, 2], k=4) == ["z", "x", "x", "z"]
    assert rng.uniform(-10.0, 20.0) == -2.4286849668911383


def test_randbelow_丢弃有偏尾部后再取样(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = FrozenRandomV1(0)
    chunks = iter((b"\xff", b"\x07"))
    calls: list[int] = []

    def take_bytes(count: int) -> bytes:
        calls.append(count)
        return next(chunks)

    monkeypatch.setattr(rng, "_take_bytes", take_bytes)

    assert rng.randbelow(10) == 7
    assert calls == [1, 1]


@pytest.mark.parametrize(
    ("call", "error"),
    (
        (lambda rng: rng.randbelow(0), ValueError),
        (lambda rng: rng.choice(()), IndexError),
        (lambda rng: rng.randint(2, 1), ValueError),
        (lambda rng: rng.sample([1], 2), ValueError),
        (lambda rng: rng.choices([1, 2], weights=[0, 0], k=1), ValueError),
    ),
)
def test_v1_rng_对无效边界给出明确异常(call, error: type[Exception]) -> None:
    with pytest.raises(error):
        call(FrozenRandomV1(0))


def test_v1_运行时代码不导入_python_random() -> None:
    sqlgen_dir = Path(__file__).parents[1] / "select_fuzz" / "sqlgen"
    runtime_files = [
        sqlgen_dir / "rng.py",
        sqlgen_dir / "registry.py",
        sqlgen_dir / "generator.py",
        sqlgen_dir / "dml.py",
    ]

    imported_modules: set[str] = set()
    for path in runtime_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.add(node.module)

    assert "random" not in imported_modules
