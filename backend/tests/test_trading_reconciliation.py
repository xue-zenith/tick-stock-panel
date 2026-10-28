"""对账服务测试 — 本地与券商差异检测、clean 判定、arm 前置."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.trading import oms, reconciliation, store


@pytest.fixture()
def d(tmp_path: Path) -> Path:
    store.init_db(tmp_path)
    return tmp_path


def test_clean_reconciliation_enables_arm(d: Path):
    store.create_account(d, "acc1", "mock", "主")
    # 空 broker 报告 = 全部对账 (本地无订单, 无持仓)
    result = oms.trigger_reconciliation(d, "acc1", [], [], None)
    assert result["status"] == "clean"
    assert result["diff_count"] == 0
    ok, _ = reconciliation.can_arm(d, "acc1")
    assert ok is True


def test_dirty_reconciliation_blocks_arm(d: Path):
    store.create_account(d, "acc1", "mock", "主")
    # 券商有持仓但本地没有 → 差异
    result = oms.trigger_reconciliation(
        d, "acc1", [], [{"symbol": "600519.SH", "qty": 100, "available_qty": 100}], None,
    )
    assert result["status"] == "dirty"
    assert result["diff_count"] >= 1
    ok, reason = reconciliation.can_arm(d, "acc1")
    assert ok is False
    assert "差异" in reason


def test_unknown_order_unresolved_blocks_arm(d: Path):
    """本地 UNKNOWN 单未对账 → arm 前置 fail."""
    store.create_account(d, "acc1", "mock", "主")
    # 先建 intent (FK 要求)
    store.insert_intent(d, {
        "intent_id": "ti_u", "account_id": "acc1", "strategy_id": None,
        "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 10.0, "tif": "day",
        "source": "manual", "dedupe_key": "dk_u", "proposed_at": "x",
        "ruleset_version": "risk-v1",
    })
    # 模拟一个 UNKNOWN 状态订单 (broker_order_id=None + state=unknown)
    store.insert_order(d, {
        "order_id": "ord_u", "broker_order_id": None,
        "account_id": "acc1", "intent_id": "ti_u",
        "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 10.0,
        "state": "unknown",
        "submitted_at": None,
        "last_update_at": "x",
        "reject_reason": None,
    })
    # 空 broker 报告 = 券商无此单 → 本地 UNKNOWN 未解决
    result = oms.trigger_reconciliation(d, "acc1", [], [], None)
    assert result["status"] == "dirty"


def test_dirty_recon_degrades_mode(d: Path):
    """脏对账 → 账户模式被自动降为 DEGRADED."""
    store.create_account(d, "acc1", "mock", "主")
    store.set_account_mode(d, "acc1", "armed_manual")
    oms.trigger_reconciliation(
        d, "acc1", [], [{"symbol": "600519.SH", "qty": 100, "available_qty": 100}], None,
    )
    acc = store.get_account(d, "acc1")
    assert acc["mode"] == "degraded"
