#!/usr/bin/env python3
"""单信号级历史胜率回测（jarvis_signal_winrate）离线 smoketest：合成数据，不联网。"""

from __future__ import annotations

import math
import os
import random
import tempfile

import pandas as pd

import jarvis_signal_winrate as jsw

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name} {extra}")


def synth_df(n: int = 420, seed: int = 7, drift: float = 0.0) -> pd.DataFrame:
    """合成 OHLCV（随机游走，可加漂移）。"""
    rng = random.Random(seed)
    rows = []
    price = 100.0
    for i in range(n):
        o = price
        c = max(1e-6, o * (1 + drift + rng.gauss(0, 0.01)))
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
        rows.append({"time": 1_700_000_000_000 + i * 3_600_000,
                     "open": o, "high": hi, "low": lo, "close": c,
                     "volume": 1000 + rng.random() * 100})
        price = c
    return pd.DataFrame(rows)


# ── 1. _resolve_sample 离场判定 ──────────────────────────────────────────
# 手工构造 5 根未来 bar：entry=100，long SL=95 TP=110
df5 = pd.DataFrame([
    {"time": t, "open": 100, "high": h, "low": lo, "close": c, "volume": 1}
    for t, (h, lo, c) in enumerate([
        (101, 99, 100),    # i=0 触发根（未来从 i=1 起）
        (102, 98, 101),    # 未触
        (111, 100, 108),   # 触 TP=110 → 赢
        (103, 94, 95),     # （不该到这）
        (100, 99, 99.5),
    ])
])
r = jsw._resolve_sample(df5, 0, "long", 100.0, 95.0, 110.0, horizon=4)
check("TP先触=赢", r is not None and r["win"] and r["mode"] == "plan", str(r))
check("bars_held=触发到离场根数", r is not None and r["bars_held"] == 2, str(r))
check("MAE为负(最大回撤)", r is not None and r["mae_pct"] < 0, str(r))

