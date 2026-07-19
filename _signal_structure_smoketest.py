#!/usr/bin/env python3
"""信号结构几何生成器冒烟：合成 df 验证 载荷契约/各系统几何/降级路径。

不联网：直接构造含明确摆动/缺口/中枢形态的合成 K 线走 build() 全链路，
末尾验证 import jarvis_dashboard 无异常且 /api/twelve/structure 路由已注册。
"""

from __future__ import annotations

import math

import pandas as pd

import jarvis_signal_structure as jss

_FAILED: list[str] = []

START_TS = 1_700_000_000   # epoch 秒
STEP_S = 3600              # 1h bar


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def make_df(closes: list[float]) -> pd.DataFrame:
    """从收盘价序列造 OHLC：open=前收（无意外跳空），高低各外扩 0.5。"""
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rows.append({"time": (START_TS + i * STEP_S) * 1000,
                     "open": o, "high": max(o, c) + 0.5, "low": min(o, c) - 0.5,
                     "close": c, "volume": 100.0})
    return pd.DataFrame(rows)


def make_df_close(closes: list[float]) -> pd.DataFrame:
    """高低点仅随收盘外扩：峰/谷处 high/low 为严格极值，可被三根分型判定。"""
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rows.append({"time": (START_TS + i * STEP_S) * 1000,
                     "open": o, "high": c + 0.5, "low": c - 0.5,
                     "close": c, "volume": 100.0})
    return pd.DataFrame(rows)


