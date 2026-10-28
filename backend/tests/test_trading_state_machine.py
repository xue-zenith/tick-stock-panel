"""订单状态机测试 — 合法/非法转换 + 终态不可逆 + UNKNOWN 对账退出。

镜像 test_paper_trading.py 的纯函数断言风格。fail-closed: 非法转换抛 IllegalTransitionError。
"""
from __future__ import annotations

import pytest

from app.trading.contracts import OrderState
from app.trading.state_machine import (
    IllegalTransitionError,
    allowed_events,
    can_transition,
    is_terminal,
    transition,
)


def test_legal_path_full_fill():
    """完整 happy path: CREATED → RISK_PASSED → APPROVED → SUBMITTED → FILLED."""
    s = OrderState.CREATED
    s = transition(s, "risk_pass")
    assert s == OrderState.RISK_PASSED
    s = transition(s, "approve")
    assert s == OrderState.APPROVED
    s = transition(s, "submit_to_broker")
    assert s == OrderState.SUBMITTED
    s = transition(s, "full_fill")
    assert s == OrderState.FILLED


def test_legal_partial_then_fill():
    s = OrderState.SUBMITTED
    s = transition(s, "partial_fill")
    assert s == OrderState.PARTIALLY_FILLED
    s = transition(s, "fill")
    assert s == OrderState.FILLED


def test_partial_can_continue_partial():
    """多次部分成交都停留在 PARTIALLY_FILLED."""
    s = OrderState.SUBMITTED
    s = transition(s, "partial_fill")
    assert s == OrderState.PARTIALLY_FILLED
    s = transition(s, "partial_fill")
    assert s == OrderState.PARTIALLY_FILLED


def test_cancel_from_submitted():
    """撤单竞态: 撤单时仍 SUBMITTED (券商尚未回报) 允许直接 CANCELLED."""
    s = OrderState.SUBMITTED
    s = transition(s, "cancel")
    assert s == OrderState.CANCELLED


def test_cancel_partial_remainder():
    s = OrderState.PARTIALLY_FILLED
    s = transition(s, "cancel")
    assert s == OrderState.CANCELLED


def test_unknown_requires_reconcile():
    """UNKNOWN 只能通过 reconcile_* 退出; 业务事件不能推走."""
    s = OrderState.SUBMITTED
    s = transition(s, "report_unknown")
    assert s == OrderState.UNKNOWN
    # 业务事件 (full_fill / cancel) 在 UNKNOWN 状态下都应拒绝
    with pytest.raises(IllegalTransitionError):
        transition(OrderState.UNKNOWN, "full_fill")
    with pytest.raises(IllegalTransitionError):
        transition(OrderState.UNKNOWN, "cancel")
    # 只有 reconcile_* 路径合法
    assert transition(OrderState.UNKNOWN, "reconcile_filled") == OrderState.FILLED
    assert transition(OrderState.UNKNOWN, "reconcile_cancelled") == OrderState.CANCELLED
    assert transition(OrderState.UNKNOWN, "reconcile_unknown") == OrderState.UNKNOWN


def test_illegal_skip_risk():
    """跳过风控: CREATED → APPROVED 非法."""
    with pytest.raises(IllegalTransitionError):
        transition(OrderState.CREATED, "approve")


def test_illegal_skip_approval():
    """跳过审批: RISK_PASSED → SUBMITTED 非法."""
    with pytest.raises(IllegalTransitionError):
        transition(OrderState.RISK_PASSED, "submit_to_broker")


def test_illegal_resurrection():
    """终态不可复活: REJECTED → APPROVED / FILLED → SUBMITTED."""
    for terminal in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED):
        with pytest.raises(IllegalTransitionError):
            transition(terminal, "approve")
        with pytest.raises(IllegalTransitionError):
            transition(terminal, "submit_to_broker")
        with pytest.raises(IllegalTransitionError):
            transition(terminal, "full_fill")


def test_unknown_event_rejected():
    """未定义事件 fail-closed."""
    with pytest.raises(IllegalTransitionError):
        transition(OrderState.CREATED, "nonexistent_event")


def test_is_terminal():
    assert is_terminal(OrderState.FILLED)
    assert is_terminal(OrderState.CANCELLED)
    assert is_terminal(OrderState.REJECTED)
    assert is_terminal(OrderState.EXPIRED)
    assert not is_terminal(OrderState.SUBMITTED)
    assert not is_terminal(OrderState.PARTIALLY_FILLED)
    assert not is_terminal(OrderState.UNKNOWN)


def test_can_transition():
    assert can_transition(OrderState.CREATED, "risk_pass")
    assert not can_transition(OrderState.CREATED, "approve")
    assert not can_transition(OrderState.FILLED, "cancel")


def test_allowed_events_terminal_empty():
    assert allowed_events(OrderState.FILLED) == ()
    assert allowed_events(OrderState.CREATED) != ()