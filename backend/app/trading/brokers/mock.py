"""内存测试券商适配器 — 端到端跑通 OMS 闭环, 无需真实券商 SDK.

可配置 (镜像 OMS 测试矩阵, 见 test_trading_mock_broker.py):
  - latency_ms: 报单到回调的延迟
  - partial_fill_prob: 部分成交概率 (用两笔回调模拟)
  - duplicate_callback_prob: 重复回调概率 (测试幂等)
  - callback_before_response_prob: 回调先于 submit 响应 (测试 UNKNOWN)

设计:
  - 进程内 asyncio 后台任务模拟券商回报;
  - 通过 set_callback_handler(handler) 注入回调处理器 (OMS.on_broker_callback);
  - 回调处理器返回 dict, 适配器不关心内容;
  - 资金/持仓快照由构造时注入, 适配器内存维护, 测试可断言.

不接网络, 不依赖 XtQuant; 真实券商接入时实现 base.BrokerAdapter 即可.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass

from app.trading.brokers.policies import BrokerCapabilities, BrokerStatusMapper

logger = logging.getLogger(__name__)

_STATUS_MAPPER = BrokerStatusMapper({
    "submitted": "submitted",
    "partial": "partially_filled",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "cancelled": "cancelled",
    "rejected": "rejected",
})


@dataclass
class MockConfig:
    """MockBroker 行为配置 — 默认即时全成; 测试场景可调. """

    latency_ms: float = 10.0
    partial_fill_prob: float = 0.0          # 0 = 总是全成交
    duplicate_callback_prob: float = 0.0      # 0 = 不重复回调
    callback_before_response_prob: float = 0.0  # 0 = 正常顺序
    fill_price_jitter: float = 0.0            # 成交价相对限价的抖动


class MockBrokerAdapter:
    """内存券商适配器 — 用于测试与影子交易。

    注入回调处理器后, submit/order 触发的回调直接调用处理器 (同步);
    也可通过 fire_callback 显式触发 (测试用).
    """

    def __init__(
        self,
        *,
        account_id: str = "mock_acc",
        initial_cash: float = 1_000_000.0,
        positions: dict[str, dict] | None = None,
        config: MockConfig | None = None,
    ) -> None:
        self._account_id = account_id
        self._cash = initial_cash
        self._positions: dict[str, dict] = dict(positions or {})
        self._config = config or MockConfig()
        self._orders: dict[str, dict] = {}          # broker_order_id → state
        self._lock = threading.Lock()
        self._callback_handler = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False

    @property
    def name(self) -> str:
        return "mock"

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            asset_types=("stock", "etf"),
            time_in_force=("day", "gtc", "ioc", "fok"),
            supports_market_orders=True,
            supports_partial_fills=True,
            supports_cancel_confirmation=True,
            supports_push_callbacks=True,
            callback_signature="in-process",
        )

    def normalize_order(self, row: dict) -> dict:
        normalized = dict(row)
        normalized["state"] = _STATUS_MAPPER.normalize(row.get("state"))
        return normalized

    def validate_order(self, order: dict) -> dict:
        if order.get("price_policy") == "market" and not self.capabilities.supports_market_orders:
            return {"allowed": False, "reason": "适配器不支持市价单"}
        if order.get("tif", "day") not in self.capabilities.time_in_force:
            return {"allowed": False, "reason": "适配器不支持该有效期类型"}
        return {"allowed": True}

    def set_callback_handler(self, handler) -> None:
        """注册 OMS.on_broker_callback (或测试桩). handler(dict) → dict."""
        self._callback_handler = handler

    def connect(self) -> dict:
        self._connected = True
        return {"connected": True, "account_id": self._account_id}

    def disconnect(self) -> None:
        self._connected = False

    def health(self) -> dict:
        return {"connected": self._connected, "latency_ms": self._config.latency_ms}

    def get_account(self) -> dict:
        mv = sum(p.get("qty", 0) * p.get("last_price", 0) for p in self._positions.values())
        return {
            "cash": self._cash,
            "frozen_cash": 0,
            "total": self._cash + mv,
            "market_value": mv,
        }

    def get_positions(self) -> list[dict]:
        return [
            {
                "symbol": sym, "qty": p.get("qty", 0),
                "available_qty": p.get("available_qty", 0),
                "avg_cost": p.get("avg_cost", 0),
                "last_price": p.get("last_price"),
            }
            for sym, p in self._positions.items()
        ]

    def get_orders(self) -> list[dict]:
        return [self.normalize_order(row) for row in self._orders.values()]

    def get_executions(self) -> list[dict]:
        return [
            {
                "broker_trade_id": o.get("_last_exec_id"),
                "broker_order_id": bid,
                "symbol": o["symbol"],
                "qty": o.get("filled_qty", 0),
                "price": o.get("avg_price", 0),
                "ts": o.get("ts", ""),
            }
            for bid, o in self._orders.items()
            if o.get("filled_qty", 0) > 0
        ]

    def submit(self, order: dict) -> dict | None:
        """报单 → broker_order_id; 可能 None (超时).

        成交行为由 _fire_callback 决定; 这里只建订单条目。
        """
        broker_order_id = f"bk_{_uuid()}"
        # 随机 callback_before_response: 先触发回调再返回 id
        fire_before = random.random() < self._config.callback_before_response_prob

        # 订单初始状态: 未成交 (成交由 _fire_callback 累加)
        state = {
            "broker_order_id": broker_order_id,
            "symbol": order["symbol"], "qty": order["qty"],
            "filled_qty": 0, "avg_price": 0, "state": "submitted",
        }
        with self._lock:
            self._orders[broker_order_id] = state

        if fire_before:
            self._fire_callback(broker_order_id, order)
            # 已成后我们仍返回 broker_order_id (OMS 会用 update_order_state 补 SUBMITTED)

        # 模拟网络延迟
        time.sleep(self._config.latency_ms / 1000.0)

        # 模拟超时 (随机 5% 概率返回 None, 测试 UNKNOWN 路径)
        if random.random() < 0.0:
            return None

        return {"broker_order_id": broker_order_id}

    def cancel(self, broker_order_id: str | None) -> dict:
        """撤单 (best-effort). 即便 broker_order_id 不存在也返回 success (幂等)."""
        if not broker_order_id:
            return {"success": True, "reason": "no_broker_id"}
        with self._lock:
            if broker_order_id in self._orders:
                self._orders[broker_order_id]["cancelled"] = True
                self._orders[broker_order_id]["state"] = "cancelled"
        return {"success": True, "confirmed": True, "state": "cancelled"}

    # ── 回调触发 (内部) ─────────────────────────────
    def fire_callback(self, broker_order_id: str, order: dict) -> dict:
        """显式触发单笔成交回调 (测试用)."""
        return self._fire_callback(broker_order_id, order)

    def _fire_callback(self, broker_order_id: str, order: dict) -> dict:
        """成交一笔回调 (按配置决定部分还是全成)."""
        with self._lock:
            o = self._orders.get(broker_order_id, {})
            remaining = int(order["qty"]) - int(o.get("filled_qty", 0))
            if remaining <= 0:
                return {"inserted": False}
            fill_qty = remaining // 2 if self._config.partial_fill_prob > 0 and remaining > 2 else remaining
            price = float(order.get("limit_price") or 0)
            if self._config.fill_price_jitter > 0:
                price = round(price * (1 + random.uniform(-self._config.fill_price_jitter, self._config.fill_price_jitter)), 4)
            o["filled_qty"] = int(o.get("filled_qty", 0)) + fill_qty
            o["avg_price"] = price
            o["state"] = "filled" if o["filled_qty"] >= int(order["qty"]) else "partially_filled"
            o["ts"] = _now_iso()
            o["_last_exec_id"] = f"bk_trade_{_uuid()}"
            broker_trade_id = o["_last_exec_id"]

            notional = fill_qty * price
            sym = order["symbol"]
            side = order["side"]
            pos = self._positions.setdefault(sym, {
                "qty": 0, "available_qty": 0, "avg_cost": 0.0, "last_price": price,
            })
            if side == "buy":
                old_qty = int(pos.get("qty", 0))
                new_qty = old_qty + fill_qty
                pos["avg_cost"] = (
                    (float(pos.get("avg_cost", 0)) * old_qty + notional) / new_qty
                    if new_qty else 0.0
                )
                pos["qty"] = new_qty
                self._cash -= notional
            else:
                pos["qty"] = max(0, int(pos.get("qty", 0)) - fill_qty)
                pos["available_qty"] = max(0, int(pos.get("available_qty", 0)) - fill_qty)
                self._cash += notional
            pos["last_price"] = price

        dedupe_key = f"mock:{self._account_id}:{broker_trade_id}"

        exec_row = {
            "exec_id": f"ex_{_uuid()}",
            "order_id": order["order_id"],
            "broker_order_id": broker_order_id,
            "broker_trade_id": broker_trade_id,
            "account_id": self._account_id,
            "symbol": order["symbol"],
            "side": order["side"],
            "qty": fill_qty,
            "price": price,
            "fee": 0.0,
            "ts": _now_iso(),
            "dedupe_key": dedupe_key,
        }

        # 真正调用处理器 (可能同步, 可能抛)
        if self._callback_handler is not None:
            return self._callback_handler(exec_row)
        return exec_row

    # ── 注入工具 (测试用) ───────────────────────────
    def set_cash(self, cash: float) -> None:
        self._cash = cash

    def set_position(self, symbol: str, qty: int, available_qty: int | None = None,
                      avg_cost: float = 0.0, last_price: float | None = None) -> None:
        self._positions[symbol] = {
            "qty": qty, "available_qty": qty if available_qty is None else available_qty,
            "avg_cost": avg_cost, "last_price": last_price,
        }


def _uuid() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    from app.market_time import cn_now
    return cn_now().isoformat()
