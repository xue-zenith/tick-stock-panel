"""端到端冒烟: 全市场日K同步 (sync_and_persist_daily_batch)。

用隔离的临时 data_dir, 不污染真实数据。验证:
1. instruments 全量拉取成功
2. sync_daily_batch 并发版全标的覆盖
3. parquet 落盘正确、DuckDB 可读
4. 行数/标的数一致、失败率可接受

免费档 60/min 限流: batch=100, rpm=30 → ~59 块 × 2s = ~118s sleep + 网络。
只拉 3 天数据减少单请求负载, 但保留全标的数以验证并发正确性。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from app.services import preferences
from app.services.instrument_sync import sync_instruments
from app.services.kline_sync import sync_and_persist_daily_batch
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# 压制 SDK 横幅和噪声
logging.getLogger("tickflow").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("smoke")


def main() -> int:
    end_date = datetime(2026, 9, 26)          # 最近一个交易日窗口
    start_date = end_date - timedelta(days=5)  # 3 个交易日左右

    with tempfile.TemporaryDirectory(prefix="smoke_e2e_") as tmp:
        data_dir = Path(tmp)
        log.info("隔离 data_dir: %s", data_dir)

        # ── 1. instruments 全量 ─────────────────────────────────────────
        t0 = time.perf_counter()
        n_inst = sync_instruments(data_dir)
        t1 = time.perf_counter()
        log.info("instruments: %d 行, %.2fs", n_inst, t1 - t0)
        if n_inst == 0:
            log.error("FAIL: instruments 为空, 无法继续")
            return 2

        # 读取 instruments 拿全市场标的
        inst_path = data_dir / "instruments" / "instruments.parquet"
        inst_df = pl.read_parquet(inst_path)
        symbols = inst_df["symbol"].to_list()
        log.info("全市场标的数: %d", len(symbols))

        # ── 2. 构造 repo + capset ───────────────────────────────────────
        store = DataStore(data_dir=data_dir)
        repo = KlineRepository(store=store)
        # 模拟付费档: KLINE_DAILY_BATCH, rpm=30, batch=100
        capset = CapabilitySet({
            Cap.KLINE_DAILY_BATCH: CapabilityLimits(rpm=30, batch=100),
        })

        # 强制走 TickFlow 路径 (sync_daily_batch), 而非用户配置的 custom provider,
        # 这样才验证我们改造的并发版
        _orig_provider = preferences.get_daily_data_provider
        preferences.get_daily_data_provider = lambda: "tickflow"
        log.info("强制 daily_data_provider=tickflow 以验证 sync_daily_batch 并发版")

        # ── 3. 端到端 sync_and_persist_daily_batch ──────────────────────
        beats: list[tuple[int, int]] = []
        failed_syms: list[str] = []
        zero_row: list[str] = []

        def _progress(cur: int, tot: int) -> None:
            beats.append((cur, tot))
            # 每 10 块打一次进度
            if cur % 10 == 0 or cur == tot:
                log.info("进度 %d/%d", cur, tot)

        t0 = time.perf_counter()
        written = sync_and_persist_daily_batch(
            symbols,
            repo=repo,
            capset=capset,
            start_date=start_date,
            end_date=end_date,
            on_chunk_done=_progress,
            zero_row_out=zero_row,
        )
        t1 = time.perf_counter()
        log.info("sync_and_persist_daily_batch: 写入 %d 行, %.2fs", written, t1 - t0)
        preferences.get_daily_data_provider = _orig_provider  # 还原

        # ── 4. 校验落盘 ─────────────────────────────────────────────────
        daily_dir = data_dir / "kline_daily"
        parquet_files = list(daily_dir.rglob("*.parquet"))
        log.info("parquet 分区文件数: %d", len(parquet_files))

        if not parquet_files:
            log.error("FAIL: 无 parquet 落盘")
            return 3

        # DuckDB 读回校验
        try:
            import duckdb
            con = duckdb.connect()
            result = con.execute(
                f"SELECT COUNT(*) as rows, "
                f"COUNT(DISTINCT symbol) as symbols, "
                f"MIN(date) as min_d, MAX(date) as max_d "
                f"FROM read_parquet('{daily_dir.as_posix()}/**/*.parquet', union_by_name=true)"
            ).fetchone()
            rows, syms, min_d, max_d = result
            log.info("DuckDB 读回: %d 行, %d 标的, 日期 %s ~ %s",
                     rows, syms, min_d, max_d)
        except Exception as e:
            log.error("FAIL: DuckDB 读回失败: %s", e)
            return 4

        # ── 5. 汇总 ─────────────────────────────────────────────────────
        log.info("=" * 60)
        log.info("端到端冒烟结果汇总")
        log.info("=" * 60)
        log.info("instruments:      %d 标的", len(symbols))
        log.info("心跳数:            %d (应为 %d 块)", len(beats), (len(symbols) + 99) // 100)
        log.info("写入行数:          %d", written)
        log.info("落盘标的:          %d / %d (%.1f%%)",
                 syms, len(symbols), 100 * syms / len(symbols) if symbols else 0)
        log.info("0 行标的:          %d", len(zero_row))
        log.info("耗时:              %.1fs", t1 - t0)

        ok = True
        if beats and beats[-1][0] != beats[-1][1]:
            log.error("FAIL: 最后心跳未到 %d/%d", beats[-1][0], beats[-1][1])
            ok = False
        if syms < len(symbols) * 0.90:
            log.error("FAIL: 落盘标的覆盖率 %.1f%% < 90%%", 100 * syms / len(symbols))
            ok = False
        if rows == 0:
            log.error("FAIL: 0 行数据落盘")
            ok = False

        if ok:
            log.info("PASS: 端到端冒烟通过 — 并发版全市场日K同步正确")
            return 0
        log.error("FAIL: 端到端冒烟未通过")
        return 1


if __name__ == "__main__":
    sys.exit(main())
