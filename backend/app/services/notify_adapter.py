"""系统通知适配器 — 三平台原生通知中心。

职责: 把后端产生的告警事件推送到操作系统通知中心。
     窗口最小化 / 被遮挡 / 后台运行时都能弹通知 (不依赖前端 WebView)。

平台实现:
  - Windows: winotify (进现代操作中心, 支持图标)
  - macOS:   terminal-notifier (优先, 通知标题/分组更清晰) → osascript 兜底
  - Linux:   notify-send (系统已内置) / plyer 兜底

设计: 失败静默降级, 绝不因通知失败阻断告警主流程 (落盘 / SSE 推送)。
     通知去重不在本层做, 复用 MonitorRuleEngine 的 cooldown 逻辑。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 单次通知最长字符 (避免某些平台截断报错)
_MAX_LEN = 200

# 避免重复探测平台能力, 缓存一次
_backend_cache: str | None = None

# macOS 通知应用名 (terminal-notifier 的 -sender 已不支持, 但 title 统一品牌)
_APP_TITLE = "TickFlow"


def _detect_backend() -> str | None:
    """探测当前平台可用的通知后端。"""
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache if _backend_cache != "none" else None

    backend = None
    if sys.platform == "win32":
        try:
            import winotify  # type: ignore[import-not-found]  # noqa: F401

            backend = "winotify"
        except ImportError:
            logger.debug("winotify 不可用, Windows 通知降级")
            backend = None
    elif sys.platform == "darwin":
        # 优先 terminal-notifier (标题/分组更清晰), 不可用则 osascript 兜底。
        # terminal-notifier 需在系统设置 → 通知里授权, 未授权时自动降级。
        if shutil.which("terminal-notifier"):
            backend = "terminal-notifier"
        else:
            backend = "osascript"
    elif sys.platform.startswith("linux"):
        backend = "notify-send"

    _backend_cache = backend or "none"
    return backend


def _truncate(text: str) -> str:
    """截断超长文本, 避免平台通知上限报错。"""
    text = (text or "").strip()
    return text[:_MAX_LEN] + ("…" if len(text) > _MAX_LEN else "")


def notify(title: str, message: str, icon: Path | None = None) -> bool:
    """推送一条系统通知。

    Args:
        title:   通知标题
        message: 通知正文
        icon:    可选图标路径 (部分平台支持)

    Returns:
        True=成功送达, False=失败或无可用后端。
        失败静默, 不抛异常 (通知是辅助通道, 不能阻断告警主流程)。
    """
    title = _truncate(title)
    message = _truncate(message)
    if not title:
        return False

    backend = _detect_backend()
    if backend is None:
        return False

    try:
        if backend == "winotify":
            return _notify_winotify(title, message)
        if backend == "terminal-notifier":
            return _notify_terminal_notifier(title, message)
        if backend == "osascript":
            return _notify_osascript(title, message)
        if backend == "notify-send":
            return _notify_notify_send(title, message, icon)
    except Exception as e:  # noqa: BLE001
        logger.debug("系统通知失败 (%s): %s", backend, e)
        return False

    return False


def _notify_winotify(title: str, message: str) -> bool:
    """Windows 通知 (winotify) — 进现代操作中心。"""
    from winotify import Notifier  # type: ignore[import-not-found]

    Notifier().create_notification(
        title=title,
        msg=message,
        # winotify 要求 duration 为 "short" 或 "long"
        duration="short",
        # 无可点击动作 (桌面版不实现"点击回到窗口"的复杂交互)
    ).show()
    return True


def _notify_terminal_notifier(title: str, message: str) -> bool:
    """macOS 通知 (terminal-notifier) — 优先使用, 标题/分组更清晰。

    terminal-notifier 的 -sender 在新版 macOS 已不支持, 通知归属仍为
    "terminal-notifier" (比 osascript 的"脚本编辑器"更直观)。需在系统
    设置 → 通知里授权; 未授权时返回 False, 由上层回退到 osascript。
    """
    result = subprocess.run(  # noqa: S603, S607
        [
            "terminal-notifier",
            "-title", f"{_APP_TITLE} · {title}",
            "-message", message,
            "-sound", "default",
            # 按标题分组, 避免同源通知刷屏
            "-group", title[:50],
        ],
        capture_output=True,
        timeout=5,
    )
    if result.returncode != 0:
        # 未授权或通知被禁: 降级到 osascript
        logger.debug("terminal-notifier 失败, 降级 osascript: %s",
                     result.stderr.decode("utf-8", "ignore")[:100])
        _backend_cache = "osascript"
        return _notify_osascript(title, message)
    return True


def _notify_osascript(title: str, message: str) -> bool:
    """macOS 通知 (osascript) — 调用系统 AppleScript。

    通知归属显示为"脚本编辑器" (macOS 把 osascript 发出的通知归到此应用),
    无法更改; 但标题统一带 TickFlow 前缀便于辨识。
    """
    # 转义双引号, 避免 AppleScript 注入
    safe_title = f'{_APP_TITLE} · {title}'.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = (
        f'display notification "{safe_msg}" with title "{safe_title}"'
    )
    result = subprocess.run(  # noqa: S603, S607
        ["osascript", "-e", script],
        capture_output=True,
        timeout=5,
    )
    return result.returncode == 0


def _notify_notify_send(title: str, message: str, icon: Path | None) -> bool:
    """Linux 通知 (notify-send) — freedesktop.org 标准。"""
    args = ["notify-send", f"{_APP_TITLE} · {title}"]
    if icon and Path(icon).exists():
        args.extend(["--icon", str(icon)])
    args.append(message)
    result = subprocess.run(  # noqa: S603
        args,
        capture_output=True,
        timeout=5,
    )
    return result.returncode == 0
