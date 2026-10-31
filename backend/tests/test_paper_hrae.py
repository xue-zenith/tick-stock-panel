"""HRAE 模拟盘实时退出的确定性规则测试。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.strategy import paper, paper_auto
from app.tickflow.repository import DataStore, KlineRepository

SYM = "002893.SZ"
DAY = date(2026, 10, 8)


def _ready_account(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(pl.DataFrame({
        "symbol": [SYM], "date": [DAY - timedelta(days=1)],
        "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
        "volume": [10000.0], "amount": [100000.0],
    }))
    monkeypatch.setattr(paper, "cn_today", lambda: DAY - timedelta(days=1))
    paper.create_account(tmp_path, 100_000.0)
    order, err = paper.create_order(tmp_path, SYM, "buy", qty=500, ref_price=10.0)
    assert err is None and order
    assert len(paper.evaluate_intraday(tmp_path, {SYM: 10.0})) == 1
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper._materialize(tmp_path)


def _rule(**overrides):
    rule = {
        "name": "HRAE 风向退出", "match_kind": "wind", "match_id": "hrae",
        "side": "sell", "size_mode": "fixed_amount", "size_value": 10000,
        "order_type": "market", "cooldown_days": 0, "enabled": True,
    }
    rule.update(overrides)
    return rule


def test_hrae_hard_stop_creates_one_sell_order(tmp_path, monkeypatch):
    _ready_account(tmp_path, monkeypatch)
    paper_auto.create_auto_rule(tmp_path, _rule())

    orders = paper_auto.on_quote_snapshot(tmp_path, [{
        "symbol": SYM, "raw_close": 8.8, "close": 8.8, "change_pct": -0.10,
        "ema20": 9.5, "ema60": 10.0, "momentum_5d": -0.12,
        "momentum_20d": -0.20, "atr_14": 0.1,
    }])

    assert len(orders) == 1
    assert orders[0]["side"] == "sell"
    assert orders[0]["qty"] == 500
    assert orders[0]["source"].startswith("auto_hrae:")


def test_hrae_rejects_non_sell_rule(tmp_path):
    try:
        paper_auto.validate_rule(_rule(side="buy"))
    except ValueError as exc:
        assert "只允许卖出" in str(exc)
    else:
        raise AssertionError("wind buy rule must be rejected")


def test_hrae_pbr_uses_equity_and_market_value_to_cap_concentration(tmp_path, monkeypatch):
    _ready_account(tmp_path, monkeypatch)
    # 将测试持仓扩大到约 90% 暴露, 验证组合预算只减仓、不加仓。
    paper._materialize(tmp_path)
    _, err = paper.create_order(tmp_path, SYM, "buy", qty=8500, ref_price=10.0)
    assert err is None
    paper.evaluate_intraday(tmp_path, {SYM: 10.0})
    paper._materialize(tmp_path)
    paper_auto.create_auto_rule(tmp_path, _rule())
    row = {
        "symbol": SYM, "raw_close": 10.0, "close": 10.0, "change_pct": 0.01,
        "ema20": 9.5, "ema60": 9.0, "momentum_5d": 0.02,
        "momentum_20d": 0.04, "atr_14": 0.2,
    }

    snapshot = paper_auto.get_hrae_risk_snapshot(tmp_path, [row])
    assert snapshot is not None
    assert snapshot["market_value"] > snapshot["equity"] * 0.80
    assert snapshot["target_exposure"] < snapshot["current_exposure"]
    assert snapshot["cumulative_pnl"] < 0  # 手续费已计入账户总资产

    orders = paper_auto.on_quote_snapshot(tmp_path, [row])
    assert len(orders) == 1
    assert orders[0]["side"] == "sell"
    assert orders[0]["qty"] > 0
