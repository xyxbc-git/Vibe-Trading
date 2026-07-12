#!/usr/bin/env python3
"""贾维斯 JARVIS — 走势预测引擎（规则概率 + AI 研判双轨）。

「能不能预测走势」的工程化落地：不做重型 ML 训练，用两条轨互补——
  规则轨（默认，无外部依赖）：
    12 套技术共识分数 → 三分类方向概率（up/down/sideways）；
    动量 + 共识倾斜 → 每根漂移；ATR×√h 扩散 → 目标区间与期望路径。
  AI 轨（可选增强）：
    复用 jarvis_llm_config 统一通道，把行情摘要 + 信号喂给 LLM 产出中文
    rationale 与概率修正（修正量夹在 ±0.15 内防幻觉大幅偏移）；
    未配 Key / 调用失败 → 优雅降级为纯规则轨，绝不阻塞接口。

输出契约（与桌面端 K 线预测可视化 UI 约定，字段名不可擅改）：
  {symbol, timeframe, generatedAt, horizon, direction, probability{up,down,sideways},
   targetZone{high,low}, path[{t,price}], confidence, rationale, signals[]}
  附加字段：ok / engine("rule"|"rule+llm") / mock / disclaimer / basis(口径说明)。

准确率回测（可信度基准）：
  backtest() 用历史数据逐窗口「只看过去 → 预测 → 对照实际」，输出方向命中率
  （含/不含横盘两种口径）与目标区间覆盖率。

纪律：预测只吃**已收盘** bar（fetch_klines_df drop_unclosed=True，与
jarvis_intraday_predict 同款防前瞻）；概率是统计参考，非投资建议。

用法：
  python jarvis_trend_predict.py --symbol BTCUSDT --timeframe 15m --horizon 16
  python jarvis_trend_predict.py --symbol BTCUSDT --timeframe 1h --json --no-llm
  python jarvis_trend_predict.py --backtest --symbol BTCUSDT --timeframe 15m --windows 40
  python jarvis_trend_predict.py --mock
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from typing import Any

import numpy as np
import pandas as pd

import jarvis_twelve_systems as jts

DISCLAIMER = "本预测为量化概率参考，非投资建议；加密货币波动剧烈，盈亏自负，请严格控制风险。"

TF_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
ALLOWED_TFS = ("5m", "15m", "30m", "1h", "4h", "1d")   # 契约主打 5m~4h；1d 顺带支持
HORIZON_DEFAULT = 16
HORIZON_MAX = 96
MIN_BARS = 60            # 低于此根数拒绝预测（swing 结构与 ATR 都不可靠）
FETCH_BARS = 400         # 实时预测拉取根数

# ── 概率/区间口径常量（改这里=改预测口径，须同步回测重校准）─────────────
PROB_TILT_K = 1.2        # score → 方向倾斜的 tanh 陡度
SIDE_BASE = 0.40         # score=0 时横盘概率
SIDE_SLOPE = 0.25        # 横盘概率随 |score| 的衰减斜率
ZONE_Z = 0.85            # 目标区间半宽 = ZONE_Z × ATR × √h
DRIFT_CAP_ATR = 0.6      # 每根漂移上限（xATR）
SIDEWAYS_RET_ATR = 0.35  # 回测实际走势三分类死区：|ret| ≤ 0.35×ATR%×√h 判横盘

_DIR_CN = {"up": "看涨", "down": "看跌", "sideways": "横盘"}


# ═══════════════════════════ 基础工具 ═══════════════════════════


def _iso(ts_sec: float) -> str:
    """UTC ISO8601（秒级，Z 后缀）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_sec))


def _norm_symbol(symbol: str) -> str:
    sym = (symbol or "BTCUSDT").upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    return sym


def _norm_tf(timeframe: str) -> str:
    return timeframe if timeframe in ALLOWED_TFS else "15m"


def _norm_horizon(horizon: Any) -> int:
    try:
        h = int(horizon)
    except (TypeError, ValueError):
        h = HORIZON_DEFAULT
    return max(1, min(HORIZON_MAX, h))


