"""实盘交易 SQLite WAL 存储 — schema / 迁移 / 单写者事务。

与模拟盘 (paper.py 的 JSON/JSONL + 单进程 RLock) 的关键差异:
  - 实盘用 SQLite 事务把意图/订单/审批/风控/回报/审计写在一个事务里,
    避免模拟盘的“台账已写、订单仍是 pending”的半成功窗口;
  - WAL 模式 + synchronous=NORMAL 适合单写者场景 (个人单机部署);
  - schema_version 表 + 幂等迁移: 升级时只新增表/列, 不破坏历史数据。

DB 路径: data_dir / "live_trading" / "oms.db"。
单写者: module-level RLock 保护所有写操作 (镜像 paper.py:49 PAPER_LOCK)。
读操作无锁 (SQLite WAL 允许并发读)。

所有 SQL 用参数化查询, 禁止字符串拼接 (CONTRIBUTING.md §8 安全要求)。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from app.trading.contracts import TradingMode

logger = logging.getLogger(__name__)

# 单写者锁 — 保护所有写事务; 与 paper.py 的 PAPER_LOCK 同语义。
# 跨进程幂等不靠这把锁 (靠 dedupe_key UNIQUE + broker_trade_id UNIQUE),
# 这把锁只防同一进程内并发写冲突。
_WRITE_LOCK = threading.RLock()

SCHEMA_VERSION = 1


def _db_path(data_dir: Path) -> Path:
    p = data_dir / "live_trading" / "oms.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect(data_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(data_dir))
    conn.row_factory = sqlite3.Row
    # WAL: 读不阻塞写, 写不阻塞读; 单写者场景下 synchronous=NORMAL 可接受
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移: 检查 schema_version, 缺表则建, 不破坏已有数据。

    升级只走“加表/加列”路径; 字段重命名/删除需提升 SCHEMA_VERSION
    并补迁移分支, 但本版 (v1) 不涉及。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at TEXT NOT NULL"
        ")"
    )
    row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
    current = row[0] if row is not None else 0

    # 本版一次性建全表 (current < 1); 未来版本在此追加 ALTER/CREATE。
    if current < 1:
        conn.executescript(_SCHEMA_V1)
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _now_iso()),
        )


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS broker_accounts (
    account_id    TEXT PRIMARY KEY,
    broker        TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'disarmed',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_intents (
    intent_id       TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    strategy_id     TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    price_policy    TEXT NOT NULL,
    limit_price     REAL,
    tif             TEXT NOT NULL,
    source          TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL UNIQUE,
    proposed_at     TEXT NOT NULL,
    valid_until     TEXT,
    ruleset_version TEXT NOT NULL,
    rationale       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON trade_intents(status);
CREATE INDEX IF NOT EXISTS idx_intents_account ON trade_intents(account_id);

CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id     TEXT PRIMARY KEY,
    intent_id       TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    reasons         TEXT NOT NULL DEFAULT '[]',
    checked_at      TEXT NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES trade_intents(intent_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT PRIMARY KEY,
    intent_id       TEXT NOT NULL,
    approver        TEXT,
    decision        TEXT NOT NULL DEFAULT 'pending',
    decided_at      TEXT,
    expires_at    TEXT NOT NULL,
    order_snapshot  TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (intent_id) REFERENCES trade_intents(intent_id)
);
CREATE INDEX IF NOT EXISTS idx_approvals_intent ON approvals(intent_id);
CREATE INDEX IF NOT EXISTS idx_approvals_decision ON approvals(decision);

CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT PRIMARY KEY,
    broker_order_id   TEXT,
    account_id        TEXT NOT NULL,
    intent_id         TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    qty               INTEGER NOT NULL,
    price_policy      TEXT NOT NULL,
    limit_price       REAL,
    state             TEXT NOT NULL,
    filled_qty        INTEGER NOT NULL DEFAULT 0,
    avg_fill_price    REAL,
    submitted_at      TEXT,
    last_update_at    TEXT NOT NULL,
    reject_reason     TEXT,
    FOREIGN KEY (intent_id) REFERENCES trade_intents(intent_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_id);

CREATE TABLE IF NOT EXISTS order_events (
    event_id    TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    ts          TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, ts);

CREATE TABLE IF NOT EXISTS executions (
    exec_id         TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL,
    broker_order_id TEXT,
    broker_trade_id TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL,
    price           REAL NOT NULL,
    fee             REAL NOT NULL DEFAULT 0,
    ts              TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL UNIQUE,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);
CREATE INDEX IF NOT EXISTS idx_executions_order ON executions(order_id);

CREATE TABLE IF NOT EXISTS account_snapshots (
    account_id    TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    cash          REAL NOT NULL,
    frozen_cash   REAL NOT NULL DEFAULT 0,
    market_value  REAL NOT NULL DEFAULT 0,
    total         REAL NOT NULL DEFAULT 0,
    nav           REAL NOT NULL DEFAULT 0,
    mode          TEXT NOT NULL DEFAULT 'disarmed',
    PRIMARY KEY (account_id, as_of)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    account_id    TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    qty           INTEGER NOT NULL DEFAULT 0,
    available_qty INTEGER NOT NULL DEFAULT 0,
    avg_cost      REAL NOT NULL DEFAULT 0,
    last_price    REAL,
    market_value    REAL NOT NULL DEFAULT 0,
    pnl           REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, symbol, as_of)
);

CREATE TABLE IF NOT EXISTS strategy_allocations (
    strategy_id    TEXT NOT NULL,
    account_id    TEXT NOT NULL,
    target_weight REAL NOT NULL DEFAULT 0,
    target_qty     INTEGER NOT NULL DEFAULT 0,
    as_of         TEXT NOT NULL,
    PRIMARY KEY (strategy_id, account_id)
);

CREATE TABLE IF NOT EXISTS autonomy_grants (
    grant_id            TEXT PRIMARY KEY,
    strategy_id        TEXT NOT NULL,
    level              INTEGER NOT NULL,
    max_order_value    REAL NOT NULL DEFAULT 0,
    daily_loss_limit_pct REAL NOT NULL DEFAULT 0,
    enabled            INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    revoked_at         TEXT
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id       TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    diff_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recon_runs_account ON reconciliation_runs(account_id, started_at);

CREATE TABLE IF NOT EXISTS reconciliation_diffs (
    diff_id      TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    order_id     TEXT,
    diff_kind   TEXT NOT NULL,
    local_value TEXT,
    broker_value TEXT,
    resolved    INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    FOREIGN KEY (run_id) REFERENCES reconciliation_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_recon_diffs_run ON reconciliation_diffs(run_id);

CREATE TABLE IF NOT EXISTS system_events (
    event_id  TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '{}',
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_events_ts ON system_events(ts);
"""


