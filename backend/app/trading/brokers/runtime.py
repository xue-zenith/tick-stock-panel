"""Vendor SDK, process and socket readiness checks."""
from __future__ import annotations

import importlib.util
import socket
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RuntimeCheck:
    id: str
    status: str
    detail: str = ""
    action: str = ""


def sdk_module(broker: str) -> str:
    return {"futu": "futu", "ibkr": "ibapi", "qmt": "xtquant"}.get(broker, broker)


def check_sdk(broker: str) -> RuntimeCheck:
    module = sdk_module(broker)
    if importlib.util.find_spec(module) is None:
        return RuntimeCheck("sdk_import", "FAIL", f"未找到 Python 模块 {module}", f"安装 {broker} gateway 依赖")
    return RuntimeCheck("sdk_import", "PASS", f"{module} 可导入")


def check_socket(host: str, port: int, timeout: float = 1.5) -> RuntimeCheck:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return RuntimeCheck("tcp", "PASS", f"{host}:{port} 可连接")
    except OSError as exc:
        return RuntimeCheck("tcp", "FAIL", f"{host}:{port} 不可连接: {exc}", "启动厂商终端并确认 API Socket 设置")


def report(*, broker: str, gateway_id: str, host: str, port: int, environment: str, extra: list[RuntimeCheck] | None = None) -> dict:
    checks = [check_sdk(broker), check_socket(host, port), *(extra or [])]
    sdk_ok = checks[0].status == "PASS"
    socket_ok = checks[1].status == "PASS"
    return {
        "gateway_id": gateway_id,
        "broker": broker,
        "process": "UP" if socket_ok else "DOWN",
        "vendor_runtime": "CONNECTED" if socket_ok else "UNAVAILABLE",
        "authenticated": False,
        "environment": environment,
        "accounts_discovered": 0,
        "account_verified": False,
        "read_capability": "READY" if sdk_ok and socket_ok else "LOCKED",
        "trade_capability": "LOCKED",
        "python": sys.version.split()[0],
        "checks": [asdict(item) for item in checks],
    }
