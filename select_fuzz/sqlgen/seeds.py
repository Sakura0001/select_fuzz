"""查询和 CRUD worker 的版本化种子工具。"""

from __future__ import annotations

import hashlib
import struct


CURRENT_QUERY_GENERATOR_VERSION = "v1"
CURRENT_CRUD_GENERATOR_VERSION = "v1"
QUERY_GENERATOR_VERSION = CURRENT_QUERY_GENERATOR_VERSION
DML_GENERATOR_VERSION = CURRENT_CRUD_GENERATOR_VERSION
MAX_UINT64 = (1 << 64) - 1
_MAX_UINT64_TEXT = str(MAX_UINT64)

# v1 冻结格式：命名空间后依次拼接 seed、role、identity；每段均为
# “四字节大端 UTF-8 字节长度 + UTF-8 内容”，取 SHA-256 前八字节的大端整数。
_WORKER_SEED_NAMESPACE_V1 = b"select-fuzz/worker-seed/v1\0"


def normalize_uint64_seed(seed: str) -> str:
    """只接受规范 ASCII 十进制形式的无符号 64 位整数字符串。"""

    valid = isinstance(seed, str) and (
        seed == "0"
        or (
            bool(seed)
            and seed[0] in "123456789"
            and all(character in "0123456789" for character in seed[1:])
        )
    )
    if not valid:
        raise ValueError("任务种子必须是规范的 ASCII 无符号十进制整数字符串")
    if len(seed) > len(_MAX_UINT64_TEXT) or (
        len(seed) == len(_MAX_UINT64_TEXT) and seed > _MAX_UINT64_TEXT
    ):
        raise ValueError(f"任务种子不能大于 {MAX_UINT64}")
    return seed


def _encode_worker_seed_component(value: str, name: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"worker {name} 必须是字符串")
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFFFFFF:
        raise ValueError(f"worker {name} 的 UTF-8 内容过长")
    return struct.pack(">I", len(encoded)) + encoded


def derive_worker_seed(seed: str, role: str, identity: str) -> int:
    """按冻结的 v1 SHA-256 字节格式派生稳定的 uint64 worker seed。"""

    normalized_seed = normalize_uint64_seed(seed)
    payload = b"".join(
        (
            _encode_worker_seed_component(normalized_seed, "根种子"),
            _encode_worker_seed_component(role, "角色"),
            _encode_worker_seed_component(identity, "身份"),
        )
    )
    digest = hashlib.sha256(_WORKER_SEED_NAMESPACE_V1 + payload).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


__all__ = [
    "CURRENT_CRUD_GENERATOR_VERSION",
    "CURRENT_QUERY_GENERATOR_VERSION",
    "DML_GENERATOR_VERSION",
    "MAX_UINT64",
    "QUERY_GENERATOR_VERSION",
    "derive_worker_seed",
    "normalize_uint64_seed",
]
