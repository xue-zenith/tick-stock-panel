"""Broker gateway configuration and secret references.

Only non-secret connection metadata is stored in ``data/user_data``.  Secrets
are placed in the OS keyring when the broker extras are installed; the API never
accepts or returns plaintext credentials after the initial write.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)


class SecretStoreUnavailableError(RuntimeError):
    """The optional OS keyring dependency is not available."""


def _keyring():
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise SecretStoreUnavailableError("请安装 broker gateway 依赖: pip install keyring") from exc
    return keyring


def put_secret(namespace: str, value: str) -> str:
    if not value:
        raise ValueError("secret 不能为空")
    ref = f"keyring://tick-stock-panel/{namespace}/{secrets.token_hex(8)}"
    _keyring().set_password("tick-stock-panel", ref, value)
    return ref


def get_secret(ref: str) -> str:
    if not ref.startswith("keyring://"):
        raise SecretStoreUnavailableError("不接受明文或非 keyring secret_ref")
    value = _keyring().get_password("tick-stock-panel", ref)
    if not value:
        raise SecretStoreUnavailableError("凭据不存在或系统密钥环不可用")
    return value


def delete_secret(ref: str) -> None:
    if ref.startswith("keyring://"):
        try:
            _keyring().delete_password("tick-stock-panel", ref)
        except Exception:  # noqa: BLE001 - deletion is best effort
            logger.debug("secret deletion failed", exc_info=True)


def account_fingerprint(server_key: bytes, broker: str, account_id: str) -> str:
    """Stable non-reversible account identifier for audit/UI correlation."""
    return hmac.new(server_key, f"{broker}:{account_id}".encode(), hashlib.sha256).hexdigest()


class GatewayConfigStore:
    """Persist gateway metadata, never secret values."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "user_data" / "broker_gateways.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, dict]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def save(self, rows: dict[str, dict]) -> None:
        atomic_write_text(self.path, json.dumps(rows, ensure_ascii=False, indent=2), mode=0o600)

    def upsert(self, gateway_id: str, row: dict) -> dict:
        rows = self.load()
        current = dict(rows.get(gateway_id) or {})
        current.update({k: v for k, v in row.items() if v is not None})
        rows[gateway_id] = current
        self.save(rows)
        return current

    def get(self, gateway_id: str) -> dict | None:
        return self.load().get(gateway_id)

    def delete(self, gateway_id: str) -> None:
        rows = self.load()
        row = rows.pop(gateway_id, None)
        self.save(rows)
        if row and row.get("auth_token_ref"):
            delete_secret(str(row["auth_token_ref"]))
