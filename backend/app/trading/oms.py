"""订单管理系统 OMS — 提交 / 审批 / 撤单 / 回报 / 对账编排。

OMS 是实盘的核心 (相对模拟盘的关键差异):
  - 幂等: dedupe_key UNIQUE 保证同一意图最多产生一个实盘订单;
  - 状态机: 每次状态转换经 state_machine.transition, 非法转换 fail-closed;
  - 审计: 每次状态变更与 order_events 一行落库 (同事务);
  - UNKNOWN 处理: 提交超时 → UNKNOWN → 只能通过 reconcile 解决.

不变量 (落库时保证):
  1. 一个 dedupe_key 在有效期内最多产生一个有效实盘订单;
  2. 每个券商成交编号最多入账一次 (executions.dedupe_key UNIQUE);
  3. 只有券商回报/查询才能把订单推到 FILLED;
  4. 网络超时不映射成 REJECTED, 而是进入 UNKNOWN;
  5. 本地数据库失败时不继续向券商发送.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.trading import autonomy, events, reconciliation, risk, store
from app.trading.contracts import (
    ApprovalDecision,
    OrderState,
    RiskVerdict,
    Side,
    SourceType,
    TradeIntent,
    TradingMode,
)
from app.trading.state_machine import transition

logger = logging.getLogger(__name__)


class TradingError(Exception):
    """OMS 通用错误."""


def _uuid() -> str:
    return uuid.uuid4().hex


def _default_expiry() -> str:
    """审批默认 24 小时有效."""
    from datetime import timedelta

    from app.market_time import cn_now
    return (cn_now() + timedelta(hours=24)).isoformat()


def submit_intent(
    data_dir: Path,
    *,
    account_id: str,
    symbol: str,
    side: str,
    qty: int,
    price_policy: str,
    limit_price: float | None,
    tif: str = "day",
    source: str = "manual",
    strategy_id: str | None = None,
    rationale: str = "",
    dedupe_key: str | None = None,
    valid_until: str | None = None,
    risk_ctx: risk.RiskContext | None = None,
) -> dict:
    """提交交易意图 → 风控 → (待审批 / 拒绝).

    幂等: 同一 dedupe_key 重复提交返回原有 intent, 不重新风控.
    risk_ctx 必须由调用方构造 (注入快照); None 时 fail-closed 拒单。
    """
    store.init_db(data_dir)
    accounts = store.list_accounts(data_dir)
    if not any(a["account_id"] == account_id for a in accounts):
        raise TradingError(f"账户 {account_id!r} 不存在")

    # AI 来源需匹配授权级别 (本版只校验 L1)
    if source == SourceType.AI_L1.value:
        autonomy.effective_level(data_dir, account_id, strategy_id or "")  # 校验授权
    if source in (SourceType.AUTO_L2.value, SourceType.AUTO_L3.value):
        raise TradingError(f"{source} 本版未启用")

    # 幂等键: 调用方未给则按内容派生
    dk = dedupe_key or _derive_dedupe(
        account_id, symbol, side, qty, price_policy, limit_price, source, strategy_id,
    )

    existing = store.get_intent_by_dedupe(data_dir, dk)
    if existing is not None:
        return {
            "intent_id": existing["intent_id"],
            "status": existing["status"],
            "dedupe_key": dk,
            "duplicate": True,
        }

    intent = TradeIntent(
        intent_id=f"ti_{_uuid()}",
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=Side(side),
        qty=qty,
        price_policy=price_policy,
        limit_price=limit_price,
        tif=tif,
        source=SourceType(source),
        dedupe_key=dk,
        proposed_at=events.now_iso(),
        valid_until=valid_until,
        ruleset_version=risk.RULESET_VERSION,
        rationale=rationale,
    )

    if risk_ctx is None:
        verdict = RiskVerdict.REJECT
        reasons = ("无风控上下文, fail-closed 拒绝",)
    else:
        v_str, reasons = risk.evaluate(intent, risk_ctx)
        verdict = RiskVerdict(v_str)

    # 落 intent + risk_decision (store 内部已有事务)
    store.insert_intent(data_dir, {
        "intent_id": intent.intent_id,
        "account_id": intent.account_id,
        "strategy_id": intent.strategy_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "qty": intent.qty,
        "price_policy": intent.price_policy,
        "limit_price": intent.limit_price,
        "tif": intent.tif,
        "source": intent.source.value,
        "dedupe_key": intent.dedupe_key,
        "proposed_at": intent.proposed_at,
        "valid_until": intent.valid_until,
        "ruleset_version": intent.ruleset_version,
        "rationale": intent.rationale,
        "status": "pending" if verdict == RiskVerdict.PASS else "rejected",
    })
    store.insert_risk_decision(data_dir, {
        "decision_id": f"rd_{_uuid()}",
        "intent_id": intent.intent_id,
        "verdict": verdict.value,
        "ruleset_version": intent.ruleset_version,
        "reasons": list(reasons),
        "checked_at": events.now_iso(),
    })

    publish: list[dict] = []
    if verdict == RiskVerdict.PASS:
        store.insert_approval(data_dir, {
            "approval_id": f"ap_{_uuid()}",
            "intent_id": intent.intent_id,
            "expires_at": valid_until or _default_expiry(),
        })
        publish.append(events.approval_pending(account_id, symbol, intent.intent_id, side))
    else:
        publish.append(events.risk_reject(account_id, symbol, intent.intent_id, reasons))
    events.publish(data_dir, publish)

    return {
        "intent_id": intent.intent_id,
        "status": "pending" if verdict == RiskVerdict.PASS else "rejected",
        "dedupe_key": dk,
        "verdict": verdict.value,
        "reasons": list(reasons),
    }


def _derive_dedupe(
    account_id: str, symbol: str, side: str, qty: int,
    price_policy: str, limit_price: float | None, source: str, strategy_id: str | None,
) -> str:
    """按内容派生 dedupe_key: 同账户同意图同方向同数量同价格视为同单."""
    import hashlib
    parts = [account_id, symbol, side, str(qty), price_policy, str(limit_price), source, strategy_id or ""]
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"dk_{h}"


def approve(
    data_dir: Path,
    intent_id: str,
    approver: str | None = None,
) -> dict:
    """人工审批通过 → 创建 BrokerOrder (APPROVED), 等待 submit_to_broker."""
    store.init_db(data_dir)
    intent = store.get_intent(data_dir, intent_id)
    if intent is None:
        raise TradingError(f"意图不存在: {intent_id!r}")
    if intent["status"] != "pending":
        raise TradingError(f"意图状态 {intent['status']!r}, 不能审批")

    rows = store._query(
        data_dir,
        "SELECT * FROM approvals WHERE intent_id = ? AND decision = 'pending'",
        (intent_id,),
    )
    if not rows:
        raise TradingError(f"意图 {intent_id!r} 无待审批记录")
    approval_id = rows[0]["approval_id"]

    order_id = f"ord_{_uuid()}"
    store.update_intent_status(data_dir, intent_id, "approved")
    store.decide_approval(data_dir, approval_id, ApprovalDecision.APPROVED.value, approver)

    store.insert_order(data_dir, {
        "order_id": order_id,
        "broker_order_id": None,
        "account_id": intent["account_id"],
        "intent_id": intent_id,
        "symbol": intent["symbol"],
        "side": intent["side"],
        "qty": intent["qty"],
        "price_policy": intent["price_policy"],
        "limit_price": intent["limit_price"],
        "state": OrderState.APPROVED.value,
        "submitted_at": None,
        "last_update_at": events.now_iso(),
        "reject_reason": None,
    })
    store.log_system_event(data_dir, "approved", {"order_id": order_id, "intent_id": intent_id})

    events.publish(
        data_dir,
        [events.approval_result(intent["account_id"], intent["symbol"], intent_id, True)],
    )
    return {"order_id": order_id, "approval_id": approval_id, "state": OrderState.APPROVED.value}


def reject(data_dir: Path, intent_id: str, approver: str | None = None) -> dict:
    """人工审批拒绝 → intent=rejected."""
    store.init_db(data_dir)
    intent = store.get_intent(data_dir, intent_id)
    if intent is None:
        raise TradingError(f"意图不存在: {intent_id!r}")
    if intent["status"] != "pending":
        raise TradingError(f"意图状态 {intent['status']!r}, 不能拒绝")

    rows = store._query(
        data_dir,
        "SELECT * FROM approvals WHERE intent_id = ? AND decision = 'pending'",
        (intent_id,),
    )
    if not rows:
        raise TradingError(f"意图 {intent_id!r} 无待审批记录")
    store.decide_approval(data_dir, rows[0]["approval_id"], ApprovalDecision.REJECTED.value, approver)
    store.update_intent_status(data_dir, intent_id, "rejected")

    events.publish(
        data_dir,
        [events.approval_result(intent["account_id"], intent["symbol"], intent_id, False)],
    )
    return {"intent_id": intent_id, "status": "rejected"}


def submit_to_broker(data_dir: Path, order_id: str, broker) -> dict:
    """把 APPROVED 单提交券商 → SUBMITTED (成功) / UNKNOWN (超时) / REJECTED.

    broker 是 BrokerAdapter Protocol 的实例:
      - submit(order_dict) → {'broker_order_id': str} | {'error': str} | None (超时)
    超时不映射成 REJECTED, 而是进入 UNKNOWN (需对账).
    """
    store.init_db(data_dir)
    order = store.get_order(data_dir, order_id)
    if order is None:
        raise TradingError(f"订单不存在: {order_id!r}")
    if order["state"] != OrderState.APPROVED.value:
        raise TradingError(f"订单状态 {order['state']!r}, 不能提交券商")

    # 调 broker (可能抛 / 返回 None)
    try:
        result = broker.submit({
            "order_id": order_id,
            "account_id": order["account_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "qty": order["qty"],
            "price_policy": order["price_policy"],
            "limit_price": order["limit_price"],
        })
    except Exception:  # noqa: BLE001 — broker 适配器异常不阻塞 OMS, 进 UNKNOWN
        logger.warning("提交券商异常, 进入 UNKNOWN", exc_info=True)
        store.update_order_state(
            data_dir, order_id, OrderState.UNKNOWN.value,
            event_from_state=order["state"], event_reason="submit_exception",
        )
        return {"order_id": order_id, "state": OrderState.UNKNOWN.value}

    if result is None:
        # 超时: 不重发, 进入 UNKNOWN
        store.update_order_state(
            data_dir, order_id, OrderState.UNKNOWN.value,
            event_from_state=order["state"], event_reason="submit_timeout",
        )
        return {"order_id": order_id, "state": OrderState.UNKNOWN.value}

    if "error" in result:
        store.update_order_state(
            data_dir, order_id, OrderState.REJECTED.value,
            broker_order_id=result.get("broker_order_id"),
            reject_reason=result["error"],
            event_from_state=order["state"], event_reason="broker_reject",
        )
        return {"order_id": order_id, "state": OrderState.REJECTED.value, "reason": result["error"]}

    broker_order_id = result.get("broker_order_id")
    store.update_order_state(
        data_dir, order_id, OrderState.SUBMITTED.value,
        broker_order_id=broker_order_id,
        submitted_at=events.now_iso(),
        event_from_state=order["state"], event_reason="submit_to_broker",
    )
    return {"order_id": order_id, "broker_order_id": broker_order_id, "state": OrderState.SUBMITTED.value}


def cancel(data_dir: Path, order_id: str, broker=None) -> dict:
    """撤单. 依当前状态走 transition:
    - CREATED/RISK_PASSED/APPROVED: 本地 cancel 事件
    - SUBMITTED/PARTIALLY_FILLED: 调 broker.cancel (best-effort), 再走 cancel 转换
    """
    store.init_db(data_dir)
    order = store.get_order(data_dir, order_id)
    if order is None:
        raise TradingError(f"订单不存在: {order_id!r}")
    cur = OrderState(order["state"])

    # 已是终态: 幂等返回
    if cur in (OrderState.CANCELLED, OrderState.FILLED, OrderState.REJECTED, OrderState.EXPIRED):
        return {"order_id": order_id, "state": cur.value, "already_terminal": True}

    # SUBMITTED/PARTIALLY_FILLED 需 broker 接口撤单 (best-effort: 失败也允许本地标 CANCELLED)
    if cur in (OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED) and broker is not None:
        try:
            broker.cancel(order.get("broker_order_id"))
        except Exception:  # noqa: BLE001 — broker.cancel 失败不阻塞本地撤单标记
            logger.warning("broker.cancel 失败 (继续标 CANCELLED)", exc_info=True)

    new_state = transition(cur, "cancel")
    store.update_order_state(
        data_dir, order_id, new_state.value,
        event_from_state=cur.value, event_reason="user_cancel",
    )
    return {"order_id": order_id, "state": new_state.value}


def on_broker_callback(data_dir: Path, exec_row: dict) -> dict:
    """券商成交回报处理. 幂等 (dedupe_key 冲突时忽略).

    exec_row 字段: {broker_trade_id, order_id, broker_order_id, account_id,
                      symbol, side, qty, price, fee, ts, dedupe_key}
    返回 {inserted: bool, state: str}.
    """
    store.init_db(data_dir)
    inserted = store.insert_execution(data_dir, exec_row)
    if not inserted:
        return {"inserted": False, "state": "duplicate_callback"}

    order = store.get_order(data_dir, exec_row["order_id"])
    if order is None:
        # 成交回来了但订单不存在 — 落 reconciliation_diff 让对账处理
        store.log_system_event(data_dir, "orphan_execution", {
            "exec_row": exec_row,
        })
        return {"inserted": True, "state": "orphan"}

    cur_state = OrderState(order["state"])
    cur_filled = int(order.get("filled_qty") or 0)
    new_filled = cur_filled + int(exec_row["qty"])
    total_qty = int(order["qty"])

    if new_filled >= total_qty:
        new_state = transition(cur_state, "full_fill")
        store.update_order_state(
            data_dir, order["order_id"], new_state.value,
            filled_qty=new_filled,
            avg_fill_price=_avg_price(order, exec_row),
            event_from_state=cur_state.value, event_reason="full_fill",
        )
    else:
        new_state = transition(cur_state, "partial_fill")
        store.update_order_state(
            data_dir, order["order_id"], new_state.value,
            filled_qty=new_filled,
            avg_fill_price=_avg_price(order, exec_row),
            event_from_state=cur_state.value, event_reason="partial_fill",
        )

    events.publish(
        data_dir,
        [events.execution_report(
            order["account_id"], order["symbol"], order["order_id"],
            int(exec_row["qty"]), float(exec_row["price"]), order["side"],
        )],
    )
    return {"inserted": True, "state": new_state.value}


def _avg_price(order: dict, exec_row: dict) -> float:
    cur_filled = int(order.get("filled_qty") or 0)
    cur_avg = float(order.get("avg_fill_price") or 0.0)
    new_qty = int(exec_row["qty"])
    new_price = float(exec_row["price"])
    total_cost = cur_avg * cur_filled + new_price * new_qty
    total = cur_filled + new_qty
    return round(total_cost / total, 4) if total > 0 else new_price


def arm_account(data_dir: Path, account_id: str, broker=None) -> dict:
    """DISARMED → ARMED_MANUAL (要求最近对账 clean)."""
    store.init_db(data_dir)
    acc = store.get_account(data_dir, account_id)
    if acc is None:
        raise TradingError(f"账户不存在: {account_id!r}")
    if acc["mode"] not in (TradingMode.DISARMED.value, TradingMode.DEGRADED.value):
        return {"account_id": account_id, "mode": acc["mode"], "already_armed": True}

    ok, reason = reconciliation.can_arm(data_dir, account_id)
    if not ok:
        raise TradingError(f"无法 arm: {reason}")

    store.set_account_mode(data_dir, account_id, TradingMode.ARMED_MANUAL.value)
    store.log_system_event(data_dir, "arm", {"account_id": account_id})
    events.publish(data_dir, [
        events.mode_change(account_id, acc["mode"], TradingMode.ARMED_MANUAL.value)
    ])
    return {"account_id": account_id, "mode": TradingMode.ARMED_MANUAL.value}


def disarm_account(data_dir: Path, account_id: str) -> dict:
    """任何 ARMED_* → DISARMED. 紧急停止单 (不撤已挂单, 仅停止新单)."""
    store.init_db(data_dir)
    acc = store.get_account(data_dir, account_id)
    if acc is None:
        raise TradingError(f"账户不存在: {account_id!r}")
    old_mode = acc["mode"]
    store.set_account_mode(data_dir, account_id, TradingMode.DISARMED.value)
    store.log_system_event(data_dir, "disarm", {"account_id": account_id})
    events.publish(data_dir, [events.mode_change(account_id, old_mode, TradingMode.DISARMED.value)])
    return {"account_id": account_id, "mode": TradingMode.DISARMED.value}


def trigger_reconciliation(
    data_dir: Path,
    account_id: str,
    broker_orders: list[dict],
    broker_positions: list[dict],
    broker_cash: float | None,
) -> dict:
    """触发对账 (由调用方注入 broker 报告)."""
    store.init_db(data_dir)
    result = reconciliation.run_reconciliation(
        data_dir, account_id, broker_orders, broker_positions, broker_cash,
    )
    if result["status"] == "dirty":
        # 脏对账 → 切回 DISARMED (但保留已挂单, 不自动撤)
        store.set_account_mode(data_dir, account_id, TradingMode.DEGRADED.value)
    return result


def list_orders(
    data_dir: Path,
    status: str | None = None,
    account_id: str | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    store.init_db(data_dir)
    return store.list_orders(data_dir, status=status, account_id=account_id, limit=limit, offset=offset)


def list_executions(
    data_dir: Path,
    *,
    account_id: str | None = None,
    order_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """成交明细列表 (透传 store.list_executions_all)."""
    store.init_db(data_dir)
    return store.list_executions_all(
        data_dir, account_id=account_id, order_id=order_id, limit=limit, offset=offset,
    )