#!/usr/bin/env python3
"""贾维斯 JARVIS — Delta/CVD 订单流引擎 + 吸收背离检测。

K 线指标（MACD/KDJ 背离）推断的是「价格的影子」；订单流看的是「谁在成交」：
  delta = 主动买量 − 主动卖量 = 2×taker_buy − volume（Binance K 线自带 k[9]）
  cvd   = delta 的窗口累计（Cumulative Volume Delta）

核心信号——吸收背离（absorption divergence）：
  看涨吸收：价格 swing 低点逐级创新低，而同期 CVD 低点抬升/持平
            → 下跌由被动买单（做市商/大玩家限价接货）吸收，空头衰竭，反转概率升高
  看跌派发：价格 swing 高点逐级创新高，而 CVD 高点不再抬升
            → 上涨被派发（买盘被动出货），多头衰竭

数据纪律：
  - 自建拉取（保留 taker_buy 字段），不改动公共 fetch 函数的其它消费方
  - 检测只用已收盘 bar（丢弃进行中的最后一根），防前瞻
  - 全部检测逻辑为纯函数（吃 bars list），离线可测

回测口径（诚实汇报，含样本数）：
  逐 bar 滚动重放检测，信号触发后 N 根期末收盘高于触发价 = 命中；
  基线 = 全体可评估 bar 的同口径概率（市场自身漂移）。

用法：
  python jarvis_delta_flow.py --symbol BTCUSDT --timeframe 15m
  python jarvis_delta_flow.py --mock --json
  python jarvis_delta_flow.py --backtest --symbol BTCUSDT --timeframe 15m
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time

import numpy as np

import jarvis_crypto_data as jcd

DISCLAIMER = ("Delta/CVD 基于币安主动成交口径（taker buy/sell），吸收背离为概率信号"
              "而非确定性反转；仅统计参考，非投资建议。")

ALLOWED_TFS = ("5m", "15m", "30m", "1h", "4h", "1d")
TF_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
LIMIT_DEFAULT = 200
LIMIT_MAX = 500
MIN_BARS = 40            # 少于此根数不检测（swing 结构不可靠）

SWING_WINDOW = 3         # swing 极值窗口（±3 根内为局部极值）
MAX_ANCHORS = 3          # 背离锚点最多取最近 3 个 swing 点
ACTIVE_WITHIN = 20       # 最后一个锚点距最新收盘 bar ≤20 根才算 active
CVD_FLAT_TOL = 0.06      # CVD「持平」容差：|ΔCVD| ≤ 6%×窗口CVD波幅
# 2 锚点背离要求 CVD 明显抬升（>tol）；「持平」证据太弱，仅 3 锚点链才接受。
# 随机游走中价格两连低+CVD 恰好为正的概率接近 50%，不收紧会大量误报。


# ═══════════════════════════ 取数 ═══════════════════════════


def _norm_symbol(symbol: str) -> str:
    sym = (symbol or "BTCUSDT").upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    return sym


def _norm_tf(timeframe: str) -> str:
    return timeframe if timeframe in ALLOWED_TFS else "15m"


def _norm_limit(limit, max_n: int = LIMIT_MAX) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = LIMIT_DEFAULT
    return max(MIN_BARS, min(max_n, n))


def fetch_bars(symbol: str, timeframe: str, limit: int = LIMIT_DEFAULT,
               max_n: int = LIMIT_MAX) -> list[dict] | None:
    """拉现货 K 线并保留 taker_buy 字段；只回已收盘 bar；失败返回 None。

    自建拉取的原因：项目公共 fetch（jarvis_twelve_systems.fetch_klines_df /
    dashboard /api/kline）都丢掉了 k[9] taker_buy_base_asset_volume，
    改它们会影响既有消费方——本模块独立取数，互不干扰。
    """
    sym, tf = _norm_symbol(symbol), _norm_tf(timeframe)
    lim = _norm_limit(limit, max_n=max_n)
    try:
        raw = jcd._get(jcd.SPOT_API + "/api/v3/klines",
                       {"symbol": sym, "interval": tf, "limit": min(lim + 1, 1000)})
    except Exception:  # noqa: BLE001 — 取数失败交调用方降级
        return None
    if not isinstance(raw, list) or not raw:
        return None
    # 丢进行中的最后一根（close_time k[6] 未到 = 未收盘），防前瞻
    try:
        if float(raw[-1][6]) >= time.time() * 1000:
            raw = raw[:-1]
    except (IndexError, TypeError, ValueError):
        pass
    if not raw:
        return None
    bars = []
    for k in raw[-lim:]:
        try:
            bars.append({
                "ts": int(k[0]),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),
                "taker_buy": float(k[9]),
            })
        except (IndexError, TypeError, ValueError):
            return None  # 字段缺损（极旧接口版本）：宁缺毋滥
    return bars


# ═══════════════════════════ Delta / CVD（纯函数） ═══════════════════════════


def compute_delta_cvd(bars: list[dict]) -> list[dict]:
    """逐根附加 delta 与窗口累计 cvd（原地不改输入，返回新 list）。"""
    out = []
    cvd = 0.0
    for b in bars:
        delta = 2.0 * b["taker_buy"] - b["volume"]
        cvd += delta
        out.append({**b, "delta": delta, "cvd": cvd})
    return out


def _swing_idx(vals: list[float], window: int, find_low: bool) -> list[int]:
    """局部极值下标（window 根内最小/最大；平台取首个）。"""
    n = len(vals)
    idx = []
    for i in range(window, n - window):
        seg = vals[i - window: i + window + 1]
        target = min(seg) if find_low else max(seg)
        if vals[i] == target:
            if idx and idx[-1] == i - 1 and vals[i] == vals[i - 1]:
                continue
            idx.append(i)
    return idx


def _iso(ts_ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ms / 1000))


def _empty_side() -> dict:
    return {"active": False, "strength": None, "note": "", "anchors": []}


def _grade(n_anchors: int, cvd_slope_norm: float, vol_ratio: float) -> str:
    """背离强度：锚点数量 + CVD 归一化斜率 + 锚点处量能配合。

    strong   ≥3 个锚点且 CVD 明显上移，或 2 锚点但 CVD 斜率大且放量
    moderate 2 锚点 CVD 明显上移，或 3 锚点但 CVD 仅持平
    weak     其余（CVD 持平的 2 锚点背离）
    """
    strong_slope = cvd_slope_norm > 0.25
    if n_anchors >= 3 and strong_slope:
        return "strong"
    if n_anchors >= 3 or (strong_slope and vol_ratio > 1.2):
        return "moderate" if n_anchors < 3 else "strong"
    if strong_slope:
        return "moderate"
    return "weak"


def _detect_one_side(rows: list[dict], bullish: bool) -> dict:
    """单边背离检测。bullish=True 检「价创新低 CVD 不创新低」，False 镜像。"""
    n = len(rows)
    if n < MIN_BARS:
        return _empty_side()
    price_key = "low" if bullish else "high"
    prices = [r[price_key] for r in rows]
    swings = _swing_idx(prices, SWING_WINDOW, find_low=bullish)
    if len(swings) < 2:
        return _empty_side()

    # 从最新 swing 点向前找「价格单调创新低（新高）」的连续序列
    chain = [swings[-1]]
    for i in reversed(swings[:-1]):
        prev_ok = (prices[i] > prices[chain[0]]) if bullish else (prices[i] < prices[chain[0]])
        if prev_ok:
            chain.insert(0, i)
            if len(chain) >= MAX_ANCHORS:
                break
        else:
            break
    if len(chain) < 2:
        return _empty_side()
    if n - 1 - chain[-1] > ACTIVE_WITHIN:
        return _empty_side()  # 背离太陈旧，不作为当前信号

    cvds = [rows[i]["cvd"] for i in chain]
    cvd_range = max(r["cvd"] for r in rows) - min(r["cvd"] for r in rows) or 1.0
    dcvd = cvds[-1] - cvds[0]
    # 接受条件按结构证据分级：3 锚点链容忍 CVD 持平；2 锚点链结构证据弱，
    # 必须 CVD 显著反向（≥2×容差），否则随机游走中「两连低+CVD 恰好为正」
    # 的巧合会大量误报。
    tol = CVD_FLAT_TOL * cvd_range
    if len(chain) >= 3:
        ok = dcvd >= -tol if bullish else dcvd <= tol
    else:
        ok = dcvd > 2 * tol if bullish else dcvd < -2 * tol
    # 锚点间 CVD 走向须一致（多锚点链中间不允许深回撤破坏背离结构）
    if ok and len(chain) >= 3:
        mono = all((cvds[k + 1] - cvds[k]) >= -tol for k in range(len(cvds) - 1)) \
            if bullish else \
            all((cvds[k + 1] - cvds[k]) <= tol for k in range(len(cvds) - 1))
        ok = mono
    if not ok:
        return _empty_side()

    slope_norm = abs(dcvd) / cvd_range
    avg_vol = (sum(r["volume"] for r in rows) / n) or 1.0
    anchor_vol = sum(rows[i]["volume"] for i in chain) / len(chain)
    strength = _grade(len(chain), slope_norm, anchor_vol / avg_vol)

    seq_cn = {2: "两", 3: "三"}.get(len(chain), str(len(chain)))
    if bullish:
        cvd_cn = "抬升" if dcvd > CVD_FLAT_TOL * cvd_range else "持平"
        note = (f"价格{seq_cn}创新低而 CVD {cvd_cn}，卖压正被吸收"
                f"（{ {'strong': '吸收信号强', 'moderate': '吸收信号中等', 'weak': '吸收迹象初现'}[strength] }）")
    else:
        cvd_cn = "走弱" if dcvd < -CVD_FLAT_TOL * cvd_range else "滞涨"
        note = (f"价格{seq_cn}创新高而 CVD {cvd_cn}，买盘正被派发"
                f"（{ {'strong': '派发信号强', 'moderate': '派发信号中等', 'weak': '派发迹象初现'}[strength] }）")
    anchors = [{"t": _iso(rows[i]["ts"]), "price": round(prices[i], 8),
                "cvd": round(rows[i]["cvd"], 4)} for i in chain]
    return {"active": True, "strength": strength, "note": note, "anchors": anchors}


def detect_divergence(rows: list[dict]) -> dict:
    """双边吸收背离检测（rows 须已含 cvd）。返回契约 divergence + absorption 块。"""
    bull = _detect_one_side(rows, bullish=True)
    bear = _detect_one_side(rows, bullish=False)
    if bull["active"] and bear["active"]:
        # 双边同时活跃（宽幅震荡）：保留强度高的一边作为吸收结论
        order = {"strong": 3, "moderate": 2, "weak": 1}
        keep_bull = order[bull["strength"]] >= order[bear["strength"]]
    else:
        keep_bull = bull["active"]
    if bull["active"] and (keep_bull or not bear["active"]):
        absorption = {"detected": True, "side": "sell-absorption",
                      "note": "检测到卖压吸收：" + bull["note"]}
    elif bear["active"]:
        absorption = {"detected": True, "side": "buy-distribution",
                      "note": "检测到买盘派发：" + bear["note"]}
    else:
        absorption = {"detected": False, "side": "none", "note": "未检测到吸收/派发背离"}
    return {"divergence": {"bullish": bull, "bearish": bear}, "absorption": absorption}


# ═══════════════════════════ 契约输出 ═══════════════════════════


def analyze(symbol: str = "BTCUSDT", timeframe: str = "15m",
            limit: int = LIMIT_DEFAULT) -> dict:
    """联网拉已收盘 K 线 → delta/cvd → 背离检测 → 完整契约。失败 ok:False。"""
    sym, tf = _norm_symbol(symbol), _norm_tf(timeframe)
    bars = fetch_bars(sym, tf, limit)
    if not bars or len(bars) < MIN_BARS:
        return {"ok": False, "error": "K线拉取失败或数据不足（可用 ?mock=1 联调）",
                "symbol": sym, "timeframe": tf, "disclaimer": DISCLAIMER}
    rows = compute_delta_cvd(bars)
    det = detect_divergence(rows)
    return {
        "ok": True,
        "symbol": sym,
        "timeframe": tf,
        "bars": [{"t": _iso(r["ts"]), "delta": round(r["delta"], 4),
                  "cvd": round(r["cvd"], 4), "volume": round(r["volume"], 4)}
                 for r in rows],
        "divergence": det["divergence"],
        "absorption": det["absorption"],
        "updatedAt": _iso(int(time.time() * 1000)),
        "mock": False,
        "disclaimer": DISCLAIMER,
    }


def mock_analyze(symbol: str = "BTCUSDT", timeframe: str = "15m",
                 limit: int = LIMIT_DEFAULT) -> dict:
    """确定性联调假数据（同参同输出，mock:true，不联网）。

    构造尾段「价格三创新低 + CVD 抬升」的看涨吸收形态，保证 MCP-1 联调时
    divergence.bullish.active 恒为 true、契约字段全覆盖。
    """
    sym, tf = _norm_symbol(symbol), _norm_tf(timeframe)
    lim = _norm_limit(limit)
    seed = int(hashlib.md5(f"{sym}:{tf}".encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    anchor_price = {"BTCUSDT": 64000.0, "ETHUSDT": 3400.0, "SOLUSDT": 150.0}.get(
        sym, 10 + (seed % 9000) / 10.0)
    step = TF_SECONDS[tf]
    t0 = (math.floor(time.time() / step) * step - lim * step) * 1000

    bars = []
    price = anchor_price
    tail = lim // 3                     # 尾段做背离形态
    for i in range(lim):
        drift = 0.0
        in_tail = i >= lim - tail
        if in_tail:
            # 三段下探：整体缓跌 + 周期性反弹，制造递降 swing 低点
            phase = (i - (lim - tail)) / tail
            drift = -0.0012 * anchor_price * (1 - 0.5 * math.sin(phase * math.pi * 3))
        noise = float(rng.normal(0, anchor_price * 0.0015))
        o = price
        price = max(anchor_price * 0.5, price + drift + noise)
        c = price
        hi = max(o, c) * (1 + abs(float(rng.normal(0, 0.0008))))
        lo = min(o, c) * (1 - abs(float(rng.normal(0, 0.0008))))
        vol = abs(float(rng.normal(100, 25))) + 1.0
        if in_tail:
            # 吸收：下跌 bar 的主动买占比反而逐步上移（被动接货推高 taker_buy 比例）
            phase = (i - (lim - tail)) / tail
            buy_ratio = 0.5 + 0.1 * phase + float(rng.normal(0, 0.02))
        else:
            buy_ratio = 0.5 + float(rng.normal(0, 0.04))
        buy_ratio = min(0.9, max(0.1, buy_ratio))
        bars.append({"ts": int(t0 + i * step * 1000),
                     "open": o, "high": hi, "low": lo, "close": c,
                     "volume": vol, "taker_buy": vol * buy_ratio})
    rows = compute_delta_cvd(bars)
    det = detect_divergence(rows)
    out = {
        "ok": True, "symbol": sym, "timeframe": tf,
        "bars": [{"t": _iso(r["ts"]), "delta": round(r["delta"], 4),
                  "cvd": round(r["cvd"], 4), "volume": round(r["volume"], 4)}
                 for r in rows],
        "divergence": det["divergence"],
        "absorption": det["absorption"],
        "updatedAt": _iso(int(time.time() * 1000)),
        "mock": True,
        "disclaimer": DISCLAIMER,
    }
    for side in ("bullish", "bearish"):
        blk = out["divergence"][side]
        if blk["active"]:
            blk["note"] = "[MOCK] " + blk["note"]
    if out["absorption"]["detected"]:
        out["absorption"]["note"] = "[MOCK] " + out["absorption"]["note"]
    return out


# ═══════════════════════════ 历史回测 ═══════════════════════════


def backtest(symbol: str = "BTCUSDT", timeframe: str = "15m",
             horizons: tuple[int, ...] = (16, 32), max_bars: int = 1000,
             bars: list[dict] | None = None) -> dict:
    """吸收背离信号的历史胜率（诚实口径，含样本数与随机基线）。

    逐 bar 滚动重放：截至 bar i 的窗口跑检测，看涨吸收背离「新出现」记一个
    信号（按最后锚点时间去重）；命中 = i+N 期末收盘 > i 收盘。
    基线 = 全体可评估 bar 的 close[i+N] > close[i] 概率（市场漂移）。
    bars 可注入（离线测试）；缺省拉 max_bars 根已收盘 K 线。
    """
    sym, tf = _norm_symbol(symbol), _norm_tf(timeframe)
    if bars is None:
        # 回测取数上限放宽到交易所单次极限 999（LIMIT_MAX 只约束实时接口）
        bars = fetch_bars(sym, tf, min(max(200, max_bars), 999), max_n=999)
    if not bars or len(bars) < MIN_BARS * 3:
        return {"ok": False, "error": "历史数据不足，无法回测",
                "symbol": sym, "timeframe": tf}
    rows = compute_delta_cvd(bars)
    n = len(rows)
    max_h = max(horizons)
    win = 300  # 与实时检测同窗宽

    signal_idx: list[int] = []
    seen_anchor_ts: set[str] = set()
    for i in range(MIN_BARS, n - max_h):
        seg = rows[max(0, i - win + 1): i + 1]
        det = _detect_one_side(seg, bullish=True)
        if not det["active"]:
            continue
        key = det["anchors"][-1]["t"]
        if key in seen_anchor_ts:
            continue  # 同一背离链持续 active，只记首次
        seen_anchor_ts.add(key)
        signal_idx.append(i)

    per_h = {}
    for h in horizons:
        hits = sum(1 for i in signal_idx if rows[i + h]["close"] > rows[i]["close"])
        total = len(signal_idx)
        base_total = n - MIN_BARS - h
        base_hits = sum(1 for i in range(MIN_BARS, n - h)
                        if rows[i + h]["close"] > rows[i]["close"])
        per_h[str(h)] = {
            "signals": total,
            "hit_rate": round(hits / total, 4) if total else None,
            "baseline": round(base_hits / base_total, 4) if base_total > 0 else None,
            "edge": (round(hits / total - base_hits / base_total, 4)
                     if total and base_total > 0 else None),
        }
    return {
        "ok": True, "symbol": sym, "timeframe": tf, "bars": n,
        "signal_count": len(signal_idx),
        "low_sample": len(signal_idx) < 10,
        "horizons": per_h,
        "basis": ("信号=看涨吸收背离首次出现；命中=N 根后期末收盘高于信号收盘；"
                  "基线=全体 bar 同口径上涨概率；edge=胜率−基线。"
                  "吸收背离为低频信号，low_sample=true 时胜率无统计意义，仅作参考。"),
        "generatedAt": _iso(int(time.time() * 1000)),
        "disclaimer": DISCLAIMER,
    }


# ═══════════════════════════ CLI ═══════════════════════════


def main() -> None:
    ap = argparse.ArgumentParser(description="Delta/CVD 订单流引擎")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframe", default="15m", choices=list(ALLOWED_TFS))
    ap.add_argument("--limit", type=int, default=LIMIT_DEFAULT)
    ap.add_argument("--mock", action="store_true", help="确定性联调假数据")
    ap.add_argument("--backtest", action="store_true", help="历史胜率回测")
    ap.add_argument("--max-bars", type=int, default=1000)
    ap.add_argument("--json", action="store_true", help="紧凑输出")
    args = ap.parse_args()

    if args.mock:
        out = mock_analyze(args.symbol, args.timeframe, args.limit)
    elif args.backtest:
        out = backtest(args.symbol, args.timeframe, max_bars=args.max_bars)
    else:
        out = analyze(args.symbol, args.timeframe, args.limit)
    if not args.json and isinstance(out.get("bars"), list) and len(out["bars"]) > 6:
        out = {**out, "bars": out["bars"][-6:] + [{"_truncated": f"共 {len(out['bars'])} 根，CLI 只展示尾 6 根"}]}
    print(json.dumps(out, ensure_ascii=False, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