# ── 连接与初始化 ─────────────────────────────────────────
def init_db(data_dir: Path) -> None:
    """初始化/迁移数据库 (幂等)。模块 RLock 下建表。"""
    with _WRITE_LOCK:
        conn = _connect(data_dir)
        try:
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()


def _now_iso() -> str:
    from app.market_time import cn_now
    return cn_now().isoformat()


# ── 写操作 (全部在 _WRITE_LOCK + sqlite 事务内) ─────────
def _execute_write(
    data_dir: Path, fn
) -> object:
    """在写锁 + 事务内执行 fn(conn) → 返回值。"""
    with _WRITE_LOCK:
        conn = _connect(data_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ── 通用查询 (无锁读, WAL 允许) ──────────────────────────
def _query(data_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = _connect(data_dir)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _query_one(data_dir: Path, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    conn = _connect(data_dir)
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


# ── broker_accounts ─────────────────────────────────────
def create_account(
    data_dir: Path,
    account_id: str,
    broker: str,
    display_name: str,
) -> dict:
    def _do(conn: sqlite3.Connection) -> dict:
        conn.execute(
            "INSERT INTO broker_accounts(account_id, broker, display_name, mode, created_at)"
            " VALUES(?, ?, ?, 'disarmed', ?)",
            (account_id, broker, display_name, _now_iso()),
        )
        return {
            "account_id": account_id,
            "broker": broker,
            "display_name": display_name,
            "mode": TradingMode.DISARMED.value,
        }

    return _execute_write(data_dir, _do)


def get_account(data_dir: Path, account_id: str) -> dict | None:
    row = _query_one(
        data_dir,
        "SELECT * FROM broker_accounts WHERE account_id = ?",
        (account_id,),
    )
    return dict(row) if row is not None else None


def list_accounts(data_dir: Path) -> list[dict]:
    return [dict(r) for r in _query(data_dir, "SELECT * FROM broker_accounts ORDER BY created_at")]


def set_account_mode(data_dir: Path, account_id: str, mode: str) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE broker_accounts SET mode = ? WHERE account_id = ?",
            (mode, account_id),
        )

    _execute_write(data_dir, _do)


# ── trade_intents ────────────────────────────────────────
def insert_intent(data_dir: Path, intent: dict) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO trade_intents("
            " intent_id, account_id, strategy_id, symbol, side, qty, price_policy,"
            " limit_price, tif, source, dedupe_key, proposed_at, valid_until,"
            " ruleset_version, rationale, status"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                intent["intent_id"], intent["account_id"], intent.get("strategy_id"),
                intent["symbol"], intent["side"], intent["qty"], intent["price_policy"],
                intent.get("limit_price"), intent["tif"], intent["source"],
                intent["dedupe_key"], intent["proposed_at"], intent.get("valid_until"),
                intent["ruleset_version"], intent.get("rationale"),
                intent.get("status", "pending"),
            ),
        )

    _execute_write(data_dir, _do)


def get_intent_by_dedupe(data_dir: Path, dedupe_key: str) -> dict | None:
    row = _query_one(
        data_dir,
        "SELECT * FROM trade_intents WHERE dedupe_key = ?",
        (dedupe_key,),
    )
    return dict(row) if row is not None else None


def get_intent(data_dir: Path, intent_id: str) -> dict | None:
    row = _query_one(
        data_dir,
        "SELECT * FROM trade_intents WHERE intent_id = ?",
        (intent_id,),
    )
    return dict(row) if row is not None else None


def update_intent_status(data_dir: Path, intent_id: str, status: str) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE trade_intents SET status = ? WHERE intent_id = ?",
            (status, intent_id),
        )

    _execute_write(data_dir, _do)


