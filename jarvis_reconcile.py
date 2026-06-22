#!/usr/bin/env python3
"""贾维斯 JARVIS — M2 闭环对账：决策 ↔ QuantDinger 成交（按订单号关联）。

整合计划 M2：把 `jarvis_journal` 的决策快照与 QuantDinger 的真实成交按
**订单号(order_uid)** 关联，用真实成交价/盈亏回填，替代纯价格估算，产出
「贾维斯说的 vs 实际成交」对账战绩。

链路：
  1. jarvis_executor 下 paper 单成功 → `link_order()` 把 order_uid 连同
     当时的决策参考入场价写进 journal DB 的 `executions` 表（订单号关联点）。
  2. `reconcile()` 经 Agent Gateway `GET /portfolio/paper-orders` 按 order_uid
     回查真实成交价/价值/状态，回填到 executions（真实成交回填）。
  3. `report()` 对每笔：决策参考价 vs 真实成交价（入场滑点），并在 journal
     已评估前向收益时换算「按真实成交价」的实际盈亏，输出对账表。

存储：复用 `~/.vibe-trading/jarvis_journal.db`（与决策快照同库，便于 JOIN）。

用法：
  export QUANTDINGER_AGENT_TOKEN=qd_agent_xxx
  python jarvis_reconcile.py reconcile          # 拉真实成交回填
  python jarvis_reconcile.py report             # 看决策 vs 实际成交对账
  python jarvis_reconcile.py list               # 列已关联订单
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

import jarvis_journal as jj

# 复用 executor 的配置加载（gateway/token/market），避免重复实现。
try:
    import jarvis_executor as jx
    _load_cfg = jx.load_config
except Exception:  # noqa: BLE001 — executor 不可用时退化为自带最小配置
    def _load_cfg(cli: dict | None = None) -> dict:
        cfg = {
            "gateway_base": os.getenv("QUANTDINGER_GATEWAY_BASE", "http://localhost:8888"),
            "agent_token": os.getenv("QUANTDINGER_AGENT_TOKEN", ""),
            "request_timeout_s": 30,
        }
        if cli:
            cfg.update({k: v for k, v in cli.items() if v is not None})
        return cfg


# ─────────────────────────── executions 表 ───────────────────────────

def init_exec_table() -> None:
    jj.init_db()
    with jj._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS executions (
                order_uid      TEXT    PRIMARY KEY,
                snapshot_id    INTEGER,
                symbol         TEXT    NOT NULL,
                as_of_date     TEXT,
                side           TEXT,
                qty            REAL,
                decision_price REAL,
                fill_price     REAL,
                fill_value     REAL,
                status         TEXT,
                reconciled_ts  REAL,
                created_ts     REAL    NOT NULL
            )
            """
        )


def link_order(
    *, order_uid: str, symbol: str, as_of_date: str | None,
    side: str, qty: float, decision_price: float | None,
    status: str | None = None, snapshot_id: int | None = None,
) -> dict:
    """下单成功后登记订单号关联（幂等：同 order_uid 重复登记会更新）。

    若未显式给 snapshot_id，按 (symbol, as_of_date) 反查 journal 快照补全。
    永不抛出——对账登记失败不应影响主下单流程。
    """
    try:
        init_exec_table()
        if snapshot_id is None and as_of_date:
            with jj._conn() as conn:
                r = conn.execute(
                    "SELECT id FROM snapshots WHERE symbol=? AND as_of_date=?",
                    (symbol.upper(), as_of_date),
                ).fetchone()
                snapshot_id = r["id"] if r else None
        with jj._conn() as conn:
            conn.execute(
                """
                INSERT INTO executions
                  (order_uid, snapshot_id, symbol, as_of_date, side, qty,
                   decision_price, fill_price, fill_value, status, reconciled_ts, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?)
                ON CONFLICT(order_uid) DO UPDATE SET
                  snapshot_id=excluded.snapshot_id,
                  decision_price=excluded.decision_price,
                  status=excluded.status
                """,
                (order_uid, snapshot_id, symbol.upper(), as_of_date, side, qty,
                 decision_price, status, time.time()),
            )
        return {"ok": True, "order_uid": order_uid, "snapshot_id": snapshot_id}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:300]}


