"""后端自愈看门狗。

2026-09-07 事故形态: polars 并发死锁把线程悬死在 collect 内部 (0 CPU 永久
挂起), 其中持锁者让 _write_lock 永久被占, 所有请求线程排队冻结, 只能人工
重启。并发闸 (app.polars_guard) 与写锁瘦身 (repository 乐观并发) 分别削减
触发概率与扩散半径; 看门狗是最后一层兜底 —— 探测走与事故相同的共享资源
路径 (collect 闸 + 全局写锁), 连续 N 次超时即判定进程已僵死, 主动退出交由
supervisor / Docker restart / dev 脚本拉起, 把恢复时间从"人工发现"缩短到
约一分钟。

误伤防护: 探测本身是毫秒级微型 collect + 1s 写锁试探, 阈值要求连续失败
(默认 2 次 × 15s 超时), 高负载下"慢而未死"不会触发。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from collections.abc import Callable

import polars as pl

import httpx

from app.config import settings
from app.polars_guard import guarded_collect

logger = logging.getLogger(__name__)


def default_probe(write_lock: threading.Lock | None = None) -> None:
    """探测关键共享资源: polars collect 闸 + 仓库全局写锁。

    任一被悬死线程占住即超时 — 正是 2026-09-07 冻结事故中被毒化的两条路径。
    """
    guarded_collect(pl.LazyFrame({"probe": [1]}).sum())
    if write_lock is not None:
        acquired = write_lock.acquire(timeout=1.0)
        if not acquired:
            raise TimeoutError("repository write lock unavailable")
        write_lock.release()


class HealthWatchdog:
    """周期探测; 连续 failure_threshold 次失败后调用 exit_cb(退出码)。"""

    def __init__(
        self,
        probe: Callable[[], None],
        *,
        exit_cb: Callable[[int], None],
        interval_s: float | None = None,
        probe_timeout_s: float | None = None,
        failure_threshold: int | None = None,
    ) -> None:
        self._probe = probe
        self._exit_cb = exit_cb
        self._interval_s = settings.watchdog_interval_s if interval_s is None else interval_s
        self._probe_timeout_s = (
            settings.watchdog_probe_timeout_s if probe_timeout_s is None else probe_timeout_s
        )
        self._failure_threshold = (
            settings.watchdog_failure_threshold if failure_threshold is None else failure_threshold
        )
        self._consecutive_failures = 0
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._probe), timeout=self._probe_timeout_s
                )
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                # 取消必须穿透: stop() 靠 task.cancel() 结束本循环, 若被下面的
                # BaseException 分支吞掉, 取消会被误计为一次"探测失败"且循环
                # 继续运行 — stop() 的 await self._task 永久等待 (关闭死锁)。
                raise
            except BaseException as exc:  # 探测任何异常都算失败 (含 to_thread 超时)
                self._consecutive_failures += 1
                logger.error(
                    "watchdog probe failed (%d/%d): %r",
                    self._consecutive_failures,
                    self._failure_threshold,
                    exc,
                )
                if self._consecutive_failures >= self._failure_threshold:
                    logger.critical(
                        "watchdog: backend wedged (probe failed %d consecutive times); "
                        "exiting for supervisor restart",
                        self._consecutive_failures,
                    )
                    self._exit_cb(70)
                    return
            await asyncio.sleep(self._interval_s)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="health-watchdog")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def start_watchdog(app_state, repo) -> HealthWatchdog | None:
    """lifespan 启动钩子; 返回实例挂到 app.state.watchdog 便于关闭。"""
    if not settings.watchdog_enabled:
        return None
    write_lock = getattr(repo, "_write_lock", None)
    watchdog = HealthWatchdog(
        lambda: default_probe(write_lock),
        exit_cb=lambda code: os._exit(code),
    )
    watchdog.start()
    return watchdog


# ================================================================
# 行情源健康检查 (QuoteHealthWatcher)
#
# 2026-10-26 事故: 后端进程长跑后 QuoteService 内 httpx 连接池僵死,
# 扶摇请求全 "网络请求失败:", 监控页下午无数据。curl 直探扶摇全 200,
# 证实是进程内连接池问题, 重启即恢复 —— 但需人工 kill。
#
# 双信号诊断: quote_age_ms 陈旧 (监控无数据) + 独立探针仍可达扶摇 (源没宕)
# → 判定 QuoteService 连接池僵死 → 软重建客户端。单信号判不准:
#   只看 age 会把源宕误判为池僵死 → 误重建;
#   只探源会漏检池僵死 (探针与行情是两个池) → 漏报。
#
# 两级阈值: rebuild_threshold 次软重建; 仍持续失败到 exit_threshold 次
# → os._exit(70) 交 supervisor 拉起 (与 HealthWatchdog 同出口码)。
# ================================================================


class QuotePoolStale(Exception):
    """行情 quote_age 陈旧但独立探针可达源 —— 池僵死信号。"""


class SourceUnavailable(Exception):
    """独立探针探不到源 —— 源/网络问题, 不重建 (重建无用)。"""


def quote_probe(qs, probe_http: "httpx.Client", mining_manager=None) -> None:
    """双信号探测 QuoteService 连接池健康度。

    非 polling 窗口直接返回 (休市不探, 避免误判)。
    当前 provider 非 fuyao 直接返回 (探针端点扶摇专属, 切到 tdxquant/tickflow 时不探)。
    quote_service 处于 paused 状态直接返回 (pipeline/数据修正正在跑, snapshot 暂停是正常的,
      此时 quote_age 涨不代表池僵死)。
    mining_manager 有活跃 job 或 pipeline job 在跑直接返回 (日K逐只拉取会占用 provider
      client 导致 snapshot 停摆, 但这是正常的 pipeline 行为, 不是池僵死)。
    quote_age 未陈旧直接返回 (健康)。
    陈旧 → 独立探针探扶摇快照端点:
      探针失败 → SourceUnavailable (源/网络问题, 不重建)
      探针成功 → QuotePoolStale (池僵死信号, 触发重建)
    """
    # 动态读 provider, 感知运行中切换 (用户从 fuyao 切到 tdxquant 时自动停探)
    from app.services import preferences

    if preferences.get_realtime_data_provider() != "fuyao":
        return  # 非 fuyao provider 不探 (探针端点扶摇专属)

    st = qs.status()
    if not st.get("is_polling_window"):
        return
    # pipeline/数据修正期间行情被显式暂停, quote_age 涨是预期行为, 不算僵死
    if st.get("paused"):
        return
    # mining job 正在跑 (日K逐只拉取) 会占用 provider client, snapshot 停摆是正常副作用
    if mining_manager is not None and mining_manager.has_active_jobs():
        return
    # pipeline job (盘后管道/手动同步) 在跑也会占用 provider client, 同样不算僵死
    try:
        from app.services.pipeline_jobs import job_store

        if job_store.active_id() is not None:
            return
    except Exception:  # noqa: BLE001
        pass
    age = st.get("quote_age_ms")
    interval = st.get("interval_s") or 0.0
    stale_threshold_ms = interval * 1000 * settings.quote_health_stale_intervals
    if age is None or interval <= 0 or age < stale_threshold_ms:
        return  # 健康
    # quote_age 陈旧 → 独立探针确认源是否可达
    # 用 /api/a-share/prices/snapshot?limit=1: 只拉 1 条快照, 响应体最小 (~300B),
    # 比 trading-days (全年日历 ~250 行, ~3KB) 轻 10 倍。
    try:
        r = probe_http.get("/api/a-share/prices/snapshot", params={"limit": 1})
    except Exception as e:  # noqa: BLE001
        raise SourceUnavailable(f"探针网络失败: {e}") from e
    if r.status_code != 200:
        raise SourceUnavailable(f"探针 HTTP {r.status_code}")
    try:
        payload = r.json()
    except ValueError as e:
        raise SourceUnavailable(f"探针响应非 JSON: {e}") from e
    if payload.get("code") not in (0, "0", None):
        raise SourceUnavailable(f"扶摇 code={payload.get('code')}: {payload.get('message', '')}")
    # 源可达 + age 陈旧 → 池僵死
    raise QuotePoolStale(
        f"quote_age={int(age)}ms > {int(stale_threshold_ms)}ms 但探针可达源 → 池僵死"
    )


class QuoteHealthWatcher:
    """周期探测 QuoteService 连接池; 软重建客户端, 持续失败到 exit 阈值再退出。"""

    def __init__(
        self,
        probe: Callable[[], None],
        *,
        rebuild_cb: Callable[[], None],
        exit_cb: Callable[[int], None],
        probe_rebuild_cb: Callable[[], None] | None = None,
        interval_s: float | None = None,
        probe_timeout_s: float | None = None,
        rebuild_threshold: int | None = None,
        exit_threshold: int | None = None,
    ) -> None:
        self._probe = probe
        self._rebuild_cb = rebuild_cb
        self._exit_cb = exit_cb
        self._probe_rebuild_cb = probe_rebuild_cb  # 探针客户端自愈 (可选)
        self._interval_s = (
            settings.quote_health_interval_s if interval_s is None else interval_s
        )
        self._probe_timeout_s = (
            settings.quote_health_probe_timeout_s if probe_timeout_s is None else probe_timeout_s
        )
        self._rebuild_threshold = (
            settings.quote_health_rebuild_threshold
            if rebuild_threshold is None
            else rebuild_threshold
        )
        self._exit_threshold = (
            settings.quote_health_exit_threshold if exit_threshold is None else exit_threshold
        )
        self._stale_count = 0
        self._rebuilt = False  # 本轮僵死周期是否已软重建过
        self._rebuild_attempts = 0
        self._source_unavailable_count = 0  # 探针连续失败 (可能探针自己池僵死)
        self._last_probe_ok = True
        self._last_probe_msg = "init"
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while True:
            await self._run_once()
            await asyncio.sleep(self._interval_s)

    async def _run_once(self, *, is_post_rebuild_verify: bool = False) -> None:
        """单轮探测; 抽出来便于重建后立即再跑一轮验证。

        is_post_rebuild_verify=True 时表示这是重建后的验证探测, 不再触发重建
        (避免无限递归), 只验证恢复与否。
        """
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._probe), timeout=self._probe_timeout_s
            )
            # 成功探测: 计数归零, 重建标记复位
            if self._stale_count > 0 or self._rebuilt:
                logger.info(
                    "quote-health: 恢复正常 (stale=%d, rebuilds=%d)",
                    self._stale_count,
                    self._rebuild_attempts,
                )
            self._stale_count = 0
            self._rebuilt = False
            self._source_unavailable_count = 0
            self._last_probe_ok = True
            self._last_probe_msg = "ok"
        except asyncio.CancelledError:
            raise
        except QuotePoolStale as exc:
            self._stale_count += 1
            self._last_probe_ok = False
            self._last_probe_msg = str(exc)
            logger.warning(
                "quote-health: 池僵死信号 %d/%d (rebuild at %d, exit at %d): %s",
                self._stale_count,
                self._exit_threshold,
                self._rebuild_threshold,
                self._exit_threshold,
                exc,
            )
            if (
                not is_post_rebuild_verify
                and self._stale_count >= self._rebuild_threshold
                and not self._rebuilt
            ):
                try:
                    self._rebuild_cb()
                    self._rebuilt = True
                    self._rebuild_attempts += 1
                    # 重建后立即再探一次, 加快恢复确认 (不等下一轮 60s)
                    await asyncio.sleep(2.0)  # 给 client 重建一点时间
                    await self._run_once(is_post_rebuild_verify=True)
                    return
                except Exception as e:  # noqa: BLE001
                    logger.error("quote-health: 软重建失败: %r", e)
            if self._stale_count >= self._exit_threshold:
                logger.critical(
                    "quote-health: 软恢复无效 (stale=%d, rebuilds=%d); 退出交 supervisor",
                    self._stale_count,
                    self._rebuild_attempts,
                )
                self._exit_cb(70)
                return
        except SourceUnavailable as exc:
            # 源/网络问题, 不重建行情客户端 (重建无用), 也不计入僵死计数
            self._source_unavailable_count += 1
            self._last_probe_ok = False
            self._last_probe_msg = str(exc)
            logger.info("quote-health: 源不可达, 不重建: %s", exc)
            # 探针连续失败 N 次 → 探针客户端可能自己也池僵死了, 重建探针
            if (
                not is_post_rebuild_verify
                and self._probe_rebuild_cb is not None
                and self._source_unavailable_count >= self._rebuild_threshold
            ):
                logger.warning(
                    "quote-health: 探针连续失败 %d 次, 重建探针客户端",
                    self._source_unavailable_count,
                )
                try:
                    self._probe_rebuild_cb()
                    self._source_unavailable_count = 0
                except Exception as e:  # noqa: BLE001
                    logger.error("quote-health: 探针重建失败: %r", e)
        except Exception as exc:  # 探测超时等普通异常, 保守计为僵死 (不含 KeyboardInterrupt/SystemExit)
            self._stale_count += 1
            self._last_probe_ok = False
            self._last_probe_msg = repr(exc)
            logger.warning(
                "quote-health: 探测异常 %d/%d: %r",
                self._stale_count,
                self._exit_threshold,
                exc,
            )
            if self._stale_count >= self._exit_threshold:
                logger.critical(
                    "quote-health: 持续异常退出 (stale=%d)", self._stale_count
                )
                self._exit_cb(70)
                return

    def status(self) -> dict:
        """暴露 watcher 运行状态, 供 /api/health/quote 端点查询。"""
        return {
            "enabled": self._task is not None and not self._task.done(),
            "stale_count": self._stale_count,
            "rebuild_attempts": self._rebuild_attempts,
            "rebuilt_this_cycle": self._rebuilt,
            "source_unavailable_count": self._source_unavailable_count,
            "last_probe_ok": self._last_probe_ok,
            "last_probe_msg": self._last_probe_msg,
            "rebuild_threshold": self._rebuild_threshold,
            "exit_threshold": self._exit_threshold,
            "interval_s": self._interval_s,
        }

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="quote-health-watcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def start_quote_health_watcher(app_state) -> QuoteHealthWatcher | None:
    """lifespan 启动钩子; 返回实例挂 app.state.quote_watcher。

    仅当启动时 provider 是 fuyao 才启动 watcher (探针端点扶摇专属)。
    运行中切到其他 provider 时, quote_probe 内动态读 preferences 会自动跳过探测
    (watcher 空转, 切回 fuyao 立即恢复, 无需重启 watcher)。
    """
    if not settings.quote_health_enabled:
        return None
    qs = getattr(app_state, "quote_service", None)
    if qs is None:
        return None
    mining_manager = getattr(app_state, "mining_manager", None)
    # 仅 fuyao provider 启用 (探针端点扶摇专属)
    from app.services import preferences

    if preferences.get_realtime_data_provider() != "fuyao":
        return None
    try:
        from app.plugins.fuyao import client as fuyao_client
        from app.plugins.fuyao.provider import get_api_key

        api_key = get_api_key()
    except Exception:  # noqa: BLE001
        return None
    if not api_key:
        return None

    # 探针客户端 holder: 可重建 (探针长期不重建也可能池僵死)
    probe_holder: dict[str, httpx.Client] = {
        "client": httpx.Client(
            base_url=fuyao_client.BASE_URL,
            headers={"X-api-key": api_key},
            timeout=10.0,
        )
    }

    def rebuild_probe_client() -> None:
        """重建探针 httpx 客户端 (探针自己池僵死时的自愈)。"""
        old = probe_holder["client"]
        probe_holder["client"] = httpx.Client(
            base_url=fuyao_client.BASE_URL,
            headers={"X-api-key": api_key},
            timeout=10.0,
        )
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass

    watcher = QuoteHealthWatcher(
        lambda: quote_probe(qs, probe_holder["client"], mining_manager),
        rebuild_cb=qs.rebuild_realtime_client,
        exit_cb=lambda code: os._exit(code),
        probe_rebuild_cb=rebuild_probe_client,
    )
    watcher.start()
    logger.info(
        "quote-health-watcher 已启动 (interval=%ss, rebuild=%d, exit=%d)",
        settings.quote_health_interval_s,
        settings.quote_health_rebuild_threshold,
        settings.quote_health_exit_threshold,
    )
    return watcher
