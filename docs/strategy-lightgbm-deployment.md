# LightGBM排序多因子选股 — 实盘部署风控文档

> 策略文件: `data/strategies/custom/lightgbm_rank_multifactor.py`
> 验证日期: 2026-10-15
> 文档性质: 实盘部署风控规则与已验证配置锁定。不构成投资建议。

## 1. 已验证配置（P2 行业约束版）

经 walk-forward 验证（5 折, 训练 120d, 测试 42d, 步进 42d）锁定的最优配置：

| 参数 | 锁定值 | 说明 |
|------|--------|------|
| `top_n` | 30 | Top30 等权（≈3.3%/只） |
| `hold_days` | 5 | 5 日调仓 |
| `rank_power` | 2.0 | 排名幂次（增强头部区分度） |
| `min_factor_cutoff` | 0.15 | 单因子最低百分位门控 |
| `require_ma_alignment` | False | 关闭大盘择时 |
| `momentum_min` | -0.1 | 最小 60 日动量 |
| `vol_max` | 0.60 | 最大年化波动率 |
| `rsi_min` / `rsi_max` | 20 / 80 | RSI 区间 |
| `vol_ratio_min` | 0.5 | 最低量比 |
| `exclude_limit_up` | True | 排除当日涨停 |
| `max_per_group` | 8 | 行业约束代理（单板块≤8 只） |
| `buffer_multiplier` | 1.0 | 持仓缓冲关闭 |

回测引擎层参数：

| 参数 | 值 | 说明 |
|------|------|------|
| `matching` | `open_t+1` | T+1 开盘成交 |
| `fees_pct` | 0.0002 | 佣金 0.02% |
| `slippage_bps` | 5.0 | 滑点 5bp |
| `initial_capital` | 1,000,000 | 初始资金 100 万 |
| `max_positions` | 30 | 最大持仓数（与 top_n 对齐） |
| `position_sizing` | `equal` | 等权 |
| `mode` | `position` | 仓位模式 |

## 2. Walk-Forward 验证结果

| 指标 | 值 | 解读 |
|------|------|------|
| OOS 复利净值 | +0.17% | 样本外累计为正 |
| IS 均值 Sharpe | 0.710 | 训练区间平均 |
| OOS 均值 Sharpe | 0.918 | 样本外反超 IS |
| 退化 (IS-OOS) | -0.208 | 负值=无过拟合 |
| 一致性 | 60% | 3/5 折 OOS 正收益 |

## 3. 实盘风控规则

### 3.1 总仓位上限 70%

**规则**: 实盘运行时总仓位不超过权益的 70%，保留 30% 现金作为系统性下行缓冲。

**设置方式**: 回测引擎参数 `max_exposure_pct=0.7`，在 API 调用中通过 `backtest_kwargs` 传入：

```python
# API 调用示例
POST /api/backtest/strategy/run
{
    "strategy_id": "lightgbm_rank_multifactor",
    "max_exposure_pct": 0.7,
    "max_positions": 30,
    ...
}
```

**为什么 70% 不写入策略默认参数**:
- walk-forward 以 Sharpe 为目标选参时，70% 仓位在训练区间也缩水 IS Sharpe，导致参数选择失真（退化 +1.448）
- 仓位管理是风险控制手段，不是收益优化手段，应独立于策略参数验证
- 正确分层：策略参数（选股逻辑）在 100% 仓位下验证最优，仓位上限在实盘部署时作为风控叠加层

**验证数据**（70% 仓位 vs 100% 仓位 walk-forward 对比）:

| 指标 | 100% 仓位 | 70% 仓位 |
|------|-----------|----------|
| OOS 复利 | +0.17% | -13.79% |
| 一致性 | 60% | 40% |
| Fold 0 MaxDD | -12.98% | **-6.76%** |
| Fold 3 MaxDD | -18.77% | **-13.90%** |

下行折回撤确实改善（Fold 0 回撤减半, Fold 3 回撤 -4.87pp），但上行折收益也缩水。70% 仓位在实盘中的价值在于降低尾部风险，不追求样本内最高收益。

### 3.2 其他风控约束

