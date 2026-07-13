#!/usr/bin/env python3
"""贾维斯 JARVIS — 供需/情绪因子引擎（多空比 · 资金费率 · 持仓量 OI · 恐慌贪婪）。

把市场情报页已接入的四类真实数据量化为统一结构的决策因子，输出综合情绪分
sentiment score（-100～+100）与综合偏置，回答「除了 K 线，如何用供需关系
（多空比等）判断进出场与止盈止损」：

  - 多空比    顺向偏置 + 极端拥挤反向警示（多头占比 >65% = 拥挤多风险）
  - 资金费率  正费率过高 → 做多成本高企/多头拥挤；负费率 → 空头拥挤（轧空燃料）
  - 持仓量 OI 与 24h 价格方向交叉：价涨+OI 增=趋势健康；价涨+OI 降=软弱反弹
  - 恐贪指数  逆向指标：极端恐惧=逆向买点区，极端贪婪=逆向卖点区
  - 爆仓/链上 预留接口位（Coinglass / Glassnode key 配置后自动参与计分）

因子统一结构（供信号引擎 / 预测引擎 / UI 复用）：
  {key, name, available, value, display, bias: bullish|bearish|neutral,
   score: -100~+100, weight, note}

融合进出场判断：apply_to_consensus() 把情绪层叠加到十二套技术共识上——
同向小幅增益置信度、极端反向降级并给出警示与止盈止损收紧建议（只做建议
展示，不强制改用户订单）。

设计原则（与十二套技术引擎同风格）：
  - 纯函数核心 build_factors(intel)：只吃 dict、不联网，可单测
  - 数据缺失的因子 available=False 不参与计分（禁止编造）
  - assess() 门面负责拉 jarvis_market_intel 缓存数据再走纯函数
"""

from __future__ import annotations

import time

# ═══════════════════════════ 阈值口径（集中定义，单测锁定） ═══════════════════════════

# 多空比（全体账户多头占比%）：中带顺向、过渡带衰减、极端带反向
LS_CROWD_HI = 65.0     # 多头拥挤阈值：>65% 反向警示（挤多）
LS_TRANS_HI = 60.0     # 顺向→拥挤过渡起点
LS_CROWD_LO = 35.0     # 空头拥挤阈值：<35%（空头占比>65%）反向看多（轧空）
LS_TRANS_LO = 40.0

# 资金费率（8h 期费率，小数）：±0.01% 视为正常区间
FUND_NEUTRAL = 0.0001      # |r| ≤ 0.01% 中性
FUND_WARM = 0.0003         # 0.01%~0.03% 牛市常态温和偏多
FUND_HOT = 0.0005          # >0.05% 多头成本极高（强反向）
FUND_SHORT_CROWD = -0.0003  # < -0.03% 空头极端拥挤

# OI × 24h 价格方向交叉的最小有效幅度
OI_MIN_PRICE_MOVE = 0.5    # 价格 24h 变化 <0.5% 视为方向不明
OI_MIN_OI_MOVE = 1.0       # OI 24h 变化 <1% 视为持仓无明显增减

# 恐贪指数（逆向）
FNG_EXTREME_FEAR = 20
FNG_FEAR = 35
FNG_GREED = 65
FNG_EXTREME_GREED = 80

# 综合偏置三分阈值 与 极端因子判定（触发止盈止损收紧建议）
BIAS_TH = 15.0
EXTREME_FACTOR_SCORE = 40.0

# 大户 vs 全网背离（T1.6）：占比差阈值默认值（生效值走 jarvis_config
# signal.divergence_threshold，此常量仅作配置读取失败时的兜底）
DIVERGENCE_THRESHOLD_DEFAULT = 0.15

# 因子权重（liquidations/onchain 为预留位：数据可用后自动参与加权）
WEIGHTS = {"long_short": 0.30, "top_divergence": 0.20, "funding": 0.25,
           "oi": 0.25, "fng": 0.20, "liquidations": 0.15, "onchain": 0.15}

_BIAS_CN = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}


def _bias_of(score: float) -> str:
    if score >= BIAS_TH:
        return "bullish"
    if score <= -BIAS_TH:
        return "bearish"
    return "neutral"


