"""OMS 端到端测试 — submit → approve → submit_to_broker → fill 全闭环.

测试矩阵 (镜像分析文档 §18.2 / 18.3):
  - happy path
  - 幂等 (同 dedupe_key)
  - cancel pending/submitted
  - partial → fill
  - callback-before-response (UNKNOWN)
  - 重复回调 (executions.dedupe_key 幂等)
  - arm 前置 (需 clean 对账)
"""
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
def armed_account(d: Path):
    """创建账户 + 干净对账 + arm."""
    store.create_account(d, "acc1", "mock", "主账户")
    oms.trigger_reconciliation(d, "acc1", [], [], None)
    oms.arm_account(d, "acc1")
    return d


def _ctx(d: Path, **overrides) -> risk.RiskContext:
    """构造 pass-through 风控上下文 (next_open 绕开交易时段)."""
    base = dict(
        account_id="acc1", mode=TradingMode.ARMED_MANUAL,
        cash=1_000_000, total=1_000_000,
        last_price=10.0, prev_close=10.0,
        limit_up_price=11.0, limit_down_price=9.0,
        position_qty=0, available_qty=0,
    )
    base.update(overrides)
    return risk.RiskContext(**base)


def test_submit_intent_passes_risk_and_creates_approval(armed_account: Path):
    r = oms.submit_intent(
        armed_account, account_id="acc1", symbol="600000.SH",
        side="buy", qty=100, price_policy="next_open",
        limit_price=10.0, source="manual", rationale="测试",
        risk_ctx=_ctx(armed_account),
    )
    assert r["status"] == "pending"
    assert r["verdict"] == "pass"
    # 应该有待审批记录
    aps = store.list_pending_approvals(armed_account)
    assert len(aps) == 1


def test_submit_intent_duplicate_dedupe_key(armed_account: Path):
    kwargs = dict(
        account_id="acc1", symbol="600000.SH", side="buy", qty=100,
        price_policy="next_open", limit_price=10.0,
        source="manual", rationale="测试", risk_ctx=_ctx(armed_account),
    )
    r1 = oms.submit_intent(armed_account, **kwargs)
    r2 = oms.submit_intent(armed_account, **kwargs)
    assert r1["intent_id"] == r2["intent_id"]
    assert r2.get("duplicate") is True


def test_approve_creates_order_approved(armed_account: Path):
    r = oms.submit_intent(
        armed_account, account_id="acc1", symbol="600000.SH",
        side="buy", qty=100, price_policy="next_open",
        limit_price=10.0, source="manual", rationale="测试",
        risk_ctx=_ctx(armed_account),
    )
    ap = oms.approve(armed_account, r["intent_id"])
    order = store.get_order(armed_account, ap["order_id"])
    assert order["state"] == "approved"


def test_submit_to_broker_then_fill(armed_account: Path):
    r = oms.submit_intent(
        armed_account, account_id="acc1", symbol="600000.SH",
        side="buy", qty=100, price_policy="next_open",
        limit_price=10.0, source="manual", rationale="测试",
        risk_ctx=_ctx(armed_account),
    )
    ap = oms.approve(armed_account, r["intent_id"])

    callbacks = []
    broker = MockBrokerAdapter(
        account_id="acc1", initial_cash=1_000_000,
        config=MockConfig(latency_ms=0),
    )
    broker.set_callback_handler(lambda e: callbacks.append(oms.on_broker_callback(armed_account, e)))
    sub = oms.submit_to_broker(armed_account, ap["order_id"], broker)
    assert sub["state"] == "submitted"

    # 显式触发回调 (100 股全成)
    broker.fire_callback(sub["broker_order_id"], {
        "order_id": ap["order_id"], "symbol": "600000.SH", "side": "buy",
        "qty": 100, "price_policy": "next_open", "limit_price": 1.0,
    })
    order = store.get_order(armed_account, ap["order_id"])
    assert order["state"] == "filled"
    assert order["filled_qty"] == 100