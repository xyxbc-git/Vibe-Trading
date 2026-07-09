#!/usr/bin/env python3
"""贾维斯 JARVIS — 「十二套技术」信号引擎。

把《十二套技术.md》中可量化的体系实现为纯函数信号器，每套输出统一结构：
  {system, name_cn, direction: bullish|bearish|neutral, strength: 0~1,
   reasoning, key_levels: [{label, price}]}

十二套体系（md 章节 → 信号器）：
   1 海龟       turtle          20日高/低突破 + 10日退出位 + ATR 止损位
   2 道氏       dow             swing 高低点结构判趋势
   3 艾略特     elliott         简化：fib 0.382/0.5/0.618 回撤位
   4 波动率     volatility      ATR/布林带宽历史分位
   5 江恩       gann            简化：斐波那契时间窗口标记
   6 缠论       chanlun         简化：分型→笔→中枢近似 + 三类买卖点近似
   7 123法则    rule123         趋势线破坏+不创新低+破反弹高三步
   8 跳空       gap             缺口检测与回补状态
   9 马丁格尔   martingale      数据不足则 neutral + reasoning 说明（不硬造）
  10 摆动震荡   oscillator      RSI/KDJ 超买超卖
  11 三重平滑RSI triple_rsi     三级平滑 + 金叉/背离
  12 套利       arbitrage       数据不足则 neutral + reasoning 说明（不硬造）

同文件实现分层共识融合 consensus()（照 md 第七节：道氏顶层过滤 → 主线策略 →
辅助共振），输出 {direction, confidence 0~1, score, votes, layers, reasoning, key_levels}。

纯函数、不联网：所有信号器只吃 DataFrame(open/high/low/close/volume)。
注意：输入若含未收盘的最新 bar，信号与点位会随该 bar 实时变化而重绘（实盘常规
行为）；需要稳定信号请在上游传入已收盘 K 线。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

DIRECTIONS = ("bullish", "bearish", "neutral")
ENTRY_TYPES = ("breakout", "pullback", "market")

# ═══════════════════════════ 公共指标 ═══════════════════════════


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(period, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.fillna(50.0)


def _kdj(df: pd.DataFrame, n: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    low_n = df["low"].rolling(n, min_periods=1).min()
    high_n = df["high"].rolling(n, min_periods=1).max()
    denom = (high_n - low_n).replace(0, np.nan)
    rsv = ((df["close"] - low_n) / denom * 100).fillna(50.0)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def _swing_points(df: pd.DataFrame, window: int = 5) -> tuple[list[int], list[int]]:
    """返回 (swing 高点下标列表, swing 低点下标列表)。window 根内为局部极值。"""
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


def _sig(system: str, name_cn: str, direction: str, strength: float,
         reasoning: str, key_levels: list[dict] | None = None,
         trade_plan: dict | None = None) -> dict:
    """统一信号结构（direction/strength 越界自动收敛；trade_plan 可选，缺依据为 None）。"""
    if direction not in DIRECTIONS:
        direction = "neutral"
    if direction == "neutral":
        trade_plan = None   # 不变量：中性信号绝不携带交易计划
    return {
        "system": system,
        "name_cn": name_cn,
        "direction": direction,
        "strength": round(float(max(0.0, min(1.0, strength))), 3),
        "reasoning": reasoning,
        "key_levels": key_levels or [],
        "trade_plan": trade_plan,
    }


def _round_price(v: float) -> float:
    """价格收敛：≥1 保留 6 位小数；<1 按 6 位有效数字（微价资产 PEPE 类不归零）。"""
    if not math.isfinite(v) or v == 0:
        return 0.0
    if abs(v) >= 1:
        return round(v, 6)
    digits = 6 - int(math.floor(math.log10(abs(v)))) - 1
    return round(v, digits)


def _plan(direction: str, entry: float, entry_type: str, stop_loss: float,
          take_profit: float, note: str) -> dict | None:
    """构造 trade_plan 并强校验方向自洽：多单 SL<entry<TP，空单 TP<entry<SL。

    不自洽 / 非有限值 → 返回 None（宁缺毋滥，不硬造点位）。
    """
    try:
        e, sl, tp = float(entry), float(stop_loss), float(take_profit)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) and x > 0 for x in (e, sl, tp)):
        return None
    if direction == "bullish" and not (sl < e < tp):
        return None
    if direction == "bearish" and not (tp < e < sl):
        return None
    if direction not in ("bullish", "bearish"):
        return None
    risk = abs(e - sl)
    if risk <= 0:
        return None
    rr = round(abs(tp - e) / risk, 2)
    return {
        # 显式多空标识（long=做多/short=做空），消除前端只看三个价位时的方向歧义
        "side": "long" if direction == "bullish" else "short",
        "entry": _round_price(e),
        "entry_type": entry_type if entry_type in ENTRY_TYPES else "market",
        "stop_loss": _round_price(sl),
        "take_profit": _round_price(tp),
        "rr": rr,
        "note": note,
    }


def _insufficient(system: str, name_cn: str, why: str) -> dict:
    return _sig(system, name_cn, "neutral", 0.0, why)


def _lv(label: str, price: float) -> dict:
    return {"label": label, "price": _round_price(float(price))}


MIN_BARS = 30


# ═══════════════════════════ 1. 海龟 ═══════════════════════════

def signal_turtle(df: pd.DataFrame) -> dict:
    """20日高/低突破入场 + 10日反向极值退出 + ATR 止损。"""
    name = ("turtle", "海龟交易")
    if len(df) < MIN_BARS:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < {MIN_BARS}）")
    close = float(df["close"].iloc[-1])
    # 突破口径：当前 bar 相对「之前 20 根」的极值（不含自身，避免自指）
    hh20 = float(df["high"].iloc[-21:-1].max())
    ll20 = float(df["low"].iloc[-21:-1].min())
    exit_low10 = float(df["low"].iloc[-11:-1].min())
    exit_high10 = float(df["high"].iloc[-11:-1].max())
    atr = float(_atr(df).iloc[-1])

    levels = [
        _lv("20日突破位(多)", hh20),
        _lv("20日突破位(空)", ll20),
    ]
    if close > hh20:
        stop = close - 2 * atr
        levels += [_lv("10日退出位", exit_low10), _lv("ATR止损(2x)", stop)]
        margin = (close - hh20) / max(atr, 1e-9)
        plan = _plan("bullish", hh20, "breakout",
                     hh20 - 2 * atr, hh20 + 4 * atr,
                     f"20日高突破入场；SL=entry-2xATR；TP=entry+2倍风险；"
                     f"另有10日低点 {exit_low10:.2f} 动态退出（以先到者为准）")
        return _sig(*name, "bullish", min(1.0, 0.5 + margin * 0.25),
                    f"价格 {close:.2f} 突破20日高点 {hh20:.2f}（超出 {margin:.2f} ATR），"
                    f"顺势做多；退出参考10日低点 {exit_low10:.2f}，止损 {stop:.2f}（2xATR）",
                    levels, trade_plan=plan)
    if close < ll20:
        stop = close + 2 * atr
        levels += [_lv("10日退出位", exit_high10), _lv("ATR止损(2x)", stop)]
        margin = (ll20 - close) / max(atr, 1e-9)
        plan = _plan("bearish", ll20, "breakout",
                     ll20 + 2 * atr, ll20 - 4 * atr,
                     f"20日低跌破入场；SL=entry+2xATR；TP=entry-2倍风险；"
                     f"另有10日高点 {exit_high10:.2f} 动态退出（以先到者为准）")
        return _sig(*name, "bearish", min(1.0, 0.5 + margin * 0.25),
                    f"价格 {close:.2f} 跌破20日低点 {ll20:.2f}（超出 {margin:.2f} ATR），"
                    f"顺势做空；退出参考10日高点 {exit_high10:.2f}，止损 {stop:.2f}（2xATR）",
                    levels, trade_plan=plan)
    pos = (close - ll20) / max(hh20 - ll20, 1e-9)
    return _sig(*name, "neutral", 0.2,
                f"价格 {close:.2f} 处于20日区间 [{ll20:.2f}, {hh20:.2f}] 内（{pos:.0%} 分位），"
                "未触发突破，观望等待", levels)


# ═══════════════════════════ 2. 道氏 ═══════════════════════════

def signal_dow(df: pd.DataFrame) -> dict:
    """swing 高低点结构：高低点逐级抬高=多头；逐级降低=空头。"""
    name = ("dow", "道氏理论")
    if len(df) < MIN_BARS:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < {MIN_BARS}）")
    highs_i, lows_i = _swing_points(df, window=5)
    if len(highs_i) < 2 or len(lows_i) < 2:
        return _insufficient(*name, "swing 高低点不足（<2 个），结构无法判定")
    h_vals = [float(df["high"].iloc[i]) for i in highs_i[-3:]]
    l_vals = [float(df["low"].iloc[i]) for i in lows_i[-3:]]
    hh = all(h_vals[i] < h_vals[i + 1] for i in range(len(h_vals) - 1))  # higher highs
    hl = all(l_vals[i] < l_vals[i + 1] for i in range(len(l_vals) - 1))  # higher lows
    lh = all(h_vals[i] > h_vals[i + 1] for i in range(len(h_vals) - 1))  # lower highs
    ll = all(l_vals[i] > l_vals[i + 1] for i in range(len(l_vals) - 1))  # lower lows
    levels = [_lv("最近swing高点", h_vals[-1]), _lv("最近swing低点", l_vals[-1])]
    n_struct = min(len(h_vals), len(l_vals))
    close = float(df["close"].iloc[-1])
    atr = float(_atr(df).iloc[-1])
    # 点位：entry=现价（趋势确认 market），SL=最近 swing 低/高点外侧 0.5xATR，TP=前高/前低。
    # 前高已被突破（TP≤entry）时 _plan 自洽校验返回 None——不硬造目标位。
    plan_bull = _plan("bullish", close, "market",
                      l_vals[-1] - 0.5 * atr, h_vals[-1],
                      "道氏趋势确认现价入场；SL=最近swing低点下方0.5xATR；TP=前高")
    plan_bear = _plan("bearish", close, "market",
                      h_vals[-1] + 0.5 * atr, l_vals[-1],
                      "道氏趋势确认现价入场；SL=最近swing高点上方0.5xATR；TP=前低")
    if hh and hl:
        return _sig(*name, "bullish", min(1.0, 0.5 + 0.15 * n_struct),
                    f"高低点逐级抬高（近{len(h_vals)}个高点、{len(l_vals)}个低点均上移），"
                    "主趋势多头", levels, trade_plan=plan_bull)
    if lh and ll:
        return _sig(*name, "bearish", min(1.0, 0.5 + 0.15 * n_struct),
                    f"高低点逐级降低（近{len(h_vals)}个高点、{len(l_vals)}个低点均下移），"
                    "主趋势空头", levels, trade_plan=plan_bear)
    if hh or hl:
        return _sig(*name, "bullish", 0.35, "高点或低点单边抬高，结构偏多但未完全确认",
                    levels, trade_plan=plan_bull)
    if lh or ll:
        return _sig(*name, "bearish", 0.35, "高点或低点单边降低，结构偏空但未完全确认",
                    levels, trade_plan=plan_bear)
    return _sig(*name, "neutral", 0.2, "swing 高低点交织，无明确趋势结构", levels)


# ═══════════════════════════ 3. 艾略特（简化） ═══════════════════════════

def signal_elliott(df: pd.DataFrame) -> dict:
    """简化：取最近一段主要波段，标注 fib 0.382/0.5/0.618 回撤位，按现价位置给方向。"""
    name = ("elliott", "艾略特波浪")
    if len(df) < MIN_BARS:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < {MIN_BARS}）")
    look = df.iloc[-120:] if len(df) >= 120 else df
    hi_pos = int(look["high"].values.argmax())
    lo_pos = int(look["low"].values.argmin())
    hi = float(look["high"].iloc[hi_pos])
    lo = float(look["low"].iloc[lo_pos])
    close = float(df["close"].iloc[-1])
    if hi - lo < 1e-9:
        return _insufficient(*name, "波段振幅为零，无法计算回撤位")
    up_leg = lo_pos < hi_pos  # 低点在前 → 上行主浪，回撤自上向下量
    if up_leg:
        f382 = hi - (hi - lo) * 0.382
        f500 = hi - (hi - lo) * 0.5
        f618 = hi - (hi - lo) * 0.618
    else:
        f382 = lo + (hi - lo) * 0.382
        f500 = lo + (hi - lo) * 0.5
        f618 = lo + (hi - lo) * 0.618
    levels = [_lv("fib 0.382", f382), _lv("fib 0.5", f500), _lv("fib 0.618", f618),
              _lv("波段高点", hi), _lv("波段低点", lo)]
    if up_leg:
        if close >= f382:
            plan = _plan("bullish", f382, "pullback", f500, hi,
                         "回踩 fib0.382 低吸；SL=下一档 fib0.5；TP=波段前高")
            return _sig(*name, "bullish", 0.55,
                        f"上行主浪后回撤未破 0.382（{f382:.2f}），浪型结构偏多，"
                        f"回踩 fib 支撑区可视为低吸参考", levels, trade_plan=plan)
        if close >= f618:
            return _sig(*name, "neutral", 0.35,
                        f"回撤进入 0.382~0.618（{f618:.2f}~{f382:.2f}）黄金分割区，多空转换观察区", levels)
        plan = _plan("bearish", close, "market", f618, lo,
                     "跌破 fib0.618 浪型破坏顺势空；SL=收复 0.618 即离场；TP=波段前低")
        return _sig(*name, "bearish", 0.5,
                    f"回撤跌破 0.618（{f618:.2f}），上行浪型大概率破坏，偏空", levels,
                    trade_plan=plan)
    # 下行主浪：反弹幅度衡量
    if close <= f382:
        plan = _plan("bearish", f382, "pullback", f500, lo,
                     "反弹至 fib0.382 承压做空；SL=下一档 fib0.5；TP=波段前低")
        return _sig(*name, "bearish", 0.55,
                    f"下行主浪后反弹未过 0.382（{f382:.2f}），浪型结构偏空", levels,
                    trade_plan=plan)
    if close <= f618:
        return _sig(*name, "neutral", 0.35,
                    f"反弹进入 0.382~0.618（{f382:.2f}~{f618:.2f}）区间，方向待确认", levels)
    plan = _plan("bullish", close, "market", f618, hi,
                 "收复 fib0.618 浪型反转做多；SL=跌回 0.618 即离场；TP=波段前高")
    return _sig(*name, "bullish", 0.5,
                f"反弹收复 0.618（{f618:.2f}），下行浪型大概率破坏，偏多", levels,
                trade_plan=plan)


# ═══════════════════════════ 4. 波动率 ═══════════════════════════

def signal_volatility(df: pd.DataFrame) -> dict:
    """ATR 与布林带宽的历史分位：极低=酝酿突破（做多波动率），极高=均值回归。"""
    name = ("volatility", "波动率系统")
    if len(df) < MIN_BARS:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < {MIN_BARS}）")
    close = df["close"]
    atr_pct = (_atr(df) / close.replace(0, np.nan) * 100).dropna()
    ma20 = close.rolling(20, min_periods=1).mean()
    std20 = close.rolling(20, min_periods=1).std().fillna(0)
    bw = ((ma20 + 2 * std20) - (ma20 - 2 * std20)) / ma20.replace(0, np.nan) * 100
    bw = bw.dropna()
    if len(atr_pct) < 20 or len(bw) < 20:
        return _insufficient(*name, "有效波动率样本不足")
    atr_rank = float((atr_pct < atr_pct.iloc[-1]).mean())
    bw_rank = float((bw < bw.iloc[-1]).mean())
    rank = (atr_rank + bw_rank) / 2
    reasoning = (f"ATR 分位 {atr_rank:.0%}、布林带宽分位 {bw_rank:.0%}"
                 f"（现值 ATR {atr_pct.iloc[-1]:.2f}%、带宽 {bw.iloc[-1]:.2f}%）")
    # 波动率体系本身不判涨跌，方向 neutral，强度表达「即将变盘/回归」的力度
    if rank <= 0.15:
        return _sig(*name, "neutral", min(1.0, 0.5 + (0.15 - rank) * 3),
                    reasoning + " → 波动率历史极低，酝酿方向性突破（做多波动率窗口）")
    if rank >= 0.85:
        return _sig(*name, "neutral", min(1.0, 0.5 + (rank - 0.85) * 3),
                    reasoning + " → 波动率历史极高，警惕波动收缩与行情反转（做空波动率窗口）")
    return _sig(*name, "neutral", 0.2, reasoning + " → 波动率处于中性区间")


# ═══════════════════════════ 5. 江恩（简化） ═══════════════════════════

_FIB_WINDOWS = (8, 13, 21, 34, 55, 89, 144)


def signal_gann(df: pd.DataFrame) -> dict:
    """简化：自最近显著高/低点起数斐波那契根数，当前 bar 落在窗口（±1 根）视为变盘敏感期。"""
    name = ("gann", "江恩时间窗")
    if len(df) < MIN_BARS:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < {MIN_BARS}）")
    look = df.iloc[-160:] if len(df) >= 160 else df
    hi_pos = int(look["high"].values.argmax())
    lo_pos = int(look["low"].values.argmin())
    last = len(look) - 1
    bars_from_hi = last - hi_pos
    bars_from_lo = last - lo_pos
    hits = []
    for base, bars in (("高点", bars_from_hi), ("低点", bars_from_lo)):
        for w in _FIB_WINDOWS:
            if abs(bars - w) <= 1:
                hits.append(f"距显著{base} {bars} 根 ≈ 斐波那契 {w}")
    levels = [_lv("显著高点", float(look["high"].iloc[hi_pos])),
              _lv("显著低点", float(look["low"].iloc[lo_pos]))]
    if hits:
        # 时间窗只提示「变盘敏感」，方向交由现价相对高低点位置微调
        close = float(df["close"].iloc[-1])
        mid = (float(look["high"].iloc[hi_pos]) + float(look["low"].iloc[lo_pos])) / 2
        direction = "bullish" if close < mid else "bearish"  # 敏感窗倾向均值回归
        return _sig(*name, direction, 0.4,
                    "；".join(hits) + " → 处于斐波那契时间窗口（±1根），变盘概率升高；"
                    + ("现价靠近波段低位，反转偏向上" if direction == "bullish"
                       else "现价靠近波段高位，反转偏向下"),
                    levels)
    nxt = min((w - bars_from_lo for w in _FIB_WINDOWS if w > bars_from_lo), default=None)
    extra = f"，距下一低点时间窗还有 {nxt} 根" if nxt is not None else ""
    return _sig(*name, "neutral", 0.15,
                f"距显著高点 {bars_from_hi} 根 / 低点 {bars_from_lo} 根，均不在斐波那契窗口{extra}",
                levels)


# ═══════════════════════════ 6. 缠论（简化） ═══════════════════════════

def _fractals(df: pd.DataFrame) -> list[dict]:
    """顶/底分型序列（简化：严格三根，中间为极值）。"""
    out = []
    h, l = df["high"].values, df["low"].values
    for i in range(1, len(df) - 1):
        if h[i] > h[i - 1] and h[i] > h[i + 1]:
            out.append({"i": i, "type": "top", "price": float(h[i])})
        elif l[i] < l[i - 1] and l[i] < l[i + 1]:
            out.append({"i": i, "type": "bottom", "price": float(l[i])})
    return out


def _strokes(fractals: list[dict], min_gap: int = 4) -> list[dict]:
    """分型 → 笔（简化）：相邻异型分型间隔 ≥ min_gap 根成一笔；同型取更极端者。"""
    strokes: list[dict] = []
    last = None
    for f in fractals:
        if last is None:
            last = f
            continue
        if f["type"] == last["type"]:
            better = (f["price"] > last["price"]) if f["type"] == "top" else (f["price"] < last["price"])
            if better:
                last = f
            continue
        if f["i"] - last["i"] >= min_gap:
            strokes.append({"from": last, "to": f,
                            "dir": "up" if f["type"] == "top" else "down"})
            last = f
    return strokes


def signal_chanlun(df: pd.DataFrame) -> dict:
    """简化缠论：分型→笔→中枢近似（最近三笔重叠区），三类买卖点近似判定。"""
    name = ("chanlun", "缠论")
    if len(df) < MIN_BARS:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < {MIN_BARS}）")
    frs = _fractals(df)
    strokes = _strokes(frs)
    if len(strokes) < 3:
        return _insufficient(*name, f"笔数量不足（{len(strokes)} < 3），无法构建中枢")
    s3 = strokes[-3:]
    close = float(df["close"].iloc[-1])
    # 中枢近似：最近三笔价格区间的重叠带
    zg = min(max(s["from"]["price"], s["to"]["price"]) for s in s3)   # 中枢上沿
    zd = max(min(s["from"]["price"], s["to"]["price"]) for s in s3)   # 中枢下沿
    levels = []
    if zg > zd:
        levels = [_lv("中枢上沿", zg), _lv("中枢下沿", zd)]
    last_stroke = strokes[-1]
    prev_same_dir = [s for s in strokes[:-1] if s["dir"] == last_stroke["dir"]]

    # 背离近似：同向最后两笔，后一笔斜率/幅度衰减
    def _mag(s: dict) -> float:
        return abs(s["to"]["price"] - s["from"]["price"])

    divergence = bool(prev_same_dir) and _mag(last_stroke) < _mag(prev_same_dir[-1]) * 0.7

    atr = float(_atr(df).iloc[-1])
    zone_h = zg - zd if zg > zd else 0.0
    if zg > zd:  # 有效中枢
        if close > zg:
            kind = "三买近似（突破中枢上沿后运行于其上）"
            plan = _plan("bullish", zg, "pullback", zd - 0.2 * atr, zg + zone_h,
                         "三买：回踩中枢上沿接多；SL=中枢下沿下方；TP=中枢测幅上翻")
            return _sig(*name, "bullish", 0.6 if not divergence else 0.4,
                        f"中枢 [{zd:.2f}, {zg:.2f}]，现价 {close:.2f} 站上中枢上沿 → {kind}"
                        + ("；但最后一笔力度衰减（背离迹象），强度打折" if divergence else ""),
                        levels, trade_plan=plan)
        if close < zd:
            kind = "三卖近似（跌破中枢下沿后运行于其下）"
            plan = _plan("bearish", zd, "pullback", zg + 0.2 * atr, zd - zone_h,
                         "三卖：反抽中枢下沿做空；SL=中枢上沿上方；TP=中枢测幅下翻")
            return _sig(*name, "bearish", 0.6 if not divergence else 0.4,
                        f"中枢 [{zd:.2f}, {zg:.2f}]，现价 {close:.2f} 跌破中枢下沿 → {kind}"
                        + ("；但最后一笔力度衰减（背离迹象），强度打折" if divergence else ""),
                        levels, trade_plan=plan)
        # 中枢内部：看最后一笔方向 + 背离 → 一买/一卖近似
        if divergence and last_stroke["dir"] == "down":
            plan = _plan("bullish", zd, "pullback", zd - 0.5 * atr, zg,
                         "一买近似：中枢下沿背离接多；SL=下沿下方0.5xATR；TP=中枢上沿")
            return _sig(*name, "bullish", 0.45,
                        f"中枢 [{zd:.2f}, {zg:.2f}] 内下跌笔力度衰减（背离）→ 一买近似，关注下沿支撑",
                        levels, trade_plan=plan)
        if divergence and last_stroke["dir"] == "up":
            plan = _plan("bearish", zg, "pullback", zg + 0.5 * atr, zd,
                         "一卖近似：中枢上沿背离做空；SL=上沿上方0.5xATR；TP=中枢下沿")
            return _sig(*name, "bearish", 0.45,
                        f"中枢 [{zd:.2f}, {zg:.2f}] 内上涨笔力度衰减（背离）→ 一卖近似，关注上沿压力",
                        levels, trade_plan=plan)
        return _sig(*name, "neutral", 0.25,
                    f"现价 {close:.2f} 位于中枢 [{zd:.2f}, {zg:.2f}] 内，等待方向选择", levels)
    # 无重叠中枢：以最后一笔方向为近似趋势
    d = "bullish" if last_stroke["dir"] == "up" else "bearish"
    return _sig(*name, d, 0.3,
                f"最近三笔无重叠中枢（趋势推进中），最后一笔方向 {'上' if d == 'bullish' else '下'}"
                + ("；力度衰减需防背离" if divergence else ""))


# ═══════════════════════════ 7. 123法则 ═══════════════════════════

def signal_rule123(df: pd.DataFrame) -> dict:
    """三步反转：①破趋势线（用 swing 点连线近似）②不创新低/高 ③破前反弹高/回调低。"""
    name = ("rule123", "123法则")
    if len(df) < MIN_BARS:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < {MIN_BARS}）")
    highs_i, lows_i = _swing_points(df, window=4)
    if len(highs_i) < 2 or len(lows_i) < 2:
        return _insufficient(*name, "swing 点不足，无法画趋势线")
    close = float(df["close"].iloc[-1])
    last = len(df) - 1

    def _trendline_val(i1: int, v1: float, i2: int, v2: float, at: int) -> float:
        if i2 == i1:
            return v2
        k = (v2 - v1) / (i2 - i1)
        return v2 + k * (at - i2)

    # 下降趋势反转检查（做多三步）
    h1, h2 = highs_i[-2], highs_i[-1]
    hv1, hv2 = float(df["high"].iloc[h1]), float(df["high"].iloc[h2])
    down_trend = hv2 < hv1
    steps_long = 0
    reason_l: list[str] = []
    rebound_high = None
    if down_trend:
        tl = _trendline_val(h1, hv1, h2, hv2, last)
        if close > tl:
            steps_long += 1
            reason_l.append(f"①收盘 {close:.2f} 上破下降趋势线 {tl:.2f}")
            lows_after = [i for i in lows_i if i > h2]
            if len(lows_after) >= 1:
                prior_low = float(df["low"].iloc[lows_after[0]])
                later_min = float(df["low"].iloc[lows_after[0]:].min())
                if later_min >= prior_low - 1e-9:
                    steps_long += 1
                    reason_l.append(f"②回调不创新低（守住 {prior_low:.2f}）")
                    hi_after = df["high"].iloc[lows_after[0]:last]
                    if len(hi_after) > 1:
                        rebound_high = float(hi_after.iloc[:-1].max())
                        if close > rebound_high:
                            steps_long += 1
                            reason_l.append(f"③突破反弹高点 {rebound_high:.2f}，反转确认")

    # 上升趋势反转检查（做空三步，对称）
    l1, l2 = lows_i[-2], lows_i[-1]
    lv1, lv2 = float(df["low"].iloc[l1]), float(df["low"].iloc[l2])
    up_trend = lv2 > lv1
    steps_short = 0
    reason_s: list[str] = []
    pullback_low = None
    if up_trend:
        tl = _trendline_val(l1, lv1, l2, lv2, last)
        if close < tl:
            steps_short += 1
            reason_s.append(f"①收盘 {close:.2f} 下破上升趋势线 {tl:.2f}")
            highs_after = [i for i in highs_i if i > l2]
            if len(highs_after) >= 1:
                prior_high = float(df["high"].iloc[highs_after[0]])
                later_max = float(df["high"].iloc[highs_after[0]:].max())
                if later_max <= prior_high + 1e-9:
                    steps_short += 1
                    reason_s.append(f"②反弹不创新高（未破 {prior_high:.2f}）")
                    lo_after = df["low"].iloc[highs_after[0]:last]
                    if len(lo_after) > 1:
                        pullback_low = float(lo_after.iloc[:-1].min())
                        if close < pullback_low:
                            steps_short += 1
                            reason_s.append(f"③跌破回调低点 {pullback_low:.2f}，反转确认")

    atr = float(_atr(df).iloc[-1])
    if steps_long > steps_short and steps_long > 0:
        levels = [_lv("反弹高点", rebound_high)] if rebound_high else []
        plan = None
        if steps_long >= 2 and rebound_high:
            stage_low = float(df["low"].iloc[max(0, h2):].min())  # 反转起点后的阶段最低
            sl = stage_low - 0.5 * atr
            risk = rebound_high - sl
            plan = _plan("bullish", rebound_high, "breakout", sl,
                         rebound_high + 1.75 * risk,
                         "123做多：突破反弹高点入场；SL=阶段最低点下方0.5xATR；TP=1.75R")
        return _sig(*name, "bullish" if steps_long >= 2 else "neutral",
                    (0.35, 0.6, 0.85)[steps_long - 1],
                    f"做多三步已完成 {steps_long}/3：" + "；".join(reason_l), levels,
                    trade_plan=plan)
    if steps_short > 0:
        levels = [_lv("回调低点", pullback_low)] if pullback_low else []
        plan = None
        if steps_short >= 2 and pullback_low:
            stage_high = float(df["high"].iloc[max(0, l2):].max())  # 反转起点后的阶段最高
            sl = stage_high + 0.5 * atr
            risk = sl - pullback_low
            plan = _plan("bearish", pullback_low, "breakout", sl,
                         pullback_low - 1.75 * risk,
                         "123做空：跌破回调低点入场；SL=阶段最高点上方0.5xATR；TP=1.75R")
        return _sig(*name, "bearish" if steps_short >= 2 else "neutral",
                    (0.35, 0.6, 0.85)[steps_short - 1],
                    f"做空三步已完成 {steps_short}/3：" + "；".join(reason_s), levels,
                    trade_plan=plan)
    return _sig(*name, "neutral", 0.15, "未出现趋势线破坏迹象，123 反转流程未启动")


# ═══════════════════════════ 8. 跳空 ═══════════════════════════

def signal_gap(df: pd.DataFrame) -> dict:
    """缺口检测与回补状态：向上缺口回踩不破做多；向下缺口反弹不过做空。"""
    name = ("gap", "跳空缺口")
    if len(df) < MIN_BARS:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < {MIN_BARS}）")
    look = min(len(df) - 1, 60)
    close = float(df["close"].iloc[-1])
    atr = float(_atr(df).iloc[-1])
    gaps = []
    for i in range(len(df) - look, len(df)):
        prev_h = float(df["high"].iloc[i - 1])
        prev_l = float(df["low"].iloc[i - 1])
        cur_l = float(df["low"].iloc[i])
        cur_h = float(df["high"].iloc[i])
        if cur_l > prev_h + 0.1 * atr:      # 向上缺口（留 0.1 ATR 噪声垫）
            filled = float(df["low"].iloc[i:].min()) <= prev_h
            gaps.append({"dir": "up", "top": cur_l, "bottom": prev_h, "i": i, "filled": filled})
        elif cur_h < prev_l - 0.1 * atr:    # 向下缺口
            filled = float(df["high"].iloc[i:].max()) >= prev_l
            gaps.append({"dir": "down", "top": prev_l, "bottom": cur_h, "i": i, "filled": filled})
    open_gaps = [g for g in gaps if not g["filled"]]
    if not open_gaps:
        n = len(gaps)
        return _sig(*name, "neutral", 0.1,
                    f"近 {look} 根内{'共 ' + str(n) + ' 个缺口均已回补' if n else '无跳空缺口'}，无未回补缺口牵引")
    g = open_gaps[-1]
    levels = [_lv("缺口上沿", g["top"]), _lv("缺口下沿", g["bottom"])]
    dist_bars = len(df) - 1 - g["i"]
    gap_h = g["top"] - g["bottom"]
    if g["dir"] == "up":
        if close >= g["bottom"]:
            plan = _plan("bullish", g["top"], "pullback", g["bottom"],
                         g["top"] + gap_h,
                         "回踩向上缺口上沿接多；SL=缺口下沿（回补=失效）；TP=缺口测幅上翻")
            return _sig(*name, "bullish", min(1.0, 0.45 + 0.05 * dist_bars),
                        f"向上缺口 [{g['bottom']:.2f}, {g['top']:.2f}] 未回补（{dist_bars} 根），"
                        "缺口上方运行=支撑有效，回踩缺口不破可做多", levels, trade_plan=plan)
        return _sig(*name, "bearish", 0.4,
                    f"价格已跌入向上缺口 [{g['bottom']:.2f}, {g['top']:.2f}] 内部，"
                    "回补进行中，支撑趋弱（回补中不给点位）", levels)
    if close <= g["top"]:
        plan = _plan("bearish", g["bottom"], "pullback", g["top"],
                     g["bottom"] - gap_h,
                     "反弹至向下缺口下沿承压做空；SL=缺口上沿（回补=失效）；TP=缺口测幅下翻")
        return _sig(*name, "bearish", min(1.0, 0.45 + 0.05 * dist_bars),
                    f"向下缺口 [{g['bottom']:.2f}, {g['top']:.2f}] 未回补（{dist_bars} 根），"
                    "缺口下方运行=压力有效，反弹承压缺口可做空", levels, trade_plan=plan)
    return _sig(*name, "bullish", 0.4,
                f"价格已涨入向下缺口 [{g['bottom']:.2f}, {g['top']:.2f}] 内部，"
                "回补进行中，压力趋弱（回补中不给点位）", levels)


# ═══════════════════════════ 9. 马丁格尔 ═══════════════════════════

def signal_martingale(df: pd.DataFrame, trade_history: list[dict] | None = None) -> dict:
    """马丁格尔是资金管理系统而非方向信号；无实盘连亏序列数据时输出 neutral 说明。"""
    name = ("martingale", "马丁格尔")
    if not trade_history:
        return _sig(*name, "neutral", 0.0,
                    "马丁格尔属于资金管理体系（亏损倍投、盈利重置），不产生方向信号；"
                    "当前无连续亏损序列数据输入，不硬造仓位建议。"
                    "仅提示：若在震荡子模块启用，必须锁死最大连亏次数与总浮亏红线")
    losses = 0
    for t in reversed(trade_history):
        if t.get("pnl", 0) < 0:
            losses += 1
        else:
            break
    if losses == 0:
        return _sig(*name, "neutral", 0.1, "最近一笔盈利，马丁序列重置为初始仓位（1x）")
    mult = 2 ** losses
    return _sig(*name, "neutral", min(1.0, 0.2 + losses * 0.15),
                f"当前连亏 {losses} 笔，按倍投规则下一仓位 {mult}x 初始仓；"
                f"⚠️ 风控铁律：连亏超限或总浮亏触红线必须停止倍投")


# ═══════════════════════════ 10. 摆动震荡 ═══════════════════════════

def signal_oscillator(df: pd.DataFrame) -> dict:
    """RSI/KDJ 超买超卖：RSI<30 或 KDJ<20 买入；RSI>70 或 KDJ>80 卖出。"""
    name = ("oscillator", "摆动震荡")
    if len(df) < MIN_BARS:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < {MIN_BARS}）")
    rsi = float(_rsi(df["close"]).iloc[-1])
    k, d, j = _kdj(df)
    kv, dv, jv = float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])
    detail = f"RSI={rsi:.1f}，KDJ K={kv:.1f}/D={dv:.1f}/J={jv:.1f}"
    votes_bull = (rsi < 30) + (kv < 20)
    votes_bear = (rsi > 70) + (kv > 80)
    close = float(df["close"].iloc[-1])
    atr = float(_atr(df).iloc[-1])
    range_hi = float(df["high"].iloc[-20:].max())
    range_lo = float(df["low"].iloc[-20:].min())
    # 点位：entry=现价，SL=近期 swing 外侧 1xATR，TP=区间对侧
    plan_bull = _plan("bullish", close, "market", range_lo - atr, range_hi,
                      "超卖均值回归做多；SL=近期低点外侧1xATR；TP=区间对侧（近期高点）")
    plan_bear = _plan("bearish", close, "market", range_hi + atr, range_lo,
                      "超买均值回归做空；SL=近期高点外侧1xATR；TP=区间对侧（近期低点）")
    if votes_bull:
        depth = max((30 - rsi) / 30 if rsi < 30 else 0, (20 - kv) / 20 if kv < 20 else 0)
        return _sig(*name, "bullish", min(1.0, 0.4 + votes_bull * 0.2 + depth * 0.3),
                    detail + f" → 超卖（{votes_bull}/2 指标命中），均值回归看反弹",
                    trade_plan=plan_bull)
    if votes_bear:
        depth = max((rsi - 70) / 30 if rsi > 70 else 0, (kv - 80) / 20 if kv > 80 else 0)
        return _sig(*name, "bearish", min(1.0, 0.4 + votes_bear * 0.2 + depth * 0.3),
                    detail + f" → 超买（{votes_bear}/2 指标命中），均值回归看回落",
                    trade_plan=plan_bear)
    lean = "bullish" if rsi < 45 and kv < dv else ("bearish" if rsi > 55 and kv > dv else "neutral")
    if lean != "neutral":
        return _sig(*name, lean, 0.2, detail + " → 未达超买超卖阈值，仅弱倾向",
                    trade_plan=plan_bull if lean == "bullish" else plan_bear)
    return _sig(*name, "neutral", 0.15, detail + " → 指标中性区间，观望")


# ═══════════════════════════ 11. 三重平滑 RSI ═══════════════════════════

def signal_triple_rsi(df: pd.DataFrame) -> dict:
    """布朗三重平滑 RSI：短→中→长逐级 EMA 平滑，金叉/死叉 + 顶底背离共振。"""
    name = ("triple_rsi", "三重平滑RSI")
    if len(df) < 60:
        return _insufficient(*name, f"数据不足（{len(df)} 根 < 60，三级平滑需要更长样本）")
    close = df["close"]
    raw = _rsi(close, 14)
    s1 = raw.ewm(span=5, adjust=False).mean()
    s2 = s1.ewm(span=8, adjust=False).mean()
    s3 = s2.ewm(span=13, adjust=False).mean()
    fast_now, slow_now = float(s2.iloc[-1]), float(s3.iloc[-1])
    fast_prev, slow_prev = float(s2.iloc[-2]), float(s3.iloc[-2])
    golden = fast_prev <= slow_prev and fast_now > slow_now
    death = fast_prev >= slow_prev and fast_now < slow_now

    # 背离：近 40 根，价格新低但平滑 RSI 抬高（底背离）/ 价格新高但 RSI 降低（顶背离）
    look = 40
    seg_c, seg_r = close.iloc[-look:], s2.iloc[-look:]
    half = look // 2
    bottom_div = (float(seg_c.iloc[half:].min()) < float(seg_c.iloc[:half].min())
                  and float(seg_r.iloc[half:].min()) > float(seg_r.iloc[:half].min()))
    top_div = (float(seg_c.iloc[half:].max()) > float(seg_c.iloc[:half].max())
               and float(seg_r.iloc[half:].max()) < float(seg_r.iloc[:half].max()))
    detail = f"三重平滑RSI 快线={fast_now:.1f} 慢线={slow_now:.1f}"
    px = float(close.iloc[-1])
    atr = float(_atr(df).iloc[-1])
    # 点位：entry=现价，SL=1.5xATR，TP=2R
    plan_bull = _plan("bullish", px, "market", px - 1.5 * atr, px + 3.0 * atr,
                      "三重平滑RSI 多头信号现价入场；SL=1.5xATR；TP=2R")
    plan_bear = _plan("bearish", px, "market", px + 1.5 * atr, px - 3.0 * atr,
                      "三重平滑RSI 空头信号现价入场；SL=1.5xATR；TP=2R")
    if golden and bottom_div:
        return _sig(*name, "bullish", 0.8, detail + " → 金叉 + 底背离双共振，多头信号强",
                    trade_plan=plan_bull)
    if death and top_div:
        return _sig(*name, "bearish", 0.8, detail + " → 死叉 + 顶背离双共振，空头信号强",
                    trade_plan=plan_bear)
    if golden:
        return _sig(*name, "bullish", 0.5, detail + " → 金叉（无背离共振），偏多",
                    trade_plan=plan_bull)
    if death:
        return _sig(*name, "bearish", 0.5, detail + " → 死叉（无背离共振），偏空",
                    trade_plan=plan_bear)
    if bottom_div:
        return _sig(*name, "bullish", 0.35, detail + " → 底背离酝酿中，待金叉确认",
                    trade_plan=plan_bull)
    if top_div:
        return _sig(*name, "bearish", 0.35, detail + " → 顶背离酝酿中，待死叉确认",
                    trade_plan=plan_bear)
    d = "bullish" if fast_now > slow_now else ("bearish" if fast_now < slow_now else "neutral")
    return _sig(*name, d, 0.2,
                detail + f" → 无交叉无背离，快慢线{'多头' if d == 'bullish' else '空头' if d == 'bearish' else '并行'}排列",
                trade_plan=(plan_bull if d == "bullish" else plan_bear if d == "bearish" else None))


# ═══════════════════════════ 12. 套利 ═══════════════════════════

def signal_arbitrage(df: pd.DataFrame, basis_data: dict | None = None) -> dict:
    """套利需要期现/跨期/跨市多腿价差数据；单腿 K 线不足以生成信号时如实说明。"""
    name = ("arbitrage", "套利系统")
    if not basis_data:
        return _sig(*name, "neutral", 0.0,
                    "套利体系需要期现基差/跨期价差/跨市场价差等多腿数据，"
                    "当前仅有单腿 K 线，数据不足，不硬造信号。"
                    "可接入 jarvis_crypto_data 的 spot-perp basis 后启用")
    basis_pct = basis_data.get("basis_pct")
    if basis_pct is None:
        return _sig(*name, "neutral", 0.0, "基差数据缺失 basis_pct 字段，无法评估套利空间")
    b = float(basis_pct)
    if abs(b) >= 0.5:
        side = "正基差（期货升水）做空期货+买现货" if b > 0 else "负基差（期货贴水）做多期货+卖现货"
        return _sig(*name, "neutral", min(1.0, 0.4 + abs(b) * 0.4),
                    f"期现基差 {b:.3f}% 偏离显著 → {side}，等待价差收敛（方向中性策略）")
    return _sig(*name, "neutral", 0.1, f"期现基差 {b:.3f}% 处于正常区间，无套利空间")


# ═══════════════════════════ 信号器注册表 ═══════════════════════════

SIGNAL_FUNCS = {
    "turtle": signal_turtle,
    "dow": signal_dow,
    "elliott": signal_elliott,
    "volatility": signal_volatility,
    "gann": signal_gann,
    "chanlun": signal_chanlun,
    "rule123": signal_rule123,
    "gap": signal_gap,
    "martingale": signal_martingale,
    "oscillator": signal_oscillator,
    "triple_rsi": signal_triple_rsi,
    "arbitrage": signal_arbitrage,
}


def run_all(df: pd.DataFrame, basis_data: dict | None = None,
            trade_history: list[dict] | None = None) -> list[dict]:
    """跑全部 12 套信号器；单个信号器异常降级为 neutral，绝不抛出。"""
    out = []
    for key, fn in SIGNAL_FUNCS.items():
        try:
            if key == "martingale":
                out.append(fn(df, trade_history))
            elif key == "arbitrage":
                out.append(fn(df, basis_data))
            else:
                out.append(fn(df))
        except Exception as e:  # noqa: BLE001 — 单信号器崩溃不拖垮整体
            name_cn = {"turtle": "海龟交易", "dow": "道氏理论", "elliott": "艾略特波浪",
                       "volatility": "波动率系统", "gann": "江恩时间窗", "chanlun": "缠论",
                       "rule123": "123法则", "gap": "跳空缺口", "martingale": "马丁格尔",
                       "oscillator": "摆动震荡", "triple_rsi": "三重平滑RSI",
                       "arbitrage": "套利系统"}.get(key, key)
            out.append(_sig(key, name_cn, "neutral", 0.0, f"信号器异常已降级: {repr(e)[:120]}"))
    return out


# ═══════════════════════════ 分层共识融合 ═══════════════════════════
# 照《十二套技术.md》第七节：
#   层级1 顶层过滤器：道氏定主趋势，仅顺趋势开仓
#   层级2 主线策略：海龟+艾略特（长线）/ 缠论+123（中短线）
#   层级3 辅助共振：江恩时间窗、三重平滑RSI
#   层级4 行情自适应：摆动震荡、跳空、波动率、马丁、套利

_LAYER_DEF = {
    "layer1_filter": {"systems": ("dow",), "weight": 0.30, "label": "顶层过滤（道氏）"},
    "layer2_main": {"systems": ("turtle", "elliott", "chanlun", "rule123"),
                    "weight": 0.40, "label": "主线策略（海龟/艾略特/缠论/123）"},
    "layer3_resonance": {"systems": ("gann", "triple_rsi"), "weight": 0.18,
                         "label": "辅助共振（江恩/三重RSI）"},
    "layer4_adaptive": {"systems": ("oscillator", "gap", "volatility", "martingale", "arbitrage"),
                        "weight": 0.12, "label": "自适应子策略（震荡/跳空/波动率/马丁/套利）"},
}

_DIR_VAL = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}

RISK_PCT_PER_TRADE = 1.0   # 单笔风险占权益 1%，仓位按止损距离反推


def _aggregate_trade_plan(direction: str, signals: list[dict],
                          atr: float | None = None) -> dict | None:
    """把主方向各系统 trade_plan 聚合成共识级交易计划。

    口径：entry_zone=入场价中位数±0.3xATR；stop_loss=同方向最保守但不超过
    2xATR 兜底（多单二者取近/较高者）；tp1=目标中位数、tp2=激进目标；
    position_pct=单笔风险 1% 权益按止损距离反推，封顶 100。
    无可用 plan 或方向中性 → None。
    """
    if direction not in ("bullish", "bearish"):
        return None
    plans = [(s["system"], s["trade_plan"]) for s in signals
             if s.get("direction") == direction and s.get("trade_plan")]
    if not plans:
        return None
    entries = sorted(p["entry"] for _, p in plans)
    sls = [p["stop_loss"] for _, p in plans]
    tps = [p["take_profit"] for _, p in plans]
    entry_mid = float(entries[len(entries) // 2]) if len(entries) % 2 else \
        float((entries[len(entries) // 2 - 1] + entries[len(entries) // 2]) / 2)
    buf = 0.3 * float(atr) if atr and math.isfinite(atr) and atr > 0 else entry_mid * 0.005
    atr_v = float(atr) if atr and math.isfinite(atr) and atr > 0 else entry_mid * 0.02

    tps_sorted = sorted(tps)
    tp_mid = float(tps_sorted[len(tps_sorted) // 2]) if len(tps_sorted) % 2 else \
        float((tps_sorted[len(tps_sorted) // 2 - 1] + tps_sorted[len(tps_sorted) // 2]) / 2)
    zone_lo, zone_hi = entry_mid - buf, entry_mid + buf
    # 自洽门禁：SL/TP 必须落在整个入场区间外侧（落进 zone 内=挂单未成交即触发，坑人计划）
    if direction == "bullish":
        sl = max(min(sls), entry_mid - 2 * atr_v)   # 最保守(最低) 与 2xATR 兜底取近
        tp2 = max(tps)
        if not (sl < zone_lo and tp_mid > zone_hi):
            return None
    else:
        sl = min(max(sls), entry_mid + 2 * atr_v)
        tp2 = min(tps)
        if not (tp_mid < zone_lo and sl > zone_hi):
            return None
    risk = abs(entry_mid - sl)
    if risk <= 0:
        return None
    rr = round(abs(tp_mid - entry_mid) / risk, 2)
    if rr < 0.5:   # 盈亏比荒谬（潜在收益不足风险一半）→ 宁缺毋滥
        return None
    risk_pct = risk / entry_mid * 100
    position_pct = round(min(100.0, RISK_PCT_PER_TRADE / max(risk_pct, 1e-9) * 100), 1)
    position_pct = max(position_pct, 0.1)
    tp1_r, tp2_r = _round_price(tp_mid), _round_price(tp2)
    return {
        # 显式多空标识（与单信号 trade_plan.side 同口径）
        "side": "long" if direction == "bullish" else "short",
        "entry_zone": [_round_price(zone_lo), _round_price(zone_hi)],
        "stop_loss": _round_price(sl),
        "take_profit_1": tp1_r,
        # TP2 与 TP1 重合时输出 None（前端允许 null，避免图上重叠双线）
        "take_profit_2": tp2_r if tp2_r != tp1_r else None,
        "rr": rr,
        "position_pct": position_pct,
        "basis": [k for k, _ in plans],
        "note": (f"聚合 {len(plans)} 套系统：入场=中位数±0.3xATR，"
                 f"SL=最保守/2xATR 兜底取近，仓位按单笔风险 {RISK_PCT_PER_TRADE}% 权益反推"),
    }


def consensus(signals: list[dict], atr: float | None = None) -> dict:
    """分层共识融合。

    口径：
      - 每层内取各信号 direction(±1/0) × strength 的均值 → 层得分 ∈ [-1, 1]
      - 总分 = Σ 层得分 × 层权重（道氏顶层过滤：与道氏相反的层贡献减半）
      - direction：|score| ≥ 0.12 → bullish/bearish，否则 neutral
      - confidence：|score| 与投票一致率的融合，∈ [0, 1]
    """
    by_system = {s["system"]: s for s in signals}
    votes = {"bullish": 0, "bearish": 0, "neutral": 0}
    for s in signals:
        votes[s.get("direction", "neutral")] += 1

    dow_dir = by_system.get("dow", {}).get("direction", "neutral")
    dow_val = _DIR_VAL.get(dow_dir, 0.0)

    layers = {}
    score = 0.0
    for lkey, ldef in _LAYER_DEF.items():
        members, contribs = [], []
        for skey in ldef["systems"]:
            sig = by_system.get(skey)
            if sig is None:
                continue
            v = _DIR_VAL.get(sig["direction"], 0.0) * float(sig["strength"])
            members.append({"system": skey, "name_cn": sig["name_cn"],
                            "direction": sig["direction"], "strength": sig["strength"]})
            contribs.append(v)
        layer_score = float(np.mean(contribs)) if contribs else 0.0
        # 道氏顶层过滤：下层若与主趋势相反，贡献减半（仅顺趋势开仓的软化版）
        effective = layer_score
        filtered = False
        if lkey != "layer1_filter" and dow_val != 0.0 and layer_score * dow_val < 0:
            effective = layer_score * 0.5
            filtered = True
        score += effective * ldef["weight"]
        layers[lkey] = {
            "label": ldef["label"],
            "weight": ldef["weight"],
            "score": round(layer_score, 4),
            "effective_score": round(effective, 4),
            "dow_filtered": filtered,
            "members": members,
        }

    score = float(max(-1.0, min(1.0, score)))
    if score >= 0.12:
        direction = "bullish"
    elif score <= -0.12:
        direction = "bearish"
    else:
        direction = "neutral"

    # 置信度：分数强度 60% + 方向投票一致率 40%。
    # 一致率按方向票样本量打折 n/(n+3)：2 票全一致（agree=1）远不如 8 票全一致可信，
    # 系数随 n 单调升、渐近 1，防止“只有一两票也满格一致”造成置信度虚高。
    n_directional = votes["bullish"] + votes["bearish"]
    agree = (max(votes["bullish"], votes["bearish"]) / n_directional) if n_directional else 0.0
    agree_weighted = agree * (n_directional / (n_directional + 3.0)) if n_directional else 0.0
    confidence = min(1.0, abs(score) * 1.8 * 0.6 + agree_weighted * 0.4)
    if direction == "neutral":
        confidence = min(confidence, 0.35)

    # 聚合关键价位：只保留方向一致或过滤层的核心位，去重取前 8
    key_levels: list[dict] = []
    seen = set()
    priority = ("dow", "turtle", "chanlun", "gap", "elliott", "rule123")
    for skey in priority:
        for lv_item in by_system.get(skey, {}).get("key_levels", []):
            tag = (lv_item["label"], lv_item["price"])
            if tag not in seen:
                seen.add(tag)
                key_levels.append({**lv_item, "source": skey})
    key_levels = key_levels[:8]

    dir_cn = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}
    strong = [s for s in signals if s["direction"] == direction and s["strength"] >= 0.5]
    reasoning = (
        f"分层融合总分 {score:+.3f} → {dir_cn[direction]}（置信度 {confidence:.0%}）。"
        f"顶层道氏方向：{dir_cn.get(dow_dir, dow_dir)}；"
        f"投票分布 涨{votes['bullish']}/跌{votes['bearish']}/中性{votes['neutral']}"
    )
    if strong:
        reasoning += "。强信号：" + "；".join(
            f"{s['name_cn']}({s['strength']:.2f})" for s in strong[:4])
    filtered_layers = [v["label"] for v in layers.values() if v["dow_filtered"]]
    if filtered_layers:
        reasoning += "。逆势层已被道氏过滤减权：" + "、".join(filtered_layers)

    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "score": round(score, 4),
        "votes": votes,
        "layers": layers,
        "reasoning": reasoning,
        "key_levels": key_levels,
        "trade_plan": _aggregate_trade_plan(direction, signals, atr=atr),
    }


def analyze(df: pd.DataFrame, basis_data: dict | None = None,
            trade_history: list[dict] | None = None) -> dict:
    """一步到位：跑 12 套信号 + 共识融合。返回 {signals: [...], consensus: {...}}。"""
    signals = run_all(df, basis_data=basis_data, trade_history=trade_history)
    try:
        atr = float(_atr(df).iloc[-1]) if len(df) else None
    except Exception:  # noqa: BLE001 — ATR 计算失败不影响共识主体
        atr = None
    return {"signals": signals, "consensus": consensus(signals, atr=atr)}


# ═══════════════════════════ 多时间框架共识融合 ═══════════════════════════

TF_WEIGHTS = {"15m": 0.3, "1h": 0.3, "4h": 0.4}
# 主周期优先级（长周期优先）：votes/layers/key_levels 取该周期的单 TF 共识明细
_TF_PRIORITY = ("1d", "4h", "1h", "30m", "15m", "5m", "1m")


def consensus_multi_tf(tf_consensus: dict[str, dict]) -> dict:
    """把多个时间框架各自的 consensus() 结果加权融合成总共识（纯函数）。

    Args:
        tf_consensus: {"15m": consensus_dict, "1h": ..., "4h": ...}（允许缺项）
    Returns:
        与 consensus() 同骨架 + 多时间框架明细：
        - votes / layers / key_levels：主周期（4h，不可用时取最长可用 TF）的
          12 系统投票（和=12）、分层明细、关键位——对齐单 TF 契约
        - tf_votes：各时间框架方向投票 {bullish, bearish, neutral}（和=TF 数）
        - tfs：各 TF 完整共识明细
    """
    valid = {tf: c for tf, c in (tf_consensus or {}).items() if isinstance(c, dict)}
    if not valid:
        return {"direction": "neutral", "confidence": 0.0, "score": 0.0,
                "votes": {"bullish": 0, "bearish": 0, "neutral": 0},
                "tf_votes": {"bullish": 0, "bearish": 0, "neutral": 0},
                "layers": {}, "trade_plan": None,
                "reasoning": "无可用时间框架数据", "key_levels": [], "tfs": {}}

    dir_cn = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}
    score, conf_sum, total_w = 0.0, 0.0, 0.0
    tf_votes = {"bullish": 0, "bearish": 0, "neutral": 0}
    parts = []
    for tf, c in valid.items():
        w = TF_WEIGHTS.get(tf, 0.3)
        total_w += w
        score += w * float(c.get("score", 0.0) or 0.0)
        conf_sum += w * float(c.get("confidence", 0.0) or 0.0)
        d = c.get("direction", "neutral")
        tf_votes[d if d in tf_votes else "neutral"] += 1
        parts.append(f"[{tf}] {dir_cn.get(d, d)}({float(c.get('confidence', 0) or 0):.0%})")
    score /= total_w
    confidence = conf_sum / total_w

    dirs = {c.get("direction") for c in valid.values()}
    if len(valid) >= 2 and len(dirs) == 1 and dirs != {"neutral"}:
        confidence = min(1.0, confidence * 1.15)
        parts.append("→ 多级别方向一致")
    elif len(valid) >= 2 and "bullish" in dirs and "bearish" in dirs:
        confidence *= 0.7
        parts.append("→ 多级别方向分歧")

    if score >= 0.12:
        direction = "bullish"
    elif score <= -0.12:
        direction = "bearish"
    else:
        direction = "neutral"
    if direction == "neutral":
        confidence = min(confidence, 0.35)

    # 主周期：4h 优先，缺则按周期从长到短取第一个可用（再兜底按权重最高）
    primary_tf = next((tf for tf in _TF_PRIORITY if tf in valid), None)
    if primary_tf is None:
        primary_tf = max(valid, key=lambda tf: TF_WEIGHTS.get(tf, 0.3))
    primary = valid[primary_tf]
    votes = dict(primary.get("votes") or {"bullish": 0, "bearish": 0, "neutral": 0})
    layers = primary.get("layers") or {}
    key_levels = list(primary.get("key_levels", []))[:8]
    parts.append(f"→ 综合 {dir_cn[direction]}（加权分 {score:+.3f}，置信度 {confidence:.0%}，"
                 f"主周期 {primary_tf}）")

    # 交易计划：MTF 综合为中性 → None；有方向时取「与综合方向一致」的最长周期的聚合计划
    trade_plan = None
    if direction in ("bullish", "bearish"):
        for tf in [primary_tf] + [t for t in _TF_PRIORITY if t in valid and t != primary_tf]:
            c = valid.get(tf) or {}
            if c.get("direction") == direction and c.get("trade_plan"):
                trade_plan = {**c["trade_plan"], "source_tf": tf}
                break

    return {
        "direction": direction,
        "confidence": round(min(1.0, confidence), 3),
        "score": round(float(max(-1.0, min(1.0, score))), 4),
        "votes": votes,
        "tf_votes": tf_votes,
        "layers": layers,
        "primary_tf": primary_tf,
        "trade_plan": trade_plan,
        "reasoning": "；".join(parts),
        "key_levels": key_levels,
        "tfs": valid,
    }


# ═══════════════════ K线取数（复用 jarvis_crypto_data，联网） ═══════════════════

def fetch_klines_df(symbol: str, interval: str = "4h", limit: int = 300,
                    *, drop_unclosed: bool = False) -> pd.DataFrame | None:
    """从 Binance 现货拉 K 线转 DataFrame（与 dashboard /api/kline 同源同参）。

    本模块唯一联网函数；失败返回 None，绝不抛出。

    [D2] drop_unclosed：按 close_time（Binance k[6]，bar 最后一毫秒）判断末根是否
    已收盘，未收盘则丢弃——供战绩回填/共识巡检等「只认已收盘 bar」的口径使用
    （与 jarvis_intraday_predict 丢进行中 bar 同款纪律）。默认 False = 保留
    进行中 bar（dashboard 实时判读的实盘常规行为，见模块 docstring）。
    """
    try:
        import time as _time

        import jarvis_crypto_data as jcd
        sym = (symbol or "").upper().replace("-", "").replace("/", "")
        if not sym.endswith(("USDT", "USDC")):
            sym += "USDT"
        lim = max(50, min(int(limit), 500))
        raw = jcd._get(jcd.SPOT_API + "/api/v3/klines",
                       {"symbol": sym, "interval": interval, "limit": lim})
        if not isinstance(raw, list) or not raw:
            return None
        if drop_unclosed:
            try:
                if float(raw[-1][6]) >= _time.time() * 1000:
                    raw = raw[:-1]
            except (IndexError, TypeError, ValueError):
                pass  # close_time 字段异常时保守保留，不因判定失败丢真数据
            if not raw:
                return None
        rows = [{"time": int(k[0]),
                 "open": float(k[1]), "high": float(k[2]),
                 "low": float(k[3]), "close": float(k[4]),
                 "volume": float(k[5])} for k in raw]
        return pd.DataFrame(rows)
    except Exception:  # noqa: BLE001 — 取数失败交由调用方降级
        return None
