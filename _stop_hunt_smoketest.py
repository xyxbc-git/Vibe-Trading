"""离线冒烟：止损扫单检测（detect_stop_hunt）+ 四条件叠加评分（aggregate_reversal_score）。

全部合成数据，不联网不碰库。
"""
import jarvis_stop_hunt as jsh

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def bars_flat(n, price=100.0, vol=100.0):
    """横盘基底：小实体小波动。"""
    out = []
    for i in range(n):
        o = price + (0.2 if i % 2 == 0 else -0.2)
        c = price - (0.2 if i % 2 == 0 else -0.2)
        out.append({"ts": 1_700_000_000_000 + i * 900_000,
                    "o": o, "h": max(o, c) + 0.5, "l": min(o, c) - 0.5,
                    "c": c, "v": vol})
    return out


# ── 1. 看涨扫单：长下影刺破前低快速收回 + 放量 ──
bars = bars_flat(30)
# 前低 ≈ 99.3；ATR ≈ 1.4 量级。扫单 bar：刺破 0.8，收盘收回，下影长，量 3 倍
bars.append({"ts": 1_700_000_000_000 + 30 * 900_000,
             "o": 100.0, "h": 100.4, "l": 98.5, "c": 100.2, "v": 300.0})
r = jsh.detect_stop_hunt(bars)
check("看涨扫单命中", r["detected"] and r["side"] == "long-stops-swept", str(r["side"]))
check("sweptLevel=前低", r["sweptLevel"] is not None and abs(r["sweptLevel"] - 99.3) < 0.01,
      str(r["sweptLevel"]))
check("wickRatio 达标", (r["wickRatio"] or 0) >= 2.0, str(r["wickRatio"]))
check("volumeSpike 达标", (r["volumeSpike"] or 0) >= 1.5, str(r["volumeSpike"]))

# ── 2. 看跌扫单（镜像）：长上影刺破前高收回 ──
bars2 = bars_flat(30)
bars2.append({"ts": 1_700_000_000_000 + 30 * 900_000,
              "o": 100.0, "h": 101.5, "l": 99.6, "c": 99.8, "v": 300.0})
r2 = jsh.detect_stop_hunt(bars2)
check("看跌扫单命中", r2["detected"] and r2["side"] == "short-stops-swept", str(r2["side"]))

# ── 3. 反例：刺破过深（> ATR 倍数）= 趋势崩塌，不是扫单 ──
bars3 = bars_flat(30)
bars3.append({"ts": 1_700_000_000_000 + 30 * 900_000,
              "o": 100.0, "h": 100.4, "l": 92.0, "c": 100.2, "v": 300.0})
r3 = jsh.detect_stop_hunt(bars3)
check("刺破过深不算扫单", not r3["detected"], str(r3["side"]))

# ── 4. 反例：无量能尖峰不算 ──
bars4 = bars_flat(30)
bars4.append({"ts": 1_700_000_000_000 + 30 * 900_000,
              "o": 100.0, "h": 100.4, "l": 98.5, "c": 100.2, "v": 110.0})
r4 = jsh.detect_stop_hunt(bars4)
check("无量不算扫单", not r4["detected"], str(r4["side"]))

# ── 5. 反例：收盘未收回破位上方（真跌破）──
bars5 = bars_flat(30)
bars5.append({"ts": 1_700_000_000_000 + 30 * 900_000,
              "o": 100.0, "h": 100.2, "l": 98.5, "c": 99.0, "v": 300.0})
r5 = jsh.detect_stop_hunt(bars5)
check("未收回不算扫单", not r5["detected"], str(r5["side"]))

# ── 6. 数据不足降级 ──
r6 = jsh.detect_stop_hunt(bars_flat(5))
check("数据不足不误报", not r6["detected"] and "不足" in r6["note"], r6["note"][:30])

# ── 7. 聚合：全链 mock 三源 → 4/4 高概率 ──
import jarvis_delta_flow as jdf
import jarvis_volume_profile as jvp

agg = jsh.aggregate_reversal_score(
    jdf.mock_analyze("BTCUSDT", "15m"),
    jvp.mock_response("BTCUSDT", "15m"),
    jsh.mock_result("bullish"),
)
check("mock 聚合方向 bullish", agg["direction"] == "bullish", agg["direction"])
check("mock 聚合 4/4", agg["satisfied"] == 4 and agg["verdict"] == "high-probability",
      f"{agg['satisfied']}/{agg['maxScore']} {agg['verdict']}")
check("契约字段齐备", all(k in agg for k in
      ("ok", "direction", "conditions", "satisfied", "maxScore", "verdict",
       "note", "updatedAt", "disclaimer")))
check("四条件键齐备", set(agg["conditions"].keys()) ==
      {"delta_divergence", "multi_distribution", "triple_confirm", "stop_hunt"})

# ── 8. 聚合：上游未就绪 → unavailable 降级不计分 ──
agg2 = jsh.aggregate_reversal_score(None, None, jsh.mock_result("bullish"))
check("delta/vp 未就绪标 unavailable",
      agg2["conditions"]["delta_divergence"].get("unavailable")
      and agg2["conditions"]["multi_distribution"].get("unavailable")
      and agg2["conditions"]["triple_confirm"].get("unavailable"))
check("未就绪不计入 met", agg2["satisfied"] == 1, str(agg2["satisfied"]))
check("扫单单独成立方向 bullish", agg2["direction"] == "bullish", agg2["direction"])
check("1/4 verdict=no-signal", agg2["verdict"] == "no-signal", agg2["verdict"])

# ── 9. 聚合：方向冲突（看涨背离 + 扫空头止损）→ 扫单不计入 ──
agg3 = jsh.aggregate_reversal_score(
    jdf.mock_analyze("BTCUSDT", "15m"),      # bullish divergence active
    None,
    jsh.mock_result("bearish"),               # short-stops-swept
)
check("方向冲突时扫单不计入",
      not agg3["conditions"]["stop_hunt"]["met"]
      and "不一致" in agg3["conditions"]["stop_hunt"]["note"],
      agg3["conditions"]["stop_hunt"]["note"][:40])

# ── 10. 全空 → no-signal / none ──
agg4 = jsh.aggregate_reversal_score(None, None, None)
check("全空 direction=none", agg4["direction"] == "none", agg4["direction"])
check("全空 0/4 no-signal", agg4["satisfied"] == 0 and agg4["verdict"] == "no-signal")

print("---")
print("ALL PASS" if not fails else f"FAILED: {fails}")
raise SystemExit(0 if not fails else 1)