def probability_from_score(score: float) -> dict[str, float]:
    """共识总分 ∈ [-1,1] → 三分类概率（透明公式，可回测校准）。

    p_sideways 随 |score| 线性收缩；方向性质量按 tanh 倾斜分给 up/down，
    score=0 时严格对称（up=down=0.30, sideways=0.40）。
    """
    s = max(-1.0, min(1.0, float(score)))
    strength = abs(s)
    p_side = SIDE_BASE - SIDE_SLOPE * strength
    tilt = 0.5 + 0.5 * math.tanh(PROB_TILT_K * s)
    p_dir = 1.0 - p_side
    p_up, p_down = p_dir * tilt, p_dir * (1.0 - tilt)
    total = p_up + p_down + p_side
    return {
        "up": round(p_up / total, 4),
        "down": round(p_down / total, 4),
        "sideways": round(p_side / total, 4),
    }


def _renormalize(prob: dict[str, float]) -> dict[str, float]:
    vals = {k: max(0.0, float(prob.get(k, 0.0))) for k in ("up", "down", "sideways")}
    total = sum(vals.values()) or 1.0
    out = {k: round(v / total, 4) for k, v in vals.items()}
    # 圆整误差归到最大项，保证和恰为 1
    gap = round(1.0 - sum(out.values()), 4)
    if gap:
        kmax = max(out, key=out.get)
        out[kmax] = round(out[kmax] + gap, 4)
    return out


def _direction_of(prob: dict[str, float]) -> str:
    return max(("up", "down", "sideways"), key=lambda k: prob.get(k, 0.0))


# ═══════════════════════════ 规则轨 ═══════════════════════════


def rule_predict(df: pd.DataFrame, symbol: str, timeframe: str, horizon: int,
                 analysis: dict | None = None) -> dict:
    """纯函数规则轨：df（已收盘 bar，升序）→ 完整契约 dict。

    analysis 可传入已算好的 jts.analyze 结果（回测复用，避免重复计算）。
    df 根数不足返回 {"ok": False, "error": ...}。
    """
    sym, tf, h = _norm_symbol(symbol), _norm_tf(timeframe), _norm_horizon(horizon)
    if df is None or len(df) < MIN_BARS:
        return {"ok": False, "error": f"K线数据不足（{0 if df is None else len(df)} 根 < {MIN_BARS}）",
                "symbol": sym, "timeframe": tf, "horizon": h}

    analysis = analysis or jts.analyze(df)
    cons = analysis["consensus"]
    score = float(cons.get("score", 0.0) or 0.0)
    confidence = float(cons.get("confidence", 0.0) or 0.0)

    prob = probability_from_score(score)
    direction = _direction_of(prob)

    closes = df["close"].values.astype(float)
    close = float(closes[-1])
    atr = float(jts._atr(df).iloc[-1])
    if not (math.isfinite(atr) and atr > 0):
        atr = max(1e-9, close * 0.005)

    # 漂移：近端动量与共识倾斜五五开，封顶 ±0.6xATR；预测横盘时斜率衰减
    look = min(48, len(closes) - 1)
    mom_per_bar = (closes[-1] - closes[-1 - look]) / look if look > 0 else 0.0
    drift = 0.45 * mom_per_bar + 0.55 * score * 0.25 * atr
    cap = DRIFT_CAP_ATR * atr
    drift = max(-cap, min(cap, drift))
    if direction == "sideways":
        drift *= 0.4

    # 目标区间：中心=线性外推终点，半宽=ZONE_Z×ATR×√h（随机游走扩散近似）
    sigma_h = atr * math.sqrt(h)
    center = close + drift * h
    zone_high = center + ZONE_Z * sigma_h
    zone_low = max(center - ZONE_Z * sigma_h, close * 0.02)

    # 期望路径：从最后收盘 bar 的收盘时刻起，逐根线性外推
    step = TF_SECONDS[tf]
    try:
        last_close_ts = int(df["time"].iloc[-1]) / 1000.0 + step
    except (KeyError, TypeError, ValueError):
        last_close_ts = time.time()
    path = [{"t": _iso(last_close_ts + i * step),
             "price": jts._round_price(close + drift * i)} for i in range(1, h + 1)]

    # 佐证信号：与主方向一致的系统 slug（横盘时取中性且有强度的）
    want = {"up": "bullish", "down": "bearish", "sideways": "neutral"}[direction]
    min_strength = 0.35 if direction != "sideways" else 0.3
    signals = [s["system"] for s in analysis["signals"]
               if s.get("direction") == want and float(s.get("strength", 0)) >= min_strength]

    strong = sorted((s for s in analysis["signals"] if s.get("direction") == want),
                    key=lambda s: -float(s.get("strength", 0)))[:3]
    strong_txt = "、".join(f"{s['name_cn']}({float(s['strength']):.2f})" for s in strong)
    rationale = (
        f"12 套技术共识{_DIR_CN[direction]}（总分 {score:+.2f}，置信 {confidence:.0%}，"
        f"概率 涨{prob['up']:.0%}/跌{prob['down']:.0%}/横盘{prob['sideways']:.0%}）。"
        + (f"同向强信号：{strong_txt}。" if strong_txt else "")
        + f"按 ATR 通道外推 {h} 根（{tf}），目标区间 "
        f"{jts._round_price(zone_low)} ~ {jts._round_price(zone_high)}。"
    )

    return {
        "ok": True,
        "symbol": sym,
        "timeframe": tf,
        "generatedAt": _iso(time.time()),
        "horizon": h,
        "direction": direction,
        "probability": _renormalize(prob),
        "targetZone": {"high": jts._round_price(zone_high), "low": jts._round_price(zone_low)},
        "path": path,
        "confidence": round(confidence, 3),
        "rationale": rationale,
        "signals": signals,
        "engine": "rule",
        "mock": False,
        "lastClose": jts._round_price(close),
        "disclaimer": DISCLAIMER,
        "basis": ("概率=12套共识分tanh映射；区间=动量/共识漂移±"
                  f"{ZONE_Z}×ATR×√h；路径=线性外推。仅统计参考。"),
    }


