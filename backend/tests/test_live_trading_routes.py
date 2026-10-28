"""实盘交易 API 路由测试 — 端点契约: 提交意图 → 审批队列 → 订单流转.

镜像 test_paper_api.py 的风格 (TestClient + tmp_path 隔离 data_dir).
业务结果断言, 不只断言 200。
"""
from __future__ import annotations

import queue
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.live_trading.routes import build_router
from app.trading import store
from app.trading.brokers import registry as broker_registry


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """构造带 data_dir 的 TestClient (镜像 paper API 测试用 SimpleNamespace)."""
    app = FastAPI()
    app.include_router(build_router())
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    store.init_db(tmp_path)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_broker_instances():
    broker_registry.clear_instances()
    yield
    broker_registry.clear_instances()


def test_create_and_list_accounts(client: TestClient):
    r = client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主账户",
    })
    assert r.status_code == 200
    assert r.json()["account"]["account_id"] == "acc1"
    r2 = client.get("/api/live/accounts")
    assert r2.status_code == 200
    assert any(a["account_id"] == "acc1" for a in r2.json()["accounts"])


def test_broker_catalog_and_unknown_broker_rejected(client: TestClient):
    catalog = client.get("/api/live/brokers")
    assert catalog.status_code == 200
    assert any(row["name"] == "mock" and row["registered"] for row in catalog.json()["brokers"])
    mock = next(row for row in catalog.json()["brokers"] if row["name"] == "mock")
    assert mock["capabilities"]["supports_partial_fills"] is True
    assert mock["capabilities"]["supports_cancel_confirmation"] is True

    r = client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "not-installed", "display_name": "主账户",
    })
    assert r.status_code == 400
    assert "未注册" in r.json()["detail"]


def test_broker_catalog_shows_real_vendor_setup_requirements(client: TestClient):
    rows = client.get("/api/live/brokers").json()["brokers"]
    by_name = {row["name"]: row for row in rows}
    for name in ("qmt", "futu", "ibkr"):
        row = by_name[name]
        assert row["registered"] is False
        assert row["available"] is False
        assert row["requires_sdk"] is True
        assert row["requires_account_env"] is True
        assert row["requires_vendor_api"] is True
        assert row["requirements"]

    custom = client.get("/api/live/brokers").json()["custom_integration"]
    assert custom["status"] == "extension_required"
    assert len(custom["steps"]) >= 3

    # 展示位不能绕过 fail-closed 账户创建校验。
    response = client.post("/api/live/accounts", json={
        "account_id": "qmt1", "broker": "qmt", "display_name": "QMT",
    })
    assert response.status_code == 400


def test_connect_account_persists_broker_snapshots(client: TestClient):
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主账户",
    })
    r = client.post("/api/live/accounts/acc1/connect")
    assert r.status_code == 200
    assert r.json()["broker_status"]["connected"] is True

    detail = client.get("/api/live/accounts/acc1").json()
    assert detail["snapshot"]["cash"] == 1_000_000
    health = client.get("/api/live/health?account=acc1").json()
    assert health["broker_status"]["connected"] is True


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

    approval_id = r.json()["approvals"][0]["approval_id"]
    r = client.post(f"/api/live/approvals/{approval_id}/approve")
    assert r.status_code == 200
    assert r.json()["state"] == "submitted"
    assert r.json()["broker_order_id"].startswith("bk_")


def test_reconciliation_without_adapter_fails_closed(client: TestClient, tmp_path: Path):
    # 模拟历史数据库里遗留了已卸载的适配器账户。不能拿空列表伪造 clean。
    store.create_account(tmp_path, "legacy", "removed-broker", "旧账户")
    r = client.post("/api/live/reconciliation/run?account=legacy")
    assert r.status_code == 503
    assert store.list_recon_runs(tmp_path, "legacy") == []


def test_disarm(client: TestClient):
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    client.post("/api/live/reconciliation/run?account=acc1")
    client.post("/api/live/arm?account=acc1")
    r = client.post("/api/live/disarm?account=acc1")
    assert r.status_code == 200
    assert r.json()["mode"] == "disarmed"


def test_limit_order_rejected_without_quote_snapshot(client: TestClient, tmp_path: Path):
    """limit 单在无实时行情快照时 fail-closed 拒绝 (蓝图 §10.1).

    测试环境无 quote_service → _fetch_quote_snapshot 返回空 → 400。
    """
    from app.trading import store
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    store.upsert_account_snapshot(tmp_path, {
        "account_id": "acc1", "as_of": "2026-10-21T10:00:00+08:00",
        "cash": 1_000_000, "frozen_cash": 0, "market_value": 0,
        "total": 1_000_000, "nav": 1_000_000, "mode": "disarmed",
    })
    client.post("/api/live/reconciliation/run?account=acc1")
    client.post("/api/live/arm?account=acc1")
    # limit 单, 无 quote_service → 400
    r = client.post("/api/live/orders", json={
        "account_id": "acc1", "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 10.0,
    })
    assert r.status_code == 400
    assert "行情" in r.json()["detail"]