# ─────────────────────────── 拉真实成交 ───────────────────────────

def pull_fills(cfg: dict) -> dict:
    """GET /portfolio/paper-orders → {order_uid: order_row}。"""
    url = f"{cfg['gateway_base'].rstrip('/')}/api/agent/v1/portfolio/paper-orders"
    headers = {"Authorization": f"Bearer {cfg.get('agent_token','')}"}
    resp = requests.get(url, headers=headers, timeout=int(cfg.get("request_timeout_s", 30)))
    resp.raise_for_status()
    body = resp.json()
    rows = (body or {}).get("data") or []
    out = {}
    for r in rows:
        uid = r.get("order_uid")
        if uid:
            out[uid] = r
    return out


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def reconcile(cfg: dict | None = None) -> dict:
    """按 order_uid 把真实成交价/价值/状态回填到 executions。"""
    cfg = cfg or _load_cfg()
    init_exec_table()
    if not cfg.get("agent_token"):
        return {"ok": False, "error": "未配置 agent_token（设 env QUANTDINGER_AGENT_TOKEN）"}
    try:
        fills = pull_fills(cfg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"拉取 paper-orders 失败: {exc!r}"[:300]}

    with jj._conn() as conn:
        links = [dict(r) for r in conn.execute("SELECT * FROM executions").fetchall()]

    updated = 0
    not_found = 0
    for lk in links:
        uid = lk["order_uid"]
        fill = fills.get(uid)
        if not fill:
            not_found += 1
            continue
        with jj._conn() as conn:
            conn.execute(
                """
                UPDATE executions
                SET fill_price=?, fill_value=?, status=?, reconciled_ts=?
                WHERE order_uid=?
                """,
                (_f(fill.get("fill_price")), _f(fill.get("fill_value")),
                 fill.get("status"), time.time(), uid),
            )
        updated += 1
    return {"ok": True, "linked": len(links), "reconciled": updated,
            "not_found_in_quantdinger": not_found, "remote_orders": len(fills)}


# ─────────────────────────── 对账报表 ───────────────────────────

def build_report(symbol: str | None = None) -> dict:
    """生成「决策 vs 实际成交」对账数据。

    对每笔已关联订单：
      - 决策参考价 decision_price vs 真实成交价 fill_price → 入场滑点。
      - 若 journal 已评估该快照的前向收益，换算「按真实成交价」的实际盈亏。
    """
    init_exec_table()
    with jj._conn() as conn:
        q = """
            SELECT e.*, s.direction, s.conviction_score, s.position_pct,
                   s.price AS snap_price,
                   o7.fwd_price AS fwd7, o7.fwd_ret_pct AS ret7,
                   o30.fwd_price AS fwd30, o30.fwd_ret_pct AS ret30
            FROM executions e
            LEFT JOIN snapshots s ON s.id = e.snapshot_id
            LEFT JOIN outcomes o7  ON o7.snapshot_id = e.snapshot_id AND o7.horizon=7
            LEFT JOIN outcomes o30 ON o30.snapshot_id = e.snapshot_id AND o30.horizon=30
        """
        params: list = []
        if symbol:
            q += " WHERE e.symbol = ?"
            params.append(symbol.upper())
        q += " ORDER BY e.created_ts DESC"
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]

    items = []
    filled_n = 0
    slippages = []
    for r in rows:
        dp = _f(r.get("decision_price"))
        fp = _f(r.get("fill_price"))
        slip = None
        if dp and fp and dp > 0:
            slip = round((fp - dp) / dp * 100, 3)
            slippages.append(slip)
        if fp:
            filled_n += 1
        # 用真实成交价换算实际盈亏（多头：(fwd-fill)/fill）。
        real_pnl = {}
        for hz, fwd in (("7d", _f(r.get("fwd7"))), ("30d", _f(r.get("fwd30")))):
            if fp and fwd and fp > 0 and (r.get("side") or "").lower() == "buy":
                real_pnl[hz] = round((fwd - fp) / fp * 100, 2)
        items.append({
            "order_uid": r["order_uid"],
            "symbol": r["symbol"],
            "as_of_date": r.get("as_of_date"),
            "direction": r.get("direction"),
            "side": r.get("side"),
            "qty": r.get("qty"),
            "decision_price": dp,
            "fill_price": fp,
            "fill_value": _f(r.get("fill_value")),
            "status": r.get("status"),
            "entry_slippage_pct": slip,
            "estimated_ret": {"7d": _f(r.get("ret7")), "30d": _f(r.get("ret30"))},
            "real_fill_ret": real_pnl,
        })
    return {
        "symbol": symbol.upper() if symbol else "ALL",
        "linked_orders": len(rows),
        "filled_orders": filled_n,
        "avg_entry_slippage_pct": round(sum(slippages) / len(slippages), 3) if slippages else None,
        "items": items,
        "db_path": jj.DB_PATH,
    }


