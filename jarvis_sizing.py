#!/usr/bin/env python3
"""贾维斯 JARVIS - 动态仓位 sizing（T-11：分数凯利 / 固定比例）。

替代纯固定比例仓位：按「胜率 + 盈亏比」用凯利公式算最优下注比例，再乘
分数凯利系数（保守），最后由调用方的风险红线（max_position_pct +
max_portfolio_risk_pct）封顶。**凯利只会建议更小或相近的仓位**，绝不突破
既有风控上限——风控红线永远是最后一道闸。

凯利公式（二元赔付）：
  b = 盈亏比 = take_profit% / |stop_loss%|
  p = 胜率, q = 1 - p
  f* = (p·b − q) / b        # 最优下注占资金比例
  f  = max(0, kelly_fraction · f*)   # 分数凯利，负 edge → 0（不下注）

设计原则：
  - 永不抛出：缺胜率/赔付等输入时返回 None，调用方回退固定比例（零回归）。
  - 只缩不放：返回值仅作「建议仓位」，仍需过 executor 的仓位/风险红线封顶。
  - 与 jarvis_config 协同：sizing_method / kelly_fraction 由配置中心提供。

用法（库）：
  from jarvis_sizing import suggest_position_pct
  pct = suggest_position_pct(win_prob=0.6, tp_pct=8, sl_pct=-10,
                             method="kelly", kelly_fraction=0.5, cap_pct=40)
"""

from __future__ import annotations

import argparse
import json
import sys


def kelly_star(win_prob: float, tp_pct: float, sl_pct: float) -> float | None:
    """原始凯利最优比例 f*（占资金）。输入非法返回 None。可为负（负 edge）。"""
    try:
        p = float(win_prob)
        b = float(tp_pct) / abs(float(sl_pct))
    except Exception:  # noqa: BLE001
        return None
    if not (0.0 <= p <= 1.0) or b <= 0:
        return None
    q = 1.0 - p
    return (p * b - q) / b


def kelly_position_pct(win_prob: float, tp_pct: float, sl_pct: float,
                       kelly_fraction: float = 0.5, cap_pct: float = 40.0) -> float | None:
    """分数凯利建议仓位%（已乘系数、负 edge 归零、封顶 cap_pct）。非法输入返回 None。"""
    f_star = kelly_star(win_prob, tp_pct, sl_pct)
    if f_star is None:
        return None
    try:
        frac = max(0.0, float(kelly_fraction))
    except Exception:  # noqa: BLE001
        frac = 0.5
    f = max(0.0, frac * f_star)
    pct = round(min(float(cap_pct), f * 100.0), 2)
    return pct


def suggest_position_pct(win_prob: float | None, tp_pct: float | None, sl_pct: float | None,
                         *, method: str = "fixed", kelly_fraction: float = 0.5,
                         cap_pct: float = 40.0, fixed_pct: float | None = None) -> dict:
    """统一 sizing 入口。返回 {method, position_pct, kelly_star, reason}。

    method='kelly' 且输入齐全 → 用分数凯利；否则回退 fixed（用 fixed_pct）。
    返回的 position_pct 永远 ≤ cap_pct；调用方仍需过组合风险红线再封顶。
    """
    out: dict = {"method": method, "position_pct": None, "kelly_star": None, "reason": ""}
    if method == "kelly":
        f_star = kelly_star(win_prob if win_prob is not None else -1,
                            tp_pct if tp_pct is not None else 0,
                            sl_pct if sl_pct is not None else 0)
        kpct = None
        if f_star is not None:
            kpct = kelly_position_pct(win_prob, tp_pct, sl_pct, kelly_fraction, cap_pct)
        out["kelly_star"] = round(f_star, 4) if f_star is not None else None
        if kpct is not None:
            out["position_pct"] = kpct
            out["reason"] = (f"分数凯利 f*={out['kelly_star']}×{kelly_fraction}→{kpct}%"
                             + ("（负 edge 归零）" if kpct == 0 else "")
                             + f"（封顶 {cap_pct}%）")
            return out
        # 凯利输入不全 → 回退固定
        out["method"] = "fixed(fallback)"
        out["reason"] = "凯利输入不全（缺胜率/赔付）→ 回退固定比例"

    # 固定比例
    fp = fixed_pct if fixed_pct is not None else 0.0
    out["position_pct"] = round(min(float(cap_pct), float(fp)), 2)
    if not out["reason"]:
        out["reason"] = f"固定比例 {out['position_pct']}%（封顶 {cap_pct}%）"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯动态仓位 sizing（凯利/固定）")
    ap.add_argument("--win-prob", type=float, required=True)
    ap.add_argument("--tp-pct", type=float, default=8.0)
    ap.add_argument("--sl-pct", type=float, default=-10.0)
    ap.add_argument("--method", choices=["fixed", "kelly"], default="kelly")
    ap.add_argument("--kelly-fraction", type=float, default=0.5)
    ap.add_argument("--cap-pct", type=float, default=40.0)
    ap.add_argument("--fixed-pct", type=float, default=None)
    args = ap.parse_args()
    out = suggest_position_pct(args.win_prob, args.tp_pct, args.sl_pct, method=args.method,
                               kelly_fraction=args.kelly_fraction, cap_pct=args.cap_pct,
                               fixed_pct=args.fixed_pct)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
