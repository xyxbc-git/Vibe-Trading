#!/usr/bin/env python3
"""贾维斯 JARVIS — 牛熊市体制识别引擎（Bull/Bear Regime Detection）。

回答「目前的大行情是牛市还是熊市」：用长周期多因子量化融合，输出
bull / bear / range 三态体制判定 + 综合分（-100～+100）+ 逐因子解释。

与 jarvis_regime_classifier 的分工：
  - 本模块       ：**大周期体制**（牛市/熊市/震荡市，日线-周线级，变化以周/月计）
  - regime_classifier：短周期市场状态（trending/ranging/breakout，开仓打标用）

四个真实因子 + 一个预留因子：
  1) ma200      200 日均线：现价 vs MA200 偏离度 + 均线斜率（牛熊分界经典锚）
  2) weekly     周线趋势结构：swing 高低点抬升/降低（道氏思路，HH/HL=牛 LH/LL=熊）
  3) momentum   长周期动量：30D/90D 涨跌幅 + 现价在 52 周高低区间的位置
  4) sentiment  情绪面（长周期同步口径）：恐贪指数为主、多空比为辅——牛市情绪偏热、
                熊市情绪冰冷；复用 jarvis_market_intel 缓存数据（不改动该模块）
  5) onchain    链上估值（MVRV 等）：需 Glassnode key，未配置 available=False 不参与

因子统一结构（与 jarvis_sentiment 同款，UI 直接复用渲染逻辑）：
  {key, name, available, value, display, bias: bullish|bearish|neutral,
   score: -100~+100, weight, note}

设计原则（与十二套技术 / 情绪引擎同风格）：
  - 纯函数核心 build_assessment(df_daily, df_weekly, intel)：不联网、可单测
  - 数据缺失的因子 available=False 不参与计分（禁止编造）
  - assess() 门面负责拉 K 线与情报缓存再走纯函数；失败 ok=False 不抛出

命令行（联网前本地验证）：
  python jarvis_bull_bear.py            # BTCUSDT
  python jarvis_bull_bear.py ETHUSDT
"""

from __future__ import annotations

import time

# ═══════════════════════════ 阈值口径（集中定义，单测锁定） ═══════════════════════════

# 体制三分阈值：综合分 ≥ +25 判牛、≤ -25 判熊，中间为震荡市
REGIME_TH = 25.0
# 震荡市内的偏向后缀阈值（|score| > 8 时标注「偏牛/偏熊」）
LEAN_TH = 8.0

# ma200 因子：偏离度每 1% 计 3 分（上限 ±55）；MA 斜率（20 日变化）每 1% 计 15 分（上限 ±45）
MA_DEV_SCALE = 300.0
MA_DEV_CAP = 55.0
MA_SLOPE_SCALE = 1500.0
MA_SLOPE_CAP = 45.0
MA_SLOPE_LOOKBACK = 20   # 斜率对比窗口（日）

# 周线结构：swing 分形左右确认根数；结构分与突破确认分
SWING_WING = 2
STRUCT_SCORE = 60.0
BREAK_SCORE = 20.0

# 动量因子：30D 涨跌幅每 1% 计 2 分、90D 每 1% 计 1 分（各 cap ±60）；
# 52 周区间位置（0~1）偏离中点映射 ±60；三者加权（0.4/0.35/0.25）后自然落在 ±60 内
MOM_30_SCALE = 200.0
MOM_90_SCALE = 100.0
MOM_CAP = 60.0
RANGE_SCORE = 60.0
RANGE_LOOKBACK = 260     # 52 周 ≈ 260 个交易日（crypto 全年 365 根，取满一年）

# 情绪因子（长周期同步口径，与 jarvis_sentiment 的短线逆向口径不同）
FNG_OVERHEAT = 80        # 极度贪婪：牛市后期特征，加分但打折并警示
FNG_GREED = 65
FNG_WARM = 55
FNG_COOL = 45
FNG_FEAR = 30
FNG_CAPITULATE = 15      # 极端恐慌：熊市深水区，历史大底常见（note 提示）