def list_intents(data_dir: Path, status: str | None = None) -> list[dict]:
    if status:
        rows = _query(
            data_dir,
            "SELECT * FROM trade_intents WHERE status = ? ORDER BY proposed_at DESC",
            (status,),
        )
    else:
        rows = _query(data_dir, "SELECT * FROM trade_intents ORDER BY proposed_at DESC")
    return [dict(r) for r in rows]


def list_pending_approvals(data_dir: Path) -> list[dict]:
    """待审批意图 (status=pending) + 对应 Approval 行。"""
    rows = _query(
        data_dir,
        "SELECT t.*, a.approval_id, a.expires_at, a.order_snapshot"
        " FROM trade_intents t"
        " LEFT JOIN approvals a ON a.intent_id = t.intent_id AND a.decision = 'pending'"
        " WHERE t.status = 'pending'"
        " ORDER BY t.proposed_at ASC",
    )
    return [dict(r) for r in rows]


# ── risk_decisions ───────────────────────────────────────
def insert_risk_decision(data_dir: Path, decision: dict) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO risk_decisions("
            " decision_id, intent_id, verdict, ruleset_version, reasons, checked_at"
            ") VALUES(?, ?, ?, ?, ?, ?)",
            (
                decision["decision_id"], decision["intent_id"], decision["verdict"],
                decision["ruleset_version"],
                json.dumps(decision.get("reasons") or [], ensure_ascii=False),
                decision["checked_at"],
            ),
        )

    _execute_write(data_dir, _do)