def test_next_open_uses_daily_reference_without_realtime(client: TestClient, tmp_path: Path):
    """盘后单不要求实时快照，但必须用本地 raw close 完成金额风控。"""
    import polars as pl

    class _Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        @staticmethod
        def resolve_asset_type(symbol):
            return "stock"

        @staticmethod
        def get_daily_asset(asset_type, symbol, start, end, columns=None):
            return pl.DataFrame({
                "date": [end], "raw_close": [10.0], "close": [10.0],
            })

    client.app.state.repo = _Repo()
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    client.post("/api/live/accounts/acc1/connect")
    client.post("/api/live/reconciliation/run?account=acc1")
    client.post("/api/live/arm?account=acc1")
    r = client.post("/api/live/orders", json={
        "account_id": "acc1", "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price_policy": "next_open", "limit_price": None,
    })
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "pass"


def test_order_validation_rejects_non_positive_qty(client: TestClient):
    r = client.post("/api/live/orders", json={
        "account_id": "acc1", "symbol": "600000.SH", "side": "buy", "qty": -100,
        "price_policy": "next_open",
    })
    assert r.status_code == 422


def test_limit_order_passes_with_quote_snapshot(client: TestClient, tmp_path: Path):
    """limit 单在有行情快照时正常提交 (注入 mock quote_service)."""
    import polars as pl

    from app.trading import store

    class _MockQuoteSvc:
        def get_enriched_today(self):
            df = pl.DataFrame({
                "symbol": ["600000.SH"],
                "close": [10.0],
                "prev_close": [9.9],
                "limit_up_price": [11.0],
                "limit_down_price": [9.0],
            })
            return df, None

    # 注入 mock quote_service 到 app.state
    client.app.state.quote_service = _MockQuoteSvc()
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    store.upsert_account_snapshot(tmp_path, {
        "account_id": "acc1", "as_of": "2026-10-21T10:00:00+08:00",
        "cash": 1_000_000, "frozen_cash": 0, "market_value": 0,
        "total": 1_000_000, "nav": 1_000_000, "mode": "disarmed",
    })
    client.post("/api/live/reconciliation/run?account=acc1")
    client.post("/api/live/arm?account=acc1")
    # limit 单, 有 quote_service → 进入风控 (非交易时段可能被 trading_session 拒)
    # 用 next_open 绕开 trading_session, 但 next_open 不需要行情 → 改用 limit 验证行情注入
    r = client.post("/api/live/orders", json={
        "account_id": "acc1", "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price_policy": "limit", "limit_price": 10.0,
    })
    # 非交易时段 trading_session 会拒, 但行情校验已通过 (不是 400 行情错误)
    body = r.json()
    if r.status_code == 400:
        assert "行情" not in body.get("detail", "")  # 可能是交易时段拒绝, 但不是行情缺失
    else:
        assert body["verdict"] in ("pass", "reject")


def test_events_publish_live_trading_type(client: TestClient, tmp_path: Path):
    """提交意图后 events.bus 收到 live_trading 类型事件 (SSE 推送链路)."""
    from app.services.events import bus
    from app.trading import store

    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    store.upsert_account_snapshot(tmp_path, {
        "account_id": "acc1", "as_of": "2026-10-21T10:00:00+08:00",
        "cash": 1_000_000, "frozen_cash": 0, "market_value": 0,
        "total": 1_000_000, "nav": 1_000_000, "mode": "disarmed",
    })
    client.post("/api/live/reconciliation/run?account=acc1")
    client.post("/api/live/arm?account=acc1")

    q = bus.subscribe()
    client.post("/api/live/orders", json={
        "account_id": "acc1", "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price_policy": "next_open", "limit_price": 10.0,
    })
    # 收到 live_trading 事件
    found = False
    for _ in range(10):
        try:
            ev = q.get_nowait()
            if ev.get("type") == "live_trading":
                found = True
                break
        except queue.Empty:
            break
    bus.unsubscribe(q)
    assert found, "未收到 live_trading 类型事件"


def test_autonomy_l1_rejects_l2(client: TestClient):
    """本版拒绝创建 level=2 授权 (L3 有界自治未启用)."""
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    r = client.post("/api/live/autonomy", json={
        "account_id": "acc1", "strategy_id": "s1", "level": 2,
        "max_order_value": 1000,
    })
    assert r.status_code == 400


