"""AI 自动交易授权租约 — 控制策略/AI 可以获得哪些执行权限。

权限等级 (镜像 contracts.AutonomyGrant):
  L0 观察: 只读账户与解释现状
  L1 建议: 生成待审批 TradeIntent, 必须人工确认每一笔
  L2 监督执行: AI 生成订单, 用户逐笔确认 (本版不激活)
  L3 有界自治: 在额度/标的池/时间段/最大亏损内自动执行 (本版不激活)

本版限制:
  - 只允许创建 level 0 / 1 的授权;
  - level 2/3 的 OMS 拒绝并记审计;
  - 按日失效原则在 oms 提交时校验 (created_at 当日有效).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from app.trading import store

logger = logging.getLogger(__name__)

# 本版允许创建的最大授权级别
MAX_ALLOWED_LEVEL = 1


def create_grant(
    data_dir: Path,
    account_id: str,
    strategy_id: str,
    level: int,
    max_order_value: float = 0.0,
) -> dict:
    """创建授权租约. level > 1 本版拒绝."""
    if level > MAX_ALLOWED_LEVEL:
        raise ValueError(f"本版只允许创建 level ≤ {MAX_ALLOWED_LEVEL} 的授权")
    if level < 0:
        raise ValueError("level 必须 ≥ 0")
    if store.get_account(data_dir, account_id) is None:
        raise ValueError(f"账户不存在: {account_id!r}")

    from app.market_time import cn_now
    grant_id = _uuid()
    doc = {
        "grant_id": grant_id,
        "account_id": account_id,
        "strategy_id": strategy_id,
        "level": level,
        "max_order_value": max_order_value,
        "enabled": 1,
        "created_at": cn_now().isoformat(),
        "revoked_at": None,
    }

    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO autonomy_grants("
            " grant_id, account_id, strategy_id, level, max_order_value, enabled, created_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                grant_id, account_id, strategy_id, level, max_order_value, 1,
                cn_now().isoformat(),
            ),
        )

    store._execute_write(data_dir, _do)
    return doc


def list_grants(data_dir: Path, active_only: bool = False) -> list[dict]:
    store.init_db(data_dir)
    if active_only:
        return [
            dict(r) for r in store._query(
                data_dir,
                "SELECT * FROM autonomy_grants WHERE revoked_at IS NULL AND enabled = 1",
            )
        ]
    return [
        dict(r) for r in store._query(data_dir, "SELECT * FROM autonomy_grants")
    ]


def revoke_grant(data_dir: Path, grant_id: str) -> bool:
    """撤销授权 (enabled→0 + revoked_at). 不可恢复."""
    from app.market_time import cn_now

    def _do(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "UPDATE autonomy_grants SET enabled = 0, revoked_at = ? WHERE grant_id = ?"
            " AND revoked_at IS NULL",
            (cn_now().isoformat(), grant_id),
        )
        return cur.rowcount

    rowcount = store._execute_write(data_dir, _do)
    return rowcount > 0


def effective_level(data_dir: Path, account_id: str, strategy_id: str) -> int:
    """某策略在某账户上的有效授权级别 (当天有效)."""
    store.init_db(data_dir)
    from app.market_time import cn_today
    rows = store._query(
        data_dir,
        "SELECT level, created_at FROM autonomy_grants"
        " WHERE account_id = ? AND strategy_id = ?"
        " AND enabled = 1 AND revoked_at IS NULL"
        " ORDER BY created_at DESC",
        (account_id, strategy_id),
    )
    if not rows:
        return 0
    # 按日失效: 只认当天创建的授权
    created_at = rows[0]["created_at"][:10]
    if created_at != cn_today().isoformat():
        return 0
    return int(rows[0]["level"])


def _uuid() -> str:
    import uuid
    return uuid.uuid4().hex
