#!/usr/bin/env python3
"""贾维斯 JARVIS — 止损扫单检测（stop hunt / liquidity sweep）。

高胜率反转四条件中的「条件 4：打掉止损末端」——末端同方向止损被扫后再入场，
与主力同行。检测几何（看涨反转 · 扫多头止损）：

  1. 刺破前低：bar 最低价跌破参考窗口的前低（关键支撑下方挂着多头止损）
  2. 刺破幅度受控：低点距前低 ≤ ATR × 倍数（真反转的扫单是「探针」，不是趋势崩塌）
  3. 快速收回：当根收盘收回被破位之上（流动性拿完就走）
  4. 长下影线：下影 / 实体 ≥ 阈值（K 线形态上的针）
  5. 量能尖峰：成交量 ≥ 窗口均量 × 倍数（止损单被集中吃掉）

看跌反转（扫空头止损）完全镜像：刺破前高 + 长上影 + 收回前高下方。
核心判定为纯函数（吃 OHLCV 列表），联网取数只在 detect() 入口做，
便于离线冒烟与上层聚合接口（/api/reversal-score）复用。

用法（CLI）：
  python jarvis_stop_hunt.py BTCUSDT --tf 15m
"""

from __future__ import annotations

import argparse
import json
import sys
import time

# ─────────────────────────── 参数（经验缺省，聚合层可覆盖） ───────────────────────────

# 前低/前高参考窗口（根）：在扫单 bar 之前的这段里找关键支撑/压力
LOOKBACK_BARS = 20
# 只认「末端」扫单：最近 N 根内发生的才算（更早的已不是入场时机）
RECENT_BARS = 5
# 刺破幅度上限：低点越过前低不超过 ATR × 该倍数
ATR_MULT = 1.0
# ATR 周期（简单 TR 均值）
ATR_PERIOD = 14
# 影线 / 实体 最小比（实体极小时按 K 线全幅 35% 兜底，防除零放大）
WICK_BODY_RATIO = 2.0
# 量能尖峰：成交量 ≥ 窗口均量 × 该倍数
VOL_SPIKE_MULT = 1.5


