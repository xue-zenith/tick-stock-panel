"""行情源健康检查测试 (不真退出进程、不真发 HTTP)。"""
from __future__ import annotations

import asyncio

import pytest

from app.watchdog import (
    QuoteHealthWatcher,
    QuotePoolStale,
    SleepTracker,
    SourceUnavailable,
    quote_probe,
    quote_stale_threshold_ms,
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


def _stale_age_ms(interval: float = 6.0) -> int:
    """超过陈旧阈值 1s 的 age, 不依赖阈值具体取值。"""
    return int(quote_stale_threshold_ms(interval)) + 1000


def test_probe_healthy_age_returns_clean(fuyao_provider) -> None:
    """age 未陈旧: probe 正常返回 (不抛, 不调探针)。"""
    qs = FakeQuoteService(_make_status(age_ms=1000, polling=True, interval=6.0))
    http = FakeHTTPClient([RuntimeError("不该被调用")])  # 探针不该触发
    quote_probe(qs, http)  # 不抛即通过


def test_probe_skips_evaluation_sized_gap(fuyao_provider) -> None:
    """监控评估耗时的 age (20s) 不算陈旧 —— 2026-11-18 误报回归。"""
    qs = FakeQuoteService(_make_status(age_ms=20_000, polling=True, interval=6.0))
    http = FakeHTTPClient([RuntimeError("不该被调用")])  # 探针不该触发
    quote_probe(qs, http)  # 不抛即通过


def test_probe_stale_and_reachable_raises_pool_stale(fuyao_provider) -> None:
    """age 陈旧 + 探针可达 → QuotePoolStale。"""
    qs = FakeQuoteService(_make_status(age_ms=_stale_age_ms(), polling=True, interval=6.0))
    http = FakeHTTPClient([FakeResponse(200, {"code": 0})])
    try:
        quote_probe(qs, http)
    except QuotePoolStale:
        return
    raise AssertionError("应抛 QuotePoolStale")


def test_probe_stale_and_source_down_raises_source_unavailable(fuyao_provider) -> None:
    """age 陈旧 + 探针 HTTP 500 → SourceUnavailable (不重建)。"""
    qs = FakeQuoteService(_make_status(age_ms=_stale_age_ms(), polling=True, interval=6.0))
    http = FakeHTTPClient([FakeResponse(500)])
    try:
        quote_probe(qs, http)
    except SourceUnavailable:
        return
    raise AssertionError("应抛 SourceUnavailable")


def test_probe_stale_and_network_error_raises_source_unavailable(fuyao_provider) -> None:
    """age 陈旧 + 探针网络异常 → SourceUnavailable。"""
    qs = FakeQuoteService(_make_status(age_ms=_stale_age_ms(), polling=True, interval=6.0))
    http = FakeHTTPClient([RuntimeError("connection reset")])
    try:
        quote_probe(qs, http)
    except SourceUnavailable:
        return
    raise AssertionError("应抛 SourceUnavailable")


def test_probe_stale_and_fuyao_code_error_raises_source_unavailable(fuyao_provider) -> None:
    """age 陈旧 + 探针返回 code != 0 → SourceUnavailable (源侧错误)。"""
    qs = FakeQuoteService(_make_status(age_ms=_stale_age_ms(), polling=True, interval=6.0))
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


# ================================================================
# 场景 (h) 陈旧阈值 = max(interval × stale_intervals, 绝对下限)
# ================================================================


def test_stale_threshold_floor_dominates_fast_polling() -> None:
    """快轮询 (1-30s) 全部落在绝对下限上, 不随 interval 缩小。"""
    floor_ms = 60_000
    for interval in (1.0, 3.0, 6.0, 10.0, 20.0):
        assert quote_stale_threshold_ms(interval) == floor_ms


def test_stale_threshold_relative_wins_on_slow_polling() -> None:
    """慢轮询 (30s × 3 = 90s / 60s × 3 = 180s) 超过下限, 用相对值。"""
    assert quote_stale_threshold_ms(30.0) == 90_000
    assert quote_stale_threshold_ms(60.0) == 180_000


def test_stale_threshold_tracks_stale_intervals(monkeypatch) -> None:
    """调大 stale_intervals 时慢轮询阈值随之放大。"""
    from app.config import settings

    monkeypatch.setattr(settings, "quote_health_stale_intervals", 5)
    assert quote_stale_threshold_ms(60.0) == 300_000


# ================================================================
# 场景 (i) 睡眠唤醒检测: 睡眠期间的旧 quote_age 不作数
#
# 2026-11-18 实况: 11:35:51 最后刷新 → 午休不轮询 → 12:35:29 起反复睡眠
# → 13:09:02 DarkWake 瞬间探针先跑, quote_age=3628s (睡眠前旧值) 被判池僵死。
# 实测 7 次大值触发 7/7 落在 DarkWake 时刻。
# ================================================================


class _FakeClock:
    """可控的 (墙钟, 单调钟) 对; 睡眠只推进墙钟。"""

    def __init__(self) -> None:
        self.wall = 1_000_000.0
        self.mono = 500.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, s: float) -> None:
        """正常运行: 两个钟同步推进。"""
        self.wall += s
        self.mono += s

    def sleep(self, s: float) -> None:
        """系统睡眠: 只有墙钟推进。"""
        self.wall += s


def _make_tracker(clock: _FakeClock, **kwargs) -> SleepTracker:
    return SleepTracker(wall_clock=clock.time, mono_clock=clock.monotonic, **kwargs)


