#!/usr/bin/env python3
"""[M2 s5] 清算/止损密集区估计器离线 smoketest：不联网。

覆盖：杠杆档权重解析 / 摆动点检测 / 爆仓价近似 / 清算簇聚合方向与档位 /
止损簇（摆动+关口重合叠加）/ forceOrder 校准提升置信度 / 降级路径 /
磁吸位提醒因子 / mock 门面契约 / 配置键三处落位。
"""

from __future__ import annotations

import jarvis_liq_map as jlm

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


# ── 1. 杠杆档权重解析 ──
w = jlm.parse_leverage_weights("5:0.1,10:0.3,25:0.3,50:0.2,100:0.1")
check("权重解析 5 档且归一", len(w) == 5 and abs(sum(x[1] for x in w) - 1.0) < 1e-9,
      str(w))
w2 = jlm.parse_leverage_weights("garbage")
check("非法串回退默认 5 档", len(w2) == 5)
w3 = jlm.parse_leverage_weights("10:1,bad,25:0")   # 25:0 权重非正跳过
check("坏项跳过只留合法档", len(w3) == 1 and w3[0][0] == 10.0, str(w3))

# ── 2. 摆动点检测（fractal）──
rows = []
for i in range(30):
    h = 100.0 + (5 if i == 10 else 0) + (3 if i == 20 else 0)
    lo = 95.0 - (5 if i == 15 else 0)
    rows.append({"high": h, "low": lo})
sw = jlm.find_swings(rows, wing=3)
check("检出摆动高点 105", 105.0 in sw["highs"], str(sw["highs"]))
check("检出摆动低点 90", 90.0 in sw["lows"], str(sw["lows"]))
check("空 K 线不抛", jlm.find_swings([]) == {"highs": [], "lows": []})

# ── 3. 爆仓价近似 ──
check("10x 多头爆仓 ≈ -9.9%", abs(jlm._liq_price(100.0, 10, "long") - 90.1) < 1e-9)
check("10x 空头爆仓 ≈ +9.9%", abs(jlm._liq_price(100.0, 10, "short") - 109.9) < 1e-9)

# ── 4. estimate：清算簇方向 + 分类输出 ──
price = 60000.0
vp = [{"poc": 60500.0, "volumeShare": 0.5}, {"poc": 59500.0, "volumeShare": 0.5}]
cfg = {"liq_leverage_weights": "25:0.5,50:0.5"}
out = jlm.estimate(price, 1e9, vp, None, cfg, None)
check("estimate ok", out["ok"])
liq = out["liq_clusters"]
check("清算簇有输出", len(liq) > 0, str(len(liq)))
check("多头清算簇在现价下方", all(m["side"] == "below" for m in liq
                                  if m["kind"] == "long_liq"), str(liq))
check("空头清算簇在现价上方", all(m["side"] == "above" for m in liq
                                  if m["kind"] == "short_liq"))
check("上下各 ≤3 个", sum(1 for m in liq if m["side"] == "above") <= 3
      and sum(1 for m in liq if m["side"] == "below") <= 3)
check("强度组内归一 max=1", abs(max(m["strength"] for m in liq) - 1.0) < 1e-9)
check("未校准置信度 0.5", all(m["confidence"] == 0.5 for m in liq))
# 25x 多头爆仓 = 60500×(1-0.0396)=58104.2 应落在某个 below 簇内
check("25x 爆仓价落簇", any(m["price_low"] <= 58104.2 <= m["price_high"] + 1
                            for m in liq if m["side"] == "below"),
      str([(m["price_low"], m["price_high"]) for m in liq if m["side"] == "below"]))

# ── 5. 止损簇：摆动点 + 整数关口重合叠加 ──
sw2 = {"highs": [61000.0], "lows": [59000.0]}   # 59000/61000 恰为整数关口 → 叠加
out2 = jlm.estimate(price, None, [], sw2, {}, None)
stops = out2["stop_clusters"]
check("止损簇有输出", len(stops) > 0)
top = stops[0]
check("重合叠加簇强度居首", top["strength"] == 1.0
      and (58900 <= top["price_mid"] <= 59100 or 60900 <= top["price_mid"] <= 61100),
      str(top))