def to_markdown(rep: dict) -> str:
    lines = [
        f"# 贾维斯 × QuantDinger 对账 — {rep['symbol']}",
        "",
        f"- 已关联订单: {rep['linked_orders']} 笔 | 已真实成交: {rep['filled_orders']} 笔",
        f"- 平均入场滑点: {rep['avg_entry_slippage_pct'] if rep['avg_entry_slippage_pct'] is not None else '—'}%",
        f"- 数据库: `{rep['db_path']}`",
    ]
    if not rep["items"]:
        lines += ["", "> 暂无关联订单。先让 jarvis_executor 下一笔 paper 单，再 `reconcile`。"]
        return "\n".join(lines)
    lines += [
        "",
        "| 日期 | 币种 | 方向 | 决策价 | 成交价 | 滑点% | 状态 | 估算30d | 真实30d |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for it in rep["items"]:
        est30 = it["estimated_ret"].get("30d")
        real30 = it["real_fill_ret"].get("30d")
        lines.append(
            f"| {it.get('as_of_date') or '—'} | {it['symbol']} | {it.get('direction') or '—'} "
            f"| {it.get('decision_price') if it.get('decision_price') is not None else '—'} "
            f"| {it.get('fill_price') if it.get('fill_price') is not None else '待成交'} "
            f"| {it.get('entry_slippage_pct') if it.get('entry_slippage_pct') is not None else '—'} "
            f"| {it.get('status') or '—'} "
            f"| {str(est30) + '%' if est30 is not None else '待评估'} "
            f"| {str(real30) + '%' if real30 is not None else '待评估'} |"
        )
    return "\n".join(lines)


def list_links(symbol: str | None = None) -> list:
    init_exec_table()
    with jj._conn() as conn:
        q = "SELECT * FROM executions"
        params: list = []
        if symbol:
            q += " WHERE symbol = ?"
            params.append(symbol.upper())
        q += " ORDER BY created_ts DESC"
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 M2 闭环对账：决策 ↔ QuantDinger 成交")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("reconcile", help="按订单号拉真实成交回填")
    p_rec.add_argument("--gateway", default=None)
    p_rec.add_argument("--json", action="store_true")

    p_rep = sub.add_parser("report", help="决策 vs 实际成交对账表")
    p_rep.add_argument("--symbol", default=None)
    p_rep.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="列已关联订单")
    p_list.add_argument("--symbol", default=None)
    p_list.add_argument("--json", action="store_true")

    args = ap.parse_args()

    if args.cmd == "reconcile":
        cfg = _load_cfg({"gateway_base": args.gateway} if args.gateway else None)
        out = reconcile(cfg)
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else (f"✅ 对账完成: 关联 {out['linked']} 笔, 回填 {out['reconciled']} 笔, "
                    f"远端 {out['remote_orders']} 单, 未匹配 {out['not_found_in_quantdinger']} 笔"
                    if out.get("ok") else f"❌ 失败: {out.get('error')}"))
    elif args.cmd == "report":
        rep = build_report(args.symbol)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else to_markdown(rep))
    elif args.cmd == "list":
        rows = list_links(args.symbol)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
