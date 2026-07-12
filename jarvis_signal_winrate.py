#!/usr/bin/env python3
"""贾维斯 JARVIS — 单信号级历史胜率回测（信号可信度引擎）。

与 `jarvis_signal_replay`（共识开仓、写 paper_positions 台账）不同，本引擎
回答的是用户对**单格信号**的信任问题：「面板上这个海龟做多信号，历史上出现
之后到底涨没涨？」——对 12 套系统的每类信号（多/空）做边沿触发统计：

  - 触发口径：某系统方向从 非该方向 → bullish/bearish 的**首次翻转 bar**
    记一个样本（信号持续多根只记首根，模拟用户「看到信号出现就进场」）。
  - 入场口径：触发 bar 收盘价市价入场（用户行为口径，非计划挂单价）。
  - 离场口径：其后 N 根内先触 SL→亏 / 先触 TP→赚（同根双触保守按亏），
    N 根仍未触发则按第 N 根收盘离场、盈亏定输赢；无交易计划的信号按
    N 根后收盘方向一致性定输赢（horizon 模式）。
  - N 与实时模拟盘时间止损同口径：15m=96根(1天) / 1h=72根(3天) /
    4h=42根(7天) / 1d=14根。
  - 防未来函数：信号只吃「触发 bar 及之前 WINDOW 根」切片；未来数据只用于
    **度量结果**，绝不参与信号生成。

产出（按 系统×方向 与 方向汇总 两级）：
  胜率、平均盈亏比(payoff)、期望值/笔、最大回撤(MAE 最差值)、平均持有根数、
  样本量与 low_sample 标记。结果缓存 JSON，供 dashboard 随信号展示。

用法：
  python jarvis_signal_winrate.py run --symbols BTC --tfs 4h --days 30
  python jarvis_signal_winrate.py show --symbol BTC --tf 4h
  python jarvis_signal_winrate.py status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

import pandas as pd

WINDOW = 300                  # 信号窗口根数（与线上 fetch_klines_df limit=300 同口径）
LOW_SAMPLE_N = 30             # 样本量阈值（与 jarvis_paper_trader.LOW_SAMPLE_N 同值）
_TF_BARS_PER_DAY = {"5m": 288, "15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}
# 观察期 = 实时模拟盘时间止损天数（jarvis_paper_trader._TWELVE_TIME_STOP）换算 bar 数
# 5m 超短线与 15m 同为 1 天（288 根）：缺省回退 7 天在 5m 下会膨胀到 2016 根，必须显式给；
# 30m 介于 15m(1天) 与 1h(3天) 之间取 2 天（96 根）
_TIME_STOP_DAYS = {"5m": 1, "15m": 1, "30m": 2, "1h": 3, "4h": 7, "1d": 14}

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
CACHE_PATH = os.path.join(CONFIG_DIR, "signal_winrate.json")

LOG_PREFIX = "[SIGWIN]"


def _log(msg: str) -> None:
    print(f"{LOG_PREFIX} [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def horizon_bars(tf: str) -> int:
    return _TIME_STOP_DAYS.get(tf, 7) * _TF_BARS_PER_DAY.get(tf, 6)


# ─────────────────────────── 单样本离场判定 ───────────────────────────

def _resolve_sample(df: pd.DataFrame, i: int, side: str, entry: float,
                    sl: float | None, tp: float | None, horizon: int) -> dict | None:
    """从触发 bar i 的下一根起向前扫 horizon 根，判定样本结果。

    仅用未来数据度量结果（信号生成不碰未来）。返回：
      {win, pnl_pct, mae_pct, bars_held, mode, exit_price}。
    尾部悬空样本（观察期被数据末尾截断且未触 SL/TP）返回 None 丢弃——
    半程快照会系统性低估波动，宁可少样本不要脏样本。
    """
    start, end = i + 1, min(i + horizon, len(df) - 1)
    if start > end:
        return None
    truncated = end < i + horizon
    sign = 1.0 if side == "long" else -1.0
    has_plan = sl is not None and tp is not None
    # 计划自洽校验：市价入场后 SL/TP 必须仍在正确一侧，否则退化为 horizon 模式
    if has_plan:
        if side == "long" and not (sl < entry < tp):
            has_plan = False
        if side == "short" and not (tp < entry < sl):
            has_plan = False

    mae = 0.0          # 最大不利偏移（%，≤0），即持有期间的最大回撤
    exit_price = None
    bars_held = end - i
    for j in range(start, end + 1):
        hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
        adverse = (lo / entry - 1.0) * 100 if side == "long" else (1.0 - hi / entry) * 100
        mae = min(mae, adverse)
        if has_plan:
            if side == "long":
                if lo <= sl:            # 同根双触保守按止损（悲观口径）
                    exit_price, bars_held = sl, j - i
                    break
                if hi >= tp:
                    exit_price, bars_held = tp, j - i
                    break
            else:
                if hi >= sl:
                    exit_price, bars_held = sl, j - i
                    break
                if lo <= tp:
                    exit_price, bars_held = tp, j - i
                    break
    if exit_price is None:
        if truncated:
            return None                 # 悬空样本：既没走完观察期也没触 SL/TP
        exit_price = float(df["close"].iloc[end])   # 满观察期 → 期末收盘离场
    pnl_pct = (exit_price / entry - 1.0) * 100 * sign
    return {"win": pnl_pct > 0, "pnl_pct": round(pnl_pct, 4),
            "mae_pct": round(mae, 4), "bars_held": bars_held,
            "mode": "plan" if has_plan else "horizon",
            "exit_price": round(float(exit_price), 8)}


# ─────────────────────────── 样本聚合 ───────────────────────────

def _grade(samples: list[dict]) -> dict | None:
    """一组样本 → 胜率/盈亏比/期望/最大回撤 标准块（空样本返回 None）。"""
    if not samples:
        return None
    pnls = [s["pnl_pct"] for s in samples]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    avg_win = round(sum(wins) / len(wins), 3) if wins else None
    avg_loss = round(sum(losses) / len(losses), 3) if losses else None   # ≤0
    payoff = None
    if avg_win is not None and avg_loss is not None and avg_loss < 0:
        payoff = round(avg_win / abs(avg_loss), 2)
    expectancy = round(sum(pnls) / n, 3)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / n, 1),
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "payoff_ratio": payoff,
        "expectancy_pct": expectancy,
        "max_drawdown_pct": round(min(s["mae_pct"] for s in samples), 2),
        "avg_bars_held": round(sum(s["bars_held"] for s in samples) / n, 1),
        "low_sample": n < LOW_SAMPLE_N,
    }


# ─────────────────────────── 单 symbol×tf 回测内核 ───────────────────────────

def backtest_df(symbol: str, tf: str, df: pd.DataFrame, *,
                run_all=None, stride: int = 1, progress_cb=None) -> dict:
    """对一段历史 K 线做单信号级边沿触发回测（纯本地计算，测试可注入 run_all）。

    Args:
        run_all: fn(window_df) -> list[signal dict]；默认 jarvis_twelve_systems.run_all
        stride: 每 N 根评估一次信号（加速用，默认逐根）
        progress_cb: fn(done_bars, total_bars)
    Returns:
        {symbol, tf, days?, horizon_bars, bars, samples, systems, directions,
         trades, computed_at}；K 线不足带 error。trades 为逐笔明细（按触发时间
        升序）：{t, exit_t(ms), system, side, entry, sl, tp, exit_price, win,
        pnl_pct, bars_held, mode}，供前端在 K 线图上标记历史盈损点。
    """
    if run_all is None:
        import jarvis_twelve_systems as jts
        run_all = jts.run_all

    sym = (symbol if symbol.upper().endswith(("USDT", "USDC")) else symbol + "USDT").upper()
    horizon = horizon_bars(tf)
    if df is None or len(df) <= WINDOW + 1:
        return {"symbol": sym, "tf": tf, "horizon_bars": horizon, "bars": 0,
                "samples": 0, "systems": {}, "directions": {}, "trades": [],
                "computed_at": time.time(),
                "error": f"K线不足（{0 if df is None else len(df)} ≤ {WINDOW + 1}）"}

    per: dict[tuple[str, str], list[dict]] = {}     # (system, side) -> samples
    names: dict[str, str] = {}                       # system -> name_cn
    last_dir: dict[str, str] = {}                    # system -> 上次评估方向
    trades: list[dict] = []                          # 逐笔明细（K 线标记用）
    total = len(df) - WINDOW
    n_samples = 0

    for i in range(WINDOW, len(df)):
        if (i - WINDOW) % max(1, stride) != 0:
            continue
        window = df.iloc[i - WINDOW: i + 1]          # 防未来函数：只含当前及之前
        close = float(df["close"].iloc[i])
        try:
            signals = run_all(window)
        except Exception as exc:  # noqa: BLE001 — 单 bar 信号异常跳过，不拖垮全程
            _log(f"⚠️ bar {i} 信号计算异常: {repr(exc)[:120]}")
            continue
        for sig in signals:
            system = sig.get("system") or ""
            direction = sig.get("direction") or "neutral"
            prev = last_dir.get(system, "neutral")
            last_dir[system] = direction
            if direction not in ("bullish", "bearish") or direction == prev:
                continue                              # 只记方向翻转的边沿触发
            side = "long" if direction == "bullish" else "short"
            plan = sig.get("trade_plan") or {}
            sl, tp = plan.get("stop_loss"), plan.get("take_profit")
            sl_f = float(sl) if sl is not None else None
            tp_f = float(tp) if tp is not None else None
            res = _resolve_sample(df, i, side, close, sl_f, tp_f, horizon)
            if res is None:
                continue                              # 观察期不足的尾部样本丢弃
            per.setdefault((system, side), []).append(res)
            names[system] = sig.get("name_cn") or system
            n_samples += 1
            trades.append({
                "t": int(df["time"].iloc[i]),
                "exit_t": int(df["time"].iloc[i + res["bars_held"]]),
                "system": system,
                "side": side,
                "entry": round(close, 8),
                "sl": round(sl_f, 8) if sl_f is not None else None,
                "tp": round(tp_f, 8) if tp_f is not None else None,
                "exit_price": res["exit_price"],
                "win": res["win"],
                "pnl_pct": res["pnl_pct"],
                "bars_held": res["bars_held"],
                "mode": res["mode"],
            })
        if progress_cb and (i - WINDOW) % 50 == 0:
            progress_cb(i - WINDOW + 1, total)

    systems_out: dict[str, dict] = {}
    for (system, side), samples in per.items():
        blk = systems_out.setdefault(
            system, {"name_cn": names.get(system, system), "long": None, "short": None})
        blk[side] = _grade(samples)
    directions_out = {
        side: _grade([s for (sy, sd), ss in per.items() if sd == side for s in ss])
        for side in ("long", "short")
    }
    if progress_cb:
        progress_cb(total, total)
    return {"symbol": sym, "tf": tf, "horizon_bars": horizon, "bars": total,
            "samples": n_samples, "systems": systems_out,
            "directions": directions_out, "trades": trades,
            "computed_at": time.time()}


# ─────────────────────────── 结果缓存（JSON） ───────────────────────────

_CACHE_LOCK = threading.Lock()


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_result(stats: dict) -> None:
    key = f"{stats['symbol']}:{stats['tf']}"
    with _CACHE_LOCK:
        cache = _load_cache()
        cache[key] = stats
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)


def get_cached(symbol: str, tf: str) -> dict | None:
    sym = (symbol if symbol.upper().endswith(("USDT", "USDC")) else symbol + "USDT").upper()
    with _CACHE_LOCK:
        return _load_cache().get(f"{sym}:{tf}")


# ─────────────────────────── 批量回测 + 进度状态 ───────────────────────────

_STATE_LOCK = threading.Lock()
_STATE: dict = {
    "running": False, "progress": 0, "detail": "", "started_at": None,
    "finished_at": None, "result": None, "error": None,
}


def get_status() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


def _set_state(**kw) -> None:
    with _STATE_LOCK:
        _STATE.update(kw)


def run_backtest(symbols: list[str], tfs: list[str], days: int = 30,
                 stride: int = 1) -> dict:
    """批量单信号回测（同步执行；dashboard 用后台线程包裹）。已在跑则拒绝。"""
    with _STATE_LOCK:
        if _STATE["running"]:
            return {"ok": False, "error": "信号胜率回测已在进行中"}
        _STATE.update({"running": True, "progress": 0, "detail": "准备中",
                       "started_at": time.time(), "finished_at": None,
                       "result": None, "error": None})
    t0 = time.time()
    pairs = [(s, tf) for s in symbols for tf in tfs if tf in _TF_BARS_PER_DAY]
    out: list[dict] = []
    total_samples = 0
    try:
        import jarvis_signal_replay as jsr   # 复用其分页取数（限流退避+缓存降级）
        for idx, (s, tf) in enumerate(pairs):
            base = idx / max(1, len(pairs)) * 100

            def _cb(done, total, _base=base):
                pct = _base + (done / max(1, total)) * (100 / max(1, len(pairs)))
                _set_state(progress=round(min(99.0, pct), 1))

            _set_state(detail=f"拉取 {s} {tf} 历史K线（{days}天）")
            df = jsr.fetch_history_df(s, tf, days)
            _set_state(detail=f"回测 {s} {tf}（{0 if df is None else len(df)} 根）")
            r = backtest_df(s, tf, df, stride=stride, progress_cb=_cb)
            r["days"] = days
            if not r.get("error"):
                _save_result(r)
            out.append(r)
            total_samples += r.get("samples", 0)
            _log(f"{s} {tf}: 样本 {r.get('samples')}"
                 + (f" / {r.get('error')}" if r.get("error") else ""))
        result = {"ok": True, "days": days,
                  "pairs": [{k: v for k, v in r.items()
                             if k in ("symbol", "tf", "samples", "error")} for r in out],
                  "total_samples": total_samples,
                  "elapsed_s": round(time.time() - t0, 1)}
        _set_state(running=False, progress=100, detail="完成",
                   finished_at=time.time(), result=result)
        return result
    except Exception as exc:  # noqa: BLE001 — 异常必须复位 running，否则永久卡锁
        err = repr(exc)[:300]
        _set_state(running=False, detail="失败", finished_at=time.time(), error=err)
        _log(f"❌ 信号胜率回测异常: {err}")
        return {"ok": False, "error": err}


def start_backtest_async(symbols: list[str], tfs: list[str], days: int = 30,
                         stride: int = 1) -> dict:
    """异步启动（daemon 线程），立即返回；进度经 get_status() 查询。"""
    with _STATE_LOCK:
        if _STATE["running"]:
            return {"ok": False, "error": "信号胜率回测已在进行中"}
    threading.Thread(target=run_backtest, args=(symbols, tfs, days, stride),
                     daemon=True, name="signal-winrate").start()
    return {"ok": True, "message": "信号胜率回测已启动", "symbols": symbols,
            "tfs": tfs, "days": days}


# ─────────────────────────── CLI ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="12 系统单信号级历史胜率回测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="同步跑一次回测")
    p_run.add_argument("--symbols", default="BTCUSDT", help="逗号分隔，如 BTC,ETH")
    p_run.add_argument("--tfs", default="4h", help="逗号分隔，如 15m,1h,4h")
    p_run.add_argument("--days", type=int, default=30)
    p_run.add_argument("--stride", type=int, default=1, help="每N根评估一次信号")
    p_run.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="查看缓存的统计结果")
    p_show.add_argument("--symbol", default="BTCUSDT")
    p_show.add_argument("--tf", default="4h")

    sub.add_parser("status", help="查看回测进度")

    args = ap.parse_args()
    if args.cmd == "run":
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        tfs = [t.strip() for t in args.tfs.split(",") if t.strip()]
        out = run_backtest(syms, tfs, days=args.days, stride=args.stride)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        elif out.get("ok"):
            print(f"回测完成：{out['total_samples']} 个信号样本，耗时 {out['elapsed_s']}s")
            for p in out["pairs"]:
                print(f"  {p['symbol']} {p['tf']}: 样本 {p['samples']}"
                      + (f"（{p['error']}）" if p.get("error") else ""))
        else:
            print(f"❌ {out.get('error')}")
            return 1
    elif args.cmd == "show":
        print(json.dumps(get_cached(args.symbol, args.tf) or {"error": "无缓存"},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(get_status(), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
