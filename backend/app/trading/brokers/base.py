"""券商适配器 Protocol — 上层 OMS 只依赖此抽象, 不依赖券商 SDK.

适配器职责 (相对模拟盘的关键差异):
  - 把券商原始枚举映射成本地 OrderState (未知状态 fail-closed 到 unknown);
  - submit 返回 {broker_order_id} 或 {error} 或 None (超时);
  - cancel 必须返回券商确认；失败时本地状态不得伪装成 CANCELLED;
  - 不修改业务数据库 (审计与状态由 OMS 统一持久化);
  - 不执行策略和风控.

真实适配器 (XtQuant/PTrade/Futu/IBKR) 实现此 Protocol;
MockBrokerAdapter (mock.py) 用于测试与影子交易, 端到端跑通 OMS 闭环.
"""
from __future__ import annotations

from typing import Protocol

from app.trading.brokers.policies import BrokerCapabilities


class BrokerAdapter(Protocol):
    """券商适配器最小协议 (OMS 侧只调这几个方法)."""

    @property
    def name(self) -> str:
        """适配器名称 (mock / xtquant / futu / ibkr)."""
        ...

    @property
    def capabilities(self) -> BrokerCapabilities:
        """声明支持的资产、订单类型、撤单确认与回调安全能力。"""
        ...

    def normalize_order(self, row: dict) -> dict:
        """把厂商委托字段和状态枚举映射为平台标准结构。"""
        ...

    def validate_order(self, order: dict) -> dict:
        """厂商专用预检 → {allowed: bool, reason?: str}。"""
        ...

    def connect(self) -> dict:
        """连接券商 → {connected: bool, account_id: str}."""
        ...

    def disconnect(self) -> None:
        """断开 (清理后台任务, 不撤已挂单)."""
        ...

    def health(self) -> dict:
        """健康检查 → {connected: bool, latency_ms: float}."""
        ...

    def get_account(self) -> dict:
        """拉取资金快照 → {cash, frozen_cash, total, market_value}."""
        ...

    def get_positions(self) -> list[dict]:
        """拉取持仓快照 → [{symbol, qty, available_qty, avg_cost, last_price}]."""
        ...

    def get_orders(self) -> list[dict]:
        """拉取当日委托 → [{broker_order_id, state, filled_qty, avg_price}]."""
        ...

    def get_executions(self) -> list[dict]:
        """拉取当日成交 → [{broker_trade_id, broker_order_id, qty, price, ts}]."""
        ...

    def submit(self, order: dict) -> dict | None:
        """报单 → {broker_order_id} | {error} | None(超时).

        可异步: 适配器可能在此返回 broker_order_id 之前先由后台线程触发回调.
        OMS 必须容忍回调先于响应到达.
        """
        ...

    def cancel(self, broker_order_id: str | None) -> dict:
        """撤单并等待券商确认 → {success: bool, error?: str}."""
        ...