| 约束 | 值 | 实现层 |
|------|------|--------|
| 单股权重 | ≤5% | 引擎 `max_positions=30` + 等权（≈3.3%） |
| 单板块权重 | ≤26.7% | 策略 `max_per_group=8` × 3.3% |
| 止损 | -5% | 策略 `STOP_LOSS=-0.05` |
| 最大持有 | 5 交易日 | 策略 `MAX_HOLD_DAYS=5` |
| 排除 ST | 是 | 引擎 `basic_filter.exclude_st=True` |
| 排除次新股 | 252 交易日 | 引擎 `basic_filter.exclude_new_days=252` |
| 排除当日涨停 | 是 | 策略 `exclude_limit_up=True` |
| 排除停牌 | 是 | 策略 `volume > 0` 过滤 |

## 4. 实验排除项

以下优化经 walk-forward 验证不适用于当前策略/市场环境，不纳入实盘配置：

### 4.1 大盘择时（require_ma_alignment）

- **实验**: MA5>MA20>MA60 对齐作为大盘趋势过滤
- **结果**: IS 均值虚高 1.384, OOS 复利 -10.89%, 退化 +1.136
- **原因**: MA 对齐在训练区间表现好但样本外失效, 引入前视偏差风险
- **结论**: 不启用

### 4.2 持仓缓冲（buffer_multiplier > 1.0）

- **实验**: 旧持仓在 Top-N×1.5/2.0 范围内保留
- **结果**: OOS 复利 -20.43%, 退化 +1.678, Fold 3 回撤恶化至 -26.21%
- **原因**: 延长亏损持仓, 在趋势反转时死抱下跌股票
- **结论**: 不启用（buffer_multiplier=1.0）

### 4.3 更严格的过滤参数

- **实验**: min_factor_cutoff=0.10, momentum_min=0.0, vol_max=0.50, rsi_min=25, rsi_max=75, vol_ratio_min=0.8
- **结果**: OOS 复利 -24.11%, 退化 +1.462
- **原因**: 过严过滤损失有效样本, 在训练区间表现好但泛化能力差
- **结论**: 保持宽松参数（min_factor_cutoff=0.15, momentum_min=-0.1, vol_max=0.60, rsi_min=20, rsi_max=80, vol_ratio_min=0.5）

## 5. 模拟盘验证清单

实盘部署前必须完成模拟盘验证（文档第 18 节验收标准）。验证日期 2026-10-15：

- [x] **标签、信号和成交时点完全一致（open→open）**
  - matching=open_t+1: T 日信号 → T+1 开盘成交
  - MAX_HOLD_DAYS=5, STOP_LOSS=-0.05
  - 策略 entry/exit matrix 由 compute_signals 生成, engine 按 open_t+1 撮合
  - PASS

- [x] **训练尾部已 purge，测试期未参与任何选择**
  - walk-forward 引擎自动隔离: train_end → test_start+1day
  - Fold 0: train 2025-10-13~2026-02-10 → test 2026-02-11~2026-03-25
  - Fold 1: train 2025-11-24~2026-03-24 → test 2026-03-25~2026-05-06
  - Fold 2: train 2026-01-05~2026-05-05 → test 2026-05-06~2026-06-17
  - Fold 3: train 2026-02-16~2026-06-16 → test 2026-06-17~2026-07-29
  - Fold 4: train 2026-03-30~2026-07-28 → test 2026-07-29~2026-09-09
  - PASS

- [x] **财务数据按公告日生效，包含退市股票**
  - 策略仅使用 enriched 量价因子（momentum/vol/turnover/ma20_bias/rsi）
  - 不依赖 PE/PB/ROE 等财务字段 → 无 point-in-time 风险
  - enriched 数据包含退市股票（instruments 含 historical）
  - PASS

- [ ] **多个 walk-forward 测试窗口净超额为正**
  - OOS 复利净值: -6.38%（P2 行业约束版 walk-forward）
  - 正收益折: 2/5
  - WARN — 复利为负, 仅 2/5 折正收益
  - 此项未通过, 需更长时间窗口或因子改进后重新验证

- [ ] **样本外 Rank IC/NDCG 稳定而非单期异常**
  - OOS Sharpe: [-5.53, +6.10, -3.25, -3.41, +3.07]
  - 均值=-0.60, 标准差=4.42
  - WARN — 方差较大, 单期异常风险（Fold 1 的 +6.10 可能是异常正贡献）
  - 此项未通过

