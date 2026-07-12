#!/usr/bin/env python3
"""贾维斯 JARVIS — 资金费率套利模拟盘（阿尔法策略第一步）。

策略本质：现货多头 + 永续空头等量对冲（delta 中性），不赌方向；
永续资金费率为正时，空头每 8 小时收一次费率——赚的是「多头拥挤税」。

模块职责：
  机会监控  fetch_opportunities()：全市场 premiumIndex 一次拉取，watchlist 币种
            按年化费率排序；7 日均费率并行补充；给出手续费回本天数与预警。
  模拟建仓  open_position()：本金对半劈成两腿（现货买 qty + 永续 1x 空 qty），
            记录两腿入场价/数量/时间；开仓手续费按 taker 口径预扣。
  收益结算  settle_positions()：懒结算——每次查询持仓/收益时补结从上次结算点
            到现在跨过的所有 8h 结算点（UTC 00/08/16），费率取交易所真实历史
            （/fapi/v1/fundingRate），UNIQUE(position_id, settle_ts) 防重复入账。
  风险提示  当前费率转负 / 7 日均转负 → 预警字段 + 建议平仓提示。

存储：与钱包同库（~/.vibe-trading/jarvis_journal.db）独立新表
funding_arb_positions / funding_arb_settlements，不触碰钱包核心表。

模拟盘口径（与实盘的已知偏差，均写进 disclaimer）：
  - 合约腿按 1x 全额保证金（无爆仓风险的最保守口径）
  - 手续费：现货 taker 0.1% + 合约 taker 0.05%，开平各一次
  - 未建模：借币成本、滑点、现货与合约价差波动的保证金占用

用法（CLI）：
  python jarvis_funding_arb.py opportunities
  python jarvis_funding_arb.py open --symbol BTCUSDT --capital 1000
  python jarvis_funding_arb.py positions
  python jarvis_funding_arb.py close --id 1
  python jarvis_funding_arb.py pnl
"""

from __future__ import annotations

import argparse
import json
import math
import threading
import time

import jarvis_crypto_data as jcd
import jarvis_journal as jj

DISCLAIMER = ("模拟盘：费率按交易所真实历史结算，但未建模滑点/借币成本/保证金波动；"
              "收益为统计参考，非投资建议。")

# 机会监控币种池（Binance USDT 永续 + 现货均有的主流币）
WATCHLIST = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
             "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT")

SPOT_TAKER_FEE = 0.001    # 现货 taker 0.1%
PERP_TAKER_FEE = 0.0005   # U 本位合约 taker 0.05%
ROUNDTRIP_FEE_PCT = 2 * (SPOT_TAKER_FEE + PERP_TAKER_FEE)  # 两腿开+平 ≈ 0.3%

SETTLE_INTERVAL = 8 * 3600          # 8h 结算周期（UTC 00/08/16）
PERIODS_PER_YEAR = 3 * 365          # 年化倍数
MIN_CAPITAL = 10.0
MAX_CAPITAL = 10_000_000.0

_OPP_LOCK = threading.Lock()
_OPP_CACHE: dict = {"ts": 0.0, "data": None}
OPP_TTL = 120


# ─────────────────────────── 建表 ───────────────────────────


