#!/usr/bin/env python3
"""贾维斯 JARVIS - 滑点 / 流动性建模（T-07）。

下单与回测都要诚实计入「成交不在理想价」的成本：买入吃单略贵、卖出略便宜，
大单冲击更明显，流动性差的 alt 尤甚。本模块给出**保守、可解释**的滑点估计，
用于：① 下单前预估冲击成本（executor）；② 回测扣除滑点后的真实 edge。

模型（一次成交，单位 bps=万分之一）：
  one_way_bps = half_spread_bps + impact_bps
  half_spread_bps : 按币种流动性分层的半价差（BTC 最紧、长尾最宽）
  impact_bps      : 平方根市场冲击 = impact_coef · sqrt(notional / ADV) · 10000
                    （订单越大 / 日成交额 ADV 越小 → 冲击越大；经典近似）
  可选波动率加成   : 高波动时点差与冲击放大（volatility_mult）

设计原则：
  - 保守优先：参数偏高估，宁可高估成本也不低估（避免回测虚高）。
  - 永不抛出：输入缺失用分层默认 ADV；异常回退一个保守常数。
  - 纯离线可算：不依赖实时盘口，ADV 可由调用方传入真实值以提升精度。

用法（库）：
  from jarvis_slippage import estimate_slippage_pct, round_trip_cost_bps
  s = estimate_slippage_pct("BTCUSDT", notional_usdt=400)
  # CLI：
  python jarvis_slippage.py BTCUSDT --notional 1000
"""

from __future__ import annotations

import argparse
import json
import math
import sys

# 币种流动性分层：half_spread_bps（半价差）+ adv_usdt（典型日成交额，保守估）。
# 数值偏保守；调用方可传真实 ADV 覆盖。
TIERS: dict[str, dict] = {
    "tier1": {"half_spread_bps": 1.0, "adv_usdt": 2.0e10},   # BTC
    "tier2": {"half_spread_bps": 1.5, "adv_usdt": 1.0e10},   # ETH
    "tier3": {"half_spread_bps": 3.0, "adv_usdt": 1.5e9},    # 主流 alt（SOL/BNB/XRP...）
    "tier4": {"half_spread_bps": 6.0, "adv_usdt": 2.0e8},    # 其它长尾
}

_TIER1 = {"BTCUSDT", "BTC"}
_TIER2 = {"ETHUSDT", "ETH"}
_TIER3 = {"SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
          "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK"}

IMPACT_COEF = 0.6           # 平方根冲击系数（保守偏高）
MAX_ONE_WAY_BPS = 800.0     # 单次滑点上限（防极端大单算出离谱值）
FALLBACK_BPS = 10.0         # 异常兜底（一次 10bps=0.1%）


def _tier(symbol: str) -> str:
    s = (symbol or "").strip().upper().replace("-", "").replace("/", "")
    if s in _TIER1:
        return "tier1"
    if s in _TIER2:
        return "tier2"
    if s in _TIER3:
        return "tier3"
    return "tier4"


def estimate_slippage_pct(symbol: str, notional_usdt: float, *, side: str = "buy",
                          adv_usdt: float | None = None, volatility_pct: float | None = None) -> dict:
    """估计单次成交滑点。返回 {one_way_bps, one_way_pct, half_spread_bps, impact_bps, tier, ...}。

    one_way_pct 为正数（成本）。side 仅用于语义标注，成本大小与方向无关。
    """
    try:
        tier = _tier(symbol)
        spec = TIERS[tier]
        half = float(spec["half_spread_bps"])
        adv = float(adv_usdt) if adv_usdt and adv_usdt > 0 else float(spec["adv_usdt"])
        notional = max(0.0, float(notional_usdt))
        impact = IMPACT_COEF * math.sqrt(notional / adv) * 10000.0 if adv > 0 else 0.0
        vol_mult = 1.0
        if volatility_pct is not None:
            try:
                # 日波动 >3% 起线性放大，最高 ×2（高波动点差/冲击扩大）
                vol_mult = min(2.0, max(1.0, 1.0 + (float(volatility_pct) - 3.0) / 10.0))
            except Exception:  # noqa: BLE001
                vol_mult = 1.0
        one_way = min(MAX_ONE_WAY_BPS, (half + impact) * vol_mult)
        return {
            "symbol": symbol,
            "tier": tier,
            "side": side,
            "notional_usdt": round(notional, 2),
            "adv_usdt": adv,
            "half_spread_bps": round(half, 3),
            "impact_bps": round(impact, 3),
            "volatility_mult": round(vol_mult, 3),
            "one_way_bps": round(one_way, 3),
            "one_way_pct": round(one_way / 100.0, 4),
        }
    except Exception as exc:  # noqa: BLE001 — 保守兜底
        return {
            "symbol": symbol, "tier": "unknown", "side": side,
            "one_way_bps": FALLBACK_BPS, "one_way_pct": round(FALLBACK_BPS / 100.0, 4),
            "error": repr(exc)[:160],
        }


def round_trip_cost_bps(symbol: str, notional_usdt: float, *, adv_usdt: float | None = None,
                        volatility_pct: float | None = None) -> float:
    """一进一出的往返滑点成本（bps）= 2 × 单次。回测每笔交易按此扣减。"""
    one = estimate_slippage_pct(symbol, notional_usdt, adv_usdt=adv_usdt,
                                volatility_pct=volatility_pct).get("one_way_bps", FALLBACK_BPS)
    return round(2.0 * float(one), 3)


def apply_fill_price(ref_price: float, side: str, one_way_bps: float) -> float:
    """把参考价按滑点调成预估成交价：买入更贵、卖出更便宜。"""
    adj = float(one_way_bps) / 10000.0
    if str(side).lower().startswith("s"):  # sell
        return round(float(ref_price) * (1.0 - adj), 8)
    return round(float(ref_price) * (1.0 + adj), 8)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯滑点/流动性估计")
    ap.add_argument("symbol")
    ap.add_argument("--notional", type=float, default=1000.0, help="名义下单额(USDT)")
    ap.add_argument("--side", default="buy")
    ap.add_argument("--adv", type=float, default=None, help="真实日成交额(USDT)覆盖分层默认")
    ap.add_argument("--vol", type=float, default=None, help="日波动率%%（可选加成）")
    args = ap.parse_args()
    out = estimate_slippage_pct(args.symbol, args.notional, side=args.side,
                                adv_usdt=args.adv, volatility_pct=args.vol)
    out["round_trip_bps"] = round_trip_cost_bps(args.symbol, args.notional,
                                                adv_usdt=args.adv, volatility_pct=args.vol)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
