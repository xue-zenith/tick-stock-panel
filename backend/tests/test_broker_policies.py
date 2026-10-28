from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from zoneinfo import ZoneInfo

from app.trading.brokers.policies import (
    BrokerStatusMapper,
    HmacSha256CallbackVerifier,
    TradingSessionPolicy,
)


def test_status_mapper_is_strict_and_unknown_fails_closed():
    mapper = BrokerStatusMapper({"已报": "submitted", "部成": "partially_filled"})
    assert mapper.normalize("已报") == "submitted"
    assert mapper.normalize("厂商新增状态") == "unknown"


def test_trading_session_policy_uses_broker_timezone():
    policy = TradingSessionPolicy()
    zone = ZoneInfo("Asia/Shanghai")
    assert policy.allows(datetime(2026, 10, 28, 10, 0, tzinfo=zone)) is True
    assert policy.allows(datetime(2026, 10, 28, 12, 0, tzinfo=zone)) is False
    assert policy.allows(datetime(2026, 10, 31, 10, 0, tzinfo=zone)) is False


def test_hmac_callback_verifier_checks_signature_and_replay_window():
    verifier = HmacSha256CallbackVerifier(b"secret", max_skew_seconds=60)
    payload = b'{"trade_id":"t1"}'
    timestamp = "1000"
    digest = hmac.new(b"secret", b"1000." + payload, hashlib.sha256).hexdigest()
    signature = f"sha256={digest}"

    assert verifier.verify(
        payload, signature=signature, timestamp=timestamp, now=1020,
    ) is True
    assert verifier.verify(
        payload, signature="sha256=bad", timestamp=timestamp, now=1020,
    ) is False
    assert verifier.verify(
        payload, signature=signature, timestamp=timestamp, now=1200,
    ) is False
