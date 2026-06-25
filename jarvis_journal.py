#!/usr/bin/env python3
"""贾维斯 JARVIS - 决策快照落库 + 前向准确率追踪。

闭环最后一块：让贾维斯「记得自己当时怎么说，后来准不准」。

  1) record  : 跑一次 jarvis_brief，把决策快照（价格/信心分/方向/信号/风控）落到 SQLite。
  2) evaluate: 对历史快照，用真实日线价格回填 7/30 天后的实际收益，判定方向是否「踩对」。
  3) report  : 按方向 / 信号统计前向收益与命中率，输出贾维斯的真实战绩。

设计原则：
  - 不偷看未来：只评估「as_of_date + horizon <= 最新可得收盘日」的快照。
  - 价格不落库做事实源，落库的是「决策」；收益用最新真实日线现算，避免脏数据。
  - 方向判定：偏多→看 ret>0 为对；偏空/观望→看 ret<0 为对；纯中性不计入命中率。

存储：~/.vibe-trading/jarvis_journal.db （与 Vibe-Trading 同目录，便于统一管理）

用法：
  python jarvis_journal.py record BTCUSDT          # 落一条今日快照
  python jarvis_journal.py evaluate                # 回填收益
  python jarvis_journal.py report                  # 看战绩
  python jarvis_journal.py report --symbol BTCUSDT --json
  python jarvis_journal.py list                    # 列最近快照
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

import jarvis_brief as jb
import jarvis_weights as jw
from jarvis_factor_backtest import _build_series, fetch_fng_all, fetch_price_daily

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")

HORIZONS = (7, 30)


def _conn() -> sqlite3.Connection:
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol            TEXT    NOT NULL,
                generated_at_utc  TEXT    NOT NULL,
                as_of_date        TEXT    NOT NULL,
                price             REAL    NOT NULL,
                conviction_score  REAL,
                direction         TEXT,
                position_pct      REAL,
                dd_pct            REAL,
                fng               INTEGER,
                above_ma200       INTEGER,
                dd30_active       INTEGER,
                stop_loss         REAL,
                take_profit       REAL,
                decision_json     TEXT,
                created_ts        REAL    NOT NULL,
                UNIQUE(symbol, as_of_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outcomes (
                snapshot_id  INTEGER NOT NULL,
                horizon      INTEGER NOT NULL,
                fwd_date     TEXT,
                fwd_price    REAL,
                fwd_ret_pct  REAL,
                correct      INTEGER,
                evaluated_ts REAL,
                PRIMARY KEY (snapshot_id, horizon)
            )
            """
        )


