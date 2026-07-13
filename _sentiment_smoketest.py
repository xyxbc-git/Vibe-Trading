"""离线冒烟：供需情绪因子引擎（纯函数，不联网）——因子评分 / 综合分 / 共识融合。"""
import jarvis_sentiment as js

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ─── 1. 多空比因子 ───
f = js.score_long_short({"long_pct": 57.1, "short_pct": 42.9, "ratio": 1.33})
check("多空比 57.1% 温和看多", f["available"] and f["bias"] == "bullish" and 0 < f["score"] <= 30,
      f"score={f['score']}")
f = js.score_long_short({"long_pct": 70.0, "short_pct": 30.0, "ratio": 2.33})
check("多空比 70% 极端挤多→反向看空", f["bias"] == "bearish" and f["score"] <= -35,
      f"score={f['score']}")
f = js.score_long_short({"long_pct": 30.0, "short_pct": 70.0, "ratio": 0.43})
check("多空比 30% 极端挤空→反向看多", f["bias"] == "bullish" and f["score"] >= 35,
      f"score={f['score']}")
f = js.score_long_short(None)
check("多空比缺数据 → 不可用不计分", not f["available"] and f["score"] == 0.0)

# ─── 1.5 大户背离因子（T1.6，阈值显式传参锁定口径） ───
f = js.score_top_divergence({"long_pct": 42.0, "top_long_pct": 58.5}, 0.15)
check("大户58.5%多 vs 全网42%多 反向+差16.5pp → 跟大户看多",
      f["available"] and f["bias"] == "bullish" and f["score"] >= 35, f"score={f['score']}")
f = js.score_top_divergence({"long_pct": 58.0, "top_long_pct": 41.0}, 0.15)
check("大户41%多 vs 全网58%多 反向+差17pp → 跟大户看空",
      f["bias"] == "bearish" and f["score"] <= -35, f"score={f['score']}")
f = js.score_top_divergence({"long_pct": 55.0, "top_long_pct": 62.0}, 0.15)
check("同向（均偏多）→ 不触发 0 分", f["available"] and f["score"] == 0.0, f"score={f['score']}")
f = js.score_top_divergence({"long_pct": 48.0, "top_long_pct": 53.0}, 0.15)
check("反向但差 5pp 未达阈值 → 不触发 0 分", f["available"] and f["score"] == 0.0,
      f"score={f['score']}")
f = js.score_top_divergence({"long_pct": 48.0, "top_long_pct": 53.0}, 0.04)
check("同数据阈值降到 4pp → 触发（配置可调生效）", f["score"] >= 35, f"score={f['score']}")
f = js.score_top_divergence({"long_pct": 57.1}, 0.15)
check("缺大户数据 → 不可用不计分", not f["available"] and f["score"] == 0.0)
f = js.score_top_divergence(None, 0.15)
check("多空比整体缺失 → 不可用", not f["available"])

# ─── 2. 资金费率因子 ───
f = js.score_funding({"BTCUSDT": 0.0001}, "BTCUSDT")
check("费率 0.01% 中性/温和", f["available"] and abs(f["score"]) <= 10, f"score={f['score']}")
f = js.score_funding({"BTCUSDT": 0.0008}, "BTCUSDT")
check("费率 0.08% 极高→看空", f["bias"] == "bearish" and f["score"] <= -40, f"score={f['score']}")
f = js.score_funding({"BTCUSDT": -0.0005}, "BTCUSDT")
check("费率 -0.05% 深负→轧空看多", f["bias"] == "bullish" and f["score"] >= 40, f"score={f['score']}")
f = js.score_funding({"ETHUSDT": 0.0002}, "BTCUSDT")
check("目标币缺失退化为均值", f["available"], f"score={f['score']}")

# ─── 3. OI × 价格交叉因子 ───
f = js.score_oi({"value": 6.45e9, "change_pct": 3.0}, {"change_pct": 2.0})
check("价涨+OI增 → 趋势健康看多", f["bias"] == "bullish" and f["score"] >= 35, f"score={f['score']}")
f = js.score_oi({"value": 6.45e9, "change_pct": -3.0}, {"change_pct": 2.0})
check("价涨+OI降 → 软弱反弹看空", f["bias"] == "bearish", f"score={f['score']}")
f = js.score_oi({"value": 6.45e9, "change_pct": 3.0}, {"change_pct": -2.0})
check("价跌+OI增 → 下跌健康看空", f["bias"] == "bearish" and f["score"] <= -35, f"score={f['score']}")
f = js.score_oi({"value": 6.45e9, "change_pct": -3.0}, {"change_pct": -2.0})
check("价跌+OI降 → 抛压衰竭轻看多", f["score"] > 0, f"score={f['score']}")
f = js.score_oi({"value": 6.45e9, "change_pct": 0.5}, {"change_pct": 0.1})
check("变化不显著 → 中性", f["bias"] == "neutral" and f["score"] == 0.0)
f = js.score_oi({"value": 6.45e9, "change_pct": -3.0}, None)
check("缺价格方向 → 可用但 0 分", f["available"] and f["score"] == 0.0)

# ─── 4. 恐贪因子（逆向） ───
f = js.score_fng({"value": 15, "classification": "Extreme Fear"})
check("极度恐惧 15 → 逆向看多", f["bias"] == "bullish" and f["score"] >= 40, f"score={f['score']}")
f = js.score_fng({"value": 26, "classification": "Fear"})
check("恐惧 26 → 轻度看多", f["bias"] == "bullish" and 0 < f["score"] < 40, f"score={f['score']}")
f = js.score_fng({"value": 88, "classification": "Extreme Greed"})
check("极度贪婪 88 → 逆向看空", f["bias"] == "bearish" and f["score"] <= -40, f"score={f['score']}")

