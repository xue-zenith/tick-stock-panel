"""XtQuant/QMT adapter boundary (Windows + MiniQMT only)."""
from __future__ import annotations

from app.trading.brokers.policies import BrokerCapabilities, BrokerStatusMapper

_STATUS = BrokerStatusMapper({"已报": "submitted", "部成": "partially_filled", "已成": "filled", "已撤": "cancelled", "废单": "rejected"})


class XtQuantBrokerAdapter:
    def __init__(self, account_id: str, config: dict | None = None):
        self._account_id = account_id
        self._cfg = config or {}
        self._trader = None
        self._account = None
        self._connected = False

    @property
    def name(self) -> str:
        return "qmt"

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(asset_types=("stock", "etf"), time_in_force=("day",), supports_market_orders=True, supports_partial_fills=True, supports_cancel_confirmation=True, supports_push_callbacks=True, callback_signature="xtquant-session")

    def _sdk(self):
        try:
            from xtquant import xtconstant, xttrader  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("未安装 XtQuant；QMT 只能在券商 Windows 交易机运行") from exc
        return xtconstant, xttrader

    def connect(self) -> dict:
        _, xttrader = self._sdk()
        userdata = str(self._cfg.get("userdata_path") or "")
        if not userdata:
            return {"connected": False, "error": "未配置 QMT userdata_mini 路径"}
        session_id = int(self._cfg.get("session_id") or 41001)
        self._trader = xttrader.XtQuantTrader(userdata, session_id)
        self._trader.start()
        if self._trader.connect() != 0:
            self.disconnect()
            return {"connected": False, "error": "XtQuant connect 未返回 0"}
        infos = self._trader.query_account_infos()
        accounts = {str(getattr(row, "account_id", "")) for row in (infos or [])}
        if self._account_id not in accounts:
            self.disconnect()
            return {"connected": False, "error": "资金账号未在 QMT 账户列表中发现"}
        self._account = xttrader.StockAccount(self._account_id, str(self._cfg.get("account_type") or "STOCK"))
        if self._trader.subscribe(self._account) != 0:
            self.disconnect()
            return {"connected": False, "error": "QMT 账户订阅失败"}
        self._connected = True
        return {"connected": True, "account_id": self._account_id}

    def disconnect(self) -> None:
        if self._trader is not None:
            self._trader.stop()
        self._trader = None
        self._connected = False

    def discover_accounts(self) -> list[dict]:
        if self._trader is None:
            raise RuntimeError("QMT 未连接")
        return [{"account_id": str(getattr(row, "account_id", "")), "account_type": str(getattr(row, "account_type", "STOCK"))} for row in (self._trader.query_account_infos() or [])]

    def health(self) -> dict:
        return {"connected": self._connected, "latency_ms": None, "error": None if self._connected else "QMT 未连接"}

    def _require(self):
        if not self._connected or self._trader is None or self._account is None:
            raise RuntimeError("QMT 未连接")
        return self._trader, self._account

    def get_account(self) -> dict:
        trader, account = self._require()
        row = trader.query_stock_asset(account)
        return {"cash": float(getattr(row, "cash", 0)), "frozen_cash": float(getattr(row, "frozen_cash", 0)), "total": float(getattr(row, "total_asset", 0)), "market_value": float(getattr(row, "market_value", 0))}

    def get_positions(self) -> list[dict]:
        trader, account = self._require()
        rows = trader.query_stock_positions(account) or []
        return [{"symbol": str(getattr(row, "stock_code", "")), "qty": int(getattr(row, "volume", 0)), "available_qty": int(getattr(row, "can_use_volume", 0)), "avg_cost": float(getattr(row, "open_price", 0)), "last_price": float(getattr(row, "market_value", 0))} for row in rows]

    def get_orders(self) -> list[dict]:
        trader, account = self._require()
        return [self.normalize_order(vars(row)) for row in (trader.query_stock_orders(account) or [])]
    def get_executions(self) -> list[dict]:
        trader, account = self._require()
        rows = trader.query_stock_trades(account) or []
        return [{"broker_trade_id": str(getattr(row, "traded_id", "")), "broker_order_id": str(getattr(row, "order_id", "")), "qty": int(getattr(row, "traded_volume", 0)), "price": float(getattr(row, "traded_price", 0)), "ts": str(getattr(row, "traded_time", ""))} for row in rows]
    def normalize_order(self, row: dict) -> dict:
        return {**row, "broker_order_id": str(row.get("order_id") or row.get("order_id", "")), "state": _STATUS.normalize(str(row.get("order_status", "unknown"))), "filled_qty": int(row.get("traded_volume", 0)), "avg_price": float(row.get("traded_price", 0))}
    def validate_order(self, order: dict) -> dict:
        return {"allowed": bool(self._cfg.get("programmatic_enabled", False)), "reason": "QMT 程序化交易权限未确认" if not self._cfg.get("programmatic_enabled", False) else ""}
    def submit(self, order: dict) -> dict | None:
        if not self.validate_order(order)["allowed"]:
            return {"error": "QMT 程序化交易权限未确认"}
        trader, account = self._require()
        constant, _ = self._sdk()
        side = constant.STOCK_BUY if order["side"] == "buy" else constant.STOCK_SELL
        oid = trader.order_stock(account, order["symbol"], side, int(order["qty"]), constant.OPT_LIMIT, float(order.get("limit_price") or 0), "", str(order.get("order_id") or ""))
        return {"broker_order_id": str(oid)} if oid and int(oid) > 0 else {"error": f"QMT 报单失败: {oid}"}
    def cancel(self, broker_order_id: str | None) -> dict:
        trader, account = self._require()
        result = trader.cancel_order_stock(account, int(broker_order_id or 0))
        return {"success": result == 0, "confirmed": result == 0, "error": None if result == 0 else str(result)}
