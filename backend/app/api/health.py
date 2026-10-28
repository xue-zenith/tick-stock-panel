"""健康检查端点。

暴露 quote_health_watcher 运行状态, 便于运维查看自愈历史 (软重建次数、僵死计数等)。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/quote")
def quote_health(request: Request) -> dict:
    """返回 QuoteHealthWatcher 运行状态。

    若 watcher 未启动 (provider 非 fuyao / 配置关闭), 返回 enabled=False。
    """
    watcher = getattr(request.app.state, "quote_watcher", None)
    if watcher is None:
        return {"enabled": False, "reason": "quote_health_watcher not started"}
    return watcher.status()
