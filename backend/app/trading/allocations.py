"""策略仓位归因 — 把券商总持仓映射为各来源 (人工/策略/AI) 的逻辑份额。

同一证券可能同时包含人工持仓 + 多个策略持仓, 模拟盘把整标归属最近一笔自动
买单的错误不能延伸到实盘。本地用它做风控归因和策略绩效归因, 券商回报仍是事实源。

约束: 总分配份额 ≤ 券商真实持仓 (禁止凭空创造份额)。
本版只提供 CRUD; 真实归因逻辑 (FIFO/按策略权重) 在阶段 4 影子交易再加。
策略权重存 strategy_allocations 表 (与持仓快照分离, 不混用表)。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from app.trading import store

logger = logging.getLogger(__name__)


def set_allocation(
    data_dir: Path,
    strategy_id: str,
    account_id: str,
    target_weight: float = 0.0,
    target_qty: int = 0,
) -> None:
    """写入/更新某策略在某账户上的目标权重/数量份额."""
    from app.market_time import cn_now

    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO strategy_allocations("
            " strategy_id, account_id, target_weight, target_qty, as_of"
            ") VALUES(?, ?, ?, ?, ?)",
            (strategy_id, account_id, target_weight, target_qty, cn_now().isoformat()),
        )

    store._execute_write(data_dir, _do)


def list_allocations(data_dir: Path, account_id: str) -> list[dict]:
    store.init_db(data_dir)
    return [
        dict(r) for r in store._query(
            data_dir,
            "SELECT * FROM strategy_allocations WHERE account_id = ?",
            (account_id,),
        )
    ]