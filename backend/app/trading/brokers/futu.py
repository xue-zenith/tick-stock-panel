"""Futu OpenAPI BrokerAdapter.

The module deliberately imports ``futu`` lazily.  The core application remains
installable without OpenD or the vendor SDK; connecting a Futu account is
fail-closed until both are present and authenticated.
"""
from __future__ import annotations

import time

from app.trading.brokers.policies import BrokerCapabilities, BrokerStatusMapper

_STATUS = BrokerStatusMapper({
    "SUBMITTED": "submitted", "WAITING_SUBMIT": "submitted", "SUBMITTING": "submitted",
    "PARTIAL_FILLED": "partially_filled", "FILLED_ALL": "filled",
    "CANCELLED_ALL": "cancelled", "FAILED": "rejected", "DISABLED": "rejected",
})


class FutuBrokerAdapter:
    def __init__(self, account_id: str, config: dict | None = None):
        self._account_id = account_id
        self._cfg = config or {}
        self._trade = None
        self._connected = False
        self._last_error: str | None = None

    @property
    def name(self) -> str:
        return "futu"

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            asset_types=("stock", "etf"),
            time_in_force=("day",),
            supports_market_orders=True,
            supports_partial_fills=True,
            supports_cancel_confirmation=True,
            supports_push_callbacks=True,
            callback_signature="opend-session",
        )

    def _sdk(self):
        try:
            import futu  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("未安装 futu-api；请运行 scripts/brokers/install-futu.sh") from exc
        return futu

    def connect(self) -> dict:
        futu = self._sdk()
        host = str(self._cfg.get("host") or "127.0.0.1")
        port = int(self._cfg.get("port") or 11111)
        env_name = str(self._cfg.get("environment") or "SIMULATE").upper()
        market = getattr(futu.TrdMarket, str(self._cfg.get("market") or "HK"), futu.TrdMarket.HK)
        env = getattr(futu.TrdEnv, env_name, futu.TrdEnv.SIMULATE)
        self._env = env
        self._market = market
        self._trade = futu.OpenSecTradeContext(host=host, port=port, filter_trdmarket=market)
        code, data = self._trade.get_acc_list()
        if code != futu.RET_OK:
            self._last_error = str(data)
            self.disconnect()
            return {"connected": False, "error": f"OpenD 账户发现失败: {data}"}
        rows = data.to_dict("records") if hasattr(data, "to_dict") else []
        account_ids = {str(row.get("acc_id")) for row in rows}
        if self._account_id and self._account_id not in account_ids:
            self.disconnect()
            return {"connected": False, "error": "指定 acc_id 不在 OpenD 账户列表中"}
        self._connected = True
        return {"connected": True, "account_id": self._account_id, "environment": env_name}

    def disconnect(self) -> None:
        if self._trade is not None:
            close = getattr(self._trade, "close", None)
            if callable(close):
                close()
        self._trade = None
        self._connected = False

    def discover_accounts(self) -> list[dict]:
        futu = self._sdk()
        if self._trade is None:
            raise RuntimeError("OpenD 未连接")
        code, data = self._trade.get_acc_list()
        if code != futu.RET_OK:
            raise RuntimeError(str(data))
        rows = data.to_dict("records") if hasattr(data, "to_dict") else []
        return [{"account_id": str(row.get("acc_id")), "environment": str(row.get("trd_env") or ""), "market": str(row.get("trd_market") or "")} for row in rows]

    def health(self) -> dict:
        if not self._connected or self._trade is None:
            return {"connected": False, "latency_ms": None, "error": self._last_error or "OpenD 未连接"}
        started = time.perf_counter()
        code, data = self._trade.get_acc_list()
        if code != self._sdk().RET_OK:
            return {"connected": False, "latency_ms": None, "error": str(data)}
        return {"connected": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2), "environment": str(self._cfg.get("environment") or "SIMULATE")}

    def _query(self, method: str, **kwargs):
        if not self._connected or self._trade is None:
            raise RuntimeError("OpenD 未连接")
        code, data = getattr(self._trade, method)(**kwargs)
        if code != self._sdk().RET_OK:
            raise RuntimeError(str(data))
        return data.to_dict("records") if hasattr(data, "to_dict") else []

    def get_account(self) -> dict:
        rows = self._query("accinfo_query", trd_env=self._env, acc_id=int(self._account_id) if self._account_id.isdigit() else None)
        if not rows:
            raise RuntimeError("OpenD 未返回账户资金")
        row = rows[0]
        return {"cash": float(row.get("cash") or 0), "frozen_cash": float(row.get("frozen_cash") or 0), "total": float(row.get("total_assets") or row.get("total") or 0), "market_value": float(row.get("market_val") or 0)}

    def get_positions(self) -> list[dict]:
        rows = self._query("position_list_query", trd_env=self._env, acc_id=int(self._account_id) if self._account_id.isdigit() else None)
        return [{"symbol": str(row.get("code") or ""), "qty": int(row.get("qty") or 0), "available_qty": int(row.get("can_sell_qty") or row.get("qty") or 0), "avg_cost": float(row.get("cost_price") or 0), "last_price": float(row.get("market_price") or 0)} for row in rows if row.get("code")]

    def get_orders(self) -> list[dict]:
        rows = self._query("order_list_query", trd_env=self._env, acc_id=int(self._account_id) if self._account_id.isdigit() else None)
        return [self.normalize_order(row) for row in rows]

    def get_executions(self) -> list[dict]:
        rows = self._query("deal_list_query", trd_env=self._env, acc_id=int(self._account_id) if self._account_id.isdigit() else None)
        return [{"broker_trade_id": str(row.get("deal_id")), "broker_order_id": str(row.get("order_id")), "qty": int(row.get("qty") or 0), "price": float(row.get("price") or 0), "ts": str(row.get("create_time") or "")} for row in rows]

    def normalize_order(self, row: dict) -> dict:
        return {**row, "broker_order_id": str(row.get("order_id") or ""), "state": _STATUS.normalize(str(row.get("order_status") or "")), "filled_qty": int(row.get("qty_dealt") or 0), "avg_price": float(row.get("dealt_avg_price") or 0)}

    def validate_order(self, order: dict) -> dict:
        if str(self._cfg.get("environment") or "SIMULATE").upper() == "REAL" and not self._cfg.get("trade_unlocked"):
            return {"allowed": False, "reason": "Futu 实盘账户尚未在 OpenD 解锁交易"}
        return {"allowed": True}

    def submit(self, order: dict) -> dict | None:
        futu = self._sdk()
        check = self.validate_order(order)
        if not check["allowed"]:
            return {"error": check["reason"]}
        side = futu.TrdSide.BUY if order["side"] == "buy" else futu.TrdSide.SELL
        order_type = futu.OrderType.MARKET if order.get("price_policy") == "market" else futu.OrderType.NORMAL
        code, data = self._trade.place_order(price=float(order.get("limit_price") or 0), qty=int(order["qty"]), code=str(order["symbol"]), trd_side=side, order_type=order_type, trd_env=self._env, acc_id=int(self._account_id) if self._account_id.isdigit() else None, remark=str(order.get("order_id") or ""))
        if code != futu.RET_OK:
            return {"error": str(data)}
        row = data.to_dict("records")[0] if hasattr(data, "to_dict") else {}
        return {"broker_order_id": str(row.get("order_id") or "")}

    def cancel(self, broker_order_id: str | None) -> dict:
        if not broker_order_id:
            return {"success": False, "confirmed": False, "error": "缺少券商订单号"}
        futu = self._sdk()
        code, data = self._trade.modify_order(op=futu.ModifyOrderOp.CANCEL, order_id=int(broker_order_id), qty=0, price=0, trd_env=self._env, acc_id=int(self._account_id) if self._account_id.isdigit() else None)
        if code != futu.RET_OK:
            return {"success": False, "confirmed": False, "error": str(data)}
        return {"success": True, "confirmed": True}
