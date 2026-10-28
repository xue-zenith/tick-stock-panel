"""真实交易 API — 薄壳: 校验在 Pydantic 模型, 业务在 app.trading 域模块.

路由前缀 /api/live, 通过 BackendExtensionRegistrar 注册, 不修改核心路由。
镜像 paper.py API 的设计: _data_dir = request.app.state.repo.store.data_dir,
HTTPException 包裹 ValueError。api_gateway 规则表按 live:read / live:propose / live:approve / live:admin
拆分 scope (见 app/services/api_gateway.py); UI 会话默认全 scope。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from app.market_time import cn_today
from app.trading import autonomy, broker_service, oms, store
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
        if svc is not None:
            df, _ = svc.get_enriched_today()
            if df is not None and not df.is_empty() and "symbol" in df.columns:
                row = df.filter(df["symbol"] == symbol)
                if not row.is_empty():
                    r = row.row(0, named=True)
                    result: dict[str, float | bool | None] = {"is_realtime": True}
                    # enriched 的 close 即最新价 (CONTRIBUTING.md §3.2)
                    import contextlib
                    for col in ("close", "limit_up_price", "limit_down_price", "prev_close"):
                        val = r.get(col)
                        if val is not None:
                            with contextlib.suppress(TypeError, ValueError):
                                result["last_price" if col == "close" else col] = float(val)
                    if result.get("last_price") is not None:
                        return result
    except Exception:  # noqa: BLE001 — 行情快照失败不阻塞下单, fail-closed 由风控处理
        logger.warning("_fetch_quote_snapshot failed for %s", symbol, exc_info=True)

    # 盘后单仍需一个可靠参考价做金额/仓位预检，但不能把日线价冒充实时价。
    try:
        repo = request.app.state.repo
        asset_type = repo.resolve_asset_type(symbol)
        end = cn_today()
        df = repo.get_daily_asset(
            asset_type, symbol, end - timedelta(days=30), end,
            columns=["date", "raw_close", "close"],
        )
        if df is not None and not df.is_empty():
            row = df.sort("date").row(-1, named=True)
            price = row.get("raw_close") or row.get("close")
            if price is not None:
                return {"last_price": float(price), "is_realtime": False}
    except Exception:  # noqa: BLE001 — 无日线参考价时由风控 fail-closed
        logger.warning("daily reference price failed for %s", symbol, exc_info=True)
    return {"is_realtime": False}


def _build_risk_ctx(request: Request, intent: dict, account: dict):
    """用最新券商快照、持仓和行情重建风控上下文。"""
    from app.trading import risk as risk_mod

    d = _data_dir(request)
    account_id = intent["account_id"]
    snap = store.latest_account_snapshot(d, account_id) or {}
    positions = {
        row["symbol"]: row for row in store.latest_positions(d, account_id)
    }
    position = positions.get(intent["symbol"], {})
    quote = _fetch_quote_snapshot(request, intent["symbol"])
    repo = getattr(request.app.state, "repo", None)
    try:
        asset_type = repo.resolve_asset_type(intent["symbol"]) if repo is not None else "stock"
    except Exception:  # noqa: BLE001 — 不确定时按股票严格规则处理
        asset_type = "stock"
    level = (
        autonomy.effective_level(d, account_id, intent.get("strategy_id") or "")
        if intent.get("source") == "ai_l1" else 0
    )
    return risk_mod.build_ctx(
        account_id=account_id,
        mode=TradingMode(account.get("mode", "disarmed")),
        cash=snap.get("cash", 0),
        total=snap.get("total", 0),
        last_price=quote.get("last_price"),
        prev_close=quote.get("prev_close"),
        limit_up_price=quote.get("limit_up_price"),
        limit_down_price=quote.get("limit_down_price"),
        position_qty=int(position.get("qty") or 0),
        available_qty=int(position.get("available_qty") or 0),
        asset_type=asset_type,
        autonomy_level=level,
    ), quote


# ── Pydantic 模型 ────────────────────────────────────────
class CreateAccountModel(BaseModel):
    account_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    broker: str = Field(default="mock", min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    display_name: str = Field(default="实盘账户", min_length=1, max_length=80)


class IntentModel(BaseModel):
    account_id: str
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0)
    price_policy: Literal["limit", "market", "next_open", "close"] = "limit"
    limit_price: float | None = Field(default=None, gt=0)
    tif: Literal["day", "gtc", "ioc", "fok"] = "day"
    source: Literal["manual", "ai_l1"] = "manual"
    strategy_id: str | None = None
    rationale: str = ""
    dedupe_key: str | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value):
        return str(value).strip().upper()

    @model_validator(mode="after")
    def validate_policy(self):
        if self.price_policy == "limit" and self.limit_price is None:
            raise ValueError("限价单必须提供 limit_price")
        if self.source == "ai_l1" and not self.strategy_id:
            raise ValueError("AI_L1 意图必须提供 strategy_id")
        return self


class CreateGrantModel(BaseModel):
    account_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    strategy_id: str
    level: int = 1
    max_order_value: float = 0.0


def _register(router: APIRouter) -> None:

    @router.get("/brokers")
    def list_brokers():
        """返回已加载适配器及厂商接入要求，避免把未安装 SDK 伪装成可交易。"""
        return {
            "brokers": broker_service.catalog(),
            "custom_integration": {
                "status": "extension_required",
                "title": "自定义券商适配器",
                "steps": [
                    "实现 BrokerAdapter：连接、健康检查、下单、撤单确认、订单/成交查询",
                    "按券商规则实现状态枚举、部分成交、交易时段和回调验签",
                    "在部署启动阶段 register('your_broker', factory, capabilities=...)",
                    "配置密钥到部署环境，连接后先完成一次全量对账再启用交易",
                ],
            },
        }

    # ── 账户 ─────────────────────────────
    @router.get("/accounts")
    def list_accounts(request: Request):
        store.init_db(_data_dir(request))
        return {"accounts": store.list_accounts(_data_dir(request))}

    @router.post("/accounts")
    def create_account(request: Request, body: CreateAccountModel):
        store.init_db(_data_dir(request))
        if not any(
            row["name"] == body.broker and row.get("registered") is True
            for row in broker_service.catalog()
        ):
            raise HTTPException(
                status_code=400,
                detail=f"券商适配器 {body.broker!r} 未注册或未安装",
            )
        try:
            acc = store.create_account(
                _data_dir(request),
                _acc_validator(body.account_id),
                body.broker, body.display_name,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"account": acc}

    @router.post("/accounts/{account_id}/connect")
    def connect_account(request: Request, account_id: str):
        d = _data_dir(request)
        store.init_db(d)
        acc = store.get_account(d, _acc_validator(account_id))
        if acc is None:
            raise HTTPException(status_code=404, detail="账户不存在")
        try:
            adapter = broker_service.connect(d, acc["broker"], account_id)
            pulled = broker_service.pull(adapter)
            # 新账户建立风控基线；已有快照必须先对账，不能用 reconnect 静默覆盖差异。
            if store.latest_account_snapshot(d, account_id) is None:
                broker_service.persist_snapshot(d, account_id, acc["mode"], pulled)
        except (broker_service.BrokerUnavailableError, broker_service.BrokerSyncError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "account": store.get_account(d, account_id),
            "broker_status": broker_service.status(acc["broker"], account_id),
        }

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
        try:
            broker_service.require_connected(d, acc["broker"], body.account_id)
        except broker_service.BrokerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        intent_payload = body.model_dump()
        ctx, quote = _build_risk_ctx(request, intent_payload, acc)
        # fail-closed: limit/market 单需要实时行情; 行情不可用时拒绝 (蓝图 §10.1)
        # NEXT_OPEN/CLOSE 是盘后管道, 不依赖盘中行情
        if body.price_policy in ("limit", "market") and not quote.get("is_realtime"):
            raise HTTPException(
                status_code=400,
                detail="实时行情不可用, 无法校验价格 (limit/market 单需要实时行情快照)",
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
        order = store.get_order(d, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在")
        acc = store.get_account(d, order["account_id"])
        adapter = None
        if order["state"] in ("submitted", "partially_filled", "unknown"):
            if acc is None:
                raise HTTPException(status_code=400, detail="订单账户不存在")
            try:
                adapter = broker_service.require_connected(
                    d, acc["broker"], order["account_id"],
                )
            except broker_service.BrokerUnavailableError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            result = oms.cancel(d, order_id, adapter)
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
        intent = store.get_intent(d, ap["intent_id"])
        if intent is None:
            raise HTTPException(status_code=404, detail="交易意图不存在")
        acc = store.get_account(d, intent["account_id"])
        if acc is None:
            raise HTTPException(status_code=404, detail="账户不存在")
        try:
            adapter = broker_service.require_connected(
                d, acc["broker"], intent["account_id"],
            )
        except broker_service.BrokerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            fresh_acc = store.get_account(d, intent["account_id"]) or acc
            risk_ctx, quote = _build_risk_ctx(request, intent, fresh_acc)
            if intent["price_policy"] in ("limit", "market") and not quote.get("is_realtime"):
                raise oms.TradingError("实时行情不可用，审批时无法重新校验价格")
            approved = oms.approve(d, ap["intent_id"], risk_ctx=risk_ctx)
            submitted = oms.submit_to_broker(d, approved["order_id"], adapter)
            return {**approved, **submitted}
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
                "broker_status": broker_service.status(acc["broker"], account),
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
        """手动触发真实对账；未注册、断线或任一快照失败都返回 503。"""
        d = _data_dir(request)
        store.init_db(d)
        acc = store.get_account(d, account)
        if acc is None:
            raise HTTPException(status_code=404, detail="账户不存在")
        try:
            adapter = broker_service.require_connected(d, acc["broker"], account)
            pulled = broker_service.pull(adapter)
            broker_service.ingest_executions(
                d, account, acc["broker"], list(pulled["executions"]),
            )
        except (broker_service.BrokerUnavailableError, broker_service.BrokerSyncError) as exc:
            store.log_system_event(d, "reconciliation_unavailable", {
                "account_id": account, "error": str(exc),
            })
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        result = oms.trigger_reconciliation(
            d, account,
            list(pulled["orders"]),
            list(pulled["positions"]),
            dict(pulled["account"])["cash"],
        )
        refreshed = store.get_account(d, account) or acc
        broker_service.persist_snapshot(d, account, refreshed["mode"], pulled)
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
                d, body.account_id, body.strategy_id, body.level, body.max_order_value,
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
        acc = store.get_account(d, account)
        if acc is None:
            raise HTTPException(status_code=404, detail="账户不存在")
        try:
            broker_service.require_connected(d, acc["broker"], account)
        except broker_service.BrokerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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
