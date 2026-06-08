from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from select_fuzz.config import JumpHostConfig, TargetNodeConfig


@dataclass
class JumpTunnel:
    jump_host: JumpHostConfig
    target_node: TargetNodeConfig
    local_host: str = "127.0.0.1"
    local_port: Optional[int] = None
    _server: object | None = None

    def start(self) -> tuple[str, int]:
        _ensure_paramiko_dss_compat()
        from sshtunnel import SSHTunnelForwarder

        self._server = SSHTunnelForwarder(
            (self.jump_host.host, self.jump_host.port),
            ssh_config_file=None,
            ssh_username=self.jump_host.username,
            ssh_password=self.jump_host.password,
            ssh_pkey=self.jump_host.private_key_path or None,
            allow_agent=False,
            host_pkey_directories=[],
            remote_bind_address=(self.target_node.host, self.target_node.port),
            local_bind_address=(self.local_host, self.local_port or 0),
        )
        self._server.start()
        bound_port = int(getattr(self._server, "local_bind_port"))
        self.local_port = bound_port
        return self.local_host, bound_port

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None


def _ensure_paramiko_dss_compat() -> None:
    import paramiko

    if hasattr(paramiko, "DSSKey"):
        return

    # sshtunnel 0.4.0 仍会引用 Paramiko 5 已移除的 DSSKey。
    class _UnsupportedDSSKey:
        @classmethod
        def from_private_key_file(cls, *_args, **_kwargs):
            raise paramiko.SSHException("当前 Paramiko 版本不支持 DSA/DSS 私钥，请改用 RSA、ECDSA 或 Ed25519 私钥")

    paramiko.DSSKey = _UnsupportedDSSKey
