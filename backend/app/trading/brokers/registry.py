"""券商适配器注册表 — broker 名称 → BrokerAdapter 工厂/实例的映射。

设计:
  - 单例式: 进程内只有一个 registry (模块级 _REGISTRY)。
  - 注册: broker 名称 (与 broker_accounts.broker 字段一致) → 工厂函数
    (工厂接收 account_id 返回 BrokerAdapter 实例, 因为同 broker 可能多账户)。
  - 默认注册 MockBroker (测试与影子交易)。
  - 真实适配器 (xtquant/futu/ibkr) 在扩展 startup 时按需注册。

调度器通过 get_adapter(broker_name, account_id) 拿到对应实例并拉取券商状态。
账户创建阶段拒绝未注册适配器；运行时适配器不可用时对账失败并阻止 Arm，
绝不能用空数据伪造 clean 对账。
"""
from __future__ import annotations

import importlib.util
import logging
import threading
from collections.abc import Callable

from app.trading.brokers.base import BrokerAdapter
from app.trading.brokers.mock import MockBrokerAdapter, MockConfig
from app.trading.brokers.policies import BrokerCapabilities

logger = logging.getLogger(__name__)

# broker 名称 → 工厂函数 (factory(account_id) → BrokerAdapter)
AdapterFactory = Callable[[str], BrokerAdapter]
_REGISTRY: dict[str, AdapterFactory] = {}
# 已创建的实例缓存 (account_id → BrokerAdapter); 同账户复用同一实例
_INSTANCES: dict[str, BrokerAdapter] = {}
_META: dict[str, dict[str, object]] = {}
_LOCK = threading.RLock()

# 已支持的厂商接入位。这里描述“平台可以接入什么”，不代表 SDK 已安装或
# 账户已连接；只有 register() 成功后，registered/available 才会变成 True。
_SUPPORTED_BROKERS: dict[str, dict[str, object]] = {
    "qmt": {
        "label": "QMT / XtQuant",
        "kind": "real",
        "setup_status": "sdk_required",
        "requires_sdk": True,
        "requires_account_env": True,
        "requires_vendor_api": True,
        "requirements": [
            "安装并授权 QMT 交易客户端或 XtQuant 运行环境",
            "配置 QMT 账户路径、资金账号与交易市场权限",
            "在后端部署 XtQuant 适配器并注册 BrokerAdapter",
        ],
    },
    "futu": {
        "label": "富途 OpenD",
        "kind": "real",
        "setup_status": "sdk_required",
        "requires_sdk": True,
        "requires_account_env": True,
        "requires_vendor_api": True,
        "requirements": [
            "安装并运行富途 OpenD（或厂商 SDK 运行环境）",
            "配置 host、port、账户与市场交易权限",
            "在后端部署富途适配器并完成回调/签名配置",
        ],
    },
    "ibkr": {
        "label": "Interactive Brokers / TWS",
        "kind": "real",
        "setup_status": "sdk_required",
        "requires_sdk": True,
        "requires_account_env": True,
        "requires_vendor_api": True,
        "requirements": [
            "安装 TWS 或 IB Gateway，并开启 API 访问",
            "配置 host、port、clientId、账户与交易权限",
            "在后端部署 IBKR API 适配器并验证订单回报",
        ],
    },
}


def register(
    broker_name: str,
    factory: AdapterFactory,
    *,
    label: str | None = None,
    kind: str = "real",
    capabilities: BrokerCapabilities | None = None,
    requirements: list[str] | None = None,
) -> None:
    """注册 broker 适配器工厂。

    重复注册会先断开并清除该 broker 的旧实例，避免测试/二开热替换后继续
    复用旧连接。凭据由适配器工厂从安全配置读取，不进入本注册表或 OMS 数据库。
    """
    if not broker_name or not callable(factory):
        raise ValueError("broker_name 与 factory 不能为空")
    with _LOCK:
        _disconnect_instances(broker_name)
        _REGISTRY[broker_name] = factory
        _META[broker_name] = {
            "label": label or broker_name,
            "kind": kind,
            "capabilities": (capabilities or BrokerCapabilities()).to_dict(),
            "requirements": requirements or [],
        }
    logger.info("broker adapter registered: %s", broker_name)


def unregister(broker_name: str) -> None:
    """取消注册 broker 适配器。"""
    with _LOCK:
        _REGISTRY.pop(broker_name, None)
        _META.pop(broker_name, None)
        _disconnect_instances(broker_name)


def get_adapter(broker_name: str, account_id: str) -> BrokerAdapter | None:
    """获取某账户的 broker 适配器实例 (缓存复用)。

    无注册工厂或构造失败时返回 None；调用方必须 fail-closed。
    """
    with _LOCK:
        factory = _REGISTRY.get(broker_name)
        if factory is None:
            return None
        # 缓存: 同账户复用实例 (避免每次对账都新建连接)
        cache_key = f"{broker_name}:{account_id}"
        if cache_key in _INSTANCES:
            return _INSTANCES[cache_key]
        try:
            adapter = factory(account_id)
            _INSTANCES[cache_key] = adapter
            return adapter
        except Exception:  # noqa: BLE001 — 状态端点会把构造失败明确呈现为 unavailable
            logger.warning(
                "broker adapter factory failed for %s/%s",
                broker_name, account_id, exc_info=True,
            )
            return None


