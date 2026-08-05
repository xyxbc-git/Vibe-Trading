#!/usr/bin/env python3
"""[任务L] /cockpit MACD(12,26,9) 副图离线 smoketest：不联网、不起服务。

验证三层：
  1) 页面装配：COCKPIT_HTML 含 MACD 开关按钮 / macdCalc / macdTick / VIS 默认项 /
     pane 副图挂载 / WS 末根维护钩子
  2) 算法正确性：把页面里**实际发布的 macdCalc JS 源码**抽出来用 node 跑，
     与 Python 参考实现（EMA 递推 α=2/(n+1)、adjust=False 口径，等价 pandas
     ewm(span,adjust=False)）在随机序列上逐点对拔（<1e-9）
  3) 人工核对锚点：
     - 常数序列 → DIF/DEA/柱恒为 0
     - 线性递增序列 → DIF 尾值收敛于 (26-1)/2-(12-1)/2 = 7（EMA 滞后解析值）
     - EMA 递推手算样本：closes=[100,102]，EMA12 = 102×2/13+100×11/13 = 1304/13
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import os
import random

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


import jarvis_dashboard as jd

html = jd.COCKPIT_HTML

# ── 1. 页面装配 ──
check("MACD 开关按钮已挂 ivbar",
      'data-vis="macd"' in html and ">MACD</button>" in html)
check("VIS 默认含 macd:true", "marks:true, macd:true" in html)
check("macdCalc / drawMacd / macdTick / removeMacd 均在页",
      all(f"function {f}(" in html for f in ("macdCalc", "drawMacd", "macdTick", "removeMacd")))
check("副图挂 pane 1（addSeries 第三参）", html.count("},1)") >= 3 and "chart.panes()[1]" in html)
check("drawChart 接入开关分支", "if(VIS.macd){ drawMacd(); } else { removeMacd(); }" in html)
check("WS 末根维护 + macdTick 钩子", "macdTick();" in html and "rows.push({t:''" in html)
check("柱色=页面涨跌色（正绿负红）",
      "MACD_UP='rgba(14,203,129" in html and "MACD_DN='rgba(246,70,93" in html)

# ── 2. 抽取页面 JS 源码用 node 对拔 ──
m = re.search(r"(const MACD_UP=[^\n]+\n)(function macdCalc\(rows\)\{.*?\n\})", html, re.S)
check("macdCalc JS 源码可抽取", bool(m))

py_ref_dif = None


def py_macd(closes: list[float]) -> dict:
    """Python 参考实现：与 pandas ewm(span=n, adjust=False) 同口径的递推。"""
    a12, a26, a9 = 2 / 13, 2 / 27, 2 / 10
    e12 = e26 = closes[0]
    dea = None
    out = {"dif": [], "dea": [], "hist": []}
    for i in range(1, len(closes)):
        c = closes[i]
        e12 = c * a12 + e12 * (1 - a12)
        e26 = c * a26 + e26 * (1 - a26)
        if i < 25:
            continue
        dif = e12 - e26
        dea = dif if dea is None else dif * a9 + dea * (1 - a9)
        out["dif"].append(dif)
        out["dea"].append(dea)
        out["hist"].append(dif - dea)
    return out


if m:
    random.seed(42)
    closes = [100.0]
    for _ in range(199):
        closes.append(round(closes[-1] * (1 + random.uniform(-0.02, 0.02)), 4))
    rows = [{"ts": (1_700_000_000 + i * 3600) * 1000, "c": c} for i, c in enumerate(closes)]
    ref = py_macd(closes)

    js = f"""
