"""真实交易扩展 — L2 后端扩展 (custom module + BackendExtensionRegistrar).

通过注册独立 FastAPI 路由 /api/live/* 暴露 OMS 能力, 不修改核心路由。
删除本目录即可整体卸载, 不影响核心功能 (镜像 assistant 扩展)。

不开放 live:trade scope (api_gateway 未加规则), 端点仅 UI 会话可达。
AI 层面只到 L0 (observe) + L1 (propose): 所有订单必须人工审批。
"""
from __future__ import annotations

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)

EXTENSION_ID = "live.trading"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from app.custom.live_trading.routes import build_router

    registrar.include_router(build_router())


def startup(context: ExtensionContext) -> None:
    # 首次启动时初始化 SQLite schema (幂等)
    from app.trading import store
    store.init_db(context.data_dir)

    # 只注册当前环境真正安装的厂商 SDK；未安装时保留 catalog 接入位，
    # 但不允许创建账户或下单。
    from app.trading.brokers import registry as _broker_registry
    _broker_registry.register_optional_vendor_adapters(context.data_dir)

    # 启动定时对账调度器 (后台 asyncio 任务, 周期性拉取 broker 状态对账)
    # 软失败: 调度器启动失败不阻止主程序 (镜像 financial_scheduler 启动模式)
    from app.trading import scheduler as _scheduler
    try:
        _scheduler.start_scheduler(context.data_dir)
    except Exception as e:  # noqa: BLE001 — 调度器是附加能力, 不阻塞核心启动
        import logging
        logging.getLogger(__name__).warning("ReconciliationScheduler not started: %s", e)


async def shutdown(context: ExtensionContext) -> None:
    """停止后台对账并断开所有券商连接，避免重载/退出后遗留任务。"""
    del context
    from app.trading import scheduler as _scheduler
    from app.trading.brokers import registry as broker_registry

    await _scheduler.stop_scheduler()
    broker_registry.clear_instances()
