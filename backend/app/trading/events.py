"""交易事件 → AlertEvent dict 格式器 — 复用 alert_store 事件总线。

复用 alert_store.append_many + events.bus.publish, SSE/toast/语音/webhook
链路无需新增通道。事件 dict 结构与 paper.py:_fill_event 一致:
  {source, type, severity, ts, symbol, account, ...detail}

事件种类:
  - order_state_change: 订单状态转换 (审计)
  - execution: 成交回报 (信息)
  - risk_reject: 风控拒绝 (警告)
  - approval_pending / approved / rejected: 审批流
  - reconciliation_diff: 对账差异 (警告)
  - mode_change: 账户模式变化 (DISARMED ↔ ARMED, 高严重)
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.market_time import cn_now

logger = logging.getLogger(__name__)

EVENT_SOURCE = "live_trading"


def _ts_ms() -> int:
    """SSE/alert_store 事件时间戳约定为毫秒 (镜像 alert_store)。"""
    import time
    return int(time.time() * 1000)


def _base(
    type_: str,
    severity: str,
    *,
    symbol: str | None = None,
    account_id: str | None = None,
    detail: dict | None = None,
) -> dict:
    ev = {
        "source": EVENT_SOURCE,
        "type": type_,
        "severity": severity,
        "ts": _ts_ms(),
    }
    if symbol:
        ev["symbol"] = symbol
    if account_id:
        ev["account"] = account_id
    if detail:
        ev.update(detail)
    return ev


def order_state_change(
    account_id: str,
    symbol: str,
    order_id: str,
    from_state: str,
    to_state: str,
    reason: str | None = None,
) -> dict:
    return _base(
        "order_state_change", "info",
        symbol=symbol, account_id=account_id,
        detail={
            "order_id": order_id,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
        },
    )


def execution_report(
    account_id: str,
    symbol: str,
    order_id: str,
    qty: int,
    price: float,
    side: str,
) -> dict:
    sev = "info"
    return _base(
        "execution", sev,
        symbol=symbol, account_id=account_id,
        detail={
            "order_id": order_id, "qty": qty, "price": price,
            "side": side, "message": f"成交 {symbol} {side} {qty}@{price}",
        },
    )


def risk_reject(
    account_id: str,
    symbol: str,
    intent_id: str,
    reasons: tuple[str, ...],
) -> dict:
    return _base(
        "risk_reject", "warning",
        symbol=symbol, account_id=account_id,
        detail={
            "intent_id": intent_id,
            "reasons": list(reasons),
            "message": f"风控拒绝 {symbol}: {'; '.join(reasons)}",
        },
    )


def approval_pending(account_id: str, symbol: str, intent_id: str, side: str) -> dict:
    return _base(
        "approval_pending", "info",
        symbol=symbol, account_id=account_id,
        detail={"intent_id": intent_id, "side": side, "message": f"待审批 {symbol} {side}"},
    )


def approval_result(
    account_id: str,
    symbol: str,
    intent_id: str,
    approved: bool,
) -> dict:
    return _base(
        "approval_result", "info" if approved else "warning",
        symbol=symbol, account_id=account_id,
        detail={
            "intent_id": intent_id, "approved": approved,
            "message": f"{'审批通过' if approved else '审批拒绝'} {symbol}",
        },
    )


def reconciliation_diff(account_id: str, diff_count: int) -> dict:
    return _base(
        "reconciliation_diff", "warning",
        account_id=account_id,
        detail={"diff_count": diff_count, "message": f"对账发现 {diff_count} 处差异"},
    )


def mode_change(account_id: str, from_mode: str, to_mode: str) -> dict:
    sev = "critical" if to_mode == "halted" else "info"
    return _base(
        "mode_change", sev,
        account_id=account_id,
        detail={
            "from_mode": from_mode, "to_mode": to_mode,
            "message": f"账户模式 {from_mode} → {to_mode}",
        },
    )


def publish(data_dir: Path, events: list[dict]) -> None:
    """落盘到 alert_store 并广播到事件总线 (软失败, 不阻塞主流程).

    双发: alert_store.append_many 内部已 bus.publish("alert", ..., scope="read:analysis")
    (告警链路); 这里追加 bus.publish("live_trading", ..., scope="live:read")
    供实盘页面 SSE 监听自动刷新 (蓝图 §12.3 REST 快照 + SSE 增量)。
    """
    if not events:
        return
    try:
        from app.services import alert_store
        alert_store.append_many(data_dir, events)
    except Exception:  # noqa: BLE001 — 事件通道绝不反噬主流程
        logger.debug("live_trading 事件落盘失败 (忽略)", exc_info=True)
    # 追加 live_trading 事件类型广播 (scope=live:read, 实盘页面订阅)
    try:
        from app.services.events import bus
        for ev in events:
            bus.publish("live_trading", ev, scope="live:read")
    except Exception:  # noqa: BLE001
        logger.debug("live_trading 事件总线广播失败 (忽略)", exc_info=True)


def now_iso() -> str:
    return cn_now().isoformat()