def test_autonomy_l1_create_and_revoke(client: TestClient):
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    r = client.post("/api/live/autonomy", json={
        "account_id": "acc1", "strategy_id": "s1", "level": 1,
    })
    assert r.status_code == 200
    assert r.json()["grant"]["account_id"] == "acc1"
    grant_id = r.json()["grant"]["grant_id"]
    r = client.post(f"/api/live/autonomy/{grant_id}/revoke")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_executions_empty(client: TestClient):
    """无成交时 /api/live/executions 返回空列表."""
    r = client.get("/api/live/executions")
    assert r.status_code == 200
    assert r.json()["executions"] == []


def test_executions_filter_and_pagination(client: TestClient, tmp_path: Path):
    """成交明细: account/order 过滤 + limit/offset 分页."""
    from app.trading import store
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    # 插入 intent + order (满足外键约束), 再注入成交
    store._execute_write(tmp_path, lambda conn: conn.execute(
        "INSERT INTO trade_intents(intent_id, account_id, symbol, side, qty,"
        " price_policy, tif, source, dedupe_key, proposed_at, ruleset_version, rationale, status)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'approved')",
        ("int1", "acc1", "600000.SH", "buy", 100, "limit", "day", "manual",
         "dk1", "2026-10-21T10:00:00+08:00", "v1", "测试"),
    ))
    store.insert_order(tmp_path, {
        "order_id": "ord1", "broker_order_id": "bk1",
        "account_id": "acc1", "intent_id": "int1",
        "symbol": "600000.SH", "side": "buy", "qty": 300,
        "price_policy": "limit", "limit_price": 10.0,
        "state": "partially_filled", "filled_qty": 300, "avg_fill_price": 11.0,
        "submitted_at": "2026-10-21T10:00:00+08:00",
        "last_update_at": "2026-10-21T10:05:00+08:00",
        "reject_reason": None,
    })
    for i in range(3):
        store.insert_execution(tmp_path, {
            "exec_id": f"ex_{i}", "order_id": "ord1",
            "broker_order_id": "bk1", "broker_trade_id": f"bt_{i}",
            "account_id": "acc1", "symbol": "600000.SH", "side": "buy",
            "qty": 100, "price": 10.0 + i, "fee": 0.0,
            "ts": f"2026-10-21T10:0{i}:00+08:00",
            "dedupe_key": f"mock:acc1:bt_{i}",
        })
    # 全量
    r = client.get("/api/live/executions")
    assert len(r.json()["executions"]) == 3
    # account 过滤
    r = client.get("/api/live/executions?account=acc1")
    assert len(r.json()["executions"]) == 3
    r = client.get("/api/live/executions?account=nope")
    assert len(r.json()["executions"]) == 0
    # order_id 过滤
    r = client.get("/api/live/executions?order_id=ord1")
    assert len(r.json()["executions"]) == 3
    # 分页
    r = client.get("/api/live/executions?limit=2&offset=0")
    assert len(r.json()["executions"]) == 2
    r = client.get("/api/live/executions?limit=2&offset=2")
    assert len(r.json()["executions"]) == 1
    # 单订单端点
    r = client.get("/api/live/orders/ord1/executions")
    assert r.status_code == 200
    assert len(r.json()["executions"]) == 3


def test_orders_pagination(client: TestClient, tmp_path: Path):
    """订单列表 limit/offset 分页."""
    from app.trading import store
    client.post("/api/live/accounts", json={
        "account_id": "acc1", "broker": "mock", "display_name": "主",
    })
    for i in range(5):
        store._execute_write(tmp_path, lambda conn, _i=i: conn.execute(
            "INSERT INTO trade_intents(intent_id, account_id, symbol, side, qty,"
            " price_policy, tif, source, dedupe_key, proposed_at, ruleset_version, rationale, status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'approved')",
            (f"int_{_i}", "acc1", "600000.SH", "buy", 100, "limit", "day", "manual",
             f"dk_{_i}", "2026-10-21T10:00:00+08:00", "v1", "测试"),
        ))
        store.insert_order(tmp_path, {
            "order_id": f"ord_{i}", "broker_order_id": None,
            "account_id": "acc1", "intent_id": f"int_{i}",
            "symbol": "600000.SH", "side": "buy", "qty": 100,
            "price_policy": "limit", "limit_price": 10.0,
            "state": "created", "filled_qty": 0, "avg_fill_price": None,
            "submitted_at": None, "last_update_at": f"2026-10-21T10:0{i}:00+08:00",
            "reject_reason": None,
        })
    r = client.get("/api/live/orders?limit=2")
    assert len(r.json()["orders"]) == 2
    r = client.get("/api/live/orders?limit=2&offset=4")
    assert len(r.json()["orders"]) == 1
