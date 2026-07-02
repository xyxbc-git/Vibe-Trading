#!/usr/bin/env python3
"""贾维斯 JARVIS - 预测层 ML（T-12：分类 + 分位数区间 + regime）。

在既有「因子事件研究 / 回测」之上加一层**有监督预测**，但严守科研纪律，
绝不做「全样本拟合 → 自我陶醉」的过拟合骗局：

  1) 三个预测头（heads）
     - 分类：未来 h 日「涨 / 跌 / 震荡」三分类（多项 Logistic）。
     - 分位数区间：未来 h 日收益的 [p10, p50, p90] 区间（分位回归）。
     - regime：当下市场状态（波动高低 × 趋势多空），纯规则、无需训练。
  2) 严格 OOS（walk-forward / 时间序列切分）
     - 只用「截至第 t 日已知信息」预测 t→t+h 收益，杜绝前瞻。
     - 用 TimeSeriesSplit 多折滚动，报告**样本外**准确率/覆盖率，不报训练内。
  3) 蒙特卡洛置换检验（permutation test）
     - 打乱标签重训 N 次，得到「无技能」零分布；真实 OOS 分数的
       p 值 = 零分布中 ≥ 真实分数的比例。p 大 → 预测力不显著（诚实告警）。

设计原则（与项目其余脚本一致）：
  - 纯库可被 smoketest **离线**调用：build_dataset / walk_forward_* 接收
    已构造好的序列，不联网；run() 才去拉真实数据。
  - 优雅降级：缺 sklearn → 回退 numpy 实现的多项 Logistic；缺 numpy →
    返回 {"_error": ...} 而非抛出。
  - 不构成交易建议，仅研究「特征对未来收益是否有统计上显著的预测力」。

用法：
  python jarvis_ml_predict.py                 # 拉真实数据跑 OOS + 蒙卡，markdown
  python jarvis_ml_predict.py --json
  python jarvis_ml_predict.py --symbol BTCUSDT --horizon 7 --mc-iter 200
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any, Optional

try:
    import numpy as np
    _HAS_NUMPY = True
except Exception:  # noqa: BLE001
    _HAS_NUMPY = False

try:
    from sklearn.linear_model import LogisticRegression, QuantileRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    from sklearn.model_selection import TimeSeriesSplit
    _HAS_SKLEARN = True
except Exception:  # noqa: BLE001
    _HAS_SKLEARN = False


# ---------------------------------------------------------------------------
# 1) 特征工程（全部因果：第 t 日特征只用 ≤t 的数据）
# ---------------------------------------------------------------------------

FEATURE_NAMES = [
    "ret_1", "ret_7", "ret_30",   # 过去 1/7/30 日动量
    "vol_30",                       # 30 日已实现波动（日收益标准差）
    "dist_ma50", "dist_ma200",     # 距 50/200 日均线（趋势位置）
    "drawdown",                     # 距滚动历史高点回撤
    "fng", "fng_chg_7",            # 恐慌贪婪 水平 + 7 日变化
    "funding",                      # 资金费率（缺则 0）
]


def _pct(a: float, b: float) -> float:
    return a / b - 1.0 if b else 0.0


def build_dataset(dates: list, prices: dict, fng: dict, funding: dict,
                  horizon: int = 7, range_thresh_pct: float = 3.0) -> dict:
    """构造监督学习数据集（防前瞻 + 三分类标签 + 前瞻收益）。

    返回 {X, y_class, y_fwd, feature_names, dates_used, latest_features}
      - X: 二维 list[list[float]]，每行一天的特征（FEATURE_NAMES 顺序）
      - y_class: 0=跌 / 1=震荡 / 2=涨（按 ±range_thresh_pct 切）
      - y_fwd:   未来 horizon 日收益（小数，如 0.05=+5%）
      - latest_features: 最新一天（无前瞻标签）的特征行，供实时预测
    标签 y[t] = 用 close[t+h]/close[t]-1 决定，因此最后 h 天无标签（仅入 latest）。
    """
    closes = [prices[d] for d in dates]
    n = len(closes)
    if n < horizon + 40:
        return {"_error": "样本不足", "n": n}

    # 预计算滚动序列
    rets = [0.0] + [_pct(closes[i], closes[i - 1]) for i in range(1, n)]
    ma50: list[Optional[float]] = []
    ma200: list[Optional[float]] = []
    dd: list[float] = []
    vol30: list[Optional[float]] = []
    peak = closes[0]
    for i in range(n):
        ma50.append(sum(closes[i - 49:i + 1]) / 50 if i >= 49 else None)
        ma200.append(sum(closes[i - 199:i + 1]) / 200 if i >= 199 else None)
        peak = max(peak, closes[i])
        dd.append(closes[i] / peak - 1.0)
        if i >= 30:
            window = rets[i - 29:i + 1]
            m = sum(window) / len(window)
            var = sum((r - m) ** 2 for r in window) / (len(window) - 1)
            vol30.append(math.sqrt(var))
        else:
            vol30.append(None)

    thr = range_thresh_pct / 100.0

    def feat_row(i: int) -> Optional[list]:
        if ma200[i] is None or vol30[i] is None:
            return None  # 需暖机期满（200 日均线）
        f = fng.get(dates[i])
        f7 = fng.get(dates[i - 7]) if i >= 7 else None
        fng_v = float(f) if f is not None else 50.0
        fng_chg = (float(f) - float(f7)) if (f is not None and f7 is not None) else 0.0
        ret7 = _pct(closes[i], closes[i - 7]) if i >= 7 else 0.0
        ret30 = _pct(closes[i], closes[i - 30]) if i >= 30 else 0.0
        fund = funding.get(dates[i])
        return [
            rets[i], ret7, ret30,
            vol30[i],
            _pct(closes[i], ma50[i]) if ma50[i] else 0.0,
            _pct(closes[i], ma200[i]),
            dd[i],
            fng_v, fng_chg,
            float(fund) if fund is not None else 0.0,
        ]

    X: list[list[float]] = []
    y_class: list[int] = []
    y_fwd: list[float] = []
    dates_used: list[str] = []
    for i in range(n):
        row = feat_row(i)
        if row is None:
            continue
        j = i + horizon
        if j >= n:
            continue  # 无前瞻标签
        fwd = _pct(closes[j], closes[i])
        cls = 2 if fwd > thr else (0 if fwd < -thr else 1)
        X.append(row)
        y_class.append(cls)
        y_fwd.append(fwd)
        dates_used.append(dates[i])

    if len(X) < 30:
        # 200 日均线暖机后有效样本过少：优雅降级（不抛、不训练）
        return {"_error": "有效样本不足(暖机期后)", "n": len(X), "raw_days": n}

    # 最新一天（可能无标签）用于实时预测
    latest_features = None
    latest_date = None
    for i in range(n - 1, -1, -1):
        row = feat_row(i)
        if row is not None:
            latest_features = row
            latest_date = dates[i]
            break

    return {
        "X": X, "y_class": y_class, "y_fwd": y_fwd,
        "feature_names": FEATURE_NAMES, "dates_used": dates_used,
        "latest_features": latest_features, "latest_date": latest_date,
        "horizon": horizon, "range_thresh_pct": range_thresh_pct, "n": len(X),
    }


# ---------------------------------------------------------------------------
# 2) 分类器（sklearn 多项 Logistic；缺则 numpy 兜底）
# ---------------------------------------------------------------------------

def _np_softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def _np_logreg_fit(X, y, n_class, epochs=300, lr=0.1, l2=1e-3):
    """极简多项 Logistic（numpy 兜底）。X 已标准化。返回 W,b。"""
    n, d = X.shape
    W = np.zeros((d, n_class))
    b = np.zeros(n_class)
    Y = np.eye(n_class)[y]
    for _ in range(epochs):
        P = _np_softmax(X @ W + b)
        gW = X.T @ (P - Y) / n + l2 * W
        gb = (P - Y).mean(axis=0)
        W -= lr * gW
        b -= lr * gb
    return W, b


def _fit_predict_classifier(Xtr, ytr, Xte):
    """训练分类器并返回 (pred_labels, pred_proba)。优先 sklearn。"""
    classes = sorted(set(ytr))
    if len(classes) < 2:
        # 训练集只有单一类：退化为常数预测
        const = ytr[0]
        return [const] * len(Xte), None
    if _HAS_SKLEARN:
        clf = make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1000, C=1.0),
        )
        clf.fit(Xtr, ytr)
        return list(clf.predict(Xte)), clf.predict_proba(Xte)
    # numpy 兜底
    Xtr = np.asarray(Xtr, float)
    Xte = np.asarray(Xte, float)
    mu = Xtr.mean(axis=0)
    sd = Xtr.std(axis=0) + 1e-9
    Xtr_s = (Xtr - mu) / sd
    Xte_s = (Xte - mu) / sd
    remap = {c: k for k, c in enumerate(classes)}
    inv = {k: c for c, k in remap.items()}
    y_idx = np.array([remap[v] for v in ytr])
    W, b = _np_logreg_fit(Xtr_s, y_idx, len(classes))
    P = _np_softmax(Xte_s @ W + b)
    preds = [inv[int(k)] for k in P.argmax(axis=1)]
    return preds, P


def fit_predict_explain(Xtr, ytr, Xte) -> tuple:
    """训练分类器并返回 (pred_labels, pred_proba, contribs)。

    contribs[i][j] = 第 i 条测试样本第 j 个特征对「预测类别」logit 的贡献
    （标准化特征值 × 该类权重），正=把预测往该类推，负=往反方向拉。
    仅线性模型（LogisticRegression / numpy 兜底）有解析归因；
    单一类退化场景返回全零贡献。
    """
    classes = sorted(set(ytr))
    if not _HAS_NUMPY:
        preds, proba = _fit_predict_classifier(Xtr, ytr, Xte)
        return preds, proba, [[0.0] * len(Xte[0]) for _ in Xte]
    if len(classes) < 2:
        const = ytr[0]
        return [const] * len(Xte), None, [[0.0] * len(Xte[0]) for _ in Xte]
    Xtr_a = np.asarray(Xtr, float)
    Xte_a = np.asarray(Xte, float)
    if _HAS_SKLEARN:
        scaler = StandardScaler().fit(Xtr_a)
        clf = LogisticRegression(max_iter=1000, C=1.0)
        clf.fit(scaler.transform(Xtr_a), ytr)
        Xte_s = scaler.transform(Xte_a)
        preds = list(clf.predict(Xte_s))
        proba = clf.predict_proba(Xte_s)
        coef = clf.coef_  # 二分类时 shape=(1,d)：正向=classes_[1]
        cls_list = list(clf.classes_)
        contribs = []
        for i, p in enumerate(preds):
            if coef.shape[0] == 1:
                sign = 1.0 if p == cls_list[1] else -1.0
                w = coef[0] * sign
            else:
                w = coef[cls_list.index(p)]
            contribs.append((w * Xte_s[i]).tolist())
        return preds, proba, contribs
    mu = Xtr_a.mean(axis=0)
    sd = Xtr_a.std(axis=0) + 1e-9
    Xtr_s = (Xtr_a - mu) / sd
    Xte_s = (Xte_a - mu) / sd
    remap = {c: k for k, c in enumerate(classes)}
    inv = {k: c for c, k in remap.items()}
    y_idx = np.array([remap[v] for v in ytr])
    W, b = _np_logreg_fit(Xtr_s, y_idx, len(classes))
    P = _np_softmax(Xte_s @ W + b)
    preds = [inv[int(k)] for k in P.argmax(axis=1)]
    contribs = [(W[:, remap[p]] * Xte_s[i]).tolist() for i, p in enumerate(preds)]
    return preds, P, contribs


def humanize_attribution(feature_names: list, values: list, contrib: list,
                         templates: dict, top_k: int = 3) -> dict:
    """把线性归因贡献转成人话：支持本次判断的前 top_k 因素 + 最强反向因素。

    templates: {feature_name: callable(value)->str}，未登记的特征跳过
    （如 hour_sin/cos 这类单看无意义的编码维度）。
    """
    items = []
    for n, v, c in zip(feature_names, values, contrib):
        fmt = templates.get(n)
        if fmt is None:
            continue
        try:
            items.append({"factor": fmt(v), "weight": round(float(c), 3)})
        except Exception:  # noqa: BLE001 — 单个因素格式化失败不拖垮整体
            continue
    support = sorted([it for it in items if it["weight"] > 0],
                     key=lambda x: -x["weight"])[:top_k]
    oppose = sorted([it for it in items if it["weight"] < 0],
                    key=lambda x: x["weight"])[:1]
    return {"support": support, "oppose": oppose}


def attribution_text(direction: str, why: dict) -> str:
    """归因 → 一句中文总结，如「看涨主因：7天动量+5.2%、RSI 31 …；反向信号：…」。"""
    sup = "、".join(it["factor"] for it in why.get("support", []))
    opp = "、".join(it["factor"] for it in why.get("oppose", []))
    label = {"涨": "看涨", "跌": "看跌", "震荡": "判震荡",
             "up": "看涨", "down": "看跌", "range": "判震荡"}.get(direction, direction)
    txt = f"{label}主因：{sup or '各因素均弱'}"
    if opp:
        txt += f"；反向信号：{opp}"
    return txt


def walk_forward_classify(X: list, y: list, n_splits: int = 5) -> dict:
    """时间序列 walk-forward 样本外分类评估。

    返回总体 OOS 准确率、各类别 precision/recall、基线（多数类/三等分）对照。
    """
    if not _HAS_NUMPY:
        return {"_error": "需要 numpy"}
    n = len(X)
    if n < n_splits + 10:
        return {"_error": "样本太少", "n": n}
    Xa = np.asarray(X, float)
    ya = np.asarray(y, int)

    # 用 TimeSeriesSplit（若有），否则手工等分滚动
    folds = []
    if _HAS_SKLEARN:
        tss = TimeSeriesSplit(n_splits=n_splits)
        folds = list(tss.split(Xa))
    else:
        step = n // (n_splits + 1)
        for k in range(1, n_splits + 1):
            tr_end = step * k
            te_end = min(step * (k + 1), n)
            if te_end > tr_end:
                folds.append((np.arange(tr_end), np.arange(tr_end, te_end)))

    all_true: list[int] = []
    all_pred: list[int] = []
    all_te_idx: list[int] = []
    for tr_idx, te_idx in folds:
        preds, _ = _fit_predict_classifier(
            Xa[tr_idx].tolist(), ya[tr_idx].tolist(), Xa[te_idx].tolist())
        all_true.extend(ya[te_idx].tolist())
        all_pred.extend(preds)
        all_te_idx.extend(int(i) for i in te_idx)

    if not all_true:
        return {"_error": "无 OOS 预测"}
    acc = sum(1 for t, p in zip(all_true, all_pred) if t == p) / len(all_true)

    # 基线：永远猜训练多数类 ≈ 测试集最大类占比
    from collections import Counter
    cnt = Counter(all_true)
    majority_acc = max(cnt.values()) / len(all_true)

    # 各类 precision / recall（0 跌 / 1 震 / 2 涨）
    labels = [0, 1, 2]
    per_class = {}
    names = {0: "down", 1: "range", 2: "up"}
    for c in labels:
        tp = sum(1 for t, p in zip(all_true, all_pred) if t == c and p == c)
        fp = sum(1 for t, p in zip(all_true, all_pred) if t != c and p == c)
        fn = sum(1 for t, p in zip(all_true, all_pred) if t == c and p != c)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        per_class[names[c]] = {
            "support": cnt.get(c, 0),
            "precision": round(prec, 3),
            "recall": round(rec, 3),
        }

    return {
        "n_oos": len(all_true),
        "n_folds": len(folds),
        "oos_accuracy": round(acc, 4),
        "majority_baseline_acc": round(majority_acc, 4),
        "skill_vs_baseline": round(acc - majority_acc, 4),
        "per_class": per_class,
        # 供蒙卡/方向命中率复用，markdown 不展示；_te_idx 与 _true/_pred 逐位对齐
        "_true": all_true, "_pred": all_pred, "_te_idx": all_te_idx,
    }


def monte_carlo_pvalue(X: list, y: list, n_splits: int = 5,
                       n_iter: int = 200, seed: int = 42) -> dict:
    """置换检验：打乱标签重做 walk-forward，估计真实 OOS 准确率的 p 值。

    p_value = (#{置换acc ≥ 真实acc} + 1) / (n_iter + 1)
    p 小（如 <0.05）→ 预测力显著；p 大 → 与随机无异，诚实告警勿轻信模型。
    """
    if not _HAS_NUMPY:
        return {"_error": "需要 numpy"}
    real = walk_forward_classify(X, y, n_splits=n_splits)
    if "_error" in real:
        return real
    real_acc = real["oos_accuracy"]
    rng = np.random.default_rng(seed)
    ya = np.asarray(y, int)
    ge = 0
    perm_accs = []
    for _ in range(n_iter):
        yp = ya.copy()
        rng.shuffle(yp)
        r = walk_forward_classify(X, yp.tolist(), n_splits=n_splits)
        a = r.get("oos_accuracy", 0.0)
        perm_accs.append(a)
        if a >= real_acc:
            ge += 1
    p = (ge + 1) / (n_iter + 1)
    return {
        "real_oos_acc": real_acc,
        "n_iter": n_iter,
        "perm_acc_mean": round(float(np.mean(perm_accs)), 4),
        "perm_acc_p95": round(float(np.quantile(perm_accs, 0.95)), 4),
        "p_value": round(p, 4),
        "significant_5pct": bool(p < 0.05),
    }


# ---------------------------------------------------------------------------
# 3) 分位数区间（分位回归 OOS 覆盖率）
# ---------------------------------------------------------------------------

def walk_forward_quantiles(X: list, y_fwd: list, quantiles=(0.1, 0.5, 0.9),
                           n_splits: int = 5) -> dict:
    """前瞻收益的分位数区间预测 + OOS 覆盖率校准。

    若 sklearn 可用 → QuantileRegressor 逐分位拟合；否则回退「训练集经验分位」
    （无条件分位，作为弱基线）。覆盖率 = 真实落入 [q_lo,q_hi] 的比例，理想≈80%。
    """
    if not _HAS_NUMPY:
        return {"_error": "需要 numpy"}
    n = len(X)
    if n < n_splits + 10:
        return {"_error": "样本太少", "n": n}
    Xa = np.asarray(X, float)
    ya = np.asarray(y_fwd, float)
    q_lo, q_mid, q_hi = quantiles

    if _HAS_SKLEARN:
        tss = TimeSeriesSplit(n_splits=n_splits)
        folds = list(tss.split(Xa))
    else:
        step = n // (n_splits + 1)
        folds = [(np.arange(step * k), np.arange(step * k, min(step * (k + 1), n)))
                 for k in range(1, n_splits + 1)]

    los, mids, his, trues = [], [], [], []
    for tr_idx, te_idx in folds:
        Xtr, ytr = Xa[tr_idx], ya[tr_idx]
        Xte = Xa[te_idx]
        if _HAS_SKLEARN:
            try:
                scaler = StandardScaler().fit(Xtr)
                Xtr_s, Xte_s = scaler.transform(Xtr), scaler.transform(Xte)
                preds = {}
                for q in (q_lo, q_mid, q_hi):
                    qr = QuantileRegressor(quantile=q, alpha=0.001, solver="highs")
                    qr.fit(Xtr_s, ytr)
                    preds[q] = qr.predict(Xte_s)
                los.extend(preds[q_lo].tolist())
                mids.extend(preds[q_mid].tolist())
                his.extend(preds[q_hi].tolist())
                trues.extend(ya[te_idx].tolist())
                continue
            except Exception:  # noqa: BLE001
                pass  # 落回经验分位
        lo_v = float(np.quantile(ytr, q_lo))
        mid_v = float(np.quantile(ytr, q_mid))
        hi_v = float(np.quantile(ytr, q_hi))
        m = len(te_idx)
        los.extend([lo_v] * m)
        mids.extend([mid_v] * m)
        his.extend([hi_v] * m)
        trues.extend(ya[te_idx].tolist())

    if not trues:
        return {"_error": "无 OOS 预测"}
    nominal = q_hi - q_lo
    inside = sum(1 for t, lo, hi in zip(trues, los, his) if lo <= t <= hi)
    coverage = inside / len(trues)
    # 区间平均宽度（小数收益）
    width = float(np.mean([hi - lo for lo, hi in zip(los, his)]))
    # 中位预测的方向命中（mid>0 且真实>0，或都<0）
    dir_hit = sum(1 for t, m in zip(trues, mids) if (t > 0) == (m > 0)) / len(trues)
    return {
        "quantiles": list(quantiles),
        "nominal_coverage": round(nominal, 3),
        "oos_coverage": round(coverage, 4),
        "coverage_gap": round(coverage - nominal, 4),
        "avg_interval_width_pct": round(width * 100, 2),
        "median_dir_hit": round(dir_hit, 4),
        "n_oos": len(trues),
        "backend": "quantile_regression" if _HAS_SKLEARN else "empirical_quantile",
    }


# ---------------------------------------------------------------------------
# 4) regime（纯规则，无需训练）
# ---------------------------------------------------------------------------

def detect_regime(features: list, feature_names: Optional[list] = None) -> dict:
    """由最新特征判定市场状态：波动(高/中/低) × 趋势(多/震/空)。"""
    fn = feature_names or FEATURE_NAMES
    f = dict(zip(fn, features))
    vol = f.get("vol_30", 0.0)
    ma200_dist = f.get("dist_ma200", 0.0)
    dd = f.get("drawdown", 0.0)
    if vol >= 0.045:
        vol_state = "high"
    elif vol >= 0.025:
        vol_state = "mid"
    else:
        vol_state = "low"
    if ma200_dist > 0.05:
        trend = "bull"
    elif ma200_dist < -0.05:
        trend = "bear"
    else:
        trend = "range"
    return {
        "vol_regime": vol_state,
        "trend_regime": trend,
        "vol_30_pct": round(vol * 100, 2),
        "dist_ma200_pct": round(ma200_dist * 100, 2),
        "drawdown_pct": round(dd * 100, 2),
        "label": f"{trend}/{vol_state}-vol",
    }


def predict_latest(dataset: dict, n_splits: int = 5) -> dict:
    """用全样本训练一个分类器，对最新一天给出概率（仅参考，非样本外）。"""
    if "_error" in dataset or not _HAS_NUMPY:
        return {"_error": dataset.get("_error", "需要 numpy")}
    X, y = dataset["X"], dataset["y_class"]
    latest = dataset.get("latest_features")
    if latest is None:
        return {"_error": "无最新特征"}
    preds, proba = _fit_predict_classifier(X, y, [latest])
    out = {
        "date": dataset.get("latest_date"),
        "pred_class": {0: "down", 1: "range", 2: "up"}.get(int(preds[0]), str(preds[0])),
    }
    if proba is not None:
        classes = sorted(set(y))
        p = proba[0]
        name = {0: "down", 1: "range", 2: "up"}
        out["proba"] = {name.get(c, str(c)): round(float(p[k]), 3)
                        for k, c in enumerate(classes)}
    out["regime"] = detect_regime(latest, dataset["feature_names"])
    return out


# ---------------------------------------------------------------------------
# 5) 编排：拉真实数据 → 全流程
# ---------------------------------------------------------------------------

def run(symbol: str = "BTCUSDT", horizon: int = 7, range_thresh_pct: float = 3.0,
        n_splits: int = 5, mc_iter: int = 200) -> dict:
    if not _HAS_NUMPY:
        return {"_error": "缺 numpy，无法运行 ML 预测层"}
    try:
        from jarvis_factor_backtest import (
            fetch_price_daily, fetch_fng_all, fetch_funding_daily)
    except Exception as e:  # noqa: BLE001
        return {"_error": f"导入数据源失败: {e!r}"}

    prices = fetch_price_daily(symbol)
    fng = fetch_fng_all()
    funding = fetch_funding_daily(symbol)
    dates = sorted(set(prices) & set(fng))
    if len(dates) < horizon + 240:
        return {"_error": "数据不足以训练（需≥240+h天）", "days": len(dates)}

    ds = build_dataset(dates, prices, fng, funding, horizon, range_thresh_pct)
    if "_error" in ds:
        return ds

    cls = walk_forward_classify(ds["X"], ds["y_class"], n_splits=n_splits)
    mc = monte_carlo_pvalue(ds["X"], ds["y_class"], n_splits=n_splits, n_iter=mc_iter)
    qt = walk_forward_quantiles(ds["X"], ds["y_fwd"], n_splits=n_splits)
    latest = predict_latest(ds, n_splits=n_splits)

    # 去掉内部大数组
    cls_pub = {k: v for k, v in cls.items() if not k.startswith("_")}
    return {
        "symbol": symbol,
        "horizon_days": horizon,
        "range_thresh_pct": range_thresh_pct,
        "sample_start": ds["dates_used"][0] if ds["dates_used"] else None,
        "sample_end": ds["dates_used"][-1] if ds["dates_used"] else None,
        "n_samples": ds["n"],
        "sklearn": _HAS_SKLEARN,
        "classification": cls_pub,
        "monte_carlo": mc,
        "quantile_interval": qt,
        "latest_prediction": latest,
    }


def to_markdown(r: dict) -> str:
    if "_error" in r:
        return f"ML 预测层失败: {r}"
    cls = r.get("classification", {})
    mc = r.get("monte_carlo", {})
    qt = r.get("quantile_interval", {})
    lt = r.get("latest_prediction", {})
    lines = [
        f"## 预测层 ML（T-12）— {r['symbol']} 未来 {r['horizon_days']} 日",
        "",
        f"样本: {r.get('sample_start')} → {r.get('sample_end')}  "
        f"({r.get('n_samples')} 行) | backend: "
        f"{'sklearn' if r.get('sklearn') else 'numpy 兜底'}",
        "",
        "### 1) 三分类 涨/跌/震（严格样本外 walk-forward）",
        "",
        f"- OOS 准确率: **{cls.get('oos_accuracy')}** vs 多数类基线 "
        f"{cls.get('majority_baseline_acc')}（净技能 {cls.get('skill_vs_baseline')}）",
        f"- 折数: {cls.get('n_folds')} | OOS 样本: {cls.get('n_oos')}",
    ]
    pc = cls.get("per_class", {})
    if pc:
        lines += [
            "",
            "| 类别 | 支持数 | 精确率 | 召回率 |",
            "|------|------|------|------|",
        ]
        for name in ("up", "range", "down"):
            m = pc.get(name, {})
            lines.append(
                f"| {name} | {m.get('support', 0)} | "
                f"{m.get('precision', 0)} | {m.get('recall', 0)} |")
    lines += [
        "",
        "### 2) 蒙特卡洛置换检验（预测力是否显著）",
        "",
        f"- 真实 OOS acc {mc.get('real_oos_acc')} | 置换均值 "
        f"{mc.get('perm_acc_mean')} | 置换 p95 {mc.get('perm_acc_p95')}",
        f"- **p 值 = {mc.get('p_value')}** → "
        f"{'✅ 显著(<0.05)，预测力非偶然' if mc.get('significant_5pct') else '⚠️ 不显著，与随机无异，勿轻信'}",
        "",
        "### 3) 分位数区间（前瞻收益 [p10,p50,p90] 校准）",
        "",
        f"- 名义覆盖 {qt.get('nominal_coverage')} vs OOS 实测覆盖 "
        f"**{qt.get('oos_coverage')}**（偏差 {qt.get('coverage_gap')}）",
        f"- 区间平均宽度 {qt.get('avg_interval_width_pct')}% | 中位方向命中 "
        f"{qt.get('median_dir_hit')} | backend: {qt.get('backend')}",
    ]
    if lt and "_error" not in lt:
        rg = lt.get("regime", {})
        proba = lt.get("proba", {})
        lines += [
            "",
            "### 4) 最新一天预测 + regime（仅参考）",
            "",
            f"- 日期 {lt.get('date')} | 分类预测: **{lt.get('pred_class')}**"
            + (f" | 概率 {proba}" if proba else ""),
            f"- regime: **{rg.get('label')}**（vol30 {rg.get('vol_30_pct')}% / "
            f"距200MA {rg.get('dist_ma200_pct')}% / 回撤 {rg.get('drawdown_pct')}%）",
        ]
    lines += [
        "",
        "> 严格防前瞻：第 t 日仅用 ≤t 信息预测 t→t+h。OOS=样本外滚动，非训练内拟合。",
        "> 蒙卡 p 值大即坦白「无显著预测力」。仅研究统计特性，不构成交易建议。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 预测层 ML（T-12）")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--horizon", type=int, default=7)
    ap.add_argument("--range-thresh", type=float, default=3.0,
                    help="涨/跌阈值(%%)，|前瞻收益|≤该值算震荡")
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--mc-iter", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = run(symbol=args.symbol, horizon=args.horizon,
            range_thresh_pct=args.range_thresh,
            n_splits=args.splits, mc_iter=args.mc_iter)
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else to_markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
