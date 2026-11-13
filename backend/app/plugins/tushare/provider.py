"""Tushare Pro 历史分钟 K 备用 Provider。

只实现分钟数据，避免改变现有 Fuyao 日 K/财务和 TdxQuant 实时路由。
接口返回先在边界层转换为项目 canonical schema，再交给现有安全流式落盘。
"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from datetime import datetime

import httpx
import polars as pl

from app.data_providers.base import ProviderRequestError
from app.data_providers.minute_normalizer import CANONICAL_MINUTE_COLS, _normalize_minute

logger = logging.getLogger(__name__)

API_KEY_ENV = "TUSHARE_TOKEN"
SECRETS_FIELD = "tushare_api_key"
API_URL = "https://api.tushare.pro"
_REQUEST_INTERVAL_S = 0.25
_RETRIES = 3


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    if get_api_key():
        return True, "ok"
    return False, f"缺少 Token(可在下方输入框配置,或设置环境变量 {API_KEY_ENV})"


def _request(token: str, api_name: str, params: dict) -> dict:
    payload = {"api_name": api_name, "token": token, "params": params, "fields": ""}
    last_error = "未知错误"
    with httpx.Client(timeout=30.0) as client:
        for attempt in range(_RETRIES):
            try:
                response = client.post(API_URL, json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt + 1 < _RETRIES:
                        time.sleep(min(8.0, 2.0**attempt) + random.uniform(0, 0.25))
                        continue
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
                if attempt + 1 < _RETRIES:
                    time.sleep(min(8.0, 2.0**attempt) + random.uniform(0, 0.25))
                    continue
                raise ProviderRequestError(f"Tushare 请求失败: {last_error}") from exc
            if result.get("code") not in (0, None):
                message = str(result.get("msg") or "未知错误")
                raise ProviderRequestError(f"Tushare {api_name} 失败: {message}")
            return result
    raise ProviderRequestError(f"Tushare 请求失败: {last_error}")


def probe_api_key(api_key: str) -> tuple[bool, str]:
    token = (api_key or "").strip()
    if len(token) < 16:
        return False, "Tushare Token 格式过短"
    try:
        result = _request(token, "trade_cal", {"exchange": "SSE", "limit": 1})
        if result.get("data") is None:
            return False, "Token 可连接但未返回权限数据"
        return True, "ok(基础权限正常；分钟权限需单独验证)"
    except ProviderRequestError as exc:
        return False, str(exc)


def _format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _frame(data: dict, symbol: str) -> pl.DataFrame:
    body = data.get("data") or {}
    fields = body.get("fields") or []
    items = body.get("items") or []
    if not fields or not items:
        return pl.DataFrame()
    rows = [dict(zip(fields, item, strict=False)) for item in items]
    df = pl.DataFrame(rows)
    if "ts_code" not in df.columns:
        df = df.with_columns(pl.lit(symbol).alias("ts_code"))
    # Tushare vol 为股,项目分钟契约为手; amount 已是元。
    if "vol" in df.columns:
        df = df.with_columns((pl.col("vol").cast(pl.Float64, strict=False) / 100.0).alias("vol"))
    return _normalize_minute(df, source="tushare")


class TushareProvider:
    name = "tushare"
    builtin = True

    def __init__(self) -> None:
        self._last_request_at = 0.0

    def close(self) -> None:
        return None

    def _pace(self) -> None:
        wait = _REQUEST_INTERVAL_S - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def iter_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        if not symbols or asset_type != "stock" or freq != "1m":
            return
        token = get_api_key()
        if not token:
            raise ProviderRequestError("Tushare Token 未配置")
        total = len(symbols)
        for index, symbol in enumerate(symbols, start=1):
            self._pace()
            result = _request(token, "pro_bar", {
                "ts_code": symbol,
                "freq": "1min",
                "start_date": _format_time(start_time),
                "end_date": _format_time(end_time),
                "adj": "" ,
            })
            frame = _frame(result, symbol)
            if not frame.is_empty():
                yield frame.select([c for c in CANONICAL_MINUTE_COLS if c in frame.columns])
            if on_chunk_done:
                on_chunk_done(index, total)

