"""实盘交易 API 路由测试 — 端点契约: 提交意图 → 审批队列 → 订单流转.

镜像 test_paper_api.py 的风格 (TestClient + tmp_path 隔离 data_dir).
业务结果断言, 不只断言 200。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.live_trading.routes import build_router
from app.trading import store


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """构造带 data_dir 的 TestClient (镜像 paper API 测试用 SimpleNamespace)."""
    app = FastAPI()
    app.include_router(build_router())
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    store.init_db(tmp_path)
    return TestClient(app)


def test_create_and_list_accounts(client: TestClient):
    r = client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主账户",
    })
    assert r.status_code == 200
    assert r.json()["account"]["account_id"] == "acc1"
    r2 = client.get("/api/live/accounts")
    assert r2.status_code == 200
    assert any(a["account_id"] == "acc1" for a in r2.json()["accounts"])


def test_arm_requires_clean_reconciliation(client: TestClient, tmp_path: Path):
    """无对账 → arm 拒绝 (400)."""
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    r = client.post("/api/live/arm?account=acc1")
    assert r.status_code == 400  # 未对账, fail-closed


def test_full_arm_recon_submit_flow(client: TestClient, tmp_path: Path):
    """完整流程: 创建 → 对账 → arm → 提交意图 → 待审批."""
    from app.trading import store
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    # 注入资金快照 (风控 cash_sufficiency 需要)
    store.upsert_account_snapshot(tmp_path, {
        "account_id": "acc1", "as_of": "2026-10-21T10:00:00+08:00",
        "cash": 1_000_000, "frozen_cash": 0, "market_value": 0,
        "total": 1_000_000, "nav": 1_000_000, "mode": "disarmed",
    })
    # 干净对账
    r = client.post("/api/live/reconciliation/run?account=acc1")
    assert r.json()["status"] == "clean"
    # arm
    r = client.post("/api/live/arm?account=acc1")
    assert r.status_code == 200
    assert r.json()["mode"] == "armed_manual"

    # 提交意图 (next_open 绕开交易时段)
    r = client.post("/api/live/orders", json={
        "account_id": "acc1", "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price_policy": "next_open", "limit_price": 10.0,
        "source": "manual", "rationale": "测试",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending", f"unexpected reject: {body.get('reasons')}"
    assert body["verdict"] == "pass"

    # 待审批队列应该有
    r = client.get("/api/live/approvals")
    assert r.status_code == 200
    assert len(r.json()["approvals"]) == 1


def test_disarm(client: TestClient):
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    client.post("/api/live/reconciliation/run?account=acc1")
    client.post("/api/live/arm?account=acc1")
    r = client.post("/api/live/disarm?account=acc1")
    assert r.status_code == 200
    assert r.json()["mode"] == "disarmed"


def test_autonomy_l1_rejects_l2(client: TestClient):
    """本版拒绝创建 level=2 授权 (L3 有界自治未启用)."""
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    r = client.post("/api/live/autonomy", json={
        "strategy_id": "s1", "level": 2, "max_order_value": 1000,
    })
    assert r.status_code == 400


def test_autonomy_l1_create_and_revoke(client: TestClient):
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    r = client.post("/api/live/autonomy", json={
        "strategy_id": "s1", "level": 1,
    })
    assert r.status_code == 200
    grant_id = r.json()["grant"]["grant_id"]
    r = client.post(f"/api/live/autonomy/{grant_id}/revoke")
    assert r.status_code == 200
    assert r.json()["ok"] is True