def record(symbol: str) -> dict:
    """跑一次简报并落库（同一 symbol+as_of_date 幂等，重复 record 会更新）。"""
    init_db()
    b = jb.build(symbol)
    fac = b.get("factor_state", {})
    dec = b.get("decision", {})
    if "_error" in fac or "_error" in dec:
        return {"ok": False, "error": fac.get("_error") or dec.get("_error")}

    row = {
        "symbol": b["symbol"],
        "generated_at_utc": b["generated_at_utc"],
        "as_of_date": fac.get("as_of"),
        "price": fac.get("price"),
        "conviction_score": dec.get("conviction_score"),
        "direction": dec.get("direction"),
        "position_pct": dec.get("suggested_position_pct"),
        "dd_pct": fac.get("drawdown_from_ath_pct"),
        "fng": b.get("real_data", {}).get("fear_greed", {}).get("fng_value"),
        "above_ma200": 1 if fac.get("above_ma200") else 0,
        "dd30_active": 1 if fac.get("dd30_signal_active") else 0,
        "stop_loss": dec.get("stop_loss"),
        "take_profit": dec.get("take_profit_ref"),
        "decision_json": json.dumps(dec, ensure_ascii=False),
        "created_ts": time.time(),
    }
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO snapshots
              (symbol, generated_at_utc, as_of_date, price, conviction_score, direction,
               position_pct, dd_pct, fng, above_ma200, dd30_active, stop_loss, take_profit,
               decision_json, created_ts)
            VALUES
              (:symbol, :generated_at_utc, :as_of_date, :price, :conviction_score, :direction,
               :position_pct, :dd_pct, :fng, :above_ma200, :dd30_active, :stop_loss, :take_profit,
               :decision_json, :created_ts)
            ON CONFLICT(symbol, as_of_date) DO UPDATE SET
               generated_at_utc=excluded.generated_at_utc,
               price=excluded.price,
               conviction_score=excluded.conviction_score,
               direction=excluded.direction,
               position_pct=excluded.position_pct,
               dd_pct=excluded.dd_pct,
               fng=excluded.fng,
               above_ma200=excluded.above_ma200,
               dd30_active=excluded.dd30_active,
               stop_loss=excluded.stop_loss,
               take_profit=excluded.take_profit,
               decision_json=excluded.decision_json
            """,
            row,
        )
        sid = cur.lastrowid
    return {"ok": True, "snapshot_id": sid, **row}


def _price_only_decision(dd: float, fng_val: int | None, above_ma200: bool) -> tuple[float, str, float]:
    """仅用历史可得的「价格因子」重建决策（不含衍生品，因为历史 funding/多空比难回溯）。

    复刻 jarvis_brief.score_and_plan 的价格相关部分，保证回测与实盘口径一致：
      因子权重与方向阈值统一从 jarvis_weights 读取（可被重训覆盖），
      配置缺失时回退内置默认（= 历史硬编码原值），保证零回归。
      - 回撤≤-30%（dd30_dip）
      - F&G<20 且 价<200MA（fear_in_downtrend）
      - F&G<20 且 价>200MA（fear_in_uptrend）
      - 价>200MA（ma200_above）/ 价<200MA（ma200_below）
    """
    W = jw.get_weights()
    TH = jw.get_thresholds()
    score = 0.0
    if dd <= -0.30:
        score += W["dd30_dip"]
    if fng_val is not None and fng_val < 20:
        if not above_ma200:
            score += W["fear_in_downtrend"]
        else:
            score += W["fear_in_uptrend"]
    if above_ma200:
        score += W["ma200_above"]
    else:
        score += W["ma200_below"]
    score = round(max(-2.0, min(2.0, score)), 2)
    if score >= TH["long"]:
        direction, pos = "偏多（战术）", min(0.4, 0.2 + score * 0.1)
    elif score <= TH["short"]:
        direction, pos = "偏空（战术）", min(0.4, 0.2 + abs(score) * 0.1)
    else:
        direction, pos = "中性观望", 0.1 if score > 0 else 0.0
    return score, direction, round(pos * 100, 0)


def backfill(symbol: str = "BTCUSDT", step_days: int = 7) -> dict:
    """用真实历史日线 + F&G + 200MA 重建过去的价格因子决策并落库，立刻产出历史战绩。"""
    init_db()
    sym = symbol.upper()
    prices = fetch_price_daily(sym)
    fng = fetch_fng_all()
    if not prices or len(prices) < 230:
        return {"ok": False, "error": "价格历史不足"}
    dates = sorted(prices)
    closes, ma200, dd = _build_series(dates, prices)
    inserted = 0
    # 从 200MA 可用处开始，每 step_days 落一条，留出 30 天评估窗口
    for i in range(200, len(dates) - 1, step_days):
        d = dates[i]
        if ma200[i] is None:
            continue
        above = closes[i] > ma200[i]
        fng_val = fng.get(d)
        score, direction, pos = _price_only_decision(dd[i], fng_val, above)
        row = {
            "symbol": sym,
            "generated_at_utc": d + " (backfill)",
            "as_of_date": d,
            "price": round(closes[i], 2),
            "conviction_score": score,
            "direction": direction,
            "position_pct": pos,
            "dd_pct": round(dd[i] * 100, 2),
            "fng": fng_val,
            "above_ma200": 1 if above else 0,
            "dd30_active": 1 if dd[i] <= -0.30 else 0,
            "stop_loss": round(closes[i] * 0.90, 2) if pos > 0 else None,
            "take_profit": round(closes[i] * 1.08, 2) if pos > 0 else None,
            "decision_json": json.dumps({"source": "backfill", "conviction_score": score}, ensure_ascii=False),
            "created_ts": time.time(),
        }
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO snapshots
                  (symbol, generated_at_utc, as_of_date, price, conviction_score, direction,
                   position_pct, dd_pct, fng, above_ma200, dd30_active, stop_loss, take_profit,
                   decision_json, created_ts)
                VALUES
                  (:symbol, :generated_at_utc, :as_of_date, :price, :conviction_score, :direction,
                   :position_pct, :dd_pct, :fng, :above_ma200, :dd30_active, :stop_loss, :take_profit,
                   :decision_json, :created_ts)
                ON CONFLICT(symbol, as_of_date) DO NOTHING
                """,
                row,
            )
            inserted += conn.total_changes
    return {"ok": True, "symbol": sym, "candidate_dates": len(range(200, len(dates) - 1, step_days)), "inserted": inserted}


def _direction_correct(direction: str, ret_pct: float) -> int | None:
    """方向是否踩对：偏多→涨为对；偏空/观望→跌为对；纯中性不计入。"""
    if direction is None:
        return None
    if "偏多" in direction:
        return 1 if ret_pct > 0 else 0
    if "偏空" in direction:
        return 1 if ret_pct < 0 else 0
    # 中性观望：不计入命中率（返回 None）
    return None


