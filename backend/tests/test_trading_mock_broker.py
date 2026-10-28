"""MockBrokerAdapter 测试 — 可配置延迟/部分成交/重复回调/回调先于响应."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.trading import oms, risk, store
from app.trading.brokers.mock import MockBrokerAdapter, MockConfig
from app.trading.contracts import TradingMode


@pytest.fixture()
def d(tmp_path: Path) -> Path:
    store.init_db(tmp_path)
    return tmp_path


@pytest.fixture()
def armed(d: Path) -> Path:
    store.create_account(d, "acc1", "mock", "主账户")
    oms.trigger_reconciliation(d, "acc1", [], [], None)
    oms.arm_account(d, "acc1")
    return d


def _ctx(d: Path) -> risk.RiskContext:
    return risk.build_ctx(
        account_id="acc1", mode=TradingMode.ARMED_MANUAL,
        cash=1_000_000, total=1_000_000,
        last_price=10.0, prev_close=10.0,
        limit_up_price=11.0, limit_down_price=9.0,
        position_qty=0, available_qty=0,
    )


def test_submit_returns_broker_order_id():
    broker = MockBrokerAdapter(account_id="acc1", config=MockConfig(latency_ms=0))
    r = broker.submit({
        "order_id": "ord_x", "symbol": "600000.SH", "side": "buy",
        "qty": 100, "price_policy": "limit", "limit_price": 1.0,
    })
    assert r is not None
    assert "broker_order_id" in r


def test_fire_callback_marks_filled(armed: Path):
    """显式触发回调 → 订单 FILLED."""
    broker = MockBrokerAdapter(account_id="acc1", config=MockConfig(latency_ms=0))
    broker.set_callback_handler(lambda e: oms.on_broker_callback(armed, e))

    intent_r = oms.submit_intent(
        armed, account_id="acc1", symbol="600000.SH", side="buy", qty=100,
        price_policy="next_open", limit_price=10.0,
        source="manual", rationale="t", risk_ctx=_ctx(armed),
    )
    assert intent_r["status"] == "pending"
    ap = oms.approve(armed, intent_r["intent_id"])
    sub = oms.submit_to_broker(armed, ap["order_id"], broker)
    bid = sub["broker_order_id"]

    broker.fire_callback(bid, {
        "order_id": ap["order_id"], "symbol": "600000.SH", "side": "buy",
        "qty": 100, "price_policy": "next_open", "limit_price": 10.0,
    })
    order = store.get_order(armed, ap["order_id"])
    assert order["state"] == "filled"
    assert order["filled_qty"] == 100


def test_duplicate_callback_idempotent(armed: Path):
    """同 broker_trade_id 的成交回报二次回调 → 只入账一次 (幂等)."""
    broker = MockBrokerAdapter(account_id="acc1", config=MockConfig(latency_ms=0))
    captured = []
    broker.set_callback_handler(lambda e: (captured.append(e), oms.on_broker_callback(armed, e))[1])

    intent_r = oms.submit_intent(
        armed, account_id="acc1", symbol="600000.SH", side="buy", qty=100,
        price_policy="next_open", limit_price=10.0,
        source="manual", rationale="t", risk_ctx=_ctx(armed),
    )
    ap = oms.approve(armed, intent_r["intent_id"])
    sub = oms.submit_to_broker(armed, ap["order_id"], broker)
    bid = sub["broker_order_id"]

    # 第一次: 全成交
    r1 = broker.fire_callback(bid, {
        "order_id": ap["order_id"], "symbol": "600000.SH", "side": "buy",
        "qty": 100, "price_policy": "next_open", "limit_price": 10.0,
    })
    assert r1["inserted"] is True
    # 第二次: 同 broker_trade_id → 幂等忽略
    r2 = broker.fire_callback(bid, {
        "order_id": ap["order_id"], "symbol": "600000.SH", "side": "buy",
        "qty": 100, "price_policy": "next_open", "limit_price": 10.0,
    })
    # 第二次 fire_callback 因为 MockBroker 重新计算 remaining=0 返回 inserted=False
    assert r2["inserted"] is False

    order = store.get_order(armed, ap["order_id"])
    assert order["filled_qty"] == 100  # 没翻倍


def test_cancel_after_submit(armed: Path):
    """提交券商后撤单 → SUBMITTED → CANCELLED."""
    broker = MockBrokerAdapter(account_id="acc1", config=MockConfig(latency_ms=0))
    intent_r = oms.submit_intent(
        armed, account_id="acc1", symbol="600000.SH", side="buy", qty=100,
        price_policy="next_open", limit_price=10.0,
        source="manual", rationale="t", risk_ctx=_ctx(armed),
    )
    ap = oms.approve(armed, intent_r["intent_id"])
    oms.submit_to_broker(armed, ap["order_id"], broker)
    r = oms.cancel(armed, ap["order_id"], broker)
    assert r["state"] == "cancelled"