# 因子权重（onchain 为预留位：Glassnode key 配置后自动参与加权）
WEIGHTS = {"ma200": 0.30, "weekly": 0.25, "momentum": 0.25,
           "sentiment": 0.20, "onchain": 0.15}

_BIAS_TH = 15.0
_BIAS_CN = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
REGIME_CN = {"bull": "牛市", "bear": "熊市", "range": "震荡市"}

DISCLAIMER = ("体制判定基于长周期技术结构与情绪因子的量化融合，存在滞后性且不构成投资建议；"
              "牛熊转换确认通常滞后顶底数周，请结合仓位管理独立决策。")


def _clamp(v: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _bias_of(score: float) -> str:
    if score >= _BIAS_TH:
        return "bullish"
    if score <= -_BIAS_TH:
        return "bearish"
    return "neutral"


def _factor(key: str, name: str, available: bool, value=None, display: str = "—",
            score: float = 0.0, note: str = "") -> dict:
    score = round(_clamp(score), 1)
    return {"key": key, "name": name, "available": available, "value": value,
            "display": display, "bias": _bias_of(score) if available else "neutral",
            "score": score if available else 0.0,
            "weight": WEIGHTS[key], "note": note}


def _fmt_price(v: float) -> str:
    if v >= 1:
        return f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}"
    return f"{v:.6g}"


# ═══════════════════════════ 单因子评分（纯函数，吃 list[dict] K 线） ═══════════════════════════
# K 线行结构：{"time": ms, "open","high","low","close","volume": float}（fetch_klines_df 行序）

def score_ma200(daily: list[dict] | None) -> dict:
    """200 日均线：现价在 MA200 上方=偏牛（偏离越大越强），均线斜率向上加分。"""
    if not daily or len(daily) < 200 + MA_SLOPE_LOOKBACK:
        need = 200 + MA_SLOPE_LOOKBACK
        return _factor("ma200", "200日均线", False,
                       note=f"日线数据不足（需 {need} 根，实得 {len(daily or [])}），无法计算 MA200 与斜率")
    closes = [float(r["close"]) for r in daily]
    price = closes[-1]
    ma_now = sum(closes[-200:]) / 200.0
    ma_prev = sum(closes[-200 - MA_SLOPE_LOOKBACK:-MA_SLOPE_LOOKBACK]) / 200.0
    dev = price / ma_now - 1.0
    slope = ma_now / ma_prev - 1.0 if ma_prev > 0 else 0.0

    dev_score = _clamp(dev * MA_DEV_SCALE, -MA_DEV_CAP, MA_DEV_CAP)
    slope_score = _clamp(slope * MA_SLOPE_SCALE, -MA_SLOPE_CAP, MA_SLOPE_CAP)
    score = dev_score + slope_score

    pos_cn = "上方" if dev >= 0 else "下方"
    slope_cn = "向上" if slope > 0.001 else ("向下" if slope < -0.001 else "走平")
    note = (f"现价 {_fmt_price(price)} 在 200D MA（{_fmt_price(ma_now)}）{pos_cn} "
            f"{abs(dev) * 100:.1f}%，均线近 {MA_SLOPE_LOOKBACK} 日{slope_cn}"
            f"（{slope * 100:+.2f}%）——"
            + ("牛市典型形态" if dev > 0 and slope > 0 else
               "熊市典型形态" if dev < 0 and slope < 0 else
               "价格与均线方向背离，体制过渡期常见"))
    return _factor("ma200", "200日均线", True, value=round(dev, 4),
                   display=f"MA200 {pos_cn} {abs(dev) * 100:.1f}% · 斜率 {slope * 100:+.2f}%",
                   score=score, note=note)


def _find_swings(rows: list[dict], wing: int = SWING_WING) -> tuple[list[float], list[float]]:
    """分形法找已确认的 swing 高/低点（左右各 wing 根都更低/更高才确认）。"""
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    sh: list[float] = []
    sl: list[float] = []
    for i in range(wing, len(rows) - wing):
        seg_h = highs[i - wing:i + wing + 1]
        seg_l = lows[i - wing:i + wing + 1]
        if highs[i] == max(seg_h) and seg_h.count(highs[i]) == 1:
            sh.append(highs[i])
        if lows[i] == min(seg_l) and seg_l.count(lows[i]) == 1:
            sl.append(lows[i])
    return sh, sl


