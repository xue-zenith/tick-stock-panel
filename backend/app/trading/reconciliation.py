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
from app.trading.contracts import OrderState
from app.trading.state_machine import can_transition, transition

logger = logging.getLogger(__name__)
MAX_ARM_RECON_AGE_SECONDS = 300


def run_reconciliation(
    data_dir: Path,
    account_id: str,
    broker_orders: list[dict],
    broker_positions: list[dict] | None,
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
    local_order_rows = store.list_orders(data_dir, account_id=account_id)
    local_orders = {
        o["broker_order_id"]: o for o in local_order_rows
        if o.get("broker_order_id")
    }
    broker_order_ids: set[str] = set()

    # 1. 本地有、券商有: 比对状态
    for bo in broker_orders:
        bid = bo.get("broker_order_id")
        if not bid:
            continue
        broker_order_ids.add(str(bid))
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
            _sync_order_from_broker(data_dir, local, bo, broker_state)

    # 2. 本地有、券商无: 未完成单不能被静默忽略。
    terminal = {"filled", "cancelled", "rejected", "expired"}
    for o in local_order_rows:
        if o.get("broker_order_id") is None and o["state"] == "unknown":
            diffs.append({
                "diff_id": uuid.uuid4().hex,
                "run_id": run_id,
                "order_id": o["order_id"],
                "diff_kind": "unknown_unresolved",
                "message": "本地 UNKNOWN 状态未对账确认",
            })
        elif (
            o.get("broker_order_id")
            and o["state"] not in terminal
            and str(o["broker_order_id"]) not in broker_order_ids
        ):
            diffs.append({
                "diff_id": uuid.uuid4().hex,
                "run_id": run_id,
                "order_id": o["order_id"],
                "diff_kind": "missing_broker_order",
                "local_value": o["state"],
                "broker_value": None,
            })

    # 3. 持仓对账 (简化版: 仅记录数量差异; 真实场景按券商字段映射)
    local_positions = {p["symbol"]: p for p in store.latest_positions(data_dir, account_id)}
    if broker_positions is not None:
        broker_position_symbols: set[str] = set()
        for pos in broker_positions:
            sym = pos.get("symbol")
            if not sym:
                continue
            broker_position_symbols.add(str(sym))
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
        for sym, lp in local_positions.items():
            if sym not in broker_position_symbols and int(lp.get("qty") or 0) != 0:
                diffs.append({
                    "diff_id": uuid.uuid4().hex,
                    "run_id": run_id,
                    "order_id": None,
                    "diff_kind": "position",
                    "local_value": lp["qty"],
                    "broker_value": 0,
                })

    # 4. 资金对账：首次尚无本地快照时只建立基线；之后按分币精度比较。
    local_account = store.latest_account_snapshot(data_dir, account_id)
    if broker_cash is not None and local_account is not None:
        local_cash = float(local_account.get("cash") or 0.0)
        if abs(local_cash - float(broker_cash)) > 0.01:
            diffs.append({
                "diff_id": uuid.uuid4().hex,
                "run_id": run_id,
                "order_id": None,
                "diff_kind": "cash",
                "local_value": f"{local_cash:.2f}",
                "broker_value": f"{float(broker_cash):.2f}",
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
    else:
        # 新的券商快照已证明一致，历史差异才能被解决；当前 clean run 不受影响。
        store.resolve_account_diffs(data_dir, account_id, before_run_id=run_id)

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


def _sync_order_from_broker(
    data_dir: Path,
    local: dict,
    broker_order: dict,
    broker_state: str,
) -> None:
    """用已查询到的券商事实推进本地状态，不掩盖本轮对账差异。

    这里只允许状态机明确定义的恢复路径。当前 run 仍保持 dirty，要求再次
    对账确认一致后才会解除差异，避免单次瞬时查询直接恢复 ARMED。
    """
    current = OrderState(local["state"])
    event = {
        (OrderState.UNKNOWN, "submitted"): "reconcile_submitted",
        (OrderState.UNKNOWN, "partially_filled"): "reconcile_partial",
        (OrderState.UNKNOWN, "filled"): "reconcile_filled",
        (OrderState.UNKNOWN, "cancelled"): "reconcile_cancelled",
        (OrderState.UNKNOWN, "rejected"): "reconcile_rejected",
        (OrderState.SUBMITTED, "partially_filled"): "partial_fill",
        (OrderState.SUBMITTED, "filled"): "full_fill",
        (OrderState.SUBMITTED, "cancelled"): "cancel",
        (OrderState.SUBMITTED, "rejected"): "broker_reject",
        (OrderState.PARTIALLY_FILLED, "filled"): "fill",
        (OrderState.PARTIALLY_FILLED, "cancelled"): "cancel",
        (OrderState.PARTIALLY_FILLED, "unknown"): "report_unknown",
    }.get((current, broker_state))
    if event is None or not can_transition(current, event):
        return

    new_state = transition(current, event)
    filled_qty = broker_order.get("filled_qty")
    avg_price = broker_order.get("avg_price")
    store.update_order_state(
        data_dir,
        local["order_id"],
        new_state.value,
        filled_qty=int(filled_qty) if filled_qty is not None else None,
        avg_fill_price=float(avg_price) if avg_price is not None else None,
        event_from_state=current.value,
        event_reason=f"reconcile_broker_{broker_state}",
    )


def can_arm(data_dir: Path, account_id: str) -> tuple[bool, str]:
    """是否允许进入 ARMED: 最近五分钟内 clean 且无未解决差异。"""
    store.init_db(data_dir)
    unresolved = store.unresolved_diff_count(data_dir, account_id)
    if unresolved > 0:
        return False, f"存在 {unresolved} 处未解决对账差异"
    runs = store.list_recon_runs(data_dir, account_id)
    if not runs or runs[0]["status"] != "clean":
        return False, "最近一次对账非 clean"
    try:
        from datetime import datetime

        from app.market_time import cn_now

        finished_at = datetime.fromisoformat(runs[0]["finished_at"])
        age = (cn_now() - finished_at).total_seconds()
    except (TypeError, ValueError):
        return False, "最近一次对账时间无效"
    if age < 0 or age > MAX_ARM_RECON_AGE_SECONDS:
        return False, "最近一次 clean 对账已过期，请重新对账"
    return True, "ok"


def _now_iso() -> str:
    from app.market_time import cn_now
    return cn_now().isoformat()