def test_sleep_tracker_ignores_normal_runtime() -> None:
    """正常运行 (两钟同步推进) 不报睡眠。"""
    clock = _FakeClock()
    tracker = _make_tracker(clock)

    clock.advance(120.0)
    assert tracker.slept_since_last_check() == 0.0

    clock.advance(3600.0)  # 连续跑一小时也不该误报
    assert tracker.slept_since_last_check() == 0.0


def test_sleep_tracker_detects_sleep_duration() -> None:
    """睡眠 948s 被准确测出。"""
    clock = _FakeClock()
    tracker = _make_tracker(clock)

    clock.advance(60.0)
    tracker.slept_since_last_check()  # 建立基线

    clock.sleep(948.0)
    assert tracker.slept_since_last_check() == 948.0


def test_sleep_tracker_baseline_advances_after_sleep() -> None:
    """睡眠只报一次, 不会在后续轮次重复计入。"""
    clock = _FakeClock()
    tracker = _make_tracker(clock)

    clock.sleep(600.0)
    assert tracker.slept_since_last_check() == 600.0
    assert tracker.slept_since_last_check() == 0.0  # 基线已推进

    clock.advance(30.0)
    assert tracker.slept_since_last_check() == 0.0


def test_sleep_tracker_ignores_short_jitter() -> None:
    """调度抖动/时钟微调 (2s) 不算睡眠。"""
    clock = _FakeClock()
    tracker = _make_tracker(clock)

    clock.sleep(2.0)
    assert tracker.slept_since_last_check() == 0.0


def test_watcher_sleep_tolerance_derives_from_stale_threshold(monkeypatch) -> None:
    """容差必须随陈旧阈值缩放, 否则调小阈值后睡眠漏检会让误报复发。

    误报必要条件: quote_age >= 阈值, 而 quote_age >= 睡眠时长
    ⇒ 任何 ≥ 阈值的睡眠都必须能被测到, 即容差必须 < 阈值。
    """
    from app.config import settings

    for floor_s in (60.0, 20.0, 10.0):
        monkeypatch.setattr(settings, "quote_health_min_stale_s", floor_s)
        watcher = QuoteHealthWatcher(
            lambda: None,
            rebuild_cb=lambda: None,
            exit_cb=lambda code: None,
        )
        assert watcher._sleep_tracker._tolerance_s < floor_s


def _run_once_sync(watcher: QuoteHealthWatcher) -> None:
    asyncio.run(watcher._run_once())


def _watcher_with_clock(clock: _FakeClock, **kwargs) -> QuoteHealthWatcher:
    watcher = QuoteHealthWatcher(**kwargs)
    watcher._sleep_tracker = _make_tracker(clock)
    return watcher


def test_watcher_discards_probe_after_sleep() -> None:
    """唤醒后第一轮探测作废: stale 不计数, 不重建, 不退出。"""
    clock = _FakeClock()
    probes: list[int] = []
    rebuilds: list[int] = []
    exits: list[int] = []
    stale_exc = QuotePoolStale("quote_age=3628271ms > 18000ms 但探针可达源 → 池僵死")

    def probe() -> None:
        probes.append(1)
        raise stale_exc

    watcher = _watcher_with_clock(
        clock,
        probe=probe,
        rebuild_cb=lambda: rebuilds.append(1),
        exit_cb=exits.append,
        rebuild_threshold=1,  # 阈值放到 1: 若不跳过, 第一次就会重建
        exit_threshold=10,
    )

    clock.sleep(948.0)  # 睡眠 → 唤醒瞬间探针读到睡前旧 age
    _run_once_sync(watcher)

    assert probes == []  # 探针根本没跑
    assert rebuilds == []  # 没有误重建
    assert exits == []
    assert watcher._sleep_skips == 1
    assert watcher._stale_count == 0
    assert watcher._last_sleep_s == 948.0


def test_watcher_judges_normally_after_sleep_round_skipped() -> None:
    """作废一轮后, 下一轮恢复正常判定 (该重建还是要重建)。"""
    clock = _FakeClock()
    rebuilds: list[int] = []
    stale_exc = QuotePoolStale("stale")
    calls = {"n": 0}

    def probe() -> None:
        calls["n"] += 1
        raise stale_exc

    watcher = _watcher_with_clock(
        clock,
        probe=probe,
        rebuild_cb=lambda: rebuilds.append(1),
        exit_cb=lambda code: None,
        rebuild_threshold=2,
        exit_threshold=10,
    )

    clock.sleep(300.0)
    _run_once_sync(watcher)  # 作废
    assert calls["n"] == 0
    assert rebuilds == []

    _run_once_sync(watcher)  # 真探测 1
    assert calls["n"] == 1
    assert rebuilds == []

    _run_once_sync(watcher)  # 真探测 2 → 达阈值重建
    assert rebuilds == [1]  # 重建发生
    # 达阈值后 watcher 会立刻补一次「重建后验证探测」, 故为 3 次而非 2 次
    assert calls["n"] == 3


def test_watcher_sleep_does_not_clear_ongoing_stale_cycle() -> None:
    """睡眠作废不能把正在累积的僵死计数清零 (否则真僵死会被睡眠掩盖)。"""
    clock = _FakeClock()
    stale_exc = QuotePoolStale("stale")
    watcher = _watcher_with_clock(
        clock,
        probe=lambda: (_ for _ in ()).throw(stale_exc),
        rebuild_cb=lambda: None,
        exit_cb=lambda code: None,
        rebuild_threshold=5,
        exit_threshold=10,
    )

    _run_once_sync(watcher)
    _run_once_sync(watcher)
    assert watcher._stale_count == 2

    clock.sleep(400.0)
    _run_once_sync(watcher)  # 作废, 但计数保持

    assert watcher._stale_count == 2
    assert watcher._sleep_skips == 1


