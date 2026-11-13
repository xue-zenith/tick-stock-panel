from __future__ import annotations

from datetime import datetime

import pytest

from app.data_providers.base import ProviderRequestError
from app.plugins.tushare import provider


def _payload():
    return {
        "code": 0,
        "data": {
            "fields": ["ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount"],
            "items": [["000001.SZ", "2026-11-13 09:31:00", 10, 11, 9, 10.5, 12345, 987654.0]],
        },
    }


def test_frame_normalizes_tushare_units_and_schema():
    frame = provider._frame(_payload(), "000001.SZ")
    assert frame.columns == ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
    assert frame["symbol"].to_list() == ["000001.SZ"]
    assert frame["volume"].to_list() == [123.45]
    assert frame["datetime"].to_list() == [datetime(2026, 11, 13, 9, 31)]


def test_iter_minute_is_streaming_and_reports_progress(monkeypatch):
    monkeypatch.setattr(provider, "get_api_key", lambda: "t" * 32)
    monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: _payload())
    monkeypatch.setattr(provider.time, "sleep", lambda _seconds: None)
    p = provider.TushareProvider()
    progress = []
    frames = list(p.iter_minute(
        ["000001.SZ"], datetime(2026, 11, 13, 9, 30), datetime(2026, 11, 13, 10),
        on_chunk_done=lambda cur, total: progress.append((cur, total)),
    ))
    assert len(frames) == 1
    assert frames[0].height == 1
    assert progress == [(1, 1)]


def test_iter_minute_requires_token(monkeypatch):
    monkeypatch.setattr(provider, "get_api_key", lambda: "")
    with pytest.raises(ProviderRequestError, match="Token 未配置"):
        next(provider.TushareProvider().iter_minute(
            ["000001.SZ"], datetime(2026, 11, 13, 9, 30), datetime(2026, 11, 13, 10),
        ))