# ═══════════════════════════ AI 轨 ═══════════════════════════


def _market_digest(df: pd.DataFrame, analysis: dict, base: dict) -> dict:
    """喂给 LLM 的行情+信号摘要（只给已有事实，不让模型自由发挥数据）。"""
    closes = df["close"].values.astype(float)
    close = float(closes[-1])
    atr = float(jts._atr(df).iloc[-1])
    cons = analysis["consensus"]

    def _chg(n: int) -> float | None:
        return round(closes[-1] / closes[-1 - n] - 1, 4) if len(closes) > n else None

    sigs = sorted(analysis["signals"], key=lambda s: -float(s.get("strength", 0)))
    return {
        "symbol": base["symbol"], "timeframe": base["timeframe"],
        "close": close, "atr_pct": round(atr / close, 5) if close else None,
        "change": {"8bar": _chg(8), "24bar": _chg(24), "96bar": _chg(96)},
        "range_96bar": {"high": float(df["high"].iloc[-96:].max()),
                        "low": float(df["low"].iloc[-96:].min())} if len(df) >= 96 else None,
        "consensus": {"direction": cons.get("direction"), "score": cons.get("score"),
                      "confidence": cons.get("confidence"),
                      "reasoning": str(cons.get("reasoning", ""))[:200]},
        "top_signals": [{"system": s["system"], "name": s["name_cn"],
                         "direction": s["direction"], "strength": s["strength"],
                         "why": str(s.get("reasoning", ""))[:60]} for s in sigs[:5]],
        "key_levels": cons.get("key_levels", [])[:6],
        "rule_probability": base["probability"],
        "horizon_bars": base["horizon"],
    }


