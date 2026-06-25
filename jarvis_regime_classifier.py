#!/usr/bin/env python3
"""贾维斯 JARVIS — 行情分类器（Market Regime Classifier）。

判定当前市场处于 震荡（ranging）/ 单边趋势（trending）/ 突破（breakout）
三种状态之一，并给出置信度和方向。

支持多时间框架输入（15m + 1h + 4h），高级别权重更大。

核心指标：
  - ADX（平均方向指数）：趋势强度
  - 布林带宽度 + 宽度变化率：波动率状态
  - EMA 三线排列（20/50/200）：趋势方向与一致性
  - 成交量比率：突破确认
  - ATR 变化率：波动率趋势

用法：
  python jarvis_regime_classifier.py --symbol BTCUSDT
  python jarvis_regime_classifier.py --symbol BTCUSDT --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
import pandas as pd


# ═══════════════════════════ 数据结构 ═══════════════════════════

@dataclass
class RegimeResult:
    """行情分类结果。"""
    regime: str          # "trending" | "ranging" | "breakout"
    confidence: float    # 0.0 ~ 1.0
    direction: str       # "bullish" | "bearish" | "neutral"
    strength: float      # 0.0 ~ 1.0，趋势/震荡强度
    sub_regime: str      # "strong_trend" | "weak_trend" | "tight_range" | ...
    reasoning: str       # 中文判定理由
    indicators: dict     # 原始指标值，供调试和展示

    def to_dict(self) -> dict:
        return asdict(self)


# ═══════════════════════════ 技术指标计算 ═══════════════════════════

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range。"""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index — 趋势强度指标。"""
    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr_vals = _atr(high, low, close, period)
    atr_safe = atr_vals.replace(0, np.nan)

    plus_di = 100 * (plus_dm.rolling(window=period, min_periods=1).mean() / atr_safe)
    minus_di = 100 * (minus_dm.rolling(window=period, min_periods=1).mean() / atr_safe)

    dx_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * ((plus_di - minus_di).abs() / dx_sum)
    adx_val = dx.rolling(window=period, min_periods=1).mean()
    return adx_val.fillna(0)


def _bollinger_width(close: pd.Series, period: int = 20, std_mult: float = 2.0) -> pd.Series:
    """布林带宽度（百分比）。"""
    ma = close.rolling(window=period, min_periods=1).mean()
    std = close.rolling(window=period, min_periods=1).std()
    upper = ma + std_mult * std
    lower = ma - std_mult * std
    ma_safe = ma.replace(0, np.nan)
    width = (upper - lower) / ma_safe * 100
    return width.fillna(0)


def _volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    """当前成交量 / 过去 N 根均量。"""
    avg_vol = volume.rolling(window=period, min_periods=1).mean()
    avg_safe = avg_vol.replace(0, np.nan)
    return (volume / avg_safe).fillna(1.0)


def _ema_alignment(close: pd.Series) -> dict[str, Any]:
    """EMA 三线排列状态。
    
    Returns:
        {
            "ema20": float, "ema50": float, "ema200": float,
            "aligned_bullish": bool,   # 20 > 50 > 200
            "aligned_bearish": bool,   # 20 < 50 < 200
            "score": float,            # -1(强空) ~ +1(强多)
        }
    """
    ema20 = _ema(close, 20).iloc[-1]
    ema50 = _ema(close, 50).iloc[-1]
    ema200 = _ema(close, 200).iloc[-1] if len(close) >= 200 else _ema(close, min(len(close), 100)).iloc[-1]

    aligned_bull = ema20 > ema50 > ema200
    aligned_bear = ema20 < ema50 < ema200

    score = 0.0
    if ema200 != 0:
        score = (ema20 - ema200) / abs(ema200) * 100
        score = max(-1.0, min(1.0, score / 5.0))

    return {
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "ema200": round(ema200, 2),
        "aligned_bullish": aligned_bull,
        "aligned_bearish": aligned_bear,
        "score": round(score, 3),
    }


# ═══════════════════════════ 单时间框架分类 ═══════════════════════════

def classify_single_tf(df: pd.DataFrame) -> RegimeResult:
    """对单个时间框架的 K 线数据进行行情分类。
    
    Args:
        df: 包含 open/high/low/close/volume 列的 DataFrame，至少 50 行
    
    Returns:
        RegimeResult
    """
    if len(df) < 20:
        return RegimeResult(
            regime="unknown", confidence=0.0, direction="neutral",
            strength=0.0, sub_regime="insufficient_data",
            reasoning="数据不足（< 20 根 K 线）", indicators={},
        )

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    adx_series = _adx(high, low, close, 14)
    adx_val = adx_series.iloc[-1]

    bb_width = _bollinger_width(close, 20)
    bb_width_now = bb_width.iloc[-1]
    bb_width_avg = bb_width.rolling(20, min_periods=5).mean().iloc[-1]
    bb_expanding = bb_width_now > bb_width_avg * 1.2

    vol_ratio = _volume_ratio(volume, 20)
    vol_ratio_now = vol_ratio.iloc[-1]

    atr_series = _atr(high, low, close, 14)
    atr_now = atr_series.iloc[-1]
    atr_prev = atr_series.iloc[-6] if len(atr_series) >= 6 else atr_now
    atr_change = (atr_now - atr_prev) / max(atr_prev, 1e-8)

    ema_info = _ema_alignment(close)

    # ─────── 行情判定逻辑 ───────

    trending_score = 0.0
    ranging_score = 0.0
    breakout_score = 0.0
    reasons = []

    # ADX 判定
    if adx_val >= 35:
        trending_score += 0.4
        reasons.append(f"ADX={adx_val:.1f}（强趋势）")
    elif adx_val >= 25:
        trending_score += 0.25
        reasons.append(f"ADX={adx_val:.1f}（中等趋势）")
    elif adx_val < 20:
        ranging_score += 0.35
        reasons.append(f"ADX={adx_val:.1f}（无趋势/震荡）")
    else:
        ranging_score += 0.15
        trending_score += 0.1
        reasons.append(f"ADX={adx_val:.1f}（偏弱）")

    # 布林带宽度判定
    if bb_width_now < bb_width_avg * 0.7:
        ranging_score += 0.2
        reasons.append("布林带收窄（低波动）")
    elif bb_expanding and vol_ratio_now > 1.5:
        breakout_score += 0.35
        reasons.append(f"布林带扩张 + 放量{vol_ratio_now:.1f}x（突破特征）")
    elif bb_expanding:
        trending_score += 0.15
        reasons.append("布林带扩张（波动放大）")

    # EMA 排列判定
    if ema_info["aligned_bullish"] or ema_info["aligned_bearish"]:
        trending_score += 0.25
        direction_text = "多头" if ema_info["aligned_bullish"] else "空头"
        reasons.append(f"三线{direction_text}排列")
    else:
        ranging_score += 0.15
        reasons.append("均线交织（无明确方向）")

    # 成交量判定
    if vol_ratio_now > 2.0:
        breakout_score += 0.2
        reasons.append(f"量比{vol_ratio_now:.1f}x（放量异常）")
    elif vol_ratio_now > 1.5:
        breakout_score += 0.1
        trending_score += 0.05

    # ATR 变化率
    if atr_change > 0.3:
        breakout_score += 0.15
        reasons.append(f"ATR 急升{atr_change:.0%}（波动率爆发）")
    elif atr_change < -0.2:
        ranging_score += 0.1
        reasons.append("ATR 下降（波动率收缩）")

    # ─────── 最终判定 ───────

    scores = {
        "trending": trending_score,
        "ranging": ranging_score,
        "breakout": breakout_score,
    }
    regime = max(scores, key=scores.get)  # type: ignore[arg-type]
    total = sum(scores.values()) or 1.0
    confidence = scores[regime] / total

    # 方向判定
    ema_score = ema_info["score"]
    price_vs_ema50 = (close.iloc[-1] - ema_info["ema50"]) / max(abs(ema_info["ema50"]), 1e-8)

    if ema_score > 0.2 or price_vs_ema50 > 0.01:
        direction = "bullish"
    elif ema_score < -0.2 or price_vs_ema50 < -0.01:
        direction = "bearish"
    else:
        direction = "neutral"

    # 子类型
    if regime == "trending":
        sub = "strong_trend" if adx_val >= 35 else "weak_trend"
        strength = min(1.0, adx_val / 50)
    elif regime == "ranging":
        sub = "tight_range" if bb_width_now < bb_width_avg * 0.6 else "wide_range"
        strength = min(1.0, (50 - adx_val) / 30) if adx_val < 50 else 0.0
    else:
        sub = "volume_breakout" if vol_ratio_now > 2.0 else "volatility_breakout"
        strength = min(1.0, vol_ratio_now / 3.0)

    indicators = {
        "adx": round(adx_val, 2),
        "bb_width": round(bb_width_now, 2),
        "bb_width_avg": round(bb_width_avg, 2),
        "bb_expanding": bb_expanding,
        "volume_ratio": round(vol_ratio_now, 2),
        "atr": round(atr_now, 4),
        "atr_change_pct": round(atr_change * 100, 1),
        "ema_alignment": ema_info,
        "scores": {k: round(v, 3) for k, v in scores.items()},
    }

    return RegimeResult(
        regime=regime,
        confidence=round(confidence, 3),
        direction=direction,
        strength=round(strength, 3),
        sub_regime=sub,
        reasoning="；".join(reasons),
        indicators=indicators,
    )


# ═══════════════════════════ 多时间框架融合 ═══════════════════════════

MTF_WEIGHTS = {"15m": 0.3, "1h": 0.3, "4h": 0.4}


def classify_multi_tf(
    klines_15m: pd.DataFrame | None = None,
    klines_1h: pd.DataFrame | None = None,
    klines_4h: pd.DataFrame | None = None,
) -> RegimeResult:
    """多时间框架行情分类。
    
    高级别权重更大：4h(40%) > 1h(30%) >= 15m(30%)。
    至少需要一个时间框架的数据。
    
    Returns:
        融合后的 RegimeResult
    """
    tf_data = {}
    if klines_15m is not None and len(klines_15m) >= 20:
        tf_data["15m"] = klines_15m
    if klines_1h is not None and len(klines_1h) >= 20:
        tf_data["1h"] = klines_1h
    if klines_4h is not None and len(klines_4h) >= 20:
        tf_data["4h"] = klines_4h

    if not tf_data:
        return RegimeResult(
            regime="unknown", confidence=0.0, direction="neutral",
            strength=0.0, sub_regime="no_data",
            reasoning="无可用数据", indicators={},
        )

    if len(tf_data) == 1:
        tf_key = list(tf_data.keys())[0]
        return classify_single_tf(tf_data[tf_key])

    results: dict[str, RegimeResult] = {}
    for tf, df in tf_data.items():
        results[tf] = classify_single_tf(df)

    regime_scores = {"trending": 0.0, "ranging": 0.0, "breakout": 0.0}
    direction_score = 0.0
    strength_sum = 0.0
    total_weight = 0.0
    all_reasons = []

    for tf, result in results.items():
        w = MTF_WEIGHTS.get(tf, 0.3)
        total_weight += w

        if result.regime in regime_scores:
            regime_scores[result.regime] += w * result.confidence

        dir_val = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}.get(result.direction, 0.0)
        direction_score += w * dir_val
        strength_sum += w * result.strength

        all_reasons.append(f"[{tf}] {result.regime}({result.confidence:.0%}) {result.direction}")

    if total_weight > 0:
        for k in regime_scores:
            regime_scores[k] /= total_weight
        direction_score /= total_weight
        strength_sum /= total_weight

    regime = max(regime_scores, key=regime_scores.get)  # type: ignore[arg-type]
    confidence = regime_scores[regime]

    if direction_score > 0.3:
        direction = "bullish"
    elif direction_score < -0.3:
        direction = "bearish"
    else:
        direction = "neutral"

    # 多级别一致性加成
    regime_set = {r.regime for r in results.values() if r.regime != "unknown"}
    direction_set = {r.direction for r in results.values() if r.direction != "neutral"}

    consistency_bonus = ""
    if len(regime_set) == 1 and len(results) >= 2:
        confidence = min(1.0, confidence * 1.2)
        consistency_bonus = "多级别一致"
    elif len(regime_set) == len(results) and len(results) >= 2:
        confidence *= 0.7
        consistency_bonus = "多级别分歧"

    if len(direction_set) == 1 and len(results) >= 2:
        strength_sum = min(1.0, strength_sum * 1.15)

    if regime == "trending":
        sub = "strong_trend" if confidence > 0.6 else "weak_trend"
    elif regime == "ranging":
        sub = "tight_range" if strength_sum > 0.5 else "wide_range"
    else:
        sub = "multi_tf_breakout"

    reasoning_parts = all_reasons
    if consistency_bonus:
        reasoning_parts.append(f"→ {consistency_bonus}")
    reasoning_parts.append(f"→ 综合判定: {regime} {direction}")

    indicators = {
        "multi_tf_scores": {k: round(v, 3) for k, v in regime_scores.items()},
        "direction_score": round(direction_score, 3),
        "consistency": consistency_bonus or "mixed",
        "per_tf": {tf: r.to_dict() for tf, r in results.items()},
    }

    return RegimeResult(
        regime=regime,
        confidence=round(confidence, 3),
        direction=direction,
        strength=round(strength_sum, 3),
        sub_regime=sub,
        reasoning="；".join(reasoning_parts),
        indicators=indicators,
    )


# ═══════════════════════════ K线拉取（复用 crypto_data 逻辑） ═══════════════════════════

def _fetch_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame | None:
    """从 Binance Futures 拉取 K 线。"""
    try:
        import requests
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        rows = []
        for k in raw:
            rows.append({
                "time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return pd.DataFrame(rows)
    except Exception as e:
        print(f"[REGIME] K线拉取失败 {symbol} {interval}: {e}", file=sys.stderr)
        return None


# ═══════════════════════════ 公开 API ═══════════════════════════

def classify(
    symbol: str = "BTCUSDT",
    klines_15m: pd.DataFrame | None = None,
    klines_1h: pd.DataFrame | None = None,
    klines_4h: pd.DataFrame | None = None,
    fetch_missing: bool = True,
) -> RegimeResult:
    """行情分类主入口。
    
    可传入已有 K 线数据，也可自动从 Binance 拉取。
    
    Args:
        symbol: 合约代码
        klines_15m/1h/4h: 已有 K 线（DataFrame with open/high/low/close/volume）
        fetch_missing: 如果某级别数据缺失，是否自动拉取
    
    Returns:
        RegimeResult
    """
    if fetch_missing:
        if klines_15m is None:
            klines_15m = _fetch_klines(symbol, "15m", 200)
        if klines_1h is None:
            klines_1h = _fetch_klines(symbol, "1h", 200)
        if klines_4h is None:
            klines_4h = _fetch_klines(symbol, "4h", 200)

    return classify_multi_tf(klines_15m, klines_1h, klines_4h)


def classify_from_klines(df: pd.DataFrame) -> RegimeResult:
    """便捷方法：只用一个级别的 K 线数据分类。"""
    return classify_single_tf(df)


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="行情分类器")
    parser.add_argument("--symbol", default="BTCUSDT", help="合约代码")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--single", choices=["15m", "1h", "4h"], help="只用单个级别")
    args = parser.parse_args()

    print(f"正在分析 {args.symbol} 行情...", flush=True)

    if args.single:
        df = _fetch_klines(args.symbol, args.single, 200)
        if df is None:
            print("数据拉取失败", file=sys.stderr)
            sys.exit(1)
        result = classify_single_tf(df)
    else:
        result = classify(args.symbol)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        regime_cn = {"trending": "趋势", "ranging": "震荡", "breakout": "突破", "unknown": "未知"}
        dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
        sub_cn = {
            "strong_trend": "强趋势", "weak_trend": "弱趋势",
            "tight_range": "窄幅震荡", "wide_range": "宽幅震荡",
            "volume_breakout": "放量突破", "volatility_breakout": "波动率突破",
            "multi_tf_breakout": "多级别突破", "insufficient_data": "数据不足",
            "no_data": "无数据",
        }

        print(f"\n{'='*50}")
        print(f"  {args.symbol} 行情分类结果")
        print(f"{'='*50}")
        print(f"  行情类型：{regime_cn.get(result.regime, result.regime)}")
        print(f"  置信度：  {result.confidence:.0%}")
        print(f"  方向：    {dir_cn.get(result.direction, result.direction)}")
        print(f"  强度：    {result.strength:.0%}")
        print(f"  子类型：  {sub_cn.get(result.sub_regime, result.sub_regime)}")
        print(f"  判定理由：{result.reasoning}")

        if "per_tf" in result.indicators:
            print(f"\n  各级别详情：")
            for tf, data in result.indicators["per_tf"].items():
                r_cn = regime_cn.get(data["regime"], data["regime"])
                d_cn = dir_cn.get(data["direction"], data["direction"])
                print(f"    [{tf}] {r_cn} {d_cn} 置信度{data['confidence']:.0%}")
        elif "adx" in result.indicators:
            ind = result.indicators
            print(f"\n  指标详情：")
            print(f"    ADX:       {ind['adx']:.1f}")
            print(f"    布林带宽:   {ind['bb_width']:.2f}%")
            print(f"    量比:       {ind['volume_ratio']:.2f}x")
            print(f"    ATR 变化:   {ind['atr_change_pct']:+.1f}%")
            ema = ind.get("ema_alignment", {})
            if ema:
                print(f"    EMA 排列:   20={ema['ema20']:.0f} 50={ema['ema50']:.0f} 200={ema['ema200']:.0f}")

        print()


if __name__ == "__main__":
    _cli()