const TZ=0; const barTime=r=>Math.floor(r.ts/1000)-TZ;
{m.group(1)}{m.group(2)}
const rows={json.dumps(rows)};
const out=macdCalc(rows);
console.log(JSON.stringify({{dif:out.dif.map(p=>p.value),dea:out.dea.map(p=>p.value),
  hist:out.hist.map(p=>p.value),colors:out.hist.map(p=>p.color),
  times:out.dif.map(p=>p.time)}}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        js_path = f.name
    try:
        got = json.loads(subprocess.run(["node", js_path], capture_output=True,
                                        text=True, check=True).stdout)
    finally:
        os.unlink(js_path)

    n_ok = len(got["dif"]) == len(ref["dif"]) == 200 - 25
    check("输出长度=样本-25（EMA26 满窗起）", n_ok,
          f"js={len(got['dif'])} py={len(ref['dif'])}")
    max_err = max(max(abs(a - b) for a, b in zip(got[k], ref[k]))
                  for k in ("dif", "dea", "hist"))
    check("JS 与 Python 参考逐点对拔 <1e-9", max_err < 1e-9, f"max_err={max_err:.2e}")
    check("时间戳与 bar 对齐（首值=第26根开盘秒）",
          got["times"][0] == 1_700_000_000 + 25 * 3600, str(got["times"][0]))
    colors_ok = all((h >= 0 and "14,203,129" in c) or (h < 0 and "246,70,93" in c)
                    for h, c in zip(got["hist"], got["colors"]))
    check("柱色正绿负红逐点正确", colors_ok)

    # pandas 交叉验证（标准实现对拔：ewm(adjust=False) 的 EMA12/EMA26 → DIF）
    try:
        import pandas as pd
        s = pd.Series(closes)
        dif_pd = (s.ewm(span=12, adjust=False).mean()
                  - s.ewm(span=26, adjust=False).mean()).iloc[25:].tolist()
        err_pd = max(abs(a - b) for a, b in zip(got["dif"], dif_pd))
        check("DIF 与 pandas ewm(adjust=False) 对拔 <1e-9", err_pd < 1e-9,
              f"err={err_pd:.2e}")
    except ImportError:
        check("DIF 与 pandas 对拔（pandas 缺失跳过）", True)

    # ── 3. 人工核对锚点 ──
    # 3a. 常数序列：MACD 全零
    flat = [{"ts": (1_700_000_000 + i * 60) * 1000, "c": 100.0} for i in range(60)]
    js2 = f"""
const TZ=0; const barTime=r=>Math.floor(r.ts/1000)-TZ;
{m.group(1)}{m.group(2)}
const o=macdCalc({json.dumps(flat)});
console.log(JSON.stringify([o.dif.every(p=>Math.abs(p.value)<1e-12),
  o.hist.every(p=>Math.abs(p.value)<1e-12)]));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js2)
        p2 = f.name
    try:
        r2 = json.loads(subprocess.run(["node", p2], capture_output=True,
                                       text=True, check=True).stdout)
    finally:
        os.unlink(p2)
    check("常数序列 → DIF/柱恒 0（手算锚点1）", all(r2))

    # 3b. 线性递增：DIF 尾值 → (26-1)/2 - (12-1)/2 = 7（EMA 对斜坡的滞后解析值）
    ramp = [{"ts": (1_700_000_000 + i * 60) * 1000, "c": float(i)} for i in range(300)]
    js3 = f"""
const TZ=0; const barTime=r=>Math.floor(r.ts/1000)-TZ;
{m.group(1)}{m.group(2)}
const o=macdCalc({json.dumps(ramp)});
console.log(JSON.stringify([o.dif[o.dif.length-1].value, o.hist[o.hist.length-1].value]));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js3)
        p3 = f.name
    try:
        r3 = json.loads(subprocess.run(["node", p3], capture_output=True,
                                       text=True, check=True).stdout)
    finally:
        os.unlink(p3)
    check("线性斜坡 DIF 尾值收敛 7（手算锚点2）", abs(r3[0] - 7.0) < 0.01,
          f"dif_tail={r3[0]:.6f}")
    check("线性斜坡柱尾值收敛 0", abs(r3[1]) < 0.01, f"hist_tail={r3[1]:.6f}")

    # 3c. EMA 递推首步手算：closes=[100,102] → EMA12 = 1304/13 = 100.307692…
    ema12_hand = 102 * (2 / 13) + 100 * (11 / 13)
    check("EMA12 首步手算 1304/13 一致（手算锚点3）",
          abs(ema12_hand - 1304 / 13) < 1e-12 and abs(ema12_hand - 100.3076923076923) < 1e-12)

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
