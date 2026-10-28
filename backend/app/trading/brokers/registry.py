"""券商适配器注册表 — broker 名称 → BrokerAdapter 工厂/实例的映射。

设计:
  - 单例式: 进程内只有一个 registry (模块级 _REGISTRY)。
  - 注册: broker 名称 (与 broker_accounts.broker 字段一致) → 工厂函数
    (工厂接收 account_id 返回 BrokerAdapter 实例, 因为同 broker 可能多账户)。
  - 默认注册 MockBroker (测试与影子交易)。
  - 真实适配器 (xtquant/futu/ibkr) 在扩展 startup 时按需注册。

调度器通过 get_adapter(account_id) 拿到对应 broker 的适配器实例,
拉取 broker 状态用于对账。无注册适配器时返回 None (对账跳过, 不抛)。
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from app.trading.brokers.base import BrokerAdapter
from app.trading.brokers.mock import MockBrokerAdapter, MockConfig

logger = logging.getLogger(__name__)

# broker 名称 → 工厂函数 (factory(account_id) → BrokerAdapter)
AdapterFactory = Callable[[str], BrokerAdapter]
_REGISTRY: dict[str, AdapterFactory] = {}
# 已创建的实例缓存 (account_id → BrokerAdapter); 同账户复用同一实例
_INSTANCES: dict[str, BrokerAdapter] = {}


def register(broker_name: str, factory: AdapterFactory) -> None:
    """注册 broker 适配器工厂。重复注册覆盖 (便于热替换)。"""
    _REGISTRY[broker_name] = factory
    logger.info("broker adapter registered: %s", broker_name)


def unregister(broker_name: str) -> None:
    """取消注册 broker 适配器。"""
    _REGISTRY.pop(broker_name, None)
    # 清理该 broker 的实例缓存
    to_remove = [acc for acc, adapter in _INSTANCES.items() if adapter.name == broker_name]
    for acc in to_remove:
        _INSTANCES.pop(acc, None)


def get_adapter(broker_name: str, account_id: str) -> BrokerAdapter | None:
    """获取某账户的 broker 适配器实例 (缓存复用)。

    无注册工厂时返回 None (对账跳过)。
    """
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
    except Exception:  # noqa: BLE001 — 适配器构造失败不阻塞对账
        logger.warning("broker adapter factory failed for %s/%s", broker_name, account_id, exc_info=True)
        return None


def list_registered() -> list[str]:
    """已注册的 broker 名称列表。"""
    return sorted(_REGISTRY.keys())


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
register("mock", _mock_factory)