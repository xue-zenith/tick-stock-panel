"""券商连接与快照同步边界。

路由和调度器都通过这里访问 BrokerAdapter，避免一处吞异常、另一处把空数据
当成 clean。所有返回值先做最小契约校验；连接或数据不可信时 fail-closed。
"""
from __future__ import annotations

import math
import uuid
from pathlib import Path

from app.trading import events, oms, store
from app.trading.brokers import registry
from app.trading.contracts import OrderState


class BrokerUnavailableError(RuntimeError):
    """适配器未安装、无法构造或没有有效连接。"""


class BrokerSyncError(RuntimeError):
    """券商快照不符合契约，不能用于对账/风控。"""


def catalog() -> list[dict[str, object]]:
    return registry.catalog()


def status(broker_name: str, account_id: str) -> dict[str, object]:
    registered = registry.is_registered(broker_name)
    base: dict[str, object] = {
        "broker": broker_name,
        "registered": registered,
        "connected": False,
        "latency_ms": None,
        "error": None,
    }
    if not registered:
        base["error"] = "适配器未注册"
        return base
    adapter = registry.get_cached_adapter(broker_name, account_id)
    if adapter is None:
        return base
    try:
        health = adapter.health() or {}
        base["connected"] = bool(health.get("connected"))
        base["latency_ms"] = health.get("latency_ms")
        if health.get("error"):
            base["error"] = str(health["error"])
        capabilities = getattr(adapter, "capabilities", None)
        if capabilities is not None and hasattr(capabilities, "to_dict"):
            base["capabilities"] = capabilities.to_dict()
    except Exception as exc:  # noqa: BLE001 — 状态端点必须返回结构化错误
        base["error"] = str(exc)
    return base


def connect(data_dir: Path, broker_name: str, account_id: str):
    if not registry.is_registered(broker_name):
        raise BrokerUnavailableError(f"券商适配器 {broker_name!r} 未注册或未安装")
    adapter = registry.get_adapter(broker_name, account_id)
    if adapter is None:
        raise BrokerUnavailableError(f"券商适配器 {broker_name!r} 构造失败")

    # 可选回调能力：真实适配器可用同名方法把成交推送到 OMS；没有回调时由
    # get_executions 周期补偿。handler 绑定 data_dir，不把数据库暴露给适配器。
    set_handler = getattr(adapter, "set_callback_handler", None)
    if callable(set_handler):
        set_handler(lambda row: oms.on_broker_callback(data_dir, row))

    try:
        result = adapter.connect() or {}
    except Exception as exc:
        raise BrokerUnavailableError(f"连接券商失败: {exc}") from exc
    if not result.get("connected"):
        raise BrokerUnavailableError(str(result.get("error") or "券商未确认连接"))
    health = status(broker_name, account_id)
    if not health["connected"]:
        raise BrokerUnavailableError(str(health.get("error") or "券商健康检查未通过"))
    return adapter


def require_connected(data_dir: Path, broker_name: str, account_id: str):
    adapter = registry.get_cached_adapter(broker_name, account_id)
    if adapter is not None:
        current = status(broker_name, account_id)
        if current["connected"]:
            return adapter
    return connect(data_dir, broker_name, account_id)


def pull(adapter, *, include_positions: bool = True) -> dict[str, object]:
    """拉取一份同批次券商视图。任何子查询失败都不生成对账结果。"""
    try:
        health = adapter.health() or {}
        if not health.get("connected"):
            raise BrokerUnavailableError(str(health.get("error") or "券商连接已断开"))
        orders = adapter.get_orders()
        positions = adapter.get_positions() if include_positions else None
        account = adapter.get_account() if include_positions else None
        executions = adapter.get_executions()
    except BrokerUnavailableError:
        raise
    except Exception as exc:
        raise BrokerSyncError(f"拉取券商快照失败: {exc}") from exc

    if not isinstance(orders, list):
        raise BrokerSyncError("券商订单快照必须是列表")
    if include_positions and not isinstance(positions, list):
        raise BrokerSyncError("券商持仓快照必须是列表")
    if include_positions and not isinstance(account, dict):
        raise BrokerSyncError("券商资金快照必须是对象")
    if not isinstance(executions, list):
        raise BrokerSyncError("券商成交快照必须是列表")

    normalized_account = _normalize_account(account) if account is not None else None
    normalized_positions = (
        [_normalize_position(row) for row in positions]
        if positions is not None else None
    )
    return {
        "orders": [_normalize_order(adapter, row) for row in orders],
        "positions": normalized_positions,
        "account": normalized_account,
        "executions": executions,
        "as_of": events.now_iso(),
    }


def persist_snapshot(
    data_dir: Path,
    account_id: str,
    mode: str,
    pulled: dict[str, object],
) -> None:
    as_of = str(pulled["as_of"])
    if pulled.get("account") is None or pulled.get("positions") is None:
        raise BrokerSyncError("轻量订单快照不能覆盖资金/持仓基线")
    account = dict(pulled["account"])
    store.upsert_account_snapshot(data_dir, {
        "account_id": account_id,
        "as_of": as_of,
        **account,
        "nav": account["total"],
        "mode": mode,
    })
    store.replace_position_snapshots(
        data_dir,
        account_id,
        as_of,
        list(pulled["positions"]),
    )