def get_cached_adapter(broker_name: str, account_id: str) -> BrokerAdapter | None:
    """只读已创建实例，不隐式构造或连接。"""
    with _LOCK:
        return _INSTANCES.get(f"{broker_name}:{account_id}")


def is_registered(broker_name: str) -> bool:
    with _LOCK:
        return broker_name in _REGISTRY


def catalog() -> list[dict[str, object]]:
    """返回已注册适配器与可安装厂商接入位，供前端展示真实接入要求。"""
    with _LOCK:
        names = set(_SUPPORTED_BROKERS) | set(_REGISTRY)
        rows: list[dict[str, object]] = []
        for name in sorted(names):
            meta = _SUPPORTED_BROKERS.get(name, {}) | _META.get(name, {})
            registered = name in _REGISTRY
            rows.append({
                "name": name,
                "label": meta.get("label", name),
                "kind": meta.get("kind", "real"),
                "capabilities": meta.get("capabilities", {}),
                "registered": registered,
                "available": registered,
                "setup_status": "ready" if registered else meta.get("setup_status", "adapter_required"),
                "requires_sdk": bool(meta.get("requires_sdk", False)),
                "requires_account_env": bool(meta.get("requires_account_env", False)),
                "requires_vendor_api": bool(meta.get("requires_vendor_api", False)),
                "requirements": list(meta.get("requirements", [])),
            })
        return rows


def clear_instances() -> None:
    """断开并清空实例缓存。主要用于测试隔离和应用关闭。"""
    with _LOCK:
        _disconnect_instances(None)


def _disconnect_instances(broker_name: str | None) -> None:
    keys = [
        key for key in _INSTANCES
        if broker_name is None or key.startswith(f"{broker_name}:")
    ]
    for key in keys:
        adapter = _INSTANCES.pop(key)
        try:
            adapter.disconnect()
        except Exception:  # noqa: BLE001 — 清理阶段 best-effort
            logger.debug("broker disconnect failed: %s", key, exc_info=True)


def list_registered() -> list[str]:
    """已注册的 broker 名称列表。"""
    return sorted(_REGISTRY.keys())


def register_optional_vendor_adapters(data_dir=None) -> list[str]:
    """Register only SDKs that are actually installed in this process.

    Vendor terminals remain an explicit runtime prerequisite.  A missing SDK is
    intentionally left unregistered so the UI reports ``sdk_required`` rather
    than pretending that a real account can be connected.
    """
    registered: list[str] = []
    config_rows = {}
    if data_dir is not None:
        from app.trading.brokers.credentials import GatewayConfigStore
        config_rows = GatewayConfigStore(data_dir).load()

    def config_for(broker: str, account_id: str) -> dict:
        for row in config_rows.values():
            if row.get("broker") == broker and (not row.get("account_id") or row.get("account_id") == account_id):
                return dict(row)
        return {"account_id": account_id}

    if importlib.util.find_spec("futu") is not None:
        from app.trading.brokers.futu import FutuBrokerAdapter
        register(
            "futu", lambda account_id: FutuBrokerAdapter(account_id, config_for("futu", account_id)),
            label="富途 OpenD", kind="real",
            capabilities=FutuBrokerAdapter("", {}).capabilities,
            requirements=["安装 futu-api 与 OpenD", "完成 OpenD 登录/设备验证", "确认账户与市场权限"],
        )
        registered.append("futu")
    if importlib.util.find_spec("ibapi") is not None:
        from app.trading.brokers.ibkr import IbkrBrokerAdapter
        register(
            "ibkr", lambda account_id: IbkrBrokerAdapter(account_id, config_for("ibkr", account_id)),
            label="Interactive Brokers / TWS", kind="real",
            capabilities=IbkrBrokerAdapter("", {}).capabilities,
            requirements=["安装官方 TWS API ibapi", "启动 TWS/IB Gateway 并开启 Socket API", "先使用 Paper + Read-Only 完成验证"],
        )
        registered.append("ibkr")
    # QMT remains Windows/MiniQMT-bound; register only when XtQuant is present.
    if importlib.util.find_spec("xtquant") is not None:
        from app.trading.brokers.xtquant import XtQuantBrokerAdapter
        register(
            "qmt", lambda account_id: XtQuantBrokerAdapter(account_id, config_for("qmt", account_id)),
            label="QMT / XtQuant", kind="real",
            capabilities=XtQuantBrokerAdapter("", {}).capabilities,
            requirements=["Windows 64 位 + MiniQMT/QMT", "Python 3.11 与 userdata_mini", "券商开通程序化交易权限"],
        )
        registered.append("qmt")
    return registered


def _mock_factory(account_id: str) -> MockBrokerAdapter:
    """默认 MockBroker 工厂 (测试与影子交易)。

    生产环境用 MockBroker 做对账意义不大 (它的状态与 OMS 一致),
    但它保证调度器链路端到端可验证。
    """
    return MockBrokerAdapter(
        account_id=account_id,
        initial_cash=1_000_000.0,
        config=MockConfig(latency_ms=0),
    )


# 默认注册 mock (可被真实适配器覆盖)
register(
    "mock",
    _mock_factory,
    label="MockBroker（安全演练）",
    kind="simulation",
    capabilities=BrokerCapabilities(
        asset_types=("stock", "etf"),
        time_in_force=("day", "gtc", "ioc", "fok"),
        supports_market_orders=True,
        supports_partial_fills=True,
        supports_cancel_confirmation=True,
        supports_push_callbacks=True,
        callback_signature="in-process",
    ),
)
