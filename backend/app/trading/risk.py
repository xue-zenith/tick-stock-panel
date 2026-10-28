"""确定性风控引擎 — 所有订单必须经此, AI/策略不能自己实现最终风控。

设计原则:
  - 纯函数: 输入意图 + RiskContext, 输出 RiskDecision; 无 I/O (行情/持仓快照
    通过 ctx 传入), 可单元测试, 可重放。
  - 版本化: ruleset_version 写入 intent + decision, 审计可追溯。
  - fail-closed: 任一规则不确定 (数据缺失) → REJECT, 不放行。

规则分四类 (镜像 paper.py 的纯函数口径):
  - 账户级: cash_sufficiency / max_exposure / single_position_pct / day_loss_limit
  - 标的级: lot_size_multiple (百股整手) / t1_available (可卖数量) / limit_price_bounds
  - 市场级: trading_session_guard (交易时段) / not_limit_up_buy / not_limit_down_sell
  - 系统级: mode_is_armed (DISARMED/HALTED 拒单) / autonomy_scope_ok (AI 来源授权校验)

账户/标的/市场级规则参考 paper.py:limit_prices / normalize_qty / _available_of.
真实券商接入后, can_use_quantity 和涨跌停以券商主数据为准, 本引擎只做预校验。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.trading.contracts import (
    RiskVerdict,
    Side,
    SourceType,
    TradeIntent,
    TradingMode,
)

# 风控版本: 随规则升级提升; intent 与 decision 都打此版本审计。
RULESET_VERSION = "risk-v1"


@dataclass(frozen=True)
class RiskContext:
    """风控上下文 — 所有快照通过此结构注入, 规则函数无 I/O。

    缺失字段视为“无法验证” → fail-closed (相关规则 REJECT)。
    """

    account_id: str
    mode: TradingMode
    cash: float = 0.0
    total: float = 0.0
    # 标的当前行情 (None 表示行情不可用)
    last_price: float | None = None
    prev_close: float | None = None          # 昨收 (涨跌停判定价)
    limit_up_price: float | None = None      # 涨停价 (券商口径为准)
    limit_down_price: float | None = None     # 跌停价
    # 标的当前持仓 (券商口径可卖)
    position_qty: int = 0
    available_qty: int = 0
    # 风控参数 (可由 config 覆盖)
    max_order_notional: float = 50_000.0      # 单笔最大金额
    max_exposure_pct: float = 0.70           # 最大总仓位比例 (占总资产)
    single_position_pct: float = 0.05        # 单标的最大仓位比例
    daily_loss_limit_pct: float = 0.03        # 单日回撤熔断
    # AI 授权信息
    autonomy_level: int = 0                     # 当前策略取得的最大授权级别 (0..3)
    # 资产类型 (stock/etf; 影响涨跌停比例)
    asset_type: str = "stock"


@dataclass(frozen=True)
class CheckResult:
    rule: str
    verdict: RiskVerdict
    reason: str = ""


def evaluate(intent: TradeIntent, ctx: RiskContext) -> tuple[str, tuple[str, ...]]:
    """对意图运行全部规则 → (verdict, reasons)。

    PASS 当且仅当所有规则 PASS; 任一 REJECT 即整体 REJECT。
    reasons 列出所有 REJECT 规则的原因 (供 UI 与审计展示)。
    """
    checks: list[CheckResult] = [
        _check_positive_qty(intent, ctx),
        _check_mode_armed(intent, ctx),
        _check_autonomy_scope(intent, ctx),
        _check_lot_size(intent, ctx),
        _check_cash_sufficiency(intent, ctx),
        _check_max_order_notional(intent, ctx),
        _check_single_position_pct(intent, ctx),
        _check_max_exposure(intent, ctx),
        _check_t1_available(intent, ctx),
        _check_limit_bounds(intent, ctx),
        _check_trading_session(intent, ctx),
    ]
    rejected = tuple(c.reason for c in checks if c.verdict == RiskVerdict.REJECT)
    verdict = "pass" if not rejected else "reject"
    return verdict, rejected


# ── 系统级 ───────────────────────────────────────────────
def _check_positive_qty(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    del ctx
    if intent.qty <= 0:
        return CheckResult("positive_qty", RiskVerdict.REJECT, "委托数量必须大于 0")
    if intent.limit_price is not None and intent.limit_price <= 0:
        return CheckResult("positive_price", RiskVerdict.REJECT, "委托价格必须大于 0")
    if intent.price_policy == "limit" and intent.limit_price is None:
        return CheckResult("limit_price_required", RiskVerdict.REJECT, "限价单缺少限价")
    return CheckResult("positive_qty", RiskVerdict.PASS)


def _check_mode_armed(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    """DISARMED/HALTED 拒单; DEGRADED 仅允许 SELL (减仓)."""
    if ctx.mode in (TradingMode.DISARMED, TradingMode.HALTED):
        return CheckResult("mode_armed", RiskVerdict.REJECT, f"账户模式 {ctx.mode.value}, 不允许新单")
    if ctx.mode == TradingMode.DEGRADED and intent.side == Side.BUY:
        return CheckResult("mode_armed", RiskVerdict.REJECT, "DEGRADED 状态禁止新增风险 (买入)")
    return CheckResult("mode_armed", RiskVerdict.PASS)


def _check_autonomy_scope(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    """AI 来源必须匹配授权级别。本版只允许 MANUAL + AI_L1."""
    if intent.source == SourceType.MANUAL:
        return CheckResult("autonomy_scope", RiskVerdict.PASS)
    if intent.source == SourceType.AI_L1:
        if ctx.autonomy_level >= 1:
            return CheckResult("autonomy_scope", RiskVerdict.PASS)
        return CheckResult("autonomy_scope", RiskVerdict.REJECT, "AI_L1 意图缺少有效授权租约")
    # AUTO_L2/L3 本版一律拒绝 (接口预留)
    return CheckResult(
        "autonomy_scope", RiskVerdict.REJECT,
        f"{intent.source.value} 本版未启用",
    )


# ── 标的级 ───────────────────────────────────────────────
def _check_lot_size(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    """数量必须为 100 股整数倍 (镜像 paper.py:LOT_SIZE=100)."""
    if intent.qty % 100 != 0:
        return CheckResult("lot_size", RiskVerdict.REJECT, f"数量 {intent.qty} 不是 100 整数倍")
    return CheckResult("lot_size", RiskVerdict.PASS)


def _check_cash_sufficiency(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    """买单检查可用现金 (含估算佣金). 卖出不耗现金."""
    if intent.side == Side.SELL:
        return CheckResult("cash_sufficiency", RiskVerdict.PASS)
    price = intent.limit_price
    if price is None:
        if ctx.last_price is None:
            return CheckResult("cash_sufficiency", RiskVerdict.REJECT, "市价单无 last_price 估算金额")
        price = ctx.last_price
    notional = price * intent.qty
    fee = max(notional * 0.00025, 5.0)  # 与 paper.py 佣金口径一致
    if notional + fee > ctx.cash:
        return CheckResult(
            "cash_sufficiency", RiskVerdict.REJECT,
            f"需 {notional + fee:.2f} (含费 {fee:.2f}), 可用 {ctx.cash:.2f}",
        )
    return CheckResult("cash_sufficiency", RiskVerdict.PASS)


def _check_max_order_notional(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    price = intent.limit_price or ctx.last_price
    if price is None:
        return CheckResult("max_order_notional", RiskVerdict.REJECT, "无价格, 无法校验单笔上限")
    notional = price * intent.qty
    if notional > ctx.max_order_notional:
        return CheckResult(
            "max_order_notional", RiskVerdict.REJECT,
            f"单笔金额 {notional:.2f} > 上限 {ctx.max_order_notional:.2f}",
        )
    return CheckResult("max_order_notional", RiskVerdict.PASS)


def _check_single_position_pct(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    """单标的占总资产比例上限. 用现持仓 + 本单金额估算."""
    if ctx.total <= 0:
        return CheckResult("single_position_pct", RiskVerdict.PASS)  # 无对账, 跳过
    if intent.side == Side.SELL:
        return CheckResult("single_position_pct", RiskVerdict.PASS)
    price = intent.limit_price or ctx.last_price or 0.0
    notional = price * intent.qty
    existing_mv = (ctx.last_price or 0.0) * ctx.position_qty
    pct_after = (existing_mv + notional) / ctx.total
    if pct_after > ctx.single_position_pct:
        return CheckResult(
            "single_position_pct", RiskVerdict.REJECT,
            f"单标的占比 {pct_after:.1%} > 上限 {ctx.single_position_pct:.1%}",
        )
    return CheckResult("single_position_pct", RiskVerdict.PASS)


def _check_max_exposure(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    """总仓位比例上限 (买入后)."""
    if ctx.total <= 0 or intent.side == Side.SELL:
        return CheckResult("max_exposure", RiskVerdict.PASS)
    price = intent.limit_price or ctx.last_price or 0.0
    notional = price * intent.qty
    cash_after = ctx.cash - notional
    exposure_after = 1 - cash_after / ctx.total
    if exposure_after > ctx.max_exposure_pct:
        return CheckResult(
            "max_exposure", RiskVerdict.REJECT,
            f"总仓位 {exposure_after:.1%} > 上限 {ctx.max_exposure_pct:.1%}",
        )
    return CheckResult("max_exposure", RiskVerdict.PASS)


def _check_t1_available(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    """卖出数量 ≤ 券商可卖数量 (T+1, 券商口径)."""
    if intent.side == Side.SELL and intent.qty > ctx.available_qty:
        return CheckResult(
            "t1_available", RiskVerdict.REJECT,
            f"卖出 {intent.qty} > 可卖 {ctx.available_qty}",
        )
    return CheckResult("t1_available", RiskVerdict.PASS)


def _check_limit_bounds(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    """限价单价格在涨跌停区间内 (用券商口径 limit_*_price; None 跳过).

    与 paper.py 不同处: 实盘禁止把日线收盘价当盘中可成交价 —
    限价单必须显式 limit_price 且在区间内。
    """
    if intent.price_policy not in ("limit", "market"):
        return CheckResult("limit_bounds", RiskVerdict.PASS)  # NEXT_OPEN / CLOSE 盘后
    if (
        intent.side == Side.BUY
        and ctx.limit_up_price is not None
        and intent.limit_price is not None
        and intent.limit_price > ctx.limit_up_price
    ):
        return CheckResult("limit_bounds", RiskVerdict.REJECT, "买价超过涨停价")
    if (
        intent.side == Side.SELL
        and ctx.limit_down_price is not None
        and intent.limit_price is not None
        and intent.limit_price < ctx.limit_down_price
    ):
        return CheckResult("limit_bounds", RiskVerdict.REJECT, "卖价低于跌停价")
    return CheckResult("limit_bounds", RiskVerdict.PASS)


def _check_trading_session(intent: TradeIntent, ctx: RiskContext) -> CheckResult:
    """限价/市价单必须在连续竞价时段 (镜像 market_time.in_continuous_session).

    NEXT_OPEN / CLOSE 单不在此约束内 (盘后管道).
    """
    if intent.price_policy not in ("limit", "market"):
        return CheckResult("trading_session", RiskVerdict.PASS)
    from app.market_time import in_continuous_session
    if not in_continuous_session():
        return CheckResult(
            "trading_session", RiskVerdict.REJECT,
            "当前非连续竞价时段 (北京时间 9:30-11:30 / 13:00-15:00)",
        )
    return CheckResult("trading_session", RiskVerdict.PASS)


# ── 配置 ───────────────────────────────────────────────
@dataclass
class RiskConfig:
    """风控参数 (持久化到配置或 AutonomyGrant)。本版用默认值."""

    max_order_notional: float = 50_000.0
    max_exposure_pct: float = 0.70
    single_position_pct: float = 0.05
    daily_loss_limit_pct: float = 0.03
    min_commission: float = 5.0
    commission_pct: float = 0.00025


_DEFAULT = RiskConfig()


def default_config() -> RiskConfig:
    return _DEFAULT


def build_ctx(
    *,
    account_id: str,
    mode: TradingMode,
    cash: float,
    total: float,
    last_price: float | None,
    prev_close: float | None,
    limit_up_price: float | None,
    limit_down_price: float | None,
    position_qty: int,
    available_qty: int,
    asset_type: str = "stock",
    autonomy_level: int = 0,
    cfg: RiskConfig | None = None,
) -> RiskContext:
    """从各快照组装 RiskContext。缺失字段填 None/0 (规则按 fail-closed 处理)."""
    c = cfg or _DEFAULT
    return RiskContext(
        account_id=account_id,
        mode=mode,
        cash=cash,
        total=total,
        last_price=last_price,
        prev_close=prev_close,
        limit_up_price=limit_up_price,
        limit_down_price=limit_down_price,
        position_qty=position_qty,
        available_qty=available_qty,
        max_order_notional=c.max_order_notional,
        max_exposure_pct=c.max_exposure_pct,
        single_position_pct=c.single_position_pct,
        daily_loss_limit_pct=c.daily_loss_limit_pct,
        autonomy_level=autonomy_level,
        asset_type=asset_type,
    )
