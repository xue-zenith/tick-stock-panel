"""IBKR TWS/IB Gateway adapter boundary.

The official ``ibapi`` package is installed from the vendor's downloaded API
source.  This adapter performs the socket handshake and keeps all operations
fail-closed until TWS/IB Gateway reports a valid managed account.
"""

from __future__ import annotations

import threading
from contextlib import suppress

from app.trading.brokers.policies import BrokerCapabilities, BrokerStatusMapper

_STATUS = BrokerStatusMapper(
    {
        "PendingSubmit": "submitted",
        "PreSubmitted": "submitted",
        "Submitted": "submitted",
        "PartiallyFilled": "partially_filled",
        "Filled": "filled",
        "Cancelled": "cancelled",
        "ApiCancelled": "cancelled",
        "Inactive": "rejected",
    }
)


class _App:
    def __init__(self, ibapi):
        class Wrapper(ibapi.wrapper.EWrapper):
            pass

        class Client(ibapi.client.EClient):
            pass

        self.wrapper_cls = Wrapper
        self.client_cls = Client


class IbkrBrokerAdapter:
    def __init__(self, account_id: str, config: dict | None = None):
        self._account_id = account_id
        self._cfg = config or {}
        self._client = None
        self._thread: threading.Thread | None = None
        self._connected = False
        self._next_order_id: int | None = None
        self._managed_accounts: list[str] = []
        self._error: str | None = None
        self._ready = threading.Event()
        self._account_done = threading.Event()
        self._positions_done = threading.Event()
        self._orders_done = threading.Event()
        self._exec_done = threading.Event()
        self._account_values: dict[str, float] = {}
        self._positions: list[dict] = []
        self._orders: dict[int, dict] = {}
        self._executions: list[dict] = []

    @property
    def name(self) -> str:
        return "ibkr"

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            asset_types=("stock", "etf", "option", "future", "forex"),
            time_in_force=("day", "gtc", "ioc"),
            supports_market_orders=True,
            supports_partial_fills=True,
            supports_cancel_confirmation=True,
            supports_push_callbacks=True,
            callback_signature="tws-socket",
        )

    def _sdk(self):
        try:
            from ibapi import client, wrapper  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "未安装 IBKR 官方 ibapi；请运行 scripts/brokers/install-ibkr.sh"
            ) from exc
        return client, wrapper

    def connect(self) -> dict:
        client_mod, wrapper_mod = self._sdk()
        expected = str(self._cfg.get("environment") or "PAPER").upper()
        port = int(self._cfg.get("port") or (4002 if expected == "PAPER" else 4001))
        account = str(self._cfg.get("account_id") or self._account_id)
        adapter = self

        class App(wrapper_mod.EWrapper, client_mod.EClient):
            def __init__(self):
                wrapper_mod.EWrapper.__init__(self)
                client_mod.EClient.__init__(self, self)

            def nextValidId(self, orderId):  # noqa: N802, N803 - ibapi callback contract
                adapter._next_order_id = int(orderId)
                adapter._ready.set()

            def managedAccounts(self, accountsList):  # noqa: N802, N803 - ibapi callback contract
                adapter._managed_accounts = [x for x in str(accountsList).split(",") if x]

            def accountSummary(self, reqId, account, tag, value, currency):  # noqa: N802, N803 - ibapi callback contract
                if account == adapter._account_id or not adapter._account_id:
                    with suppress(TypeError, ValueError):
                        adapter._account_values[tag] = float(value)

            def accountSummaryEnd(self, reqId):  # noqa: N802, N803 - ibapi callback contract
                adapter._account_done.set()

            def position(self, account, contract, pos, avgCost):  # noqa: N803 - ibapi callback contract
                if account == adapter._account_id or not adapter._account_id:
                    adapter._positions.append(
                        {
                            "symbol": contract.symbol,
                            "exchange": contract.exchange,
                            "qty": int(pos),
                            "available_qty": int(pos),
                            "avg_cost": float(avgCost),
                            "last_price": None,
                        }
                    )

            def positionEnd(self):  # noqa: N802 - ibapi callback contract
                adapter._positions_done.set()

            def openOrder(self, orderId, contract, order, orderState):  # noqa: N802, N803 - ibapi callback contract
                adapter._orders[int(orderId)] = {
                    "broker_order_id": str(orderId),
                    "symbol": contract.symbol,
                    "state": orderState.status,
                    "filled_qty": 0,
                    "avg_price": 0,
                    "side": str(order.action).lower(),
                    "qty": int(order.totalQuantity),
                    "perm_id": getattr(order, "permId", None),
                }

            def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, *_args):  # noqa: N802, N803 - ibapi callback contract
                row = adapter._orders.setdefault(int(orderId), {"broker_order_id": str(orderId)})
                row.update(
                    {
                        "state": status,
                        "filled_qty": int(filled),
                        "avg_price": float(avgFillPrice or 0),
                    }
                )

            def openOrderEnd(self):  # noqa: N802 - ibapi callback contract
                adapter._orders_done.set()

            def execDetails(self, reqId, contract, execution):  # noqa: N802, N803 - ibapi callback contract
                adapter._executions.append(
                    {
                        "broker_trade_id": str(execution.execId),
                        "broker_order_id": str(execution.orderId),
                        "qty": int(execution.shares),
                        "price": float(execution.price),
                        "ts": str(execution.time),
                        "symbol": contract.symbol,
                        "side": str(execution.side).lower(),
                    }
                )

            def execDetailsEnd(self, reqId):  # noqa: N802, N803 - ibapi callback contract
                adapter._exec_done.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N803 - ibapi callback contract
                if int(errorCode) not in (2104, 2106, 2158):
                    adapter._error = f"{errorCode}: {errorString}"

        self._client = App()
        self._client.connect(
            str(self._cfg.get("host") or "127.0.0.1"), port, int(self._cfg.get("client_id") or 17)
        )
        self._thread = threading.Thread(
            target=self._client.run, name=f"ibkr-{self._account_id}", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=float(self._cfg.get("timeout_s") or 5)):
            self.disconnect()
            return {
                "connected": False,
                "error": self._error or "未收到 TWS nextValidId；请检查 API Socket/端口/环境",
            }
        if account and self._managed_accounts and account not in self._managed_accounts:
            self.disconnect()
            return {"connected": False, "error": "指定账户不在 TWS managedAccounts 列表中"}
        self._connected = True
        return {"connected": True, "account_id": account, "environment": expected}

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.disconnect()
        self._client = None
        self._connected = False
        self._ready.clear()

    def discover_accounts(self) -> list[dict]:
        if not self._connected:
            raise RuntimeError("IBKR TWS/IB Gateway 未连接")
        return [
            {"account_id": account, "environment": str(self._cfg.get("environment") or "PAPER")}
            for account in self._managed_accounts
        ]

    def health(self) -> dict:
        return {
            "connected": bool(self._connected and self._client and self._client.isConnected()),
            "latency_ms": None,
            "error": self._error,
        }

    def _not_implemented(self, operation: str):
        raise RuntimeError(
            f"IBKR {operation} 需要 Gateway 事件订阅已就绪；当前连接仅完成 Socket/账户握手"
        )

    def get_account(self) -> dict:
        client = self._require_client()
        self._account_values = {}
        self._account_done.clear()
        client.reqAccountSummary(9001, "All", "AvailableFunds,TotalCashValue,NetLiquidation")
        self._account_done.wait(timeout=5)
        client.cancelAccountSummary(9001)
        if not self._account_values:
            self._not_implemented("账户摘要查询")
        cash = self._account_values.get(
            "AvailableFunds", self._account_values.get("TotalCashValue", 0.0)
        )
        total = self._account_values.get("NetLiquidation", cash)
        return {
            "cash": cash,
            "frozen_cash": max(0.0, total - cash),
            "total": total,
            "market_value": max(0.0, total - cash),
        }

    def get_positions(self) -> list[dict]:
        client = self._require_client()
        self._positions = []
        self._positions_done.clear()
        client.reqPositions()
        self._positions_done.wait(timeout=5)
        client.cancelPositions()
        return self._positions

    def get_orders(self) -> list[dict]:
        client = self._require_client()
        self._orders = {}
        self._orders_done.clear()
        client.reqOpenOrders()
        self._orders_done.wait(timeout=5)
        return [self.normalize_order(row) for row in self._orders.values()]

    def get_executions(self) -> list[dict]:
        client = self._require_client()
        self._executions = []
        self._exec_done.clear()
        from ibapi.execution import ExecutionFilter  # type: ignore[import-not-found]

        client.reqExecutions(9002, ExecutionFilter())
        self._exec_done.wait(timeout=5)
        return self._executions

    def submit(self, order: dict) -> dict | None:
        check = self.validate_order(order)
        if not check["allowed"]:
            return {"error": check["reason"]}
        client = self._require_client()
        from ibapi.contract import Contract  # type: ignore[import-not-found]
        from ibapi.order import Order  # type: ignore[import-not-found]

        contract = Contract()
        contract.symbol = str(order["symbol"])
        contract.secType = "STK"
        contract.exchange = str(order.get("exchange") or "SMART")
        contract.currency = str(order.get("currency") or "USD")
        request = Order()
        request.action = "BUY" if order["side"] == "buy" else "SELL"
        request.orderType = "MKT" if order.get("price_policy") == "market" else "LMT"
        request.totalQuantity = int(order["qty"])
        request.lmtPrice = float(order.get("limit_price") or 0)
        request.tif = "DAY"
        request.account = self._account_id
        order_id = int(self._next_order_id or 0)
        self._next_order_id = order_id + 1
        client.placeOrder(order_id, contract, request)
        return {"broker_order_id": str(order_id)}

    def cancel(self, broker_order_id: str | None) -> dict:
        if not broker_order_id:
            return {"success": False, "confirmed": False, "error": "缺少 IBKR orderId"}
        client = self._require_client()
        client.cancelOrder(int(broker_order_id), "")
        return {
            "success": True,
            "confirmed": False,
            "reason": "已发送取消请求，等待 orderStatus 回报",
        }

    def normalize_order(self, row: dict) -> dict:
        return {
            **row,
            "state": _STATUS.normalize(str(row.get("status") or row.get("state") or "unknown")),
        }

    def validate_order(self, order: dict) -> dict:
        if self._cfg.get("read_only", True):
            return {"allowed": False, "reason": "IBKR 当前为 Read-Only 阶段"}
        return {"allowed": True}

    def _require_client(self):
        if not self._connected or self._client is None:
            raise RuntimeError("IBKR TWS/IB Gateway 未连接")
        return self._client
