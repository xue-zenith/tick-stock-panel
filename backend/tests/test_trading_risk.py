"""风控引擎测试 — 每条规则的 PASS/REJECT 路径。

纯函数: 注入 RiskContext, 跑 evaluate(); 不接 OMS/DB.
fail-closed: 缺数据时拒单。
"""
from __future__ import annotations

from app.trading import risk
from app.trading.contracts import (
    Side,
    SourceType,
    TradeIntent,
    TradingMode,
)


def _intent(*, side="buy", qty=100, limit_price=10.0, source="manual",
             price_policy="limit", strategy_id=None) -> TradeIntent:
    return TradeIntent(
        intent_id="ti_test", account_id="acc1", strategy_id=strategy_id,
        symbol="600519.SH", side=Side(side), qty=qty,
        price_policy=price_policy, limit_price=limit_price,
        tif="day", source=SourceType(source),
        dedupe_key="dk_test", proposed_at="x",
        valid_until=None, ruleset_version=risk.RULESET_VERSION,
        rationale="t",
    )


def _ctx(**overrides) -> risk.RiskContext:
    base = dict(
        account_id="acc1", mode=TradingMode.ARMED_MANUAL,
        cash=1_000_000, total=1_000_000,
        last_price=10.0, prev_close=10.0,
        limit_up_price=11.0, limit_down_price=9.0,
        position_qty=0, available_qty=0,
    )
    base.update(overrides)
    return risk.RiskContext(**base)


# ── 系统级 ─────────────────────────────────────────────
def test_mode_disarmed_rejects():
    v, reasons = risk.evaluate(_intent(), _ctx(mode=TradingMode.DISARMED))
    assert v == "reject"
    assert any("模式" in r for r in reasons)


def test_mode_halted_rejects():
    v, _ = risk.evaluate(_intent(), _ctx(mode=TradingMode.HALTED))
    assert v == "reject"


def test_mode_degraded_allows_sell_only():
    """直接测 _check_mode_armed: DEGRADED 允许 SELL, 禁止 BUY."""
    # 买被拒
    r_buy = risk._check_mode_armed(
        _intent(side="buy", price_policy="next_open"),
        _ctx(mode=TradingMode.DEGRADED),
    )
    assert r_buy.verdict.value == "reject"
    # 卖可过
    r_sell = risk._check_mode_armed(
        _intent(side="sell", price_policy="next_open"),
        _ctx(mode=TradingMode.DEGRADED),
    )
    assert r_sell.verdict.value == "pass"


def test_autonomy_l1_requires_grant():
    """AI_L1 无授权 → 拒."""
    v, reasons = risk.evaluate(
        _intent(source="ai_l1", strategy_id="s1"),
        _ctx(autonomy_level=0),
    )
    assert v == "reject"
    assert any("AI_L1" in r for r in reasons)


def test_autonomy_l1_with_grant_passes_rules():
    """AI_L1 有授权 → autonomy_scope pass (其他规则仍跑)."""
    _, reasons = risk.evaluate(
        _intent(source="ai_l1", strategy_id="s1"),
        _ctx(autonomy_level=1),
    )
    # autonomy 不再拒 (其他规则可能拒, 但不应含 autonomy_scope)
    assert not any("AI_L1" in r for r in reasons)


def test_auto_l2_rejected():
    v, reasons = risk.evaluate(_intent(source="auto_l2"), _ctx())
    assert v == "reject"
    assert any("auto_l2" in r for r in reasons)


# ── 标的级 ─────────────────────────────────────────────
def test_lot_size_passes_on_multiple_of_100():
    """next_open 单绕开交易时段, 100 股 → lot_size pass."""
    _, reasons = risk.evaluate(
        _intent(qty=100, price_policy="next_open"), _ctx()
    )
    assert not any("100" in r for r in reasons)


def test_lot_size_rejects_non_100():
    """next_open 单绕开交易时段, 150 股 → lot_size 拒."""
    v, reasons = risk.evaluate(
        _intent(qty=150, price_policy="next_open"), _ctx()
    )
    assert v == "reject"
    assert any("100" in r for r in reasons)


def test_cash_sufficiency_rejects_low_cash():
    v, reasons = risk.evaluate(
        _intent(qty=100, limit_price=1500.0, price_policy="next_open"),
        _ctx(cash=1000.0),
    )
    assert v == "reject"
    assert any("可用" in r for r in reasons)


def test_t1_available_rejects_oversell():
    """卖出数量 > 券商可卖 → 拒."""
    v, reasons = risk.evaluate(
        _intent(side="sell", qty=200, price_policy="next_open"),
        _ctx(available_qty=100),
    )
    assert v == "reject"
    assert any("可卖" in r for r in reasons)


def test_limit_bounds_rejects_above_limit_up():
    """直接测 _check_limit_bounds (绕开 trading_session: 该规则对 limit/market 才生效)."""
    # 买价超过涨停价
    r = risk._check_limit_bounds(
        _intent(side="buy", limit_price=11.5, price_policy="limit"),
        _ctx(limit_up_price=11.0),
    )
    assert r.verdict.value == "reject"
    assert "涨停" in r.reason
    # 卖价低于跌停价
    r = risk._check_limit_bounds(
        _intent(side="sell", limit_price=8.5, price_policy="limit"),
        _ctx(limit_down_price=9.0),
    )
    assert r.verdict.value == "reject"
    assert "跌停" in r.reason
    # 价格在区间内 → pass
    r = risk._check_limit_bounds(
        _intent(side="buy", limit_price=10.5, price_policy="limit"),
        _ctx(limit_up_price=11.0),
    )
    assert r.verdict.value == "pass"