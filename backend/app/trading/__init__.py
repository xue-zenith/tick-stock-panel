"""实盘交易域模块 (与模拟盘 paper.py 严格隔离)。

子模块:
  - contracts: 不可变值对象与状态枚举 (TradeIntent / BrokerOrder / Execution / ...)。
  - state_machine: 订单状态转换表 + fail-closed 转换函数。
  - store: SQLite WAL 存储 (schema / 迁移 / 单写者事务)。
  - oms: 订单管理系统 (提交/审批/撤单/回报/对账)。
  - risk: 确定性风控引擎 (账户/标的/市场/系统级规则)。
  - reconciliation: 券商对账与差异处理。
  - allocations: 策略/人工/AI 仓位归因。
  - autonomy: AI 自动交易授权租约 (本版只到 L0/L1)。
  - events: 交易事件 → AlertEvent dict 格式器 (复用 SSE/通知链路)。
  - brokers: 券商适配器 (base=Protocol, mock=测试用内存适配器)。

券商为实盘事实源; 本地 SQLite 保存意图/审批/风控/回报副本/审计。
实盘写入走 _execute_write (RLock + BEGIN IMMEDIATE + commit/rollback),
不触碰 paper.py 的 fills.jsonl。
"""
from __future__ import annotations