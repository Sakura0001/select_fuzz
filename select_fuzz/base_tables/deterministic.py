"""`v1` 基表生成器使用的冻结确定性派生原语。"""

from __future__ import annotations

import hashlib
from typing import Sequence, TypeVar


_NAMESPACE = b"select-fuzz/base-table/v1\0"
_UINT64_SPACE = 1 << 64
_MAX_UINT64 = _UINT64_SPACE - 1
_MAX_COUNTER = (1 << 32) - 1
_T = TypeVar("_T")


def _seed_bytes(seed: str) -> bytes:
    """把规范 uint64 十进制种子编码为固定八字节大端值。"""

    if not isinstance(seed, str) or not seed:
        raise ValueError("基表种子必须是规范的 ASCII 无符号十进制整数")
    if seed != "0" and (seed[0] not in "123456789" or any(char not in "0123456789" for char in seed[1:])):
        raise ValueError("基表种子必须是规范的 ASCII 无符号十进制整数")
    if seed == "0" and len(seed) != 1:
        raise ValueError("基表种子必须是规范的 ASCII 无符号十进制整数")
    if any(char not in "0123456789" for char in seed):
        raise ValueError("基表种子必须是规范的 ASCII 无符号十进制整数")
    value = int(seed)
    if value > _MAX_UINT64:
        raise ValueError(f"基表种子不能大于 {_MAX_UINT64}")
    return value.to_bytes(8, "big")


def derive_uint64(*, seed: str, domain: str, counter: int = 0) -> int:
    """按冻结命名空间、用途域和计数器派生一个 64 位无符号整数。"""

    if not isinstance(domain, str) or not domain:
        raise ValueError("确定性派生用途域不能为空")
    if not isinstance(counter, int) or isinstance(counter, bool) or not 0 <= counter <= _MAX_COUNTER:
        raise ValueError(f"确定性派生计数器必须在 0 到 {_MAX_COUNTER} 之间")
    try:
        domain_bytes = domain.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("确定性派生用途域必须能编码为 UTF-8") from exc
    payload = _NAMESPACE + _seed_bytes(seed) + domain_bytes + counter.to_bytes(4, "big")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def derive_range(*, seed: str, domain: str, minimum: int, maximum: int) -> int:
    """通过 rejection sampling 无偏派生闭区间整数。"""

    if not isinstance(minimum, int) or isinstance(minimum, bool):
        raise ValueError("确定性派生范围下界必须是整数")
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        raise ValueError("确定性派生范围上界必须是整数")
    if minimum > maximum:
        raise ValueError("确定性派生范围下界不能大于上界")
    width = maximum - minimum + 1
    if width > _UINT64_SPACE:
        raise ValueError("确定性派生范围宽度不能超过 2^64")

    rejection_limit = _UINT64_SPACE - (_UINT64_SPACE % width)
    for counter in range(_MAX_COUNTER + 1):
        value = derive_uint64(seed=seed, domain=domain, counter=counter)
        if value < rejection_limit:
            return minimum + value % width
    raise RuntimeError("确定性派生拒绝采样耗尽计数器")


def pick(*, seed: str, domain: str, candidates: Sequence[_T]) -> _T:
    """从顺序已经冻结的非空候选序列中做无偏选择。"""

    if not candidates:
        raise ValueError("确定性派生候选列表不能为空")
    index = derive_range(seed=seed, domain=domain, minimum=0, maximum=len(candidates) - 1)
    return candidates[index]


__all__ = ["derive_range", "derive_uint64", "pick"]
