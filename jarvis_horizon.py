#!/usr/bin/env python3
"""贾维斯 JARVIS - 中长线多周期预测头（15 / 30 天，日线级）。

在 4h 盘中引擎（jarvis_intraday_predict）与 30 天均值回归旧模式之外，
给驾驶舱提供统一口径的「未来 h 天」方向 + 点位预测：

  - 方向/概率：复用 jarvis_ml_predict.build_dataset 三分类（涨/跌/震荡），
    阈值随周期缩放（3% × √(h/7)，波动随时间开方增长）。
  - 点位：分位回归 p10/p50/p90 前瞻收益 → 换算目标价与区间
    （sklearn 缺失时回退训练集经验分位）。
  - 门禁：与 4h 引擎同款——方向命中率（交易语义）+ 同口径置换检验，
    未达标 tradeable=False，点位标灰仅参考。

纯函数可离线测（_data 注入），predict_horizon 才联网拉数据。
用法：
  python jarvis_horizon.py --symbol BTCUSDT --horizon 15 --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Optional

from jarvis_ml_predict import (
    _HAS_NUMPY,
    _HAS_SKLEARN,
    _fit_predict_classifier,
    build_dataset,
)
from jarvis_intraday_predict import (
    GATE_MAX_P,
    GATE_MIN_HIT,
    monte_carlo_hit_pvalue,
)

# 支持的周期（天）；驾驶舱三周期 = 4h（盘中引擎）+ 这里的 15/30
HORIZONS = (15, 30)


def _range_thresh(horizon_days: int) -> float:
    """三分类涨/跌阈值(%)：7 天 3% 基准按 √时间 缩放。"""
    return round(3.0 * math.sqrt(horizon_days / 7.0), 2)


def _latest_quantiles(X: list, y_fwd: list, latest: list,
                      quantiles=(0.1, 0.5, 0.9)) -> Optional[dict]:
    """全样本拟合分位回归，对最新一天出 [p10,p50,p90] 前瞻收益。

    sklearn 缺失/拟合失败 → 回退 y_fwd 无条件经验分位（弱基线，仍可用）。
    """
    if not _HAS_NUMPY:
        return None
    import numpy as np
    ya = np.asarray(y_fwd, float)
    out = {}
    if _HAS_SKLEARN:
        try:
            from sklearn.linear_model import QuantileRegressor
            from sklearn.preprocessing import StandardScaler
            Xa = np.asarray(X, float)
            scaler = StandardScaler().fit(Xa)
            Xs = scaler.transform(Xa)
            ls = scaler.transform([latest])
            for q in quantiles:
                qr = QuantileRegressor(quantile=q, alpha=0.001, solver="highs")
                qr.fit(Xs, ya)
                out[q] = float(qr.predict(ls)[0])
            out["backend"] = "quantile_regression"
            return out
        except Exception:  # noqa: BLE001
            pass
    for q in quantiles:
        out[q] = float(np.quantile(ya, q))
    out["backend"] = "empirical_quantile"
    return out


def predict_horizon(symbol: str = "BTCUSDT", horizon_days: int = 15,
                    n_splits: int = 5, mc_iter: int = 60,
                    _data: Optional[tuple] = None) -> dict:
    """未来 horizon_days 天 方向 + 目标点位 + 门禁。永不抛出。

    _data: 可选 (dates, prices, fng, funding)，供离线测试注入。
    返回与 4h 引擎对齐的结构：direction/prob/entry/target/target_lo/target_hi/
    tradeable/oos_hit_rate/p_value/reason。
    """
    try:
        if _data is not None:
            dates, prices, fng, funding = _data
        else:
            import time as _time
            from datetime import datetime, timezone
            import jarvis_crypto_data as jcd
            from jarvis_factor_backtest import fetch_fng_all, fetch_funding_daily
            # 日线走 jcd.fetch_kline：带缓存降级 + 限流退避，
            # 比 factor_backtest 直连 Binance 更抗断网（T-06 同款纪律）。
            bars = jcd.fetch_kline(symbol, "1d", 1400)
            now_ms = _time.time() * 1000
            if bars and bars[-1]["ts"] + 86_400_000 > now_ms:
                bars = bars[:-1]  # 丢弃进行中的当日 bar
            prices = {
                datetime.fromtimestamp(b["ts"] / 1000, tz=timezone.utc)
                .strftime("%Y-%m-%d"): b["close"]
                for b in bars
            }
            fng = fetch_fng_all()
            try:
                funding = fetch_funding_daily(symbol)
            except Exception:  # noqa: BLE001 — 资金费率可缺省（特征回退 0）
                funding = {}
            dates = sorted(set(prices) & set(fng))
        if len(dates) < horizon_days + 240:
            return {"symbol": symbol, "horizon_days": horizon_days,
                    "tradeable": False,
                    "reason": f"数据不足（{len(dates)} 天 < {horizon_days + 240}）"}

        thr = _range_thresh(horizon_days)
        ds = build_dataset(dates, prices, fng, funding,
                           horizon=horizon_days, range_thresh_pct=thr)
        if "_error" in ds:
            return {"symbol": symbol, "horizon_days": horizon_days,
                    "tradeable": False, "reason": ds["_error"]}

        # 门禁：与 4h 引擎完全同款（命中率交易口径 + 同口径置换检验）
        mc = monte_carlo_hit_pvalue(ds["X"], ds["y_class"], ds["y_fwd"],
                                    n_splits=n_splits, n_iter=mc_iter)
        if "_error" in mc:
            return {"symbol": symbol, "horizon_days": horizon_days,
                    "tradeable": False, "reason": mc["_error"]}
        hit, n_calls, p = mc["real_hit_rate"], mc["n_calls"], mc["p_value"]
        tradeable = bool(hit > GATE_MIN_HIT and p < GATE_MAX_P and n_calls >= 30)

        # 最新一天：方向 + 概率（全样本训练，仅实时出口）
        latest = ds.get("latest_features")
        if latest is None:
            return {"symbol": symbol, "horizon_days": horizon_days,
                    "tradeable": False, "reason": "最新特征缺失"}
        preds, proba = _fit_predict_classifier(ds["X"], ds["y_class"], [latest])
        cls = int(preds[0])
        prob = None
        if proba is not None:
            classes = sorted(set(ds["y_class"]))
            if cls in classes:
                prob = round(float(proba[0][classes.index(cls)]), 4)

        # 点位：分位回归 → 目标价（p50）与区间（p10~p90）
        price = prices[dates[-1]]
        qs = _latest_quantiles(ds["X"], ds["y_fwd"], latest)
        target = target_lo = target_hi = None
        if qs:
            target_lo = round(price * (1 + qs[0.1]), 8)
            target = round(price * (1 + qs[0.5]), 8)
            target_hi = round(price * (1 + qs[0.9]), 8)

        out = {
            "symbol": symbol,
            "horizon_days": horizon_days,
            "as_of_date": dates[-1],
            "direction": {0: "跌", 1: "震荡", 2: "涨"}[cls],
            "prob": prob,
            "entry": price,
            "target": target,
            "target_lo": target_lo,
            "target_hi": target_hi,
            "range_thresh_pct": thr,
            "quantile_backend": qs.get("backend") if qs else None,
            "n_samples": ds["n"],
            "oos_hit_rate": hit,
            "n_direction_calls": n_calls,
            "p_value": p,
            "tradeable": tradeable,
        }
        if not tradeable:
            reasons = []
            if hit <= GATE_MIN_HIT:
                reasons.append(f"OOS 方向命中率 {hit} ≤ {GATE_MIN_HIT}")
            if p >= GATE_MAX_P:
                reasons.append(f"置换检验 p={p} ≥ {GATE_MAX_P}（预测力不显著）")
            if n_calls < 30:
                reasons.append(f"方向样本仅 {n_calls} 条（<30 统计不足）")
            out["reason"] = "；".join(reasons) or "未知"
        return out
    except Exception as e:  # noqa: BLE001 — 预测失败绝不拖垮看板
        return {"symbol": symbol, "horizon_days": horizon_days,
                "tradeable": False, "reason": f"异常: {e!r}"[:300]}


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 中长线多周期预测头")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--horizon", type=int, default=15, choices=list(HORIZONS))
    ap.add_argument("--mc-iter", type=int, default=60)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = predict_horizon(args.symbol.upper(), args.horizon, mc_iter=args.mc_iter)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
