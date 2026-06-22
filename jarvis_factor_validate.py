#!/usr/bin/env python3
"""贾维斯 JARVIS - P4 因子验证：回撤因子是不是过拟合？

针对 P3 找到的「距历史高点回撤越深 → 30 天反弹」因子，做三重严格验证：
  1) 分年度拆解：edge 是每年都在，还是被某一年带飞？
  2) 样本外(OOS)：用前半段当“训练”，后半段当“验证”，看 edge 是否延续。
  3) 蒙特卡洛置换检验：随机抽同样多的天，看观测 edge 是否显著（p 值）。

复用 jarvis_factor_backtest.py 的真实数据拉取与序列构造。
不构成交易建议，仅评估因子统计稳健性。

用法：
  python jarvis_factor_validate.py
  python jarvis_factor_validate.py --threshold -0.30 --horizon 30
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time

from jarvis_factor_backtest import _build_series, fetch_fng_all, fetch_price_daily


def _fwd_returns(closes: list, horizon: int):
    """每个可用起点的 horizon 天前瞻收益，返回 [(i, ret)]。"""
    out = []
    for i in range(len(closes) - horizon):
        out.append((i, closes[i + horizon] / closes[i] - 1.0))
    return out


def _mean(xs: list) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _win(xs: list) -> float:
    return sum(1 for x in xs if x > 0) / len(xs) * 100 if xs else 0.0


def validate(threshold: float, horizon: int) -> dict:
    prices = fetch_price_daily()
    fng = fetch_fng_all()  # 暂未用于回撤因子，仅触发同源数据（保持一致）
    dates = sorted(prices)
    closes, _ma, dd = _build_series(dates, prices)

    fr = _fwd_returns(closes, horizon)  # [(i, ret)]
    baseline = [r for _, r in fr]
    b_mean = _mean(baseline)

    # 信号样本：回撤 <= threshold 的起点
    signal = [(i, r) for (i, r) in fr if dd[i] <= threshold]
    s_rets = [r for _, r in signal]
    observed_edge = _mean(s_rets) - b_mean

    # 1) 分年度
    by_year: dict[str, dict] = {}
    for (i, r) in fr:
        y = dates[i][:4]
        by_year.setdefault(y, {"sig": [], "all": []})
        by_year[y]["all"].append(r)
        if dd[i] <= threshold:
            by_year[y]["sig"].append(r)
    year_rows = []
    for y in sorted(by_year):
        s = by_year[y]["sig"]
        a = by_year[y]["all"]
        year_rows.append({
            "year": y,
            "n_signal": len(s),
            "signal_ret_pct": round(_mean(s) * 100, 2) if s else None,
            "baseline_ret_pct": round(_mean(a) * 100, 2),
            "edge_pct": round((_mean(s) - _mean(a)) * 100, 2) if s else None,
            "win_pct": round(_win(s), 1) if s else None,
        })

    # 2) 样本外切分（按时间中点）
    mid = len(dates) // 2
    split_date = dates[mid]

    def seg_edge(lo, hi):
        seg = [(i, r) for (i, r) in fr if lo <= i < hi]
        seg_all = [r for _, r in seg]
        seg_sig = [r for (i, r) in seg if dd[i] <= threshold]
        return {
            "n_signal": len(seg_sig),
            "signal_ret_pct": round(_mean(seg_sig) * 100, 2) if seg_sig else None,
            "baseline_ret_pct": round(_mean(seg_all) * 100, 2),
            "edge_pct": round((_mean(seg_sig) - _mean(seg_all)) * 100, 2) if seg_sig else None,
            "win_pct": round(_win(seg_sig), 1) if seg_sig else None,
        }

    oos = {
        "split_date": split_date,
        "train_first_half": seg_edge(0, mid),
        "test_second_half": seg_edge(mid, len(dates)),
    }

    # 3) 蒙特卡洛置换：随机抽同样多起点，比较平均收益分布
    n = len(s_rets)
    observed_mean = _mean(s_rets)
    random.seed(42)
    iters = 5000
    ge = 0
    rand_means = []
    pool = baseline
    for _ in range(iters):
        sample = [pool[random.randrange(len(pool))] for _ in range(n)]
        m = _mean(sample)
        rand_means.append(m)
        if m >= observed_mean:
            ge += 1
    p_value = ge / iters
    rand_means.sort()
    pct = sum(1 for m in rand_means if m < observed_mean) / iters * 100

    return {
        "factor": f"距历史高点回撤 <= {int(threshold*100)}% -> 做多, 持有 {horizon} 天",
        "fetched_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "sample": f"{dates[0]} → {dates[-1]} ({len(dates)} 天)",
        "fng_days": len(fng),
        "n_signal_events": n,
        "signal_mean_ret_pct": round(observed_mean * 100, 2),
        "baseline_mean_ret_pct": round(b_mean * 100, 2),
        "observed_edge_pct": round(observed_edge * 100, 2),
        "by_year": year_rows,
        "oos": oos,
        "monte_carlo": {
            "iterations": iters,
            "p_value": round(p_value, 4),
            "observed_percentile": round(pct, 1),
            "random_edge_p5_pct": round((rand_means[int(iters*0.05)] - b_mean) * 100, 2),
            "random_edge_p95_pct": round((rand_means[int(iters*0.95)] - b_mean) * 100, 2),
        },
    }


# ───────────── T-05: Walk-Forward 滚动窗口 / 带 embargo 的 Purged K-Fold ─────────────

def _factor_label(factor: str, threshold: float, horizon: int) -> str:
    if factor == "drawdown":
        return f"距高点回撤 <= {int(threshold * 100)}% → 做多, 持有 {horizon} 天"
    if factor == "breakout20":
        return f"20日新高突破（C4 正交动量）→ 做多, 持有 {horizon} 天"
    return f"{factor}, 持有 {horizon} 天"


def _signal_indices(factor: str, closes: list, dd: list, threshold: float) -> set:
    """构造因子信号起点集合，定义与生产 jarvis_brief 严格一致，避免验证错对象。"""
    idx: set = set()
    for i in range(len(closes)):
        if factor == "drawdown":
            if dd[i] <= threshold:
                idx.add(i)
        elif factor == "breakout20":
            # 当日收盘 >= 近20日(含当日)最高收盘 == brief.breakout_20d_active
            if i >= 19 and closes[i] >= max(closes[i - 19:i + 1]):
                idx.add(i)
    return idx


def _seg_edge(fr_block: list, sig_set: set) -> dict:
    """给定一段 [(i, ret)]，算信号 edge / 基线 / 信号数 / 胜率（edge_raw 为未百分化原值）。"""
    all_r = [r for _, r in fr_block]
    sig_r = [r for (i, r) in fr_block if i in sig_set]
    base = round(_mean(all_r) * 100, 2) if all_r else None
    if not all_r or not sig_r:
        return {"n_signal": len(sig_r), "edge_pct": None, "edge_raw": None,
                "signal_ret_pct": None, "baseline_ret_pct": base, "win_pct": None}
    edge = _mean(sig_r) - _mean(all_r)
    return {"n_signal": len(sig_r), "edge_pct": round(edge * 100, 2), "edge_raw": edge,
            "signal_ret_pct": round(_mean(sig_r) * 100, 2),
            "baseline_ret_pct": base, "win_pct": round(_win(sig_r), 1)}


def _stability(raw_edges: list) -> dict:
    """edge 序列稳定性：有效窗口 / 平均 / 标准差 / 正占比 / 极值。"""
    vals = [e for e in raw_edges if e is not None]
    if not vals:
        return {"windows_with_edge": 0, "mean_edge_pct": None, "std_edge_pct": None,
                "positive_ratio_pct": None, "min_edge_pct": None, "max_edge_pct": None}
    m = _mean(vals)
    std = (_mean([(v - m) ** 2 for v in vals])) ** 0.5
    return {"windows_with_edge": len(vals),
            "mean_edge_pct": round(m * 100, 2),
            "std_edge_pct": round(std * 100, 2),
            "positive_ratio_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
            "min_edge_pct": round(min(vals) * 100, 2),
            "max_edge_pct": round(max(vals) * 100, 2)}


def _prepare(factor: str, threshold: float, horizon: int):
    """拉真实数据并构造对齐序列 + 信号集 + 前瞻收益。"""
    prices = fetch_price_daily()
    fetch_fng_all()  # 触发同源数据，保持与 validate() 一致
    dates = sorted(prices)
    closes, _ma, dd = _build_series(dates, prices)
    fr = _fwd_returns(closes, horizon)
    sig = _signal_indices(factor, closes, dd, threshold)
    return dates, closes, fr, sig


def walk_forward(factor: str, threshold: float, horizon: int, n_windows: int = 6) -> dict:
    """滚动窗口逐段验证 edge 是否持续存在。"""
    dates, _closes, fr, sig = _prepare(factor, threshold, horizon)
    N = len(fr)
    if N < n_windows * 2:
        n_windows = max(2, N // 2)
    size = max(1, N // n_windows)
    rows: list = []
    raw_edges: list = []
    for w in range(n_windows):
        lo = w * size
        hi = (w + 1) * size if w < n_windows - 1 else N
        block = fr[lo:hi]
        if not block:
            continue
        seg = _seg_edge(block, sig)
        seg["window"] = w + 1
        seg["date_from"] = dates[block[0][0]]
        seg["date_to"] = dates[block[-1][0]]
        rows.append(seg)
        if seg["edge_raw"] is not None:
            raw_edges.append(seg["edge_raw"])
    return {"mode": "walk_forward", "factor": _factor_label(factor, threshold, horizon),
            "sample": f"{dates[0]} → {dates[-1]} ({len(dates)} 天)",
            "n_windows": len(rows), "horizon": horizon,
            "windows": rows, "stability": _stability(raw_edges)}


def purged_kfold(factor: str, threshold: float, horizon: int,
                 k: int = 5, embargo: int = 5) -> dict:
    """带 embargo 的 Purged K-Fold：剔除验证/训练块间因 horizon 前瞻重叠的泄漏样本。"""
    dates, _closes, fr, sig = _prepare(factor, threshold, horizon)
    N = len(fr)
    if k < 2:
        k = 2
    fold = max(1, N // k)
    guard = horizon + embargo
    rows: list = []
    raw_edges: list = []
    for f in range(k):
        lo = f * fold
        hi = (f + 1) * fold if f < k - 1 else N
        val = fr[lo:hi]
        if not val:
            continue
        train = fr[:max(0, lo - guard)] + fr[min(N, hi + guard):]
        v = _seg_edge(val, sig)
        t = _seg_edge(train, sig)
        v["fold"] = f + 1
        v["val_from"] = dates[val[0][0]]
        v["val_to"] = dates[val[-1][0]]
        v["train_edge_pct"] = t["edge_pct"]
        v["train_n_signal"] = t["n_signal"]
        rows.append(v)
        if v["edge_raw"] is not None:
            raw_edges.append(v["edge_raw"])
    return {"mode": "purged_kfold", "factor": _factor_label(factor, threshold, horizon),
            "sample": f"{dates[0]} → {dates[-1]} ({len(dates)} 天)",
            "k": k, "embargo": embargo, "horizon": horizon, "purge_guard_days": guard,
            "folds": rows, "stability": _stability(raw_edges),
            "note": ("因子为固定规则（无参数拟合），purge+embargo 在此用于剔除验证/训练块间因 "
                     f"{horizon} 天前瞻收益重叠造成的样本泄漏，使各折验证 edge 更独立可比。")}


def wf_to_markdown(r: dict) -> str:
    st = r["stability"]
    lines = [f"## T-05 Walk-Forward 滚动验证 — {r['factor']}", "",
             f"样本: {r['sample']} | 窗口数 {r['n_windows']} | 前瞻 {r['horizon']} 天", "",
             "| 窗口 | 区间 | 信号数 | 信号收益 | 基线收益 | edge | 胜率 |",
             "|------|------|------|------|------|------|------|"]
    for w in r["windows"]:
        lines.append(f"| {w['window']} | {w['date_from']}→{w['date_to']} | {w['n_signal']} | "
                     f"{w['signal_ret_pct']}% | {w['baseline_ret_pct']}% | {w['edge_pct']}% | {w['win_pct']}% |")
    lines += ["", "### 稳定性",
              f"- 有效窗口: {st['windows_with_edge']} | 正 edge 占比: **{st['positive_ratio_pct']}%**",
              f"- 平均 edge: {st['mean_edge_pct']}% | 标准差: {st['std_edge_pct']}%",
              f"- edge 区间: [{st['min_edge_pct']}%, {st['max_edge_pct']}%]", "",
              "> 滚动窗口逐段验证 edge 是否持续存在；正占比越高、标准差越小越稳健。数据真实拉取。"]
    return "\n".join(lines)


def purged_to_markdown(r: dict) -> str:
    st = r["stability"]
    lines = [f"## T-05 Purged K-Fold 验证 — {r['factor']}", "",
             f"样本: {r['sample']} | 折数 K={r['k']} | embargo={r['embargo']} 天 | "
             f"purge 隔离={r['purge_guard_days']} 天 | 前瞻 {r['horizon']} 天", "",
             "| 折 | 验证区间 | 验证信号数 | 验证 edge | 胜率 | 训练 edge | 训练信号数 |",
             "|------|------|------|------|------|------|------|"]
    for w in r["folds"]:
        lines.append(f"| {w['fold']} | {w['val_from']}→{w['val_to']} | {w['n_signal']} | "
                     f"{w['edge_pct']}% | {w['win_pct']}% | {w['train_edge_pct']}% | {w['train_n_signal']} |")
    lines += ["", "### 稳定性",
              f"- 有效折: {st['windows_with_edge']} | 正 edge 占比: **{st['positive_ratio_pct']}%**",
              f"- 平均 edge: {st['mean_edge_pct']}% | 标准差: {st['std_edge_pct']}%",
              f"- edge 区间: [{st['min_edge_pct']}%, {st['max_edge_pct']}%]", "",
              f"> {r['note']}"]
    return "\n".join(lines)


def to_markdown(r: dict) -> str:
    mc = r["monte_carlo"]
    oos = r["oos"]
    lines = [
        f"## P4 因子验证 — {r['factor']}",
        "",
        f"样本: {r['sample']} | 信号事件 {r['n_signal_events']} 个",
        f"信号后平均收益 {r['signal_mean_ret_pct']}% vs 基线 {r['baseline_mean_ret_pct']}% → **观测 edge {r['observed_edge_pct']}%**",
        "",
        "### 1) 分年度拆解（edge 是否每年都在）",
        "",
        "| 年份 | 信号数 | 信号收益 | 基线收益 | edge | 胜率 |",
        "|------|------|------|------|------|------|",
    ]
    for y in r["by_year"]:
        lines.append(
            f"| {y['year']} | {y['n_signal']} | {y['signal_ret_pct']}% | {y['baseline_ret_pct']}% | "
            f"{y['edge_pct']}% | {y['win_pct']}% |"
        )
    lines += [
        "",
        f"### 2) 样本外验证（中点 {oos['split_date']} 切分）",
        "",
        "| 区段 | 信号数 | 信号收益 | 基线收益 | edge | 胜率 |",
        "|------|------|------|------|------|------|",
        f"| 训练(前半) | {oos['train_first_half']['n_signal']} | {oos['train_first_half']['signal_ret_pct']}% | {oos['train_first_half']['baseline_ret_pct']}% | {oos['train_first_half']['edge_pct']}% | {oos['train_first_half']['win_pct']}% |",
        f"| 验证(后半) | {oos['test_second_half']['n_signal']} | {oos['test_second_half']['signal_ret_pct']}% | {oos['test_second_half']['baseline_ret_pct']}% | {oos['test_second_half']['edge_pct']}% | {oos['test_second_half']['win_pct']}% |",
        "",
        "### 3) 蒙特卡洛置换检验（随机抽同样多的天 5000 次）",
        "",
        f"- 观测信号均值位于随机分布的 **{mc['observed_percentile']} 百分位**",
        f"- p 值(随机 ≥ 观测的概率) = **{mc['p_value']}**",
        f"- 随机 edge 的 90% 区间: [{mc['random_edge_p5_pct']}%, {mc['random_edge_p95_pct']}%]",
        "",
        "> ⚠️ 注意：前瞻窗口重叠导致事件高度自相关，p 值会偏乐观；置换法对观测/随机两侧同样重叠，相对公平但仍非独立样本。",
        "> 数据真实拉取，非估算。仅评估统计稳健性，不构成交易建议。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 P4 因子验证 + T-05 WF/Purged K-Fold")
    ap.add_argument("--mode", choices=["legacy", "wf", "purged", "all"], default="legacy",
                    help="legacy=分年度+OOS+蒙卡(默认)；wf=滚动窗口；purged=Purged K-Fold；all=全部")
    ap.add_argument("--factor", choices=["drawdown", "breakout20"], default="drawdown",
                    help="wf/purged 验证因子：drawdown 回撤 / breakout20 C4新高突破")
    ap.add_argument("--threshold", type=float, default=-0.50, help="回撤阈值(仅 drawdown), 如 -0.30")
    ap.add_argument("--horizon", type=int, default=30, help="持有/前瞻天数")
    ap.add_argument("--windows", type=int, default=6, help="wf 滚动窗口数")
    ap.add_argument("--kfolds", type=int, default=5, help="purged K 折数")
    ap.add_argument("--embargo", type=int, default=5, help="purged embargo 天数")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    outs: list = []
    if args.mode in ("legacy", "all"):
        outs.append((validate(args.threshold, args.horizon), to_markdown))
    if args.mode in ("wf", "all"):
        outs.append((walk_forward(args.factor, args.threshold, args.horizon, args.windows),
                     wf_to_markdown))
    if args.mode in ("purged", "all"):
        outs.append((purged_kfold(args.factor, args.threshold, args.horizon,
                                   args.kfolds, args.embargo), purged_to_markdown))

    if args.json:
        print(json.dumps([r for r, _ in outs], ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(md(r) for r, md in outs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
