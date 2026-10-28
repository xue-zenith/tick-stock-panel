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


def test_broker_query_recovers_unknown_order_then_requires_clean_recheck(d: Path):
    store.create_account(d, "acc1", "mock", "主")
    store.insert_intent(d, {
        "intent_id": "ti_recover", "account_id": "acc1", "strategy_id": None,
        "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 10.0, "tif": "day",
        "source": "manual", "dedupe_key": "dk_recover", "proposed_at": "x",
        "ruleset_version": "risk-v1",
    })
    store.insert_order(d, {
        "order_id": "ord_recover", "broker_order_id": "broker-1",
        "account_id": "acc1", "intent_id": "ti_recover",
        "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 10.0,
        "state": "unknown", "submitted_at": "x", "last_update_at": "x",
        "reject_reason": None,
    })
    broker_orders = [{
        "broker_order_id": "broker-1", "state": "submitted",
        "filled_qty": 0, "avg_price": None,
    }]

    first = oms.trigger_reconciliation(d, "acc1", broker_orders, [], None)
    assert first["status"] == "dirty"
    assert store.get_order(d, "ord_recover")["state"] == "submitted"
    assert reconciliation.can_arm(d, "acc1")[0] is False

    second = oms.trigger_reconciliation(d, "acc1", broker_orders, [], None)
    assert second["status"] == "clean"
    assert reconciliation.can_arm(d, "acc1")[0] is True


def test_dirty_recon_degrades_mode(d: Path):
    """脏对账 → 账户模式被自动降为 DEGRADED."""
    store.create_account(d, "acc1", "mock", "主")
    store.set_account_mode(d, "acc1", "armed_manual")
    oms.trigger_reconciliation(
        d, "acc1", [], [{"symbol": "600519.SH", "qty": 100, "available_qty": 100}], None,
    )
    acc = store.get_account(d, "acc1")
    assert acc["mode"] == "degraded"


def test_clean_reconciliation_resolves_old_diffs_and_returns_to_disarmed(d: Path):
    store.create_account(d, "acc1", "mock", "主")
    store.set_account_mode(d, "acc1", "armed_manual")
    oms.trigger_reconciliation(
        d, "acc1", [], [{"symbol": "600519.SH", "qty": 100, "available_qty": 100}], None,
    )
    assert store.get_account(d, "acc1")["mode"] == "degraded"

    # 同步券商持仓后再次对账应 clean，并解决历史差异；仍需人工重新 arm。
    store.replace_position_snapshots(
        d, "acc1", "2026-10-21T10:00:00+08:00",
        [{"symbol": "600519.SH", "qty": 100, "available_qty": 100}],
    )
    result = oms.trigger_reconciliation(
        d, "acc1", [], [{"symbol": "600519.SH", "qty": 100, "available_qty": 100}], None,
    )
    assert result["status"] == "clean"
    assert store.unresolved_diff_count(d, "acc1") == 0
    assert store.get_account(d, "acc1")["mode"] == "disarmed"


def test_reconciliation_detects_local_position_missing_at_broker(d: Path):
    store.create_account(d, "acc1", "mock", "主")
    store.replace_position_snapshots(
        d, "acc1", "2026-10-21T10:00:00+08:00",
        [{"symbol": "600519.SH", "qty": 100, "available_qty": 100}],
    )
    result = oms.trigger_reconciliation(d, "acc1", [], [], None)
    assert result["status"] == "dirty"
    diffs = store.list_recon_diffs(d, result["run_id"])
    assert any(row["diff_kind"] == "position" and row["broker_value"] == "0" for row in diffs)


def test_stale_clean_reconciliation_cannot_arm(d: Path):
    store.create_account(d, "acc1", "mock", "主")
    result = oms.trigger_reconciliation(d, "acc1", [], [], None)
    store._execute_write(d, lambda conn: conn.execute(
        "UPDATE reconciliation_runs SET finished_at = ? WHERE run_id = ?",
        ("2020-01-01T10:00:00+08:00", result["run_id"]),
    ))
    ok, reason = reconciliation.can_arm(d, "acc1")
    assert ok is False
    assert "过期" in reason