def _clamp_simplex(raw: dict[str, float], base: dict[str, float],
                   tol: float = 0.15) -> dict[str, float]:
    """把 raw 投影到「每项 ∈ base±tol ∩ [0,1] 且三项和=1」的可行域（水填法）。

    base 本身和为 1 且在盒内，可行域非空，迭代必收敛；
    朴素的「先夹紧再归一化」会在归一化时把值再次推出 ±tol 盒，故不可用。
    """
    keys = ("up", "down", "sideways")
    lo = {k: max(0.0, base[k] - tol) for k in keys}
    hi = {k: min(1.0, base[k] + tol) for k in keys}
    v = {k: max(lo[k], min(hi[k], float(raw[k]))) for k in keys}
    for _ in range(8):
        gap = 1.0 - sum(v.values())
        if abs(gap) < 1e-9:
            break
        room = {k: (hi[k] - v[k]) if gap > 0 else (v[k] - lo[k]) for k in keys}
        movable = [k for k in keys if room[k] > 1e-12]
        if not movable:
            break
        share = gap / len(movable)
        for k in movable:
            v[k] = max(lo[k], min(hi[k], v[k] + share))
    return {k: round(v[k], 4) for k in keys}


def llm_refine(base: dict, digest: dict) -> dict | None:
    """AI 轨：LLM 产出中文 rationale + 概率修正。失败/未配置返回 None（降级）。

    防幻觉护栏：概率投影到「规则轨 ±0.15 盒 ∩ 单纯形」；解析失败即弃用。
    """
    try:
        import jarvis_llm_config as jlc
    except ImportError:
        return None
    system = (
        "你是加密货币量化研判助手。基于用户给出的量化信号摘要（唯一事实来源，"
        "禁止编造其中不存在的数据），输出严格 JSON："
        '{"probability": {"up": 0~1, "down": 0~1, "sideways": 0~1}, "rationale": "中文研判"}。'
        "probability 三项和为 1，且每项与 rule_probability 的偏差不超过 0.15；"
        "rationale 为 80~160 字中文，须引用摘要中的具体信号/价位，"
        "结尾提示概率仅供参考。只输出 JSON，不要输出其它内容。"
    )
    text = jlc.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": json.dumps(digest, ensure_ascii=False)}],
        temperature=0.2, max_tokens=600, json_mode=True, timeout=25,
        module="trend_predict",
    )
    if not text:
        return None
    try:
        obj = json.loads(text)
        raw = obj.get("probability") or {}
        rationale = str(obj.get("rationale", "")).strip()
        if not rationale:
            return None
        rule_p = base["probability"]
        for k in ("up", "down", "sideways"):
            if not (0.0 <= float(raw[k]) <= 1.0):
                return None
        prob = _clamp_simplex(raw, rule_p)
        # 投影后和恰为 1（残差归到最大项，消除圆整误差）
        gap = round(1.0 - sum(prob.values()), 4)
        if gap:
            kmax = max(prob, key=prob.get)
            prob[kmax] = round(prob[kmax] + gap, 4)
        return {"probability": prob, "rationale": rationale[:500]}
    except (ValueError, KeyError, TypeError):
        return None


# ═══════════════════════════ 主入口 ═══════════════════════════


def predict(symbol: str = "BTCUSDT", timeframe: str = "15m",
            horizon: int = HORIZON_DEFAULT, use_llm: bool = True) -> dict:
    """联网拉已收盘 K 线 → 规则轨 →（可选）AI 轨修正。取数失败返回 ok:False。"""
    sym, tf, h = _norm_symbol(symbol), _norm_tf(timeframe), _norm_horizon(horizon)
    df = jts.fetch_klines_df(sym, tf, FETCH_BARS, drop_unclosed=True)
    if df is None or len(df) < MIN_BARS:
        return {"ok": False,
                "error": "K线拉取失败或数据不足（可用 ?mock=1 获取联调假数据）",
                "symbol": sym, "timeframe": tf, "horizon": h,
                "disclaimer": DISCLAIMER}
    analysis = jts.analyze(df)
    base = rule_predict(df, sym, tf, h, analysis=analysis)
    if not base.get("ok") or not use_llm:
        return base
    refined = llm_refine(base, _market_digest(df, analysis, base))
    if refined:
        base["probability"] = refined["probability"]
        base["direction"] = _direction_of(refined["probability"])
        base["rationale"] = refined["rationale"]
        base["engine"] = "rule+llm"
    return base


