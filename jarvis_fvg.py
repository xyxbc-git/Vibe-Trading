#!/usr/bin/env python3
"""贾维斯 JARVIS — FVG（Fair Value Gap，公允价值缺口）检测 + swing 支撑/压力汇总。

[任务 N] 情绪风控导师配套的证据模块：纯函数、不联网，只吃
DataFrame(open/high/low/close[/volume])。供 jarvis_trade_mentor（agent-8）
try-import 调用：`jarvis_fvg.detect(df)`——签名与返回契约见 detect() docstring。

FVG 定义（三根 K 线价格失衡，ICT/订单流口径）：
  看涨 FVG：第 1 根 high < 第 3 根 low（中间大阳拉升，[bar1.high, bar3.low]
            区间没有发生过交易，留下价格真空，回踩时倾向充当支撑）
  看跌 FVG：第 1 根 low  > 第 3 根 high（镜像，[bar3.high, bar1.low]，压力）
  噪声门槛：缺口高度 ≥ MIN_GAP_ATR_MULT × 创建时刻 ATR14 才计。
  回补（mitigation）：创建后价格重新交易回缺口区。fill_pct 为最深回踩占缺口
  高度的百分比（0~100）；mitigated = 完全回补（fill_pct ≥ 100，缺口失效）。

swing 支撑/压力：与 jarvis_twelve_systems._swing_points 同口径（window 根内
局部极值）在本文件独立实现（任务红线：不改动 jarvis_twelve_systems）；相邻
价位按 CLUSTER_ATR_MULT×ATR 聚簇并计触碰次数，现价上下分列压力/支撑。

[任务 N2] Premium/Discount 折价溢价区（SMC dealing range，波段口径预登记，
防事后改口径）：
  - 波段选取：近 lookback（默认 120）根内的 swing 高/低点（window=5，与
    swing_levels 同口径）构成 dealing range——range_high=窗口内最高 swing
    高点，range_low=窗口内最低 swing 低点；窗口内 swing 点不足（高或低 <1
    个）时退化为该窗口的原始 high/low 极值。
  - 有效性门槛：range 高度 ≥ 0.5×ATR14，否则视为无显著波段（ok=False）。
  - 分位口径：pos_pct = (现价-range_low)/(range_high-range_low)×100，夹到
    [0,100]；zone：>55=premium（溢价，做多=追高）、<45=discount（折价，
    做空=杀跌）、45~55=equilibrium 均衡带。equilibrium 价=区间 0.5 中点。

所有距离标注均按 ATR 归一（dist_atr）并附百分比（dist_pct），供导师解释层
把「离入场多远」讲成人话。
"""

from __future__ import annotations

import math

import pandas as pd

MIN_BARS = 30              # 有效检测最少 K 线数（ATR/swing 需要热身）
MIN_GAP_ATR_MULT = 0.1     # 噪声门槛：缺口高度 ≥ 0.1×ATR 才计
CLUSTER_ATR_MULT = 0.25    # swing 价位聚簇半径（×ATR）
SR_MAX_LEVELS = 6          # 支撑/压力各最多返回条数
_REQUIRED_COLS = ("open", "high", "low", "close")

# [N2] Premium/Discount 预登记参数（改动须走评审，不许跑完数据后调）
PD_LOOKBACK = 120          # dealing range 取段窗口（根）
PD_EQ_BAND = (45.0, 55.0)  # 均衡带分位区间（含边界）
PD_MIN_RANGE_ATR = 0.5     # 波段有效性门槛：区间高度 ≥ 0.5×ATR


# ─────────────────────────── 公共指标（本文件自足） ───────────────────────────