# ── approvals ────────────────────────────────────────────
def insert_approval(data_dir: Path, approval: dict) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO approvals("
            " approval_id, intent_id, approver, decision, decided_at, expires_at, order_snapshot"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                approval["approval_id"], approval["intent_id"], approval.get("approver"),
                approval.get("decision", "pending"), approval.get("decided_at"),
                approval["expires_at"], approval.get("order_snapshot", "{}"),
            ),
        )

    _execute_write(data_dir, _do)


def get_approval(data_dir: Path, approval_id: str) -> dict | None:
    row = _query_one(
        data_dir,
        "SELECT * FROM approvals WHERE approval_id = ?",
        (approval_id,),
    )
    return dict(row) if row is not None else None


def decide_approval(
    data_dir: Path,
    approval_id: str,
    decision: str,
    approver: str | None,
) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE approvals SET decision = ?, decided_at = ?, approver = ?"
            " WHERE approval_id = ?",
            (decision, _now_iso(), approver, approval_id),
        )

    _execute_write(data_dir, _do)


# ── orders ───────────────────────────────────────────────
def insert_order(data_dir: Path, order: dict) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO orders("
            " order_id, broker_order_id, account_id, intent_id, symbol, side, qty,"
            " price_policy, limit_price, state, filled_qty, avg_fill_price,"
            " submitted_at, last_update_at, reject_reason"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order["order_id"], order.get("broker_order_id"), order["account_id"],
                order["intent_id"], order["symbol"], order["side"], order["qty"],
                order["price_policy"], order.get("limit_price"), order["state"],
                order.get("filled_qty", 0), order.get("avg_fill_price"),
                order.get("submitted_at"), order.get("last_update_at", _now_iso()),
                order.get("reject_reason"),
            ),
        )

    _execute_write(data_dir, _do)


def get_order(data_dir: Path, order_id: str) -> dict | None:
    row = _query_one(
        data_dir,
        "SELECT * FROM orders WHERE order_id = ?",
        (order_id,),
    )
    return dict(row) if row is not None else None


def get_order_by_broker_id(data_dir: Path, broker_order_id: str) -> dict | None:
    row = _query_one(
        data_dir,
        "SELECT * FROM orders WHERE broker_order_id = ?",
        (broker_order_id,),
    )
    return dict(row) if row is not None else None


def list_orders(
    data_dir: Path,
    status: str | None = None,
    account_id: str | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    sql = "SELECT * FROM orders"
    params: list[object] = []
    where: list[str] = []
    if status:
        where.append("state = ?")
        params.append(status)
    if account_id:
        where.append("account_id = ?")
        params.append(account_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_update_at DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
    return [dict(r) for r in _query(data_dir, sql, tuple(params))]


def update_order_state(
    data_dir: Path,
    order_id: str,
    new_state: str,
    *,
    filled_qty: int | None = None,
    avg_fill_price: float | None = None,
    broker_order_id: str | None = None,
    submitted_at: str | None = None,
    reject_reason: str | None = None,
    event_from_state: str | None = None,
    event_reason: str | None = None,
) -> None:
    """原子更新订单状态 + 同事务写 order_events 审计行。

    业务约定: status 变更必须留下审计事件 (from_state, to_state, reason);
    不附件写入事件视为不变量破坏, 抛 RuntimeError 阻止提交。
    """
    def _do(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT state FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"update_order_state: 订单不存在 {order_id!r}")
        cur_state = row[0]
        conn.execute(
            "UPDATE orders SET state = ?,"
            " filled_qty = COALESCE(?, filled_qty),"
            " avg_fill_price = COALESCE(?, avg_fill_price),"
            " broker_order_id = COALESCE(?, broker_order_id),"
            " submitted_at = COALESCE(?, submitted_at),"
            " reject_reason = COALESCE(?, reject_reason),"
            " last_update_at = ?"
            " WHERE order_id = ?",
            (
                new_state, filled_qty, avg_fill_price, broker_order_id,
                submitted_at, reject_reason, _now_iso(), order_id,
            ),
        )
        conn.execute(
            "INSERT INTO order_events(event_id, order_id, from_state, to_state, reason, ts)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (
                _uuid(), order_id, event_from_state or cur_state, new_state,
                event_reason, _now_iso(),
            ),
        )

    _execute_write(data_dir, _do)


def insert_execution(data_dir: Path, exec_row: dict) -> bool:
    """插入成交回报 (幂等)。dedupe_key 冲突时返回 False (重复回调, 已入账)。"""
    import sqlite3 as _sqlite3

    def _do(conn: sqlite3.Connection) -> bool:
        try:
            conn.execute(
                "INSERT INTO executions("
                " exec_id, order_id, broker_order_id, broker_trade_id, account_id,"
                " symbol, side, qty, price, fee, ts, dedupe_key"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    exec_row["exec_id"], exec_row["order_id"],
                    exec_row.get("broker_order_id"), exec_row["broker_trade_id"],
                    exec_row["account_id"], exec_row["symbol"], exec_row["side"],
                    exec_row["qty"], exec_row["price"], exec_row.get("fee", 0.0),
                    exec_row["ts"], exec_row["dedupe_key"],
                ),
            )
            return True
        except _sqlite3.IntegrityError:
            return False

    return _execute_write(data_dir, _do)


