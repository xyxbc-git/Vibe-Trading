#!/usr/bin/env python3
"""贾维斯 JARVIS - 4 小时预测引擎（4h 一轮：特征 → walk-forward 验证 → 实时预测）。

在 `jarvis_ml_predict.py`（日线 7/30 天）之外，新增 **4h bar 级别**的短周期
预测头，供 `jarvis_intraday_trader.py` 每 4 小时一轮自动模拟下单使用。

科研纪律与日线版完全一致，且更严：
  1) 特征全部因果（第 t 根 bar 的特征只用 ≤t 的数据），预测只用**已收盘** bar。
  2) walk-forward 严格样本外（复用 jarvis_ml_predict 的分类器与折法）。
  3) 置换检验 p 值。
  4) **可交易门禁写死在代码里**：样本外方向命中率 > GATE_MIN_HIT 且
     p < GATE_MAX_P 才输出 tradeable=True；不达标 → 交易引擎只观察不下单。

标签设计：未来 1 根（4h）收益三分类，阈值 = LABEL_ATR_MULT × ATR%（自适应
波动，替代固定百分比——BTC 平静期和暴动期的"显著波动"不是一个量级）。

用法：
  python jarvis_intraday_predict.py --symbol BTCUSDT            # markdown 报告
  python jarvis_intraday_predict.py --symbol BTCUSDT --json
  python jarvis_intraday_predict.py --symbol BTCUSDT --mc-iter 100
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from typing import Any, Optional

from jarvis_ml_predict import (
    _HAS_NUMPY,
    _fit_predict_classifier,
    walk_forward_classify,
)

# ── 门禁常量（改这里 = 改交易准入口径，须走 code-audit）──────────────────────
GATE_MIN_HIT = 0.52     # 样本外方向命中率下限（喊方向后下根收盘同向即命中）
GATE_MAX_P = 0.10       # 置换检验 p 值上限（与命中率同口径，含市场漂移基线）
LABEL_ATR_MULT = 0.5    # 标签阈值 = 0.5 × ATR%（4h 尺度的"显著"波动）
WARMUP_BARS = 200       # 200 根均线暖机
MIN_SAMPLES = 120       # 有效样本下限（约 20 天 4h bar）

FEATURE_NAMES_4H = [
    "ret_1", "ret_6", "ret_42",    # 过去 1 根(4h) / 6 根(24h) / 42 根(7天) 动量
    "vol_30",                       # 30 根已实现波动
    "dist_ma50", "dist_ma200",     # 距 50/200 根均线
    "rsi_14",                       # 14 根 RSI（0~1 归一）
    "vol_zscore",                   # 成交量 z 分数（30 根窗口）
    "atr_pct",                      # 14 根 ATR / close（小数）
    "hour_sin", "hour_cos",        # bar 开盘 UTC 小时周期编码
    "funding",                      # 当期资金费率（缺则 0）
]


def _pct(a: float, b: float) -> float:
    return a / b - 1.0 if b else 0.0


def _rsi(closes: list[float], i: int, period: int = 14) -> Optional[float]:
    """Wilder RSI 简化版（等权窗口），返回 0~1；暖机不足返回 None。"""
    if i < period:
        return None
    gains = losses = 0.0
    for k in range(i - period + 1, i + 1):
        chg = closes[k] - closes[k - 1]
        if chg >= 0:
            gains += chg
        else:
            losses -= chg
    if gains + losses <= 0:
        return 0.5
    return gains / (gains + losses)


def build_dataset_4h(bars: list[dict], funding: Optional[dict] = None) -> dict:
    """从 4h bars 构造监督数据集（防前瞻 + ATR 自适应三分类标签）。

    bars: fetch_kline 输出（升序，含 ts/open/high/low/close/volume），
          调用方须已丢弃进行中的最后一根（本函数不判断收盘状态）。
    funding: 可选 {bar_ts_ms: rate}，缺省全 0。
    返回 {X, y_class, y_fwd, feature_names, ts_used, latest_features,
          latest_ts, latest_close, latest_atr_pct, n}；样本不足返回 {"_error"}。
    标签 y[t]：fwd = close[t+1]/close[t]-1，thr = LABEL_ATR_MULT*atr_pct[t]，
              fwd>thr → 2(涨)，fwd<-thr → 0(跌)，否则 1(震荡)。
    """
    n = len(bars)
    if n < WARMUP_BARS + 40:
        return {"_error": f"bar 不足（{n} < {WARMUP_BARS + 40}）", "n": n}
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]
    fund = funding or {}

    rets = [0.0] + [_pct(closes[i], closes[i - 1]) for i in range(1, n)]
    ma50: list[Optional[float]] = []
    ma200: list[Optional[float]] = []
    vol30: list[Optional[float]] = []
    atrp: list[Optional[float]] = []
    for i in range(n):
        ma50.append(sum(closes[i - 49:i + 1]) / 50 if i >= 49 else None)
        ma200.append(sum(closes[i - 199:i + 1]) / 200 if i >= 199 else None)
        if i >= 30:
            w = rets[i - 29:i + 1]
            m = sum(w) / len(w)
            var = sum((r - m) ** 2 for r in w) / (len(w) - 1)
            vol30.append(math.sqrt(var))
        else:
            vol30.append(None)
        if i >= 14:
            trs = []
            for k in range(i - 13, i + 1):
                tr = max(highs[k] - lows[k],
                         abs(highs[k] - closes[k - 1]),
                         abs(lows[k] - closes[k - 1]))
                trs.append(tr)
            atrp.append((sum(trs) / len(trs)) / closes[i] if closes[i] else None)
        else:
            atrp.append(None)

    def feat_row(i: int) -> Optional[list]:
        if ma200[i] is None or vol30[i] is None or atrp[i] is None:
            return None
        r = _rsi(closes, i)
        if r is None:
            return None
        # 成交量 z 分数（30 根窗口）
        w = vols[i - 29:i + 1]
        m = sum(w) / len(w)
        sd = math.sqrt(sum((v - m) ** 2 for v in w) / (len(w) - 1)) or 1e-9
        vz = (vols[i] - m) / sd
        hour = time.gmtime(bars[i]["ts"] / 1000).tm_hour
        return [
            rets[i],
            _pct(closes[i], closes[i - 6]) if i >= 6 else 0.0,
            _pct(closes[i], closes[i - 42]) if i >= 42 else 0.0,
            vol30[i],
            _pct(closes[i], ma50[i]) if ma50[i] else 0.0,
            _pct(closes[i], ma200[i]),
            r,
            vz,
            atrp[i],
            math.sin(2 * math.pi * hour / 24),
            math.cos(2 * math.pi * hour / 24),
            float(fund.get(bars[i]["ts"], 0.0) or 0.0),
        ]

    X: list[list[float]] = []
    y_class: list[int] = []
    y_fwd: list[float] = []
    ts_used: list[int] = []
    for i in range(n - 1):  # 最后一根无前瞻标签
        row = feat_row(i)
        if row is None:
            continue
        fwd = _pct(closes[i + 1], closes[i])
        thr = LABEL_ATR_MULT * (atrp[i] or 0.0)
        cls = 2 if fwd > thr else (0 if fwd < -thr else 1)
        X.append(row)
        y_class.append(cls)
        y_fwd.append(fwd)
        ts_used.append(bars[i]["ts"])

    if len(X) < MIN_SAMPLES:
        return {"_error": f"有效样本不足（{len(X)} < {MIN_SAMPLES}）", "n": len(X)}

    latest_features = feat_row(n - 1)
    return {
        "X": X, "y_class": y_class, "y_fwd": y_fwd,
        "feature_names": FEATURE_NAMES_4H, "ts_used": ts_used,
        "latest_features": latest_features, "latest_ts": bars[-1]["ts"],
        "latest_close": closes[-1], "latest_atr_pct": atrp[-1],
        "n": len(X),
    }


def directional_hit_rate(y_fwd: list[float], y_pred: list[int]) -> dict:
    """方向命中率（交易语义）：只统计模型敢喊方向（预测≠震荡）的样本。

    hit = 喊涨且下根实际收涨(fwd>0) 或 喊跌且下根实际收跌(fwd<0)。
    与 jarvis_intraday_trader._backfill 的前向回填口径完全一致——
    旧版曾要求「实际也越过 ±0.5×ATR 阈值」才算命中，导致随机基线仅
    ≈17%~33%，门禁 0.52 按 50% 基线设定根本够不着（口径错配 bug）。
    """
    calls = [(f, p) for f, p in zip(y_fwd, y_pred) if p != 1]
    if not calls:
        return {"n_calls": 0, "hit_rate": 0.0}
    hits = sum(1 for f, p in calls
               if (p == 2 and f > 0) or (p == 0 and f < 0))
    return {"n_calls": len(calls), "hit_rate": round(hits / len(calls), 4)}


def monte_carlo_hit_pvalue(X: list, y_class: list, y_fwd: list,
                           n_splits: int = 5, n_iter: int = 200,
                           seed: int = 42) -> dict:
    """置换检验（与门禁同口径）：打乱训练标签重做 walk-forward，
    统计「方向命中率 ≥ 真实值」的比例作为 p 值。

    直接在 directional_hit_rate 指标上做置换，而非三分类准确率——
    市场有漂移（上涨 bar 占比≠50%）时，瞎猜同一方向也能拿到漂移收益，
    置换分布会如实反映这一点，比固定 50% 基线诚实。
    置换后模型不喊方向（n_calls=0）按「与真实打平」计（保守，抬高 p）。
    """
    if not _HAS_NUMPY:
        return {"_error": "需要 numpy"}
    import numpy as np
    real_wf = walk_forward_classify(X, y_class, n_splits=n_splits)
    if "_error" in real_wf:
        return real_wf
    fwd_oos = [y_fwd[i] for i in real_wf["_te_idx"]]
    real = directional_hit_rate(fwd_oos, real_wf["_pred"])
    rng = np.random.default_rng(seed)
    ya = np.asarray(y_class, int)
    ge = 0
    perm_hits = []
    for _ in range(n_iter):
        yp = ya.copy()
        rng.shuffle(yp)
        wf = walk_forward_classify(X, yp.tolist(), n_splits=n_splits)
        if "_error" in wf:
            ge += 1
            continue
        fwd_p = [y_fwd[i] for i in wf["_te_idx"]]
        d = directional_hit_rate(fwd_p, wf["_pred"])
        if d["n_calls"] == 0:
            ge += 1  # 无方向调用视为打平（保守）
            continue
        perm_hits.append(d["hit_rate"])
        if d["hit_rate"] >= real["hit_rate"]:
            ge += 1
    p = (ge + 1) / (n_iter + 1)
    return {
        "real_hit_rate": real["hit_rate"],
        "n_calls": real["n_calls"],
        "n_iter": n_iter,
        "perm_hit_mean": round(float(np.mean(perm_hits)), 4) if perm_hits else None,
        "p_value": round(p, 4),
        "_wf": real_wf,
    }


def validate(dataset: dict, n_splits: int = 5, mc_iter: int = 200) -> dict:
    """walk-forward OOS + 方向命中率 + 同口径置换检验 → 可交易门禁判定。"""
    if "_error" in dataset:
        return {"_error": dataset["_error"]}
    if not _HAS_NUMPY:
        return {"_error": "缺 numpy"}
    X, y, y_fwd = dataset["X"], dataset["y_class"], dataset["y_fwd"]
    mc = monte_carlo_hit_pvalue(X, y, y_fwd, n_splits=n_splits, n_iter=mc_iter)
    if "_error" in mc:
        return {"_error": mc["_error"]}
    wf = mc["_wf"]
    dhr = {"hit_rate": mc["real_hit_rate"], "n_calls": mc["n_calls"]}
    p = mc["p_value"]
    tradeable = bool(dhr["hit_rate"] > GATE_MIN_HIT and p < GATE_MAX_P
                     and dhr["n_calls"] >= 30)
    out = {
        "n_samples": dataset["n"],
        "oos_accuracy": wf["oos_accuracy"],
        "majority_baseline_acc": wf["majority_baseline_acc"],
        "oos_hit_rate": dhr["hit_rate"],
        "n_direction_calls": dhr["n_calls"],
        "p_value": p,
        "tradeable": tradeable,
        "gate": {"min_hit": GATE_MIN_HIT, "max_p": GATE_MAX_P},
    }
    if not tradeable:
        reasons = []
        if dhr["hit_rate"] <= GATE_MIN_HIT:
            reasons.append(f"OOS 方向命中率 {dhr['hit_rate']} ≤ {GATE_MIN_HIT}")
        if p >= GATE_MAX_P:
            reasons.append(f"置换检验 p={p} ≥ {GATE_MAX_P}（预测力不显著）")
        if dhr["n_calls"] < 30:
            reasons.append(f"方向样本仅 {dhr['n_calls']} 条（<30 统计不足）")
        out["reason"] = "；".join(reasons) or "未知"
    return out


def predict_latest(symbol: str = "BTCUSDT", n_splits: int = 5,
                   mc_iter: int = 200,
                   stop_atr_mult: float = 1.2,
                   take_atr_mult: float = 1.8,
                   _bars: Optional[list] = None) -> dict:
    """拉最新 4h bars → 验证门禁 → 对最新已收盘 bar 出实时预测。永不抛出。

    _bars 仅供离线测试注入；生产调用不传。
    同一根 bar 内重复调用结果一致（数据与模型均确定性）。
    """
    try:
        if _bars is not None:
            bars = list(_bars)
        else:
            import jarvis_crypto_data as jcd
            bars = jcd.fetch_kline(symbol, "4h", 1500)
        if len(bars) >= 2:
            # 丢弃进行中的最后一根（开盘时间 + 4h > 现在 → 未收盘）
            if bars[-1]["ts"] + 4 * 3600 * 1000 > time.time() * 1000:
                bars = bars[:-1]
        ds = build_dataset_4h(bars)
        if "_error" in ds:
            return {"symbol": symbol, "tradeable": False, "reason": ds["_error"]}
        gate = validate(ds, n_splits=n_splits, mc_iter=mc_iter)
        if "_error" in gate:
            return {"symbol": symbol, "tradeable": False, "reason": gate["_error"]}

        latest = ds["latest_features"]
        if latest is None:
            return {"symbol": symbol, "tradeable": False, "reason": "最新 bar 特征缺失"}
        preds, proba = _fit_predict_classifier(ds["X"], ds["y_class"], [latest])
        cls = int(preds[0])
        name = {0: "跌", 1: "震荡", 2: "涨"}
        prob = None
        if proba is not None:
            classes = sorted(set(ds["y_class"]))
            idx = classes.index(cls) if cls in classes else None
            prob = round(float(proba[0][idx]), 4) if idx is not None else None

        price = ds["latest_close"]
        atr_pct = ds["latest_atr_pct"] or 0.0
        atr_abs = price * atr_pct
        if cls == 2:
            stop, take = price - stop_atr_mult * atr_abs, price + take_atr_mult * atr_abs
        elif cls == 0:
            stop, take = price + stop_atr_mult * atr_abs, price - take_atr_mult * atr_abs
        else:
            stop = take = None
        return {
            "symbol": symbol,
            "as_of_bar_ts": ds["latest_ts"],
            "direction": name[cls],
            "prob": prob,
            "entry": price,
            "stop": round(stop, 8) if stop else None,
            "take": round(take, 8) if take else None,
            "atr_pct": round(atr_pct * 100, 3),
            "tradeable": gate["tradeable"],
            "oos_hit_rate": gate["oos_hit_rate"],
            "p_value": gate["p_value"],
            "n_samples": gate["n_samples"],
            **({"reason": gate["reason"]} if not gate["tradeable"] else {}),
        }
    except Exception as e:  # noqa: BLE001 — 预测失败绝不拖垮交易心跳
        return {"symbol": symbol, "tradeable": False, "reason": f"异常: {e!r}"[:300]}


def to_markdown(r: dict) -> str:
    lines = [
        f"## 4h 预测引擎 — {r.get('symbol')}",
        "",
        f"- 方向: **{r.get('direction', '—')}**（prob {r.get('prob', '—')}）",
        f"- 入场 {r.get('entry', '—')} / 止损 {r.get('stop', '—')} / 止盈 {r.get('take', '—')}"
        f"（ATR {r.get('atr_pct', '—')}%）",
        f"- 可交易门禁: {'✅ 通过' if r.get('tradeable') else '🚫 未通过'}"
        + (f"（{r.get('reason')}）" if r.get("reason") else ""),
        f"- OOS 方向命中率 {r.get('oos_hit_rate', '—')} · p={r.get('p_value', '—')}"
        f" · 样本 {r.get('n_samples', '—')}",
        "",
        "> 门禁未通过时交易引擎只观察不下单；模拟盘研究，不构成投资建议。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 4h 预测引擎")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--mc-iter", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = predict_latest(args.symbol.upper(), mc_iter=args.mc_iter)
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else to_markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
