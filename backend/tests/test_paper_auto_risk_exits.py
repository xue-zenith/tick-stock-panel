"""模拟盘风控平仓 (apply_risk_exits) 测试 — 止损 / 最大持有天数。

覆盖:
- 止损触发: close 跌破 avg_cost*(1-abs(stop_loss)) → 生成 close 卖单 + 成交
- 止损未触发: 跌幅不足 → 不平
- 到期触发: 持有交易日数 >= max_hold_days → 平仓
- 到期未触发: 持有日数不足 → 不平
- rule override 优先于策略值
- 策略值兜底 (rule 无 override, match_kind=strategy)
- T+1: 当日买入当日不可被止损卖 (available=0)
- 幂等: settle_day 重跑不重复平仓
- manual 仓位不平 (source="manual")
- 无 stop_loss/max_hold_days (都 None) → 不平
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_auto

SYM = "600519.SH"
DAY = date(2026, 9, 24)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture(autouse=True)
def _market_session(monkeypatch):
    """钉在连续竞价时段, 当日买入次日才可卖 (T+1)。"""
    now = datetime.combine(DAY, time(10, 0), tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: now)
    monkeypatch.setattr(paper, "cn_today", lambda: now.date())
    monkeypatch.setattr(paper_auto, "cn_now", lambda: now)


def _write_daily(tmp_path: Path, rows: list[tuple[date, float, float]]) -> None:
    """写 kline_daily 分区: [(day, open, close)] (不复权 raw 价)。"""
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    df = pl.DataFrame(
        {
            "symbol": [SYM] * len(rows),
            "date": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [max(r[1], r[2]) for r in rows],
            "low": [min(r[1], r[2]) for r in rows],
            "close": [r[2] for r in rows],
            "volume": [10000.0] * len(rows),
            "amount": [r[2] * 10000.0 for r in rows],
        }
    )
    repo.append_daily(df)


def _rule(**over) -> dict:
    base = {
        "name": "风控跟单", "match_kind": "strategy", "match_id": "strat_1",
        "side": "auto", "size_mode": "fixed_amount", "size_value": 10000,
        "order_type": "market", "cooldown_days": 0,
    }
    base.update(over)
    return base


def _ev(symbol: str = SYM, price: float = 10.0) -> dict:
    return {"source": "strategy", "strategy_id": "strat_1", "symbol": symbol, "price": price, "type": "buy_signal"}


def _buy_via_auto(data_dir: Path, price: float = 10.0) -> None:
    """用 auto 规则买入建仓 (source=auto:xxx), 盘中成交。"""
    paper_auto.on_rule_events(data_dir, [_ev(SYM, price)])
    paper.evaluate_intraday(data_dir, {SYM: price})


def _advance_to(monkeypatch, day: date) -> None:
    """推进 cn_now/cn_today 到指定交易日 (模拟盘 T+1 判断用 cn_today)。"""
    now = datetime.combine(day, time(10, 0), tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: now)
    monkeypatch.setattr(paper, "cn_today", lambda: now.date())
    monkeypatch.setattr(paper_auto, "cn_now", lambda: now)


def _fake_strategy_engine(stop_loss: float | None = None, max_hold_days: int | None = None) -> MagicMock:
    """构造假策略引擎: get() 返回带 stop_loss/max_hold_days 的对象。"""
    sdef = SimpleNamespace(stop_loss=stop_loss, max_hold_days=max_hold_days)
    eng = MagicMock()
    eng.get.return_value = sdef
    return eng


# ── 止损 ──────────────────────────────────────────────


def test_stop_loss_triggers_when_below_threshold(data_dir: Path, monkeypatch):
    """avg_cost=10, stop_loss=-0.07, close=9.2 (pnl=-8%) → 平仓。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(stop_loss_pct=-0.07))
    _buy_via_auto(data_dir, 10.0)
    assert paper.load_positions(data_dir)[SYM]["qty"] == 1000  # 1万/10元

    # 次日: close 跌到 9.2 (pnl=-8% ≤ -7%)
    d1 = DAY + timedelta(days=1)
    _write_daily(data_dir, [(d1, 9.5, 9.2)])
    _advance_to(monkeypatch, d1)
    eng = _fake_strategy_engine()
    created = paper_auto.apply_risk_exits(data_dir, d1.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created) == 1
    assert created[0]["side"] == "sell"
    assert created[0]["order_type"] == "close"


def test_stop_loss_not_triggered_when_above_threshold(data_dir: Path, monkeypatch):
    """avg_cost=10, stop_loss=-0.07, close=9.5 (pnl=-5% > -7%) → 不平。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(stop_loss_pct=-0.07))
    _buy_via_auto(data_dir, 10.0)

    d1 = DAY + timedelta(days=1)
    _write_daily(data_dir, [(d1, 9.6, 9.5)])
    _advance_to(monkeypatch, d1)
    eng = _fake_strategy_engine()
    created = paper_auto.apply_risk_exits(data_dir, d1.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created) == 0


def test_stop_loss_strategy_fallback(data_dir: Path, monkeypatch):
    """rule 无 stop_loss_pct, match_kind=strategy → 从策略读 stop_loss。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule())  # 无 stop_loss_pct
    _buy_via_auto(data_dir, 10.0)

    d1 = DAY + timedelta(days=1)
    _write_daily(data_dir, [(d1, 9.5, 9.2)])
    _advance_to(monkeypatch, d1)
    # 策略 stop_loss=-0.07
    eng = _fake_strategy_engine(stop_loss=-0.07)
    created = paper_auto.apply_risk_exits(data_dir, d1.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created) == 1


