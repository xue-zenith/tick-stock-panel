"""验证 _materialize 传入 settle_day 时, 盘后跨日不会错误解锁当日买入的 T+1 持仓。

复现场景: 盘后管道在凌晨 00:30 运行, 此时 cn_today() 已跨日到次日,
但撮合日期仍为昨日。如果 _materialize 用 cn_today() 计算 T+1 可卖数量,
会把昨日买入的股票标记为可卖 (available_qty > 0), 违反 T+1 规则。
传入 settle_day 后应正确保持 available_qty=0。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.strategy import paper
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
TRADE_DAY = date(2026, 9, 24)  # 周四
NEXT_DAY = date(2026, 9, 25)   # 周五


def _write_daily(tmp_path: Path, rows: list[tuple[date, float, float]]) -> None:
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


def test_settle_day_prevents_overnight_t1_unlock(tmp_path, monkeypatch):
    """盘后 settle_day 在跨日后运行: available_qty 应为 0 (T+1 不可卖)。"""
    # 交易日盘中买入
    monkeypatch.setattr(paper, "cn_today", lambda: TRADE_DAY)
    _write_daily(tmp_path, [(TRADE_DAY - timedelta(days=1), 10.0, 10.0)])
    paper.create_account(tmp_path, 1_000_000)
    _, err = paper.create_order(tmp_path, SYM, "buy", qty=100, ref_price=10.0)
    assert err is None
    assert len(paper.evaluate_intraday(tmp_path, {SYM: 10.0})) == 1

    # 盘后跨日: cn_today 已是次日, 但 settle_day 传入的是交易日
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT_DAY)
    _write_daily(tmp_path, [(TRADE_DAY, 10.0, 10.8)])

    # 挂一个 close 卖单用于 settle_day 撮合 (不会成交因为 T+1)
    paper.create_order(tmp_path, SYM, "sell", qty=100, order_type="close", ref_price=10.8)
    summary = paper.settle_day(tmp_path, TRADE_DAY.isoformat())

    # settle_day 撮合后, positions.json 的 available_qty 应为 0
    # (因为 settle_day=TRADE_DAY, 当日买入不可卖)
    positions = paper.load_positions(tmp_path)
    pos = positions[SYM]
    assert pos["available_qty"] == 0, (
        f"available_qty 应为 0 (T+1 当日买入不可卖), 实际为 {pos['available_qty']}"
    )


def test_materialize_without_settle_day_uses_cn_today(tmp_path, monkeypatch):
    """不传 settle_day 时 (旧行为), 跨日后 available_qty 错误变为 100。"""
    monkeypatch.setattr(paper, "cn_today", lambda: TRADE_DAY)
    _write_daily(tmp_path, [(TRADE_DAY - timedelta(days=1), 10.0, 10.0)])
    paper.create_account(tmp_path, 1_000_000)
    _, err = paper.create_order(tmp_path, SYM, "buy", qty=100, ref_price=10.0)
    assert err is None
    assert len(paper.evaluate_intraday(tmp_path, {SYM: 10.0})) == 1

    # 跨日后调用 _materialize 不传 settle_day (旧行为)
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT_DAY)
    paper._materialize(tmp_path)  # 不传 settle_day

    positions = paper.load_positions(tmp_path)
    pos = positions[SYM]
    # 旧行为: cn_today=NEXT_DAY, 当日买入(NEXT_DAY-1)的股票 T+1 可卖
    assert pos["available_qty"] == 100, (
        f"旧行为 available_qty 应为 100 (跨日误解锁), 实际为 {pos['available_qty']}"
    )


def test_materialize_with_settle_day_keeps_t1_locked(tmp_path, monkeypatch):
    """传入 settle_day=TRADE_DAY 时, available_qty 正确保持 0。"""
    monkeypatch.setattr(paper, "cn_today", lambda: TRADE_DAY)
    _write_daily(tmp_path, [(TRADE_DAY - timedelta(days=1), 10.0, 10.0)])
    paper.create_account(tmp_path, 1_000_000)
    _, err = paper.create_order(tmp_path, SYM, "buy", qty=100, ref_price=10.0)
    assert err is None
    assert len(paper.evaluate_intraday(tmp_path, {SYM: 10.0})) == 1

    # 跨日后调用 _materialize 传入 settle_day=TRADE_DAY (新行为)
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT_DAY)
    paper._materialize(tmp_path, settle_day=TRADE_DAY.isoformat())

    positions = paper.load_positions(tmp_path)
    pos = positions[SYM]
    assert pos["available_qty"] == 0, (
        f"新行为 available_qty 应为 0 (settle_day 正确保持 T+1), 实际为 {pos['available_qty']}"
    )