def score_weekly_structure(weekly: list[dict] | None) -> dict:
    """周线趋势结构（道氏思路）：swing 高低点同步抬升=牛结构，同步降低=熊结构。"""
    min_bars = SWING_WING * 2 + 8
    if not weekly or len(weekly) < min_bars:
        return _factor("weekly", "周线结构", False,
                       note=f"周线数据不足（需 ≥{min_bars} 根，实得 {len(weekly or [])}），无法识别趋势结构")
    sh, sl = _find_swings(weekly)
    if len(sh) < 2 or len(sl) < 2:
        return _factor("weekly", "周线结构", True, value=0.0, display="摆动点不足",
                       score=0.0, note="近段周线未形成足够的确认摆动高/低点，结构不明")
    h1, h2 = sh[-2], sh[-1]
    l1, l2 = sl[-2], sl[-1]
    hh, hl = h2 > h1, l2 > l1
    close = float(weekly[-1]["close"])

    if hh and hl:
        score, struct_cn = STRUCT_SCORE, "高点抬升 + 低点抬升（HH/HL）＝上升趋势结构"
    elif (not hh) and (not hl):
        score, struct_cn = -STRUCT_SCORE, "高点降低 + 低点降低（LH/LL）＝下降趋势结构"
    elif hh:
        score, struct_cn = 0.0, "高点抬升但低点降低＝波幅扩张，方向未定"
    else:
        score, struct_cn = 0.0, "高点降低但低点抬升＝区间收敛，等待选向"

    brk = ""
    if close > h2:
        score += BREAK_SCORE
        brk = f"；现价已突破近期摆动高点 {_fmt_price(h2)}，多头确认"
    elif close < l2:
        score -= BREAK_SCORE
        brk = f"；现价已跌破近期摆动低点 {_fmt_price(l2)}，空头确认"

    disp = (f"高点 {_fmt_price(h1)}→{_fmt_price(h2)} · "
            f"低点 {_fmt_price(l1)}→{_fmt_price(l2)}")
    return _factor("weekly", "周线结构", True,
                   value=1.0 if (hh and hl) else (-1.0 if (not hh and not hl) else 0.0),
                   display=disp, score=score, note=struct_cn + brk)


def score_momentum(daily: list[dict] | None) -> dict:
    """长周期动量：30D/90D 涨跌幅 + 现价在 52 周高低区间中的位置。"""
    if not daily or len(daily) < 91:
        return _factor("momentum", "长周期动量", False,
                       note=f"日线数据不足（需 ≥91 根，实得 {len(daily or [])}），无法计算 30D/90D 动量")
    closes = [float(r["close"]) for r in daily]
    price = closes[-1]
    ret30 = price / closes[-31] - 1.0
    ret90 = price / closes[-91] - 1.0

    lb = daily[-RANGE_LOOKBACK:] if len(daily) >= RANGE_LOOKBACK else daily
    hi = max(float(r["high"]) for r in lb)
    lo = min(float(r["low"]) for r in lb)
    pos = (price - lo) / (hi - lo) if hi > lo else 0.5

    s30 = _clamp(ret30 * MOM_30_SCALE, -MOM_CAP, MOM_CAP)
    s90 = _clamp(ret90 * MOM_90_SCALE, -MOM_CAP, MOM_CAP)
    s_rng = (pos - 0.5) * 2 * RANGE_SCORE
    score = s30 * 0.40 + s90 * 0.35 + s_rng * 0.25

    span_w = "52周" if len(daily) >= RANGE_LOOKBACK else f"{len(lb)}日"
    pos_cn = ("贴近区间顶部（强势）" if pos >= 0.8 else
              "上半区" if pos >= 0.55 else
              "贴近区间底部（弱势）" if pos <= 0.2 else
              "下半区" if pos <= 0.45 else "区间中位")
    note = (f"30D {ret30 * 100:+.1f}% / 90D {ret90 * 100:+.1f}%；"
            f"现价处于{span_w}高低区间（{_fmt_price(lo)}~{_fmt_price(hi)}）的 "
            f"{pos * 100:.0f}% 分位——{pos_cn}")
    return _factor("momentum", "长周期动量", True, value=round(pos, 3),
                   display=f"30D {ret30 * 100:+.1f}% · 90D {ret90 * 100:+.1f}% · 区间 {pos * 100:.0f}%",
                   score=score, note=note)


