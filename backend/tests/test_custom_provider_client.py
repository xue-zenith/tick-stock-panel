"""GenericHTTPProvider 的 httpx 客户端生命周期。

httpx.Client 多线程共用是安全的, 但 close() 是销毁性的: 在请求在途时关掉同一个
client, 在途请求会以 ReadTimeout 失败 (实测, 不是 ConnectionError) —— 自愈重建
反而制造一次「网络故障」。所以客户端必须引用计数: 换下只标记, 最后一个租约归还
时才真正 close。
"""
from __future__ import annotations

import contextlib
import http.server
import socketserver
import threading
import time

import pytest

from app.data_providers.base import ProviderRequestError
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.provider import GenericHTTPProvider


class _FakeClient:
    """复刻 httpx.Client 与本层相关的语义: close 后再请求会报错。"""

    def __init__(self) -> None:
        self.closed_at: float | None = None
        self.requests: list[str] = []

    @property
    def is_closed(self) -> bool:
        return self.closed_at is not None

    def request(self, method, url, **kwargs):
        if self.is_closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        self.requests.append(url)

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": []}

        return _Resp()

    def close(self):
        self.closed_at = time.monotonic()


def _config() -> CustomSourceConfig:
    return CustomSourceConfig(
        name="lifecycle",
        display_name="Lifecycle",
        datasets={
            "realtime": DatasetConfig(
                url="https://example.test/realtime",
                field_map={"c": "symbol", "p": "last_price"},
            )
        },
    )


def _provider(monkeypatch) -> GenericHTTPProvider:
    """用 _FakeClient 替换真实 httpx 客户端 (测试不碰网络)。"""
    monkeypatch.setattr(GenericHTTPProvider, "_new_client", staticmethod(_FakeClient))
    return GenericHTTPProvider(_config())


def test_rebuild_swaps_to_a_fresh_client(monkeypatch):
    provider = _provider(monkeypatch)
    old = provider._current[0]
    provider.rebuild_client()
    assert provider._current[0] is not old
    assert old.is_closed, "旧客户端应被关闭"
    assert provider._current[2] is False, "provider 本身仍可用"
    assert provider.get_realtime() == []


def test_rebuild_defers_close_until_request_returns(monkeypatch):
    """重建时若有请求在途, 旧客户端必须等它归还后才关闭。"""
    provider = _provider(monkeypatch)
    old = provider._current[0]
    release = threading.Event()
    entered = threading.Event()
    original = old.request

    def slow_request(method, url, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(method, url, **kwargs)

    old.request = slow_request
    worker = threading.Thread(target=provider.get_realtime)
    worker.start()
    assert entered.wait(3)

    provider.rebuild_client()
    assert not old.is_closed, "在途请求未归还前不能关闭旧客户端"

    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert old.is_closed, "最后一个租约归还后才关闭"


def test_close_waits_for_in_flight_request(monkeypatch):
    provider = _provider(monkeypatch)
    old = provider._current[0]
    release = threading.Event()
    entered = threading.Event()
    original = old.request

    def slow_request(method, url, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(method, url, **kwargs)

    old.request = slow_request
    worker = threading.Thread(target=provider.get_realtime)
    worker.start()
    assert entered.wait(3)

    provider.close()
    assert not old.is_closed

    release.set()
    worker.join(5)
    assert old.is_closed


def test_close_is_idempotent(monkeypatch):
    provider = _provider(monkeypatch)
    provider.close()
    provider.close()  # 不应抛错
    assert provider._current[2] is True


def test_request_after_close_raises_instead_of_using_dead_client(monkeypatch):
    provider = _provider(monkeypatch)
    provider.close()
    with pytest.raises(ProviderRequestError, match="已关闭"):
        provider.get_realtime()


def test_rebuild_after_close_raises(monkeypatch):
    """close() 停用的 provider 不能被 rebuild 悄悄复活 (与 ManagedProvider 同语义)。"""
    provider = _provider(monkeypatch)
    provider.close()
    with pytest.raises(ProviderRequestError, match="已关闭"):
        provider.rebuild_client()
    assert provider._current[2] is True, "状态不能被 rebuild 改回可用"
    with pytest.raises(ProviderRequestError, match="已关闭"):
        provider.get_realtime()


def test_failed_request_still_releases_its_lease(monkeypatch):
    """请求抛异常也必须归还租约, 否则旧客户端永远关不掉 (句柄/连接泄漏)。"""
    provider = _provider(monkeypatch)
    old = provider._current[0]

    def boom(method, url, **kwargs):
        raise RuntimeError("网络炸了")

    old.request = boom
    with pytest.raises(RuntimeError, match="网络炸了"):
        provider.get_realtime()

    provider.rebuild_client()
    assert old.is_closed, "异常后租约必须已归还, 旧客户端才能被关闭"


def test_concurrent_rebuild_and_requests_never_use_closed_client(monkeypatch):
    """并发重建 + 取数: 不能有请求打在已关闭的客户端上, 也不能死锁。"""
    provider = _provider(monkeypatch)
    errors: list[BaseException] = []

    def churn():
        try:
            for _ in range(80):
                provider.rebuild_client()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def fetch():
        try:
            for _ in range(80):
                provider.get_realtime()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=churn), threading.Thread(target=fetch)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
        assert not t.is_alive(), "并发下出现死锁"

    assert errors == []


# ------------------------------------------------------------------
# 真实 httpx: 证明在途请求不会被 close 打成 ReadTimeout
# ------------------------------------------------------------------

class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(0.35)
        body = b'{"data": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)  # 客户端提前断开 (测试主动关闭), 服务端无需处理

    def log_message(self, *args):
        pass


@pytest.fixture()
def slow_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_in_flight_request_survives_rebuild_with_real_httpx(slow_server):
    """回归: 自愈重建不能把正在跑的请求打成 ReadTimeout。"""
    provider = GenericHTTPProvider(
        CustomSourceConfig(
            name="real",
            display_name="Real",
            datasets={
                "realtime": DatasetConfig(url=slow_server, field_map={"c": "symbol"})
            },
        )
    )
    old_client = provider._current[0]
    result: list = []

    def fetch():
        try:
            provider.get_realtime()
            result.append("ok")
        except BaseException as e:  # noqa: BLE001
            result.append(f"{type(e).__name__}: {e}")

    worker = threading.Thread(target=fetch)
    worker.start()
    time.sleep(0.12)          # 让请求真正进入读响应阶段
    provider.rebuild_client()  # 换下但延迟关闭
    worker.join(10)

    assert result == ["ok"], f"在途请求被重建打断了: {result}"
    assert old_client.is_closed, "归还后旧客户端才关闭"
    assert provider.get_realtime() == []
    provider.close()