check("无 VP 时无清算簇", len(out2["liq_clusters"]) == 0)

# ── 6. forceOrder 校准 ──
fo_hits = [{"avg_price": 58104.0}] * 8    # 全落在 25x 多头簇
out3 = jlm.estimate(price, 1e9, vp, None, cfg, fo_hits)
check("校准生效 hit_ratio=1", out3["calibration"] is not None
      and out3["calibration"]["hit_ratio"] == 1.0, str(out3["calibration"]))
check("置信度提升到 0.9", all(abs(m["confidence"] - 0.9) < 1e-9
                              for m in out3["liq_clusters"]))
fo_miss = [{"avg_price": 40000.0}] * 8    # 远离所有簇（距现价 33% 被过滤）
out4 = jlm.estimate(price, 1e9, vp, None, cfg, fo_miss)
check("全脱靶 hit_ratio=0 置信度回落 0.5",
      out4["calibration"]["hit_ratio"] == 0.0
      and all(m["confidence"] == 0.5 for m in out4["liq_clusters"]),
      str(out4["calibration"]))
out5 = jlm.estimate(price, 1e9, vp, None, cfg, [{"avg_price": 58104.0}] * 3)
check("样本<5 不校准", out5["calibration"] is None)

# ── 7. 降级路径 ──
check("现价非法 ok=False", jlm.estimate(0, None, vp, None, {}, None)["ok"] is False)
empty = jlm.estimate(price, None, [], {"highs": [], "lows": []}, {}, None)
check("无上游仍 ok（关口簇兜底）", empty["ok"] is True)

# ── 8. 磁吸位提醒因子 ──
near = jlm.magnet_factor(
    {"magnets": [{"kind": "long_liq", "side": "below", "price_mid": 59500.0,
                  "strength": 0.9, "confidence": 0.5, "dist_pct": -0.83,
                  "label": "多头清算簇 59500（-0.83%）"}]}, warn_pct=1.5)
check("1.5% 内强磁吸位触发提醒", near["near"] is True and "插针" in near["note"],
      near["note"])
far = jlm.magnet_factor(
    {"magnets": [{"kind": "long_liq", "side": "below", "price_mid": 57000.0,
                  "strength": 0.9, "confidence": 0.5, "dist_pct": -5.0,
                  "label": "x"}]}, warn_pct=1.5)
check("5% 外不提醒", far["near"] is False)
weak = jlm.magnet_factor(
    {"magnets": [{"kind": "stop_cluster", "side": "above", "price_mid": 60100.0,
                  "strength": 0.3, "confidence": 0.5, "dist_pct": 0.17,
                  "label": "x"}]}, warn_pct=1.5)
check("弱簇(强度<0.5)不提醒", weak["near"] is False)
check("空 magnets 不抛", jlm.magnet_factor({"magnets": []}, 1.5)["near"] is False)
check("None 不抛", jlm.magnet_factor(None, 1.5)["near"] is False)

# ── 9. mock 门面契约 ──
mk = jlm.mock_assess("BTCUSDT")
check("mock 契约键齐全", all(k in mk for k in
      ("ok", "symbol", "magnets", "liq_clusters", "stop_clusters",
       "calibration", "updatedAt", "disclaimer")), str(list(mk.keys())))
check("mock ok 且有磁吸位", mk["ok"] and len(mk["magnets"]) > 0)

# ── 10. 配置键三处落位 ──
import jarvis_config as jc
check("配置键默认值", jc.DEFAULTS["liq_magnet_warn_pct"] == 1.5
      and jc.DEFAULTS["liq_map_seatbelt_enabled"] is True
      and "5:0.1" in jc.DEFAULTS["liq_leverage_weights"])
check("分组落位 signal", all(jc.GROUPS.get(k) == "signal" for k in
      ("liq_leverage_weights", "liq_magnet_warn_pct", "liq_map_seatbelt_enabled")))
check("warn_pct 夹护栏", jc.clamp("liq_magnet_warn_pct", 99) == 10.0
      and jc.clamp("liq_magnet_warn_pct", 0) == 0.1)

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
