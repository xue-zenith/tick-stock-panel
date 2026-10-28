"""订单状态机 — 合法转换表与 fail-closed 转换函数。

实盘与模拟盘 (paper.py 的 pending/filled/cancelled/expired) 的关键差异:
  - 状态更细: 覆盖部分成交、UNKNOWN 对账、撤单竞态、审批前后;
  - fail-closed: 非法转换直接抛 IllegalTransition, 不静默兜底;
  - 终态不可逆: FILLED/CANCELLED/REJECTED/EXPIRED 任何事件都不再迁移。

不变量 (OMS 落库时必须保证):
  1. CREATED → APPROVED 跳过风控: 非法。
  2. RISK_PASSED → SUBMITTED 跳过审批: 非法。
  3. FILLED/REJECTED/CANCELLED/EXPIRED 不可再迁移到任何状态。
  4. UNKNOWN 是临时态: 只能通过 reconcile → FILLED/CANCELLED/REJECTED 退出,
     不允许任何业务事件直接把 UNKNOWN 推到 SUBMITTED。
"""
from __future__ import annotations

from app.trading.contracts import OrderState


class IllegalTransitionError(Exception):
    """非法状态转换 — OMS 必须捕获并 fail-closed, 不静默落库脏状态。"""


# 合法转换表: (from_state, event) → to_state
# event 是 OMS 内部业务事件名 (与券商回报一一映射)。
_TRANSITIONS: dict[tuple[OrderState, str], OrderState] = {
    # 风控阶段
    (OrderState.CREATED, "risk_pass"): OrderState.RISK_PASSED,
    (OrderState.CREATED, "risk_reject"): OrderState.REJECTED,
    # 审批阶段
    (OrderState.RISK_PASSED, "approve"): OrderState.APPROVED,
    (OrderState.RISK_PASSED, "reject"): OrderState.REJECTED,
    (OrderState.RISK_PASSED, "expire"): OrderState.EXPIRED,
    (OrderState.RISK_PASSED, "cancel"): OrderState.CANCELLED,
    # 提交券商
    (OrderState.APPROVED, "submit_to_broker"): OrderState.SUBMITTED,
    (OrderState.APPROVED, "cancel"): OrderState.CANCELLED,
    # 券商回报 (SUBMITTED 起步)
    (OrderState.SUBMITTED, "partial_fill"): OrderState.PARTIALLY_FILLED,
    (OrderState.SUBMITTED, "full_fill"): OrderState.FILLED,
    (OrderState.SUBMITTED, "broker_reject"): OrderState.REJECTED,
    (OrderState.SUBMITTED, "cancel"): OrderState.CANCELLED,        # 撤单竞态: 撤单时可能仍 SUBMITTED
    (OrderState.SUBMITTED, "report_unknown"): OrderState.UNKNOWN,  # 超时/断线 → 需对账
    # 部分成交后
    (OrderState.PARTIALLY_FILLED, "fill"): OrderState.FILLED,            # 后续成交补足
    (OrderState.PARTIALLY_FILLED, "partial_fill"): OrderState.PARTIALLY_FILLED,  # 继续部分成交
    (OrderState.PARTIALLY_FILLED, "cancel"): OrderState.CANCELLED,      # 撤剩余
    (OrderState.PARTIALLY_FILLED, "report_unknown"): OrderState.UNKNOWN,
    # UNKNOWN 退出路径: 只能通过 reconcile (券商查询) 解决
    (OrderState.UNKNOWN, "reconcile_filled"): OrderState.FILLED,
    (OrderState.UNKNOWN, "reconcile_cancelled"): OrderState.CANCELLED,
    (OrderState.UNKNOWN, "reconcile_rejected"): OrderState.REJECTED,
    (OrderState.UNKNOWN, "reconcile_partial"): OrderState.PARTIALLY_FILLED,
    (OrderState.UNKNOWN, "reconcile_unknown"): OrderState.UNKNOWN,  # 仍查不到 → 保持, 禁止新单
}

_TERMINAL_STATES: frozenset[OrderState] = frozenset({
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
})


def is_terminal(state: OrderState) -> bool:
    """终态判断 — 终态订单任何事件都不再迁移。"""
    return state in _TERMINAL_STATES


def transition(current: OrderState, event: str) -> OrderState:
    """纯函数: (current, event) → new_state。

    非法转换 (包括终态试图再迁移、跳过风控/审批、UNKNOWN 被
    业务事件直接推走) 抛 IllegalTransition。调用方必须 fail-closed:
    捕获后拒绝继续写库与发券商, 不静默兜底成某个“合理”状态。
    """
    if is_terminal(current):
        raise IllegalTransitionError(
            f"非法转换: 终态 {current.value} 不能响应事件 {event!r}"
        )
    key = (current, event)
    if key not in _TRANSITIONS:
        raise IllegalTransitionError(
            f"非法转换: {current.value} --{event!r}--> 未定义"
        )
    return _TRANSITIONS[key]


def can_transition(current: OrderState, event: str) -> bool:
    """是否允许该转换 (不抛异常)。供 OMS 预检使用。"""
    if is_terminal(current):
        return False
    return (current, event) in _TRANSITIONS


def allowed_events(current: OrderState) -> tuple[str, ...]:
    """当前状态允许的事件列表 (供状态机自检与 UI 展示)。"""
    if is_terminal(current):
        return ()
    return tuple(
        event for (state, event) in _TRANSITIONS if state == current
    )
