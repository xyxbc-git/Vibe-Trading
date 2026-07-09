#!/usr/bin/env python3
"""贾维斯 JARVIS — 回测历史持久化（backtest_runs 表）。

解决「跑过的回测找不回来」：每次 /api/backtest/run 完成后自动落库，
提供列表（分页）与详情（可完整重现结果页）两个查询端点。

存储：独立 SQLite ~/.vibe-trading/backtest_history.db（WAL），
不挂在 jarvis_journal 主库上——历史记录含大 JSON（逐笔成交/资金曲线），
独立文件避免污染决策库备份与 pg 迁移路径。

用法（CLI 自检）：
  python jarvis_backtest_history.py list
  python jarvis_backtest_history.py show --id 1
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from typing import Any

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(CONFIG_DIR, "backtest_history.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    strategy_name TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    timeframe TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    capital REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    total_return_pct REAL NOT NULL DEFAULT 0,
    win_rate REAL NOT NULL DEFAULT 0,
    profit_factor REAL NOT NULL DEFAULT 0,
    max_drawdown_pct REAL NOT NULL DEFAULT 0,
    sharpe_ratio REAL NOT NULL DEFAULT 0,
    total_trades INTEGER NOT NULL DEFAULT 0,
    diagnosis TEXT,
    error TEXT,
    trades_json TEXT,
    equity_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_created ON backtest_runs(created_at DESC);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)


def record_run(params: dict[str, Any] | None, result: dict[str, Any] | None,
               error: str | None = None) -> int | None:
    """回测完成（成功或失败）后落一条历史；返回记录 id，失败返回 None（不阻断主流程）。"""
    params = params or {}
    result = result or {}
    try:
        init_db()
        raw = result.get("raw") or {}
        equity = raw.get("equity_curve") or raw.get("equityCurve") or []
        trades = result.get("trades") or []
        with _conn() as conn:
            cur = conn.execute(
                """INSERT INTO backtest_runs
                   (created_at, strategy_name, symbol, timeframe, start_date, end_date,
                    capital, status, total_return_pct, win_rate, profit_factor,
                    max_drawdown_pct, sharpe_ratio, total_trades, diagnosis, error,
                    trades_json, equity_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    str(params.get("name") or params.get("strategy_name") or "custom"),
                    str(params.get("symbol") or ""),
                    str(params.get("timeframe") or ""),
                    str(params.get("start") or params.get("start_date") or ""),
                    str(params.get("end") or params.get("end_date") or ""),
                    float(params.get("capital") or params.get("initial_capital") or 0),
                    str(result.get("status") or ("error" if error else "unknown")),
                    float(result.get("total_return_pct") or 0),
                    float(result.get("win_rate") or 0),
                    float(result.get("profit_factor") or 0),
                    float(result.get("max_drawdown_pct") or 0),
                    float(result.get("sharpe_ratio") or 0),
                    int(result.get("total_trades") or 0),
                    result.get("diagnosis"),
                    error or (result.get("error") if result.get("status") != "succeeded" else None),
                    json.dumps(trades, ensure_ascii=False),
                    json.dumps(equity, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)
    except Exception:  # noqa: BLE001 — 历史落库失败绝不影响回测主链路
        return None


def list_runs(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """分页列表（不含 trades/equity 大字段）。"""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    init_db()
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM backtest_runs").fetchone()["n"]
        rows = conn.execute(
            """SELECT id, created_at, strategy_name, symbol, timeframe, start_date,
                      end_date, capital, status, total_return_pct, win_rate,
                      profit_factor, max_drawdown_pct, sharpe_ratio, total_trades,
                      diagnosis, error
               FROM backtest_runs ORDER BY id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [dict(r) for r in rows]}


def get_run(run_id: int) -> dict[str, Any] | None:
    """详情：含逐笔成交与资金曲线，可完整重现结果页。"""
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM backtest_runs WHERE id = ?", (int(run_id),),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key, target in (("trades_json", "trades"), ("equity_json", "equity_curve")):
        try:
            d[target] = json.loads(d.pop(key) or "[]")
        except (ValueError, TypeError):
            d[target] = []
    return d


# ═══════════════════════════ FastAPI 路由（dashboard include 用） ═══════════════════════════

try:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/api/backtest", tags=["backtest-history"])

    @router.get("/history")
    def api_history(limit: int = 20, offset: int = 0):
        """回测历史列表（分页，倒序；不含大 JSON 字段）。"""
        try:
            return JSONResponse(list_runs(limit=limit, offset=offset))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=500)

    @router.get("/history/{run_id}")
    def api_history_detail(run_id: int):
        """单次回测详情（含逐笔成交与资金曲线，可重现结果）。"""
        try:
            run = get_run(run_id)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=500)
        if run is None:
            return JSONResponse({"error": f"找不到回测记录 id={run_id}"}, status_code=404)
        return JSONResponse(run)

except ImportError:  # CLI 场景无需 fastapi
    router = None


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="回测历史持久化")
    sub = parser.add_subparsers(dest="cmd")
    p_list = sub.add_parser("list", help="列出历史")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--offset", type=int, default=0)
    p_show = sub.add_parser("show", help="查看详情")
    p_show.add_argument("--id", type=int, required=True)
    args = parser.parse_args()
    if args.cmd == "list":
        print(json.dumps(list_runs(args.limit, args.offset), ensure_ascii=False, indent=2))
    elif args.cmd == "show":
        print(json.dumps(get_run(args.id) or {"error": "not found"}, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