def _atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR14（TR 滚动均值），与 jarvis_twelve_systems._atr 同口径的本地实现。"""
    high, low, close = df["high"], df["low"], df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def _swing_points(df: pd.DataFrame, window: int = 5) -> tuple[list[int], list[int]]:
    """swing 高/低点下标（window 根内局部极值；同口径独立实现，不改 twelve 文件）。"""
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    n = len(df)
    for i in range(window, n - window):
        if h[i] == max(h[i - window: i + window + 1]):
            if not highs or highs[-1] != i - 1 or h[i] != h[i - 1]:
                highs.append(i)
        if l[i] == min(l[i - window: i + window + 1]):
            if not lows or lows[-1] != i - 1 or l[i] != l[i - 1]:
                lows.append(i)
    return highs, lows


def _round_price(v: float) -> float:
    """价格收敛：≥1 保留 6 位小数；<1 按 6 位有效数字（微价资产不归零）。"""
    if not math.isfinite(v) or v == 0:
        return 0.0
    if abs(v) >= 1:
        return round(v, 6)
    digits = 6 - int(math.floor(math.log10(abs(v)))) - 1
    return round(v, digits)


def _valid_df(df) -> str | None:
    """输入校验：返回不合格原因；None=合格。"""
    if df is None or not isinstance(df, pd.DataFrame):
        return "输入不是 DataFrame"
    if any(c not in df.columns for c in _REQUIRED_COLS):
        missing = [c for c in _REQUIRED_COLS if c not in df.columns]
        return f"缺少必需列 {missing}"
    if len(df) < MIN_BARS:
        return f"数据不足（{len(df)} 根 < {MIN_BARS}）"
    return None


# ─────────────────────────── FVG 检测 ───────────────────────────

def detect_fvg(df: pd.DataFrame, *, min_atr_mult: float = MIN_GAP_ATR_MULT,
               max_zones: int = 10) -> list[dict]:
    """扫描全段 K 线的 FVG 区，返回排序后的 zone 列表（纯函数）。

    每个 zone：
      {type: bullish|bearish, top, bottom, height, height_atr,
       created_i（第 3 根 bar 的位置下标）, age_bars（距最新 bar 根数）,
       mitigated（完全回补=缺口失效）, fill_pct（最深回踩占缺口高度 0~100）,
       dist_pct / dist_atr（现价到缺口最近边界的距离标注，ATR 归一）}
    排序：未回补优先，其后按 age_bars 升序（越新鲜越靠前）；截断 max_zones。
    数据不合格返回 []（详细原因走 detect() 的 ok/reason）。
    """
    if _valid_df(df) is not None:
        return []
    atr = _atr_series(df)
    n = len(df)
    price = float(df["close"].iloc[-1])
    atr_now = float(atr.iloc[-1])
    highs, lows = df["high"].values, df["low"].values
    zones: list[dict] = []
    for i in range(2, n):
        atr_i = float(atr.iloc[i])
        if not (math.isfinite(atr_i) and atr_i > 0):
            continue
        h1, l1 = float(highs[i - 2]), float(lows[i - 2])
        h3, l3 = float(highs[i]), float(lows[i])
        zone = None
        if l3 > h1 and (l3 - h1) >= min_atr_mult * atr_i:      # 看涨 FVG
            zone = {"type": "bullish", "top": l3, "bottom": h1}
        elif h3 < l1 and (l1 - h3) >= min_atr_mult * atr_i:    # 看跌 FVG（镜像）
            zone = {"type": "bearish", "top": l1, "bottom": h3}
        if zone is None:
            continue
        top, bottom = zone["top"], zone["bottom"]
        height = top - bottom
        # 回补度量：创建后（i 之后的 bar）价格回到缺口区的最深穿透
        fill = 0.0
        if i + 1 < n:
            if zone["type"] == "bullish":
                revisit_low = float(df["low"].iloc[i + 1:].min())
                fill = (top - revisit_low) / height if revisit_low < top else 0.0
            else:
                revisit_high = float(df["high"].iloc[i + 1:].max())
                fill = (revisit_high - bottom) / height if revisit_high > bottom else 0.0
        fill_pct = round(min(1.0, max(0.0, fill)) * 100, 1)
        mitigated = fill >= 1.0
        # 现价到缺口的距离：在缺口内=0；否则到最近边界
        if bottom <= price <= top:
            dist = 0.0
        else:
            dist = min(abs(price - top), abs(price - bottom))
        zones.append({
            "type": zone["type"],
            "top": _round_price(top),
            "bottom": _round_price(bottom),
            "height": _round_price(height),
            "height_atr": round(height / atr_i, 3),
            "created_i": int(i),
            "age_bars": int(n - 1 - i),
            "mitigated": bool(mitigated),
            "fill_pct": fill_pct,
            "dist_pct": round(dist / price * 100, 4) if price > 0 else None,
            "dist_atr": round(dist / atr_now, 3) if atr_now > 0 else None,
        })
    zones.sort(key=lambda z: (z["mitigated"], z["age_bars"]))
    return zones[: max(1, int(max_zones))]


# ─────────────────────────── swing 支撑/压力汇总 ───────────────────────────

def swing_levels(df: pd.DataFrame, *, window: int = 5,
                 max_levels: int = SR_MAX_LEVELS) -> dict:
    """swing 高/低点聚簇成支撑/压力汇总（纯函数）。

    返回 {supports: [...], resistances: [...], nearest_support, nearest_resistance}；
    每条 {price, touches, dist_pct, dist_atr}。supports=现价下方（含簇价==现价），
    resistances=现价上方；按距现价由近到远排序，各截断 max_levels。
    数据不合格返回空结构。
    """
    empty = {"supports": [], "resistances": [],
             "nearest_support": None, "nearest_resistance": None}
    if _valid_df(df) is not None:
        return empty
    atr_now = float(_atr_series(df).iloc[-1])
    if not (math.isfinite(atr_now) and atr_now > 0):
        return empty
    price = float(df["close"].iloc[-1])
    highs_i, lows_i = _swing_points(df, window=window)
    pts = sorted([float(df["high"].iloc[i]) for i in highs_i]
                 + [float(df["low"].iloc[i]) for i in lows_i])
    # 相邻价位 ≤ CLUSTER_ATR_MULT×ATR 归入同簇；簇价取均值、触碰数=成员数
    clusters: list[dict] = []
    for p in pts:
        if clusters and p - clusters[-1]["_hi"] <= CLUSTER_ATR_MULT * atr_now:
            c = clusters[-1]
            c["_members"].append(p)
            c["_hi"] = p
        else:
            clusters.append({"_members": [p], "_hi": p})
    levels = []
    for c in clusters:
        cp = sum(c["_members"]) / len(c["_members"])
        dist = abs(price - cp)
        levels.append({"price": _round_price(cp), "touches": len(c["_members"]),
                       "dist_pct": round(dist / price * 100, 4) if price > 0 else None,
                       "dist_atr": round(dist / atr_now, 3)})
    supports = sorted([lv for lv in levels if lv["price"] <= price],
                      key=lambda lv: lv["dist_atr"])[: max_levels]
    resistances = sorted([lv for lv in levels if lv["price"] > price],
                         key=lambda lv: lv["dist_atr"])[: max_levels]
    return {
        "supports": supports,
        "resistances": resistances,
        "nearest_support": supports[0] if supports else None,
        "nearest_resistance": resistances[0] if resistances else None,
    }


# ─────────────────────────── [N2] Premium/Discount 折价溢价区 ───────────────────────────

def premium_discount(df: pd.DataFrame, *, lookback: int = PD_LOOKBACK) -> dict:
    """SMC 折价/溢价区分类（纯函数；波段口径见模块头预登记）。

    返回 {ok, reason, range_high, range_low, equilibrium, price, pos_pct, zone,
    lookback_bars}；zone ∈ premium|discount|equilibrium（45~55 分位=均衡带）。
    数据不合格 / 波段高度 < 0.5×ATR → ok=False + reason，其余键为 None。
    """
    empty = {"ok": False, "reason": None, "range_high": None, "range_low": None,
             "equilibrium": None, "price": None, "pos_pct": None, "zone": None,
             "lookback_bars": None}
    reason = _valid_df(df)
    if reason is not None:
        return {**empty, "reason": reason}
    try:
        look = df.iloc[-max(MIN_BARS, int(lookback)):]
        atr_now = float(_atr_series(df).iloc[-1])
        if not (math.isfinite(atr_now) and atr_now > 0):
            return {**empty, "reason": "ATR 无效，无法评估波段有效性"}
        price = float(df["close"].iloc[-1])
        highs_i, lows_i = _swing_points(look, window=5)
        if highs_i and lows_i:
            range_high = max(float(look["high"].iloc[i]) for i in highs_i)
            range_low = min(float(look["low"].iloc[i]) for i in lows_i)
        else:  # swing 点不足：退化为窗口原始极值（预登记兜底口径）
            range_high = float(look["high"].max())
            range_low = float(look["low"].min())
        height = range_high - range_low
        if height < PD_MIN_RANGE_ATR * atr_now:
            return {**empty, "reason": (f"波段高度 {height:.6g} < "
                                        f"{PD_MIN_RANGE_ATR}×ATR，无显著 dealing range")}
        pos = (price - range_low) / height * 100.0
        pos_pct = round(min(100.0, max(0.0, pos)), 2)
        lo_b, hi_b = PD_EQ_BAND
        zone = ("equilibrium" if lo_b <= pos_pct <= hi_b
                else "premium" if pos_pct > hi_b else "discount")
        return {"ok": True, "reason": None,
                "range_high": _round_price(range_high),
                "range_low": _round_price(range_low),
                "equilibrium": _round_price((range_high + range_low) / 2),
                "price": _round_price(price), "pos_pct": pos_pct, "zone": zone,
                "lookback_bars": int(len(look))}
    except Exception as exc:  # noqa: BLE001 — 证据模块绝不拖垮消费方
        return {**empty, "reason": f"premium_discount 异常降级: {repr(exc)[:100]}"}


_PD_ZONE_CN = {"premium": "溢价区", "discount": "折价区", "equilibrium": "均衡带"}


def premium_discount_judge(pd_out: dict | None, plan_dir: str) -> dict | None:
    """导师证据项判定（任务 N2）：做多在溢价/做空在折价 → warn，反之 pass。

    Args:
        pd_out: premium_discount() 输出（或 detect()['premium_discount']）
        plan_dir: long / short
    返回 {level: warn|pass, evidence, detail, raw}——由 jarvis_trade_mentor 以
    key='premium_discount'（WEIGHTS 无此键 → weight=0）挂进 items：纯人话提示，
    不改分数。数据缺失 / 方向非法返回 None（证据不可用不硬造）。
    """
    if not isinstance(pd_out, dict) or not pd_out.get("ok") \
            or plan_dir not in ("long", "short"):
        return None
    zone, pos = pd_out["zone"], pd_out["pos_pct"]
    zone_cn = _PD_ZONE_CN.get(zone, zone)
    base = (f"现价处于 dealing range {zone_cn} {pos:.0f}% 分位"
            f"（区间 [{pd_out['range_low']}, {pd_out['range_high']}]，"
            f"均衡价 {pd_out['equilibrium']}）")
    detail = f"{zone_cn} {pos:.0f}%"
    raw = {k: pd_out[k] for k in ("zone", "pos_pct", "range_high", "range_low",
                                  "equilibrium")}
    counter = (plan_dir == "long" and zone == "premium") or \
              (plan_dir == "short" and zone == "discount")
    if counter:
        advice = ("做多等于追高，回落到折价区/均衡价再考虑低吸"
                  if plan_dir == "long" else
                  "做空等于杀跌，反弹到溢价区/均衡价再考虑高空")
        return {"level": "warn", "evidence": base + f"——{advice}", "detail": detail,
                "raw": raw}
    if zone == "equilibrium":
        return {"level": "pass", "evidence": base + "——处于均衡带，价格位置不加分不减分",
                "detail": detail, "raw": raw}
    good = ("折价区低吸，入场位置站在便宜的一侧" if plan_dir == "long"
            else "溢价区高空，入场位置站在贵的一侧")
    return {"level": "pass", "evidence": base + f"——{good}", "detail": detail,
            "raw": raw}


# ─────────────────────────── 聚合契约入口（agent-8 对接面） ───────────────────────────

def detect(df: pd.DataFrame, max_zones: int = 10) -> dict:
    """FVG + swing 支撑/压力 + 折价溢价区 一步到位的聚合契约（对接面）。

    签名固定：detect(df, max_zones=10)。返回：
      {ok: bool, reason: str|None,           # 数据不合格时 ok=False + 原因
       price: float, atr: float,             # 现价 / ATR14 绝对值
       zones: [...],                          # detect_fvg 输出（未回补优先、新鲜优先）
       sr: {...},                             # swing_levels 输出
       nearest_fvg: {...}|None,               # 最近的未回补 FVG（按 dist_atr）
       premium_discount: {...},               # [N2] 折价溢价区（R8 渲染均衡线/着色）
       summary: str}                          # 一句话小白话摘要
    纯函数、不联网、绝不抛出（异常降级为 ok=False）。
    """
    _empty_pd = {"ok": False, "reason": "未执行", "range_high": None,
                 "range_low": None, "equilibrium": None, "price": None,
                 "pos_pct": None, "zone": None, "lookback_bars": None}
    try:
        reason = _valid_df(df)
        if reason is not None:
            return {"ok": False, "reason": reason, "price": None, "atr": None,
                    "zones": [], "sr": {"supports": [], "resistances": [],
                                        "nearest_support": None,
                                        "nearest_resistance": None},
                    "nearest_fvg": None,
                    "premium_discount": {**_empty_pd, "reason": reason},
                    "summary": f"FVG 检测未执行：{reason}"}
        price = float(df["close"].iloc[-1])
        atr_now = float(_atr_series(df).iloc[-1])
        zones = detect_fvg(df, max_zones=max_zones)
        sr = swing_levels(df)
        pd_out = premium_discount(df)
        open_zones = [z for z in zones if not z["mitigated"]]
        nearest_fvg = min(open_zones, key=lambda z: z["dist_atr"] or 0.0) \
            if open_zones else None
        n_bull = sum(1 for z in open_zones if z["type"] == "bullish")
        n_bear = len(open_zones) - n_bull
        parts = [f"未回补 FVG {len(open_zones)} 个（看涨 {n_bull}/看跌 {n_bear}）"]
        if nearest_fvg is not None:
            side_cn = "看涨（下方支撑型）" if nearest_fvg["type"] == "bullish" \
                else "看跌（上方压力型）"
            parts.append(
                f"最近一个为{side_cn} [{nearest_fvg['bottom']}, {nearest_fvg['top']}]"
                f"，距现价 {nearest_fvg['dist_atr']}×ATR")
        ns, nr = sr.get("nearest_support"), sr.get("nearest_resistance")
        if ns:
            parts.append(f"最近支撑 {ns['price']}（{ns['dist_atr']}×ATR）")
        if nr:
            parts.append(f"最近压力 {nr['price']}（{nr['dist_atr']}×ATR）")
        if pd_out.get("ok"):
            parts.append(f"现价处于 {_PD_ZONE_CN.get(pd_out['zone'], pd_out['zone'])} "
                         f"{pd_out['pos_pct']:.0f}% 分位")
        return {"ok": True, "reason": None, "price": _round_price(price),
                "atr": _round_price(atr_now), "zones": zones, "sr": sr,
                "nearest_fvg": nearest_fvg, "premium_discount": pd_out,
                "summary": "；".join(parts)}
    except Exception as exc:  # noqa: BLE001 — 证据模块绝不拖垮导师主链
        return {"ok": False, "reason": f"FVG 检测异常降级: {repr(exc)[:120]}",
                "price": None, "atr": None, "zones": [],
                "sr": {"supports": [], "resistances": [],
                       "nearest_support": None, "nearest_resistance": None},
                "nearest_fvg": None, "premium_discount": _empty_pd,
                "summary": "FVG 检测异常，本次不提供缺口证据"}
