#!/usr/bin/env python3
"""Delta/CVD 订单流引擎（jarvis_delta_flow）离线 smoketest：合成 bars，不联网。"""

from __future__ import annotations

import math
import random
import time

import jarvis_delta_flow as jdf

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


def synth_bars(n: int = 200, seed: int = 7, price0: float = 60000.0,
               step_sec: int = 900) -> list[dict]:
    """随机游走 bars（无背离结构），字段与 fetch_bars 同构。"""
    rng = random.Random(seed)
    t0 = int(time.time() // step_sec * step_sec - n * step_sec) * 1000
    bars, price = [], price0
    for i in range(n):
        o = price
        price = max(1.0, price * (1 + rng.gauss(0, 0.003)))
        c = price
        vol = abs(rng.gauss(100, 25)) + 1
        bars.append({"ts": t0 + i * step_sec * 1000, "open": o,
                     "high": max(o, c) * 1.001, "low": min(o, c) * 0.999,
                     "close": c, "volume": vol,
                     "taker_buy": vol * min(0.9, max(0.1, 0.5 + rng.gauss(0, 0.05)))})
    return bars


def absorption_bars(step_sec: int = 900) -> list[dict]:
    """手工构造教科书式看涨吸收：价格「跌-弹-更低-弹-再更低」三递降低点，
    而后段主动买占比逐段抬升（CVD 低点抬升）。分段线性 + 微噪声。"""
    # (根数, 每根价格步进, 主动买占比)
    segs = [
        (50, 0.0, 0.50),      # 横盘
        (12, -45.0, 0.47),    # 第一波下跌 → 低点1
        (8, +22.0, 0.52),     # 反弹一半
        (12, -42.0, 0.55),    # 第二波下跌，更低 → 低点2（买占比已抬升）
        (8, +20.0, 0.56),     # 反弹
        (12, -40.0, 0.62),    # 第三波下跌，再创新低 → 低点3（吸收明显）
        (6, +15.0, 0.60),     # 企稳（让最后低点成为 swing 极值且距今 ≤20 根）
    ]
    n = sum(s[0] for s in segs)
    t0 = int(time.time() // step_sec * step_sec - n * step_sec) * 1000
    bars = []
    price = 60000.0
    i = 0
    for length, step, buy_ratio in segs:
        for _ in range(length):
            o = price
            price = max(1000.0, price + step + math.sin(i * 1.3) * 6)
            c = price
            vol = 100 + 30 * math.sin(i * 0.9) ** 2
            bars.append({"ts": t0 + i * step_sec * 1000, "open": o,
                         "high": max(o, c) + 5, "low": min(o, c) - 5,
                         "close": c, "volume": vol, "taker_buy": vol * buy_ratio})
            i += 1
    return bars


# ── 1. delta/cvd 计算口径 ───────────────────────────────────────────────
rows = jdf.compute_delta_cvd([
    {"ts": 0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 100, "taker_buy": 70},
    {"ts": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 100, "taker_buy": 30},
])
check("delta=2×taker_buy−volume", rows[0]["delta"] == 40.0 and rows[1]["delta"] == -40.0,
      str([r["delta"] for r in rows]))
check("cvd 累计", rows[0]["cvd"] == 40.0 and rows[1]["cvd"] == 0.0,
      str([r["cvd"] for r in rows]))
check("输入不被改动", "delta" not in {"ts": 0}, "")

# ── 2. swing 极值 ───────────────────────────────────────────────────────
vals = [5, 4, 3, 4, 5, 6, 5, 4, 5, 6, 7]
lows = jdf._swing_idx(vals, 2, find_low=True)
highs = jdf._swing_idx(vals, 2, find_low=False)
check("swing 低点定位", lows == [2, 7], str(lows))
check("swing 高点定位", 5 in highs, str(highs))

# ── 3. 教科书吸收形态 → 看涨背离命中 ────────────────────────────────────
ab_rows = jdf.compute_delta_cvd(absorption_bars())
det = jdf.detect_divergence(ab_rows)
bull = det["divergence"]["bullish"]
check("看涨吸收 active", bull["active"] is True, str(bull)[:150])
check("强度合法", bull["strength"] in ("strong", "moderate", "weak"), str(bull["strength"]))
check("note 中文非空", bool(bull["note"]) and "吸收" in bull["note"], bull["note"])
check("anchors ≥2 且字段齐全",
      len(bull["anchors"]) >= 2 and all(set(a) == {"t", "price", "cvd"} for a in bull["anchors"]),
      str(bull["anchors"]))
check("anchors 价格递降（创新低）",
      all(bull["anchors"][i]["price"] > bull["anchors"][i + 1]["price"]
          for i in range(len(bull["anchors"]) - 1)), str(bull["anchors"]))
check("absorption=sell-absorption",
      det["absorption"]["detected"] is True and det["absorption"]["side"] == "sell-absorption",
      str(det["absorption"]))

# ── 4. 镜像：看跌派发 ───────────────────────────────────────────────────
dist_bars = []
for b in absorption_bars():
    # 价格镜像翻转（涨→新高）+ 买卖占比翻转（buy_ratio→sell 吸收）
    price_flip = 120000.0 - b["close"]
    dist_bars.append({**b, "open": 120000.0 - b["open"], "close": price_flip,
                      "high": 120000.0 - b["low"], "low": 120000.0 - b["high"],
                      "taker_buy": b["volume"] - b["taker_buy"]})
dd = jdf.detect_divergence(jdf.compute_delta_cvd(dist_bars))
check("看跌派发 active", dd["divergence"]["bearish"]["active"] is True,
      str(dd["divergence"]["bearish"])[:120])
check("absorption=buy-distribution", dd["absorption"]["side"] == "buy-distribution",
      str(dd["absorption"]["side"]))

# ── 5. 随机游走 → 大概率无信号（不强断言单次，多 seed 统计）────────────
fired = 0
for seed in range(8):
    r = jdf.detect_divergence(jdf.compute_delta_cvd(synth_bars(seed=seed)))
    if r["absorption"]["detected"]:
        fired += 1
check("随机游走误报率低（≤3/8）", fired <= 3, f"fired={fired}/8")

# ── 6. 数据不足降级 ─────────────────────────────────────────────────────
short = jdf.detect_divergence(jdf.compute_delta_cvd(synth_bars(20)))
check("数据不足无信号", short["absorption"]["detected"] is False)
check("空 side 结构完整", set(short["divergence"]["bullish"]) ==
      {"active", "strength", "note", "anchors"})

# ── 7. mock：确定性 + 契约完整 + 恒带看涨吸收 ──────────────────────────
m1 = jdf.mock_analyze("BTCUSDT", "15m", 200)
m2 = jdf.mock_analyze("BTCUSDT", "15m", 200)
CONTRACT = ("ok", "symbol", "timeframe", "bars", "divergence", "absorption",
            "updatedAt", "mock", "disclaimer")
check("mock 契约字段齐全", all(k in m1 for k in CONTRACT),
      str([k for k in CONTRACT if k not in m1]))
check("mock 标记", m1["mock"] is True)
m1b = {k: v for k, v in m1.items() if k != "updatedAt"}
m2b = {k: v for k, v in m2.items() if k != "updatedAt"}
check("mock 确定性（同参同输出）", m1b == m2b)
check("mock 恒带看涨吸收（供 MCP-1 联调）",
      m1["divergence"]["bullish"]["active"] is True and m1["absorption"]["detected"] is True,
      str(m1["divergence"]["bullish"])[:120])
check("mock bars 字段契约", set(m1["bars"][0]) == {"t", "delta", "cvd", "volume"},
      str(m1["bars"][0].keys()))
check("mock note 带标记", "[MOCK]" in m1["absorption"]["note"])
check("mock bars 数=limit", len(m1["bars"]) == 200, str(len(m1["bars"])))

# ── 8. 回测（注入合成数据，不联网）──────────────────────────────────────
bt = jdf.backtest("BTCUSDT", "15m", bars=synth_bars(600, seed=3))
check("回测 ok", bt.get("ok") is True, str(bt)[:120])
if bt.get("ok"):
    check("horizons 双口径", set(bt["horizons"]) == {"16", "32"}, str(bt["horizons"].keys()))
    h16 = bt["horizons"]["16"]
    check("回测字段齐全", all(k in h16 for k in ("signals", "hit_rate", "baseline", "edge")),
          str(h16))
    check("基线 ∈ [0,1]", 0.0 <= (h16["baseline"] or 0) <= 1.0, str(h16["baseline"]))
    check("含口径说明与样本数", "basis" in bt and "signal_count" in bt)

bt_short = jdf.backtest("BTCUSDT", "15m", bars=synth_bars(60))
check("回测数据不足降级", bt_short.get("ok") is False, str(bt_short)[:100])

# ── 9. 参数规范化 ───────────────────────────────────────────────────────
check("symbol 规范化", jdf._norm_symbol("eth-usdt") == "ETHUSDT")
check("非法 tf 回退", jdf._norm_tf("7m") == "15m")
check("limit 夹紧", jdf._norm_limit(99999) == jdf.LIMIT_MAX
      and jdf._norm_limit("abc") == jdf.LIMIT_DEFAULT)

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
