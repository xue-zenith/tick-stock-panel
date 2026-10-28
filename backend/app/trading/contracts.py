"""实盘交易域契约 — 不可变值对象与状态枚举。

实盘与模拟盘 (paper.py) 严格隔离:
  - 模拟盘: 本地 fills.jsonl 台账 + 简化 pending/filled 状态机 + 单进程 RLock。
  - 实盘: 券商回报为事实源, 本地 SQLite 保存意图/审批/风控/回报副本/审计;
    订单状态机覆盖部分成交、撤单竞态、UNKNOWN 对账等真实场景。

本模块只定义数据结构, 不含行为 (行为在 state_machine / oms / risk 等)。
所有 dataclass(frozen=True), 镜像 app/extensions/registry.py:20 的值对象风格,
避免运行中被意外修改破坏状态机不变量。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


# ── 枚举 ────────────────────────────────────────────────
class OrderState(StrEnum):
    """订单状态机 (实盘完整版, 见 state_machine.py 合法转换表)。

    UNKNOWN 是 fail-closed 状态: 网络超时或断线后无法确认券商是否接收,
    必须先对账, 禁止盲目重发同笔订单。
    """

    CREATED = "created"                       # 已创建意图, 等待风控
    RISK_PASSED = "risk_passed"               # 风控通过, 等待审批
    APPROVED = "approved"                     # 已审批, 等待提交券商
    SUBMITTED = "submitted"                   # 已发往券商, 等待回报
    PARTIALLY_FILLED = "partially_filled"     # 部分成交
    FILLED = "filled"                         # 全部成交 (终态)
    CANCELLED = "cancelled"                   # 已撤单 (终态)
    REJECTED = "rejected"                     # 已拒单 (终态)
    EXPIRED = "expired"                       # 已过期 (终态)
    UNKNOWN = "unknown"                       # 状态未知, 需对账


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PricePolicy(StrEnum):
    """价格策略。默认推荐 PROTECTED_LIMIT (带保护价格的限价单), 不用裸市价单。"""

    MARKET = "market"             # 市价 (实盘慎用, 需券商支持)
    LIMIT = "limit"               # 限价
    NEXT_OPEN = "next_open"       # 次日开盘价 (盘后管道, 与 paper order_type 对齐)
    CLOSE = "close"               # 当日收盘价 (盘后管道)


class TimeInForce(StrEnum):
    DAY = "day"     # 当日有效
    GTC = "gtc"     # 撤销前有效
    IOC = "ioc"     # 立即成交剩余撤销
    FOK = "fok"     # 全部成交或立即撤销


class SourceType(StrEnum):
    """订单来源。本版只产生 MANUAL 与 AI_L1; AUTO_L2/L3 接口预留但不激活。"""

    MANUAL = "manual"             # 人工下单
    AI_L1 = "ai_l1"               # AI 建议 (待人工审批, 不自动执行)
    AUTO_L2 = "auto_l2"           # AI 监督执行 (本版不激活)
    AUTO_L3 = "auto_l3"           # AI 有界自治 (本版不激活)


class TradingMode(StrEnum):
    """实盘运行模式 (fail-closed: 启动默认 DISARMED, 需人工 + 干净对账才能 ARMED)。

    本版 arm 只允许 DISARMED → ARMED_MANUAL; ARMED_AUTO 接口存在但不可达。
    """

    DISARMED = "disarmed"             # 只读, 不允许发送新单
    ARMED_MANUAL = "armed_manual"     # 允许人工审批后的订单
    ARMED_AUTO = "armed_auto"         # 允许自动授权订单 (本版不可达)
    DEGRADED = "degraded"             # 连接或对账异常, 只读或仅受控减仓
    HALTED = "halted"                 # 风控熔断, 停止新增风险


class RiskVerdict(StrEnum):
    PASS = "pass"
    REJECT = "reject"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ── 值对象 ──────────────────────────────────────────────
@dataclass(frozen=True)
class TradeIntent:
    """交易意图 — 人工/策略/AI 指令的统一归一化形态。

    所有下单入口先生成 TradeIntent, 经风控与审批后再由 OMS 提交券商。
    AI 只能产出此结构, 不能直接调用券商报单函数。
    """

    intent_id: str
    account_id: str
    strategy_id: str | None              # 来源策略/规则 ID; 人工单为 None
    symbol: str
    side: Side
    qty: int
    price_policy: PricePolicy
    limit_price: float | None            # LIMIT/NEXT_OPEN/CLOSE 参考价; MARKET 为 None
    tif: TimeInForce
    source: SourceType
    dedupe_key: str                       # 幂等键: 同一来源同一意图在有效期内最多产生一个实盘订单
    proposed_at: str                      # ISO 北京时间
    valid_until: str | None               # 意图失效时间; 超期由 OMS 标 EXPIRED
    ruleset_version: str                  # 风控版本 (写入时打版本, 审计可追溯)
    rationale: str                        # 下单理由 (AI/策略/人工各填)
    status: str = "pending"               # pending|approved|rejected|expired|submitted


@dataclass(frozen=True)
class RiskDecision:
    """风控结果 (不可变, 落库审计)。verdict=REJECT 时 reasons 非空。"""

    decision_id: str
    intent_id: str
    verdict: RiskVerdict
    ruleset_version: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    checked_at: str = ""


@dataclass(frozen=True)
class BrokerOrder:
    """券商订单 — 本地 OMS 视角。broker_order_id 在券商回报后填充。"""

    order_id: str                          # 本地 OMS 订单 ID
    broker_order_id: str | None            # 券商订单号 (回报后填充; UNKNOWN 时仍为 None)
    account_id: str
    intent_id: str
    symbol: str
    side: Side
    qty: int
    price_policy: PricePolicy
    limit_price: float | None
    state: OrderState
    filled_qty: int = 0
    avg_fill_price: float | None = None
    submitted_at: str | None = None
    last_update_at: str = ""
    reject_reason: str | None = None


@dataclass(frozen=True)
class Execution:
    """单笔成交回报。一个订单可能由多笔成交组成 (部分成交场景)。

    dedupe_key 用于回调幂等: 同一 broker_trade_id 最多入账一次。
    """

    exec_id: str                          # 本地成交 ID
    order_id: str                          # 本地 OMS 订单 ID
    broker_order_id: str | None
    broker_trade_id: str                   # 券商成交编号 (幂等键来源)
    account_id: str
    symbol: str
    side: Side
    qty: int
    price: float
    fee: float
    ts: str                                # ISO 北京时间
    dedupe_key: str                        # (broker, account, broker_trade_id) 派生


@dataclass(frozen=True)
class Approval:
    """人工审批记录 — 实盘授权凭证 (持久化, 不依赖进程内存)。

    与 assistant/actions.py 的 120 秒内存确认卡不同: 实盘审批绑定不可变订单快照,
    审批后应用重启仍可恢复; 超期/已决策不可二次审批。
    """

    approval_id: str
    intent_id: str
    approver: str | None = None
    decision: ApprovalDecision = ApprovalDecision.PENDING
    decided_at: str | None = None
    expires_at: str = ""
    order_snapshot: str = ""               # 审批时订单快照 JSON (含价格/数量/风控结果)


@dataclass(frozen=True)
class AccountSnapshot:
    """券商账户资金快照 (定期从券商拉取)。券商为事实源, 本地只保存副本。"""

    account_id: str
    broker: str
    cash: float                            # 可用资金
    frozen_cash: float = 0.0              # 冻结资金
    market_value: float = 0.0            # 持仓市值
    total: float = 0.0                    # 总资产
    nav: float = 0.0                      # 净值 (= total)
    mode: TradingMode = TradingMode.DISARMED
    as_of: str = ""


@dataclass(frozen=True)
class PositionSnapshot:
    """券商持仓快照 (单标的)。可卖数量以券商 can_use_volume 为准, 不本地推导。"""

    account_id: str
    symbol: str
    qty: int = 0                           # 总持仓
    available_qty: int = 0                # 可卖 (T+1, 券商口径)
    avg_cost: float = 0.0
    last_price: float | None = None
    market_value: float = 0.0
    pnl: float = 0.0
    as_of: str = ""


@dataclass(frozen=True)
class AutonomyGrant:
    """AI 自动交易授权租约。本版只允许 level 0 (观察) 与 1 (建议)。

    level 2/3 接口存在但 OMS 拒绝任何 AUTO_L2/L3 来源的意图。
    按日失效: created_at 当日有效, 次日或网关重启后默认回到 DISARMED。
    """

    grant_id: str
    account_id: str                       # 授权仅对该实盘账户生效
    strategy_id: str                      # 授权对象 (策略或 AI agent ID)
    level: int                             # 0=观察 / 1=建议 / 2=监督执行 / 3=有界自治
    max_order_value: float = 0.0          # 单笔上限 (level>=2 才生效)
    daily_loss_limit_pct: float = 0.0    # 日内最大亏损比例 (level>=2 才生效)
    enabled: bool = True
    created_at: str = ""
    revoked_at: str | None = None


@dataclass(frozen=True)
class ReconciliationDiff:
    """对账差异记录。resolved=True 表示已通过重新对账或人工确认消除。"""

    diff_id: str
    run_id: str
    order_id: str | None                   # 本地订单 ID; 资金/持仓差异时可为 None
    diff_kind: str                         # order_state / position / cash / unknown_order
    local_value: str | None = None
    broker_value: str | None = None
    resolved: bool = False
    resolved_at: str | None = None
