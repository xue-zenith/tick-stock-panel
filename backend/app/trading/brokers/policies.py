"""券商专用策略构件：能力声明、状态映射、交易时段和回调验签。"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from app.trading.contracts import OrderState


@dataclass(frozen=True)
class BrokerCapabilities:
    """适配器能力必须显式声明，OMS 不根据券商名称猜测。"""

    asset_types: tuple[str, ...] = ("stock",)
    time_in_force: tuple[str, ...] = ("day",)
    supports_market_orders: bool = False
    supports_partial_fills: bool = True
    supports_cancel_confirmation: bool = True
    supports_push_callbacks: bool = False
    callback_signature: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class BrokerStatusMapper:
    """把厂商状态枚举严格映射为平台状态；未知值一律 UNKNOWN。"""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = {str(k).strip().lower(): str(v) for k, v in mapping.items()}
        valid = {state.value for state in OrderState}
        invalid = set(self._mapping.values()) - valid
        if invalid:
            raise ValueError(f"非法平台订单状态: {sorted(invalid)}")

    def normalize(self, raw_state: object) -> str:
        key = str(raw_state or "").strip().lower()
        return self._mapping.get(key, OrderState.UNKNOWN.value)


@dataclass(frozen=True)
class TradingSessionPolicy:
    """适配器侧交易时段预检；券商服务端仍是最终事实源。"""

    timezone: str = "Asia/Shanghai"
    sessions: tuple[tuple[str, str], ...] = (("09:30", "11:30"), ("13:00", "15:00"))
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)

    def allows(self, now: datetime | None = None) -> bool:
        zone = ZoneInfo(self.timezone)
        current = now.astimezone(zone) if now is not None else datetime.now(zone)
        if current.weekday() not in self.weekdays:
            return False
        clock = current.time().replace(tzinfo=None)
        return any(
            _parse_clock(start) <= clock <= _parse_clock(end)
            for start, end in self.sessions
        )


@dataclass(frozen=True)
class HmacSha256CallbackVerifier:
    """通用 HMAC-SHA256 回调验签，含时间窗防重放。"""

    secret: bytes
    max_skew_seconds: int = 300
    prefix: str = "sha256="

    def verify(
        self,
        payload: bytes,
        *,
        signature: str,
        timestamp: str,
        now: float | None = None,
    ) -> bool:
        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        current = time.time() if now is None else now
        if abs(current - ts) > self.max_skew_seconds:
            return False
        signed = timestamp.encode("utf-8") + b"." + payload
        expected = self.prefix + hmac.new(self.secret, signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(signature))


def _parse_clock(value: str) -> dt_time:
    return dt_time.fromisoformat(value)
