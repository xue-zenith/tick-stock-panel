"""真实交易 API — 薄壳: 校验在 Pydantic 模型, 业务在 app.trading 域模块.

路由前缀 /api/live, 通过 BackendExtensionRegistrar 注册, 不修改核心路由。
镜像 paper.py API 的设计: _data_dir = request.app.state.repo.store.data_dir,
HTTPException 包裹 ValueError。未对 api_gateway 添加 live:trade scope,
端点仅 UI 会话可达 (与 /api/paper/* 同语义)。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.trading import autonomy, oms, store
from app.trading.contracts import TradingMode

logger = logging.getLogger(__name__)


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/live", tags=["live-trading"])
    _register(router)
    return router


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _acc_validator(account_id: str) -> str:
    """校验账户 ID 格式 (与 paper.validate_account_id 同风格)."""
    if not account_id or not all(
        c.isalnum() or c in "-_." for c in account_id
    ) or len(account_id) > 64:
        raise HTTPException(status_code=400, detail=f"非法账户 ID: {account_id!r}")
    return account_id


# ── Pydantic 模型 ────────────────────────────────────────
class CreateAccountModel(BaseModel):
    account_id: str
    broker: str = "mock"
    display_name: str = "实盘账户"


class IntentModel(BaseModel):
    account_id: str
    symbol: str
    side: str                # buy / sell
    qty: int
    price_policy: str = "limit"   # limit / market / next_open / close
    limit_price: float | None = None
    tif: str = "day"
    source: str = "manual"        # manual / ai_l1
    strategy_id: str | None = None
    rationale: str = ""
    dedupe_key: str | None = None


class CreateGrantModel(BaseModel):
    strategy_id: str
    level: int = 1
    max_order_value: float = 0.0


def _register(router: APIRouter) -> None:

    # ── 账户 ─────────────────────────────
    @router.get("/accounts")
    def list_accounts(request: Request):
        store.init_db(_data_dir(request))
        return {"accounts": store.list_accounts(_data_dir(request))}

    @router.post("/accounts")
    def create_account(request: Request, body: CreateAccountModel):
        store.init_db(_data_dir(request))
        try:
            acc = store.create_account(
                _data_dir(request),
                _acc_validator(body.account_id),
                body.broker, body.display_name,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"account": acc}

    @router.get("/accounts/{account_id}")
    def get_account(request: Request, account_id: str):
        store.init_db(_data_dir(request))
        acc = store.get_account(_data_dir(request), account_id)
        if acc is None:
            raise HTTPException(status_code=404, detail="账户不存在")
        snap = store.latest_account_snapshot(_data_dir(request), account_id)
        positions = store.latest_positions(_data_dir(request), account_id)
        return {"account": acc, "snapshot": snap, "positions": positions}

    # ── 订单/意图 ─────────────────────────
    @router.get("/orders")
    def list_orders(
        request: Request,
        status: str | None = None,
        account: str | None = Query(None),
    ):
        store.init_db(_data_dir(request))
        return {"orders": oms.list_orders(_data_dir(request), status=status, account_id=account)}

    @router.post("/orders")
    def submit_intent(request: Request, body: IntentModel):
        """提交交易意图 → 风控 → 待审批 (不自动执行)."""
        d = _data_dir(request)
        store.init_db(d)
        acc = store.get_account(d, body.account_id)
        if acc is None:
            raise HTTPException(status_code=404, detail="账户不存在")
        # 构造风控上下文 (此处简化: 用最近快照; 真实生产由调用方或 OMS 注入行情)
        snap = store.latest_account_snapshot(d, body.account_id) or {}
        mode = TradingMode(acc.get("mode", "disarmed"))
        from app.trading import risk as risk_mod
        ctx = risk_mod.build_ctx(
            account_id=body.account_id,
            mode=mode,
            cash=snap.get("cash", 0),
            total=snap.get("total", 0),
            last_price=body.limit_price,
            prev_close=body.limit_price,
            limit_up_price=None, limit_down_price=None,
            position_qty=0,
            available_qty=store.available_qty(d, body.account_id, body.symbol),
        )
        try:
            result = oms.submit_intent(
                d,
                account_id=body.account_id, symbol=body.symbol, side=body.side,
                qty=body.qty, price_policy=body.price_policy,
                limit_price=body.limit_price, tif=body.tif,
                source=body.source, strategy_id=body.strategy_id,
                rationale=body.rationale, dedupe_key=body.dedupe_key,
                risk_ctx=ctx,
            )
        except oms.TradingError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return result

    @router.delete("/orders/{order_id}")
    def cancel_order(request: Request, order_id: str):
        d = _data_dir(request)
        store.init_db(d)
        try:
            result = oms.cancel(d, order_id)
        except oms.TradingError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return result

    @router.get("/orders/{order_id}/events")
    def order_events(request: Request, order_id: str):
        store.init_db(_data_dir(request))
        return {"events": store.list_order_events(_data_dir(request), order_id)}

    # ── 审批 ──────────────────────────────
    @router.get("/approvals")
    def pending_approvals(request: Request):
        store.init_db(_data_dir(request))
        return {"approvals": store.list_pending_approvals(_data_dir(request))}

    @router.post("/approvals/{approval_id}/approve")
    def approve_approval(request: Request, approval_id: str):
        """人工审批通过 → 创建 BrokerOrder (APPROVED)."""
        d = _data_dir(request)
        store.init_db(d)
        ap = store.get_approval(d, approval_id)
        if ap is None:
            raise HTTPException(status_code=404, detail="审批记录不存在")
        try:
            return oms.approve(d, ap["intent_id"])
        except oms.TradingError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/approvals/{approval_id}/reject")
    def reject_approval(request: Request, approval_id: str):
        d = _data_dir(request)
        store.init_db(d)
        ap = store.get_approval(d, approval_id)
        if ap is None:
            raise HTTPException(status_code=404, detail="审批记录不存在")
        try:
            return oms.reject(d, ap["intent_id"])
        except oms.TradingError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # ── 健康/对账 ─────────────────────────
    @router.get("/health")
    def health(request: Request, account: str | None = Query(None)):
        d = _data_dir(request)
        store.init_db(d)
        if account:
            acc = store.get_account(d, account)
            if acc is None:
                raise HTTPException(status_code=404, detail="账户不存在")
            return {
                "account": account,
                "mode": acc["mode"],
                "unresolved_diffs": store.unresolved_diff_count(d, account),
                "recent_runs": store.list_recon_runs(d, account)[:5],
                "recent_events": store.recent_system_events(d, limit=20),
            }
        return {
            "accounts": store.list_accounts(d),
            "recent_events": store.recent_system_events(d, limit=20),
        }

    @router.get("/scheduler")
    def scheduler_status(request: Request):
        """定时对账调度器状态 (运行中/频率/最近对账时间/已注册 broker)."""
        from app.trading import scheduler as _scheduler
        s = _scheduler.get_scheduler()
        if s is None:
            return {"running": False, "message": "调度器未启动"}
        return s.status()

    @router.post("/scheduler/run-now")
    async def scheduler_run_now(request: Request):
        """立即触发一次全账户对账 (不等周期), 供手动验证调度链路."""
        from app.trading import scheduler as _scheduler
        s = _scheduler.get_scheduler()
        if s is None:
            raise HTTPException(status_code=503, detail="调度器未启动")
        await s._reconcile_all(reason="manual", fetch_positions=True)
        return {"ok": True, "message": "已触发一次全账户对账"}

    @router.get("/reconciliation")
    def list_reconciliation(request: Request, account: str | None = Query(None)):
        d = _data_dir(request)
        store.init_db(d)
        runs = store.list_recon_runs(d, account)
        diffs = []
        if runs:
            diffs = store.list_recon_diffs(d, runs[0]["run_id"])
        return {"runs": runs, "diffs": diffs}

    @router.post("/reconciliation/run")
    def trigger_reconciliation(request: Request, account: str = Query(...)):
        """手动触发对账: 优先用已注册 broker 适配器拉取真实状态, 无适配器则空对账。"""
        d = _data_dir(request)
        store.init_db(d)
        acc = store.get_account(d, account)
        if acc is None:
            raise HTTPException(status_code=404, detail="账户不存在")
        broker_name = acc["broker"]
        # 尝试用 broker 适配器拉取真实状态 (调度器同款路径)
        from app.trading.brokers import registry as broker_registry
        adapter = broker_registry.get_adapter(broker_name, account)
        broker_orders: list[dict] = []
        broker_positions: list[dict] = []
        broker_cash: float | None = None
        if adapter is not None:
            import contextlib
            with contextlib.suppress(Exception):
                broker_orders = adapter.get_orders()
            with contextlib.suppress(Exception):
                broker_positions = adapter.get_positions()
            with contextlib.suppress(Exception):
                broker_cash = adapter.get_account().get("cash")
        result = oms.trigger_reconciliation(
            d, account, broker_orders, broker_positions, broker_cash,
        )
        return result

    # ── 授权 ──────────────────────────────
    @router.get("/autonomy")
    def list_autonomy(request: Request):
        d = _data_dir(request)
        store.init_db(d)
        return {"grants": autonomy.list_grants(d)}

    @router.post("/autonomy")
    def create_autonomy(request: Request, body: CreateGrantModel):
        d = _data_dir(request)
        store.init_db(d)
        try:
            grant = autonomy.create_grant(
                d, body.strategy_id, body.level, body.max_order_value,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"grant": grant}

    @router.post("/autonomy/{grant_id}/revoke")
    def revoke_autonomy(request: Request, grant_id: str):
        d = _data_dir(request)
        store.init_db(d)
        if not autonomy.revoke_grant(d, grant_id):
            raise HTTPException(status_code=404, detail="授权不存在或已撤销")
        return {"ok": True}

    # ── 紧急停止 ─────────────────────────
    @router.post("/arm")
    def arm(request: Request, account: str = Query(...)):
        d = _data_dir(request)
        store.init_db(d)
        try:
            return oms.arm_account(d, account)
        except oms.TradingError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/disarm")
    def disarm(request: Request, account: str = Query(...)):
        d = _data_dir(request)
        store.init_db(d)
        try:
            return oms.disarm_account(d, account)
        except oms.TradingError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e