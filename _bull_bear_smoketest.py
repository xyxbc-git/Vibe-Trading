"""牛熊体制识别引擎冒烟测试。纯函数合成数据，不联网不碰真实库。"""
from __future__ import annotations

import jarvis_bull_bear as jbb

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def mk_daily(n: int, start: float, step: float) -> list[dict]:
    """等差走势日线：start 起每根 +step。high/low 留 0.5% 边。"""
    rows = []
    p = start
    for i in range(n):
        rows.append({"time": i, "open": p, "high": p * 1.005,
                     "low": p * 0.995, "close": p, "volume": 1.0})
        p += step
    return rows


def mk_weekly_trend(up: bool) -> list[dict]:
    """锯齿趋势周线：涨3跌2（或反向）的波浪，峰谷逐周期抬升/降低，
    保证 wing=2 分形能确认出 HH/HL（或 LH/LL）摆动点。"""
    pattern = [4.0, 4.0, 4.0, -2.0, -2.0] if up else [-4.0, -4.0, -4.0, 2.0, 2.0]
    base = 100.0 if up else 300.0
    rows = []
    for i in range(30):
        base += pattern[i % 5]
        rows.append({"time": i, "open": base - 1, "high": base + 2,
                     "low": base - 2, "close": base, "volume": 1.0})
    return rows


# ── 1. ma200：多头形态 ──
up_daily = mk_daily(400, 100.0, 0.5)  # 单调上涨，现价远在 MA200 上方且斜率向上
f = jbb.score_ma200(up_daily)
check("ma200 多头形态偏牛", f["available"] and f["score"] > 30 and f["bias"] == "bullish",
      f"score={f['score']}")

down_daily = mk_daily(400, 300.0, -0.5)
f = jbb.score_ma200(down_daily)
check("ma200 空头形态偏熊", f["available"] and f["score"] < -30 and f["bias"] == "bearish",
      f"score={f['score']}")

f = jbb.score_ma200(mk_daily(100, 100, 0.1))
check("ma200 数据不足降级", not f["available"] and f["score"] == 0.0)

# ── 2. 周线结构 ──
f = jbb.score_weekly_structure(mk_weekly_trend(up=True))
check("周线 HH/HL 判上升结构", f["available"] and f["score"] > 30, f"score={f['score']}")

f = jbb.score_weekly_structure(mk_weekly_trend(up=False))
check("周线 LH/LL 判下降结构", f["available"] and f["score"] < -30, f"score={f['score']}")

f = jbb.score_weekly_structure(None)
check("周线缺数据降级", not f["available"])

# ── 3. 动量 ──
f = jbb.score_momentum(up_daily)
check("动量：上涨走势偏牛", f["available"] and f["score"] > 15, f"score={f['score']}")

f = jbb.score_momentum(down_daily)
check("动量：下跌走势偏熊", f["available"] and f["score"] < -15, f"score={f['score']}")

f = jbb.score_momentum(mk_daily(50, 100, 0.1))
check("动量数据不足降级", not f["available"])

# ── 4. 情绪（长周期同步口径） ──
f = jbb.score_sentiment_regime({"value": 72, "classification": "Greed"}, {"long_pct": 58.0})
check("恐贪贪婪=偏牛温度", f["available"] and f["score"] > 20, f"score={f['score']}")

f = jbb.score_sentiment_regime({"value": 22, "classification": "Fear"}, None)
check("恐贪恐惧=偏熊温度", f["available"] and f["score"] < -20, f"score={f['score']}")

f = jbb.score_sentiment_regime({"value": 85, "classification": "Extreme Greed"}, None)
check("极度贪婪打折仍偏牛", f["available"] and 0 < f["score"] < 30, f"score={f['score']}")

f = jbb.score_sentiment_regime(None, {"long_pct": 60.0})
check("无恐贪数据降级", not f["available"])

# ── 5. 链上预留位 ──
f = jbb.score_onchain_valuation(None)
check("链上未接入 available=False", not f["available"] and "Glassnode" in f["note"])

f = jbb.score_onchain_valuation({"value": 3.5})
check("MVRV 3.5 高估偏熊", f["available"] and f["score"] < -30, f"score={f['score']}")

f = jbb.score_onchain_valuation({"value": 0.8})
check("MVRV 0.8 低估偏牛", f["available"] and f["score"] > 30, f"score={f['score']}")

# ── 6. 综合判定 ──
out = jbb.build_assessment(up_daily, mk_weekly_trend(True),
                           {"fng": {"value": 70, "classification": "Greed"},
                            "long_short": {"long_pct": 56.0}}, "BTCUSDT")
check("全多头因子 → bull", out["regime"] == "bull",
      f"regime={out['regime']} score={out['score']}")
check("bull 分数 ≥ 阈值", out["score"] >= jbb.REGIME_TH)
check("置信度在 (0,0.95]", 0 < out["confidence"] <= 0.95, f"conf={out['confidence']}")
check("headline 含判定", "牛市" in out["headline"], out["headline"])
check("免责声明存在", bool(out["disclaimer"]))
check("因子含 5 项", len(out["factors"]) == 5)

out = jbb.build_assessment(down_daily, mk_weekly_trend(False),
                           {"fng": {"value": 20, "classification": "Extreme Fear"}},
                           "BTCUSDT")
check("全空头因子 → bear", out["regime"] == "bear",
      f"regime={out['regime']} score={out['score']}")

flat_daily = mk_daily(400, 100.0, 0.001)
out = jbb.build_assessment(flat_daily, None,
                           {"fng": {"value": 50, "classification": "Neutral"}}, "BTC")
check("横盘+中性情绪 → range", out["regime"] == "range",
      f"regime={out['regime']} score={out['score']}")
check("symbol 自动补 USDT", out["symbol"] == "BTCUSDT")

out = jbb.build_assessment(None, None, {}, "BTCUSDT")
check("全缺数据 → range + 低置信", out["regime"] == "range" and out["confidence"] == 0.0,
      f"conf={out['confidence']}")

print()
if fails:
    print(f"FAILED: {len(fails)} 项 → {fails}")
    raise SystemExit(1)
print("牛熊体制识别冒烟测试全部通过 ✅")
