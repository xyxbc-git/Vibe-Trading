#!/usr/bin/env python3
"""FVG 检测 + swing 支撑压力（jarvis_fvg）离线 smoketest：全手工构造 K 线，不联网。

覆盖：看涨/看跌检测、部分/完全回补、噪声门槛、未回补优先+新鲜度排序、
max_zones 截断、聚合契约 detect() 字段、支撑压力聚簇与距离标注、
数据不足/坏输入降级、幂等。
"""

from __future__ import annotations

import pandas as pd

import jarvis_fvg as jf

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


def mk(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c, "volume": 1000.0}
                         for o, h, l, c in bars])


FLAT = (100.0, 100.5, 99.5, 100.0)

# ── A. 看涨 FVG：大阳拉升留缺口 [100.5, 103]，后续不回踩 ─────────────────
bars_a = [FLAT] * 40
bars_a += [(100.0, 106.5, 99.8, 106.0),      # bar40 大阳
           (106.0, 107.0, 103.0, 106.5)]     # bar41：low=103 > bar39.high=100.5
bars_a += [(106.5, 107.5, 104.0, 106.8)] * 8
df_a = mk(bars_a)
za = jf.detect_fvg(df_a)
check("A1 看涨 FVG 检出且唯一", len(za) == 1 and za[0]["type"] == "bullish",
      str(za)[:120])
check("A2 缺口边界 [100.5, 103]", za and abs(za[0]["bottom"] - 100.5) < 1e-9
      and abs(za[0]["top"] - 103.0) < 1e-9, str(za and za[0]))
check("A3 未回踩：fill=0 未回补", za and za[0]["fill_pct"] == 0.0
      and za[0]["mitigated"] is False, str(za and za[0]))
check("A4 created_i/age_bars 正确", za and za[0]["created_i"] == 41
      and za[0]["age_bars"] == len(df_a) - 1 - 41, str(za and za[0]))
check("A5 距离标注 ATR 归一存在且非负", za and za[0]["dist_atr"] is not None
      and za[0]["dist_atr"] >= 0 and za[0]["dist_pct"] >= 0, str(za and za[0]))

# ── B. 部分回补：回踩到 102（缺口高 2.5，穿透 1.0 → 40%）────────────────
df_b = mk(bars_a + [(106.8, 107.0, 102.0, 105.0)])
zb = jf.detect_fvg(df_b)
check("B1 部分回补 fill_pct=40 且未失效", zb and abs(zb[0]["fill_pct"] - 40.0) < 0.11
      and zb[0]["mitigated"] is False, str(zb and zb[0]))

# ── C. 完全回补：跌破缺口下沿 100.5 → mitigated ─────────────────────────
df_c = mk(bars_a + [(106.8, 107.0, 100.3, 105.0)])
zc = jf.detect_fvg(df_c)
check("C1 完全回补 fill=100 且 mitigated", zc and zc[0]["fill_pct"] == 100.0
      and zc[0]["mitigated"] is True, str(zc and zc[0]))

# ── D. 看跌 FVG 镜像：大阴杀跌留缺口 [97, 99.5] ─────────────────────────
bars_d = [FLAT] * 40
bars_d += [(100.0, 100.2, 93.5, 94.0),
           (94.0, 97.0, 93.0, 94.5)]         # bar41：high=97 < bar39.low=99.5
bars_d += [(94.5, 96.0, 93.5, 94.8)] * 8
zd = jf.detect_fvg(mk(bars_d))
check("D1 看跌 FVG 检出 [97, 99.5]", zd and zd[0]["type"] == "bearish"
      and abs(zd[0]["bottom"] - 97.0) < 1e-9 and abs(zd[0]["top"] - 99.5) < 1e-9,
      str(zd and zd[0]))
check("D2 看跌未回补（价格未反弹进缺口）", zd and zd[0]["fill_pct"] == 0.0
      and zd[0]["mitigated"] is False, str(zd and zd[0]))

# ── E. 噪声门槛：缺口 0.05 < 0.1×ATR(≈0.95) → 不计 ──────────────────────
bars_e = [FLAT] * 40
bars_e += [(100.0, 100.8, 99.9, 100.7),
           (100.7, 100.9, 100.55, 100.8)]    # 缺口 [100.5, 100.55] 高 0.05
bars_e += [(100.8, 101.0, 100.6, 100.9)] * 5
check("E1 噪声缺口被门槛过滤", jf.detect_fvg(mk(bars_e)) == [],
      str(jf.detect_fvg(mk(bars_e)))[:100])