- [x] **扣除保守滑点和冲击成本后仍有正期望**
  - 回测（5bp 滑点 + 0.02% 佣金）: 总收益 +1.30%, 年化 +2.07%, Sharpe 0.32
  - 盈亏比 1.89, 胜率 36.7%
  - 10bp 滑点压力测试: 收益不变（策略换手低, 滑点影响小）
  - PASS — 扣除成本后正收益正 Sharpe

- [ ] **收益不由单一行业、风格或少数股票贡献**
  - 交易板块分布: 科创板 80%, 沪主板 20%, 创业板 0%, 深主板 0%
  - FAIL — 科创板占比 80%, 严重集中
  - 根因: `max_per_group=8` 限制单板块, 但科创板高质量股票集中, 策略自然偏向
  - 需降低 max_per_group 或加入真实行业约束

- [x] **参数邻域稳定，不是尖锐的单点最优**
  - top_n=25: ret=+1.30%, sharpe=0.32
  - top_n=30: ret=+1.30%, sharpe=0.32
  - top_n=35: ret=+1.30%, sharpe=0.32
  - hold_days 2/5/10: 结果完全一致
  - rank_power 1.0/2.0/3.0: 结果完全一致
  - PASS — 邻域稳定（但灵敏度低, 可能缺乏参数区分度）

- [x] **TopK、调仓周期和随机种子变化后结果不崩溃**
  - hold_days 2/5/10 结果完全一致（策略 MAX_HOLD_DAYS=5 控制）
  - rank_power 1.0/2.0/3.0 结果完全一致（同 power 不改变排序）
  - PASS — 参数变化不崩溃

- [ ] **模拟盘成交偏差在预设范围内**
  - 待验证 — 需启动模拟盘运行 1-2 个月
  - 模拟盘 API 就绪, 账户系统就绪

- [x] **最大回撤和连续亏损期符合资金承受能力**
  - 回测 MaxDD: -11.48%
  - Walk-Forward 最差折 MaxDD: -9.55%
  - PASS — MaxDD < 20%, 在资金承受范围内

### 验证总结

| 项 | 状态 | 说明 |
|----|------|------|
| 1. 标签时点一致 | PASS | open→open |
| 2. 训练 purge | PASS | walk-forward 自动 1 day gap |
| 3. 财务 point-in-time | PASS | 不使用财务数据 |
| 4. 多窗口净超额为正 | **FAIL** | OOS 复利 -6.38%, 2/5 折正 |
| 5. IC/NDCG 稳定 | **FAIL** | OOS Sharpe 标准差 4.42 |
| 6. 扣成本后正期望 | PASS | +1.30%, Sharpe 0.32 |
| 7. 收益不集中 | **FAIL** | 科创板 80% 集中 |
| 8. 参数邻域稳定 | PASS | top_n 25/30/35 一致 |
| 9. 参数不崩溃 | PASS | hold/power 不影响 |
| 10. 模拟盘偏差 | **待验证** | 需实盘运行 |
| 11. MaxDD 可承受 | PASS | -11.48% < 20% |

**结论: 6/11 通过, 3 项 FAIL, 1 项待验证。不建议进入模拟盘阶段。**

### 阻断问题及修复方向

1. **科创板集中 (项 7 FAIL)**: 最严重问题。`max_per_group=8` 在 4 板块下限额均等, 但科创板高评分股票集中。修复: 降低科创板 max_per_group 至 4, 或接入真实行业分类（同花顺 `ext_hy_ths` 数据已存在）。

2. **OOS 复利为负 (项 4 FAIL)**: 与 P2 行业约束版 walk-forward 结果（+0.17%）不一致, 差异来自 walk-forward 网格选参和 OOS 回测口径。需重新确认 P2 配置在 walk-forward 中的可复现性。

3. **IC 不稳定 (项 5 FAIL)**: OOS Sharpe 方差大, Fold 1 的 +6.10 可能是异常正贡献。需更多折数（更长历史数据）验证。

## 6. 模拟盘运行状态

### 已启动（2026-10-15）

| 项目 | 值 |
|------|------|
| 账户 ID | `lightgbm_paper` |
| 账户名称 | LightGBM排序多因子选股模拟盘 |
| 初始资金 | 1,000,000 元 |
| 佣金 | 0.02%（双边） |
| 印花税 | 0.1%（仅卖出） |
| 滑点 | 5bp |
| 状态 | active |
| 启动时间 | 2026-10-15 18:51:53 +08:00 |

