# Changelog

本项目变更记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。

## [Unreleased]

### Added — 行情源健康检查 + 自愈机制

2026-10-26 事故: 后端进程长跑 (>2 天) 后 QuoteService 内 httpx 连接池僵死,
扶摇行情请求全 "网络请求失败:", 监控页下午无任何交易数据。curl 直探扶摇端点
全 200, 证实是进程内连接池问题, 重启即恢复, 但需人工 kill。

新增 `QuoteHealthWatcher` (`app/watchdog.py`) 自动检测并恢复:

- **双信号诊断**: `quote_age_ms` 陈旧 (监控无数据) + 独立探针仍可达扶摇 (源没宕)
  → 判定连接池僵死。单信号判不准: 只看 age 会把源宕误判为池僵死; 只探源会漏检
  (探针与行情是两个独立连接池)。
- **两级自愈阈值**:
  1. 连续 3 次僵死信号 → 调 `QuoteService.rebuild_realtime_client()` 软重建 httpx 客户端
  2. 重建后仍持续失败到 10 次 → `os._exit(70)` 交 supervisor 拉起
  3. 重建后立即验证 (等 2s 再探一次, 不等下一轮 60s)
- **防误报 5 层** (任一成立即跳过探测):
  1. 非 polling 窗口 (休市不探)
  2. provider 非 fuyao (探针端点扶摇专属)
  3. `quote_service.paused=True` (pipeline/数据修正显式暂停)
  4. `mining_manager.has_active_jobs()` (mining job 在跑)
  5. `job_store.active_id()` (pipeline job 在跑) — 2026-10-28 事故修复
- **探针客户端自愈**: 探针 httpx.Client 连续失败 N 次后自动重建 (探针自己也可能池僵死)
- **provider 动态切换感知**: `quote_probe` 内动态读 `preferences.get_realtime_data_provider()`,
  用户运行中从 fuyao 切到 tdxquant 时自动停探, 切回立即恢复
- **`/api/health/quote` 端点** (`app/api/health.py`): 暴露 watcher 运行状态
  (stale_count / rebuild_attempts / last_probe_ok 等), 供运维查看自愈历史

### Fixed

- **`'Client' object has no attribute 'snapshot_all'`** (2026-10-27):
  `rebuild_realtime_client()` 兜底分支用裸 `httpx.Client` 替换了 `FuyaoProvider._client`
  (本应是 `FuyaoClient`, 有 `snapshot_all` / `dump_download_url` 等方法), 导致行情拉取
  全部 AttributeError。改为: `FuyaoProvider` 调 `close()` (置 None + 懒重建 FuyaoClient),
  `GenericHTTPProvider` 调 `rebuild_client()`。

- **pipeline 期间 watcher 误退出** (2026-10-28):
  pipeline job (日K逐只拉取) 占用 provider client 导致 snapshot 停摆, watcher 检测到
  "僵死" 并连续报 10 次 → `os._exit(70)` → 进程退出 → 重启 → pipeline 恢复 → 循环。
  根因: 当时只检查 `mining_manager` (管挖掘任务), 不检查 `job_store` (管 pipeline)。
  修复: `quote_probe` 新增 `job_store.active_id()` 检查。

- **自愈重建把数据源永久打死** (2026-11-18):
  `rebuild_realtime_client()` 用 `getattr(provider, "close")` 取重建入口, 但
  `get_provider()` 返回的是 `ManagedProvider` handle, `close()` 命中 handle 自己的实现
  → 标记 `_current[2]=True` 永久停用 → 此后每轮抛「数据源已停止」→ age 一路涨到 10/10
  → `os._exit(70)`。表现为"不重建还能自己恢复, 一重建必死", 11-13 起每日 1-2 次重启。
  修复: `ManagedProvider` 新增 `rebuild_client()` 转发到内层 provider (持锁计入在途引用,
  避免被 `replace()` 提前关闭), `rebuild_realtime_client()` 只走该入口。

