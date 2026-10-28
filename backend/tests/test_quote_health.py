"""行情源健康检查测试 (不真退出进程、不真发 HTTP)。"""
from __future__ import annotations

import asyncio

import pytest

from app.watchdog import (
    QuoteHealthWatcher,
    QuotePoolStale,
    SourceUnavailable,
    quote_probe,
)


# ================================================================
# Fake 协作对象
# ================================================================


class FakeQuoteService:
    """可控的 QuoteService 替身, 暴露 status() / rebuild_realtime_client()。"""

    def __init__(self, status: dict) -> None:
        self._status = status
        self.rebuild_calls = 0

    def status(self) -> dict:
        return self._status

    def rebuild_realtime_client(self) -> None:
        self.rebuild_calls += 1


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"code": 0, "message": "success"}

    def json(self) -> dict:
        return self._payload


class FakeHTTPClient:
    """httpx.Client 替身; get() 按脚本返回预设响应或抛异常。"""

    def __init__(self, responses: list) -> None:
        # responses: 每项是 FakeResponse 或 BaseException (抛出)
        self._responses = list(responses)
        self._idx = 0

    def get(self, *args, **kwargs) -> FakeResponse:
        if self._idx >= len(self._responses):
            # 脚本耗尽后恒为成功
            return FakeResponse()
        item = self._responses[self._idx]
        self._idx += 1
        if isinstance(item, BaseException):
            raise item
        return item


# ================================================================
# Provider fixtures (quote_probe 内动态读 preferences)
# ================================================================


@pytest.fixture
def fuyao_provider(monkeypatch):
    """让 quote_probe 看到 provider=fuyao。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "fuyao")


@pytest.fixture
def non_fuyao_provider(monkeypatch):
    """让 quote_probe 看到 provider=tdxquant (模拟运行中切换)。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tdxquant")


# ================================================================
# 驱动器
# ================================================================


async def _run_watcher(
    probe_results: list,
    *,
    rebuild_threshold: int = 3,
    exit_threshold: int = 10,
    interval: float = 0.01,
    timeout: float = 0.5,
) -> tuple[list[int], int]:
    """跑一轮 watcher, 返回 (exit_codes, rebuild_calls)。

    不返回 stale_count: 脚本耗尽后驱动器恒为成功探测, 会把计数归零,
    末尾的 stale_count 不稳定 (取决于 stop 时机)。
    """
    exits: list[int] = []
    rebuilds: list[int] = []
    idx = 0

    def probe() -> None:
        nonlocal idx
        if idx < len(probe_results):
            result = probe_results[idx]
            idx += 1
            if isinstance(result, BaseException):
                raise result
        # 脚本耗尽后恒为成功 (不抛)

    watcher = QuoteHealthWatcher(
        probe,
        rebuild_cb=lambda: rebuilds.append(1),
        exit_cb=exits.append,
        interval_s=interval,
        probe_timeout_s=timeout,
        rebuild_threshold=rebuild_threshold,
        exit_threshold=exit_threshold,
    )
    watcher.start()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if exits or watcher._task.done():
            break
    await watcher.stop()
    return exits, len(rebuilds)


# ================================================================
# 场景 (a) 交易窗口内 age 健康 → 不动作
# ================================================================


async def test_healthy_no_action() -> None:
    """age 未陈旧: 不重建、不退出。"""
    exits, rebuilds = await _run_watcher([None, None, None])
    assert exits == []
    assert rebuilds == 0


# ================================================================
# 场景 (b) age 陈旧 + 探针可达 → 第 rebuild_threshold 次重建
# ================================================================


async def test_stale_and_reachable_triggers_rebuild() -> None:
    """连续 3 次 QuotePoolStale → rebuild_cb 在第 3 次被调用, 不退出。"""
    stale_exc = QuotePoolStale("stale")
    exits, rebuilds = await _run_watcher(
        [stale_exc, stale_exc, stale_exc],
        rebuild_threshold=3,
        exit_threshold=10,
    )
    assert exits == []  # 未达 exit 阈值
    assert rebuilds == 1  # 恰在第 3 次重建一次


# ================================================================
# 场景 (c) 重建后恢复 → 计数归零、重建标记复位
# ================================================================


async def test_rebuild_then_recover_resets_counters() -> None:
    """3 次 stale → 重建 → 成功探测 → 计数归零, 再 2 次 stale 不重建 (未到阈值)。"""
    stale_exc = QuotePoolStale("stale")
    exits, rebuilds = await _run_watcher(
        [stale_exc, stale_exc, stale_exc, None, stale_exc, stale_exc],
        rebuild_threshold=3,
        exit_threshold=10,
    )
    assert exits == []
    assert rebuilds == 1  # 只重建过一次 (恢复后重新计数, 2 次未到阈值 3)


# ================================================================
# 场景 (d) 重建后仍失败到 exit_threshold → exit_cb(70)
# ================================================================


async def test_rebuild_failure_escalates_to_exit() -> None:
    """3 次重建 → 仍持续失败到 10 次 → exit_cb(70)。"""
    stale_exc = QuotePoolStale("stale")
    exits, rebuilds = await _run_watcher(
        [stale_exc] * 10,
        rebuild_threshold=3,
        exit_threshold=10,
    )
    assert exits == [70]
    assert rebuilds == 1  # 只重建一次 (已标记 _rebuilt)


