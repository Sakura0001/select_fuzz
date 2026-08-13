"""项目自有、可跨 Python 版本复现的 v1 伪随机数流。

v1 字节格式永久冻结如下；任何算法或消费顺序变更都必须发布新版本：

1. ``seed`` 是十进制 ASCII 整数字节，前置四字节大端无符号长度；
2. 每个 32 字节块为
   ``SHA-256(namespace || seed_length || seed || counter_u64_be)``；
3. ``namespace`` 固定为 ``b"select-fuzz/frozen-rng/v1\\0"``，计数器从零开始；
4. 所有 API 从连续块字节流依次消费，不使用 Python ``random`` 实现；
5. ``randbelow`` 丢弃不能被上界整除的尾部区间，避免取模偏差。
"""

from __future__ import annotations

import hashlib
import os
import struct
from fractions import Fraction
from math import gcd
from typing import Optional, Protocol, Sequence, TypeVar


_T = TypeVar("_T")
_NAMESPACE_V1 = b"select-fuzz/frozen-rng/v1\0"
_MAX_COUNTER = (1 << 64) - 1


class RandomSource(Protocol):
    """SQL 生成器实际需要的最小随机接口。"""

    def randbelow(self, upper: int) -> int: ...

    def random(self) -> float: ...

    def choice(self, population: Sequence[_T]) -> _T: ...

    def randint(self, start: int, end: int) -> int: ...

    def sample(self, population: Sequence[_T], count: int) -> list[_T]: ...

    def choices(
        self,
        population: Sequence[_T],
        weights: Optional[Sequence[object]] = None,
        *,
        cum_weights: Optional[Sequence[object]] = None,
        k: int = 1,
    ) -> list[_T]: ...

    def uniform(self, start: float, end: float) -> float: ...


class FrozenRandomV1:
    """基于 SHA-256 counter stream 的冻结 v1 随机实现。"""

    def __init__(self, seed: int | None = None) -> None:
        if seed is None:
            seed = int.from_bytes(os.urandom(8), byteorder="big", signed=False)
        if type(seed) is not int:
            raise TypeError("v1 随机种子必须是整数或 None")
        seed_bytes = str(seed).encode("ascii")
        if len(seed_bytes) > 0xFFFFFFFF:
            raise ValueError("v1 随机种子十进制文本过长")
        self._prefix = _NAMESPACE_V1 + struct.pack(">I", len(seed_bytes)) + seed_bytes
        self._counter = 0
        self._buffer = b""

    def _take_bytes(self, count: int) -> bytes:
        if type(count) is not int or count < 0:
            raise ValueError("随机字节数必须是非负整数")
        while len(self._buffer) < count:
            if self._counter > _MAX_COUNTER:
                raise RuntimeError("v1 随机流计数器已耗尽")
            block = hashlib.sha256(
                self._prefix + struct.pack(">Q", self._counter)
            ).digest()
            self._counter += 1
            self._buffer += block
        result = self._buffer[:count]
        self._buffer = self._buffer[count:]
        return result

    def randbelow(self, upper: int) -> int:
        """使用拒绝采样返回 ``[0, upper)`` 内的无偏整数。"""

        if type(upper) is not int or upper <= 0:
            raise ValueError("randbelow 上界必须是正整数")
        byte_count = max(1, ((upper - 1).bit_length() + 7) // 8)
        sample_space = 1 << (byte_count * 8)
        unbiased_limit = sample_space - sample_space % upper
        while True:
            candidate = int.from_bytes(self._take_bytes(byte_count), byteorder="big")
            if candidate < unbiased_limit:
                return candidate % upper

    def random(self) -> float:
        """按 53 个无偏随机位返回 ``[0.0, 1.0)`` 浮点数。"""

        return self.randbelow(1 << 53) / float(1 << 53)

    def choice(self, population: Sequence[_T]) -> _T:
        if not population:
            raise IndexError("不能从空序列选择随机元素")
        return population[self.randbelow(len(population))]

    def randint(self, start: int, end: int) -> int:
        if type(start) is not int or type(end) is not int:
            raise TypeError("randint 边界必须是整数")
        if end < start:
            raise ValueError("randint 结束值不能小于开始值")
        return start + self.randbelow(end - start + 1)

    def sample(self, population: Sequence[_T], count: int) -> list[_T]:
        if type(count) is not int or not 0 <= count <= len(population):
            raise ValueError("sample 数量必须位于零和总体长度之间")
        pool = list(population)
        selected: list[_T] = []
        for _ in range(count):
            selected.append(pool.pop(self.randbelow(len(pool))))
        return selected

    def choices(
        self,
        population: Sequence[_T],
        weights: Optional[Sequence[object]] = None,
        *,
        cum_weights: Optional[Sequence[object]] = None,
        k: int = 1,
    ) -> list[_T]:
        if not population:
            raise IndexError("不能从空序列选择随机元素")
        if type(k) is not int or k < 0:
            raise ValueError("choices 数量必须是非负整数")
        if weights is not None and cum_weights is not None:
            raise TypeError("weights 与 cum_weights 不能同时传入")
        if weights is None and cum_weights is None:
            return [self.choice(population) for _ in range(k)]

        raw_weights: Sequence[object]
        if cum_weights is not None:
            if len(cum_weights) != len(population):
                raise ValueError("cum_weights 长度必须与总体一致")
            cumulative = [_fraction(value) for value in cum_weights]
            previous = Fraction(0)
            exact_weights: list[Fraction] = []
            for current in cumulative:
                exact_weights.append(current - previous)
                previous = current
            raw_weights = exact_weights
        else:
            assert weights is not None
            if len(weights) != len(population):
                raise ValueError("weights 长度必须与总体一致")
            raw_weights = weights

        exact_weights = [
            value if isinstance(value, Fraction) else _fraction(value)
            for value in raw_weights
        ]
        if any(weight < 0 for weight in exact_weights):
            raise ValueError("choices 权重不能为负数")
        common_denominator = 1
        for weight in exact_weights:
            common_denominator = _lcm(common_denominator, weight.denominator)
        integer_weights = [
            weight.numerator * (common_denominator // weight.denominator)
            for weight in exact_weights
        ]
        total = sum(integer_weights)
        if total <= 0:
            raise ValueError("choices 权重总和必须大于零")

        selected: list[_T] = []
        for _ in range(k):
            threshold = self.randbelow(total)
            cumulative_weight = 0
            for item, weight in zip(population, integer_weights):
                cumulative_weight += weight
                if threshold < cumulative_weight:
                    selected.append(item)
                    break
        return selected

    def uniform(self, start: float, end: float) -> float:
        return start + (end - start) * self.random()


def _fraction(value: object) -> Fraction:
    try:
        return Fraction(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError("choices 权重必须是有限实数") from exc


def _lcm(left: int, right: int) -> int:
    return abs(left * right) // gcd(left, right)


__all__ = ["FrozenRandomV1", "RandomSource"]
