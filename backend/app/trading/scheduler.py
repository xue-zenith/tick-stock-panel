"""实盘交易定时对账调度器 — 周期性拉取券商状态与本地 OMS 对账。

设计 (镜像 financial_sync.FinancialScheduler 的类 + asyncio 任务模式):
  - 自包含 asyncio 后台任务, 不依赖核心 daily_pipeline 调度器 (避免修改核心热点)。
  - 启动时扫描所有 broker_accounts, 为每个账户按 broker 名称取适配器。
  - 无适配器或适配器构造失败时跳过该账户 (软失败, 不阻塞循环)。
  - 频率 (对齐分析文档 §13.2):
      - 未完成订单对账: orders_interval_s (默认 30s; 文档建议 2-5s 但无真实券商时意义不大)
      - 资金/持仓快照: positions_interval_s (默认 60s; 文档建议 15-30s)
      - 日终完整对账: daily at 15:05 北京时间 (收盘集合竞价后)
  - 启动即跑一次 (boot_reconcile), 确保启动后状态干净才能 arm。
  - 脏对账自动把账户降为 DEGRADED (oms.trigger_reconciliation 已做)。

不在本版实现:
  - 回调断线恢复后立即对账 (由 broker adapter 的 reconnect 钩子触发)。
  - 多账户并发对账 (本版串行, 个人单机账户数少)。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from app.market_time import cn_now
from app.trading import broker_service, oms, store
from app.trading.brokers import registry as broker_registry

logger = logging.getLogger(__name__)


class ReconciliationScheduler:
    """定时对账调度器 — 自包含 asyncio 后台任务。

    生命周期:
      start(data_dir) → 后台任务运行
      shutdown()      → 取消任务, 等待退出
    """

    def __init__(
        self,
        *,
        orders_interval_s: float = 30.0,
        positions_interval_s: float = 60.0,
        daily_hour: int = 15,
        daily_minute: int = 5,
    ) -> None:
        self._orders_interval = orders_interval_s
        self._positions_interval = positions_interval_s
        self._daily_hour = daily_hour
        self._daily_minute = daily_minute
        self._data_dir: Path | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_orders_recon: dict[str, str] = {}   # account_id → iso
        self._last_positions_recon: dict[str, str] = {}
        self._last_daily_recon: str | None = None      # iso date (yyyy-mm-dd)

    @property
    def running(self) -> bool:
        return self._running

    def start(self, data_dir: Path) -> None:
        """启动后台对账任务。幂等: 已运行时不重复启动。"""
        if self._running:
            return
        self._data_dir = data_dir
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "ReconciliationScheduler started (orders=%ss, positions=%ss, daily=%02d:%02d)",
            self._orders_interval, self._positions_interval,
            self._daily_hour, self._daily_minute,
        )

    async def shutdown(self) -> None:
        """优雅停止。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=5.0)
            self._task = None
        logger.info("ReconciliationScheduler stopped")

    async def _run_loop(self) -> None:
        """主循环: 启动即跑一次, 之后按频率周期对账。"""
        assert self._data_dir is not None
        # 启动即对账一次 (boot reconcile): 确保启动后状态干净
        await self._reconcile_all(reason="boot")

        orders_elapsed = 0.0
        positions_elapsed = 0.0
        tick = 1.0  # 循环粒度 1 秒

        while self._running:
            await asyncio.sleep(tick)
            orders_elapsed += tick
            positions_elapsed += tick

            if orders_elapsed >= self._orders_interval:
                await self._reconcile_all(reason="orders", fetch_positions=False)
                orders_elapsed = 0.0

            if positions_elapsed >= self._positions_interval:
                await self._reconcile_all(reason="positions", fetch_positions=True)
                positions_elapsed = 0.0

            # 日终对账 (收盘后 15:05)
            now = cn_now()
            if (now.hour == self._daily_hour and now.minute == self._daily_minute
                    and self._last_daily_recon != now.date().isoformat()):
                await self._reconcile_all(reason="daily", fetch_positions=True)
                self._last_daily_recon = now.date().isoformat()

    async def _reconcile_all(self, *, reason: str, fetch_positions: bool = True) -> None:
        """对所有 broker 账户跑一次对账。串行 (个人单机账户数少)。

        reason: boot / orders / positions / daily — 写入 system_events 审计。
        fetch_positions=False 时只对账订单 (轻量), True 时拉持仓+资金 (重)。
        """
        assert self._data_dir is not None
        try:
            store.init_db(self._data_dir)
            accounts = store.list_accounts(self._data_dir)
        except Exception:  # noqa: BLE001 — DB 读失败不崩循环
            logger.warning("reconcile_all: list_accounts failed (%s)", reason, exc_info=True)
            return

        for acc in accounts:
            account_id = acc["account_id"]
            broker_name = acc["broker"]
            try:
                await self._reconcile_account(account_id, broker_name, reason, fetch_positions)
            except Exception:  # noqa: BLE001 — 单账户对账失败不阻塞其他账户
                logger.warning(
                    "reconcile failed for %s (%s, reason=%s)",
                    account_id, broker_name, reason, exc_info=True,
                )

    async def _reconcile_account(
        self,
        account_id: str,
        broker_name: str,
        reason: str,
        fetch_positions: bool,
    ) -> None:
        """对单个账户拉取 broker 状态并触发对账。"""
        assert self._data_dir is not None
        try:
            # 券商 SDK 多为同步阻塞接口，放到 worker thread，不能卡住 asyncio 主循环。
            adapter = await asyncio.to_thread(
                broker_service.require_connected,
                self._data_dir, broker_name, account_id,
            )
            pulled = await asyncio.to_thread(
                broker_service.pull, adapter, include_positions=fetch_positions,
            )
            await asyncio.to_thread(
                broker_service.ingest_executions,
                self._data_dir, account_id, broker_name, list(pulled["executions"]),
            )
        except (broker_service.BrokerUnavailableError, broker_service.BrokerSyncError) as exc:
            acc = store.get_account(self._data_dir, account_id)
            if acc is not None and acc["mode"] in ("armed_manual", "armed_auto"):
                store.set_account_mode(self._data_dir, account_id, "degraded")
            store.log_system_event(self._data_dir, "scheduled_reconcile_failed", {
                "account_id": account_id, "reason": reason, "error": str(exc),
            })
            return

        result = oms.trigger_reconciliation(
            self._data_dir, account_id,
            list(pulled["orders"]),
            None if pulled["positions"] is None else list(pulled["positions"]),
            None if pulled["account"] is None else dict(pulled["account"])["cash"],
        )

        if fetch_positions:
            acc = store.get_account(self._data_dir, account_id)
            await asyncio.to_thread(
                broker_service.persist_snapshot,
                self._data_dir, account_id,
                acc["mode"] if acc is not None else "disarmed",
                pulled,
            )

        # 记录对账时间 (供 health 端点展示)
        ts = cn_now().isoformat()
        if fetch_positions:
            self._last_positions_recon[account_id] = ts
        else:
            self._last_orders_recon[account_id] = ts

        store.log_system_event(self._data_dir, "scheduled_reconcile", {
            "account_id": account_id, "reason": reason,
            "status": result["status"], "diff_count": result["diff_count"],
        })

    def status(self) -> dict:
        """调度器状态 (供 /api/live/health 展示)。"""
        return {
            "running": self._running,
            "orders_interval_s": self._orders_interval,
            "positions_interval_s": self._positions_interval,
            "daily_at": f"{self._daily_hour:02d}:{self._daily_minute:02d}",
            "last_orders_recon": dict(self._last_orders_recon),
            "last_positions_recon": dict(self._last_positions_recon),
            "last_daily_recon": self._last_daily_recon,
            "registered_brokers": broker_registry.list_registered(),
        }


# 模块级单例 (镜像 financial_scheduler 的 app.state.financial_scheduler 模式)
_scheduler: ReconciliationScheduler | None = None


def get_scheduler() -> ReconciliationScheduler | None:
    """获取已启动的调度器单例 (供 routes / startup 使用)。"""
    return _scheduler


def start_scheduler(data_dir: Path, **kwargs) -> ReconciliationScheduler:
    """启动调度器单例。幂等: 已启动则返回现有实例。"""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    _scheduler = ReconciliationScheduler(**kwargs)
    _scheduler.start(data_dir)
    return _scheduler


async def stop_scheduler() -> None:
    """停止调度器单例 (应用关闭时调用)。"""
    global _scheduler
    if _scheduler is not None:
        await _scheduler.shutdown()
    _scheduler = None