# ── F. 排序与截断：三段阶梯上行造 3 个缺口，gap1 已回补 ─────────────────
bars_f = [FLAT] * 35
bars_f += [(100.0, 106.5, 99.8, 106.0), (106.0, 107.0, 103.0, 106.5)]  # gap1 i=36
bars_f += [(106.5, 107.0, 104.0, 106.5)] * 4                            # 37-40
bars_f += [(106.0, 106.5, 100.2, 106.0)]                                # 41 回补 gap1
bars_f += [(106.0, 107.0, 104.0, 106.5)] * 3                            # 42-44
bars_f += [(106.0, 113.0, 105.8, 112.5), (112.5, 113.5, 109.5, 113.0)]  # gap2 i=46
bars_f += [(113.0, 114.0, 110.0, 113.5)] * 3                            # 47-49
bars_f += [(113.0, 120.0, 112.8, 119.5), (119.5, 120.5, 116.5, 120.0)]  # gap3 i=51
bars_f += [(120.0, 121.0, 117.0, 120.0)] * 2                            # 52-53
df_f = mk(bars_f)
zf = jf.detect_fvg(df_f)
check("F1 三个缺口全检出", len(zf) == 3, f"got {len(zf)}: " + str(zf)[:160])
check("F2 未回补优先+新鲜优先（gap3→gap2→已回补gap1）",
      len(zf) == 3 and [z["created_i"] for z in zf] == [51, 46, 36]
      and [z["mitigated"] for z in zf] == [False, False, True],
      str([(z["created_i"], z["mitigated"]) for z in zf]))
check("F3 max_zones 截断", len(jf.detect_fvg(df_f, max_zones=2)) == 2)
check("F4 zone 不变量 top>bottom 且高度≥0.1×ATR",
      all(z["top"] > z["bottom"] and z["height_atr"] >= 0.1 for z in zf),
      str([(z["top"], z["bottom"], z["height_atr"]) for z in zf]))

# ── G. 聚合契约 detect()（agent-8 对接面）────────────────────────────────
out = jf.detect(df_f)
CONTRACT_KEYS = {"ok", "reason", "price", "atr", "zones", "sr", "nearest_fvg", "summary"}
check("G1 契约字段齐全", CONTRACT_KEYS.issubset(out.keys()),
      str(CONTRACT_KEYS - set(out.keys())))
check("G2 ok=True 且 price/atr 有值", out["ok"] is True and out["price"] == 120.0
      and out["atr"] > 0, str({k: out[k] for k in ('ok', 'price', 'atr')}))
check("G3 nearest_fvg=最近未回补缺口（gap3）", out["nearest_fvg"] is not None
      and out["nearest_fvg"]["created_i"] == 51, str(out["nearest_fvg"]))
check("G4 summary 小白话摘要非空", isinstance(out["summary"], str)
      and "FVG" in out["summary"], out["summary"][:100])
check("G5 detect 幂等", jf.detect(df_f) == out)

# ── H. swing 支撑/压力：三角波震荡盘 ─────────────────────────────────────
def _tri_close(i: int, period: int = 12, amp: float = 3.0) -> float:
    phase = i % period
    half = period // 2
    return 100.0 + amp * (phase if phase <= half else period - phase) / half


bars_h = [(c, c + 0.2, c - 0.2, c) for c in (_tri_close(i) for i in range(96))]
sr = jf.swing_levels(mk(bars_h))
check("H1 支撑/压力两侧均有产出", len(sr["supports"]) >= 1
      and len(sr["resistances"]) >= 1,
      f"sup={len(sr['supports'])} res={len(sr['resistances'])}")
check("H2 最近支撑<现价<最近压力", sr["nearest_support"] is not None
      and sr["nearest_resistance"] is not None
      and sr["nearest_support"]["price"] <= 100.5 < sr["nearest_resistance"]["price"],
      str({"s": sr["nearest_support"], "r": sr["nearest_resistance"]}))
check("H3 聚簇触碰数≥2 且距离标注齐全",
      all(lv["touches"] >= 1 and lv["dist_atr"] >= 0 and lv["dist_pct"] >= 0
          for lv in sr["supports"] + sr["resistances"])
      and any(lv["touches"] >= 2 for lv in sr["supports"] + sr["resistances"]),
      str(sr["supports"][:2] + sr["resistances"][:2]))

# ── I. 降级路径：数据不足 / 坏输入 / 缺列，均不抛出 ──────────────────────
tiny = jf.detect(mk([FLAT] * 10))
check("I1 数据不足 ok=False+原因", tiny["ok"] is False and "数据不足" in tiny["reason"]
      and tiny["zones"] == [], str(tiny)[:120])
check("I2 None 输入优雅降级", jf.detect(None)["ok"] is False)
bad = jf.detect(pd.DataFrame({"open": [1] * 40, "close": [1] * 40}))
check("I3 缺列 ok=False+原因", bad["ok"] is False and "缺少必需列" in bad["reason"],
      str(bad["reason"]))
check("I4 detect_fvg/swing_levels 坏输入返回空结构",
      jf.detect_fvg(None) == [] and jf.swing_levels(None)["supports"] == [])

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