def ingest_executions(data_dir: Path, account_id: str, broker_name: str, rows: list[dict]) -> int:
    """用券商成交查询补偿可能丢失的回调，按 broker_trade_id 幂等入账。"""
    inserted = 0
    for row in rows:
        broker_trade_id = str(row.get("broker_trade_id") or "").strip()
        broker_order_id = str(row.get("broker_order_id") or "").strip()
        if not broker_trade_id or not broker_order_id:
            continue
        order = store.get_order_by_broker_id(data_dir, broker_order_id)
        if order is None or order["account_id"] != account_id:
            store.log_system_event(data_dir, "unmatched_broker_execution", {
                "account_id": account_id,
                "broker_order_id": broker_order_id,
                "broker_trade_id": broker_trade_id,
            })
            continue
        qty = int(row.get("qty") or 0)
        price = float(row.get("price") or 0)
        if qty <= 0 or not math.isfinite(price) or price <= 0:
            continue
        result = oms.on_broker_callback(data_dir, {
            "exec_id": f"ex_{uuid.uuid4().hex}",
            "order_id": order["order_id"],
            "broker_order_id": broker_order_id,
            "broker_trade_id": broker_trade_id,
            "account_id": account_id,
            "symbol": order["symbol"],
            "side": order["side"],
            "qty": qty,
            "price": price,
            "fee": float(row.get("fee") or 0),
            "ts": str(row.get("ts") or events.now_iso()),
            "dedupe_key": f"{broker_name}:{account_id}:{broker_trade_id}",
        })
        inserted += int(bool(result.get("inserted")))
    return inserted


def _normalize_account(row: dict) -> dict[str, float]:
    result: dict[str, float] = {}
    for key in ("cash", "frozen_cash", "market_value", "total"):
        try:
            value = float(row.get(key, 0))
        except (TypeError, ValueError) as exc:
            raise BrokerSyncError(f"资金字段 {key} 非数字") from exc
        if not math.isfinite(value) or value < 0:
            raise BrokerSyncError(f"资金字段 {key} 非法: {value!r}")
        result[key] = value
    if result["total"] == 0:
        result["total"] = result["cash"] + result["frozen_cash"] + result["market_value"]
    return result


def _normalize_position(row: dict) -> dict[str, object]:
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        raise BrokerSyncError("持仓缺少 symbol")
    try:
        qty = int(row.get("qty") or 0)
        available = int(row.get("available_qty") or 0)
        avg_cost = float(row.get("avg_cost") or 0)
        last_price_raw = row.get("last_price")
        last_price = None if last_price_raw is None else float(last_price_raw)
    except (TypeError, ValueError) as exc:
        raise BrokerSyncError(f"持仓 {symbol} 数值字段非法") from exc
    if qty < 0 or available < 0 or available > qty:
        raise BrokerSyncError(f"持仓 {symbol} 数量非法")
    if avg_cost < 0 or (last_price is not None and (last_price < 0 or not math.isfinite(last_price))):
        raise BrokerSyncError(f"持仓 {symbol} 价格非法")
    market_value = qty * (last_price or 0)
    pnl = qty * ((last_price or avg_cost) - avg_cost)
    return {
        "symbol": symbol,
        "qty": qty,
        "available_qty": available,
        "avg_cost": avg_cost,
        "last_price": last_price,
        "market_value": market_value,
        "pnl": pnl,
    }


def _normalize_order(adapter, row: dict) -> dict[str, object]:
    if not isinstance(row, dict):
        raise BrokerSyncError("券商订单快照每一项必须是对象")
    normalizer = getattr(adapter, "normalize_order", None)
    normalized = normalizer(dict(row)) if callable(normalizer) else dict(row)
    if not isinstance(normalized, dict):
        raise BrokerSyncError("券商订单映射结果必须是对象")
    broker_order_id = str(normalized.get("broker_order_id") or "").strip()
    if not broker_order_id:
        raise BrokerSyncError("券商订单缺少 broker_order_id")
    state = str(normalized.get("state") or OrderState.UNKNOWN.value)
    valid_states = {item.value for item in OrderState}
    if state not in valid_states:
        state = OrderState.UNKNOWN.value
    try:
        filled_qty = int(normalized.get("filled_qty") or 0)
        avg_price_raw = normalized.get("avg_price")
        avg_price = None if avg_price_raw is None else float(avg_price_raw)
    except (TypeError, ValueError) as exc:
        raise BrokerSyncError(f"券商订单 {broker_order_id} 成交字段非法") from exc
    if filled_qty < 0 or (avg_price is not None and (avg_price < 0 or not math.isfinite(avg_price))):
        raise BrokerSyncError(f"券商订单 {broker_order_id} 成交字段非法")
    return {
        **normalized,
        "broker_order_id": broker_order_id,
        "state": state,
        "filled_qty": filled_qty,
        "avg_price": avg_price,
    }