def evaluate(symbol: str | None = None) -> dict:
    """对已到期的快照回填前向收益与命中判定。"""
    init_db()
    with _conn() as conn:
        q = "SELECT * FROM snapshots"
        params: list = []
        if symbol:
            q += " WHERE symbol = ?"
            params.append(symbol.upper())
        snaps = [dict(r) for r in conn.execute(q, params).fetchall()]

    # 按 symbol 拉一次价格，避免重复请求
    price_cache: dict[str, dict] = {}
    filled = 0
    skipped = 0
    for s in snaps:
        sym = s["symbol"]
        if sym not in price_cache:
            price_cache[sym] = fetch_price_daily(sym)
        prices = price_cache[sym]
        if not prices:
            continue
        dates = sorted(prices)
        latest = dates[-1]
        try:
            base_idx = dates.index(s["as_of_date"])
        except ValueError:
            # as_of_date 不在日线序列里（可能是非收盘对齐），用最接近的前一日
            base_idx = max((i for i, d in enumerate(dates) if d <= s["as_of_date"]), default=-1)
            if base_idx < 0:
                continue
        base_price = prices[dates[base_idx]]
        for h in HORIZONS:
            tgt_idx = base_idx + h
            if tgt_idx >= len(dates):
                skipped += 1
                continue  # 还没到期
            fwd_date = dates[tgt_idx]
            fwd_price = prices[fwd_date]
            ret_pct = round((fwd_price / base_price - 1.0) * 100, 2)
            correct = _direction_correct(s["direction"], ret_pct)
            with _conn() as conn:
                conn.execute(
                    """
                    INSERT INTO outcomes (snapshot_id, horizon, fwd_date, fwd_price, fwd_ret_pct, correct, evaluated_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(snapshot_id, horizon) DO UPDATE SET
                        fwd_date=excluded.fwd_date,
                        fwd_price=excluded.fwd_price,
                        fwd_ret_pct=excluded.fwd_ret_pct,
                        correct=excluded.correct,
                        evaluated_ts=excluded.evaluated_ts
                    """,
                    (s["id"], h, fwd_date, fwd_price, ret_pct,
                     correct if correct is not None else None, time.time()),
                )
            filled += 1
    return {"ok": True, "snapshots": len(snaps), "outcomes_filled": filled, "not_due": skipped}


def report(symbol: str | None = None) -> dict:
    """统计战绩：总快照、已评估、按方向/信号的平均前向收益与命中率。"""
    init_db()
    with _conn() as conn:
        sq = "SELECT * FROM snapshots"
        params: list = []
        if symbol:
            sq += " WHERE symbol = ?"
            params.append(symbol.upper())
        snaps = {r["id"]: dict(r) for r in conn.execute(sq, params).fetchall()}
        outs = [dict(r) for r in conn.execute(
            "SELECT * FROM outcomes WHERE snapshot_id IN (%s)" % (
                ",".join(str(i) for i in snaps) or "0"
            )
        ).fetchall()]

    def _agg(rows: list) -> dict:
        if not rows:
            return {"n": 0}
        rets = [r["fwd_ret_pct"] for r in rows]
        graded = [r["correct"] for r in rows if r["correct"] is not None]
        return {
            "n": len(rows),
            "avg_ret_pct": round(sum(rets) / len(rets), 2),
            "best_pct": round(max(rets), 2),
            "worst_pct": round(min(rets), 2),
            "graded_n": len(graded),
            "hit_rate_pct": round(100 * sum(graded) / len(graded), 1) if graded else None,
        }

    by_h: dict[str, dict] = {}
    for h in HORIZONS:
        h_rows = [o for o in outs if o["horizon"] == h]
        by_dir: dict[str, list] = {}
        for o in h_rows:
            d = snaps.get(o["snapshot_id"], {}).get("direction", "?")
            by_dir.setdefault(d, []).append(o)
        by_h[f"{h}d"] = {
            "overall": _agg(h_rows),
            "by_direction": {k: _agg(v) for k, v in by_dir.items()},
        }

    return {
        "symbol": symbol.upper() if symbol else "ALL",
        "total_snapshots": len(snaps),
        "evaluated_outcomes": len(outs),
        "by_horizon": by_h,
        "db_path": DB_PATH,
    }