def make_df_ohlc(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    rows = []
    for i, (o, h, l, c) in enumerate(bars):
        rows.append({"time": (START_TS + i * STEP_S) * 1000,
                     "open": o, "high": h, "low": l, "close": c, "volume": 100.0})
    return pd.DataFrame(rows)


def tri(i: int, period: int) -> int:
    """三角波 0..period//2..0。"""
    phase = i % period
    half = period // 2
    return phase if phase <= half else period - phase


ALL_SYSTEMS = ("turtle", "dow", "elliott", "volatility", "gann", "chanlun",
               "rule123", "gap", "martingale", "oscillator", "triple_rsi",
               "arbitrage")

VALID_SHAPES = {"arrow_up", "arrow_down", "circle", "square"}
VALID_POS = {"above", "below"}
VALID_STYLE = {"solid", "dashed"}


def validate_contract(name: str, out: dict) -> None:
    """载荷契约校验：键齐全、枚举合法、数值有限、ts 为 int 秒。"""
    ok = isinstance(out, dict) and set(out.keys()) == {"drawings", "note"}
    check(f"{name}: build 返回键 {{drawings, note}}", ok, str(set(out.keys())))
    dw = out["drawings"]
    ok = set(dw.keys()) == {"polylines", "markers", "boxes", "hlines"}
    check(f"{name}: drawings 四键齐全", ok, str(set(dw.keys())))
    issues = []
    for p in dw["polylines"]:
        if p["style"] not in VALID_STYLE:
            issues.append(f"polyline style {p['style']}")
        if len(p["points"]) < 2:
            issues.append("polyline 点数 < 2")
        for pt in p["points"]:
            if not isinstance(pt["ts"], int) or pt["ts"] < 1_000_000_000 or pt["ts"] > 10_000_000_000:
                issues.append(f"polyline ts 非 epoch 秒: {pt['ts']}")
            if not math.isfinite(pt["price"]):
                issues.append("polyline price 非有限值")
    for m in dw["markers"]:
        if m["shape"] not in VALID_SHAPES:
            issues.append(f"marker shape {m['shape']}")
        if m["position"] not in VALID_POS:
            issues.append(f"marker position {m['position']}")
        if not isinstance(m["ts"], int) or not math.isfinite(m["price"]):
            issues.append("marker ts/price 异常")
    for b in dw["boxes"]:
        if not (b["ts1"] <= b["ts2"]):
            issues.append("box ts1 > ts2")
        if not (b["price_lo"] < b["price_hi"]):
            issues.append("box price_lo >= price_hi")
    for h in dw["hlines"]:
        if h["style"] not in VALID_STYLE or not math.isfinite(h["price"]):
            issues.append("hline style/price 异常")
    check(f"{name}: 载荷字段契约", not issues, "；".join(issues[:3]))


# ─────────────────────────── 场景 df ───────────────────────────

# 震荡：三角波 周期12 振幅±3，96 根 → 分型/笔/重叠中枢
# （用 make_df_close：峰谷 high/low 为严格极值，满足缠论三根分型判定）
df_osc = make_df_close([97.0 + tri(i, 12) for i in range(96)])
# 上升趋势：斜率0.9 + 三角波 周期14，90 根 → HH/HL 结构
df_up = make_df([100.0 + 0.9 * i + 4 * tri(i, 14) for i in range(90)])
# 海龟突破：58 根窄幅震荡 + 末 2 根拉升
df_turtle = make_df([100.0 + (i % 5) * 0.3 for i in range(58)] + [112.0, 115.0])

# 向上缺口未回补：40 根平盘 → 跳空上行 → 高位持稳
_flat = [(100.0, 100.5, 99.5, 100.0)] * 40
_gap_bar = [(105.5, 106.5, 105.0, 106.0)]
_stay_up = [(106.0 + 0.1 * j, 106.8 + 0.1 * j, 105.6 + 0.1 * j, 106.4 + 0.1 * j)
            for j in range(19)]
df_gap_open = make_df_ohlc(_flat + _gap_bar + _stay_up)

# 向上缺口已回补：同上，但第 50 根跌回缺口下沿以下
_stay_then_fill = ([(106.0, 106.8, 105.6, 106.4)] * 9
                   + [(106.0, 106.2, 98.5, 99.0)]
                   + [(99.0, 99.8, 98.6, 99.4)] * 9)
df_gap_filled = make_df_ohlc(_flat + _gap_bar + _stay_then_fill)


def main() -> int:
    # 1) 全 12 系统 build 不抛异常 + 载荷契约
    for s in ALL_SYSTEMS:
        try:
            out = jss.build(s, df_up)
            validate_contract(f"契约[{s}]", out)
        except Exception as exc:  # noqa: BLE001
            check(f"契约[{s}]: build 不抛异常", False, repr(exc)[:120])

    # 2) chanlun：折线存在、点时间严格递增、中枢框 lo<hi
    out = jss.build("chanlun", df_osc)
    dw = out["drawings"]
    polys = [p for p in dw["polylines"] if p["label"] == "笔"]
    check("chanlun: 有笔折线", len(polys) == 1)
    if polys:
        ts_seq = [pt["ts"] for pt in polys[0]["points"]]
        check("chanlun: 折线点时间严格递增",
              all(ts_seq[i] < ts_seq[i + 1] for i in range(len(ts_seq) - 1)),
              f"{len(ts_seq)} 点")
    check("chanlun: 中枢框存在且 lo<hi",
          len(dw["boxes"]) == 1 and dw["boxes"][0]["price_lo"] < dw["boxes"][0]["price_hi"],
          str(dw["boxes"][:1]))
    check("chanlun: 中枢框为约定色 #d29922",
          bool(dw["boxes"]) and dw["boxes"][0]["color"] == "#d29922")

    # 3) dow：HH/HL/LH/LL 标注枚举合法 + 上升趋势末端箭头
    out = jss.build("dow", df_up)
    dw = out["drawings"]
    circles = [m for m in dw["markers"] if m["shape"] == "circle"]
    check("dow: 摆动折线存在", len(dw["polylines"]) == 1)
    check("dow: 摆动点标注枚举合法（HH/HL/LH/LL）",
          bool(circles) and all(m["text"] in {"HH", "HL", "LH", "LL"} for m in circles),
          f"{len(circles)} 个标注")
    arrows = [m for m in dw["markers"] if m["shape"] == "arrow_up"]
    check("dow: 上升趋势末端箭头为 arrow_up", len(arrows) == 1,
          str([m["text"] for m in arrows]))

    # 4) turtle：通道点数 = bar 数 + 突破箭头
    out = jss.build("turtle", df_turtle)
    dw = out["drawings"]
    check("turtle: 两条通道折线", len(dw["polylines"]) == 2)
    check("turtle: 通道点数 = bar 数",
          all(len(p["points"]) == len(df_turtle) for p in dw["polylines"]),
          f"bars={len(df_turtle)}, pts={[len(p['points']) for p in dw['polylines']]}")
    check("turtle: 最近突破箭头存在",
          any(m["shape"] == "arrow_up" and "突破" in m["text"] for m in dw["markers"]))

    # 5) gap：框与缺口一致（上沿=跳空 bar 低点、下沿=前 bar 高点）+ 回补状态
    out = jss.build("gap", df_gap_open)
    boxes = out["drawings"]["boxes"]
    check("gap[未回补]: 恰 1 个缺口框", len(boxes) == 1, str(len(boxes)))
    if boxes:
        b = boxes[0]
        check("gap[未回补]: 框边界与缺口一致",
              abs(b["price_lo"] - 100.5) < 1e-9 and abs(b["price_hi"] - 105.0) < 1e-9,
              f"lo={b['price_lo']} hi={b['price_hi']}")
        check("gap[未回补]: 状态标注·未回补", "未回补" in b["label"], b["label"])
        check("gap[未回补]: 框延伸至最新 bar",
              b["ts2"] == START_TS + (len(df_gap_open) - 1) * STEP_S)
    out = jss.build("gap", df_gap_filled)
    boxes = out["drawings"]["boxes"]
    check("gap[已回补]: 状态标注·已回补",
          len(boxes) == 1 and "已回补" in boxes[0]["label"],
          str([b["label"] for b in boxes]))
    if boxes:
        check("gap[已回补]: 框止于回补 bar",
              boxes[0]["ts2"] == START_TS + 50 * STEP_S,
              f"ts2={boxes[0]['ts2']} 期望={START_TS + 50 * STEP_S}")

    # 6) 无几何语义系统：空 drawings + note，不抛异常
    for s in ("volatility", "martingale", "arbitrage"):
        out = jss.build(s, df_up)
        dw = out["drawings"]
        empty = not (dw["polylines"] or dw["markers"] or dw["boxes"] or dw["hlines"])
        check(f"{s}: 返回空 drawings + note", empty and bool(out["note"]),
              str(out["note"])[:40])

    # 7) 降级路径：未知系统 / None / 空 df / 数据不足 均不抛异常
    for label, args in (("未知系统", ("nope", df_up)),
                        ("df=None", ("chanlun", None)),
                        ("空df", ("chanlun", pd.DataFrame())),
                        ("数据不足", ("chanlun", df_up.head(5)))):
        try:
            out = jss.build(*args)
            dw = out["drawings"]
            empty = not (dw["polylines"] or dw["markers"] or dw["boxes"] or dw["hlines"])
            check(f"降级[{label}]: 空 drawings + note", empty and bool(out["note"]),
                  str(out["note"])[:40])
        except Exception as exc:  # noqa: BLE001
            check(f"降级[{label}]: 不抛异常", False, repr(exc)[:120])

    # 8) elliott：fib 水平线 5 条 + 主浪折线时间递增
    out = jss.build("elliott", df_up)
    dw = out["drawings"]
    check("elliott: fib/波段水平线 5 条", len(dw["hlines"]) == 5,
          str([h["label"] for h in dw["hlines"]]))
    if dw["polylines"]:
        ts_seq = [pt["ts"] for pt in dw["polylines"][0]["points"]]
        check("elliott: 主浪折线点时间严格递增",
              all(ts_seq[i] < ts_seq[i + 1] for i in range(len(ts_seq) - 1)))
    else:
        check("elliott: 主浪折线存在", False)

    # 9) dashboard 可导入且路由已注册
    try:
        import jarvis_dashboard as jd
        check("dashboard: import 无异常", True)
        check("dashboard: /api/twelve/structure 路由已注册",
              any(getattr(r, "path", "") == "/api/twelve/structure"
                  for r in jd.app.routes))
    except Exception as exc:  # noqa: BLE001
        check("dashboard: import 无异常", False, repr(exc)[:200])

    print()
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败: {_FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