# ═══════════════════════════ mock（前端联调） ═══════════════════════════


def mock_predict(symbol: str = "BTCUSDT", timeframe: str = "15m",
                 horizon: int = HORIZON_DEFAULT) -> dict:
    """确定性假数据（同参数同输出），契约与真实预测完全一致，mock=True。"""
    sym, tf, h = _norm_symbol(symbol), _norm_tf(timeframe), _norm_horizon(horizon)
    seed = int(hashlib.md5(f"{sym}:{tf}".encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    anchor = {"BTCUSDT": 64000.0, "ETHUSDT": 3400.0, "SOLUSDT": 150.0}.get(
        sym, 10 + (seed % 9000) / 10.0)
    score = float(rng.uniform(-0.6, 0.6))
    prob = probability_from_score(score)
    direction = _direction_of(prob)
    atr = anchor * 0.004 * (1 + (seed % 5) / 10)
    drift = score * 0.25 * atr * (0.4 if direction == "sideways" else 1.0)
    sigma_h = atr * math.sqrt(h)
    step = TF_SECONDS[tf]
    t0 = math.floor(time.time() / step) * step
    path = [{"t": _iso(t0 + i * step),
             "price": jts._round_price(anchor + drift * i
                                       + float(rng.normal(0, atr * 0.15)))}
            for i in range(1, h + 1)]
    center = anchor + drift * h
    pool = {"up": ["dow", "turtle", "triple_rsi"], "down": ["dow", "rule123", "chanlun"],
            "sideways": ["oscillator", "volatility"]}[direction]
    return {
        "ok": True, "symbol": sym, "timeframe": tf, "generatedAt": _iso(time.time()),
        "horizon": h, "direction": direction, "probability": prob,
        "targetZone": {"high": jts._round_price(center + ZONE_Z * sigma_h),
                       "low": jts._round_price(max(center - ZONE_Z * sigma_h, anchor * 0.02))},
        "path": path, "confidence": round(0.35 + abs(score) * 0.5, 3),
        "rationale": (f"[MOCK] 假数据联调：12 套共识{_DIR_CN[direction]}"
                      f"（模拟总分 {score:+.2f}），ATR 通道外推 {h} 根目标区间。"
                      "仅供前端渲染开发，非真实预测。"),
        "signals": pool, "engine": "mock", "mock": True,
        "lastClose": jts._round_price(anchor), "disclaimer": DISCLAIMER,
        "basis": "mock 确定性假数据（seed=symbol+timeframe），契约与真实预测一致。",
    }


# ═══════════════════════════ 准确率回测 ═══════════════════════════


def _actual_class(ret: float, atr_pct: float, horizon: int) -> str:
    """实际走势三分类：|ret| ≤ SIDEWAYS_RET_ATR×ATR%×√h 判横盘（口径与预测匹配）。"""
    thr = SIDEWAYS_RET_ATR * atr_pct * math.sqrt(horizon)
    if ret > thr:
        return "up"
    if ret < -thr:
        return "down"
    return "sideways"


def backtest(symbol: str = "BTCUSDT", timeframe: str = "15m",
             horizon: int = HORIZON_DEFAULT, windows: int = 40,
             df: pd.DataFrame | None = None) -> dict:
    """滚动窗口回测：逐窗口「只看过去→预测→对照实际」，给可信度基准。

    只回测规则轨（LLM 轨逐窗口调用成本高且不可复现）。
    df 可注入（离线测试）；缺省拉 500 根已收盘 K 线。
    返回方向命中率（三分类 / 不含横盘两种口径）+ 区间覆盖率 + 分方向明细。
    """
    sym, tf, h = _norm_symbol(symbol), _norm_tf(timeframe), _norm_horizon(horizon)
    windows = max(5, min(120, int(windows)))
    if df is None:
        df = jts.fetch_klines_df(sym, tf, 500, drop_unclosed=True)
    if df is None or len(df) < MIN_BARS + h + 20:
        return {"ok": False, "error": "历史数据不足，无法回测",
                "symbol": sym, "timeframe": tf, "horizon": h}

    n = len(df)
    warmup = max(MIN_BARS, min(250, n - h - windows - 1))
    last_i = n - 1 - h
    if last_i <= warmup:
        return {"ok": False, "error": f"可评估窗口不足（bars={n}, horizon={h}）",
                "symbol": sym, "timeframe": tf, "horizon": h}
    step = max(1, (last_i - warmup) // windows)
    idxs = list(range(warmup, last_i + 1, step))[-windows:]

    total = hit3 = 0
    dir_total = dir_hit = 0          # 不含横盘口径：预测有方向 & 实际有方向
    zone_in = 0
    by_pred: dict[str, dict[str, int]] = {
        k: {"n": 0, "hit": 0} for k in ("up", "down", "sideways")}
    t0 = time.time()

    for i in idxs:
        win = df.iloc[max(0, i - 299): i + 1].reset_index(drop=True)
        pred = rule_predict(win, sym, tf, h)
        if not pred.get("ok"):
            continue
        entry = float(df["close"].iloc[i])
        actual_close = float(df["close"].iloc[i + h])
        ret = actual_close / entry - 1 if entry else 0.0
        atr_pct = float(jts._atr(win).iloc[-1]) / entry if entry else 0.0
        actual = _actual_class(ret, atr_pct, h)

        total += 1
        p_dir = pred["direction"]
        by_pred[p_dir]["n"] += 1
        if p_dir == actual:
            hit3 += 1
            by_pred[p_dir]["hit"] += 1
        if p_dir in ("up", "down") and actual in ("up", "down"):
            dir_total += 1
            if p_dir == actual:
                dir_hit += 1
        zone = pred["targetZone"]
        if zone["low"] <= actual_close <= zone["high"]:
            zone_in += 1

    if total == 0:
        return {"ok": False, "error": "无有效回测窗口", "symbol": sym,
                "timeframe": tf, "horizon": h}
    return {
        "ok": True, "symbol": sym, "timeframe": tf, "horizon": h,
        "windows": total, "bars": n,
        "direction_hit_rate": round(hit3 / total, 4),
        "direction_hit_rate_excl_sideways": round(dir_hit / dir_total, 4) if dir_total else None,
        "directional_samples": dir_total,
        "zone_coverage": round(zone_in / total, 4),
        "by_prediction": {k: {"n": v["n"],
                              "hit_rate": round(v["hit"] / v["n"], 4) if v["n"] else None}
                          for k, v in by_pred.items()},
        "baseline_random3": 0.3333,
        "elapsed_sec": round(time.time() - t0, 2),
        "basis": (f"逐窗口只用过去数据预测未来 {h} 根；实际三分类死区="
                  f"{SIDEWAYS_RET_ATR}×ATR%×√h；区间覆盖=期末收盘落入 targetZone。"
                  "仅回测规则轨。"),
        "generatedAt": _iso(time.time()),
        "disclaimer": DISCLAIMER,
    }


# ═══════════════════════════ CLI ═══════════════════════════


def main() -> None:
    ap = argparse.ArgumentParser(description="走势预测引擎（规则+AI 双轨）")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframe", default="15m", choices=list(ALLOWED_TFS))
    ap.add_argument("--horizon", type=int, default=HORIZON_DEFAULT)
    ap.add_argument("--no-llm", action="store_true", help="仅规则轨")
    ap.add_argument("--mock", action="store_true", help="输出联调假数据")
    ap.add_argument("--backtest", action="store_true", help="跑准确率回测")
    ap.add_argument("--windows", type=int, default=40, help="回测窗口数")
    ap.add_argument("--json", action="store_true", help="紧凑 JSON 输出")
    args = ap.parse_args()

    if args.mock:
        out = mock_predict(args.symbol, args.timeframe, args.horizon)
    elif args.backtest:
        out = backtest(args.symbol, args.timeframe, args.horizon, args.windows)
    else:
        out = predict(args.symbol, args.timeframe, args.horizon, use_llm=not args.no_llm)
    indent = None if args.json else 2
    print(json.dumps(out, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()