def _atr(bars: list[dict], period: int = ATR_PERIOD) -> float | None:
    """简单 TR 均值（取末尾 period 根）；数据不足返回 None。"""
    if len(bars) < 2:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l = float(bars[i]["h"]), float(bars[i]["l"])
        pc = float(bars[i - 1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    tail = trs[-period:] if len(trs) > period else trs
    return sum(tail) / len(tail) if tail else None


def _fmt(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4g}"
    return f"{v:.6g}"


def detect_stop_hunt(
    bars: list[dict],
    *,
    lookback: int = LOOKBACK_BARS,
    recent: int = RECENT_BARS,
    atr_mult: float = ATR_MULT,
    wick_body_ratio: float = WICK_BODY_RATIO,
    vol_spike_mult: float = VOL_SPIKE_MULT,
) -> dict:
    """对 OHLCV 序列做止损扫单检测（纯函数）。

    bars: [{t?, ts?, o, h, l, c, v}, ...] 时间升序。
    返回 {detected, side, sweptLevel, wickRatio, volumeSpike, barT, note}；
    多空两侧都命中时取更近的一根（同根取幅度大者）。
    """
    none_result = {
        "detected": False, "side": "none", "sweptLevel": None,
        "wickRatio": None, "volumeSpike": None, "barT": None,
        "note": "未见扫单：近端无「刺破关键位快速收回」的针形 K 线",
    }
    n = len(bars)
    if n < lookback + 2:
        return {**none_result, "note": f"K 线不足（{n} 根 < {lookback + 2}），无法判定扫单"}

    hits: list[dict] = []
    start = max(lookback, n - recent)
    for i in range(start, n):
        bar = bars[i]
        o, h, l, c = (float(bar["o"]), float(bar["h"]), float(bar["l"]), float(bar["c"]))
        v = float(bar.get("v") or 0)
        window = bars[i - lookback : i]
        atr = _atr(bars[max(0, i - lookback) : i + 1])
        if atr is None or atr <= 0:
            continue
        vols = [float(b.get("v") or 0) for b in window]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        vol_spike = round(v / avg_vol, 2) if avg_vol > 0 else None
        vol_ok = vol_spike is not None and vol_spike >= vol_spike_mult

        body = abs(c - o)
        rng = h - l
        # 实体极小（十字星）时影线/实体比爆大：兜底改用「影线 ≥ 全幅 35%」口径
        body_floor = max(body, rng * 0.35 if rng > 0 else 0)

        bar_t = bar.get("t") or (
            time.strftime("%m-%d %H:%M", time.localtime(int(bar["ts"]) / 1000))
            if bar.get("ts") else None
        )

        # ── 看涨：刺破前低扫多头止损 ──
        prior_low = min(float(b["l"]) for b in window)
        lower_wick = min(o, c) - l
        if (
            l < prior_low
            and 0 < (prior_low - l) <= atr * atr_mult
            and c > prior_low
            and body_floor > 0
            and lower_wick / body_floor >= wick_body_ratio
            and vol_ok
        ):
            hits.append({
                "detected": True, "side": "long-stops-swept",
                "sweptLevel": round(prior_low, 8),
                "wickRatio": round(lower_wick / body_floor, 2),
                "volumeSpike": vol_spike, "barT": bar_t, "_i": i,
                "_depth": prior_low - l,
                "note": (
                    f"长下影刺破前低 {_fmt(prior_low)}（幅度 {_fmt(prior_low - l)}"
                    f" ≤ {atr_mult}×ATR）后收回，量能 {vol_spike}×均量——"
                    "多头止损被扫，可与主力同向做多"
                ),
            })

        # ── 看跌：刺破前高扫空头止损（镜像） ──
        prior_high = max(float(b["h"]) for b in window)
        upper_wick = h - max(o, c)
        if (
            h > prior_high
            and 0 < (h - prior_high) <= atr * atr_mult
            and c < prior_high
            and body_floor > 0
            and upper_wick / body_floor >= wick_body_ratio
            and vol_ok
        ):
            hits.append({
                "detected": True, "side": "short-stops-swept",
                "sweptLevel": round(prior_high, 8),
                "wickRatio": round(upper_wick / body_floor, 2),
                "volumeSpike": vol_spike, "barT": bar_t, "_i": i,
                "_depth": h - prior_high,
                "note": (
                    f"长上影刺破前高 {_fmt(prior_high)}（幅度 {_fmt(h - prior_high)}"
                    f" ≤ {atr_mult}×ATR）后回落，量能 {vol_spike}×均量——"
                    "空头止损被扫，可与主力同向做空"
                ),
            })

    if not hits:
        return none_result
    # 更近优先；同一根 K 线上双向命中取刺破幅度大的一侧
    hits.sort(key=lambda x: (x["_i"], x["_depth"]))
    best = hits[-1]
    best.pop("_i", None)
    best.pop("_depth", None)
    return best


# ─────────────────────────── 联网取数入口 ───────────────────────────

def fetch_bars(symbol: str, timeframe: str = "15m", limit: int = 120) -> list[dict]:
    """经 jarvis_crypto_data 拉 Binance 现货 K 线，返回升序 OHLCV 列表。"""
    import jarvis_crypto_data as jcd

    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    allowed = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    iv = timeframe if timeframe in allowed else "15m"
    raw = jcd._get(jcd.SPOT_API + "/api/v3/klines",
                   {"symbol": spot, "interval": iv, "limit": max(40, min(int(limit), 500))})
    if isinstance(raw, dict):  # {_error: ...}
        raise RuntimeError(str(raw.get("_error", "kline fetch failed")))
    return [
        {"ts": int(k[0]), "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
        for k in raw
    ]


def detect(symbol: str, timeframe: str = "15m", limit: int = 120) -> dict:
    """取数 + 检测一步到位（/api/reversal-score 聚合用）。"""
    return detect_stop_hunt(fetch_bars(symbol, timeframe, limit))


def mock_result(direction: str = "bullish") -> dict:
    """演示数据（?mock=1 全链 mock 用）。"""
    if direction == "bearish":
        return {
            "detected": True, "side": "short-stops-swept", "sweptLevel": 64740.2,
            "wickRatio": 3.1, "volumeSpike": 2.2, "barT": "07-12 14:45",
            "note": "长上影刺破前高 64,740.20 后回落，量能 2.2×均量——空头止损被扫（mock）",
        }
    return {
        "detected": True, "side": "long-stops-swept", "sweptLevel": 61544.56,
        "wickRatio": 2.8, "volumeSpike": 1.9, "barT": "07-12 14:45",
        "note": "长下影刺破前低 61,544.56 后收回，量能 1.9×均量——多头止损被扫（mock）",
    }


# ─────────────────────────── 四条件叠加评分（纯函数） ───────────────────────────

DISCLAIMER = ("四条件叠加为概率框架而非确定性信号：条件越多胜率越高、机会越少，"
              "4/4 才是高概率入场点；仅统计参考，非投资建议。")

# 条件中文名（note 汇总用）
_COND_CN = {
    "delta_divergence": "Delta 背离",
    "multi_distribution": "过程多分布",
    "triple_confirm": "三连分布确认",
    "stop_hunt": "末端止损扫单",
}


def _unavailable(name_cn: str) -> dict:
    return {"met": False, "unavailable": True,
            "note": f"{name_cn}数据源未就绪，本条件暂不计入"}


def aggregate_reversal_score(delta: dict | None, vp: dict | None,
                             hunt: dict | None) -> dict:
    """三个上游响应 → 四条件叠加评分契约主体（纯函数，可离线冒烟）。

    delta: /api/delta 响应（None / ok:False = 未就绪）
    vp:    /api/volume-profile 响应（同上）
    hunt:  detect() / mock_result() 输出（None = 检测失败）
    """
    delta_ok = bool(delta) and delta.get("ok") is not False
    vp_ok = bool(vp) and vp.get("ok") is not False
    hunt_ok = bool(hunt)

    div = (delta or {}).get("divergence") or {}
    bull_div = bool((div.get("bullish") or {}).get("active")) if delta_ok else False
    bear_div = bool((div.get("bearish") or {}).get("active")) if delta_ok else False
    hunt_side = (hunt or {}).get("side") if hunt_ok else "none"

    # ── 方向投票：方向性条件（背离 + 扫单）决定 direction ──
    # 平局破局优先级：absorption（delta 模块已按强度裁决的主力行为实质）
    # > 扫单侧（仅时机信号）。冲突方向的条件不计入叠加（见下）。
    absorption_side = ((delta or {}).get("absorption") or {}).get("side") if delta_ok else None
    bull_votes = int(bull_div) + int(hunt_side == "long-stops-swept")
    bear_votes = int(bear_div) + int(hunt_side == "short-stops-swept")
    if bull_votes > bear_votes:
        direction = "bullish"
    elif bear_votes > bull_votes:
        direction = "bearish"
    elif bull_votes == 0:
        direction = "none"
    elif absorption_side == "sell-absorption":
        direction = "bullish"
    elif absorption_side == "buy-distribution":
        direction = "bearish"
    elif hunt_side == "long-stops-swept":
        direction = "bullish"
    else:
        direction = "bearish"

    conditions: dict[str, dict] = {}

    # 条件 1 · Delta 背离（方向性）
    if not delta_ok:
        conditions["delta_divergence"] = _unavailable(_COND_CN["delta_divergence"])
    else:
        side_blk = div.get("bullish" if direction != "bearish" else "bearish") or {}
        met = bull_div if direction == "bullish" else bear_div if direction == "bearish" else False
        conditions["delta_divergence"] = {
            "met": met,
            "note": (side_blk.get("note") or "")
            if met else "未见同向 Delta 背离（价格新低/新高时 Delta 未确认吸收）",
        }

    # 条件 2 · 过程多分布 + 条件 3 · 三连分布确认（结构性，方向无关）
    if not vp_ok:
        conditions["multi_distribution"] = _unavailable(_COND_CN["multi_distribution"])
        conditions["triple_confirm"] = _unavailable(_COND_CN["triple_confirm"])
    else:
        st = (vp or {}).get("structure") or {}
        multi_met = st.get("type") == "multi-distribution" or int(st.get("normalCount") or 0) >= 2
        conditions["multi_distribution"] = {
            "met": bool(multi_met),
            "note": st.get("note") or ("过程形成多个正态分布，非单边直拉" if multi_met
                                       else "未形成多分布结构（单边直拉或分布混杂）"),
        }
        triple_met = bool(st.get("tripleConfirmed"))
        n_normal = int(st.get("normalCount") or 0)
        conditions["triple_confirm"] = {
            "met": triple_met,
            "note": ("已出现 ≥3 个相邻正态分布，三连确认成立" if triple_met
                     else f"仅 {n_normal} 个正态分布，等第 3 个（耐心等待回补）"),
        }

    # 条件 4 · 末端止损扫单（方向性）
    if not hunt_ok:
        conditions["stop_hunt"] = _unavailable(_COND_CN["stop_hunt"])
    else:
        want = ("long-stops-swept" if direction == "bullish"
                else "short-stops-swept" if direction == "bearish" else None)
        met = want is not None and hunt_side == want
        conditions["stop_hunt"] = {
            "met": met,
            "note": (hunt or {}).get("note") or "未见扫单",
        }
        if not met and hunt_side != "none" and direction != "none":
            conditions["stop_hunt"]["note"] = (
                f"检测到{'空头' if hunt_side == 'short-stops-swept' else '多头'}止损被扫，"
                f"但与当前研判方向不一致，不计入叠加"
            )

    satisfied = sum(1 for c in conditions.values() if c.get("met"))
    verdict = ("high-probability" if satisfied == 4
               else "watch" if satisfied >= 2 else "no-signal")

    missing = [_COND_CN[k] for k, c in conditions.items() if not c.get("met")]
    unavailable_cnt = sum(1 for c in conditions.values() if c.get("unavailable"))
    if satisfied == 4:
        note = "4/4 条件齐备——高概率入场点（条件越多胜率越高，注意仓位与止损纪律）"
    elif satisfied >= 2:
        note = (f"4/4 才是高概率入场点；当前 {satisfied}/4 观察，"
                f"缺：{'、'.join(missing)}")
    else:
        note = f"当前 {satisfied}/4，无有效反转信号；缺：{'、'.join(missing)}"
    if unavailable_cnt:
        note += f"（{unavailable_cnt} 个数据源未就绪）"

    return {
        "ok": True,
        "direction": direction,
        "conditions": conditions,
        "satisfied": satisfied,
        "maxScore": 4,
        "verdict": verdict,
        "note": note,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "disclaimer": DISCLAIMER,
    }


# ─────────────────────────── CLI ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="止损扫单检测")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--limit", type=int, default=120)
    args = ap.parse_args()
    out = detect(args.symbol, args.tf, args.limit)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