def init_db() -> None:
    jj.init_db()
    with jj._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS funding_arb_positions (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol               TEXT NOT NULL,
                qty                  REAL NOT NULL,
                capital_usdt         REAL NOT NULL,
                spot_entry           REAL NOT NULL,
                perp_entry           REAL NOT NULL,
                opened_ts            REAL NOT NULL,
                opened_at            TEXT,
                status               TEXT NOT NULL DEFAULT 'open',
                last_settled_ts      REAL,
                funding_accrued_usdt REAL NOT NULL DEFAULT 0,
                settle_count         INTEGER NOT NULL DEFAULT 0,
                fees_usdt            REAL NOT NULL DEFAULT 0,
                closed_ts            REAL,
                closed_at            TEXT,
                spot_exit            REAL,
                perp_exit            REAL,
                basis_pnl_usdt       REAL,
                total_pnl_usdt       REAL,
                note                 TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS funding_arb_settlements (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id  INTEGER NOT NULL,
                settle_ts    REAL NOT NULL,
                rate         REAL NOT NULL,
                amount_usdt  REAL NOT NULL,
                mark_price   REAL,
                created_ts   REAL,
                UNIQUE (position_id, settle_ts)
            )
            """
        )


def _now_str(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or time.time()))


def _norm_symbol(symbol: str) -> str:
    sym = (symbol or "").upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    return sym


# ─────────────────────────── 行情 ───────────────────────────


def _spot_price(symbol: str) -> float | None:
    data = jcd._get(jcd.SPOT_API + "/api/v3/ticker/price", {"symbol": symbol})
    try:
        return float(data["price"])
    except (TypeError, KeyError, ValueError):
        return None


def _premium_index_all() -> list[dict]:
    """全市场 premiumIndex（单次请求含所有 symbol 的费率与标记价）。"""
    rows = jcd._get(jcd.FAPI + "/fapi/v1/premiumIndex")
    return rows if isinstance(rows, list) else []


def _premium_index_one(symbol: str) -> dict | None:
    row = jcd._get(jcd.FAPI + "/fapi/v1/premiumIndex", {"symbol": symbol})
    return row if isinstance(row, dict) and "lastFundingRate" in row else None


def _funding_history(symbol: str, start_ms: int | None = None,
                     end_ms: int | None = None, limit: int = 100) -> list[dict]:
    """交易所真实费率历史：[{fundingTime, fundingRate, markPrice?}, ...] 升序。"""
    params: dict = {"symbol": symbol, "limit": max(1, min(int(limit), 1000))}
    if start_ms:
        params["startTime"] = int(start_ms)
    if end_ms:
        params["endTime"] = int(end_ms)
    rows = jcd._get(jcd.FAPI + "/fapi/v1/fundingRate", params)
    return rows if isinstance(rows, list) else []


# ─────────────────────────── 机会监控 ───────────────────────────


def _apr(rate_8h: float) -> float:
    """单期费率 → 年化 %（复利不计，与项目 annualized_pct 同口径）。"""
    return round(rate_8h * PERIODS_PER_YEAR * 100, 2)


def fetch_opportunities(force: bool = False) -> dict:
    """watchlist 套利机会列表，按当前费率年化降序。TTL 缓存 120s。

    单币字段：symbol / mark_price / funding_rate（当期）/ apr_now /
    apr_7d（7 日均年化，取数失败为 None）/ next_funding_ts /
    break_even_days（往返手续费 ÷ 单期费率 ÷ 3，费率≤0 为 None）/ warning。
    """
    now = time.time()
    with _OPP_LOCK:
        hit = _OPP_CACHE["data"]
        if not force and hit is not None and now - _OPP_CACHE["ts"] < OPP_TTL:
            return hit

    rows = _premium_index_all()
    want = {s: None for s in WATCHLIST}
    for row in rows:
        sym = row.get("symbol")
        if sym in want and want[sym] is None:
            want[sym] = row

    # 7 日均费率：并行补充（每币一次历史请求，失败置 None 不拖垮列表）
    hist_avg: dict[str, float | None] = {s: None for s in WATCHLIST}

    def _one(sym: str) -> None:
        try:
            hist = _funding_history(sym, limit=21)
            rates = [float(h["fundingRate"]) for h in hist if "fundingRate" in h]
            if rates:
                hist_avg[sym] = sum(rates) / len(rates)
        except Exception:  # noqa: BLE001 — 单币历史失败仅缺 apr_7d
            pass

    threads = [threading.Thread(target=_one, args=(s,), daemon=True)
               for s, row in want.items() if row is not None]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    opps = []
    for sym in WATCHLIST:
        row = want[sym]
        if row is None:
            continue
        try:
            rate = float(row.get("lastFundingRate") or 0)
            mark = float(row.get("markPrice") or 0)
            next_ts = int(row.get("nextFundingTime") or 0) // 1000
        except (TypeError, ValueError):
            continue
        avg7 = hist_avg[sym]
        income_per_period = rate  # 空头收正费率
        be_days = None
        if income_per_period > 0:
            be_days = round(ROUNDTRIP_FEE_PCT / (income_per_period * 3), 1)
        warning = None
        if rate < 0:
            warning = "当期费率为负：空头腿正在倒贴，不适合建仓"
        elif avg7 is not None and avg7 < 0:
            warning = "7日均费率为负：费率收益不稳定，谨慎建仓"
        opps.append({
            "symbol": sym,
            "mark_price": mark,
            "funding_rate": rate,
            "funding_rate_pct": round(rate * 100, 5),
            "apr_now": _apr(rate),
            "apr_7d": _apr(avg7) if avg7 is not None else None,
            "next_funding_ts": next_ts,
            "break_even_days": be_days,
            "fee_roundtrip_pct": round(ROUNDTRIP_FEE_PCT * 100, 3),
            "warning": warning,
        })
    opps.sort(key=lambda o: o["apr_now"], reverse=True)

    out = {
        "ok": bool(opps),
        "generated_ts": int(now),
        "opportunities": opps,
        "basis": ("年化=当期8h费率×3×365；回本天数=往返手续费0.3%÷日费率收益；"
                  "费率每8h结算一次（UTC 00/08/16）。"),
        "disclaimer": DISCLAIMER,
    }
    if not opps:
        out["error"] = "premiumIndex 拉取失败或 watchlist 无数据"
    with _OPP_LOCK:
        # 失败结果不覆盖上次成功缓存（保留旧值可退化展示）
        if opps or _OPP_CACHE["data"] is None:
            _OPP_CACHE.update(ts=now, data=out)
    return out


# ─────────────────────────── 结算 ───────────────────────────


def _settle_points_between(t0: float, t1: float) -> list[float]:
    """(t0, t1] 之间的 8h 结算点（UTC 00/08/16 整点）。"""
    first = math.floor(t0 / SETTLE_INTERVAL + 1) * SETTLE_INTERVAL
    pts = []
    t = first
    while t <= t1:
        pts.append(float(t))
        t += SETTLE_INTERVAL
    return pts


def settle_positions(position_id: int | None = None) -> dict:
    """懒结算：补结所有 open 持仓（或指定持仓）欠结的费率期。

    费率取交易所真实历史；历史里找不到对应结算点（拉取失败/太新）则该期
    跳过、下次再补，绝不硬造费率。返回 {settled: 新入账笔数, details}。
    """
    init_db()
    now = time.time()
    with jj._conn() as conn:
        q = "SELECT * FROM funding_arb_positions WHERE status = 'open'"
        args: tuple = ()
        if position_id is not None:
            q += " AND id = ?"
            args = (int(position_id),)
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]

    settled_total = 0
    details = []
    for pos in rows:
        anchor = float(pos.get("last_settled_ts") or pos["opened_ts"])
        points = _settle_points_between(anchor, now)
        if not points:
            continue
        # 一次拉齐该持仓欠结区间的费率历史（fundingTime 即结算点毫秒）
        hist = _funding_history(pos["symbol"],
                                start_ms=int((points[0] - 3600) * 1000),
                                end_ms=int((points[-1] + 3600) * 1000))
        by_ts: dict[int, dict] = {}
        for h in hist:
            try:
                by_ts[int(round(int(h["fundingTime"]) / 1000 / SETTLE_INTERVAL)
                          * SETTLE_INTERVAL)] = h
            except (KeyError, TypeError, ValueError):
                continue
        pos_settled = 0
        accrued = 0.0
        last_ok_ts = None
        for pt in points:
            h = by_ts.get(int(pt))
            if h is None:
                continue  # 该期费率还没出（或拉取失败），下次再补
            try:
                rate = float(h["fundingRate"])
                mark = float(h.get("markPrice") or 0) or float(pos["perp_entry"])
            except (KeyError, TypeError, ValueError):
                continue
            amount = pos["qty"] * mark * rate  # 空头：正费率=收入，负费率=支出
            try:
                with jj._conn() as conn:
                    conn.execute(
                        "INSERT INTO funding_arb_settlements "
                        "(position_id, settle_ts, rate, amount_usdt, mark_price, created_ts) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (pos["id"], pt, rate, round(amount, 8), mark, now),
                    )
            except Exception:  # noqa: BLE001 — UNIQUE 冲突 = 已结过该期，跳过
                continue
            pos_settled += 1
            accrued += amount
            last_ok_ts = pt
        if pos_settled:
            with jj._conn() as conn:
                conn.execute(
                    "UPDATE funding_arb_positions SET "
                    "funding_accrued_usdt = funding_accrued_usdt + ?, "
                    "settle_count = settle_count + ?, last_settled_ts = ? "
                    "WHERE id = ?",
                    (round(accrued, 8), pos_settled, last_ok_ts, pos["id"]),
                )
            settled_total += pos_settled
            details.append({"position_id": pos["id"], "symbol": pos["symbol"],
                            "periods": pos_settled, "amount_usdt": round(accrued, 4)})
    return {"ok": True, "settled": settled_total, "details": details}


# ─────────────────────────── 建仓 / 平仓 ───────────────────────────


def open_position(symbol: str, capital_usdt: float, note: str = "") -> dict:
    """模拟同时开两腿：现货买 qty + 永续 1x 空 qty（等量，delta 中性）。

    本金对半劈：qty = (capital/2) / spot_price；合约腿 1x 全额保证金 qty×perp。
    开仓手续费（taker 两腿）先记入 fees_usdt。
    """
    init_db()
    sym = _norm_symbol(symbol)
    try:
        capital = float(capital_usdt)
    except (TypeError, ValueError):
        return {"ok": False, "error": "capital 非法"}
    if not (MIN_CAPITAL <= capital <= MAX_CAPITAL):
        return {"ok": False, "error": f"本金须在 {MIN_CAPITAL} ~ {MAX_CAPITAL} USDT 之间"}

    spot = _spot_price(sym)
    prem = _premium_index_one(sym)
    if not spot or not prem:
        return {"ok": False, "error": f"{sym} 行情拉取失败（现货或永续不可用）"}
    try:
        perp = float(prem.get("markPrice") or 0)
        rate = float(prem.get("lastFundingRate") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "premiumIndex 数据异常"}
    if perp <= 0:
        return {"ok": False, "error": "标记价异常（≤0）"}

    qty = (capital / 2.0) / spot
    open_fee = qty * spot * SPOT_TAKER_FEE + qty * perp * PERP_TAKER_FEE
    now = time.time()
    with jj._conn() as conn:
        cur = conn.execute(
            "INSERT INTO funding_arb_positions "
            "(symbol, qty, capital_usdt, spot_entry, perp_entry, opened_ts, opened_at, "
            " status, fees_usdt, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (sym, round(qty, 10), capital, spot, perp, now, _now_str(now),
             round(open_fee, 8), note[:200]),
        )
        pid = cur.lastrowid
    warning = None
    if rate < 0:
        warning = "注意：当期费率为负，空头腿将倒贴（建仓即亏费率）"
    return {"ok": True, "position_id": pid, "symbol": sym,
            "qty": round(qty, 10), "capital_usdt": capital,
            "spot_entry": spot, "perp_entry": perp,
            "open_fee_usdt": round(open_fee, 4),
            "current_funding_rate": rate, "warning": warning,
            "disclaimer": DISCLAIMER}


def close_position(position_id: int) -> dict:
    """平仓：先补结欠着的费率期，再按当前两腿价格结算基差损益与总收益。"""
    init_db()
    with jj._conn() as conn:
        row = conn.execute("SELECT * FROM funding_arb_positions WHERE id = ?",
                           (int(position_id),)).fetchone()
    if not row:
        return {"ok": False, "error": f"持仓 {position_id} 不存在"}
    pos = dict(row)
    if pos["status"] != "open":
        return {"ok": False, "error": f"持仓 {position_id} 已平仓"}

    settle_positions(position_id=int(position_id))  # 先把欠结的费率期补齐
    with jj._conn() as conn:
        pos = dict(conn.execute("SELECT * FROM funding_arb_positions WHERE id = ?",
                                (int(position_id),)).fetchone())

    sym = pos["symbol"]
    spot_exit = _spot_price(sym)
    prem = _premium_index_one(sym)
    perp_exit = None
    if prem:
        try:
            perp_exit = float(prem.get("markPrice") or 0) or None
        except (TypeError, ValueError):
            perp_exit = None
    if not spot_exit or not perp_exit:
        return {"ok": False, "error": f"{sym} 平仓行情拉取失败，请稍后重试"}

    qty = float(pos["qty"])
    basis_pnl = qty * (spot_exit - pos["spot_entry"]) + qty * (pos["perp_entry"] - perp_exit)
    close_fee = qty * spot_exit * SPOT_TAKER_FEE + qty * perp_exit * PERP_TAKER_FEE
    fees_total = float(pos["fees_usdt"]) + close_fee
    total = float(pos["funding_accrued_usdt"]) + basis_pnl - fees_total
    now = time.time()
    with jj._conn() as conn:
        conn.execute(
            "UPDATE funding_arb_positions SET status='closed', closed_ts=?, closed_at=?, "
            "spot_exit=?, perp_exit=?, basis_pnl_usdt=?, fees_usdt=?, total_pnl_usdt=? "
            "WHERE id = ?",
            (now, _now_str(now), spot_exit, perp_exit, round(basis_pnl, 8),
             round(fees_total, 8), round(total, 8), int(position_id)),
        )
    held_days = max((now - pos["opened_ts"]) / 86400, 1e-9)
    return {"ok": True, "position_id": int(position_id), "symbol": sym,
            "funding_accrued_usdt": round(pos["funding_accrued_usdt"], 4),
            "basis_pnl_usdt": round(basis_pnl, 4),
            "fees_usdt": round(fees_total, 4),
            "total_pnl_usdt": round(total, 4),
            "held_days": round(held_days, 2),
            "realized_apr_pct": round(total / pos["capital_usdt"] / held_days * 365 * 100, 2),
            "disclaimer": DISCLAIMER}


# ─────────────────────────── 查询 ───────────────────────────


def _enrich_open(pos: dict, rate_map: dict[str, dict], now: float) -> dict:
    """open 持仓补充实时字段：当前费率/净敞口/累计收益年化/费率预警。"""
    sym = pos["symbol"]
    held_days = max((now - pos["opened_ts"]) / 86400, 1e-9)
    accrued = float(pos["funding_accrued_usdt"])
    capital = float(pos["capital_usdt"]) or 1e-9
    out = {**pos,
           "held_days": round(held_days, 2),
           "funding_apr_pct": round(accrued / capital / held_days * 365 * 100, 2),
           "net_exposure_pct": 0.0, "current_funding_rate": None,
           "next_funding_ts": None, "warning": None, "mark_price": None}
    prem = rate_map.get(sym)
    if prem:
        try:
            rate = float(prem.get("lastFundingRate") or 0)
            mark = float(prem.get("markPrice") or 0)
            out["current_funding_rate"] = rate
            out["mark_price"] = mark
            out["next_funding_ts"] = int(prem.get("nextFundingTime") or 0) // 1000
            if mark > 0:
                spot_leg = pos["qty"] * mark          # 现货腿现值（近似用 mark）
                perp_leg = -pos["qty"] * mark         # 空头腿名义
                out["net_exposure_pct"] = round(
                    abs(spot_leg + perp_leg) / capital * 100, 4)
            if rate < 0:
                out["warning"] = "费率已转负：空头腿正在倒贴，建议考虑平仓"
        except (TypeError, ValueError):
            pass
    return out


def list_positions(status: str = "all") -> dict:
    """持仓列表（自动先补结欠账费率期）。status: all|open|closed。"""
    init_db()
    settle_positions()
    now = time.time()
    q = "SELECT * FROM funding_arb_positions"
    args: tuple = ()
    if status in ("open", "closed"):
        q += " WHERE status = ?"
        args = (status,)
    q += " ORDER BY opened_ts DESC"
    with jj._conn() as conn:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]

    open_syms = {r["symbol"] for r in rows if r["status"] == "open"}
    rate_map: dict[str, dict] = {}
    if open_syms:
        for row in _premium_index_all():
            if row.get("symbol") in open_syms:
                rate_map[row["symbol"]] = row
    out_rows = [(_enrich_open(r, rate_map, now) if r["status"] == "open" else r)
                for r in rows]
    return {"ok": True, "positions": out_rows, "disclaimer": DISCLAIMER}


def get_pnl() -> dict:
    """收益总览：open/closed 分组汇总 + 全局累计费率收益与年化。"""
    init_db()
    settle_positions()
    now = time.time()
    with jj._conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM funding_arb_positions").fetchall()]
    open_rows = [r for r in rows if r["status"] == "open"]
    closed_rows = [r for r in rows if r["status"] == "closed"]

    open_capital = sum(r["capital_usdt"] for r in open_rows)
    open_accrued = sum(r["funding_accrued_usdt"] for r in open_rows)
    # open 组合年化：按资金×持有时间加权（各持仓时长不同，不能简单平均）
    weighted_days = sum(r["capital_usdt"] * max((now - r["opened_ts"]) / 86400, 1e-9)
                        for r in open_rows)
    open_apr = (open_accrued / weighted_days * 365 * 100) if weighted_days > 0 else 0.0

    closed_pnl = sum((r["total_pnl_usdt"] or 0) for r in closed_rows)
    closed_funding = sum(r["funding_accrued_usdt"] for r in closed_rows)
    return {
        "ok": True,
        "open": {"count": len(open_rows), "capital_usdt": round(open_capital, 2),
                 "funding_accrued_usdt": round(open_accrued, 4),
                 "funding_apr_pct": round(open_apr, 2)},
        "closed": {"count": len(closed_rows),
                   "funding_accrued_usdt": round(closed_funding, 4),
                   "total_pnl_usdt": round(closed_pnl, 4)},
        "all_time_funding_usdt": round(open_accrued + closed_funding, 4),
        "generated_ts": int(now),
        "basis": ("open 年化=累计费率收益÷(资金×持有天数)加权×365；"
                  "closed 总收益=费率累计+两腿基差损益-开平手续费。"),
        "disclaimer": DISCLAIMER,
    }


# ─────────────────────────── CLI ───────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description="资金费率套利模拟盘")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("opportunities", help="套利机会列表")
    p_open = sub.add_parser("open", help="模拟建仓")
    p_open.add_argument("--symbol", required=True)
    p_open.add_argument("--capital", type=float, required=True)
    p_pos = sub.add_parser("positions", help="持仓列表")
    p_pos.add_argument("--status", default="all", choices=["all", "open", "closed"])
    p_close = sub.add_parser("close", help="平仓")
    p_close.add_argument("--id", type=int, required=True)
    sub.add_parser("pnl", help="收益总览")
    sub.add_parser("settle", help="手动触发费率结算")
    args = ap.parse_args()

    if args.cmd == "opportunities":
        out = fetch_opportunities()
    elif args.cmd == "open":
        out = open_position(args.symbol, args.capital)
    elif args.cmd == "positions":
        out = list_positions(args.status)
    elif args.cmd == "close":
        out = close_position(args.id)
    elif args.cmd == "pnl":
        out = get_pnl()
    elif args.cmd == "settle":
        out = settle_positions()
    else:
        ap.print_help()
        return
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
