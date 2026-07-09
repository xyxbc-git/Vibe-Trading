#!/usr/bin/env python3
"""贾维斯 JARVIS — 12 系统信号历史回放引擎：预积累信号胜率样本。

真实模拟盘一天只有几笔单，每个统计分组攒 30 笔要等数周。本引擎用历史 K 线
逐根回放 12 系统共识信号，按与 `jarvis_paper_trader.open_from_twelve` 同一套
开仓/离场规则虚拟成交，几分钟内为「信号胜率统计」灌入可筛选的历史样本。

口径与隔离（铁律）：
  - 开仓：共识方向明确 + 置信度 ≥ TWELVE_MIN_CONFIDENCE + 共识计划自洽（现价
    在 SL/TP 正确一侧）——与实时路径完全同一套判定。
  - 离场：止损/止盈（按 bar 高低价触发，同根双触保守按止损）→ 到期（按 tf
    换算 bar 数）→ 共识反转（每 4 根重算一次）。
  - 防未来函数：信号与市场状态打标只吃「当前 bar 及之前 REPLAY_WINDOW 根」
    的切片，绝不偷看后面的数据。
  - 严格隔离：样本写入 paper_positions 时 signal_source='replay'，带全归因
    标签；**不动钱包**（固定名义 100U/笔，量纲可比）；回放结束不留 open 单
    （未走完的交易直接删除，样本只收完整闭环）。
  - 幂等：同 symbol+tf 重新回放前先清旧 replay 记录，不叠加。

用法：
  python jarvis_signal_replay.py run --symbols BTC --tfs 15m --days 30
  python jarvis_signal_replay.py run --symbols BTC,ETH --tfs 15m,1h,4h --days 30
  python jarvis_signal_replay.py status
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import pandas as pd

import jarvis_journal as jj
import jarvis_paper_trader as jpt

REPLAY_WINDOW = 300           # 信号窗口根数（与线上 fetch_klines_df limit=300 同口径）
REPLAY_NOTIONAL_USDT = 100.0  # 每笔固定名义，保证跨样本盈亏量纲可比
REVERSAL_CHECK_STRIDE = 4     # 持仓期间每 N 根重算一次共识判反转（控制计算量）
_PAGE_LIMIT = 500             # 分页拉取每页根数（与 jcd 其它调用同量级）
_PAGE_SLEEP_S = 0.15          # 页间隙，礼貌限频

_TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
_TF_BARS_PER_DAY = {"15m": 96, "1h": 24, "4h": 6, "1d": 1}

LOG_PREFIX = "[REPLAY]"


def _log(msg: str) -> None:
    print(f"{LOG_PREFIX} [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ─────────────────────────── 历史 K 线分页拉取 ───────────────────────────

def fetch_history_df(symbol: str, tf: str, days: int) -> pd.DataFrame | None:
    """startTime 游标分页拉取历史 K 线（复用 jarvis_crypto_data 的限流退避+缓存降级）。

    额外多拉 REPLAY_WINDOW 根作为窗口预热；失败返回 None，绝不抛出。
    """
    try:
        import jarvis_crypto_data as jcd
        sym = (symbol or "").upper().replace("-", "").replace("/", "")
        if not sym.endswith(("USDT", "USDC")):
            sym += "USDT"
        tf_ms = _TF_MS.get(tf)
        if not tf_ms:
            return None
        need_bars = days * _TF_BARS_PER_DAY[tf] + REPLAY_WINDOW
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - need_bars * tf_ms
        rows: list[dict] = []
        cursor = start_ms
        # 页数上限兜底：need_bars/页 + 3，防接口异常时死循环
        for _ in range(need_bars // _PAGE_LIMIT + 3):
            raw = jcd._get(jcd.SPOT_API + "/api/v3/klines",
                           {"symbol": sym, "interval": tf, "limit": _PAGE_LIMIT,
                            "startTime": cursor})
            if not isinstance(raw, list) or not raw:
                break
            for k in raw:
                rows.append({"time": int(k[0]),
                             "open": float(k[1]), "high": float(k[2]),
                             "low": float(k[3]), "close": float(k[4]),
                             "volume": float(k[5])})
            last_open = int(raw[-1][0])
            cursor = last_open + tf_ms
            if len(raw) < _PAGE_LIMIT or cursor >= end_ms:
                break
            time.sleep(_PAGE_SLEEP_S)
        if not rows:
            return None
        df = pd.DataFrame(rows).drop_duplicates(subset="time").sort_values("time")
        # 丢未收盘的最新 bar（open_time + tf > now = 进行中），回放只认已收盘
        df = df[df["time"] + tf_ms <= int(time.time() * 1000)]
        return df.reset_index(drop=True) if len(df) else None
    except Exception as exc:  # noqa: BLE001 — 取数失败交由调用方降级
        _log(f"⚠️ {symbol} {tf} 历史K线拉取失败: {exc!r}"[:160])
        return None


# ─────────────────────────── 虚拟开平仓（不动钱包） ───────────────────────────

def _clear_replay(symbol: str, tf: str) -> int:
    """清指定 symbol+tf 的旧 replay 记录（重复回放幂等）。返回删除行数。"""
    jpt.init_positions_table()
    with jj._conn() as conn:
        cur = conn.execute(
            "DELETE FROM paper_positions "
            "WHERE signal_source='replay' AND symbol=? AND signal_tf=?",
            (symbol, tf))
        return cur.rowcount or 0


def _replay_close(pid: int, entry: float, qty: float, side: str,
                  exit_price: float, reason: str, bar_ts: float) -> dict:
    """回放单平仓：只 UPDATE 记录，不下单、不动钱包。"""
    sign = -1 if side == "sell" else 1
    pnl_usdt = round((exit_price - entry) * qty * sign, 4)
    pnl_pct = round((exit_price / entry - 1.0) * 100 * sign, 2)
    with jj._conn() as conn:
        conn.execute(
            """
            UPDATE paper_positions
            SET status='closed', exit_date=?, exit_price=?, exit_reason=?,
                realized_pnl_usdt=?, realized_pnl_pct=?, closed_ts=?
            WHERE id=?
            """,
            (time.strftime("%Y-%m-%d", time.localtime(bar_ts)), exit_price,
             reason, pnl_usdt, pnl_pct, bar_ts, pid))
    return {"position_id": pid, "exit_price": exit_price, "reason": reason,
            "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct}


def _delete_position(pid: int) -> None:
    """删除未走完的回放持仓（回放结束不留 open 单，样本只收完整闭环）。"""
    with jj._conn() as conn:
        conn.execute("DELETE FROM paper_positions WHERE id=?", (pid,))


# ─────────────────────────── 单 symbol×tf 回放内核 ───────────────────────────

def replay_df(symbol: str, tf: str, df: pd.DataFrame, *,
              analyze=None, classify_regime=None, stride: int = 1,
              progress_cb=None) -> dict:
    """对一段历史 K 线做逐根回放（纯本地计算，测试可注入 analyze/classify）。

    Args:
        analyze: fn(window_df) -> {"consensus": {...}}；默认 jts.analyze
        classify_regime: fn(window_df) -> str|None；默认 regime 分类器单 TF
        stride: 无持仓时每 N 根找一次开仓信号（加速用，默认逐根）
        progress_cb: fn(done_bars, total_bars)
    Returns:
        {symbol, tf, bars, trades, opened, closed, cleared, skipped_open}
    """
    import jarvis_twelve_systems as jts
    if analyze is None:
        analyze = jts.analyze
    if classify_regime is None:
        def classify_regime(window: pd.DataFrame):
            try:
                import jarvis_regime_classifier as jrc
                r = jrc.classify_single_tf(window).regime
                return r if r in jpt.REGIMES else None
            except Exception:  # noqa: BLE001 — 打标失败样本归未知
                return None

    sym = (symbol if symbol.endswith(("USDT", "USDC")) else symbol + "USDT").upper()
    cleared = _clear_replay(sym, tf)
    if df is None or len(df) <= REPLAY_WINDOW:
        return {"symbol": sym, "tf": tf, "bars": 0, "trades": [], "opened": 0,
                "closed": 0, "cleared": cleared, "skipped_open": 0,
                "error": f"K线不足（{0 if df is None else len(df)} ≤ {REPLAY_WINDOW}）"}

    min_conf = jpt.TWELVE_MIN_CONFIDENCE
    time_stop_bars = jpt._TWELVE_TIME_STOP.get(tf, 7) * _TF_BARS_PER_DAY.get(tf, 24)
    total = len(df) - REPLAY_WINDOW
    pos = None          # 虚拟持仓 {pid, side, entry, qty, sl, tp, entry_i, systems}
    trades: list[dict] = []
    opened = 0

    for i in range(REPLAY_WINDOW, len(df)):
        # 防未来函数：窗口只含当前 bar 及之前 REPLAY_WINDOW 根
        window = df.iloc[i - REPLAY_WINDOW: i + 1]
        bar = df.iloc[i]
        bar_ts = float(bar["time"]) / 1000.0
        close = float(bar["close"])

        if pos is None:
            if (i - REPLAY_WINDOW) % max(1, stride) != 0:
                continue
            out = analyze(window)
            cons = out.get("consensus") or {}
            direction = cons.get("direction")
            plan = cons.get("trade_plan")
            conf = float(cons.get("confidence") or 0.0)
            if direction not in ("bullish", "bearish") or conf < min_conf or not plan:
                continue
            sl, tp = plan.get("stop_loss"), plan.get("take_profit_1")
            if sl is None or tp is None:
                continue
            side = "buy" if direction == "bullish" else "sell"
            # 与实时路径同一自洽门禁：现价必须在 SL/TP 正确一侧
            if side == "buy" and not (float(sl) < close < float(tp)):
                continue
            if side == "sell" and not (float(tp) < close < float(sl)):
                continue
            qty = round(REPLAY_NOTIONAL_USDT / close, 8)
            systems = [s for s in (plan.get("basis") or []) if s in jpt._TWELVE_NAME_CN]
            regime = classify_regime(window)
            pid = jpt._insert_position(
                sym, qty, time.strftime("%Y-%m-%d", time.localtime(bar_ts)), close,
                None, float(sl), float(tp), jpt._TWELVE_TIME_STOP.get(tf, 7), conf,
                side=side, signal_source="replay", signal_systems=systems,
                signal_tf=tf, signal_regime=regime, opened_ts=bar_ts)
            pos = {"pid": pid, "side": side, "entry": close, "qty": qty,
                   "sl": float(sl), "tp": float(tp), "entry_i": i, "systems": systems}
            opened += 1
        else:
            hi, lo = float(bar["high"]), float(bar["low"])
            reason = exit_price = None
            if pos["side"] == "buy":
                if lo <= pos["sl"]:            # 同根双触保守按止损（悲观口径）
                    reason, exit_price = "stop", pos["sl"]
                elif hi >= pos["tp"]:
                    reason, exit_price = "take", pos["tp"]
            else:
                if hi >= pos["sl"]:
                    reason, exit_price = "stop", pos["sl"]
                elif lo <= pos["tp"]:
                    reason, exit_price = "take", pos["tp"]
            if reason is None and i - pos["entry_i"] >= time_stop_bars:
                reason, exit_price = "time", close
            if reason is None and (i - pos["entry_i"]) % REVERSAL_CHECK_STRIDE == 0:
                cons2 = (analyze(window).get("consensus") or {})
                d2 = cons2.get("direction")
                if (pos["side"] == "buy" and d2 == "bearish") or \
                        (pos["side"] == "sell" and d2 == "bullish"):
                    reason, exit_price = "signal", close
            if reason is not None:
                res = _replay_close(pos["pid"], pos["entry"], pos["qty"],
                                    pos["side"], float(exit_price), reason, bar_ts)
                trades.append(res)
                pos = None

        if progress_cb and (i - REPLAY_WINDOW) % 50 == 0:
            progress_cb(i - REPLAY_WINDOW + 1, total)

    skipped_open = 0
    if pos is not None:   # 回放走完仍在仓：删除，不留 open 单污染实时台账
        _delete_position(pos["pid"])
        skipped_open = 1
    if progress_cb:
        progress_cb(total, total)
    return {"symbol": sym, "tf": tf, "bars": total, "trades": trades,
            "opened": opened, "closed": len(trades), "cleared": cleared,
            "skipped_open": skipped_open}


# ─────────────────────────── 批量回放 + 进度状态 ───────────────────────────

_STATE_LOCK = threading.Lock()
_REPLAY_STATE: dict = {
    "running": False, "progress": 0, "detail": "", "started_at": None,
    "finished_at": None, "result": None, "error": None,
}


def get_status() -> dict:
    with _STATE_LOCK:
        return dict(_REPLAY_STATE)


def _set_state(**kw) -> None:
    with _STATE_LOCK:
        _REPLAY_STATE.update(kw)


def run_replay(symbols: list[str], tfs: list[str], days: int = 30,
               stride: int = 1) -> dict:
    """批量回放（同步执行；dashboard 用后台线程包裹）。已在跑则直接拒绝。"""
    with _STATE_LOCK:
        if _REPLAY_STATE["running"]:
            return {"ok": False, "error": "回放已在进行中"}
        _REPLAY_STATE.update({"running": True, "progress": 0, "detail": "准备中",
                              "started_at": time.time(), "finished_at": None,
                              "result": None, "error": None})
    t0 = time.time()
    pairs = [(s, tf) for s in symbols for tf in tfs if tf in _TF_MS]
    out: list[dict] = []
    total_trades = 0
    try:
        for idx, (s, tf) in enumerate(pairs):
            base = idx / max(1, len(pairs)) * 100

            def _cb(done, total, _base=base):
                pct = _base + (done / max(1, total)) * (100 / max(1, len(pairs)))
                _set_state(progress=round(min(99.0, pct), 1))

            _set_state(detail=f"拉取 {s} {tf} 历史K线（{days}天）")
            df = fetch_history_df(s, tf, days)
            _set_state(detail=f"回放 {s} {tf}（{0 if df is None else len(df)} 根）")
            r = replay_df(s, tf, df, stride=stride, progress_cb=_cb)
            out.append(r)
            total_trades += r.get("closed", 0)
            _log(f"{s} {tf}: 清旧 {r.get('cleared')} / 开 {r.get('opened')} / "
                 f"平 {r.get('closed')}" + (f" / {r.get('error')}" if r.get("error") else ""))
        result = {"ok": True, "days": days, "pairs": out,
                  "total_trades": total_trades,
                  "elapsed_s": round(time.time() - t0, 1)}
        _set_state(running=False, progress=100, detail="完成",
                   finished_at=time.time(), result=result)
        return result
    except Exception as exc:  # noqa: BLE001 — 异常必须复位 running，否则永久卡锁
        err = repr(exc)[:300]
        _set_state(running=False, detail="失败", finished_at=time.time(), error=err)
        _log(f"❌ 回放异常: {err}")
        return {"ok": False, "error": err}


def start_replay_async(symbols: list[str], tfs: list[str], days: int = 30,
                       stride: int = 1) -> dict:
    """异步启动回放（daemon 线程），立即返回；进度经 get_status() 查询。"""
    with _STATE_LOCK:
        if _REPLAY_STATE["running"]:
            return {"ok": False, "error": "回放已在进行中"}
    threading.Thread(target=run_replay, args=(symbols, tfs, days, stride),
                     daemon=True, name="signal-replay").start()
    return {"ok": True, "message": "回放已启动", "symbols": symbols,
            "tfs": tfs, "days": days}


# ─────────────────────────── CLI ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="12 系统信号历史回放：预积累胜率样本")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="同步跑一次回放")
    p_run.add_argument("--symbols", default="BTCUSDT", help="逗号分隔，如 BTC,ETH")
    p_run.add_argument("--tfs", default="15m", help="逗号分隔，如 15m,1h,4h")
    p_run.add_argument("--days", type=int, default=30)
    p_run.add_argument("--stride", type=int, default=1, help="无持仓时每N根找一次信号")
    p_run.add_argument("--json", action="store_true")

    sub.add_parser("status", help="查看回放进度")

    args = ap.parse_args()
    if args.cmd == "run":
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        tfs = [t.strip() for t in args.tfs.split(",") if t.strip()]
        out = run_replay(syms, tfs, days=args.days, stride=args.stride)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        elif out.get("ok"):
            print(f"回放完成：{out['total_trades']} 笔闭环样本，耗时 {out['elapsed_s']}s")
            for p in out["pairs"]:
                print(f"  {p['symbol']} {p['tf']}: 开 {p['opened']} / 平 {p['closed']}"
                      + (f"（{p['error']}）" if p.get("error") else ""))
        else:
            print(f"❌ {out.get('error')}")
            return 1
    else:
        print(json.dumps(get_status(), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