def test_stop_loss_rule_override_prioritized(data_dir: Path, monkeypatch):
    """rule stop_loss_pct=-0.03 优先于策略 stop_loss=-0.07。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(stop_loss_pct=-0.03))
    _buy_via_auto(data_dir, 10.0)

    d1 = DAY + timedelta(days=1)
    # close=9.5 (pnl=-5%): 在 -3% 以下 (触发 rule override), 在 -7% 以上 (不触发策略值)
    _write_daily(data_dir, [(d1, 9.6, 9.5)])
    _advance_to(monkeypatch, d1)
    eng = _fake_strategy_engine(stop_loss=-0.07)
    created = paper_auto.apply_risk_exits(data_dir, d1.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created) == 1  # rule override -3% 触发


# ── 到期 ──────────────────────────────────────────────


def test_max_hold_triggers_when_expired(data_dir: Path, monkeypatch):
    """max_hold_days=2, 持有 2 个交易日 → 平仓。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(max_hold_days=2))
    _buy_via_auto(data_dir, 10.0)

    # DAY 买入, DAY+1、DAY+2 两个交易日 → 持有 2 日, 到期
    d1 = DAY + timedelta(days=1)
    d2 = DAY + timedelta(days=2)
    _write_daily(data_dir, [(d1, 10.0, 10.0), (d2, 10.0, 10.0)])
    _advance_to(monkeypatch, d2)
    eng = _fake_strategy_engine()
    created = paper_auto.apply_risk_exits(data_dir, d2.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created) == 1


def test_max_hold_not_triggered_when_short(data_dir: Path, monkeypatch):
    """max_hold_days=5, 持有 2 个交易日 < 5 → 不平。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(max_hold_days=5))
    _buy_via_auto(data_dir, 10.0)

    d1 = DAY + timedelta(days=1)
    d2 = DAY + timedelta(days=2)
    _write_daily(data_dir, [(d1, 10.0, 10.0), (d2, 10.0, 10.0)])
    _advance_to(monkeypatch, d2)
    eng = _fake_strategy_engine()
    created = paper_auto.apply_risk_exits(data_dir, d2.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created) == 0


# ── T+1 / 幂等 / manual / 无风控 ────────────────────


def test_t1_blocks_same_day_stop_loss(data_dir: Path):
    """当日买入当日不可被止损卖 (available=0)。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(stop_loss_pct=-0.07))
    _buy_via_auto(data_dir, 10.0)  # DAY 建仓

    # 同日 close 大跌, 但 T+1 当日不可卖
    _write_daily(data_dir, [(DAY, 9.5, 9.2)])
    eng = _fake_strategy_engine()
    created = paper_auto.apply_risk_exits(data_dir, DAY.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created) == 0  # available=0, 跳过


def test_idempotent_settle_day_rerun(data_dir: Path, monkeypatch):
    """settle_day 重跑不重复平仓: 首次平仓后持仓 qty=0, 再次不触发。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule(stop_loss_pct=-0.07))
    _buy_via_auto(data_dir, 10.0)

    d1 = DAY + timedelta(days=1)
    _write_daily(data_dir, [(d1, 9.5, 9.2)])
    _advance_to(monkeypatch, d1)
    eng = _fake_strategy_engine()
    # 首次: 生成 close 卖单
    created1 = paper_auto.apply_risk_exits(data_dir, d1.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created1) == 1
    # 撮合成交 (settle_day 会撮合 close 单)
    paper.settle_day(data_dir, d1.isoformat(), strategy_engine=eng)
    assert paper.load_positions(data_dir)[SYM]["qty"] == 0
    # 重跑: qty=0, 不再触发
    created2 = paper_auto.apply_risk_exits(data_dir, d1.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created2) == 0


def test_manual_position_not_closed(data_dir: Path, monkeypatch):
    """manual 仓位 (source=manual) 不归 auto 规则管, 不平。"""
    paper.create_account(data_dir, 1_000_000)
    # 手动建仓 (不走 auto 规则)
    paper.create_order(
        data_dir, SYM, "buy",
        account_id=paper.DEFAULT_ACCOUNT_ID, qty=100, order_type="market",
        ref_price=10.0, source="manual",
    )
    paper.evaluate_intraday(data_dir, {SYM: 10.0})
    assert paper.load_positions(data_dir)[SYM]["qty"] == 100

    paper_auto.create_auto_rule(data_dir, _rule(stop_loss_pct=-0.07))
    d1 = DAY + timedelta(days=1)
    _write_daily(data_dir, [(d1, 9.5, 9.2)])
    _advance_to(monkeypatch, d1)
    eng = _fake_strategy_engine()
    created = paper_auto.apply_risk_exits(data_dir, d1.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created) == 0  # manual 仓位跳过


def test_no_risk_params_no_close(data_dir: Path, monkeypatch):
    """rule 和策略都无 stop_loss/max_hold_days → 不平。"""
    paper.create_account(data_dir, 1_000_000)
    paper_auto.create_auto_rule(data_dir, _rule())  # 无风控字段
    _buy_via_auto(data_dir, 10.0)

    d1 = DAY + timedelta(days=1)
    _write_daily(data_dir, [(d1, 9.5, 9.2)])
    _advance_to(monkeypatch, d1)
    eng = _fake_strategy_engine()  # 策略也无 stop_loss/max_hold_days
    created = paper_auto.apply_risk_exits(data_dir, d1.isoformat(), paper.DEFAULT_ACCOUNT_ID, eng)
    assert len(created) == 0
