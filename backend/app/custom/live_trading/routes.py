"""真实交易 API — 薄壳: 校验在 Pydantic 模型, 业务在 app.trading 域模块.

路由前缀 /api/live, 通过 BackendExtensionRegistrar 注册, 不修改核心路由。
镜像 paper.py API 的设计: _data_dir = request.app.state.repo.store.data_dir,
HTTPException 包裹 ValueError。api_gateway 规则表按 live:read / live:propose / live:approve / live:admin
拆分 scope (见 app/services/api_gateway.py); UI 会话默认全 scope。
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


def _fetch_quote_snapshot(request: Request, symbol: str) -> dict:
    """从平台 enriched 实时缓存取标的行情快照 (last_price/prev_close/涨跌停).

    复用 quote_service.get_enriched_today() 的 polars DataFrame, 按 symbol 过滤。
    缺失列或无数据时返回空 dict (risk_ctx 对应字段为 None → fail-closed)。
    不触发网络请求, 只读内存缓存; 非交易时段或行情未启动时快照可能为空。
    """
    try:
        svc = getattr(request.app.state, "quote_service", None)
        if svc is None:
            return {}
        df, _ = svc.get_enriched_today()
        if df is None or df.is_empty() or "symbol" not in df.columns:
            return {}
        row = df.filter(df["symbol"] == symbol)
        if row.is_empty():
            return {}
        r = row.row(0, named=True)
        result: dict[str, float | None] = {}
        # enriched 的 close 即最新价 (CONTRIBUTING.md §3.2)
        import contextlib
        for col in ("close", "limit_up_price", "limit_down_price", "prev_close"):
            val = r.get(col)
            if val is not None:
                with contextlib.suppress(TypeError, ValueError):
                    result["last_price" if col == "close" else col] = float(val)
        return result
    except Exception:  # noqa: BLE001 — 行情快照失败不阻塞下单, fail-closed 由风控处理
        logger.warning("_fetch_quote_snapshot failed for %s", symbol, exc_info=True)
        return {}


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
        limit: int | None = Query(None, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        store.init_db(_data_dir(request))
        return {"orders": oms.list_orders(
            _data_dir(request), status=status, account_id=account,
            limit=limit, offset=offset,
        )}

    @router.post("/orders")
    def submit_intent(request: Request, body: IntentModel):
        """提交交易意图 → 风控 → 待审批 (不自动执行).

        行情价格校验: 优先取平台实时 enriched 快照 (last_price/prev_close/
        limit_up_price/limit_down_price) 注入 risk_ctx; 快照缺失时 fail-closed
        (last_price=None → risk._check_limit_bounds 返回 REJECT)。
        """
        d = _data_dir(request)
        store.init_db(d)
        acc = store.get_account(d, body.account_id)
        if acc is None:
            raise HTTPException(status_code=404, detail="账户不存在")
        snap = store.latest_account_snapshot(d, body.account_id) or {}
        mode = TradingMode(acc.get("mode", "disarmed"))
        # 行情快照: 从平台 enriched 缓存取标的实时价格 + 涨跌停判定价
        quote = _fetch_quote_snapshot(request, body.symbol)
        # fail-closed: limit/market 单需要实时行情; 行情不可用时拒绝 (蓝图 §10.1)
        # NEXT_OPEN/CLOSE 是盘后管道, 不依赖盘中行情
        if body.price_policy in ("limit", "market") and quote.get("last_price") is None:
            raise HTTPException(
                status_code=400,
                detail="实时行情不可用, 无法校验价格 (limit/market 单需要实时行情快照)",
            )
        from app.trading import risk as risk_mod
        ctx = risk_mod.build_ctx(
            account_id=body.account_id,
            mode=mode,
            cash=snap.get("cash", 0),
            total=snap.get("total", 0),
            last_price=quote.get("last_price"),
            prev_close=quote.get("prev_close"),
            limit_up_price=quote.get("limit_up_price"),
            limit_down_price=quote.get("limit_down_price"),
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

    @router.get("/orders/{order_id}/executions")
    def order_executions(request: Request, order_id: str):
        """单订单成交明细 (与 /events 对称)."""
        store.init_db(_data_dir(request))
        return {"executions": store.list_executions(_data_dir(request), order_id)}

    # ── 成交明细 ─────────────────────────
    @router.get("/executions")
    def list_executions(
        request: Request,
        account: str | None = Query(None),
        order_id: str | None = Query(None),
        limit: int | None = Query(None, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        """成交明细列表 (支持账户/订单过滤 + 服务端分页)."""
        store.init_db(_data_dir(request))
        return {"executions": oms.list_executions(
            _data_dir(request),
            account_id=account, order_id=order_id,
            limit=limit, offset=offset,
        )}

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