- **睡眠唤醒被误判成池僵死** (2026-11-18):
  探针在系统唤醒 (DarkWake) 的瞬间先于行情轮询线程恢复, 此时 `quote_age` 还是睡眠
  或休盘期间留下的旧值, 探针据此判定「池僵死」并逐级升级: 连续 3 次即软重建, 10 次即
  `os._exit(70)`。13:09:02 实测 `quote_age`=3628s, 拆开看是「睡眠 1020s + 午休不轮询的
  清醒时间」, 与墙钟间隔减去累计睡眠后的 60.4 分钟精确吻合。

  **机制澄清 (此前的猜测是错的)**: 曾以为 `quote_age` 会因睡眠而虚高, 实测不是。
  `time.perf_counter()` 在 macOS 是 `CLOCK_UPTIME_RAW`、在 Linux 是 `CLOCK_MONOTONIC`,
  两者都**不计入** suspend, 所以睡眠期间 age 干脆冻住不动。真正的问题是唤醒竞态:
  进程恢复瞬间探针线程先跑, 读到的 age 尚未被轮询线程刷新 —— 13:09:02.376 探针发出请求,
  13:09:03.216 轮询才开始取数, 差了 0.84 秒。判定赢在了数据更新前面。

  修复: 新增 `SleepTracker`, 用「墙钟 − 单调钟」偏移的增量判睡眠 —— 该偏移正常运行期间
  恒定 (实测漂移 1 微秒/3 秒), 只在系统睡过之后跳变, 无需读系统日志、无需任何权限。
  唤醒后第一轮探测直接作废, 既不记僵死 (防误重建) 也不清零 (防真僵死在睡眠中漏报)。
  容差取陈旧阈值的一半, 保证「长到能顶破阈值的睡眠」必定被测到。

  验证 (回放全部历史日志):
  - pmset 覆盖起点为 11-11 16:58, 此前 28 次触发无系统事件可核对, 单独归为「不可判定」。
  - 覆盖范围内 78 次触发中, 有 11 次是睡眠唤醒后的首轮探测 —— 阈值仍是 18s 时它们全部
    会误报, 现被拦截; 另 2 次 age<60s, 本就够不到阈值。
  - 单看 ≥1000s 的极端触发: 覆盖范围内 8 次, **8/8 命中唤醒瞬间**, 无一例外。
  - 11-13 14:28:53 那次尤其值得记: 唤醒后 3s 误判 → 触发软重建 → 撞上「重建把数据源
    永久打死」的旧 bug → 之后 7 次 age 阶梯式上涨 (249→610s, 每轮 +60s, 14:28:56~14:34:57)
    都是这个误判的下游后果, 直到 14:34:59 进程重启才恢复 (中断约 6 分钟)。
    两个 bug 叠加, 一次睡眠就能演变成一段真实的行情空窗。

- **陈旧阈值把正常评估耗时误判成池僵死** (2026-11-18):
  阈值原为 `interval × stale_intervals` (= 18s @6s 轮询), 但轮询循环每轮还串行跑监控
  评估 + 模拟盘撮合, 实测 15-20s (当日 p90 相邻刷新间隔 21s, p99 30s), 于是探测器每个
  周期 (60s) 都能撞上一次 —— 11-18 上午连续出现 10:25~10:33、11:09~11:17 两段
  「每 60 秒一条」的连击, 每段都触发一次没必要的软重建。改为
  `max(interval × stale_intervals, 绝对下限)`, 新增 `quote_health_min_stale_s`
  (默认 60s) 兜住评估耗时; 慢轮询源 (60s) 仍按倍数放大。
  回放全部 106 次触发: 其中 61 次 age 落在 18.2~56.8s, 全部低于新下限, 这类误报被整体消除。

### Changed

- **`GenericHTTPProvider` 新增 `rebuild_client()` 方法** (`app/data_providers/custom/provider.py`):
  与 `__init__` 构造逻辑一致 (统一 timeout, 无自定义 headers/auth), 供健康检查器调用。
- **`MiningJobManager` 新增 `has_active_jobs()` 方法** (`app/services/mining_manager.py`):
  供健康检查器判断是否有 mining job 在跑。
- **`_run_once` 重建后验证改为标志位** (`app/watchdog.py`):
  `is_post_rebuild_verify=True` 时不再触发重建 (避免无限递归), 只验证恢复与否。
- **`except BaseException` → `except Exception`** (`QuoteHealthWatcher._run_once`):
  让 `KeyboardInterrupt` / `SystemExit` 正常穿透, 不被当僵死计数。

### Config

新增 7 项设置 (`app/config.py`), 均可通过环境变量覆盖:

| 设置 | 默认 | 说明 |
|------|------|------|
| `quote_health_enabled` | `True` | 启用行情健康检查 |
| `quote_health_interval_s` | `60.0` | 探测周期 (秒) |
| `quote_health_probe_timeout_s` | `10.0` | 单次探测超时 (秒) |
| `quote_health_rebuild_threshold` | `3` | 软重建触发阈值 (连续僵死次数) |
| `quote_health_exit_threshold` | `10` | 硬退出触发阈值 |
| `quote_health_stale_intervals` | `3` | quote_age > interval × stale_intervals 即陈旧 |
| `quote_health_min_stale_s` | `60.0` | 陈旧阈值绝对下限 (秒); 实际阈值取 max(相对, 本值) |

### Tests

新增 `backend/tests/test_quote_health.py` (25 个测试):

- 5 个 watcher 场景 (健康/陈旧重建/恢复复位/僵死退出/源宕不重建)
- 9 个 `quote_probe` 单元测试 (双信号诊断各分支 + 非 fuyao 跳过 + paused 跳过)
- 3 个陈旧阈值测试 (下限主导/相对值主导/随 stale_intervals 缩放)
- 8 个睡眠唤醒检测测试 (`SleepTracker` 四态 + 容差随阈值派生 + watcher 作废/恢复判定/不清零僵死周期)

`backend/tests/test_watchdog.py` (4 个) 无回归。