# ================================================================
# 场景 (e) 探针 500 (SourceUnavailable) → 不重建
# ================================================================


async def test_source_unavailable_no_rebuild() -> None:
    """源不可达不计入僵死计数, 不重建。"""
    src_exc = SourceUnavailable("probe HTTP 500")
    exits, rebuilds = await _run_watcher(
        [src_exc, src_exc, src_exc, src_exc],
        rebuild_threshold=3,
        exit_threshold=10,
    )
    assert exits == []
    assert rebuilds == 0  # SourceUnavailable 不计僵死


# ================================================================
# quote_probe 单元测试 (双信号诊断逻辑)
# ================================================================


def _make_status(*, age_ms, polling=True, interval=6.0, paused=False) -> dict:
    return {
        "is_polling_window": polling,
        "quote_age_ms": age_ms,
        "interval_s": interval,
        "paused": paused,
    }


def test_probe_non_polling_window_returns_clean(fuyao_provider) -> None:
    """休市不探: probe 正常返回 (不抛)。"""
    qs = FakeQuoteService(_make_status(age_ms=999_999, polling=False))
    http = FakeHTTPClient([])  # 不会被调用
    quote_probe(qs, http)  # 不抛即通过


def test_probe_healthy_age_returns_clean(fuyao_provider) -> None:
    """age 未陈旧: probe 正常返回 (不抛, 不调探针)。"""
    qs = FakeQuoteService(_make_status(age_ms=1000, polling=True, interval=6.0))
    # stale_threshold = 6 * 1000 * 3 = 18000ms; age=1000 远小于 → 健康
    http = FakeHTTPClient([RuntimeError("不该被调用")])  # 探针不该触发
    quote_probe(qs, http)  # 不抛即通过


def test_probe_stale_and_reachable_raises_pool_stale(fuyao_provider) -> None:
    """age 陈旧 + 探针可达 → QuotePoolStale。"""
    qs = FakeQuoteService(_make_status(age_ms=60_000, polling=True, interval=6.0))
    http = FakeHTTPClient([FakeResponse(200, {"code": 0})])
    try:
        quote_probe(qs, http)
    except QuotePoolStale:
        return
    raise AssertionError("应抛 QuotePoolStale")


def test_probe_stale_and_source_down_raises_source_unavailable(fuyao_provider) -> None:
    """age 陈旧 + 探针 HTTP 500 → SourceUnavailable (不重建)。"""
    qs = FakeQuoteService(_make_status(age_ms=60_000, polling=True, interval=6.0))
    http = FakeHTTPClient([FakeResponse(500)])
    try:
        quote_probe(qs, http)
    except SourceUnavailable:
        return
    raise AssertionError("应抛 SourceUnavailable")


def test_probe_stale_and_network_error_raises_source_unavailable(fuyao_provider) -> None:
    """age 陈旧 + 探针网络异常 → SourceUnavailable。"""
    qs = FakeQuoteService(_make_status(age_ms=60_000, polling=True, interval=6.0))
    http = FakeHTTPClient([RuntimeError("connection reset")])
    try:
        quote_probe(qs, http)
    except SourceUnavailable:
        return
    raise AssertionError("应抛 SourceUnavailable")


def test_probe_stale_and_fuyao_code_error_raises_source_unavailable(fuyao_provider) -> None:
    """age 陈旧 + 探针返回 code != 0 → SourceUnavailable (源侧错误)。"""
    qs = FakeQuoteService(_make_status(age_ms=60_000, polling=True, interval=6.0))
    http = FakeHTTPClient([FakeResponse(200, {"code": 4001, "message": "rate limited"})])
    try:
        quote_probe(qs, http)
    except SourceUnavailable:
        return
    raise AssertionError("应抛 SourceUnavailable")


# ================================================================
# 场景 (f) provider 切换感知: 非 fuyao 时不探 (不抛、不调探针)
# ================================================================


def test_probe_skips_when_provider_not_fuyao(non_fuyao_provider) -> None:
    """provider=tdxquant 时 probe 直接返回 (不探, 不抛, 不调探针)。"""
    qs = FakeQuoteService(_make_status(age_ms=999_999, polling=True, interval=6.0))
    # age 陈旧 + polling=True, 但 provider 非 fuyao → 应直接返回
    http = FakeHTTPClient([RuntimeError("不该被调用")])  # 探针不该触发
    quote_probe(qs, http)  # 不抛即通过


# ================================================================
# 场景 (g) pipeline 暂停期间不探 (paused=True 时跳过, 避免误判)
# ================================================================


def test_probe_skips_when_paused(fuyao_provider) -> None:
    """quote_service.paused=True 时 probe 直接返回 (pipeline 在跑, age 涨是正常的)。"""
    qs = FakeQuoteService(
        _make_status(age_ms=999_999, polling=True, interval=6.0, paused=True)
    )
    # age 陈旧 + polling=True + paused=True → 应直接返回, 不触发探针
    http = FakeHTTPClient([RuntimeError("不该被调用")])
    quote_probe(qs, http)  # 不抛即通过
