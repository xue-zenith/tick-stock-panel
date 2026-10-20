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
    if rule.get("match_kind") not in ("strategy", "rule"):
        raise ValueError(f"match_kind 非法: {rule.get('match_kind')!r} (应为 strategy / rule)")
    if not (rule.get("match_id") or "").strip():
        raise ValueError("match_id 不能为空")
    if rule.get("side") not in ("buy", "sell", "auto"):
        raise ValueError(f"side 非法: {rule.get('side')!r}")
    if rule.get("side") == "auto" and rule.get("match_kind") != "strategy":
        raise ValueError("side=auto (跟信号方向) 仅支持匹配策略 — 监控规则事件无方向语义")
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
    return d


def load_auto_rules(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID, enabled_only: bool = False) -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir(data_dir, account_id).glob("arule_*.json")):
        try:
            r = normalize_rule(json.loads(f.read_text(encoding="utf-8")))
            if enabled_only and not r.get("enabled"):
                continue
            out.append(r)
        except Exception as e:
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


_ORDER_TYPE_LABEL = {"market": "即时", "next_open": "次日开盘", "close": "当日收盘"}


def auto_order_events(created: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """自动跟单创建的订单 → 推送事件。与成交事件同构 (source=paper 对齐
    AlertEvent), 走同一推送通道; rule_id 从 order.source (auto:{rule_id}) 还原。
    """
    events: list[dict] = []
    for o in created:
        source = str(o.get("source", ""))
        rule_id = source.removeprefix("auto:") if source.startswith("auto:") else ""
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
            "message": f"自动跟单触发{side_label}下单 ({type_label}) {o['qty']}股",
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