def list_executions(data_dir: Path, order_id: str) -> list[dict]:
    return [
        dict(r) for r in _query(
            data_dir,
            "SELECT * FROM executions WHERE order_id = ? ORDER BY ts",
            (order_id,),
        )
    ]


def list_executions_all(
    data_dir: Path,
    *,
    account_id: str | None = None,
    order_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """成交明细列表 (支持账户/订单过滤 + 分页, 供 /api/live/executions 使用)."""
    sql = "SELECT * FROM executions"
    params: list[object] = []
    where: list[str] = []
    if account_id:
        where.append("account_id = ?")
        params.append(account_id)
    if order_id:
        where.append("order_id = ?")
        params.append(order_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
    return [dict(r) for r in _query(data_dir, sql, tuple(params))]


# ── order_events ─────────────────────────────────────────
def list_order_events(data_dir: Path, order_id: str) -> list[dict]:
    return [
        dict(r) for r in _query(
            data_dir,
            "SELECT * FROM order_events WHERE order_id = ? ORDER BY ts",
            (order_id,),
        )
    ]


# ── account_snapshots / position_snapshots ───────────────
def upsert_account_snapshot(data_dir: Path, snap: dict) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO account_snapshots("
            " account_id, as_of, cash, frozen_cash, market_value, total, nav, mode"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snap["account_id"], snap["as_of"], snap.get("cash", 0),
                snap.get("frozen_cash", 0), snap.get("market_value", 0),
                snap.get("total", 0), snap.get("nav", 0),
                snap.get("mode", TradingMode.DISARMED.value),
            ),
        )

    _execute_write(data_dir, _do)


def latest_account_snapshot(data_dir: Path, account_id: str) -> dict | None:
    row = _query_one(
        data_dir,
        "SELECT * FROM account_snapshots WHERE account_id = ?"
        " ORDER BY as_of DESC LIMIT 1",
        (account_id,),
    )
    return dict(row) if row is not None else None


def upsert_position_snapshot(data_dir: Path, snap: dict) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO position_snapshots("
            " account_id, symbol, as_of, qty, available_qty, avg_cost,"
            " last_price, market_value, pnl"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snap["account_id"], snap["symbol"], snap["as_of"],
                snap.get("qty", 0), snap.get("available_qty", 0),
                snap.get("avg_cost", 0), snap.get("last_price"),
                snap.get("market_value", 0), snap.get("pnl", 0),
            ),
        )

    _execute_write(data_dir, _do)


def latest_positions(data_dir: Path, account_id: str) -> list[dict]:
    rows = _query(
        data_dir,
        "SELECT p.* FROM position_snapshots p"
        " WHERE account_id = ? AND as_of = ("
        "   SELECT MAX(as_of) FROM position_snapshots WHERE account_id = ?"
        " ) ORDER BY symbol",
        (account_id, account_id),
    )
    return [dict(r) for r in rows]


