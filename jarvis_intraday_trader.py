#!/usr/bin/env python3
"""贾维斯 JARVIS — 4h 自动模拟下单引擎（预测达标才开仓，全程 paper 不碰真钱）。

每 4 小时一轮（由 jarvis_daemon --intraday 驱动，也可手动 cycle）：
  1) 盯平仓：对未平仓位按 止损 → 止盈 → 时间止损 → 信号反转 顺序判定平仓
  2) 落预测：对 watchlist 每币调 jarvis_intraday_predict.predict_latest 落库（幂等）
  3) 回填：用真实收盘回填上一根 bar 预测的 outcome_ret / hit（诚实前向战绩）
  4) 开仓：tradeable ∧ prob≥min_prob ∧ 未熔断 ∧ 未冷却 ∧ 持仓数未满 → 模拟开仓
     （方向支持 涨→做多 / 跌→做空；仓位按「单笔风险% ÷ 止损距离」反推）

安全设计：
  - **可交易门禁在预测层写死**（OOS 命中率 + 置换检验），引擎无法绕过。
  - 连亏熔断：最近连续 N 笔亏损 → 写 halt 文件停开仓 + 告警；人工 resume 解除。
  - 永不抛出：任何数据/网络异常只记日志，绝不拖垮 daemon 心跳。

存储：~/.vibe-trading/jarvis_journal.db 的 intraday_positions / intraday_predictions。

用法：
  python jarvis_intraday_trader.py cycle --symbols BTCUSDT,ETHUSDT
  python jarvis_intraday_trader.py status
  python jarvis_intraday_trader.py report
  python jarvis_intraday_trader.py close BTCUSDT --reason manual
  python jarvis_intraday_trader.py resume        # 解除熔断
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Callable, Optional

import jarvis_config as jc

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")
LOG_PATH = os.path.join(DB_DIR, "jarvis_intraday.log")
HALT_PATH = os.path.join(DB_DIR, "intraday_halt.json")
RESUME_PATH = os.path.join(DB_DIR, "intraday_resume.json")
BAR_MS = 4 * 3600 * 1000
LONG, SHORT = "long", "short"


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — 日志失败不中断
        pass


def _conn(db_path: Optional[str] = None):
    """显式传 db_path（测试用临时库）时固定用 SQLite；否则按 jarvis_db 选后端（pg/SQLite）。"""
    os.makedirs(DB_DIR, exist_ok=True)
    if db_path is not None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    import jarvis_db as jdb
    return jdb.connect(DB_PATH)


def ensure_db(db_path: Optional[str] = None) -> None:
    with _conn(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS intraday_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, side TEXT NOT NULL,
                opened_ts INTEGER NOT NULL,
                entry REAL, stop REAL, take REAL, qty REAL, notional_usdt REAL,
                prob REAL,
                closed_ts INTEGER, exit_price REAL, exit_reason TEXT,
                pnl_usdt REAL,
                UNIQUE(symbol, opened_ts))""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS intraday_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, bar_ts INTEGER NOT NULL,
                direction TEXT, prob REAL, tradeable INTEGER,
                entry REAL, stop REAL, take REAL, atr_pct REAL,
                oos_hit_rate REAL, p_value REAL, reason TEXT,
                outcome_ret REAL, hit INTEGER,
                UNIQUE(symbol, bar_ts))""")
        try:  # 归因人话列（老库迁移；已存在则忽略）
            conn.execute("ALTER TABLE intraday_predictions ADD COLUMN why_text TEXT")
        except sqlite3.OperationalError:
            pass


# ── 行情 / 预测（可注入，供离线测试）────────────────────────────────────────

