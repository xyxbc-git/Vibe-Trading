#!/usr/bin/env python3
"""FVG 叠加层端点冒烟（R8）：created_ts 映射 + 路由注册 + 检测器联通。

不联网：合成 K 线直调 jarvis_fvg.detect 与 _fvg_zones_with_ts；
路由注册用 app.routes 静态核对（不起服务、不取真数）。
"""

from __future__ import annotations

import pandas as pd

import jarvis_dashboard as jd
import jarvis_fvg as jfvg

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def synth_df_with_gap(n: int = 120) -> pd.DataFrame:
    """合成含明显看涨 FVG 的序列：中段强力跳涨两根，使 bar[i].low ≫ bar[i-2].high。"""
    t0 = 1_700_000_000_000
    rows = []
    px = 100.0
    for i in range(n):
        if i == 59:
            px = 120.0  # 中间 bar 强拉（大实体）
        elif i == 60:
            px = 141.0  # 第三根低点远高于第一根高点 → 看涨 FVG
        o = px
        c = px + 0.5
        rows.append({"time": t0 + i * 900_000, "open": o, "high": c + 1.0,
                     "low": o - 1.0, "close": c, "volume": 10.0})
        px = c
    return pd.DataFrame(rows)


# ── 1) 检测器联通（只读复用，契约字段齐全）─────────────────────────
df = synth_df_with_gap()
out = jfvg.detect(df, max_zones=10)
check("detect ok", out.get("ok") is True, str(out.get("reason")))
zones = out.get("zones") or []
check("检出至少一个 zone", len(zones) >= 1, f"n={len(zones)}")
need_keys = {"type", "top", "bottom", "created_i", "mitigated", "fill_pct", "age_bars"}
check("zone 契约字段齐全", bool(zones) and need_keys <= set(zones[0].keys()))

# ── 2) created_ts / first_touch_ts 映射（端点核心逻辑）─────────────
mapped = jd._fvg_zones_with_ts(df, zones)
check("每个 zone 附 created_ts", all("created_ts" in z for z in mapped))
ok_ts = all(
    z["created_ts"] == int(df["time"].iloc[int(z["created_i"])])
    for z in mapped
    if z["created_ts"] is not None
)
check("created_ts 与 created_i 对齐", ok_ts)
check("每个 zone 附 first_touch_ts 键", all("first_touch_ts" in z for z in mapped))
# 合成序列缺口形成后价格持续上行不回踩 → 全部未触碰（None，前端延伸右缘）
check("未回踩缺口 first_touch_ts=None", all(z["first_touch_ts"] is None for z in mapped))
# 构造被回踩场景：末段价格跌回缺口区内 → first_touch_ts 指向首根触及 bar
df_touch = df.copy()
df_touch.loc[len(df_touch) - 5, "low"] = float(zones[0]["bottom"]) + 0.01
touched = jd._fvg_zones_with_ts(df_touch, zones)
z0 = touched[0]
check("回踩后 first_touch_ts 命中触及 bar",
      z0["first_touch_ts"] == int(df_touch["time"].iloc[len(df_touch) - 5]),
      f"got={z0['first_touch_ts']}")
bad = jd._fvg_zones_with_ts(df, [{"created_i": 9999}, {"created_i": "x"}, {}])
check("越界/异常下标 → created_ts=None 不抛", all(z["created_ts"] is None for z in bad))
check("空 zones 容错", jd._fvg_zones_with_ts(df, []) == [])

# ── 3) 路由注册 ────────────────────────────────────────────────────
paths = {getattr(r, "path", None) for r in jd.app.routes}
check("GET /api/fvg 已注册", "/api/fvg" in paths)

# ── 4) [R14补] 停更判定（数据源新鲜度分裂修复的守卫）────────────────
import time as _t

fresh = df.copy()
fresh["time"] = fresh["time"] - int(fresh["time"].iloc[-1]) + int(_t.time() * 1000) - 900_000
check("新鲜 df → 不判停更", jd._klines_df_stale(fresh, 900) is False)
stale = df.copy()  # 合成序列 t0=1.7e12（2023 年）——远古数据必判停更
check("陈旧 df → 判停更", jd._klines_df_stale(stale, 900) is True)
check("None/空 df → 判停更", jd._klines_df_stale(None, 900) is True
      and jd._klines_df_stale(df.iloc[0:0], 900) is True)

print()
if _FAILED:
    print(f"❌ {len(_FAILED)} 项失败: {_FAILED}")
    raise SystemExit(1)
print("✅ FVG 端点冒烟全部通过")
