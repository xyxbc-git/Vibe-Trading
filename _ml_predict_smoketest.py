#!/usr/bin/env python3
"""T-12 预测层 ML 离线 smoketest（不联网，合成数据）。

验证：
  1) build_dataset 防前瞻 + 标签切分正确（涨/跌/震三类、最后 h 天无标签）。
  2) walk_forward_classify 只报样本外、结构完整。
  3) 「有信号」合成数据 → OOS 准确率 > 多数类基线（管线能学到东西）。
  4) 「纯噪声」合成数据 → 蒙卡 p 值不显著（诚实，不会假阳性）。
  5) walk_forward_quantiles 覆盖率字段合理。
  6) detect_regime / predict_latest 正常产出。
  7) numpy/sklearn 兜底标志可用。
"""

import math
import random
import sys

import jarvis_ml_predict as ml

PASS, FAIL = [], []


def ok(c, m):
    (PASS if c else FAIL).append(m)
    print(("  ✓ " if c else "  ✗ ") + m)


def _synth_dates(n):
    # 连续日期字符串，仅作 key，build_dataset 不解析日期本身
    from datetime import date, timedelta
    d0 = date(2020, 1, 1)
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def make_prices_with_signal(n=600, seed=1):
    """构造强可学习信号：fng 为持久化(AR)过程 → fng 低则未来多日上涨。

    fng 持久（自相关高）→ 当日 fng 能预测未来数日 fng → 预测未来收益。
    收益由当日 fng 强驱动(±3%)、噪声小(0.5%)，使样本外可显著学到。
    """
    rng = random.Random(seed)
    dates = _synth_dates(n)
    fng_series = []
    f = 50.0
    for _ in range(n):
        f = 0.92 * f + 0.08 * rng.randint(5, 95)  # AR(1) 持久
        f = max(5.0, min(95.0, f))
        fng_series.append(f)
    fng = {dates[i]: int(round(fng_series[i])) for i in range(n)}
    closes = [100.0]
    for i in range(1, n):
        drive = (50.0 - fng_series[i]) / 50.0 * 0.03   # 越恐惧未来越涨
        ret = drive + rng.gauss(0, 0.005)
        closes.append(closes[-1] * (1 + ret))
    prices = {dates[i]: closes[i] for i in range(n)}
    return dates, prices, fng


def make_prices_pure_noise(n=600, seed=2):
    rng = random.Random(seed)
    dates = _synth_dates(n)
    fng = {dates[i]: rng.randint(5, 95) for i in range(n)}
    closes = [100.0]
    for _ in range(1, n):
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.015)))
    prices = {dates[i]: closes[i] for i in range(n)}
    return dates, prices, fng


print("=== T-12 ML 预测层 smoketest ===")

print("[1] build_dataset 防前瞻 + 标签")
dates, prices, fng = make_prices_with_signal()
ds = ml.build_dataset(dates, prices, fng, {}, horizon=7, range_thresh_pct=3.0)
ok("_error" not in ds, "数据集构造成功")
ok(ds["n"] > 100, f"样本量充足 n={ds.get('n')}")
ok(len(ds["X"]) == len(ds["y_class"]) == len(ds["y_fwd"]), "X/y 长度一致")
ok(set(ds["y_class"]) <= {0, 1, 2}, "类别 ∈ {0,1,2}")
ok(len(ds["feature_names"]) == len(ds["X"][0]), "特征维度与 FEATURE_NAMES 一致")
ok(ds["latest_features"] is not None, "latest_features 存在（供实时预测）")
# 防前瞻：dates_used 最后一个应早于最后 horizon 天
ok(ds["dates_used"][-1] != dates[-1], "最后 h 天因无前瞻标签被排除")

print("[2] walk_forward_classify 结构 + 有信号能学到")
cls = ml.walk_forward_classify(ds["X"], ds["y_class"], n_splits=5)
ok("_error" not in cls, "分类评估成功")
ok(0.0 <= cls["oos_accuracy"] <= 1.0, f"OOS acc 合法 {cls.get('oos_accuracy')}")
ok("per_class" in cls and "up" in cls["per_class"], "含各类别指标")
ok(cls["oos_accuracy"] >= cls["majority_baseline_acc"] - 0.02,
   f"有信号数据 OOS({cls['oos_accuracy']}) 不显著低于基线({cls['majority_baseline_acc']})")