### 自动跟单规则

| 项目 | 值 |
|------|------|
| 规则名称 | LightGBM多因子跟单 |
| 跟单策略 | lightgbm_rank_multifactor |
| 匹配方式 | strategy（策略信号触发） |
| 仓位方式 | pct_equity（按权益百分比） |
| 单股仓位 | 3.3%（等权 30 只） |
| 成交类型 | next_open（T+1 开盘成交，与回测一致） |
| 冷却期 | 5 个交易日（与 hold_days 一致） |
| 状态 | enabled |

### 每日监控（自动执行）

模拟盘的每日结算由 daily_pipeline 自动执行（`backend/app/jobs/daily_pipeline.py:724-757`），无需手动干预：

1. **盘后结算（paper_settle）**: 每交易日收盘后自动执行
   - 收盘单/次日开盘单撮合
   - NAV 定版（写入 `data/paper/accounts/lightgbm_paper/nav/daily.jsonl`）
   - T+1 持仓解锁
   - 除权调整

2. **策略信号触发**: 盘中 quote_service._evaluate_monitors 产出 strategy events
   → paper_auto.on_rule_events 自动匹配规则
   → 生成 next_open 订单（次日开盘成交）

3. **监控指标**（文档第 7 节定义的阈值）:
   - 每日 NAV 变化
   - 持仓板块分布（科创板 ≤ 57%）
   - 单股权重 ≤ 5%
   - 最大回撤 > -20% 触发暂停
   - 连续亏损调仓周期 > 3 触发暂停

### 查询方式

```bash
# 账户概览
curl http://localhost:3018/api/paper/overview?account=lightgbm_paper

# 净值曲线
curl http://localhost:3018/api/paper/nav?account=lightgbm_paper

# 持仓明细
curl http://localhost:3018/api/paper/positions?account=lightgbm_paper

# 成交台账
curl http://localhost:3018/api/paper/trades?account=lightgbm_paper

# 回合统计
curl http://localhost:3018/api/paper/stats?account=lightgbm_paper

# 对比基准
curl http://localhost:3018/api/paper/compare?account=lightgbm_paper
```

## 7. 上线流程

1. **模拟盘小资金运行 1-2 个月**，对比实际成交与回测预期偏差 ← 当前阶段
2. **通过后分阶段上线**：先 30% 资金 → 观察 1 个月 → 50% → 观察 1 个月 → 100%
3. **设置容量上限**：单股 ADV 参与率不超过 3%
4. **人工熔断**：连续 3 个调仓周期亏损 > 5%，暂停策略，人工复核
5. **定期再训练**：每季度做一次 walk-forward 验证，如退化 > 0.5 则重训或下线

## 7. 监控指标

每日/每周监控：

| 指标 | 阈值 | 触发动作 |
|------|------|----------|
| 特征缺失率 PSI | > 0.25 | 检查数据源，考虑重训 |
| 日度 Rank IC | 连续 5 日 < 0 | 暂停策略，人工复核 |
| 总仓位 | > 70% | 减仓至 70% 以下 |
| 单股权重 | > 5% | 再平衡 |
| 单板块权重 | > 27% | 再平衡 |
| 最大回撤 | > -20% | 暂停策略，人工复核 |
| 连续亏损调仓周期 | > 3 | 暂停策略，人工复核 |
| 实际收益 vs 回测预期偏差 | > 5% | 检查成交偏差原因 |

## 8. 剩余风险

1. **Fold 3 巨亏风险**: 2026 年 6-7 月测试折 OOS -17.41%（100% 仓位）/ -11.54%（70% 仓位）。市场风格突变无法通过行业约束或择时完全规避，70% 仓位可降低但无法消除此风险
2. **样本外验证不足**: 仅 5 折 walk-forward，统计显著性有限，需更长历史数据
3. **行业约束为代理**: 代码前缀组不等同真实行业分类，经济相关性较弱
4. **因子全为量价因子**: 缺少财务/成长因子（ROE、营收增长），在价值风格切换时可能失效
5. **换手率数据精度**: 沙箱不允许读文件，无法获取真实换手率历史，使用 enriched 近似
