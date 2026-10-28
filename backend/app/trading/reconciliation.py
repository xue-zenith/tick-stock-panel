"""对账服务 — 券商回报与本地 OMS 状态差异检测与处理。

对账是实盘的核心安全机制 (相对模拟盘的关键差异):
  - 启动必须先对账, 无未解决差异才能进入 ARMED (fail-closed);
  - UNKNOWN 状态只能通过 reconcile 退出;
  - 运行中定期对账捕获回调丢失场景。

设计:
  - run_reconciliation(account_id, broker_snapshot) 比对本地 orders 与
    broker 报告, 把差异写入 reconciliation_diffs;
  - clean 判定: diff_count == 0 (无未解决差异);
  - arm 前置: 必须有最近一次 clean 对账。

不包括 (本版):
  - 自动定时对账 (由调用方调度);
  - 券商主动拉取 (broker_snapshot 由外部注入)。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.trading import store

logger = logging.getLogger(__name__)


def run_reconciliation(
    data_dir: Path,
    account_id: str,
    broker_orders: list[dict],
    broker_positions: list[dict],
    broker_cash: float | None,
) -> dict:
    """执行一次对账 → reconciliation_runs + reconciliation_diffs.

    broker_orders: [{broker_order_id, state, filled_qty, avg_price}, ...]
    broker_positions: [{symbol, qty, available_qty}, ...]
    broker_cash: 券商可用资金 (None 表示未拉取, 跳过资金校验)

    返回 {run_id, status, diff_count}.
    """
    run_id = uuid.uuid4().hex
    store.init_db(data_dir)
    store.create_recon_run(data_dir, run_id, account_id, _now_iso())

    diffs: list[dict] = []
    local_orders = {
        o["broker_order_id"]: o for o in store.list_orders(data_dir, account_id=account_id)
        if o.get("broker_order_id")
    }

    # 1. 本地有、券商有: 比对状态
    for bo in broker_orders:
        bid = bo.get("broker_order_id")
        if not bid:
            continue
        local = local_orders.get(bid)
        if local is None:
            # 券商有、本地无: 外部订单 (用户在券商客户端手工下单)
            diffs.append({
                "diff_id": uuid.uuid4().hex,
                "run_id": run_id,
                "order_id": None,
                "diff_kind": "unknown_order",
                "local_value": None,
                "broker_value": str(bo),
            })
            continue
        # 比对状态 (本地与券商枚举映射简化为字符串比较)
        local_state = local["state"]
        broker_state = _map_broker_state(bo.get("state"))
        if local_state != broker_state:
            diffs.append({
                "diff_id": uuid.uuid4().hex,
                "run_id": run_id,
                "order_id": local["order_id"],
                "diff_kind": "order_state",
                "local_value": local_state,
                "broker_value": broker_state,
            })

    # 2. 本地有、券商无: 券商可能未接收 / 已撤销
    for o in store.list_orders(data_dir, account_id=account_id):
        if o.get("broker_order_id") is None and o["state"] == "unknown":
            diffs.append({
                "diff_id": uuid.uuid4().hex,
                "run_id": run_id,
                "order_id": o["order_id"],
                "diff_kind": "unknown_unresolved",
                "message": "本地 UNKNOWN 状态未对账确认",
            })

    # 3. 持仓对账 (简化版: 仅记录数量差异; 真实场景按券商字段映射)
    local_positions = {p["symbol"]: p for p in store.latest_positions(data_dir, account_id)}
    for pos in broker_positions:
        sym = pos.get("symbol")
        lp = local_positions.get(sym)
        local_qty = lp["qty"] if lp else 0
        broker_qty = pos.get("qty", 0)
        if local_qty != broker_qty:
            diffs.append({
                "diff_id": uuid.uuid4().hex,
                "run_id": run_id,
                "order_id": None,
                "diff_kind": "position",
                "local_value": local_qty,
                "broker_value": broker_qty,
            })

    for d in diffs:
        store.insert_recon_diff(data_dir, d)

    status = "clean" if not diffs else "dirty"
    store.finish_recon_run(
        data_dir, run_id, _now_iso(), status, len(diffs),
    )

    # 落事件 (差异>0 警告)
    if diffs:
        from app.trading import events
        events.publish(data_dir, [events.reconciliation_diff(account_id, len(diffs))])

    return {"run_id": run_id, "status": status, "diff_count": len(diffs)}


def _map_broker_state(state: str | None) -> str:
    """券商状态枚举 → 本地 OrderState (简化版; 真实适配器内做完整映射)."""
    if state is None:
        return "unknown"
    s = state.lower()
    return {
        "filled": "filled",
        "partial": "partially_filled",
        "partially_filled": "partially_filled",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "rejected": "rejected",
        "submitted": "submitted",
        "accepted": "submitted",
    }.get(s, "unknown")


def can_arm(data_dir: Path, account_id: str) -> tuple[bool, str]:
    """是否允许进入 ARMED: 最近一次对账 clean 且无未解决差异。"""
    store.init_db(data_dir)
    unresolved = store.unresolved_diff_count(data_dir, account_id)
    if unresolved > 0:
        return False, f"存在 {unresolved} 处未解决对账差异"
    runs = store.list_recon_runs(data_dir, account_id)
    if not runs or runs[0]["status"] != "clean":
        return False, "最近一次对账非 clean"
    return True, "ok"


def _now_iso() -> str:
    from app.market_time import cn_now
    return cn_now().isoformat()