def _clamp(v: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _factor(key: str, name: str, available: bool, value=None, display: str = "—",
            score: float = 0.0, note: str = "") -> dict:
    score = round(_clamp(score), 1)
    return {"key": key, "name": name, "available": available, "value": value,
            "display": display, "bias": _bias_of(score) if available else "neutral",
            "score": score if available else 0.0,
            "weight": WEIGHTS[key], "note": note}


# ═══════════════════════════ 单因子评分（纯函数） ═══════════════════════════

def score_long_short(ls: dict | None) -> dict:
    """多空比：中带顺向（多头略多=温和看多），极端带反向（拥挤=反噬风险）。"""
    if not ls or ls.get("long_pct") is None:
        return _factor("long_short", "多空比", False, note="多空比数据暂不可用")
    lp = float(ls["long_pct"])
    ratio = ls.get("ratio")
    if lp > LS_CROWD_HI:          # 极端挤多：反向看空，越拥挤越强
        score = -35.0 - (lp - LS_CROWD_HI) * 5.0
        note = (f"多头账户占比 {lp:.1f}% 超过 {LS_CROWD_HI:.0f}% 拥挤阈值——"
                "散户挤多常是反向信号，谨防多头踩踏回撤")
    elif lp > LS_TRANS_HI:        # 过渡带：顺向收益快速衰减转向警惕
        score = 30.0 - (lp - LS_TRANS_HI) * 13.0
        note = f"多头占比 {lp:.1f}% 进入拥挤过渡区（{LS_TRANS_HI:.0f}~{LS_CROWD_HI:.0f}%），偏多动能仍在但拥挤度升高"
    elif lp < LS_CROWD_LO:        # 极端挤空：反向看多（轧空燃料）
        score = 35.0 + (LS_CROWD_LO - lp) * 5.0
        note = (f"空头账户占比 {100 - lp:.1f}% 极端拥挤——"
                "空头回补易引发轧空，反向看多信号")
    elif lp < LS_TRANS_LO:
        score = -30.0 + (LS_TRANS_LO - lp) * 13.0
        note = f"空头占比 {100 - lp:.1f}% 进入拥挤过渡区，偏空但轧空风险在积累"
    else:                          # 中带 [40,60]：温和顺向
        score = (lp - 50.0) * 3.0
        lean = "多头略占优，人气温和偏多" if lp >= 50 else "空头略占优，人气温和偏空"
        note = f"多空账户比处于正常区间（{lp:.1f}% 多），{lean}；未达拥挤阈值"
    disp = f"多 {lp:.1f}% / 空 {100 - lp:.1f}%" + (f" · 比值 {ratio:.2f}" if ratio else "")
    return _factor("long_short", "多空比", True, value=ratio or lp, display=disp,
                   score=score, note=note)


def score_top_divergence(ls: dict | None, threshold: float | None = None) -> dict:
    """大户 vs 全网多空比背离（T1.6）：聪明钱与散户反向站队时跟随大户。

    触发条件（同时满足，任务口径）：
      1. 反向：大户多头占比与全网多头占比分踞 50% 两侧（一方偏多一方偏空）
      2. 差值：|大户多头占比 - 全网多头占比| ≥ threshold（小数，0.15=15 个百分点）
    触发后向**大户方向**加分（刚过阈值 ±35，随差值扩大最高 ±60）；
    未触发时因子可用但 0 分（仅展示两侧占比，供观察拥挤演变）。
    threshold 显式传参供单测锁定；缺省由 build_factors 从配置中心注入。
    """
    if not ls or ls.get("top_long_pct") is None or ls.get("long_pct") is None:
        return _factor("top_divergence", "大户背离", False,
                       note="大户多空比数据暂不可用（topLongShortAccountRatio）")
    thr = DIVERGENCE_THRESHOLD_DEFAULT if threshold is None else float(threshold)
    top_lp = float(ls["top_long_pct"])
    glob_lp = float(ls["long_pct"])
    diff = (top_lp - glob_lp) / 100.0          # 占比差（小数，正=大户更偏多）
    opposite = (top_lp - 50.0) * (glob_lp - 50.0) < 0   # 分踞 50% 两侧
    disp = f"大户多 {top_lp:.1f}% / 全网多 {glob_lp:.1f}%"
    if opposite and abs(diff) >= thr:
        sign = 1.0 if top_lp > 50.0 else -1.0   # 向大户方向加分
        score = sign * (35.0 + min((abs(diff) - thr) * 250.0, 25.0))
        top_cn = "偏多" if sign > 0 else "偏空"
        retail_cn = "偏空" if sign > 0 else "偏多"
        note = (f"大户{top_cn}（{top_lp:.1f}%）与全网{retail_cn}（{glob_lp:.1f}%）反向，"
                f"占比差 {abs(diff) * 100:.1f}pp ≥ 阈值 {thr * 100:.0f}pp——"
                f"聪明钱与散户背离，跟随大户{top_cn}")
    else:
        score = 0.0
        why = ("大户与全网同向" if not opposite
               else f"占比差 {abs(diff) * 100:.1f}pp 未达阈值 {thr * 100:.0f}pp")
        note = f"{why}，无背离信号（大户多 {top_lp:.1f}% / 全网多 {glob_lp:.1f}%）"
    return _factor("top_divergence", "大户背离", True,
                   value=round(diff, 4), display=disp, score=score, note=note)


def score_funding(funding_rates: dict | None, symbol: str = "BTCUSDT") -> dict:
    """资金费率：正费率过高=做多成本高/多头拥挤；负费率=空头付费（轧空燃料）。"""
    if not funding_rates:
        return _factor("funding", "资金费率", False, note="资金费率数据暂不可用")
    r = funding_rates.get(symbol)
    if r is None:  # 目标币种缺失时退化为已有币种均值
        vals = [float(v) for v in funding_rates.values()]
        if not vals:
            return _factor("funding", "资金费率", False, note="资金费率数据暂不可用")
        r = sum(vals) / len(vals)
    r = float(r)
    if r > FUND_HOT:
        score = -40.0 - min((r - FUND_HOT) * 40000.0, 40.0)
        note = (f"8h 费率 {r * 100:+.4f}% 极高——多头为持仓付出高成本、杠杆多头拥挤，"
                "谨慎追多，回调风险大")
    elif r > FUND_WARM:
        score = -20.0 - (r - FUND_WARM) * 100000.0
        note = f"8h 费率 {r * 100:+.4f}% 偏高——做多成本抬升，多头开始拥挤"
    elif r > FUND_NEUTRAL:
        score = 10.0
        note = f"8h 费率 {r * 100:+.4f}% 温和为正——牛市常态，需求健康不拥挤"
    elif r >= -FUND_NEUTRAL:
        score = 0.0
        note = f"8h 费率 {r * 100:+.4f}% 处于中性区间（±0.01%），多空成本均衡"
    elif r >= FUND_SHORT_CROWD:
        score = 20.0
        note = f"8h 费率 {r * 100:+.4f}% 转负——空头付费维持仓位，存在轧空反弹燃料"
    else:
        score = 45.0
        note = (f"8h 费率 {r * 100:+.4f}% 深度为负——空头极端拥挤，"
                "任何利多都可能触发剧烈轧空")
    return _factor("funding", "资金费率", True, value=round(r, 8),
                   display=f"{symbol.replace('USDT', '')} {r * 100:+.4f}%/8h",
                   score=score, note=note)


def score_oi(oi: dict | None, price_24h: dict | None) -> dict:
    """持仓量 OI × 24h 价格方向交叉：量价配合判断趋势健康度。"""
    if not oi or oi.get("change_pct") is None:
        return _factor("oi", "持仓量 OI", False, note="持仓量数据暂不可用")
    oi_chg = float(oi["change_pct"])
    disp_val = float(oi.get("value") or 0)
    disp = f"${disp_val / 1e9:.2f}B · 24h {oi_chg:+.1f}%"
    p_chg = price_24h.get("change_pct") if price_24h else None
    if p_chg is None:
        return _factor("oi", "持仓量 OI", True, value=oi_chg, display=disp, score=0.0,
                       note="缺少 24h 价格方向，无法做量价交叉判断（仅展示 OI 变化）")
    p_chg = float(p_chg)
    if abs(p_chg) < OI_MIN_PRICE_MOVE or abs(oi_chg) < OI_MIN_OI_MOVE:
        return _factor("oi", "持仓量 OI", True, value=oi_chg, display=disp, score=0.0,
                       note=f"价格 24h {p_chg:+.1f}%、OI {oi_chg:+.1f}% 变化均不显著，量价无明确信号")
    if p_chg > 0 and oi_chg > 0:
        score, note = 40.0, f"价涨 {p_chg:+.1f}% + OI 增 {oi_chg:+.1f}%：新资金顺势进场，上涨趋势健康"
    elif p_chg > 0 and oi_chg < 0:
        score, note = -20.0, f"价涨 {p_chg:+.1f}% 但 OI 降 {oi_chg:+.1f}%：空头回补推动的反弹，缺乏新多接力、动能存疑"
    elif p_chg < 0 and oi_chg > 0:
        score, note = -40.0, f"价跌 {p_chg:+.1f}% + OI 增 {oi_chg:+.1f}%：新空顺势加仓，下跌趋势健康、别急着抄底"
    else:
        score, note = 15.0, f"价跌 {p_chg:+.1f}% + OI 降 {oi_chg:+.1f}%：多头去杠杆接近尾声，抛压趋弱"
    return _factor("oi", "持仓量 OI", True, value=oi_chg, display=disp, score=score, note=note)


def score_fng(fng: dict | None) -> dict:
    """恐慌贪婪指数：经典逆向指标——别人恐惧我贪婪。"""
    if not fng or fng.get("value") is None:
        return _factor("fng", "恐贪指数", False, note="恐贪指数暂不可用")
    v = int(fng["value"])
    cls = fng.get("classification") or ""
    if v <= FNG_EXTREME_FEAR:
        score, note = 40.0, f"指数 {v}（极度恐惧）——历史上极度恐惧多为阶段性底部区域，逆向偏多"
    elif v <= FNG_FEAR:
        score, note = 15.0, f"指数 {v}（恐惧）——市场情绪偏冷，下行空间被悲观预期部分消化"
    elif v < FNG_GREED:
        score, note = 0.0, f"指数 {v}（中性）——情绪不构成方向依据"
    elif v < FNG_EXTREME_GREED:
        score, note = -15.0, f"指数 {v}（贪婪）——情绪偏热，追高性价比下降"
    else:
        score, note = -40.0, f"指数 {v}（极度贪婪）——历史上极度贪婪常临近阶段顶部，逆向偏空"
    return _factor("fng", "恐贪指数", True, value=v, display=f"{v} · {cls}",
                   score=score, note=note)


def score_liquidations(liq: dict | None) -> dict:
    """爆仓因子（预留接口位）：Coinglass key 配置后自动参与计分。

    接入后口径：24h 空头爆仓额远大于多头 → 轧空动能已释放（偏空转折警示）；
    多头大规模爆仓后 → 杠杆出清、超跌反弹土壤（逆向偏多）。
    """
    if not liq or liq.get("long_usd") is None or liq.get("short_usd") is None:
        return _factor("liquidations", "爆仓数据", False, display="未接入",
                       note="需 Coinglass API key；接入后自动参与计分："
                            "空头大额爆仓=轧空动能释放、多头大额爆仓=杠杆出清反弹土壤")
    long_usd, short_usd = float(liq["long_usd"]), float(liq["short_usd"])
    total = long_usd + short_usd
    if total <= 0:
        return _factor("liquidations", "爆仓数据", True, value=0.0,
                       display="24h 无显著爆仓", score=0.0, note="爆仓规模可忽略")
    dominance = (long_usd - short_usd) / total  # +1 全是多头爆仓 … -1 全是空头爆仓
    score = dominance * 35.0  # 多头被大量爆仓 → 杠杆出清 → 逆向偏多
    note = (f"24h 多头爆仓 ${long_usd / 1e6:.0f}M / 空头爆仓 ${short_usd / 1e6:.0f}M——"
            + ("多头杠杆集中出清，超跌反弹土壤" if dominance > 0.2 else
               "空头集中爆仓，轧空动能已部分释放" if dominance < -0.2 else "多空爆仓均衡"))
    return _factor("liquidations", "爆仓数据", True, value=round(dominance, 3),
                   display=f"多爆 ${long_usd / 1e6:.0f}M / 空爆 ${short_usd / 1e6:.0f}M",
                   score=score, note=note)


def score_onchain(onchain: dict | None) -> dict:
    """链上因子（预留接口位）：Glassnode key 配置后自动参与计分。

    接入后口径：交易所净流入为正（筹码涌向交易所）→ 抛压偏空；
    净流出（提币囤仓）→ 惜售偏多；活跃地址趋势做辅助确认。
    """
    if not onchain or onchain.get("exchange_netflow_usd") is None:
        return _factor("onchain", "链上指标", False, display="未接入",
                       note="需 Glassnode API key；接入后自动参与计分："
                            "交易所净流入=抛压、净流出=惜售囤仓")
    netflow = float(onchain["exchange_netflow_usd"])
    scale = min(abs(netflow) / 200e6, 1.0)  # ±2 亿美金视为满幅
    score = -scale * 35.0 if netflow > 0 else scale * 35.0
    note = ("筹码净流入交易所，短期抛压偏空" if netflow > 0 else "筹码净流出交易所，惜售囤仓偏多")
    return _factor("onchain", "链上指标", True, value=netflow,
                   display=f"净流{'入' if netflow > 0 else '出'} ${abs(netflow) / 1e6:.0f}M",
                   score=score, note=note)


# ═══════════════════════════ 综合研判（纯函数） ═══════════════════════════

def _divergence_threshold() -> float:
    """从配置中心读大户背离阈值；任何异常回退内置默认（决策链不被配置拖垮）。"""
    try:
        import jarvis_config
        v = float(jarvis_config.get("divergence_threshold"))
        return v if 0.0 < v < 1.0 else DIVERGENCE_THRESHOLD_DEFAULT
    except Exception:  # noqa: BLE001
        return DIVERGENCE_THRESHOLD_DEFAULT


def _top_divergence_summary(ls: dict | None, factor: dict, threshold: float) -> dict:
    """背离状态摘要块（前端 MarketIntel 背离小卡直取，免于解析因子 note）。"""
    available = bool(factor.get("available"))
    active = available and abs(float(factor.get("score") or 0.0)) > 0.0
    top_lp = float(ls["top_long_pct"]) if available else None
    glob_lp = float(ls["long_pct"]) if available else None
    top_bias = ("bullish" if top_lp > 50 else "bearish" if top_lp < 50 else "neutral") \
        if top_lp is not None else "neutral"
    retail_bias = ("bullish" if glob_lp > 50 else "bearish" if glob_lp < 50 else "neutral") \
        if glob_lp is not None else "neutral"
    diff_pp = round(abs(top_lp - glob_lp), 1) if available else None
    if active:
        lean_cn = "偏多" if top_bias == "bullish" else "偏空"
        suggestion = f"大户与散户反向站队，建议倾向跟随大户{lean_cn}；散户拥挤方向作反指参考"
    elif available:
        suggestion = "大户与全网未构成背离，该因子暂不提供方向倾向"
    else:
        suggestion = "大户多空比数据暂不可用"
    return {"available": available, "active": active,
            "top_bias": top_bias, "retail_bias": retail_bias,
            "top_long_pct": top_lp, "global_long_pct": glob_lp,
            "diff_pp": diff_pp, "threshold_pp": round(threshold * 100, 1),
            "score": factor.get("score", 0.0), "note": factor.get("note", ""),
            "suggestion": suggestion}


def build_factors(intel: dict, symbol: str = "BTCUSDT",
                  divergence_threshold: float | None = None) -> dict:
    """intel（jarvis_market_intel.get_intel() 返回体）→ 因子集 + 综合情绪分。

    多空比/OI/价格为 BTC 口径（大盘情绪基准，山寨与大盘高度联动）；
    资金费率按 symbol 取对应币种（缺失退化为均值）。
    divergence_threshold 显式传参供单测锁定，缺省读配置中心。
    """
    sym = (symbol if symbol.upper().endswith("USDT") else symbol + "USDT").upper()
    thr = _divergence_threshold() if divergence_threshold is None else float(divergence_threshold)
    ls = intel.get("long_short")
    top_div = score_top_divergence(ls, thr)
    factors = [
        score_long_short(ls),
        top_div,
        score_funding(intel.get("funding_rate"), sym),
        score_oi(intel.get("oi"), intel.get("price_24h")),
        score_fng(intel.get("fng")),
        score_liquidations(intel.get("liquidations")),
        score_onchain(intel.get("onchain")),
    ]
    live = [f for f in factors if f["available"]]
    wsum = sum(f["weight"] for f in live)
    score = round(_clamp(sum(f["score"] * f["weight"] for f in live) / wsum), 1) if wsum else 0.0
    bias = _bias_of(score)

    # 一句话结论：综合偏置 + 最强的两个有效因子
    tops = sorted(live, key=lambda f: abs(f["score"]), reverse=True)[:2]
    drivers = "；".join(f"{f['name']}{_BIAS_CN[f['bias']]}（{f['score']:+.0f}）"
                        for f in tops if abs(f["score"]) >= 5) or "各因子均接近中性"
    headline = f"供需情绪综合 {score:+.0f} 分（{_BIAS_CN[bias]}）。主导因子：{drivers}"

    warnings = _build_warnings(factors)
    return {"symbol": sym, "score": score, "bias": bias, "headline": headline,
            "factors": factors, "warnings": warnings,
            "top_divergence": _top_divergence_summary(ls, top_div, thr),
            "sl_tp_advice": _sl_tp_advice(score, factors)}


def _build_warnings(factors: list[dict]) -> list[str]:
    """极端因子 → 醒目警示（拥挤/逆向风险，独立于技术面）。"""
    out = []
    for f in factors:
        if f["available"] and abs(f["score"]) >= EXTREME_FACTOR_SCORE:
            out.append(f"{f['name']}处于极端区：{f['note']}")
    return out


def _sl_tp_advice(score: float, factors: list[dict]) -> str | None:
    """情绪极端时的止盈止损收紧建议（只做建议展示，不强制改单）。"""
    extreme = [f for f in factors if f["available"] and abs(f["score"]) >= EXTREME_FACTOR_SCORE]
    if not extreme and abs(score) < 45:
        return None
    side = "多头" if score < 0 else "空头"
    names = "、".join(f["name"] for f in extreme) or "综合情绪"
    return (f"{names}进入极端区，{side}持仓建议收紧风控：止盈分档前移（如 1:2 目标降为 1:1.5 先落袋）、"
            "止损上移至结构位内侧，避免情绪逆转时利润回吐")


# ═══════════════════════════ 与技术面共识融合（纯函数） ═══════════════════════════

def apply_to_consensus(cons: dict, sentiment: dict) -> dict:
    """把情绪层叠加到十二套技术共识（consensus/consensus_multi_tf 输出）上。

    返回 cons 的浅拷贝，新增 sentiment 键并微调 reasoning；不修改原 dict、
    不改变 direction / confidence 原始值（调整值放 sentiment.adjusted_confidence，
    由消费方自行决定是否采纳）。
    """
    out = dict(cons)
    s = float(sentiment.get("score") or 0.0)
    direction = cons.get("direction") or "neutral"
    confidence = float(cons.get("confidence") or 0.0)

    tech_val = {"bullish": 1, "bearish": -1}.get(direction, 0)
    senti_val = 1 if s >= BIAS_TH else (-1 if s <= -BIAS_TH else 0)

    if tech_val == 0 or senti_val == 0:
        alignment, delta = "neutral", 0.0
        summary = "情绪面不构成方向修正"
    elif tech_val == senti_val:
        alignment = "aligned"
        delta = round(min(0.10, abs(s) / 100 * 0.15), 3)  # 同向：小幅增益，上限 +0.10
        summary = f"情绪面与技术面同向共振（{s:+.0f}），置信度 +{delta:.2f}"
    else:
        alignment = "divergent"
        if abs(s) >= 45:
            delta = -round(min(0.20, abs(s) / 100 * 0.30), 3)  # 极端反向：显著降级
            summary = f"情绪面与技术面显著背离（{s:+.0f}），置信度 {delta:+.2f}，谨慎执行"
        else:
            delta = -0.05
            summary = f"情绪面轻度背离（{s:+.0f}），置信度 {delta:+.2f}"

    warnings = list(sentiment.get("warnings") or [])
    if alignment == "divergent" and abs(s) >= 45:
        dir_cn = "看多" if tech_val > 0 else "看空"
        senti_cn = "拥挤/过热" if senti_val < 0 else "恐慌/超卖"
        warnings.insert(0, f"技术面{dir_cn}但供需情绪极端{senti_cn}——按计划轻仓执行或等情绪降温")

    out["sentiment"] = {
        "score": s,
        "bias": sentiment.get("bias") or "neutral",
        "alignment": alignment,
        "confidence_delta": delta,
        "adjusted_confidence": round(_clamp(confidence + delta, 0.0, 1.0), 3),
        "headline": sentiment.get("headline") or "",
        "warnings": warnings,
        "sl_tp_advice": sentiment.get("sl_tp_advice"),
        "factors": sentiment.get("factors") or [],
    }
    if out.get("reasoning"):
        out["reasoning"] = f"{out['reasoning']}。情绪面：{summary}"
    return out


# ═══════════════════════════ 门面（带 IO） ═══════════════════════════

def assess(symbol: str = "BTCUSDT") -> dict:
    """拉市场情报缓存 → 因子化 → 综合研判。数据源异常时 ok=False 不抛出。"""
    try:
        import jarvis_market_intel as jmi
        intel = jmi.get_intel()
    except Exception as exc:  # noqa: BLE001 — 情绪层失败不拖垮调用方主流程
        return {"ok": False, "symbol": symbol, "error": repr(exc)[:200]}
    out = build_factors(intel, symbol)
    out.update({"ok": True, "as_of": int(time.time()),
                "intel_updated_at": intel.get("updated_at"),
                "unavailable": intel.get("unavailable")})
    return out


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(assess(), ensure_ascii=False, indent=2))