def score_sentiment_regime(fng: dict | None, long_short: dict | None) -> dict:
    """情绪面（长周期同步口径）：牛市情绪偏热、熊市情绪冰冷。

    与 jarvis_sentiment 的短线逆向口径（极端贪婪=偏空）不同，体制层把情绪当
    「与体制同步的温度计」：持续贪婪多出现在牛市、持续恐惧多出现在熊市；仅在
    两端极值处打折并提示可能临近体制反转。
    """
    if not fng or fng.get("value") is None:
        return _factor("sentiment", "市场情绪", False,
                       note="恐贪指数暂不可用（多空比仅作辅助，不单独计分）")
    v = int(fng["value"])
    cls = str(fng.get("classification") or "")
    if v >= FNG_OVERHEAT:
        score = 20.0
        note = (f"恐贪 {v}（极度贪婪）——情绪过热是牛市后期特征，体制仍偏牛但"
                "历史上极度贪婪区常临近周期顶部，加分打折并警示")
    elif v >= FNG_GREED:
        score = 30.0
        note = f"恐贪 {v}（贪婪）——风险偏好旺盛，牛市体制的典型情绪温度"
    elif v >= FNG_WARM:
        score = 15.0
        note = f"恐贪 {v}（偏暖）——情绪温和偏多，体制天平略向牛市倾斜"
    elif v > FNG_COOL:
        score = 0.0
        note = f"恐贪 {v}（中性）——情绪不构成体制方向依据"
    elif v > FNG_FEAR:
        score = -15.0
        note = f"恐贪 {v}（偏冷）——情绪转谨慎，警惕体制走弱"
    elif v > FNG_CAPITULATE:
        score = -30.0
        note = f"恐贪 {v}（恐惧）——避险情绪浓厚，熊市/深度回调体制的典型温度"
    else:
        score = -20.0
        note = (f"恐贪 {v}（极度恐慌）——熊市深水区特征；历史上极端恐慌区亦常见"
                "周期大底，减分打折并提示反转观察窗")

    lp = None
    if long_short and long_short.get("long_pct") is not None:
        lp = float(long_short["long_pct"])
        if lp >= 55:
            score += 5.0
        elif lp <= 45:
            score -= 5.0

    disp = f"恐贪 {v} · {cls}" + (f" · 多头占比 {lp:.0f}%" if lp is not None else "")
    return _factor("sentiment", "市场情绪", True, value=v, display=disp,
                   score=score, note=note)


def score_onchain_valuation(mvrv: dict | None = None) -> dict:
    """链上估值因子（预留接口位）：Glassnode key 配置后自动参与计分。

    接入后口径：MVRV > 3 高估（周期顶部带，偏熊）、< 1 低估（周期底部带，偏牛）、
    1~3 之间按 Z 分线性过渡。
    """
    if not mvrv or mvrv.get("value") is None:
        return _factor("onchain", "链上估值", False, display="未接入",
                       note="需 Glassnode API key；接入后自动参与计分："
                            "MVRV>3=周期顶部带（偏熊）、<1=周期底部带（偏牛）")
    v = float(mvrv["value"])
    if v >= 3.0:
        score = -_clamp((v - 3.0) * 40.0 + 30.0, 0, 60)
        note = f"MVRV {v:.2f} 处于历史高估带——持币成本利润率过高，周期顶部特征"
    elif v <= 1.0:
        score = _clamp((1.0 - v) * 60.0 + 30.0, 0, 60)
        note = f"MVRV {v:.2f} 低于 1——市场整体浮亏，周期底部带特征"
    else:
        score = (2.0 - v) * 30.0  # v=1→+30, v=2→0, v=3→-30
        note = f"MVRV {v:.2f} 处于中性估值带"
    return _factor("onchain", "链上估值", True, value=round(v, 3),
                   display=f"MVRV {v:.2f}", score=score, note=note)


