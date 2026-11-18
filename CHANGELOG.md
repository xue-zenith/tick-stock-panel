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

- **陈旧阈值把正常评估耗时误判成池僵死** (2026-11-18):
  阈值原为 `interval × stale_intervals` (= 18s @6s 轮询), 但轮询循环每轮还串行跑监控
  评估 + 模拟盘撮合, 实测 15-20s (当日 p90 相邻刷新间隔 21s, p99 35s), 于是每 6 分钟
  误报一次并触发一次没必要的重建。改为 `max(interval × stale_intervals, 绝对下限)`,
  新增 `quote_health_min_stale_s` (默认 90s) 兜住评估耗时; 慢轮询源 (60s) 仍按倍数放大。
  用当日日志回放: 池僵死信号从 21 次降到 11 次, 剩余全部落在源确实被打死的故障段。

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

新增 6 项设置 (`app/config.py`), 均可通过环境变量覆盖:

| 设置 | 默认 | 说明 |
|------|------|------|
| `quote_health_enabled` | `True` | 启用行情健康检查 |
| `quote_health_interval_s` | `60.0` | 探测周期 (秒) |
| `quote_health_probe_timeout_s` | `10.0` | 单次探测超时 (秒) |
| `quote_health_rebuild_threshold` | `3` | 软重建触发阈值 (连续僵死次数) |
| `quote_health_exit_threshold` | `10` | 硬退出触发阈值 |
| `quote_health_stale_intervals` | `3` | quote_age > interval × stale_intervals 即陈旧 |
| `quote_health_min_stale_s` | `90.0` | 陈旧阈值绝对下限 (秒); 实际阈值取 max(相对, 本值) |

### Tests

新增 `backend/tests/test_quote_health.py` (13 个测试):

- 5 个 watcher 场景 (健康/陈旧重建/恢复复位/僵死退出/源宕不重建)
- 7 个 `quote_probe` 单元测试 (双信号诊断各分支)
- 1 个 pipeline 暂停期间不探测 (`test_probe_skips_when_paused`)

`backend/tests/test_watchdog.py` (4 个) 无回归。
