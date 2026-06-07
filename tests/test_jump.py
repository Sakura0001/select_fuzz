import sys
from types import SimpleNamespace

from select_fuzz.config import JumpHostConfig, TargetNodeConfig
from select_fuzz.runner.jump import JumpTunnel


def test_跳板机隧道支持_ssh_账户密码(monkeypatch) -> None:
    captured: dict = {}

    class FakeForwarder:
        local_bind_port = 43001

        def __init__(self, address, **kwargs) -> None:
            captured["address"] = address
            captured["kwargs"] = kwargs

        def start(self) -> None:
            captured["started"] = True

        def stop(self) -> None:
            captured["stopped"] = True

    monkeypatch.setitem(sys.modules, "sshtunnel", SimpleNamespace(SSHTunnelForwarder=FakeForwarder))
    tunnel = JumpTunnel(
        jump_host=JumpHostConfig(
            name="jump-prod",
            host="10.2.0.8",
            port=22,
            username="ops",
            password="ssh-secret",
        ),
        target_node=TargetNodeConfig(
            name="node-a",
            host="172.18.4.12",
            port=3306,
            username="fuzz",
            password="db-secret",
        ),
    )

    host, port = tunnel.start()
    tunnel.stop()

    assert (host, port) == ("127.0.0.1", 43001)
    assert captured["address"] == ("10.2.0.8", 22)
    assert captured["kwargs"]["ssh_username"] == "ops"
    assert captured["kwargs"]["ssh_password"] == "ssh-secret"
    assert captured["kwargs"]["ssh_pkey"] is None
    assert captured["kwargs"]["remote_bind_address"] == ("172.18.4.12", 3306)
    assert captured["started"] is True
    assert captured["stopped"] is True


def test_跳板机隧道保留私钥登录方式(monkeypatch) -> None:
    captured: dict = {}

    class FakeForwarder:
        local_bind_port = 43002

        def __init__(self, address, **kwargs) -> None:
            captured["address"] = address
            captured["kwargs"] = kwargs

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "sshtunnel", SimpleNamespace(SSHTunnelForwarder=FakeForwarder))
    tunnel = JumpTunnel(
        jump_host=JumpHostConfig(
            name="jump-prod",
            host="10.2.0.8",
            port=22,
            username="ops",
            private_key_path="/Users/yuyu/.ssh/id_rsa",
        ),
        target_node=TargetNodeConfig(
            name="node-a",
            host="172.18.4.12",
            port=3306,
            username="fuzz",
            password="db-secret",
        ),
    )

    tunnel.start()

    assert captured["kwargs"]["ssh_password"] is None
    assert captured["kwargs"]["ssh_pkey"] == "/Users/yuyu/.ssh/id_rsa"
