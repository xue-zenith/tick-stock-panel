"""交易域 SQLite 存储测试 — schema/迁移/幂等/事务。

镜像 test_paper_trading.py 的 _cap_account 模式; 用 tmp_path 隔离。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.trading import store


@pytest.fixture()
def d(tmp_path: Path) -> Path:
    store.init_db(tmp_path)
    return tmp_path


def test_init_db_creates_schema(d: Path):
    """建库后 14 张业务表 + schema_version 存在."""
    conn = store._connect(d)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [r[0] for r in rows]
    conn.close()
    for t in [
        "broker_accounts", "trade_intents", "risk_decisions", "approvals",
        "orders", "order_events", "executions", "account_snapshots",
        "position_snapshots", "strategy_allocations", "autonomy_grants",
        "reconciliation_runs", "reconciliation_diffs", "system_events",
    ]:
        assert t in names, f"缺表 {t}"


def test_init_db_idempotent(d: Path):
    """重复 init_db 不破坏数据."""
    store.create_account(d, "acc1", "mock", "主")
    store.init_db(d)  # 再次迁移
    store.init_db(d)
    assert store.get_account(d, "acc1") is not None


def test_wal_mode_enabled(d: Path):
    """PRAGMA journal_mode=WAL (个人单机部署的关键配置)."""
    conn = store._connect(d)
    mode = conn.execute("PRAGMA journal_mode").fetchone()
    conn.close()
    assert mode[0].lower() == "wal"


def test_dedupe_key_unique(d: Path):
    """同 dedupe_key 第二次插入必须失败 (幂等不变量)."""
    store.create_account(d, "acc1", "mock", "主")
    intent = {
        "intent_id": "ti_1", "account_id": "acc1", "strategy_id": None,
        "symbol": "600519.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 1500.0, "tif": "day",
        "source": "manual", "dedupe_key": "dk1", "proposed_at": "x",
        "ruleset_version": "risk-v1",
    }
    store.insert_intent(d, intent)
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_intent(d, {**intent, "intent_id": "ti_2"})  # 同 dedupe_key


def test_order_state_change_writes_audit_event(d: Path):
    """状态变更必须同事务写 order_events (审计不变量)."""
    store.create_account(d, "acc1", "mock", "主")
    # 先建 intent (FK 要求)
    store.insert_intent(d, {
        "intent_id": "ti_x", "account_id": "acc1", "strategy_id": None,
        "symbol": "600519.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 1500.0, "tif": "day",
        "source": "manual", "dedupe_key": "dk_x", "proposed_at": "x",
        "ruleset_version": "risk-v1",
    })
    store.insert_order(d, {
        "order_id": "ord_1", "broker_order_id": None,
        "account_id": "acc1", "intent_id": "ti_x",
        "symbol": "600519.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 1500.0,
        "state": "approved",
        "submitted_at": None,
        "last_update_at": "x",
        "reject_reason": None,
    })
    store.update_order_state(
        d, "ord_1", "submitted",
        event_from_state="approved", event_reason="submit_to_broker",
    )
    events = store.list_order_events(d, "ord_1")
    assert len(events) == 1
    assert events[0]["from_state"] == "approved"
    assert events[0]["to_state"] == "submitted"
    assert events[0]["reason"] == "submit_to_broker"


def test_execution_dedupe(d: Path):
    """同 dedupe_key 成交回报二次插入返回 False (幂等)."""
    store.create_account(d, "acc1", "mock", "主")
    store.insert_intent(d, {
        "intent_id": "ti_x", "account_id": "acc1", "strategy_id": None,
        "symbol": "600519.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 1500.0, "tif": "day",
        "source": "manual", "dedupe_key": "dk_x", "proposed_at": "x",
        "ruleset_version": "risk-v1",
    })
    store.insert_order(d, {
        "order_id": "ord_1", "broker_order_id": None,
        "account_id": "acc1", "intent_id": "ti_x",
        "symbol": "600519.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 1500.0,
        "state": "submitted",
        "submitted_at": "x",
        "last_update_at": "x",
        "reject_reason": None,
    })
    exec_row = {
        "exec_id": "ex_1", "order_id": "ord_1", "broker_order_id": "bk_1",
        "broker_trade_id": "bt_1", "account_id": "acc1",
        "symbol": "600519.SH", "side": "buy", "qty": 100,
        "price": 1500.0, "fee": 0.0, "ts": "x",
        "dedupe_key": "de_1",
    }
    assert store.insert_execution(d, exec_row) is True
    assert store.insert_execution(d, exec_row) is False  # 重复回报