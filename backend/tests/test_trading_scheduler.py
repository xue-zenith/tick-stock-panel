"""定时对账调度器测试 — 启动/关闭/周期对账/无适配器跳过/状态。

不跑真实周期 (避免 sleep); 用 asyncio 模拟时钟推进验证链路。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.trading import scheduler, store
from app.trading.brokers import registry as broker_registry
from app.trading.brokers.mock import MockBrokerAdapter, MockConfig


@pytest.fixture()
def d(tmp_path: Path) -> Path:
    store.init_db(tmp_path)
    return tmp_path


def test_get_scheduler_returns_none_before_start():
    # 单例可能被其他测试污染, 强制置空
    scheduler._scheduler = None
    assert scheduler.get_scheduler() is None


@pytest.mark.asyncio
async def test_start_scheduler_creates_singleton(d: Path):
    scheduler._scheduler = None
    s = scheduler.start_scheduler(d, orders_interval_s=0.1, positions_interval_s=0.2)
    assert s.running is True
    assert scheduler.get_scheduler() is s
    # 幂等: 再次 start 返回同一实例
    s2 = scheduler.start_scheduler(d)
    assert s2 is s
    await s.shutdown()
    scheduler._scheduler = None


@pytest.mark.asyncio
async def test_scheduler_status(d: Path):
    scheduler._scheduler = None
    s = scheduler.start_scheduler(d, orders_interval_s=5, positions_interval_s=10)
    status = s.status()
    assert status["running"] is True
    assert status["orders_interval_s"] == 5
    assert status["positions_interval_s"] == 10
    assert "mock" in status["registered_brokers"]
    await s.shutdown()
    scheduler._scheduler = None


@pytest.mark.asyncio
async def test_boot_reconcile_runs_on_start(d: Path):
    """启动即跑一次对账 (boot reconcile), 验证 system_events 有记录。"""
    scheduler._scheduler = None
    store.create_account(d, "acc1", "mock", "主账户")
    s = scheduler.start_scheduler(d, orders_interval_s=999, positions_interval_s=999)
    # 给 boot reconcile 一点时间执行
    await asyncio.sleep(0.3)
    events = store.recent_system_events(d, limit=10)
    kinds = [e["kind"] for e in events]
    assert "scheduled_reconcile" in kinds
    await s.shutdown()
    scheduler._scheduler = None


@pytest.mark.asyncio
async def test_no_adapter_skips_account(d: Path):
    """无注册适配器时不生成伪对账结果，并记录连接失败事件。"""
    scheduler._scheduler = None
    store.create_account(d, "acc1", "nonexistent_broker", "主")
    s = scheduler.start_scheduler(d, orders_interval_s=0.05, positions_interval_s=0.05)
    await asyncio.sleep(0.3)
    # 无适配器 → 对账跳过, 不产生 scheduled_reconcile 事件 (boot 也跳过)
    events = store.recent_system_events(d, limit=10)
    kinds = [e["kind"] for e in events]
    assert "scheduled_reconcile" not in kinds
    assert "scheduled_reconcile_failed" in kinds
    await s.shutdown()
    scheduler._scheduler = None


def test_reregister_disconnects_cached_alias_even_if_adapter_name_differs():
    class _AliasAdapter(MockBrokerAdapter):
        @property
        def name(self) -> str:
            return "sdk-internal-name"

    adapter = _AliasAdapter(account_id="acc1")
    adapter.connect()
    broker_registry.register("company-alias", lambda _account_id: adapter)
    assert broker_registry.get_adapter("company-alias", "acc1") is adapter

    broker_registry.register(
        "company-alias", lambda account_id: _AliasAdapter(account_id=account_id),
    )

    assert adapter.health()["connected"] is False
    broker_registry.unregister("company-alias")


@pytest.mark.asyncio
async def test_reconcile_account_pulls_from_broker(d: Path):
    """对账从 broker 适配器拉取订单/持仓/资金。"""
    scheduler._scheduler = None
    store.create_account(d, "acc1", "mock", "主账户")
    # 清理实例缓存 (避免前序测试的 mock:acc1 实例复用)
    broker_registry._INSTANCES.clear()
    # 注册一个自定义 mock 适配器, 注入已知持仓
    class _TestBroker(MockBrokerAdapter):
        def get_positions(self):
            return [{"symbol": "600519.SH", "qty": 100, "available_qty": 100, "avg_cost": 1500, "last_price": 1500}]
        def get_orders(self):
            return []
        def get_account(self):
            return {"cash": 800000, "frozen_cash": 0, "total": 950000, "market_value": 150000}
    broker_registry.register("mock", lambda acc_id: _TestBroker(account_id=acc_id))
    broker_registry._INSTANCES.clear()  # 注册后再次清理确保新工厂生效
    try:
        s = scheduler.start_scheduler(d, orders_interval_s=0.05, positions_interval_s=0.05)
        await asyncio.sleep(0.3)
        # 应该有对账差异 (本地无持仓, 券商有 600519 100 股)
        runs = store.list_recon_runs(d, "acc1")
        assert len(runs) > 0
        assert runs[0]["diff_count"] >= 1
        await s.shutdown()
    finally:
        # 恢复默认 mock 工厂 + 清理缓存
        broker_registry.register("mock", lambda acc_id: MockBrokerAdapter(
            account_id=acc_id, initial_cash=1_000_000, config=MockConfig(latency_ms=0),
        ))
        broker_registry._INSTANCES.clear()
        scheduler._scheduler = None


@pytest.mark.asyncio
async def test_stop_scheduler_sets_not_running(d: Path):
    scheduler._scheduler = None
    s = scheduler.start_scheduler(d, orders_interval_s=999)
    assert s.running is True
    await scheduler.stop_scheduler()
    assert scheduler.get_scheduler() is None
