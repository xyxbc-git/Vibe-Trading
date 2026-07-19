#!/usr/bin/env python3
"""贾维斯 JARVIS — 十二系统信号结构几何生成器（K 线画线载荷）。

把 jarvis_twelve_systems 各 signal_* 内部已计算但未对外暴露的结构
（摆动点/分型/笔/中枢/缺口/通道/趋势线/时间窗）按同口径重算，输出前端
可直接叠加到蜡烛图上的几何载荷——缠论教科书式画线（笔折线+中枢框+
买卖点箭头），而不是只有水平线。

载荷契约（与前端约定，字段严格保持）：
  drawings = {
    "polylines": [{"points": [{"ts", "price"}...], "color",
                   "style": "solid"|"dashed", "width", "label"}],
    "markers":   [{"ts", "price", "position": "above"|"below",
                   "shape": "arrow_up"|"arrow_down"|"circle"|"square",
                   "color", "text"}],
    "boxes":     [{"ts1", "ts2", "price_lo", "price_hi", "color", "label"}],
    "hlines":    [{"price", "color", "label", "style"}],
  }
  ts = bar 开盘时间 epoch 秒（与 /api/kline rows 的 ts/1000 同源对齐）。

纯函数、不联网：只吃 fetch_klines_df 形状的 DataFrame
（列 time[epoch 毫秒]/open/high/low/close/volume）。
复用 jarvis_twelve_systems 的辅助函数（import 不复制，不改动既有函数）。
build() 对未知 system / 数据不足 / 计算异常一律返回空 drawings + note，绝不抛出。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import jarvis_twelve_systems as jts

# 颜色缺省（与任务契约一致）
C_BULL = "#3fb950"   # 看涨结构
C_BEAR = "#f85149"   # 看跌结构
C_NEUT = "#8b949e"   # 中性
C_BOX = "#d29922"    # 中枢/缺口框

_rp = jts._round_price


# ─────────────────────────── 载荷构件 ───────────────────────────


def _empty() -> dict:
    return {"polylines": [], "markers": [], "boxes": [], "hlines": []}


def _ts(df: pd.DataFrame, i: int) -> int:
    """bar 开盘时间 epoch 秒（df.time 为毫秒，与 /api/kline rows.ts 同源）。"""
    return int(df["time"].iloc[i]) // 1000


def _poly(points: list[dict], color: str, style: str = "solid",
          width: int = 1, label: str = "") -> dict:
    return {"points": points, "color": color, "style": style,
            "width": width, "label": label}


def _marker(ts: int, price: float, position: str, shape: str,
            color: str, text: str) -> dict:
    return {"ts": ts, "price": _rp(price), "position": position,
            "shape": shape, "color": color, "text": text}


def _box(ts1: int, ts2: int, price_lo: float, price_hi: float,
         color: str, label: str) -> dict:
    return {"ts1": ts1, "ts2": ts2, "price_lo": _rp(price_lo),
            "price_hi": _rp(price_hi), "color": color, "label": label}


def _hline(price: float, color: str, label: str, style: str = "dashed") -> dict:
    return {"price": _rp(price), "color": color, "label": label, "style": style}


def _pt(df: pd.DataFrame, i: int, price: float) -> dict:
    return {"ts": _ts(df, i), "price": _rp(price)}


# ─────────────────────────── 1. 海龟：20期通道 + 突破箭头 ───────────────────────────


def _build_turtle(df: pd.DataFrame) -> tuple[dict, str | None]:
    d = _empty()
    n = len(df)
    if n < jts.MIN_BARS:
        return d, f"数据不足（{n} 根 < {jts.MIN_BARS}）"
    # 与 signal_turtle 同口径：每根 bar 对比「之前 20 根」极值（shift 排除自身）。
    # 首根无前置数据，用自身高低补齐，保证通道点数 = bar 数（前端逐 bar 对齐）。
    hh = df["high"].rolling(20, min_periods=1).max().shift(1)
    ll = df["low"].rolling(20, min_periods=1).min().shift(1)
    hh.iloc[0] = float(df["high"].iloc[0])
    ll.iloc[0] = float(df["low"].iloc[0])
    d["polylines"].append(_poly(
        [_pt(df, i, float(hh.iloc[i])) for i in range(n)],
        C_BULL, "solid", 1, "20期高通道（突破做多位）"))
    d["polylines"].append(_poly(
        [_pt(df, i, float(ll.iloc[i])) for i in range(n)],
        C_BEAR, "solid", 1, "20期低通道（跌破做空位）"))
    # 最近一次突破 bar（自后向前找第一根收盘越过通道的 bar）
    for i in range(n - 1, 0, -1):
        c = float(df["close"].iloc[i])
        if c > float(hh.iloc[i]):
            d["markers"].append(_marker(_ts(df, i), float(df["low"].iloc[i]),
                                        "below", "arrow_up", C_BULL, "突破20期高"))
            break
        if c < float(ll.iloc[i]):
            d["markers"].append(_marker(_ts(df, i), float(df["high"].iloc[i]),
                                        "above", "arrow_down", C_BEAR, "跌破20期低"))
            break
    return d, None


# ─────────────────────────── 2. 道氏：摆动折线 + HH/HL/LH/LL + 趋势箭头 ───────────────────────────


def _build_dow(df: pd.DataFrame) -> tuple[dict, str | None]:
    d = _empty()
    if len(df) < jts.MIN_BARS:
        return d, f"数据不足（{len(df)} 根 < {jts.MIN_BARS}）"
    highs_i, lows_i = jts._swing_points(df, window=5)
    if len(highs_i) < 2 or len(lows_i) < 2:
        return d, "swing 高低点不足（<2 个），结构无法判定"
    pts_all = sorted(
        [(i, float(df["high"].iloc[i]), "H") for i in highs_i]
        + [(i, float(df["low"].iloc[i]), "L") for i in lows_i])
    d["polylines"].append(_poly(
        [_pt(df, i, v) for i, v, _ in pts_all], C_NEUT, "solid", 1, "摆动高低点折线"))
    # 每个摆动点相对前一同类点标注 HH/HL/LH/LL
    prev_h: float | None = None
    prev_l: float | None = None
    for i, v, kind in pts_all:
        if kind == "H":
            if prev_h is not None:
                tag = "HH" if v > prev_h else "LH"
                d["markers"].append(_marker(_ts(df, i), v, "above", "circle",
                                            C_BULL if tag == "HH" else C_BEAR, tag))
            prev_h = v
        else:
            if prev_l is not None:
                tag = "HL" if v > prev_l else "LL"
                d["markers"].append(_marker(_ts(df, i), v, "below", "circle",
                                            C_BULL if tag == "HL" else C_BEAR, tag))
            prev_l = v
    # 末端趋势方向箭头（判定口径与 signal_dow 完全一致；中性不硬画）
    h_vals = [float(df["high"].iloc[i]) for i in highs_i[-3:]]
    l_vals = [float(df["low"].iloc[i]) for i in lows_i[-3:]]
    hh = all(h_vals[k] < h_vals[k + 1] for k in range(len(h_vals) - 1))
    hl = all(l_vals[k] < l_vals[k + 1] for k in range(len(l_vals) - 1))
    lh = all(h_vals[k] > h_vals[k + 1] for k in range(len(h_vals) - 1))
    ll = all(l_vals[k] > l_vals[k + 1] for k in range(len(l_vals) - 1))
    last = len(df) - 1
    if hh or hl:
        d["markers"].append(_marker(_ts(df, last), float(df["low"].iloc[last]),
                                    "below", "arrow_up", C_BULL,
                                    "趋势多" if (hh and hl) else "偏多"))
    elif lh or ll:
        d["markers"].append(_marker(_ts(df, last), float(df["high"].iloc[last]),
                                    "above", "arrow_down", C_BEAR,
                                    "趋势空" if (lh and ll) else "偏空"))
    return d, None


# ─────────────────────────── 3. 艾略特：主浪 zigzag + 浪号 + fib 水平线 ───────────────────────────


def _build_elliott(df: pd.DataFrame) -> tuple[dict, str | None]:
    d = _empty()
    if len(df) < jts.MIN_BARS:
        return d, f"数据不足（{len(df)} 根 < {jts.MIN_BARS}）"
    look = df.iloc[-120:] if len(df) >= 120 else df
    off = len(df) - len(look)
    hi_pos = int(look["high"].values.argmax())
    lo_pos = int(look["low"].values.argmin())
    hi = float(look["high"].iloc[hi_pos])
    lo = float(look["low"].iloc[lo_pos])
    if hi - lo < 1e-9:
        return d, "波段振幅为零，无法计算回撤位"
    up_leg = lo_pos < hi_pos
    # fib 回撤水平线（与 signal_elliott 同口径）
    if up_leg:
        f382, f500, f618 = (hi - (hi - lo) * r for r in (0.382, 0.5, 0.618))
    else:
        f382, f500, f618 = (lo + (hi - lo) * r for r in (0.382, 0.5, 0.618))
    d["hlines"] += [
        _hline(f382, C_BOX, "fib 0.382", "dashed"),
        _hline(f500, C_BOX, "fib 0.5", "dashed"),
        _hline(f618, C_BOX, "fib 0.618", "dashed"),
        _hline(hi, C_NEUT, "波段高点", "solid"),
        _hline(lo, C_NEUT, "波段低点", "solid"),
    ]
    a, b = (lo_pos, hi_pos) if up_leg else (hi_pos, lo_pos)
    if a == b:
        return d, "波段高低点位于同根K线，无法画主浪折线"
    # 主浪段 zigzag：起止极值点 + 之间的摆动点串联；主浪结束后接现价（回撤/反弹段）
    sh_i, sl_i = jts._swing_points(look, window=4)
    inner = sorted(
        [(i, float(look["high"].iloc[i]), "H") for i in sh_i if a < i < b]
        + [(i, float(look["low"].iloc[i]), "L") for i in sl_i if a < i < b])
    leg_pts: list[tuple[int, float, str]] = (
        [(a, lo if up_leg else hi, "L" if up_leg else "H")]
        + inner
        + [(b, hi if up_leg else lo, "H" if up_leg else "L")])
    pts = list(leg_pts)
    last = len(look) - 1
    if b < last:
        pts.append((last, float(look["close"].iloc[-1]), "C"))
    d["polylines"].append(_poly(
        [_pt(df, off + i, v) for i, v, _ in pts],
        C_BULL if up_leg else C_BEAR, "solid", 2, "主浪段"))
    # 简化浪号：仅当主浪内部恰为 5 段清晰推进结构（浪3/浪5 逐级延伸、
    # 浪2/浪4 回撤不破前低/前高）才标 1-5，判不出不硬造
    if len(leg_pts) == 6:
        v = [p[1] for p in leg_pts]
        kinds = [p[2] for p in leg_pts]
        alternating = all(kinds[k] != kinds[k + 1] for k in range(5))
        if up_leg:
            ok5 = (alternating and v[1] > v[0] and v[0] < v[2] < v[1]
                   and v[3] > v[1] and v[2] < v[4] < v[3] and v[5] > v[3])
        else:
            ok5 = (alternating and v[1] < v[0] and v[0] > v[2] > v[1]
                   and v[3] < v[1] and v[2] > v[4] > v[3] and v[5] < v[3])
        if ok5:
            color = C_BULL if up_leg else C_BEAR
            for wave_no, (i, price, kind) in enumerate(leg_pts[1:], start=1):
                d["markers"].append(_marker(
                    _ts(df, off + i), price,
                    "above" if kind == "H" else "below",
                    "circle", color, f"浪{wave_no}"))
    return d, None


# ─────────────────────────── 5. 江恩：斐波时间窗 marker ───────────────────────────


def _build_gann(df: pd.DataFrame) -> tuple[dict, str | None]:
    d = _empty()
    if len(df) < jts.MIN_BARS:
        return d, f"数据不足（{len(df)} 根 < {jts.MIN_BARS}）"
    look = df.iloc[-160:] if len(df) >= 160 else df
    off = len(df) - len(look)
    hi_pos = int(look["high"].values.argmax())
    lo_pos = int(look["low"].values.argmin())
    seen: set[int] = set()
    for base_pos, base_cn in ((hi_pos, "高"), (lo_pos, "低")):
        anchor_price = (float(look["high"].iloc[base_pos]) if base_cn == "高"
                        else float(look["low"].iloc[base_pos]))
        d["markers"].append(_marker(
            _ts(df, off + base_pos), anchor_price,
            "above" if base_cn == "高" else "below", "square",
            C_NEUT, f"显著{base_cn}点"))
        for w in jts._FIB_WINDOWS:
            t = base_pos + w
            if t >= len(look) or (off + t) in seen:
                continue
            seen.add(off + t)
            d["markers"].append(_marker(
                _ts(df, off + t), float(look["high"].iloc[t]),
                "above", "circle", C_BOX, f"T{w}"))
    return d, None


# ─────────────────────────── 6. 缠论：笔折线 + 中枢框 + 买卖点箭头 ───────────────────────────


def _build_chanlun(df: pd.DataFrame) -> tuple[dict, str | None]:
    d = _empty()
    if len(df) < jts.MIN_BARS:
        return d, f"数据不足（{len(df)} 根 < {jts.MIN_BARS}）"
    frs = jts._fractals(df)
    strokes = jts._strokes(frs)
    if len(strokes) < 3:
        return d, f"笔数量不足（{len(strokes)} < 3），无法构建中枢"
    # 笔 zigzag：首笔起点 + 各笔终点串联
    stroke_pts = [strokes[0]["from"]] + [s["to"] for s in strokes]
    d["polylines"].append(_poly(
        [_pt(df, p["i"], p["price"]) for p in stroke_pts],
        C_NEUT, "solid", 2, "笔"))
    # 中枢：最近三笔重叠区（与 signal_chanlun 同口径），时间跨度画到最新 bar
    s3 = strokes[-3:]
    zg = min(max(s["from"]["price"], s["to"]["price"]) for s in s3)
    zd = max(min(s["from"]["price"], s["to"]["price"]) for s in s3)
    close = float(df["close"].iloc[-1])
    last = len(df) - 1
    has_zone = zg > zd
    if has_zone:
        d["boxes"].append(_box(_ts(df, s3[0]["from"]["i"]), _ts(df, last),
                               zd, zg, C_BOX, f"中枢 [{_rp(zd)}, {_rp(zg)}]"))
    # 买卖点（与 signal_chanlun 判定同口径：能判定几类画几类，判不出不硬造）
    last_stroke = strokes[-1]
    prev_same = [s for s in strokes[:-1] if s["dir"] == last_stroke["dir"]]

    def _mag(s: dict) -> float:
        return abs(s["to"]["price"] - s["from"]["price"])

    divergence = bool(prev_same) and _mag(last_stroke) < _mag(prev_same[-1]) * 0.7
    used_ts: set[int] = set()

    def _add(ts: int, price: float, bullish: bool, text: str) -> None:
        if ts in used_ts:
            return
        used_ts.add(ts)
        d["markers"].append(_marker(
            ts, price, "below" if bullish else "above",
            "arrow_up" if bullish else "arrow_down",
            C_BULL if bullish else C_BEAR, text))

    if has_zone:
        if close > zg:      # 三买近似：突破中枢上沿后运行于其上
            _add(_ts(df, last), float(df["low"].iloc[last]), True, "三买近似")
        elif close < zd:    # 三卖近似：跌破中枢下沿后运行于其下
            _add(_ts(df, last), float(df["high"].iloc[last]), False, "三卖近似")
        elif divergence and last_stroke["dir"] == "down":   # 一买近似：中枢内下跌笔背离
            p = last_stroke["to"]
            _add(_ts(df, p["i"]), p["price"], True, "一买近似")
        elif divergence and last_stroke["dir"] == "up":     # 一卖近似：中枢内上涨笔背离
            p = last_stroke["to"]
            _add(_ts(df, p["i"]), p["price"], False, "一卖近似")
        # 二类近似：次低点不破极低（前极低为全部笔端点最低）/ 次高点不破极高
        downs = [s for s in strokes if s["dir"] == "down"]
        if len(downs) >= 2:
            d1, d2 = downs[-2], downs[-1]
            floor = min(min(s["from"]["price"], s["to"]["price"]) for s in strokes)
            if (d1["to"]["price"] <= floor + 1e-12
                    and d2["to"]["price"] > d1["to"]["price"]
                    and close > d2["to"]["price"]):
                _add(_ts(df, d2["to"]["i"]), d2["to"]["price"], True, "二买近似")
        ups = [s for s in strokes if s["dir"] == "up"]
        if len(ups) >= 2:
            u1, u2 = ups[-2], ups[-1]
            ceil_ = max(max(s["from"]["price"], s["to"]["price"]) for s in strokes)
            if (u1["to"]["price"] >= ceil_ - 1e-12
                    and u2["to"]["price"] < u1["to"]["price"]
                    and close < u2["to"]["price"]):
                _add(_ts(df, u2["to"]["i"]), u2["to"]["price"], False, "二卖近似")
    return d, None


# ─────────────────────────── 7. 123法则：三点 marker + 趋势线 + 破坏箭头 ───────────────────────────


def _tl_val(i1: int, v1: float, i2: int, v2: float, at: int) -> float:
    if i2 == i1:
        return v2
    return v2 + (v2 - v1) / (i2 - i1) * (at - i2)


def _rule123_long(df: pd.DataFrame, highs_i: list[int], lows_i: list[int]):
    """做多三步几何复算（判定口径与 signal_rule123 一致）。返回 None 表示未启动。"""
    close = float(df["close"].iloc[-1])
    last = len(df) - 1
    h1, h2 = highs_i[-2], highs_i[-1]
    hv1, hv2 = float(df["high"].iloc[h1]), float(df["high"].iloc[h2])
    if not (hv2 < hv1):
        return None
    tl_last = _tl_val(h1, hv1, h2, hv2, last)
    if close <= tl_last:
        return None
    steps = 1
    p1 = next((i for i in range(h2 + 1, last + 1)
               if float(df["close"].iloc[i]) > _tl_val(h1, hv1, h2, hv2, i)), last)
    p2 = p3 = rebound_high = rb_i = None
    lows_after = [i for i in lows_i if i > h2]
    if lows_after:
        prior_low = float(df["low"].iloc[lows_after[0]])
        later_min = float(df["low"].iloc[lows_after[0]:].min())
        if later_min >= prior_low - 1e-9:
            steps = 2
            p2 = lows_after[0]
            hi_after = df["high"].iloc[lows_after[0]:last]
            if len(hi_after) > 1:
                rebound_high = float(hi_after.iloc[:-1].max())
                rb_i = int(hi_after.iloc[:-1].values.argmax()) + lows_after[0]
                if close > rebound_high:
                    steps = 3
                    p3 = next((i for i in range(rb_i + 1, last + 1)
                               if float(df["close"].iloc[i]) > rebound_high), last)
    return {"steps": steps, "tl": (h1, hv1, h2, hv2, tl_last),
            "p1": p1, "p2": p2, "p3": p3, "rebound_high": rebound_high,
            "bullish": True}


def _rule123_short(df: pd.DataFrame, highs_i: list[int], lows_i: list[int]):
    close = float(df["close"].iloc[-1])
    last = len(df) - 1
    l1, l2 = lows_i[-2], lows_i[-1]
    lv1, lv2 = float(df["low"].iloc[l1]), float(df["low"].iloc[l2])
    if not (lv2 > lv1):
        return None
    tl_last = _tl_val(l1, lv1, l2, lv2, last)
    if close >= tl_last:
        return None
    steps = 1
    p1 = next((i for i in range(l2 + 1, last + 1)
               if float(df["close"].iloc[i]) < _tl_val(l1, lv1, l2, lv2, i)), last)
    p2 = p3 = pullback_low = pb_i = None
    highs_after = [i for i in highs_i if i > l2]
    if highs_after:
        prior_high = float(df["high"].iloc[highs_after[0]])
        later_max = float(df["high"].iloc[highs_after[0]:].max())
        if later_max <= prior_high + 1e-9:
            steps = 2
            p2 = highs_after[0]
            lo_after = df["low"].iloc[highs_after[0]:last]
            if len(lo_after) > 1:
                pullback_low = float(lo_after.iloc[:-1].min())
                pb_i = int(lo_after.iloc[:-1].values.argmin()) + highs_after[0]
                if close < pullback_low:
                    steps = 3
                    p3 = next((i for i in range(pb_i + 1, last + 1)
                               if float(df["close"].iloc[i]) < pullback_low), last)
    return {"steps": steps, "tl": (l1, lv1, l2, lv2, tl_last),
            "p1": p1, "p2": p2, "p3": p3, "rebound_high": pullback_low,
            "bullish": False}


def _build_rule123(df: pd.DataFrame) -> tuple[dict, str | None]:
    d = _empty()
    if len(df) < jts.MIN_BARS:
        return d, f"数据不足（{len(df)} 根 < {jts.MIN_BARS}）"
    highs_i, lows_i = jts._swing_points(df, window=4)
    if len(highs_i) < 2 or len(lows_i) < 2:
        return d, "swing 点不足，无法画趋势线"
    geo_l = _rule123_long(df, highs_i, lows_i)
    geo_s = _rule123_short(df, highs_i, lows_i)
    # 方向优先级与 signal_rule123 一致：steps_long > steps_short 取多，否则有空取空
    geo = None
    if geo_l and (not geo_s or geo_l["steps"] > geo_s["steps"]):
        geo = geo_l
    elif geo_s:
        geo = geo_s
    if geo is None:
        return d, "未出现趋势线破坏迹象，123 反转流程未启动"
    bullish = geo["bullish"]
    color = C_BULL if bullish else C_BEAR
    i1, v1, i2, v2, tl_last = geo["tl"]
    last = len(df) - 1
    d["polylines"].append(_poly(
        [_pt(df, i1, v1), _pt(df, i2, v2), _pt(df, last, tl_last)],
        C_NEUT, "dashed", 1, "下降趋势线" if bullish else "上升趋势线"))
    # ①破线 ②不创新低/新高 ③破反弹高/回调低（完成几步画几步）
    p1 = geo["p1"]
    d["markers"].append(_marker(
        _ts(df, p1),
        float(df["close"].iloc[p1]),
        "below" if bullish else "above", "circle", color, "①破趋势线"))
    if geo["p2"] is not None:
        p2 = geo["p2"]
        price2 = float(df["low"].iloc[p2]) if bullish else float(df["high"].iloc[p2])
        d["markers"].append(_marker(
            _ts(df, p2), price2, "below" if bullish else "above", "circle", color,
            "②不创新低" if bullish else "②不创新高"))
    if geo["rebound_high"] is not None:
        d["hlines"].append(_hline(geo["rebound_high"], color,
                                  "反弹高点" if bullish else "回调低点", "dashed"))
    if geo["p3"] is not None:
        p3 = geo["p3"]
        if bullish:
            d["markers"].append(_marker(_ts(df, p3), float(df["low"].iloc[p3]),
                                        "below", "arrow_up", color, "③反转确认"))
        else:
            d["markers"].append(_marker(_ts(df, p3), float(df["high"].iloc[p3]),
                                        "above", "arrow_down", color, "③反转确认"))
    return d, None


# ─────────────────────────── 8. 跳空：缺口框 + 回补状态 ───────────────────────────


def _build_gap(df: pd.DataFrame) -> tuple[dict, str | None]:
    d = _empty()
    if len(df) < jts.MIN_BARS:
        return d, f"数据不足（{len(df)} 根 < {jts.MIN_BARS}）"
    look = min(len(df) - 1, 60)
    atr = float(jts._atr(df).iloc[-1])
    last = len(df) - 1
    for i in range(len(df) - look, len(df)):
        prev_h = float(df["high"].iloc[i - 1])
        prev_l = float(df["low"].iloc[i - 1])
        cur_l = float(df["low"].iloc[i])
        cur_h = float(df["high"].iloc[i])
        if cur_l > prev_h + 0.1 * atr:      # 向上缺口（同 signal_gap 噪声垫）
            seg = df["low"].iloc[i:].values
            filled = bool(seg.min() <= prev_h)
            end_i = (i + int(np.argmax(seg <= prev_h))) if filled else last
            d["boxes"].append(_box(
                _ts(df, i - 1), _ts(df, end_i), prev_h, cur_l,
                C_NEUT if filled else C_BOX,
                f"向上缺口·{'已回补' if filled else '未回补'}"))
        elif cur_h < prev_l - 0.1 * atr:    # 向下缺口
            seg = df["high"].iloc[i:].values
            filled = bool(seg.max() >= prev_l)
            end_i = (i + int(np.argmax(seg >= prev_l))) if filled else last
            d["boxes"].append(_box(
                _ts(df, i - 1), _ts(df, end_i), cur_h, prev_l,
                C_NEUT if filled else C_BOX,
                f"向下缺口·{'已回补' if filled else '未回补'}"))
    if not d["boxes"]:
        return d, f"近 {look} 根内无跳空缺口"
    return d, None


# ─────────────────────────── 10. 摆动震荡：超买超卖翻转箭头 ───────────────────────────

_MAX_EVENT_MARKERS = 40  # 事件类箭头上限：只保留最近 N 个，防止载荷爆炸


def _build_oscillator(df: pd.DataFrame) -> tuple[dict, str | None]:
    d = _empty()
    if len(df) < jts.MIN_BARS:
        return d, f"数据不足（{len(df)} 根 < {jts.MIN_BARS}）"
    rsi = jts._rsi(df["close"])
    k, _kd, _kj = jts._kdj(df)
    events: list[tuple[int, bool, str]] = []
    for t in range(1, len(df)):
        r0, r1 = float(rsi.iloc[t - 1]), float(rsi.iloc[t])
        k0, k1 = float(k.iloc[t - 1]), float(k.iloc[t])
        if r0 < 30 <= r1:
            events.append((t, True, "RSI超卖回升"))
        elif r0 > 70 >= r1:
            events.append((t, False, "RSI超买回落"))
        if k0 < 20 <= k1:
            events.append((t, True, "KDJ超卖回升"))
        elif k0 > 80 >= k1:
            events.append((t, False, "KDJ超买回落"))
    for t, bullish, text in events[-_MAX_EVENT_MARKERS:]:
        if bullish:
            d["markers"].append(_marker(_ts(df, t), float(df["low"].iloc[t]),
                                        "below", "arrow_up", C_BULL, text))
        else:
            d["markers"].append(_marker(_ts(df, t), float(df["high"].iloc[t]),
                                        "above", "arrow_down", C_BEAR, text))
    if not d["markers"]:
        return d, "回看范围内无超买超卖翻转事件"
    return d, None


# ─────────────────────────── 11. 三重平滑RSI：金叉/死叉箭头 ───────────────────────────


def _build_triple_rsi(df: pd.DataFrame) -> tuple[dict, str | None]:
    d = _empty()
    if len(df) < 60:
        return d, f"数据不足（{len(df)} 根 < 60，三级平滑需要更长样本）"
    raw = jts._rsi(df["close"], 14)
    s2 = raw.ewm(span=5, adjust=False).mean().ewm(span=8, adjust=False).mean()
    s3 = s2.ewm(span=13, adjust=False).mean()
    events: list[tuple[int, bool]] = []
    for t in range(1, len(df)):
        f0, f1 = float(s2.iloc[t - 1]), float(s2.iloc[t])
        g0, g1 = float(s3.iloc[t - 1]), float(s3.iloc[t])
        if f0 <= g0 and f1 > g1:
            events.append((t, True))
        elif f0 >= g0 and f1 < g1:
            events.append((t, False))
    for t, golden in events[-_MAX_EVENT_MARKERS:]:
        if golden:
            d["markers"].append(_marker(_ts(df, t), float(df["low"].iloc[t]),
                                        "below", "arrow_up", C_BULL, "金叉"))
        else:
            d["markers"].append(_marker(_ts(df, t), float(df["high"].iloc[t]),
                                        "above", "arrow_down", C_BEAR, "死叉"))
    if not d["markers"]:
        return d, "回看范围内无金叉/死叉事件"
    return d, None


# ─────────────────────────── 无几何语义系统 ───────────────────────────

_NO_GEOMETRY_NOTE = {
    "volatility": "波动率系统输出的是环境分位（酝酿突破/均值回归提示），无K线几何结构可画",
    "martingale": "马丁格尔是资金管理体系（仓位倍投序列），不产生K线几何结构",
    "arbitrage": "套利系统基于期现基差统计（双腿价差），无单腿K线几何结构可画",
}

_BUILDERS = {
    "turtle": _build_turtle,
    "dow": _build_dow,
    "elliott": _build_elliott,
    "gann": _build_gann,
    "chanlun": _build_chanlun,
    "rule123": _build_rule123,
    "gap": _build_gap,
    "oscillator": _build_oscillator,
    "triple_rsi": _build_triple_rsi,
}

_REQUIRED_COLS = {"time", "open", "high", "low", "close"}


def build(system: str, df: pd.DataFrame | None) -> dict:
    """生成指定系统的画线载荷 → {"drawings": {...}, "note": str|None}。

    未知 system / 数据不足 / 计算异常一律降级为空 drawings + note，绝不抛出。
    """
    key = str(system or "").strip().lower()
    if key in _NO_GEOMETRY_NOTE:
        return {"drawings": _empty(), "note": _NO_GEOMETRY_NOTE[key]}
    fn = _BUILDERS.get(key)
    if fn is None:
        return {"drawings": _empty(),
                "note": f"未知信号系统 {system!r}，无对应几何生成器"}
    if df is None or len(df) == 0 or not _REQUIRED_COLS.issubset(df.columns):
        return {"drawings": _empty(), "note": "K线数据缺失或缺少必需列"}
    try:
        drawings, note = fn(df)
        return {"drawings": drawings, "note": note}
    except Exception as exc:  # noqa: BLE001 — 画线失败绝不拖垮信号主链路
        return {"drawings": _empty(), "note": f"结构计算异常已降级: {repr(exc)[:120]}"}
