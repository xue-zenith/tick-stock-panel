"""FuyaoClient 的客户端租约语义。

httpx.Client 可以多线程共用, 但 close() 是销毁性的: 在请求在途时关闭同一个
client, 在途请求会以 ReadTimeout 失败 (不是 ConnectionError) —— 看起来像网络
抖动, 实际是自愈重建打的。所以请求走引用计数, close() 只标记, 等最后一个在途
请求归还才真正关 httpx.Client。
"""

from __future__ import annotations

import contextlib
import threading
import time
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer

import pytest

from app.plugins.fuyao import client as fc


class _SlowHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        time.sleep(0.35)
        body = b'{"code":0,"data":{"timestamp":1,"total":0,"item":[]}}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def slow_server():
    server = ThreadingTCPServer(("127.0.0.1", 0), _SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    # 限流会引入 0.35s/请求 的全局间隔, 与本测试要观察的时序无关
    monkeypatch.setattr(fc, "_REQUEST_LIMITER", type("_", (), {"wait": lambda self: None})())


def _client(url: str) -> fc.FuyaoClient:
    return fc.FuyaoClient(api_key="test-key", base_url=url, timeout=10.0)


def test_in_flight_request_survives_close(slow_server):
    """核心回归: 请求在途时 close(), 该请求必须正常返回而不是 ReadTimeout。"""
    client = _client(slow_server)
    result = {}

    def call():
        try:
            result["v"] = client.snapshot_page(limit=1)
        except Exception as exc:  # noqa: BLE001
            result["err"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=call)
    thread.start()
    time.sleep(0.12)  # 请求已在途
    client.close()
    thread.join(timeout=5.0)

    assert "err" not in result, result["err"]
    assert result["v"] == ([], 0)


def test_close_defers_http_close_until_last_lease_returns(slow_server):
    """close() 期间旧 httpx.Client 不能被立刻销毁。"""
    client = _client(slow_server)
    started = threading.Event()

    def call():
        started.set()
        with contextlib.suppress(Exception):
            client.snapshot_page(limit=1)

    thread = threading.Thread(target=call)
    thread.start()
    started.wait(1.0)
    time.sleep(0.12)
    client.close()
    # 关了但请求还没回来 -> 底层连接池不该被销毁
    assert client._retired is True
    assert not client._http.is_closed
    thread.join(timeout=5.0)
    # 最后一个租约归还后才真正关
    assert client._http.is_closed


def test_close_is_idempotent(slow_server):
    client = _client(slow_server)
    client.close()
    client.close()
    assert client._http.is_closed


def test_request_after_close_raises_instead_of_using_dead_client(slow_server):
    client = _client(slow_server)
    client.close()
    with pytest.raises(fc.FuyaoError, match="已关闭"):
        client.snapshot_page(limit=1)


def test_failed_request_still_releases_its_lease(monkeypatch):
    """请求抛异常也必须归还租约, 否则 close() 永远等不到 0 而漏关 socket。"""
    client = _client("http://127.0.0.1:1")

    def boom(*args, **kwargs):
        raise fc.httpx.ConnectError("连接失败")

    # 直接让底层 httpx 抛错, 不依赖本机是否有代理把连接变成 HTTP 502
    monkeypatch.setattr(client._http, "get", boom)

    with pytest.raises(fc.FuyaoError, match="网络请求失败"):
        client.snapshot_page(limit=1)
    assert client._leases == 0

    client.close()
    assert client._http.is_closed


def test_no_close_when_leases_never_reach_zero_then_they_do(slow_server):
    """两个在途请求, 先归还一个不关, 最后一个归还才关。"""
    client = _client(slow_server)
    gate = threading.Barrier(3)

    def call():
        gate.wait()
        with contextlib.suppress(Exception):
            client.snapshot_page(limit=1)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for item in threads:
        item.start()
    gate.wait()
    time.sleep(0.1)
    assert client._leases == 2

    client.close()
    assert not client._http.is_closed  # 还有两个在用

    for item in threads:
        item.join(timeout=5.0)
    assert client._http.is_closed