# ═══════════════════════════ 综合研判（纯函数） ═══════════════════════════

def build_assessment(daily: list[dict] | None, weekly: list[dict] | None,
                     intel: dict | None, symbol: str = "BTCUSDT",
                     mvrv: dict | None = None) -> dict:
    """K 线 + 情报数据 → 体制判定。全纯函数：不联网，可单测。"""
    sym = (symbol if symbol.upper().endswith(("USDT", "USDC")) else symbol + "USDT").upper()
    intel = intel or {}
    factors = [
        score_ma200(daily),
        score_weekly_structure(weekly),
        score_momentum(daily),
        score_sentiment_regime(intel.get("fng"), intel.get("long_short")),
        score_onchain_valuation(mvrv),
    ]
    live = [f for f in factors if f["available"]]
    wsum = sum(f["weight"] for f in live)
    score = round(_clamp(sum(f["score"] * f["weight"] for f in live) / wsum), 1) if wsum else 0.0

    if score >= REGIME_TH:
        regime = "bull"
    elif score <= -REGIME_TH:
        regime = "bear"
    else:
        regime = "range"

    # 置信度：判定强度（|score|）× 因子覆盖率（可用权重占比）
    coverage = wsum / sum(WEIGHTS.values()) if WEIGHTS else 0.0
    confidence = round(min(0.95, (0.35 + min(abs(score), 60.0) / 60.0 * 0.6) * coverage), 2)

    regime_cn = REGIME_CN[regime]
    if regime == "range" and abs(score) > LEAN_TH:
        regime_cn += "偏牛" if score > 0 else "偏熊"

    headline = _build_headline(regime_cn, live)
    return {"symbol": sym, "regime": regime, "regime_cn": regime_cn,
            "score": score, "confidence": confidence, "headline": headline,
            "factors": factors, "disclaimer": DISCLAIMER}


def _build_headline(regime_cn: str, live: list[dict]) -> str:
    """一句话结论：主导因子方向描述 + 体制判定。"""
    if not live:
        return f"数据源不足，暂按{regime_cn}处理"
    tops = sorted(live, key=lambda f: abs(f["score"]) * f["weight"], reverse=True)[:2]
    parts = []
    for f in tops:
        if abs(f["score"]) < 5:
            continue
        parts.append(f"{f['name']}{_BIAS_CN[f['bias']]}（{f['score']:+.0f}）")
    drivers = "、".join(parts) or "各因子接近中性"
    return f"{drivers}，判定：{regime_cn}"


# ═══════════════════════════ 门面（带 IO） ═══════════════════════════

def assess(symbol: str = "BTCUSDT") -> dict:
    """拉日线/周线 K 线与市场情报缓存 → 体制判定。异常 ok=False 不抛出。"""
    sym = (symbol if symbol.upper().endswith(("USDT", "USDC")) else symbol + "USDT").upper()
    daily = weekly = None
    intel: dict = {}
    try:
        import jarvis_twelve_systems as jts
        df_d = jts.fetch_klines_df(sym, "1d", 500)
        df_w = jts.fetch_klines_df(sym, "1w", 200)
        daily = df_d.to_dict("records") if df_d is not None else None
        weekly = df_w.to_dict("records") if df_w is not None else None
    except Exception:  # noqa: BLE001 — K 线不可得时因子各自降级为 available=False
        pass
    try:
        import jarvis_market_intel as jmi
        intel = jmi.get_intel() or {}
    except Exception:  # noqa: BLE001 — 情绪因子降级
        intel = {}

    if not daily and not weekly and not intel:
        return {"ok": False, "symbol": sym,
                "error": "K 线与市场情报数据源均不可用，无法判定体制"}

    out = build_assessment(daily, weekly, intel, sym)
    out.update({"ok": True, "updatedAt": int(time.time())})
    return out


if __name__ == "__main__":
    import json as _json
    import sys as _sys
    _sym = _sys.argv[1] if len(_sys.argv) > 1 else "BTCUSDT"
    print(_json.dumps(assess(_sym), ensure_ascii=False, indent=2))
