#!/usr/bin/env python3
"""贾维斯 JARVIS — [T-10] 多币相关性折算仓位。

破除"多币 = 分散"的假象：加密资产在暴跌时高度同向，名义上分散到多个币、
实际承担的仍是同一波系统性风险。本模块把雷达里**同向多币仓位**按日线收益的
相关性矩阵折算成「有效敞口」，并在有效敞口超过组合风险上限时**等比缩减**各币仓位。

设计：
  - 相关性来源：优先用日线收益的 Pearson 矩阵（数据由 jarvis_crypto_data 提供）；
    某对样本不足或无数据时，回退保守默认 DEFAULT_CORR（加密高相关，宁高勿低）。
  - 有效敞口：effective = sqrt(wᵀ·C·w)，把相关性当作单位波动下的协方差近似。
    · 全 1 相关矩阵 → effective == Σw（毫无分散，等于直接叠加）。
    · 相关越低 → effective 越小（真分散）。
  - 分散比 diversification_ratio = effective / Σw ∈ (0,1]，越接近 1 越说明"分散是假象"。
  - 超限缩减：effective 对权重是一次齐次（缩放 k → effective 缩放 k），
    故超 cap 时统一乘 factor = cap/effective，即可让有效敞口恰好压到 cap。

本模块纯计算 + 可注入数据源，便于离线测试，绝不抛出影响主流程。
"""

from __future__ import annotations

import math
from typing import Callable, Optional

# 加密暴跌高相关，数据缺失时的保守默认相关系数（宁高勿低，避免低估组合风险）。
DEFAULT_CORR = 0.8
# 组合有效敞口上限（占组合的百分比）。全相关时即等于名义总仓位上限。
DEFAULT_MAX_EFFECTIVE_PCT = 50.0


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """两序列的皮尔逊相关系数；长度 < 2 或任一方差为 0 时返回 None。"""
    n = min(len(xs), len(ys))
    if n < 2:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = sxy / math.sqrt(sxx * syy)
    return max(-1.0, min(1.0, r))


def returns_from_closes(closes: list[float]) -> list[float]:
    """收盘价序列 → 简单收益率序列（剔除非正价位避免除零）。"""
    out: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev and prev > 0:
            out.append((closes[i] - prev) / prev)
    return out


def correlation_matrix(
    symbols: list[str],
    returns_by_symbol: dict[str, list[float]],
    default_corr: float = DEFAULT_CORR,
) -> tuple[list[list[float]], dict]:
    """对称相关矩阵（对角 1.0）。某对样本不足时回退 default_corr。

    返回 (matrix, meta)，meta 含 n_estimated / n_defaulted / avg_offdiag。
    """
    n = len(symbols)
    mat = [[1.0] * n for _ in range(n)]
    n_est = 0
    n_def = 0
    offdiag: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            r = pearson(
                returns_by_symbol.get(symbols[i], []),
                returns_by_symbol.get(symbols[j], []),
            )
            if r is None:
                r = default_corr
                n_def += 1
            else:
                n_est += 1
            mat[i][j] = mat[j][i] = r
            offdiag.append(r)
    meta = {
        "n_pairs_estimated": n_est,
        "n_pairs_defaulted": n_def,
        "avg_offdiag_corr": round(sum(offdiag) / len(offdiag), 4) if offdiag else None,
        "default_corr": default_corr,
    }
    return mat, meta


def effective_exposure(weights: list[float], matrix: list[list[float]]) -> float:
    """有效敞口 = sqrt(wᵀ·C·w)。全 1 矩阵时等于 Σw。"""
    n = len(weights)
    quad = 0.0
    for i in range(n):
        for j in range(n):
            quad += weights[i] * weights[j] * matrix[i][j]
    return math.sqrt(quad) if quad > 0 else 0.0