def latest_price(symbol: str) -> Optional[float]:
    """Binance 现货最新价；失败返回 None（调用方跳过该币，绝不抛出）。"""
    try:
        import jarvis_crypto_data as jcd
        d = jcd._get(jcd.SPOT_API + "/api/v3/ticker/price", {"symbol": symbol})
        if isinstance(d, dict) and d.get("price"):
            return float(d["price"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _default_predict(symbol: str) -> dict:
    import jarvis_intraday_predict as jip
    cfg = jc.get_all()
    return jip.predict_latest(
        symbol,
        stop_atr_mult=float(cfg["intraday_stop_atr_mult"]),
        take_atr_mult=float(cfg["intraday_take_atr_mult"]),
    )


def _notify(text: str) -> None:
    try:
        import jarvis_notify as jn
        jn.notify(text)
    except Exception:  # noqa: BLE001 — 通知失败不影响交易主流程
        pass


# ── 熔断 ────────────────────────────────────────────────────────────────────

def is_halted() -> Optional[dict]:
    try:
        if os.path.exists(HALT_PATH):
            with open(HALT_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception:  # noqa: BLE001 — halt 文件损坏视为熔断中（保守）
        return {"reason": "halt 文件损坏（保守视为熔断）"}
    return None


def _trip_halt(reason: str) -> None:
    try:
        with open(HALT_PATH, "w", encoding="utf-8") as f:
            json.dump({"reason": reason,
                       "tripped_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f,
                      ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    _log(f"🛑 盘中熔断触发：{reason}")
    _notify(f"🛑 贾维斯 4h 引擎熔断：{reason}（已停开仓，`intraday resume` 解除）")


def resume(db_path: Optional[str] = None) -> bool:
    """人工解除熔断：记录「当前最大平仓时间」为分界，此前的连亏不再计入，
    避免解除后下一轮立刻复触发。"""
    try:
        ensure_db(db_path)
        with _conn(db_path) as conn:
            row = conn.execute("SELECT MAX(closed_ts) AS t FROM intraday_positions "
                               "WHERE closed_ts IS NOT NULL").fetchone()
        with open(RESUME_PATH, "w", encoding="utf-8") as f:
            json.dump({"after_closed_ts": int(row["t"] or 0),
                       "resumed_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:  # noqa: BLE001
        pass
    if os.path.exists(HALT_PATH):
        os.remove(HALT_PATH)
        _log("✅ 盘中熔断已人工解除（连亏计数已重置）")
        return True
    return False


def _resume_watermark() -> int:
    try:
        if os.path.exists(RESUME_PATH):
            with open(RESUME_PATH, encoding="utf-8") as f:
                return int(json.load(f).get("after_closed_ts", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _consecutive_losses(conn: sqlite3.Connection) -> int:
    """最近连续亏损笔数；只统计上次人工 resume 分界之后平掉的仓。"""
    rows = conn.execute(
        "SELECT pnl_usdt FROM intraday_positions WHERE closed_ts IS NOT NULL "
        "AND closed_ts > ? ORDER BY closed_ts DESC LIMIT 20",
        (_resume_watermark(),)).fetchall()
    n = 0
    for r in rows:
        if r["pnl_usdt"] is not None and r["pnl_usdt"] < 0:
            n += 1
        else:
            break
    return n


# ── 平仓 / 开仓 ─────────────────────────────────────────────────────────────

def _close(conn: sqlite3.Connection, pos: sqlite3.Row, price: float,
           reason: str, now_ms: int) -> dict:
    sign = 1.0 if pos["side"] == LONG else -1.0
    pnl = round((price - pos["entry"]) * pos["qty"] * sign, 4)
    conn.execute(
        "UPDATE intraday_positions SET closed_ts=?, exit_price=?, exit_reason=?, "
        "pnl_usdt=? WHERE id=?", (now_ms, price, reason, pnl, pos["id"]))
    emo = "🟢" if pnl >= 0 else "🔴"
    _log(f"{emo} 平仓 {pos['symbol']} {pos['side']} @{price} 原因={reason} pnl={pnl}U")
    _notify(f"{emo} 4h 模拟平仓 {pos['symbol']} {pos['side']} @{price}"
            f"（{reason}）盈亏 {pnl}U")
    return {"symbol": pos["symbol"], "reason": reason, "pnl_usdt": pnl}


def _maybe_close(conn: sqlite3.Connection, pos: sqlite3.Row, price: float,
                 pred: Optional[dict], cfg: dict, now_ms: int) -> Optional[dict]:
    """按 止损→止盈→时间→信号反转 顺序判定；不满足返回 None。"""
    long_side = pos["side"] == LONG
    if (long_side and price <= pos["stop"]) or (not long_side and price >= pos["stop"]):
        return _close(conn, pos, price, "stop", now_ms)
    if (long_side and price >= pos["take"]) or (not long_side and price <= pos["take"]):
        return _close(conn, pos, price, "take", now_ms)
    max_age = int(cfg["intraday_time_stop_bars"]) * BAR_MS
    if now_ms - pos["opened_ts"] >= max_age:
        return _close(conn, pos, price, "time", now_ms)
    if pred and pred.get("tradeable") and pred.get("prob") is not None:
        opposite = "跌" if long_side else "涨"
        if pred.get("direction") == opposite and pred["prob"] >= float(cfg["intraday_min_prob"]):
            return _close(conn, pos, price, "signal", now_ms)
    return None


def _in_cooldown(conn: sqlite3.Connection, symbol: str, cfg: dict, now_ms: int) -> bool:
    bars = int(cfg["intraday_cooldown_bars"])
    if bars <= 0:
        return False
    row = conn.execute(
        "SELECT MAX(closed_ts) AS t FROM intraday_positions WHERE symbol=? "
        "AND closed_ts IS NOT NULL", (symbol,)).fetchone()
    return bool(row and row["t"] and now_ms - row["t"] < bars * BAR_MS)


def _open(conn: sqlite3.Connection, pred: dict, cfg: dict, now_ms: int) -> Optional[dict]:
    sym = pred["symbol"]
    entry, stop, take = pred.get("entry"), pred.get("stop"), pred.get("take")
    if not entry or not stop or not take:
        return None
    stop_dist = abs(entry - stop) / entry
    if stop_dist <= 0:
        return None
    # [Sprint1 P1-3] 统一开仓门禁：组合熔断 + 冷静期（平仓/回填不经此处不受限）。
    # 拦截只记日志返回 None，绝不抛出——引擎循环不能因门禁中断。
    try:
        import jarvis_circuit_breaker as _cb
        _g = _cb.guard_new_order()
        if not _g.get("allow"):
            _log(f"⛔ {sym} 开仓被统一门禁拦截：{_g.get('reason')}")
            return None
    except Exception as exc:  # noqa: BLE001 — 门禁自身异常放行（paper-only，与 guard 内语义一致）
        _log(f"⚠️ 统一门禁检查异常（放行继续）: {exc!r}")
    equity = float(cfg["account_equity_usdt"])
    risk_usdt = equity * float(cfg["intraday_risk_pct_per_trade"]) / 100.0
    notional = min(risk_usdt / stop_dist, equity * float(cfg["max_position_pct"]) / 100.0)
    qty = notional / entry
    side = LONG if pred["direction"] == "涨" else SHORT
    cur = conn.execute(
        "INSERT INTO intraday_positions (symbol, side, opened_ts, entry, stop, "
        "take, qty, notional_usdt, prob) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, opened_ts) DO NOTHING",
        (sym, side, now_ms, entry, stop, take, qty, round(notional, 2),
         pred.get("prob")))
    if not cur.rowcount:
        return None  # 同一时刻重复开仓（幂等兜底，两种后端通用）
    _log(f"📈 开仓 {sym} {side} @{entry} 止损{stop} 止盈{take} "
         f"名义{round(notional, 2)}U prob={pred.get('prob')}")
    _notify(f"📈 4h 模拟开仓 {sym} {'做多' if side == LONG else '做空'} @{entry}"
            f"（prob {pred.get('prob')}，名义 {round(notional, 2)}U）")
    return {"symbol": sym, "side": side, "entry": entry, "notional_usdt": round(notional, 2)}


# ── 预测落库 / 回填 ─────────────────────────────────────────────────────────

def _record_prediction(conn: sqlite3.Connection, pred: dict) -> None:
    if not pred.get("as_of_bar_ts"):
        return
    conn.execute(
        "INSERT OR REPLACE INTO intraday_predictions "
        "(symbol, bar_ts, direction, prob, tradeable, entry, stop, take, atr_pct, "
        "oos_hit_rate, p_value, reason, why_text, outcome_ret, hit) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, "
        " (SELECT outcome_ret FROM intraday_predictions WHERE symbol=? AND bar_ts=?),"
        " (SELECT hit FROM intraday_predictions WHERE symbol=? AND bar_ts=?))",
        (pred["symbol"], pred["as_of_bar_ts"], pred.get("direction"),
         pred.get("prob"), 1 if pred.get("tradeable") else 0,
         pred.get("entry"), pred.get("stop"), pred.get("take"),
         pred.get("atr_pct"), pred.get("oos_hit_rate"), pred.get("p_value"),
         pred.get("reason"), pred.get("why_text"),
         pred["symbol"], pred["as_of_bar_ts"],
         pred["symbol"], pred["as_of_bar_ts"]))


def _backfill(conn: sqlite3.Connection, symbol: str,
              bars: Optional[list] = None) -> int:
    """用真实 4h 收盘回填到期预测的 outcome_ret / hit。返回回填条数。"""
    rows = conn.execute(
        "SELECT id, bar_ts, direction, entry FROM intraday_predictions "
        "WHERE symbol=? AND outcome_ret IS NULL ORDER BY bar_ts DESC LIMIT 50",
        (symbol,)).fetchall()
    if not rows:
        return 0
    if bars is None:
        try:
            import jarvis_crypto_data as jcd
            bars = jcd.fetch_kline(symbol, "4h", 200)
        except Exception:  # noqa: BLE001
            return 0
    # 只用已收盘 bar 回填（进行中 bar 的 close 是实时价，非最终收盘，会污染战绩）
    now_ms = int(time.time() * 1000)
    close_by_ts = {b["ts"]: b["close"] for b in bars
                   if b["ts"] + BAR_MS <= now_ms}
    filled = 0
    for r in rows:
        nxt = close_by_ts.get(r["bar_ts"] + BAR_MS)
        base = r["entry"] or close_by_ts.get(r["bar_ts"])
        if nxt is None or not base:
            continue
        ret = nxt / base - 1.0
        hit = None
        if r["direction"] == "涨":
            hit = 1 if ret > 0 else 0
        elif r["direction"] == "跌":
            hit = 1 if ret < 0 else 0
        conn.execute("UPDATE intraday_predictions SET outcome_ret=?, hit=? WHERE id=?",
                     (round(ret, 6), hit, r["id"]))
        filled += 1
    return filled


# ── 主循环 ──────────────────────────────────────────────────────────────────

def cycle(symbols: Optional[list[str]] = None, *,
          predict_fn: Optional[Callable[[str], dict]] = None,
          price_fn: Optional[Callable[[str], Optional[float]]] = None,
          now_ms: Optional[int] = None,
          db_path: Optional[str] = None) -> dict:
    """跑一轮 4h 周期。永不抛出。predict_fn/price_fn/now_ms/db_path 供测试注入。"""
    cfg = jc.get_all()
    out: dict = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "closed": [], "opened": [], "predictions": {}, "backfilled": 0,
                 "halted": False}
    if not cfg.get("intraday_enabled", True):
        out["skipped"] = "intraday_enabled=False"
        return out
    symbols = [s.strip().upper() for s in (symbols or cfg["watchlist"]) if s.strip()]
    predict = predict_fn or _default_predict
    get_price = price_fn or latest_price
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    ensure_db(db_path)
    try:
        with _conn(db_path) as conn:
            # 1) 预测（开仓与信号反转平仓共用，一轮只算一次）
            preds: dict[str, dict] = {}
            for sym in symbols:
                try:
                    p = predict(sym)
                except Exception as e:  # noqa: BLE001
                    p = {"symbol": sym, "tradeable": False, "reason": f"预测异常 {e!r}"[:200]}
                preds[sym] = p
                _record_prediction(conn, p)
                out["predictions"][sym] = {
                    "direction": p.get("direction"), "prob": p.get("prob"),
                    "tradeable": bool(p.get("tradeable")),
                    **({"reason": p.get("reason")} if p.get("reason") else {})}

            # 2) 盯平仓（含 watchlist 之外的遗留持仓）
            open_rows = conn.execute(
                "SELECT * FROM intraday_positions WHERE closed_ts IS NULL").fetchall()
            for pos in open_rows:
                price = get_price(pos["symbol"])
                if price is None:
                    _log(f"⚠️ {pos['symbol']} 取价失败，本轮跳过盯仓")
                    continue
                r = _maybe_close(conn, pos, price, preds.get(pos["symbol"]), cfg, now)
                if r:
                    out["closed"].append(r)

            # 3) 回填前向战绩
            for sym in symbols:
                out["backfilled"] += _backfill(conn, sym)

            # 4) 熔断检查（平仓后最新口径）
            halt = is_halted()
            if not halt:
                losses = _consecutive_losses(conn)
                if losses >= int(cfg["intraday_max_consecutive_losses"]):
                    _trip_halt(f"连亏 {losses} 笔（阈值 {cfg['intraday_max_consecutive_losses']}）")
                    halt = is_halted()
            out["halted"] = bool(halt)

            # 5) 开仓
            if not halt:
                n_open = conn.execute(
                    "SELECT COUNT(*) AS c FROM intraday_positions "
                    "WHERE closed_ts IS NULL").fetchone()["c"]
                for sym in symbols:
                    p = preds.get(sym) or {}
                    if not p.get("tradeable") or p.get("direction") not in ("涨", "跌"):
                        continue
                    if p.get("prob") is None or p["prob"] < float(cfg["intraday_min_prob"]):
                        continue
                    if n_open >= int(cfg["intraday_max_open_positions"]):
                        break
                    already = conn.execute(
                        "SELECT COUNT(*) AS c FROM intraday_positions "
                        "WHERE symbol=? AND closed_ts IS NULL", (sym,)).fetchone()["c"]
                    if already or _in_cooldown(conn, sym, cfg, now):
                        continue
                    r = _open(conn, p, cfg, now)
                    if r:
                        out["opened"].append(r)
                        n_open += 1
            conn.commit()
    except Exception as e:  # noqa: BLE001 — 主循环兜底，绝不拖垮 daemon
        out["error"] = repr(e)[:300]
        _log(f"❌ 4h 轮异常（已兜底）: {out['error']}")
    out["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return out


# ── 状态 / 报表 ─────────────────────────────────────────────────────────────

def status(db_path: Optional[str] = None) -> dict:
    ensure_db(db_path)
    with _conn(db_path) as conn:
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM intraday_positions WHERE closed_ts IS NULL "
            "ORDER BY opened_ts").fetchall()]
        agg = conn.execute(
            "SELECT COUNT(*) AS n, SUM(pnl_usdt) AS pnl, "
            "SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) AS wins "
            "FROM intraday_positions WHERE closed_ts IS NOT NULL").fetchone()
        losses = _consecutive_losses(conn)
    n = agg["n"] or 0
    return {
        "open_positions": open_rows,
        "closed_trades": n,
        "win_rate": round((agg["wins"] or 0) / n, 4) if n else None,
        "total_pnl_usdt": round(agg["pnl"] or 0.0, 2),
        "consecutive_losses": losses,
        "halted": is_halted(),
    }


def stats(db_path: Optional[str] = None) -> dict:
    """驾驶舱 /api/intraday/stats 数据源：命中率 + 持仓 + 最新预测 + 熔断。"""
    ensure_db(db_path)
    now = int(time.time() * 1000)
    with _conn(db_path) as conn:
        def _hit(days: int) -> Optional[float]:
            r = conn.execute(
                "SELECT SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) AS h, "
                "SUM(CASE WHEN hit IS NOT NULL THEN 1 ELSE 0 END) AS c "
                "FROM intraday_predictions WHERE bar_ts>=?",
                (now - days * 86400_000,)).fetchone()
            return round((r["h"] or 0) / r["c"], 4) if r["c"] else None
        recent = [dict(r) for r in conn.execute(
            "SELECT symbol, bar_ts, direction, prob, tradeable, entry, stop, take, "
            "atr_pct, outcome_ret, hit, reason, why_text FROM intraday_predictions "
            "ORDER BY bar_ts DESC LIMIT 30").fetchall()]
        st = status(db_path)
    return {
        "hit_rate_7d": _hit(7), "hit_rate_30d": _hit(30),
        "consecutive_losses": st["consecutive_losses"],
        "halted": st["halted"],
        "closed_trades": st["closed_trades"],
        "win_rate": st["win_rate"],
        "total_pnl_usdt": st["total_pnl_usdt"],
        "positions": st["open_positions"],
        "recent_predictions": recent,
    }


def report(days: int = 30, db_path: Optional[str] = None) -> str:
    ensure_db(db_path)
    since = int((time.time() - days * 86400) * 1000)
    with _conn(db_path) as conn:
        preds = conn.execute(
            "SELECT symbol, COUNT(*) AS n, "
            "SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) AS hits, "
            "SUM(CASE WHEN hit IS NOT NULL THEN 1 ELSE 0 END) AS calls "
            "FROM intraday_predictions WHERE bar_ts>=? GROUP BY symbol",
            (since,)).fetchall()
        trades = conn.execute(
            "SELECT symbol, COUNT(*) AS n, SUM(pnl_usdt) AS pnl, "
            "SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END) AS wins "
            "FROM intraday_positions WHERE closed_ts>=? AND closed_ts IS NOT NULL "
            "GROUP BY symbol", (since,)).fetchall()
    lines = [f"## 4h 引擎报表（近 {days} 天）", "", "### 前向预测战绩（诚实回填）", "",
             "| 币种 | 预测数 | 方向调用 | 命中率 |", "| --- | --- | --- | --- |"]
    for r in preds:
        hr = f"{(r['hits'] or 0) / r['calls']:.1%}" if r["calls"] else "—"
        lines.append(f"| {r['symbol']} | {r['n']} | {r['calls'] or 0} | {hr} |")
    lines += ["", "### 模拟盘交易", "", "| 币种 | 笔数 | 胜率 | 累计盈亏(U) |",
              "| --- | --- | --- | --- |"]
    for r in trades:
        wr = f"{(r['wins'] or 0) / r['n']:.1%}" if r["n"] else "—"
        lines.append(f"| {r['symbol']} | {r['n']} | {wr} | {round(r['pnl'] or 0, 2)} |")
    st = status(db_path)
    lines += ["", f"- 当前持仓 {len(st['open_positions'])} · 连亏 {st['consecutive_losses']}"
              f" · 熔断 {'是' if st['halted'] else '否'}"]
    return "\n".join(lines)


def close_manual(symbol: str, reason: str = "manual",
                 db_path: Optional[str] = None) -> dict:
    ensure_db(db_path)
    now = int(time.time() * 1000)
    with _conn(db_path) as conn:
        pos = conn.execute(
            "SELECT * FROM intraday_positions WHERE symbol=? AND closed_ts IS NULL",
            (symbol.upper(),)).fetchone()
        if not pos:
            return {"ok": False, "error": "无该币未平仓位"}
        price = latest_price(symbol.upper()) or pos["entry"]
        r = _close(conn, pos, price, reason, now)
        conn.commit()
    return {"ok": True, **r}


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 4h 自动模拟下单引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cycle", help="跑一轮：盯平仓→落预测→回填→开仓")
    c.add_argument("--symbols", default="", help="逗号分隔；缺省用配置 watchlist")
    sub.add_parser("status", help="当前持仓与累计战绩")
    r = sub.add_parser("report", help="前向战绩报表")
    r.add_argument("--days", type=int, default=30)
    cl = sub.add_parser("close", help="手动平仓")
    cl.add_argument("symbol")
    cl.add_argument("--reason", default="manual")
    sub.add_parser("resume", help="解除熔断")
    args = ap.parse_args()

    if args.cmd == "cycle":
        syms = [s for s in args.symbols.split(",") if s.strip()] or None
        print(json.dumps(cycle(syms), ensure_ascii=False, indent=2))
    elif args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.cmd == "report":
        print(report(args.days))
    elif args.cmd == "close":
        print(json.dumps(close_manual(args.symbol, args.reason), ensure_ascii=False))
    elif args.cmd == "resume":
        print("✅ 已解除熔断" if resume() else "（当前未熔断）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