# ─── 5. 预留因子接口位 ───
f = js.score_liquidations(None)
check("爆仓未接入 → 占位不计分", not f["available"] and "Coinglass" in f["note"])
f = js.score_liquidations({"long_usd": 300e6, "short_usd": 50e6})
check("多头爆仓主导 → 杠杆出清偏多", f["available"] and f["score"] > 0, f"score={f['score']}")
f = js.score_onchain(None)
check("链上未接入 → 占位不计分", not f["available"] and "Glassnode" in f["note"])
f = js.score_onchain({"exchange_netflow_usd": 150e6})
check("净流入交易所 → 抛压偏空", f["available"] and f["score"] < 0, f"score={f['score']}")

# ─── 6. 综合研判 build_factors ───
intel = {
    "long_short": {"long_pct": 57.1, "short_pct": 42.9, "ratio": 1.33},
    "funding_rate": {"BTCUSDT": 0.0001, "ETHUSDT": 0.00008},
    "oi": {"value": 6.45e9, "change_pct": -3.0},
    "price_24h": {"change_pct": 1.2},
    "fng": {"value": 26, "classification": "Fear"},
    "liquidations": None, "onchain": None,
}
s = js.build_factors(intel, "BTCUSDT", divergence_threshold=0.15)
check("综合分在 [-100,100]", -100 <= s["score"] <= 100, f"score={s['score']}")
check("七因子齐全（含 2 预留 + 大户背离）", len(s["factors"]) == 7, f"n={len(s['factors'])}")
check("预留因子不计分", all(x["score"] == 0.0 for x in s["factors"] if not x["available"]))
check("headline 非空", bool(s["headline"]), s["headline"])
check("无大户数据 → top_divergence 摘要不可用",
      s["top_divergence"]["available"] is False and s["top_divergence"]["active"] is False)

# 大户背离摘要块（前端小卡口径）：反向+超阈值 → active + 建议倾向跟大户
intel_div = {**intel, "long_short": {"long_pct": 42.0, "short_pct": 58.0, "ratio": 0.72,
                                     "top_long_pct": 58.5, "top_short_pct": 41.5,
                                     "top_ratio": 1.41}}
s_div = js.build_factors(intel_div, "BTCUSDT", divergence_threshold=0.15)
td = s_div["top_divergence"]
check("背离激活：active + 大户偏多 + 散户偏空",
      td["active"] and td["top_bias"] == "bullish" and td["retail_bias"] == "bearish",
      f"td={td['top_bias']}/{td['retail_bias']}")
check("背离摘要含差值与阈值（pp）", td["diff_pp"] == 16.5 and td["threshold_pp"] == 15.0,
      f"diff={td['diff_pp']} thr={td['threshold_pp']}")
check("背离建议倾向跟随大户", "跟随大户" in td["suggestion"], td["suggestion"][:30])

# 全极端拥挤情景（价涨+OI 降=软弱反弹，四因子齐看空）：应触发警示 + 止盈止损收紧建议
intel_hot = {
    "long_short": {"long_pct": 71.0, "short_pct": 29.0, "ratio": 2.45},
    "funding_rate": {"BTCUSDT": 0.0009},
    "oi": {"value": 6.45e9, "change_pct": -4.0},
    "price_24h": {"change_pct": 3.0},
    "fng": {"value": 85, "classification": "Extreme Greed"},
}
s_hot = js.build_factors(intel_hot, "BTCUSDT")
check("极端拥挤 → 有警示", len(s_hot["warnings"]) >= 2, f"warnings={len(s_hot['warnings'])}")
check("极端拥挤 → 有 SLTP 收紧建议", bool(s_hot["sl_tp_advice"]), str(s_hot["sl_tp_advice"])[:40])

# ─── 7. 与技术面共识融合 apply_to_consensus ───
cons_bull = {"direction": "bullish", "confidence": 0.6, "reasoning": "多周期共识看涨"}

s_pos = js.build_factors({**intel, "fng": {"value": 15, "classification": "Extreme Fear"},
                          "oi": {"value": 6.45e9, "change_pct": 3.0}}, "BTCUSDT")
merged = js.apply_to_consensus(cons_bull, s_pos)
check("同向共振 → 置信度增益", merged["sentiment"]["alignment"] == "aligned"
      and merged["sentiment"]["confidence_delta"] > 0
      and merged["sentiment"]["adjusted_confidence"] > 0.6,
      f"delta={merged['sentiment']['confidence_delta']}")
check("原 confidence 不被改写", merged["confidence"] == 0.6)
check("reasoning 追加情绪尾注", "情绪面" in merged["reasoning"])

s_neg = js.build_factors(intel_hot, "BTCUSDT")
merged2 = js.apply_to_consensus(cons_bull, s_neg)
check("技术看多×情绪极端拥挤 → 背离降级", merged2["sentiment"]["alignment"] == "divergent"
      and merged2["sentiment"]["confidence_delta"] < 0,
      f"delta={merged2['sentiment']['confidence_delta']}")
check("背离 → 首条警示为执行提醒", any("背离" in w or "轻仓" in w
                                       for w in merged2["sentiment"]["warnings"]),
      str(merged2["sentiment"]["warnings"][:1]))

neutral_cons = {"direction": "neutral", "confidence": 0.3, "reasoning": "中性"}
merged3 = js.apply_to_consensus(neutral_cons, s_neg)
check("技术中性 → 情绪不修正", merged3["sentiment"]["alignment"] == "neutral"
      and merged3["sentiment"]["confidence_delta"] == 0.0)

print("\n=== " + ("全部通过" if not fails else f"失败 {len(fails)}: {fails}") + " ===")
raise SystemExit(1 if fails else 0)