def list_recent(symbol: str | None = None, limit: int = 20) -> list:
    init_db()
    with _conn() as conn:
        q = "SELECT s.*, " \
            "o7.fwd_ret_pct AS r7, o7.correct AS c7, " \
            "o30.fwd_ret_pct AS r30, o30.correct AS c30 " \
            "FROM snapshots s " \
            "LEFT JOIN outcomes o7 ON o7.snapshot_id=s.id AND o7.horizon=7 " \
            "LEFT JOIN outcomes o30 ON o30.snapshot_id=s.id AND o30.horizon=30 "
        params: list = []
        if symbol:
            q += "WHERE s.symbol = ? "
            params.append(symbol.upper())
        q += "ORDER BY s.as_of_date DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def to_markdown_report(rep: dict) -> str:
    lines = [
        f"# 贾维斯战绩追踪 — {rep['symbol']}",
        f"- 累计快照: {rep['total_snapshots']} 条 | 已评估前向结果: {rep['evaluated_outcomes']} 条",
        f"- 数据库: `{rep['db_path']}`",
    ]
    for hk, hv in rep["by_horizon"].items():
        ov = hv["overall"]
        if not ov.get("n"):
            continue
        lines.append("")
        lines.append(f"## {hk} 前瞻")
        hr = f"{ov['hit_rate_pct']}%" if ov.get("hit_rate_pct") is not None else "—"
        lines.append(
            f"- 全部: n={ov['n']} 平均收益 {ov['avg_ret_pct']}% | 命中率 {hr}"
            f"（最好 {ov['best_pct']}% / 最差 {ov['worst_pct']}%）"
        )
        for d, a in hv["by_direction"].items():
            if not a.get("n"):
                continue
            dhr = f"{a['hit_rate_pct']}%" if a.get("hit_rate_pct") is not None else "—（中性不计）"
            lines.append(f"  - {d}: n={a['n']} 平均 {a['avg_ret_pct']}% | 命中 {dhr}")
    if rep["total_snapshots"] == 0:
        lines.append("")
        lines.append("> 还没有快照。先 `python jarvis_journal.py record BTCUSDT` 落几条，过 7/30 天再 evaluate。")
    return "\n".join(lines)


def to_markdown_list(rows: list) -> str:
    if not rows:
        return "（暂无快照）"
    lines = ["# 最近决策快照", "", "| as_of | symbol | 价格 | 信心 | 方向 | 仓位% | 7d | 30d |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        def _fmt(ret, ok):
            if ret is None:
                return "待评估"
            mark = "✅" if ok == 1 else ("❌" if ok == 0 else "·")
            return f"{ret}% {mark}"
        lines.append(
            f"| {r['as_of_date']} | {r['symbol']} | {r['price']} | {r['conviction_score']} | "
            f"{r['direction']} | {r['position_pct']} | {_fmt(r.get('r7'), r.get('c7'))} | {_fmt(r.get('r30'), r.get('c30'))} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯决策快照落库 + 准确率追踪")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("record", help="落一条今日决策快照")
    p_rec.add_argument("symbol", nargs="?", default="BTCUSDT")
    p_rec.add_argument("--json", action="store_true")

    p_eval = sub.add_parser("evaluate", help="回填已到期快照的前向收益")
    p_eval.add_argument("--symbol", default=None)
    p_eval.add_argument("--json", action="store_true")

    p_rep = sub.add_parser("report", help="查看战绩统计")
    p_rep.add_argument("--symbol", default=None)
    p_rep.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="列出最近快照")
    p_list.add_argument("--symbol", default=None)
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--json", action="store_true")

    p_bf = sub.add_parser("backfill", help="用历史日线重建价格因子决策并落库")
    p_bf.add_argument("symbol", nargs="?", default="BTCUSDT")
    p_bf.add_argument("--step", type=int, default=7, help="每隔几天落一条")
    p_bf.add_argument("--json", action="store_true")

    args = ap.parse_args()

    if args.cmd == "record":
        out = record(args.symbol)
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else (f"✅ 已落库快照 #{out['snapshot_id']}: {out['symbol']} {out['as_of_date']} "
                    f"价 {out['price']} 信心 {out['conviction_score']} → {out['direction']} 仓位 {out['position_pct']}%"
                    if out.get("ok") else f"❌ 失败: {out.get('error')}"))
    elif args.cmd == "evaluate":
        out = evaluate(args.symbol)
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else f"✅ 评估完成: 快照 {out['snapshots']} 条, 回填 {out['outcomes_filled']} 条结果, {out['not_due']} 条未到期")
    elif args.cmd == "report":
        rep = report(args.symbol)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else to_markdown_report(rep))
    elif args.cmd == "list":
        rows = list_recent(args.symbol, args.limit)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else to_markdown_list(rows))
    elif args.cmd == "backfill":
        out = backfill(args.symbol, args.step)
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else (f"✅ 回填完成: {out['symbol']} 候选 {out['candidate_dates']} 个日期, 新增 {out['inserted']} 条快照"
                    if out.get("ok") else f"❌ 失败: {out.get('error')}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
