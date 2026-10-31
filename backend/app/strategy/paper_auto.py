"""自动跟单规则域 — 监控事件触发模拟盘自动下单 (V2)。

规则匹配: 事件 source=="strategy" 且 strategy_id 相等 (跟策略), 或 rule_id 相等
(跟任意监控规则, 含信号/价格规则)。仓位: 固定金额 或 账户权益百分比。
冷却: 同规则同 symbol 最近一次自动下单后 N 个交易日内不再触发 (按订单
created_at 日期判断, 无独立状态文件 — 可由订单列表重放推导)。

规则按账户存放 (accounts/{account_id}/auto_rules/), 账户间隔离;
on_rule_events 由钩子对每个账户各调一次。

触发链路: quote_service._evaluate_monitors 产出的 rule_events →
paper_auto.on_rule_events → paper.create_order(source=f"auto:{rule_id}")。
所有下单走 paper 域的同一把锁与校验, 账户冻结/资金/涨跌停等约束自动生效。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date as _date
from datetime import datetime
from math import isfinite
from pathlib import Path

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text
from app.strategy import paper

logger = logging.getLogger(__name__)


def _dir(data_dir: Path, account_id: str) -> Path:
    d = paper._root(data_dir, account_id) / "auto_rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return cn_now().isoformat()


def _new_id() -> str:
    return f"arule_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def validate_rule(rule: dict) -> None:
    """校验规则字段, 非法抛 ValueError (中文信息)。"""
    if (rule.get("name") or "").strip() == "":
        raise ValueError("规则名称不能为空")
    if rule.get("match_kind") not in ("strategy", "rule", "wind"):
        raise ValueError(f"match_kind 非法: {rule.get('match_kind')!r} (应为 strategy / rule / wind)")
    if not (rule.get("match_id") or "").strip():
        raise ValueError("match_id 不能为空")
    if rule.get("side") not in ("buy", "sell", "auto"):
        raise ValueError(f"side 非法: {rule.get('side')!r}")
    if rule.get("side") == "auto" and rule.get("match_kind") != "strategy":
        raise ValueError("side=auto (跟信号方向) 仅支持匹配策略 — 监控规则事件无方向语义")
    if rule.get("match_kind") == "wind":
        if rule.get("match_id") != "hrae":
            raise ValueError("wind 规则的 match_id 必须为 hrae")
        if rule.get("side") != "sell":
            raise ValueError("wind 规则只允许卖出")
    if rule.get("size_mode") not in ("fixed_amount", "pct_equity"):
        raise ValueError(f"size_mode 非法: {rule.get('size_mode')!r}")
    value = rule.get("size_value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("size_value 必须是正数")
    if rule.get("size_mode") == "pct_equity" and value > 100:
        raise ValueError("pct_equity 的 size_value 不能超过 100 (%)")
    if rule.get("order_type") not in ("market", "next_open", "close"):
        raise ValueError(f"order_type 非法: {rule.get('order_type')!r}")
    cooldown = rule.get("cooldown_days", 0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
        raise ValueError("cooldown_days 必须是非负整数")
    max_positions = rule.get("max_positions", 0)
    if isinstance(max_positions, bool) or not isinstance(max_positions, int) or max_positions < 0:
        raise ValueError("max_positions 必须是非负整数 (0 = 不限制)")
    # 风控字段 (可选): stop_loss_pct 负小数 (如 -0.07); max_hold_days 正整数。
    # 两者均缺省时回落到策略 STOP_LOSS / MAX_HOLD_DAYS (仅 match_kind=strategy)。
    sl = rule.get("stop_loss_pct")
    if sl is not None and (isinstance(sl, bool) or not isinstance(sl, (int, float))):
        raise ValueError("stop_loss_pct 必须是数值 (如 -0.07)")
    if sl is not None and sl > 0:
        raise ValueError("stop_loss_pct 应为负数或零 (如 -0.07)")
    mhd = rule.get("max_hold_days")
    if mhd is not None and (isinstance(mhd, bool) or not isinstance(mhd, int) or mhd < 1):
        raise ValueError("max_hold_days 必须是正整数")


def normalize_rule(rule: dict) -> dict:
    d = dict(rule)
    d["name"] = (d.get("name") or "").strip()
    d["match_id"] = (d.get("match_id") or "").strip()
    # side=auto: 跟随策略信号方向 (入场/进池 → 买入, 出场/移出 → 卖出)。
    # 存量规则缺 side 时按 buy 兼容 (原语义), 新建默认 auto。
    if d.get("match_kind") == "strategy":
        d.setdefault("side", "auto")
    else:
        d.setdefault("side", "buy")
    d.setdefault("size_mode", "fixed_amount")
    d.setdefault("order_type", "next_open")
    d.setdefault("cooldown_days", 5)
    d.setdefault("max_positions", 0)   # 0 = 不限制 (对齐回测 max_positions 语义)
    d.setdefault("enabled", True)
    d.setdefault("created_at", _now_iso())
    # 风控字段缺省 None (回落策略); 不做类型转换, 由 validate_rule 校验
    d.setdefault("stop_loss_pct", None)
    d.setdefault("max_hold_days", None)
    d.setdefault("exit_config", {})
    return d


def load_auto_rules(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID, enabled_only: bool = False) -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir(data_dir, account_id).glob("arule_*.json")):
        try:
            r = normalize_rule(json.loads(f.read_text(encoding="utf-8")))
            if enabled_only and not r.get("enabled"):
                continue
            out.append(r)
        except Exception as e:  # noqa: BLE001 - malformed user rule is skipped and logged
            logger.warning("paper auto rule load failed %s: %s", f.name, e)
    return out


def save_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    validate_rule(rule)
    atomic_write_text(_dir(data_dir, account_id) / f"{rule['id']}.json", json.dumps(rule, ensure_ascii=False, indent=2))
    return rule


def create_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    rule = normalize_rule({**rule, "id": _new_id()})
    validate_rule(rule)
    return save_auto_rule(data_dir, rule, account_id)


def delete_auto_rule(data_dir: Path, rule_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> bool:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def set_enabled(data_dir: Path, rule_id: str, enabled: bool, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if not p.exists():
        return None
    rule = normalize_rule(json.loads(p.read_text(encoding="utf-8")))
    rule["enabled"] = enabled
    return save_auto_rule(data_dir, rule, account_id)


def _matches(rule: dict, ev: dict) -> bool:
    if rule["match_kind"] == "strategy":
        return ev.get("source") == "strategy" and ev.get("strategy_id") == rule["match_id"]
    return ev.get("rule_id") == rule["match_id"]


def _in_cooldown(data_dir: Path, rule: dict, symbol: str, cooldown_days: int, account_id: str) -> bool:
    """同规则同 symbol 最近一次自动下单是否仍在冷却期 (按日历日, 含当日)。"""
    if cooldown_days <= 0:
        return False
    prefix = f"auto:{rule['id']}"
    today = cn_now().date()
    for order in paper.load_orders(data_dir, account_id):
        if order.get("source") != prefix or order.get("symbol") != symbol:
            continue
        try:
            created = _date.fromisoformat(order["created_at"][:10])
        except (KeyError, ValueError):
            continue
        if (today - created).days < cooldown_days:
            return True
    return False


def _sizing_qty(data_dir: Path, rule: dict, ref_price: float, account_id: str) -> int:
    if ref_price <= 0:
        return 0
    if rule["size_mode"] == "fixed_amount":
        amount = float(rule["size_value"])
    else:  # pct_equity: 按账户总权益 (现金 + 最新定版持仓市值)
        acc = paper.get_account(data_dir, account_id)
        if acc is None:
            return 0
        nav_rows = paper.load_nav(data_dir, account_id)
        equity = nav_rows[-1]["nav"] if nav_rows else float(acc["cash"])
        amount = equity * float(rule["size_value"]) / 100.0
    return paper.qty_from_amount(amount, ref_price)


def _position_slots_used(data_dir: Path, account_id: str) -> set[str]:
    """已占用的持仓槽: 当前持仓标的 + 待成交买单标的 (去重)。
    模拟盘 next_open 单未成交也占槽, 防同日多信号重复开仓超限。"""
    used: set[str] = set()
    for symbol, p in paper.load_positions(data_dir, account_id).items():
        if p["qty"] > 0:
            used.add(symbol)
    for o in paper.load_orders(data_dir, account_id):
        if o.get("status") == "pending" and o["side"] == "buy":
            used.add(o["symbol"])
    return used


_AUTO_BUY_TYPES = {"buy_signal", "pool_entry"}


def _resolve_side(rule: dict, ev: dict) -> str | None:
    """规则方向 → 本次事件实际方向。side=auto (跟策略信号): 入场/进池买,
    出场/移出卖; 其余按规则固定方向。返回 None 表示该事件不适用 (如 auto
    收到无方向语义的事件类型)。"""
    side = rule.get("side", "buy")
    if side != "auto":
        return side
    etype = str(ev.get("type") or "")
    if etype in _AUTO_BUY_TYPES:
        return "buy"
    if etype in ("sell_signal", "pool_exit"):
        return "sell"
    return None


def _held_qty(data_dir: Path, symbol: str, account_id: str) -> int:
    p = paper.load_positions(data_dir, account_id).get(symbol)
    return int(p["qty"]) if p else 0


def on_rule_events(data_dir: Path, events: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """监控事件 → 自动下单。返回本次创建的订单列表 (被拒订单只记日志)。

    触发条件: 事件带 symbol 与价格、有规则匹配、未冷却; 下单复用 paper.create_order
    (冻结/资金/T+1/涨跌停等校验自动生效), ref_price 用事件价格 (当前快照价)。
    买入信号受 max_positions 约束 (对齐回测口径): 已持仓 + 待成交买单的标的数
    达到上限后, 新标的开仓被跳过; 已持仓标的加仓与卖出信号不受限。
    """
    created: list[dict] = []
    if not events:
        return created
    rules = load_auto_rules(data_dir, account_id, enabled_only=True)
    if not rules:
        return created
    with paper.PAPER_LOCK:
        # 同一批事件内复用快照, 由 create_order 后的持仓变化反映 (循环内重取)
        for ev in events:
            symbol = (ev.get("symbol") or "").strip()
            price = ev.get("price")
            if not symbol or price is None or price <= 0:
                continue
            for rule in rules:
                if not _matches(rule, ev):
                    continue
                side = _resolve_side(rule, ev)
                if side is None:
                    continue
                if _in_cooldown(data_dir, rule, symbol, int(rule.get("cooldown_days", 0)), account_id):
                    continue
                if side == "buy":
                    max_positions = int(rule.get("max_positions", 0))
                    if max_positions > 0:
                        used = _position_slots_used(data_dir, account_id)
                        if len(used) >= max_positions and symbol not in used:
                            logger.info(
                                "paper auto %s: %s 跳过开仓 — 已达最大持仓 %d (%s)",
                                rule["name"], symbol, max_positions, ", ".join(sorted(used)) or "空",
                            )
                            continue
                    qty = _sizing_qty(data_dir, rule, float(price), account_id)
                else:
                    # auto 的出场卖出 = 全平该标的 (策略出场语义); 固定 sell 规则仍按 sizing。
                    if rule.get("side") == "auto":
                        qty = _held_qty(data_dir, symbol, account_id)
                        if qty <= 0:
                            continue  # 未持仓, 出场信号无意义
                    else:
                        qty = _sizing_qty(data_dir, rule, float(price), account_id)
                        # 金额换算的股数与实际持仓对不上: 以可卖余量封顶, 金额设足够大即清仓
                        held = paper.sell_capacity(data_dir, symbol, account_id)
                        if held is None:
                            continue
                        available, pending_sell = held
                        qty = min(qty, paper.normalize_qty(available - pending_sell))
                if qty <= 0:
                    logger.info("paper auto %s: %s 金额不足以一手 (价 %s)", rule["name"], symbol, price)
                    continue
                order, err = paper.create_order(
                    data_dir, symbol, side,
                    account_id=account_id,
                    qty=qty,
                    order_type=rule["order_type"],
                    ref_price=float(price),
                    source=f"auto:{rule['id']}",
                )
                if err:
                    logger.info("paper auto %s: %s 下单被拒: %s", rule["name"], symbol, err)
                    continue
                created.append(order)
                logger.info("paper auto %s: %s 触发 %s %d 股 (%s)", rule["name"], symbol, side, qty, order["id"])
    return created


# ── HRAE 实时风向退出 ─────────────────────────────────────────

def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _state_path(data_dir: Path, account_id: str, rule_id: str) -> Path:
    return _dir(data_dir, account_id) / f"hrae_{rule_id}.json"


def _load_hrae_state(data_dir: Path, account_id: str, rule_id: str) -> dict:
    p = _state_path(data_dir, account_id, rule_id)
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_hrae_state(data_dir: Path, account_id: str, rule_id: str, state: dict) -> None:
    atomic_write_text(_state_path(data_dir, account_id, rule_id), json.dumps(state, ensure_ascii=False, indent=2))


def _row_float(row: dict, key: str) -> float | None:
    value = row.get(key)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _market_score(rows: list[dict]) -> float | None:
    """实时市场分: breadth + EMA20/60 趋势, 不混用指数百分数口径。"""
    valid = [r for r in rows if _row_float(r, "raw_close") and _row_float(r, "change_pct") is not None]
    if not valid:
        return None
    breadth = sum(1 if (_row_float(r, "change_pct") or 0) > 0 else -1 if (_row_float(r, "change_pct") or 0) < 0 else 0 for r in valid) / len(valid)
    trend_values = []
    for r in valid:
        close, ema20, ema60 = (_row_float(r, k) for k in ("close", "ema20", "ema60"))
        if close is not None and ema20 is not None and ema60 is not None and close > 0:
            trend_values.append((1 if close >= ema20 else -1) * 0.6 + (1 if close >= ema60 else -1) * 0.4)
    trend = sum(trend_values) / len(trend_values) if trend_values else 0.0
    return _clamp(0.6 * breadth + 0.4 * trend)


def _stock_score(row: dict) -> float | None:
    close = _row_float(row, "close") or _row_float(row, "raw_close")
    if close is None or close <= 0:
        return None
    parts: list[tuple[float, float]] = []
    for field, weight in (("ema20", 0.30), ("ema60", 0.25)):
        value = _row_float(row, field)
        if value is not None and value > 0:
            parts.append((1.0 if close >= value else -1.0, weight))
    for field, weight, scale in (("momentum_5d", 0.20, 0.10), ("momentum_20d", 0.15, 0.20), ("change_pct", 0.10, 0.05)):
        value = _row_float(row, field)
        if value is not None:
            parts.append((_clamp(value / scale), weight))
    if not parts:
        return None
    return _clamp(sum(score * weight for score, weight in parts) / sum(weight for _, weight in parts))


def _risk_multiplier(score: float, *, account: bool = False) -> float:
    """HRAE-PBR 的分段乘数；阈值来自方案首轮候选，后续须样本外验证。"""
    if score > -0.30:
        return 1.0
    if score > -0.50:
        return 0.80 if account else 0.75
    if score > -0.70:
        return 0.55 if account else 0.40
    return 0.25 if account else 0.0


def _annualized_vol(row: dict) -> float:
    value = _row_float(row, "annual_vol_20d")
    if value is not None and value > 0:
        return max(0.01, min(value, 3.0))
    # 没有年化波动率时, 用 ATR/价格的保守年化近似; 不把缺失数据伪装成低风险。
    close = _row_float(row, "close") or _row_float(row, "raw_close")
    atr = _row_float(row, "atr_14")
    if close and close > 0 and atr and atr > 0:
        return max(0.01, min(atr / close * (252 ** 0.5), 3.0))
    return 0.50


def _portfolio_risk_snapshot(
    account: dict,
    positions: dict[str, dict],
    rows: list[dict],
    state: dict,
    cfg: dict,
    market_score: float,
) -> tuple[dict, dict[str, dict]]:
    """构造账户风险快照和持仓目标, 只计算不下单。

    当前 provider 契约没有稳定的行业收益矩阵, 因此不虚构行业/协方差数据;
    组合波动率采用带压力相关性的持仓波动率近似, 用于降仓而非加仓。
    """
    by_symbol = {str(r.get("symbol")): r for r in rows if r.get("symbol")}
    holdings: dict[str, dict] = {}
    market_value = 0.0
    for symbol, pos in positions.items():
        qty = int(pos.get("qty", 0))
        row = by_symbol.get(symbol)
        price = _row_float(row or {}, "raw_close") if row else None
        if qty <= 0:
            continue
        price = price if price and price > 0 else float(pos.get("avg_cost") or 0)
        value = qty * price
        score = _stock_score(row or {}) if row else None
        holdings[symbol] = {"value": value, "score": score if score is not None else 0.0, "vol": _annualized_vol(row or {})}
        market_value += value
    cash = float(account.get("cash") or 0.0)
    equity = cash + market_value
    initial = float(account.get("initial_cash") or 0.0)
    previous = state.get("__portfolio__") if isinstance(state.get("__portfolio__"), dict) else {}
    hwm = max(float(previous.get("high_water_mark") or 0.0), equity)
    drawdown = max(0.0, 1.0 - equity / hwm) if hwm > 0 else 0.0
    pnl = equity - initial

    base = min(1.0, max(0.0, float(cfg.get("base_max_exposure", 0.85))))
    target_vol = max(0.01, float(cfg.get("target_annual_volatility", 0.10)))
    draw_warn = max(0.0, float(cfg.get("drawdown_warning", 0.04)))
    draw_limit = max(draw_warn + 1e-9, float(cfg.get("drawdown_limit", 0.12)))
    lock_rate = min(1.0, max(0.0, float(cfg.get("profit_lock_rate", 0.50))))
    draw_floor = hwm * (1.0 - draw_limit)
    profit_floor = initial + lock_rate * max(hwm - initial, 0.0)
    capital_floor = max(draw_floor, profit_floor)
    cushion = max(equity - capital_floor, 0.0)
    current_exposure = market_value / equity if equity > 0 else 0.0

    weighted_score = 0.0
    weighted_vol = 0.0
    if market_value > 0:
        for item in holdings.values():
            weight = item["value"] / market_value
            weighted_score += weight * item["score"]
            weighted_vol += weight * item["vol"]
    account_score = _clamp(0.30 * market_score + 0.70 * weighted_score) if holdings else market_score
    # 压力相关性 rho=0.25: 只会压低目标暴露, 不会在低波动时放大仓位。
    variance = (1.0 - 0.25) * sum((item["value"] / equity * item["vol"]) ** 2 for item in holdings.values())
    variance += 0.25 * (sum(item["value"] / equity * item["vol"] for item in holdings.values()) ** 2)
    forecast_vol = variance ** 0.5 if variance > 0 else 0.0
    vol_mult = min(1.0, target_vol / forecast_vol) if forecast_vol > 0 else 1.0
    draw_mult = 1.0 if drawdown <= draw_warn else max(0.0, (draw_limit - drawdown) / (draw_limit - draw_warn))
    account_mult = _risk_multiplier(account_score, account=True)
    target_exposure = min(base, base * vol_mult, base * draw_mult, base * account_mult)
    if equity <= capital_floor or drawdown >= draw_limit:
        target_exposure = 0.0

    dominant = "BASE"
    constraints = {"BASE": base, "VOLATILITY": base * vol_mult, "DRAWDOWN": base * draw_mult, "HRAE": base * account_mult}
    if equity <= capital_floor or drawdown >= draw_limit:
        dominant = "CAPITAL_FLOOR"
    else:
        dominant = min(constraints, key=constraints.get)
    risk_state = "HALT" if target_exposure <= 0 and (equity <= capital_floor or drawdown >= draw_limit) else "REDUCE" if target_exposure + 1e-9 < current_exposure else "CAUTION" if dominant != "BASE" else "NORMAL"
    snapshot = {
        "version": "hrae-pbr-v1",
        "equity": round(equity, 2), "initial_equity": round(initial, 2),
        "cash": round(cash, 2), "market_value": round(market_value, 2),
        "cumulative_pnl": round(pnl, 2), "high_water_mark": round(hwm, 2),
        "drawdown": round(drawdown, 6), "drawdown_floor": round(draw_floor, 2),
        "profit_floor": round(profit_floor, 2), "capital_floor": round(capital_floor, 2),
        "risk_cushion": round(cushion, 2), "current_exposure": round(current_exposure, 6),
        "target_exposure": round(target_exposure, 6), "forecast_volatility": round(forecast_vol, 6),
        "account_hrae_score": round(account_score, 6), "dominant_constraint": dominant,
        "risk_state": risk_state, "updated_at": _now_iso(),
    }
    plan: dict[str, dict] = {}
    rebalance_threshold = max(0.0, float(cfg.get("rebalance_threshold", 0.05)))
    for symbol, item in holdings.items():
        score = float(item["score"])
        multiplier = _risk_multiplier(score)
        position_cap = equity * min(1.0, max(0.0, float(cfg.get("single_position_cap", 0.10)))) * multiplier
        target_value = min(item["value"], position_cap)
        if current_exposure > 0 and target_exposure < current_exposure:
            target_value = min(target_value, item["value"] * target_exposure / current_exposure)
        reduction = max(0.0, 1.0 - target_value / item["value"]) if item["value"] > 0 else 0.0
        if current_exposure - target_exposure < rebalance_threshold and score > -0.70 and equity > capital_floor:
            reduction = 0.0
        plan[symbol] = {"target_value": round(target_value, 2), "reduction_ratio": reduction, "score": score, "multiplier": multiplier}
    state["__portfolio__"] = snapshot
    return snapshot, plan


def _hrae_order(data_dir: Path, account_id: str, rule: dict, symbol: str, price: float, reason: str, ratio: float) -> dict | None:
    capacity = paper.sell_capacity(data_dir, symbol, account_id)
    if capacity is None:
        return None
    available, pending = capacity
    qty = paper.normalize_qty(int((available - pending) * ratio))
    if qty <= 0:
        return None
    order, err = paper.create_order(
        data_dir, symbol, "sell", account_id=account_id, qty=qty,
        order_type="market", ref_price=price, source=f"auto_hrae:{rule['id']}",
    )
    if err:
        logger.info("paper HRAE %s: %s 下单被拒: %s", rule["name"], symbol, err)
        return None
    order["reason"] = reason
    # create_order 已完成校验和落盘; 将确定性触发原因补写进同一订单审计记录。
    paper.save_order(data_dir, order, account_id)
    return order


def on_quote_snapshot(data_dir: Path, rows: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """用当前标准化快照评估 HRAE；只处理已持仓标的并统一走 paper.create_order。"""
    rules = [r for r in load_auto_rules(data_dir, account_id, enabled_only=True) if r.get("match_kind") == "wind"]
    if not rules or not rows:
        return []
    by_symbol = {str(r.get("symbol")): r for r in rows if r.get("symbol")}
    market = _market_score(rows)
    if market is None:
        logger.warning("paper HRAE DATA_STALE: 缺少有效市场快照")
        return []
    positions = paper.load_positions(data_dir, account_id)
    account = paper.get_account(data_dir, account_id)
    if account is None:
        return []
    created: list[dict] = []
    with paper.PAPER_LOCK:
        for rule in rules:
            state = _load_hrae_state(data_dir, account_id, rule["id"])
            cfg = rule.get("exit_config") or {}
            risk_snapshot, plan = _portfolio_risk_snapshot(
                account, positions, rows, state, cfg, market,
            )
            observe = float(cfg.get("observe_threshold", -0.30))
            reduce = float(cfg.get("reduce_threshold", -0.50))
            exit_threshold = float(cfg.get("exit_threshold", -0.70))
            confirm = max(1, int(cfg.get("consecutive_samples", 3)))
            max_loss = abs(float(cfg.get("max_loss_pct", 0.08)))
            atr_mult = float(cfg.get("atr_stop_atr", 2.5))
            for symbol, pos in positions.items():
                if int(pos.get("qty", 0)) <= 0 or symbol not in by_symbol:
                    continue
                row = by_symbol[symbol]
                price = _row_float(row, "raw_close")
                close = _row_float(row, "close") or price
                if price is None or close is None or price <= 0:
                    continue
                item = state.setdefault(symbol, {"peak": price, "weak": 0, "stage": 0})
                item["peak"] = max(float(item.get("peak", price)), price)
                # 当前实时快照尚未提供行业/事件字段: 只在可用的市场+个股权重上归一化,
                # 防止总分范围被锁在 [-0.7, 0.7] 而使 -0.70 强风险阈值失去语义。
                score = _clamp((0.30 * market + 0.40 * (_stock_score(row) or 0.0)) / 0.70)
                item["weak"] = int(item.get("weak", 0)) + 1 if score <= observe else 0
                reason = None
                ratio = 0.0
                portfolio_action = plan.get(symbol) or {}
                portfolio_ratio = float(portfolio_action.get("reduction_ratio") or 0.0)
                avg_cost = float(pos.get("avg_cost") or 0)
                atr = _row_float(row, "atr_14")
                stop = avg_cost * (1 - max_loss) if avg_cost > 0 else 0
                # atr_14 基于 enriched 价格; 换算到 raw_close 后再与原始成交价比较。
                if avg_cost > 0 and atr is not None and close > 0:
                    raw_atr = atr / close * price
                    stop = max(stop, avg_cost - atr_mult * raw_atr)
                if stop > 0 and price <= stop:
                    reason, ratio = f"HRAE硬止损 score={score:.3f}", 1.0
                elif score <= exit_threshold and item["weak"] >= confirm:
                    reason, ratio = f"HRAE强风险 score={score:.3f}", 1.0
                elif score <= reduce and item["weak"] >= confirm and int(item.get("stage", 0)) < 1:
                    reason, ratio = f"HRAE风向转弱 score={score:.3f}", 0.5
                if portfolio_ratio > ratio:
                    ratio = portfolio_ratio
                    reason = (
                        f"HRAE-PBR组合减仓 exposure={risk_snapshot['current_exposure']:.2%}"
                        f"→{risk_snapshot['target_exposure']:.2%}"
                        f" constraint={risk_snapshot['dominant_constraint']}"
                    )
                if reason:
                    order = _hrae_order(data_dir, account_id, rule, symbol, price, reason, ratio)
                    if order:
                        item["stage"] = 2 if ratio >= 1 else 1
                        order["reason"] = reason
                        created.append(order)
            _save_hrae_state(data_dir, account_id, rule["id"], state)
    return created


def get_hrae_risk_snapshot(
    data_dir: Path,
    rows: list[dict],
    account_id: str = paper.DEFAULT_ACCOUNT_ID,
) -> dict | None:
    """返回当前账户 HRAE-PBR 风险快照, 供 API/前端只读展示。"""
    rules = [r for r in load_auto_rules(data_dir, account_id, enabled_only=True) if r.get("match_kind") == "wind"]
    account = paper.get_account(data_dir, account_id)
    if not rules or account is None or not rows:
        return None
    market = _market_score(rows)
    if market is None:
        return None
    state = _load_hrae_state(data_dir, account_id, rules[0]["id"])
    snapshot, _ = _portfolio_risk_snapshot(
        account, paper.load_positions(data_dir, account_id), rows,
        state, rules[0].get("exit_config") or {}, market,
    )
    _save_hrae_state(data_dir, account_id, rules[0]["id"], state)
    return snapshot


_ORDER_TYPE_LABEL = {"market": "即时", "next_open": "次日开盘", "close": "当日收盘"}


def auto_order_events(created: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """自动跟单创建的订单 → 推送事件。与成交事件同构 (source=paper 对齐
    AlertEvent), 走同一推送通道; rule_id 从 order.source (auto:{rule_id}) 还原。
    """
    events: list[dict] = []
    for o in created:
        source = str(o.get("source", ""))
        rule_id = source.removeprefix("auto:") if source.startswith("auto:") else source.removeprefix("auto_hrae:") if source.startswith("auto_hrae:") else ""
        side_label = "买入" if o["side"] == "buy" else "卖出"
        type_label = _ORDER_TYPE_LABEL.get(o["order_type"], o["order_type"])
        events.append({
            "source": "paper",
            "type": "auto_order",
            "severity": "info",
            "ts": int(cn_now().timestamp() * 1000),
            "symbol": o["symbol"],
            "rule_id": rule_id,
            "side": o["side"],
            "qty": o["qty"],
            "account_id": account_id,
            "message": f"自动跟单触发{side_label}下单 ({type_label}) {o['qty']}股"
            + (f" · {o['reason']}" if o.get("reason") else ""),
        })
    return events


# ── 盘后风控平仓 (apply_risk_exits) ───────────────────────────


def _resolve_risk_params(rule: dict, strategy_engine) -> tuple[float | None, int | None]:
    """解析规则的风控参数: rule override 优先, 缺省回落策略值。
    返回 (stop_loss, max_hold_days); None 表示该项不启用。"""
    sl = rule.get("stop_loss_pct")
    mhd = rule.get("max_hold_days")
    if sl is None and mhd is None and rule.get("match_kind") == "strategy":
        s = strategy_engine.get(rule["match_id"])
        if s is not None:
            sl = sl if sl is not None else s.stop_loss
            mhd = mhd if mhd is not None else s.max_hold_days
    if sl is not None:
        sl = float(sl)
    if mhd is not None:
        mhd = int(mhd)
    return sl, mhd


def _count_trading_days(data_dir: Path, symbol: str, buy_date: str, end_day: str) -> int:
    """统计 (buy_date, end_day] 区间内 symbol 的交易日数。

    扫描 kline_daily 分区目录名 (Hive style `date=YYYY-MM-DD`)
    —— 与是否复权/asset_type 无关, 只要该日有 K 落盘即算交易日。
    不读 parquet 内容, 仅按目录名判断, 快且不加载。
    """
    daily_root = data_dir / "kline_daily"
    if not daily_root.is_dir():
        return 0
    count = 0
    for p in daily_root.iterdir():
        if not p.is_dir():
            continue
        name = p.name
        if not name.startswith("date="):
            continue
        d = name.removeprefix("date=")
        if buy_date < d <= end_day:
            count += 1
    return count


def apply_risk_exits(data_dir: Path, day: str, account_id: str, strategy_engine) -> list[dict]:
    """盘后风控: 按 auto_rule 的 stop_loss/max_hold_days 对 auto 仓位生成 close 卖单。

    只处理 source 以 'auto:' 开头的仓位。在 settle_day 撮合循环之前调用;
    生成的 close 卖单会被同一次 settle_day 的既有撮合按 raw_close 成交,
    复用全部费用/涨跌停/T+1/资金校验。返回生成的订单列表。

    幂等: 已平仓的持仓 qty<=0 不再触发; qty>0 但当日已生成的 close 卖单
    会占可卖额度 (create_order 的 pending_sell 检查), 不会重复生成超额。
    """
    created: list[dict] = []
    positions = paper.load_positions(data_dir, account_id)
    if not positions:
        return created
    rules = load_auto_rules(data_dir, account_id, enabled_only=True)
    if not rules:
        return created
    # 持仓 → 最近一笔 auto 买入 order → rule_id
    orders = paper.load_orders(data_dir, account_id)
    auto_buy_by_symbol: dict[str, dict] = {}
    for o in orders:
        if o.get("side") != "buy" or o.get("status") != "filled":
            continue
        src = str(o.get("source", ""))
        if not src.startswith("auto:"):
            continue
        sym = o.get("symbol")
        if not sym:
            continue
        prev = auto_buy_by_symbol.get(sym)
        if prev is None or str(prev.get("created_at", "")) < str(o.get("created_at", "")):
            auto_buy_by_symbol[sym] = o
    if not auto_buy_by_symbol:
        return created
    rules_by_id = {r["id"]: r for r in rules if r.get("id")}
    for symbol, pos in positions.items():
        if pos["qty"] <= 0:
            continue
        order = auto_buy_by_symbol.get(symbol)
        if order is None:
            continue  # manual 仓位不归 auto 规则管
        rule_id = str(order.get("source", "")).removeprefix("auto:")
        rule = rules_by_id.get(rule_id)
        if rule is None:
            continue  # 规则已删除
        try:
            sl, mhd = _resolve_risk_params(rule, strategy_engine)
        except (ValueError, AttributeError, TypeError) as e:
            logger.warning("paper risk resolve failed %s: %s", symbol, e)
            continue
        if sl is None and mhd is None:
            continue  # 无风控约束
        bar = paper.read_daily_bar(data_dir, symbol, pos.get("asset_type", "stock"), day) or {}
        close = bar.get("close")
        trigger_reason = None
        if sl is not None and sl < 0 and close is not None and float(pos["avg_cost"]) > 0:
            pnl = (float(close) - float(pos["avg_cost"])) / float(pos["avg_cost"])
            if pnl <= -abs(sl):
                trigger_reason = f"止损 (pnl={pnl:.2%}, 阈值 {sl:.2%})"
        if trigger_reason is None and mhd is not None:
            buy_date = max(lot["date"] for lot in pos["lots"] if lot.get("date"))
            trading_days = _count_trading_days(data_dir, symbol, buy_date, day)
            if trading_days >= mhd:
                trigger_reason = f"到期 (持有 {trading_days} 交易日, 阈值 {mhd})"
        if trigger_reason is None:
            continue
        available = paper._available_of(pos, day)
        if available <= 0:
            logger.info("paper risk %s: %s 触发%s 但当日买入无可卖, 跳过", rule["name"], symbol, trigger_reason)
            continue
        o, err = paper.create_order(
            data_dir, symbol, "sell",
            account_id=account_id,
            qty=available,
            order_type="close",
            ref_price=float(pos["avg_cost"]),
            source=f"auto_risk:{rule_id}",
        )
        if err:
            logger.info("paper risk %s: %s 风控平仓被拒: %s", rule["name"], symbol, err)
            continue
        created.append(o)
        logger.info("paper risk %s: %s 触发%s 平仓 %d 股", rule["name"], symbol, trigger_reason, available)
    return created
