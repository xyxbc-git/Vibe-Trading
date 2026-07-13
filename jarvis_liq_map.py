#!/usr/bin/env python3
"""贾维斯 JARVIS — 清算/止损密集区估计器（M2 s5-liq-map）。

预判庄家扫单/插针目标位（磁吸位）：把 jarvis_stop_hunt 的「事后确认」升级为
「事前预判」。价格倾向被吸向流动性密集处——清算簇（强平单密集触发区）与
散户止损聚集区（前低/前高、整数关口）就是插针的「磁铁」。

估算模型（三层）：
  1) 清算簇：VP 入场分布（jarvis_volume_profile 的 POC/volumeShare 代表近期
     真实建仓密集价位）× 常见杠杆档（5/10/25/50/100x，占比权重可配）推算
     多/空双向爆仓价：多头爆仓 ≈ 入场 ×(1 − 0.99/L)、空头 ≈ ×(1 + 0.99/L)，
     强度 = 分布量占比 × 档位权重，OI 名义规模作总量刻度；按价格分箱聚合，
     现价上/下方各取前 3 个密集区。
  2) 止损密集区：摆动高/低点外侧（散户止损贴结构位）+ 整数关口
     （复用 jarvis_position_calc._round_step 的 {1,2,5}×10^n 心理刻度），
     二者重合时强度叠加。
  3) forceOrder 实时校准：真实强平单落在预估簇内 → 置信度升；WS 降级
     （health.degraded_streams 含 forceOrder）或无数据时优雅跳过，
     估算主体不依赖它（confidence 保持基线并标注 uncalibrated）。

输出（GET /api/liq-map/{symbol}）：磁吸位列表，每个含
  price_mid/price_low/price_high、kind（long_liq/short_liq/stop_cluster）、
  side（above/below 现价）、strength（0~1 归一）、confidence、dist_pct、label。

信号联动：现价距最强磁吸位 < signal.liq_magnet_warn_pct（默认 1.5%）时，
magnet_factor() 输出「接近磁吸位，插针风险」提醒因子，dashboard 在共识
seatbelt 段附加（开关 signal.liq_map_seatbelt_enabled）。

设计原则（与其它 jarvis 引擎同风格）：
  - 纯函数核心 estimate(...)：只吃现成数据结构，不联网，可离线单测
  - assess() 门面拉真实数据；mock_assess() 出确定性假数据
  - 永不抛出：任何上游缺失都降级输出（ok=False 或空 magnets），不断链

用法（CLI）：
  python jarvis_liq_map.py BTCUSDT --tf 15m
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time

from jarvis_position_calc import _round_step

# 分箱宽度 = 现价 × 该比例（磁吸区颗粒度）
BIN_PCT = 0.005
# 摆动点检测：左右各 N 根确认（fractal 口径）
SWING_WING = 3
# 摆动点/关口止损簇的基础强度（相对清算簇 0~1 刻度）
STOP_BASE_STRENGTH = 0.45
ROUND_LEVEL_STRENGTH = 0.35
# 关口与摆动点重合的判定半径（占现价比例）
OVERLAP_PCT = 0.004
# forceOrder 校准窗口
FO_LOOKBACK = 200


def parse_leverage_weights(raw: str | None) -> list[tuple[float, float]]:
    """'5:0.1,10:0.3' → [(5.0, 0.1), (10.0, 0.3)]；非法项跳过，全空回默认。"""
    out: list[tuple[float, float]] = []
    for part in str(raw or "").split(","):
        if ":" not in part:
            continue
        try:
            lev, w = part.split(":", 1)
            lv, wv = float(lev.strip()), float(w.strip())
            if lv >= 1 and wv > 0:
                out.append((lv, wv))
        except (TypeError, ValueError):
            continue
    if not out:
        out = [(5.0, 0.1), (10.0, 0.3), (25.0, 0.3), (50.0, 0.2), (100.0, 0.1)]
    total = sum(w for _, w in out)
    return [(lv, w / total) for lv, w in out]


def find_swings(rows: list[dict], wing: int = SWING_WING,
                max_points: int = 6) -> dict:
    """滚动窗口 fractal 摆动点：左右各 wing 根都更高/更低才算。

    返回 {"highs": [price...], "lows": [price...]}（各取最近 max_points 个）。
    rows 需含 high/low 键（jarvis 各引擎 K 线统一结构）。
    """
    highs: list[float] = []
    lows: list[float] = []
    n = len(rows or [])
    for i in range(wing, n - wing):
        try:
            h = float(rows[i]["high"])
            lo = float(rows[i]["low"])
        except (KeyError, TypeError, ValueError):
            continue
        # 严格突出才算摆动点：等值平台（横盘密集 bar）不构成结构高低点
        if all(h > float(rows[j]["high"]) for j in range(i - wing, i + wing + 1) if j != i):
            highs.append(h)
        if all(lo < float(rows[j]["low"]) for j in range(i - wing, i + wing + 1) if j != i):
            lows.append(lo)
    return {"highs": highs[-max_points:], "lows": lows[-max_points:]}


def _liq_price(entry: float, leverage: float, side: str) -> float:
    """近似爆仓价：多头 entry×(1−0.99/L)，空头 entry×(1+0.99/L)。"""
    d = 0.99 / max(1.0, leverage)
    return entry * (1.0 - d) if side == "long" else entry * (1.0 + d)


def estimate(price: float, oi_notional_usdt: float | None,
             vp_distributions: list[dict], swing: dict | None,
             cfg: dict | None = None,
             force_orders: list[dict] | None = None) -> dict:
    """纯函数核心：三层估算 → 磁吸位列表。永不抛出（调用方保证入参类型）。

    vp_distributions: [{poc, volumeShare, ...}]（jarvis_volume_profile 契约）
    swing: {"highs": [...], "lows": [...]}（find_swings 输出）
    force_orders: [{price|avg_price, ...}]（jarvis_ws_stream 落库行；None=不可用）
    """
    c = cfg or {}
    if not (price and math.isfinite(price) and price > 0):
        return {"ok": False, "error": "现价非法", "magnets": []}
    lev_weights = parse_leverage_weights(c.get("liq_leverage_weights"))
    bin_w = price * BIN_PCT

    # ── 1. 清算簇：VP 入场分布 × 杠杆档 → 双向爆仓价加权点 ──
    points: list[tuple[float, float, str]] = []  # (price, strength, kind)
    for dist in vp_distributions or []:
        try:
            entry = float(dist.get("poc") or 0)
            share = float(dist.get("volumeShare") or 0)
        except (TypeError, ValueError):
            continue
        if entry <= 0 or share <= 0:
            continue
        for lev, w in lev_weights:
            points.append((_liq_price(entry, lev, "long"), share * w, "long_liq"))
            points.append((_liq_price(entry, lev, "short"), share * w, "short_liq"))

    # ── 2. 止损密集区：摆动点外侧 + 整数关口 ──
    sw = swing or {}
    for h in sw.get("highs") or []:
        points.append((float(h) * 1.001, STOP_BASE_STRENGTH, "stop_cluster"))
    for lo in sw.get("lows") or []:
        points.append((float(lo) * 0.999, STOP_BASE_STRENGTH, "stop_cluster"))
    step = _round_step(price)
    base = math.floor(price / step) * step
    for k in range(-3, 4):
        lv = base + k * step
        if lv <= 0 or abs(lv - price) / price > 0.15:
            continue
        strength = ROUND_LEVEL_STRENGTH
        # 关口贴近摆动点 → 双重流动性，强度叠加
        near_swing = any(abs(lv - float(p)) / price < OVERLAP_PCT
                         for p in (sw.get("highs") or []) + (sw.get("lows") or []))
        if near_swing:
            strength += STOP_BASE_STRENGTH * 0.6
        points.append((lv, strength, "stop_cluster"))

    if not points:
        return {"ok": True, "price": price, "magnets": [], "calibration": None,
                "note": "上游数据不足，无可估算磁吸位"}

    # ── 3. 分箱聚合（按 kind 分开聚，避免多空清算簇互相稀释）──
    bins: dict[tuple[int, str], dict] = {}
    for p, s, kind in points:
        if p <= 0 or abs(p - price) / price > 0.25:   # 距现价 25% 外无操盘意义
            continue
        idx = int(p // bin_w)
        key = (idx, kind)
        b = bins.setdefault(key, {"kind": kind, "strength": 0.0,
                                  "lo": (idx) * bin_w, "hi": (idx + 1) * bin_w,
                                  "wsum": 0.0, "psum": 0.0})
        b["strength"] += s
        b["wsum"] += s
        b["psum"] += p * s

    clusters = []
    for b in bins.values():
        mid = b["psum"] / b["wsum"] if b["wsum"] > 0 else (b["lo"] + b["hi"]) / 2
        clusters.append({"kind": b["kind"], "price_mid": mid,
                         "price_low": b["lo"], "price_high": b["hi"],
                         "strength_raw": b["strength"]})

    # ── 4. forceOrder 校准 ──
    calibration = None
    hit_ratio = None
    if force_orders:
        hits = 0
        total = 0
        for fo in force_orders[:FO_LOOKBACK]:
            try:
                fp = float(fo.get("avg_price") or fo.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if fp <= 0:
                continue
            total += 1
            if any(c2["price_low"] - bin_w <= fp <= c2["price_high"] + bin_w
                   for c2 in clusters):
                hits += 1
        if total >= 5:
            hit_ratio = hits / total
            calibration = {"samples": total, "hits": hits,
                           "hit_ratio": round(hit_ratio, 3)}

    base_conf = 0.5
    confidence = round(base_conf + 0.4 * hit_ratio, 3) if hit_ratio is not None \
        else base_conf

    # ── 5. 分类输出：清算簇（多空爆仓，组内归一，上下各前 3）+ 止损簇独立 ──
    kind_cn = {"long_liq": "多头清算簇", "short_liq": "空头清算簇",
               "stop_cluster": "止损/关口簇"}

    def _fmt(c2: dict, max_s: float) -> dict:
        side = "above" if c2["price_mid"] > price else "below"
        dist_pct = (c2["price_mid"] / price - 1.0) * 100.0
        return {
            "kind": c2["kind"], "side": side,
            "price_mid": round(c2["price_mid"], 6),
            "price_low": round(c2["price_low"], 6),
            "price_high": round(c2["price_high"], 6),
            "strength": round(c2["strength_raw"] / max_s, 3) if max_s > 0 else 0.0,
            "confidence": confidence,
            "dist_pct": round(dist_pct, 3),
            "label": f"{kind_cn[c2['kind']]} {round(c2['price_mid'], 2)}"
                     f"（{dist_pct:+.2f}%）",
        }

    liq_raw = [c2 for c2 in clusters if c2["kind"] in ("long_liq", "short_liq")]
    stop_raw = [c2 for c2 in clusters if c2["kind"] == "stop_cluster"]
    liq_max = max((c2["strength_raw"] for c2 in liq_raw), default=0.0)
    stop_max = max((c2["strength_raw"] for c2 in stop_raw), default=0.0)

    liq_above = sorted((c2 for c2 in liq_raw if c2["price_mid"] > price),
                       key=lambda x: -x["strength_raw"])[:3]
    liq_below = sorted((c2 for c2 in liq_raw if c2["price_mid"] <= price),
                       key=lambda x: -x["strength_raw"])[:3]
    liq_clusters = [_fmt(c2, liq_max) for c2 in liq_above + liq_below]
    stop_clusters = [_fmt(c2, stop_max) for c2 in
                     sorted(stop_raw, key=lambda x: -x["strength_raw"])[:8]]

    # 合并视图（前端画线/磁吸位提醒统一消费）：按距现价排序
    magnets = sorted(liq_clusters + stop_clusters, key=lambda m: abs(m["dist_pct"]))
    return {"ok": True, "price": price, "magnets": magnets,
            "liq_clusters": liq_clusters, "stop_clusters": stop_clusters,
            "calibration": calibration,
            "oi_notional_usdt": oi_notional_usdt,
            "note": ("forceOrder 校准生效" if calibration else
                     "forceOrder 数据不可用/不足，置信度为未校准基线 0.5")}


def magnet_factor(result: dict, warn_pct: float) -> dict:
    """信号联动因子：现价是否逼近强磁吸位（插针风险提醒）。纯函数。"""
    magnets = (result or {}).get("magnets") or []
    if not magnets:
        return {"near": False, "magnet": None, "note": "无磁吸位数据"}
    # 只认强度 ≥0.5 的簇，取距现价最近者
    strong = [m for m in magnets if m.get("strength", 0) >= 0.5]
    if not strong:
        return {"near": False, "magnet": None, "note": "附近无强磁吸位"}
    nearest = min(strong, key=lambda m: abs(m.get("dist_pct", 999)))
    if abs(nearest["dist_pct"]) <= warn_pct:
        arrow = "上方" if nearest["side"] == "above" else "下方"
        return {"near": True, "magnet": nearest,
                "note": (f"⚠️ 现价距{arrow}强磁吸位 {nearest['price_mid']} 仅 "
                         f"{abs(nearest['dist_pct']):.2f}%（{nearest['label']}），"
                         "存在扫单/插针风险，谨慎追单")}
    return {"near": False, "magnet": nearest,
            "note": f"最近强磁吸位 {nearest['label']}，距离尚安全"}


# ────────────────────────── 门面（联网）──────────────────────────

def assess(symbol: str = "BTCUSDT", timeframe: str = "15m") -> dict:
    """拉真实数据 → estimate。任何上游失败降级，永不抛出。"""
    sym = (symbol if symbol.upper().endswith(("USDT", "USDC"))
           else symbol + "USDT").upper()
    try:
        import jarvis_config as jc
        cfg = jc.load()
    except Exception:  # noqa: BLE001
        cfg = {}
    price = None
    vp_dists: list[dict] = []
    swings = None
    oi_notional = None
    force_orders = None

    try:
        import jarvis_twelve_systems as jts
        df = jts.fetch_klines_df(sym, timeframe, 300)
        if df is not None and len(df) >= 30:
            price = float(df["close"].iloc[-1])
            rows = df.to_dict("records")
            swings = find_swings(rows)
    except Exception:  # noqa: BLE001
        pass
    if not price:
        return {"ok": False, "symbol": sym, "timeframe": timeframe,
                "error": "K 线数据不可用", "magnets": []}

    try:
        import jarvis_volume_profile as jvp
        vp = jvp.assess(sym, timeframe)
        if vp.get("ok"):
            vp_dists = vp.get("distributions") or []
    except Exception:  # noqa: BLE001
        pass

    try:
        import jarvis_crypto_data as jcd
        oi = jcd.fetch_oi(sym)
        contracts = oi.get("open_interest_contracts")
        if contracts:
            oi_notional = round(float(contracts) * price, 2)
    except Exception:  # noqa: BLE001
        pass

    try:
        import jarvis_ws_stream as jws
        if "forceOrder" not in (jws.health().get("degraded_streams") or []):
            rows_fo = jws.force_orders_recent(sym, limit=FO_LOOKBACK)
            if rows_fo:
                force_orders = rows_fo
    except Exception:  # noqa: BLE001
        pass

    out = estimate(price, oi_notional, vp_dists, swings, cfg, force_orders)
    out.update({"symbol": sym, "timeframe": timeframe,
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "disclaimer": "磁吸位为统计估算，仅供风险提示，不构成投资建议"})
    return out


def mock_assess(symbol: str = "BTCUSDT") -> dict:
    """确定性 mock（?mock=1 / 前端联调）。"""
    price = 60000.0
    vp = [{"poc": 60500.0, "volumeShare": 0.4},
          {"poc": 59000.0, "volumeShare": 0.35},
          {"poc": 61500.0, "volumeShare": 0.25}]
    swings = {"highs": [61800.0, 61000.0], "lows": [58800.0, 59400.0]}
    out = estimate(price, 1.2e9, vp, swings, {}, None)
    out.update({"symbol": symbol.upper(), "timeframe": "15m", "mock": True,
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "disclaimer": "mock 数据"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯清算/止损密集区估计器")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()
    out = mock_assess(args.symbol) if args.mock else assess(args.symbol, args.tf)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