def scale_to_cap(
    weights: list[float],
    matrix: list[list[float]],
    cap: float = DEFAULT_MAX_EFFECTIVE_PCT,
) -> dict:
    """有效敞口超 cap 时等比缩减权重，使有效敞口恰好压到 cap。

    返回 dict：naive_total / effective_before / effective_after /
    diversification_ratio / scale_factor / scaled(bool) / weights(缩减后)。
    """
    naive = sum(weights)
    eff_before = effective_exposure(weights, matrix)
    if eff_before > cap and eff_before > 0:
        factor = cap / eff_before
        scaled = True
    else:
        factor = 1.0
        scaled = False
    new_weights = [round(w * factor, 2) for w in weights]
    eff_after = effective_exposure(new_weights, matrix)
    return {
        "naive_total_pct": round(naive, 2),
        "effective_before_pct": round(eff_before, 2),
        "effective_after_pct": round(eff_after, 2),
        "diversification_ratio": round(eff_before / naive, 4) if naive > 0 else None,
        "scale_factor": round(factor, 4),
        "scaled": scaled,
        "cap_pct": cap,
        "weights": new_weights,
    }


def build_returns(
    symbols: list[str],
    days: int = 30,
    closes_provider: Optional[Callable[[str], list[float]]] = None,
) -> tuple[dict[str, list[float]], str]:
    """为每个币取日线收益序列。closes_provider 默认走 jarvis_crypto_data（可注入测试）。

    返回 (returns_by_symbol, source)；source ∈ {"daily_closes", "none"}。
    """
    if closes_provider is None:
        import jarvis_crypto_data as jcd
        closes_provider = lambda s: jcd.fetch_daily_closes(s, days)  # noqa: E731
    rbs: dict[str, list[float]] = {}
    got_any = False
    for s in symbols:
        try:
            closes = closes_provider(s) or []
        except Exception:  # noqa: BLE001 — 单币失败不拖垮整体
            closes = []
        rets = returns_from_closes(closes)
        rbs[s] = rets
        if len(rets) >= 2:
            got_any = True
    return rbs, ("daily_closes" if got_any else "none")


def adjust_positions(
    symbols: list[str],
    weights: list[float],
    days: int = 30,
    cap: float = DEFAULT_MAX_EFFECTIVE_PCT,
    closes_provider: Optional[Callable[[str], list[float]]] = None,
    default_corr: float = DEFAULT_CORR,
) -> dict:
    """端到端：取收益 → 相关矩阵 → 折算有效敞口 → 超 cap 缩减。

    返回汇总 dict（含 per_symbol 缩减明细、matrix meta、数据源）。
    symbols 为空或仅 1 个时不折算（单币无相关性问题）。
    """
    if len(symbols) <= 1:
        eff = sum(weights)
        return {
            "n": len(symbols),
            "naive_total_pct": round(eff, 2),
            "effective_before_pct": round(eff, 2),
            "effective_after_pct": round(eff, 2),
            "diversification_ratio": 1.0 if eff > 0 else None,
            "scale_factor": 1.0,
            "scaled": False,
            "cap_pct": cap,
            "corr_source": "n/a(single)",
            "avg_offdiag_corr": None,
            "per_symbol": [
                {"symbol": s, "position_pct": round(w, 2), "position_pct_adjusted": round(w, 2)}
                for s, w in zip(symbols, weights)
            ],
        }
    rbs, src = build_returns(symbols, days=days, closes_provider=closes_provider)
    matrix, meta = correlation_matrix(symbols, rbs, default_corr=default_corr)
    res = scale_to_cap(weights, matrix, cap=cap)
    if src == "none":
        # 无任何真实收益数据 → 全部按默认相关，标注来源便于排障。
        src = f"default_corr={default_corr}"
    return {
        "n": len(symbols),
        "naive_total_pct": res["naive_total_pct"],
        "effective_before_pct": res["effective_before_pct"],
        "effective_after_pct": res["effective_after_pct"],
        "diversification_ratio": res["diversification_ratio"],
        "scale_factor": res["scale_factor"],
        "scaled": res["scaled"],
        "cap_pct": cap,
        "corr_source": src,
        "avg_offdiag_corr": meta["avg_offdiag_corr"],
        "n_pairs_defaulted": meta["n_pairs_defaulted"],
        "per_symbol": [
            {"symbol": s, "position_pct": round(w, 2), "position_pct_adjusted": adj}
            for s, w, adj in zip(symbols, weights, res["weights"])
        ],
    }