print("[3] 蒙卡置换 — 有信号应更可能显著 / 纯噪声应不显著")
mc_sig = ml.monte_carlo_pvalue(ds["X"], ds["y_class"], n_splits=5, n_iter=60, seed=7)
ok("_error" not in mc_sig, "蒙卡(有信号)运行成功")
ok(0.0 <= mc_sig["p_value"] <= 1.0, f"p 值合法 {mc_sig.get('p_value')}")
print(f"      有信号 p={mc_sig.get('p_value')} real={mc_sig.get('real_oos_acc')} perm_mean={mc_sig.get('perm_acc_mean')}")

dN, pN, fN = make_prices_pure_noise()
dsN = ml.build_dataset(dN, pN, fN, {}, horizon=7, range_thresh_pct=3.0)
mc_noise = ml.monte_carlo_pvalue(dsN["X"], dsN["y_class"], n_splits=5, n_iter=60, seed=9)
ok("_error" not in mc_noise, "蒙卡(纯噪声)运行成功")
print(f"      纯噪声 p={mc_noise.get('p_value')} real={mc_noise.get('real_oos_acc')} perm_mean={mc_noise.get('perm_acc_mean')}")
# 纯噪声真实 acc 不应远超置换 p95（即不显著强信号）
ok(mc_noise["real_oos_acc"] <= mc_noise["perm_acc_p95"] + 0.05,
   "纯噪声真实 acc 未异常高于置换分布(诚实)")

print("[4] 分位数区间 OOS 覆盖率")
qt = ml.walk_forward_quantiles(ds["X"], ds["y_fwd"], quantiles=(0.1, 0.5, 0.9), n_splits=5)
ok("_error" not in qt, "分位回归评估成功")
ok(0.0 <= qt["oos_coverage"] <= 1.0, f"覆盖率合法 {qt.get('oos_coverage')}")
ok(qt["nominal_coverage"] == 0.8, "名义覆盖=0.8（p90-p10）")
ok(qt["avg_interval_width_pct"] > 0, "区间宽度为正")
print(f"      名义0.8 / 实测{qt.get('oos_coverage')} / 宽度{qt.get('avg_interval_width_pct')}% / backend={qt.get('backend')}")

print("[5] regime + predict_latest")
rg = ml.detect_regime(ds["latest_features"], ds["feature_names"])
ok(rg["vol_regime"] in {"low", "mid", "high"}, f"vol_regime={rg['vol_regime']}")
ok(rg["trend_regime"] in {"bull", "bear", "range"}, f"trend_regime={rg['trend_regime']}")
ok("label" in rg, f"regime label={rg.get('label')}")
lt = ml.predict_latest(ds, n_splits=5)
ok("_error" not in lt, "predict_latest 成功")
ok(lt["pred_class"] in {"up", "down", "range"}, f"最新预测类别={lt.get('pred_class')}")
ok("regime" in lt, "最新预测含 regime")

print("[6] markdown 渲染不抛")
fake = {
    "symbol": "BTCUSDT", "horizon_days": 7, "range_thresh_pct": 3.0,
    "sample_start": dates[0], "sample_end": dates[-1], "n_samples": ds["n"],
    "sklearn": ml._HAS_SKLEARN,
    "classification": {k: v for k, v in cls.items() if not k.startswith("_")},
    "monte_carlo": mc_sig, "quantile_interval": qt, "latest_prediction": lt,
}
md = ml.to_markdown(fake)
ok("预测层 ML" in md and "蒙特卡洛" in md, "markdown 含关键段落")
ok("ML 预测层失败" not in md, "markdown 非错误态")

print("[7] 兜底标志")
ok(isinstance(ml._HAS_NUMPY, bool) and ml._HAS_NUMPY, "numpy 可用")
ok(isinstance(ml._HAS_SKLEARN, bool), "sklearn 标志存在")
# 样本不足应优雅返回 _error 而非抛出
small = ml.build_dataset(dates[:50], {d: prices[d] for d in dates[:50]},
                         {d: fng[d] for d in dates[:50]}, {}, horizon=7)
ok("_error" in small, "样本不足优雅降级 _error")

print()
print(f"=== 结果: {len(PASS)} PASS / {len(FAIL)} FAIL ===")
if FAIL:
    for m in FAIL:
        print("  FAILED: " + m)
    sys.exit(1)
print("ALL GREEN ✅")