# ── reconciliation ───────────────────────────────────────
def create_recon_run(data_dir: Path, run_id: str, account_id: str, started_at: str) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO reconciliation_runs(run_id, account_id, started_at)"
            " VALUES(?, ?, ?)",
            (run_id, account_id, started_at),
        )

    _execute_write(data_dir, _do)


def finish_recon_run(
    data_dir: Path,
    run_id: str,
    finished_at: str,
    status: str,
    diff_count: int,
) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE reconciliation_runs SET finished_at = ?, status = ?, diff_count = ?"
            " WHERE run_id = ?",
            (finished_at, status, diff_count, run_id),
        )

    _execute_write(data_dir, _do)


def insert_recon_diff(data_dir: Path, diff: dict) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO reconciliation_diffs("
            " diff_id, run_id, order_id, diff_kind, local_value, broker_value,"
            " resolved, resolved_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                diff["diff_id"], diff["run_id"], diff.get("order_id"),
                diff["diff_kind"], diff.get("local_value"), diff.get("broker_value"),
                int(diff.get("resolved", False)),
                diff.get("resolved_at"),
            ),
        )

    _execute_write(data_dir, _do)


def list_recon_runs(data_dir: Path, account_id: str | None = None) -> list[dict]:
    if account_id:
        return [
            dict(r) for r in _query(
                data_dir,
                "SELECT * FROM reconciliation_runs WHERE account_id = ?"
                " ORDER BY started_at DESC LIMIT 50",
                (account_id,),
            )
        ]
    return [
        dict(r) for r in _query(
            data_dir,
            "SELECT * FROM reconciliation_runs ORDER BY started_at DESC LIMIT 50",
        )
    ]


def list_recon_diffs(data_dir: Path, run_id: str) -> list[dict]:
    return [
        dict(r) for r in _query(
            data_dir,
            "SELECT * FROM reconciliation_diffs WHERE run_id = ? ORDER BY diff_id",
            (run_id,),
        )
    ]


def unresolved_diff_count(data_dir: Path, account_id: str | None = None) -> int:
    sql = (
        "SELECT COUNT(*) FROM reconciliation_diffs d"
        " JOIN reconciliation_runs r ON r.run_id = d.run_id"
        " WHERE d.resolved = 0"
    )
    params: tuple[object, ...] = ()
    if account_id:
        sql += " AND r.account_id = ?"
        params = (account_id,)
    row = _query_one(data_dir, sql, params)
    return int(row[0]) if row is not None else 0


def resolve_diff(data_dir: Path, diff_id: str) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE reconciliation_diffs SET resolved = 1, resolved_at = ?"
            " WHERE diff_id = ?",
            (_now_iso(), diff_id),
        )

    _execute_write(data_dir, _do)


# ── system_events ────────────────────────────────────────
def log_system_event(data_dir: Path, kind: str, detail: dict) -> None:
    def _do(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO system_events(event_id, kind, detail, ts) VALUES(?, ?, ?, ?)",
            (_uuid(), kind, json.dumps(detail, ensure_ascii=False), _now_iso()),
        )

    _execute_write(data_dir, _do)


def recent_system_events(data_dir: Path, limit: int = 50) -> list[dict]:
    return [
        dict(r) for r in _query(
            data_dir,
            "SELECT * FROM system_events ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
    ]


# ── 工具 ─────────────────────────────────────────────────
def _uuid() -> str:
    import uuid
    return uuid.uuid4().hex


def list_pending_intent_row(data_dir: Path, intent_id: str) -> dict | None:
    return get_intent(data_dir, intent_id)


# ── 持仓 (可卖数量, 风控用) ─────────────────────────────
def available_qty(data_dir: Path, account_id: str, symbol: str) -> int:
    """券商口径可卖数量 (T+1 已计入)。从最近一次持仓快照读; 未对账时返回 0。"""
    rows = _query(
        data_dir,
        "SELECT available_qty FROM position_snapshots"
        " WHERE account_id = ? AND symbol = ?"
        " ORDER BY as_of DESC LIMIT 1",
        (account_id, symbol),
    )
    return int(rows[0][0]) if rows else 0