# 同根双触（high≥TP 且 low≤SL）保守按止损计亏
df_dual = pd.DataFrame([
    {"time": 0, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
    {"time": 1, "open": 100, "high": 111, "low": 94, "close": 100, "volume": 1},
    {"time": 2, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
])
r = jsw._resolve_sample(df_dual, 0, "long", 100.0, 95.0, 110.0, horizon=2)
check("同根双触保守计亏", r is not None and not r["win"] and r["pnl_pct"] < 0, str(r))

# 空单：low≤TP 先触 → 赢
df_short = pd.DataFrame([
    {"time": 0, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
    {"time": 1, "open": 100, "high": 103, "low": 89, "close": 92, "volume": 1},
])
r = jsw._resolve_sample(df_short, 0, "short", 100.0, 105.0, 90.0, horizon=1)
check("空单TP先触=赢", r is not None and r["win"] and r["pnl_pct"] > 0, str(r))

# 无计划 → horizon 模式按期末收盘定输赢
df_h = pd.DataFrame([
    {"time": t, "open": 100, "high": 101, "low": 99, "close": c, "volume": 1}
    for t, c in enumerate([100, 100.5, 101, 102])
])
r = jsw._resolve_sample(df_h, 0, "long", 100.0, None, None, horizon=3)
check("horizon模式期末涨=赢", r is not None and r["win"] and r["mode"] == "horizon", str(r))

# 尾部悬空（观察期被截断且未触发）→ None 丢弃
r = jsw._resolve_sample(df_h, 0, "long", 100.0, 90.0, 120.0, horizon=50)
check("悬空样本丢弃", r is None, str(r))

# 计划不自洽（long 但 entry 不在 SL/TP 之间）→ 退化 horizon 模式
r = jsw._resolve_sample(df_h, 0, "long", 100.0, 105.0, 120.0, horizon=3)
check("计划不自洽退化horizon", r is not None and r["mode"] == "horizon", str(r))

# ── 2. _grade 聚合口径 ───────────────────────────────────────────────────
g = jsw._grade([
    {"pnl_pct": 2.0, "mae_pct": -1.0, "bars_held": 3},
    {"pnl_pct": 4.0, "mae_pct": -0.5, "bars_held": 5},
    {"pnl_pct": -2.0, "mae_pct": -3.0, "bars_held": 2},
])
check("胜率=2/3", g is not None and abs(g["win_rate_pct"] - 66.7) < 0.1, str(g))
check("盈亏比=均盈/|均亏|", g is not None and abs(g["payoff_ratio"] - 1.5) < 0.01, str(g))
check("最大回撤取最差MAE", g is not None and g["max_drawdown_pct"] == -3.0, str(g))
check("样本<30标记low_sample", g is not None and g["low_sample"], str(g))
check("空样本返回None", jsw._grade([]) is None)

# ── 3. backtest_df 边沿触发 + 结构 ───────────────────────────────────────
CALLS = {"n": 0}


def fake_run_all(window: pd.DataFrame) -> list[dict]:
    """注入信号器：收盘价高于窗口均值 → bullish（带计划）；低于 → bearish（无计划）。"""
    CALLS["n"] += 1
    close = float(window["close"].iloc[-1])
    mean = float(window["close"].mean())
    if close > mean * 1.01:
        atr = close * 0.02
        return [{"system": "fake", "name_cn": "假信号", "direction": "bullish",
                 "strength": 0.7, "trade_plan": {
                     "stop_loss": close - 2 * atr, "take_profit": close + 2 * atr}}]
    if close < mean * 0.99:
        return [{"system": "fake", "name_cn": "假信号", "direction": "bearish",
                 "strength": 0.7, "trade_plan": None}]
    return [{"system": "fake", "name_cn": "假信号", "direction": "neutral",
             "strength": 0.1, "trade_plan": None}]


df = synth_df(420, seed=11)
out = jsw.backtest_df("TEST", "1h", df, run_all=fake_run_all)
check("回测不抛出且结构完整",
      all(k in out for k in ("symbol", "tf", "horizon_bars", "samples",
                             "systems", "directions", "trades", "computed_at")), str(out)[:200])
# 逐笔明细（K 线标记历史盈损点用）：条数与样本数一致，字段齐全，时间升序
check("逐笔明细条数=样本数", len(out["trades"]) == out["samples"],
      f"trades={len(out['trades'])} samples={out['samples']}")
if out["trades"]:
    t0 = out["trades"][0]
    check("逐笔明细字段齐全",
          all(k in t0 for k in ("t", "exit_t", "system", "side", "entry",
                                "sl", "tp", "exit_price", "win", "pnl_pct",
                                "bars_held", "mode")), str(t0)[:200])
    check("逐笔明细按触发时间升序",
          all(a["t"] <= b["t"] for a, b in zip(out["trades"], out["trades"][1:])))
    check("出场时间不早于入场", all(t["exit_t"] >= t["t"] for t in out["trades"]))
check("symbol 规整为 USDT 对", out["symbol"] == "TESTUSDT", out["symbol"])
check("观察期=1h→72根", out["horizon_bars"] == 72, str(out["horizon_bars"]))
has_any = out["samples"] > 0 and "fake" in out["systems"]
check("产出样本并按系统归组", has_any, str(out)[:200])
if has_any:
    blk = out["systems"]["fake"]
    sides = [s for s in ("long", "short") if blk.get(s)]
    check("系统块含方向统计", len(sides) >= 1, str(blk)[:200])
    d = out["directions"]
    check("方向汇总与系统块同源",
          sum((blk[s] or {}).get("trades", 0) for s in ("long", "short"))
          == sum((d[s] or {}).get("trades", 0) for s in ("long", "short")), str(d)[:200])

# 边沿触发：同方向连续 bar 只记一次（样本数应远小于评估 bar 数）
check("边沿触发样本数远小于bar数", out["samples"] < out["bars"] / 3,
      f"samples={out['samples']} bars={out['bars']}")

# stride 加速路径可跑
out_s = jsw.backtest_df("TEST", "1h", df, run_all=fake_run_all, stride=4)
check("stride加速路径可跑", out_s["samples"] >= 0 and not out_s.get("error"), str(out_s)[:120])

# K 线不足 → error 且不抛出
out_short = jsw.backtest_df("TEST", "1h", synth_df(100), run_all=fake_run_all)
check("K线不足返回error", bool(out_short.get("error")) and out_short["samples"] == 0,
      str(out_short)[:120])

# ── 4. 缓存写读回环（重定向到临时文件，不污染真实缓存） ──────────────────
_orig = jsw.CACHE_PATH
with tempfile.TemporaryDirectory() as td:
    jsw.CACHE_PATH = os.path.join(td, "wr.json")
    jsw._save_result(out)
    back = jsw.get_cached("TEST", "1h")
    check("缓存写读回环", back is not None and back["samples"] == out["samples"],
          str(back)[:120])
    check("未知key返回None", jsw.get_cached("NOPE", "4h") is None)
jsw.CACHE_PATH = _orig

# ── 5. 真信号引擎冒烟：12套跑通 + explain 画像下发 ───────────────────────
import jarvis_twelve_systems as jts  # noqa: E402

sigs = jts.run_all(synth_df(360, seed=3))
check("12套信号器全部出信号", len(sigs) == 12, str(len(sigs)))
with_explain = [s for s in sigs if isinstance(s.get("explain"), dict)]
check("每个信号附explain画像", len(with_explain) == 12, str(len(with_explain)))
ex = sigs[0].get("explain") or {}
check("explain含四要素", all(k in ex for k in ("type", "trigger", "best_tfs", "lag")),
      str(ex)[:120])

# 真引擎 × 合成数据端到端（stride=8 控时长）
out_real = jsw.backtest_df("TEST", "4h", synth_df(400, seed=5, drift=0.001),
                           stride=8)
check("真引擎端到端不抛出", not out_real.get("error") or out_real["samples"] == 0,
      str(out_real)[:160])
check("真引擎观察期=4h→42根", out_real["horizon_bars"] == 42, str(out_real["horizon_bars"]))

# martingale/arbitrage 恒中性 → 不应出现在胜率统计里
check("资金管理类系统无样本",
      "martingale" not in out_real["systems"] and "arbitrage" not in out_real["systems"],
      str(list(out_real["systems"].keys())))

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
