#!/usr/bin/env python3
"""贾维斯 JARVIS — 成交层「振幅准入门槛」计算模块（开发计划 T8）。

回答的问题（裁决 6 的落地闸门）：**每个 system×tf 在 T1 纠偏口径下的毛价格位移，
能否跨过 2×单边费率 的往返过路费地板？** 振幅是低方差量，小样本即可定案；
它不问「方向对不对」（那是高方差量，需要千级样本，见裁决 5 的 84 年）。

纯函数模块：不连数据库、不读配置、无副作用。trades 由调用方
（jarvis_dashboard `GET /api/twelve/attribution`）查询后传入，便于单测与对账。

────────────────────── 口径预登记（对齐 T9 jarvis_signal_floor.py） ──────────────────────
A1 T1 纠偏      sl 单一律按计划止损位（stop_loss 列）结算重算毛位移——台账 exit_price
                被轮询结算伪影单边劣化（240 笔止损 134 笔劣化、0 笔优于计划位，
                纠偏前后 t 从 -3.77 掉到 -1.97，见计划 §裁决6）。stop_loss 为空的
                sl 单退回 exit_price 并计数曝光（corrected_missing）。
A2 地板         floor = 2 × 单边费率（twelve_sim_fee_pct），随费率动态走，不写死
                0.10%——换费率档位时栏杆必须跟着动（计划 §T8 门槛定义）。
A3 聚类         独立赌注 = 同 UTC 小时 × 同方向 归并（裁决 1 口径：433 笔 → 76 注；
                平均并发 17.3 仓压在同一标的上，逐笔口径会把显著性虚高数倍）。
                每格以赌注簇均值为观测单位求稳健标准误。
A4 多重比较     Bonferroni，除数 m = 本次运行中所有出具判定的 (system×tf) 单元格数；
                FAIL 与 PASS 同用调整后区间（双侧 α=0.05/m）——只校正 PASS 不校正
                FAIL 是反方向挑樱桃（T9 P7 的教训）。
A5 判定次序     终局结论优先于样本充足性（T9 P7）：
                1. 调整后 CI 上界 < 地板 → fail（不达标，终局）
                2. 调整后 CI 下界 > 地板 → pass（达标，终局）
                3. n < need             → insufficient（样本不足 → 展示「不可判定」）
                4. 其余                 → undecided（区间跨地板 → 展示「不可判定」）
A6 功效标注     每格给 80% 功效所需笔数 need_trades（备择 μ₁ = 1.5×地板，单侧
                α=0.05/m，按赌注簇 sd 推 need_bets 再乘 每注平均笔数 折回原始笔数）。
A7 观察名单     fail 格进 watch_list——**不自动停用**，停用决定留给用户（§T8 落地3）。
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats

PREREG_VERSION = "T8-prereg-v1"

PREREG = {
    "version": PREREG_VERSION,
    "A1_correction": "sl 单按计划止损位(stop_loss)重算；缺列退回 exit_price 并计数",
    "A2_floor": "地板 = 2 × 单边费率（动态，不写死 0.10%）",
    "A3_cluster": "独立赌注 = 同 UTC 小时 × 同方向；簇均值为观测单位",
    "A4_multiplicity": "Bonferroni，m = 出具判定的单元格数；FAIL/PASS 同用调整区间",
    "A5_order": "fail/pass（终局）优先于 insufficient/undecided",
    "A6_power": "80% 功效所需笔数；备择 μ₁ = 1.5×地板，单侧 α=0.05/m",
    "A7_watchlist": "不达标进观察名单，不自动停用",
}

POWER = 0.80
ALPHA_ONE_SIDED = 0.05
ALT_FLOOR_MULT = 1.5
MIN_BETS_FOR_TEST = 2      # 赌注簇 < 2 无法估计标准误，格子不出具判定

# toll_ratio 分档（=2×单边费率% ÷ 计划SL距离%），对齐计划 §三实测表的五档
# （SL距离 >1.0 / 0.5-1.0 / 0.2-0.5 / 0.1-0.2 / <0.1% 与地板 0.1% 时一一对应）
TOLL_BANDS = (("<0.1", 0.0, 0.1), ("0.1-0.2", 0.1, 0.2), ("0.2-0.5", 0.2, 0.5),
              ("0.5-1.0", 0.5, 1.0), (">1.0", 1.0, math.inf))

VERDICT_CN = {"pass": "达标", "fail": "不达标",
              "insufficient": "不可判定（样本不足）", "undecided": "不可判定（区间跨地板）"}


# ─────────────────────────── 单笔口径 ───────────────────────────

def corrected_move_pct(trade: dict) -> tuple[float | None, bool]:
    """单笔按预测方向的毛价格位移%（T1 纠偏口径，A1）。

    返回 (毛位移% | None, 是否做了 sl 纠偏)。entry 非法返回 (None, False)。
    """
    try:
        entry = float(trade["entry_price"])
        if not math.isfinite(entry) or entry <= 0:
            return None, False
    except (TypeError, ValueError, KeyError):
        return None, False
    corrected = False
    exit_eff = trade.get("exit_price")
    if str(trade.get("exit_reason")) == "sl" and trade.get("stop_loss") is not None:
        exit_eff = trade["stop_loss"]
        corrected = True
    try:
        exit_eff = float(exit_eff)
    except (TypeError, ValueError):
        return None, False
    sign = 1.0 if str(trade.get("direction")) == "long" else -1.0
    return 100.0 * sign * (exit_eff - entry) / entry, corrected


def naive_move_pct(trade: dict) -> float | None:
    """台账原值口径（exit_price，不纠偏）——仅作对照输出，不用于判定。"""
    try:
        entry = float(trade["entry_price"])
        exit_px = float(trade["exit_price"])
        if not math.isfinite(entry) or entry <= 0:
            return None
    except (TypeError, ValueError, KeyError):
        return None
    sign = 1.0 if str(trade.get("direction")) == "long" else -1.0
    return 100.0 * sign * (exit_px - entry) / entry


def toll_ratio_of(trade: dict, fee_side_pct: float) -> float | None:
    """单笔 toll_ratio = 2×单边费率% ÷ 计划SL距离%（与杠杆无关的不变量，T3 判据）。

    stop_loss 缺失 / entry 非法 → None（调用方归入 no_sl 桶显式曝光，不静默丢）。
    """
    try:
        entry = float(trade["entry_price"])
        sl = float(trade["stop_loss"])
        sl_dist_pct = abs(entry - sl) / entry * 100.0
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        return None
    if not math.isfinite(sl_dist_pct) or sl_dist_pct <= 0:
        return None
    return 2.0 * fee_side_pct / sl_dist_pct


def _artifact_cost(trade: dict) -> float:
    """sl 单被结算伪影劣化的金额（U，≥0）：|结算价-计划位|×qty，仅劣化方向计。"""
    if str(trade.get("exit_reason")) != "sl" or trade.get("stop_loss") is None:
        return 0.0
    try:
        sl = float(trade["stop_loss"])
        exit_px = float(trade["exit_price"])
        qty = float(trade["qty"])
    except (TypeError, ValueError, KeyError):
        return 0.0
    worse = (sl - exit_px) if str(trade.get("direction")) == "long" else (exit_px - sl)
    return max(0.0, worse * qty)


# ─────────────────────────── 统计（A3/A4/A5/A6） ───────────────────────────

def _bet_cluster(moves: np.ndarray, bet_keys: list) -> tuple[float, float, int, float]:
    """独立赌注聚类（A3）→ (均值, 标准误, 赌注数, 每注平均笔数)。"""
    groups: dict = {}
    for m, k in zip(moves, bet_keys):
        groups.setdefault(k, []).append(m)
    means = np.array([float(np.mean(v)) for v in groups.values()])
    n_bets = len(means)
    per_bet = len(moves) / n_bets if n_bets else 0.0
    if n_bets < 2:
        return (float(means.mean()) if n_bets else 0.0), float("inf"), n_bets, per_bet
    return (float(means.mean()),
            float(means.std(ddof=1) / math.sqrt(n_bets)), n_bets, per_bet)


def _required_trades(cluster_sd: float, floor: float, m_tested: int,
                     trades_per_bet: float) -> int:
    """80% 功效所需原始笔数（A6）：先推所需赌注数，再按每注平均笔数折回。"""
    delta = (ALT_FLOOR_MULT - 1.0) * floor
    if delta <= 0 or cluster_sd <= 0 or not math.isfinite(cluster_sd) \
            or trades_per_bet <= 0:
        return 0
    z = stats.norm.ppf(1 - ALPHA_ONE_SIDED / max(m_tested, 1)) + stats.norm.ppf(POWER)
    need_bets = math.ceil((z * cluster_sd / delta) ** 2)
    return math.ceil(need_bets * trades_per_bet)


def _evaluate_cell(system: str, tf: str, moves: np.ndarray, bet_keys: list,
                   floor: float, m_tested: int) -> dict:
    """对一个 system×tf 格做地板检验（A4 调整区间 + A5 判定次序 + A6 功效）。"""
    mean, se, n_bets, per_bet = _bet_cluster(moves, bet_keys)
    cluster_sd = se * math.sqrt(n_bets) if n_bets >= 2 else float("inf")
    need = _required_trades(cluster_sd, floor, m_tested, per_bet)
    crit = stats.t.ppf(1 - (0.05 / max(m_tested, 1)) / 2, max(n_bets - 1, 1))
    ci_lo, ci_hi = mean - crit * se, mean + crit * se
    if ci_hi < floor:
        status = "fail"
    elif ci_lo > floor:
        status = "pass"
    elif len(moves) < need:
        status = "insufficient"
    else:
        status = "undecided"
    finite = math.isfinite(se)
    return {
        "system": system, "tf": tf,
        "trades": int(len(moves)), "bets": int(n_bets),
        "gross_displacement_pct": round(mean, 4),
        "net_of_toll_pct": round(mean - floor, 4),
        "ci_lo": round(ci_lo, 4) if finite else None,
        "ci_hi": round(ci_hi, 4) if finite else None,
        "status": status, "verdict_cn": VERDICT_CN[status],
        "need_trades_80pct_power": need,
        # 样本不足时数值仅为过程量：结论以 verdict_cn 为准（A5），不得引用数字下结论
        "note": (None if status in ("pass", "fail")
                 else f"还差 {max(need - len(moves), 0)} 笔达到 80% 功效判定所需"),
    }


# ─────────────────────────── toll_ratio 分档（对齐计划 §三） ───────────────────────────

def toll_ratio_bands(trades: list[dict], fee_side_pct: float) -> dict:
    """按 toll_ratio 五档聚合（总额比口径，均值口径受极端单带偏，见计划 §一）。

    每档输出：笔数 / 胜率 / 净盈亏U / 双边费U / 过路费R / 净额R / 剔摩擦R。
    R = Σ|entry−stop_loss|×qty（该笔计划止损亏损额）；
    剔摩擦R = (Σpnl + Σ双边费 + Σ结算伪影) ÷ ΣR。
    stop_loss 缺失的笔归 no_sl 桶显式曝光。
    """
    bands = {label: {"toll_band": label, "trades": 0, "wins": 0, "net_pnl": 0.0,
                     "fee": 0.0, "risk_r_sum": 0.0, "artifact": 0.0}
             for label, _, _ in TOLL_BANDS}
    no_sl = 0
    for t in trades:
        ratio = toll_ratio_of(t, fee_side_pct)
        if ratio is None:
            no_sl += 1
            continue
        label = next(lb for lb, lo, hi in TOLL_BANDS if lo <= ratio < hi)
        b = bands[label]
        pnl = float(t.get("pnl") or 0.0)
        entry, exit_px = float(t["entry_price"]), float(t["exit_price"])
        qty = float(t["qty"])
        b["trades"] += 1
        b["wins"] += 1 if pnl > 0 else 0
        b["net_pnl"] += pnl
        b["fee"] += (entry + exit_px) * qty * fee_side_pct / 100.0
        b["risk_r_sum"] += abs(entry - float(t["stop_loss"])) * qty
        b["artifact"] += _artifact_cost(t)
    out = []
    for label, _, _ in TOLL_BANDS:
        b = bands[label]
        n, rsum = b["trades"], b["risk_r_sum"]
        out.append({
            "toll_band": label, "trades": n,
            "win_rate_pct": round(b["wins"] / n * 100.0, 2) if n else None,
            "net_pnl": round(b["net_pnl"], 4), "fee": round(b["fee"], 4),
            "toll_r": round(b["fee"] / rsum, 4) if rsum > 0 else None,
            "net_r": round(b["net_pnl"] / rsum, 4) if rsum > 0 else None,
            "ex_friction_r": (round((b["net_pnl"] + b["fee"] + b["artifact"]) / rsum, 4)
                              if rsum > 0 else None),
        })
    return {"bands": out, "no_sl_trades": no_sl,
            "note": "总额比口径（Σ分子/Σ分母）；toll_ratio=2×单边费率%÷计划SL距离%；"
                    "剔摩擦R=(净盈亏+双边费+sl结算伪影)/ΣR"}


# ─────────────────────────── 主入口 ───────────────────────────

def amplitude_report(trades: list[dict], fee_side_pct: float) -> dict:
    """T8 振幅准入门槛主报告（纯函数）。

    trades 需含列：system/tf/direction/entry_price/exit_price/stop_loss/
    exit_reason/entry_ts/qty/pnl（twelve_sim_trade 已平仓行）。
    """
    floor = 2.0 * float(fee_side_pct)
    usable: list[tuple[dict, float, bool]] = []
    n_corrected = corrected_missing = skipped = 0
    for t in trades:
        mv, corr = corrected_move_pct(t)
        if mv is None:
            skipped += 1
            continue
        if corr:
            n_corrected += 1
        elif str(t.get("exit_reason")) == "sl":
            corrected_missing += 1      # sl 单但 stop_loss 缺失，纠偏不可用，曝光
        usable.append((t, mv, corr))

    def _bet_key(t: dict):
        return (int(float(t.get("entry_ts") or 0.0) // 3600.0),
                str(t.get("direction")))

    # 全局口径（对账锚点：计划 §一 毛位移三口径）
    all_moves = np.array([mv for _, mv, _ in usable]) if usable else np.array([])
    all_keys = [_bet_key(t) for t, _, _ in usable]
    naive_vals = [naive_move_pct(t) for t, _, _ in usable]
    naive_moves = np.array([v for v in naive_vals if v is not None])
    summary: dict = {
        "trades": len(usable), "skipped_bad_rows": skipped,
        "sl_corrected": n_corrected, "sl_correction_missing": corrected_missing,
    }
    if len(usable) >= 2:
        g_mean, g_se, g_bets, _ = _bet_cluster(all_moves, all_keys)
        summary.update({
            "independent_bets": g_bets,
            "gross_displacement_pct": round(float(all_moves.mean()), 4),
            "gross_displacement_bet_pct": round(g_mean, 4),
            "gross_displacement_naive_pct": (round(float(naive_moves.mean()), 4)
                                             if len(naive_moves) else None),
            "net_of_toll_pct": round(float(all_moves.mean()) - floor, 4),
            "t_vs_floor_bet": (round((g_mean - floor) / g_se, 2)
                               if math.isfinite(g_se) and g_se > 0 else None),
        })

    # 每格判定：m 取全部参检格子数（A4——每个出具判定的格子都在做声明）
    cells_raw: dict[tuple[str, str], list[tuple[dict, float]]] = {}
    for t, mv, _ in usable:
        cells_raw.setdefault((str(t.get("system")), str(t.get("tf"))), []).append((t, mv))
    testable = {k: v for k, v in cells_raw.items()
                if len({_bet_key(t) for t, _ in v}) >= MIN_BETS_FOR_TEST}
    m = len(testable)
    cells = []
    for (system, tf), pairs in sorted(testable.items()):
        moves = np.array([mv for _, mv in pairs])
        keys = [_bet_key(t) for t, _ in pairs]
        cells.append(_evaluate_cell(system, tf, moves, keys, floor, m))
    # 赌注数不足 2 的格子如实曝光为不可判定（不进 Bonferroni 除数）
    for (system, tf), pairs in sorted(cells_raw.items()):
        if (system, tf) in testable:
            continue
        cells.append({
            "system": system, "tf": tf, "trades": len(pairs),
            "bets": len({_bet_key(t) for t, _ in pairs}),
            "gross_displacement_pct": None, "net_of_toll_pct": None,
            "ci_lo": None, "ci_hi": None,
            "status": "insufficient",
            "verdict_cn": VERDICT_CN["insufficient"],
            "need_trades_80pct_power": None,
            "note": "独立赌注 < 2，无法估计标准误",
        })
    cells.sort(key=lambda c: (c["system"], c["tf"]))

    tally = {s: sum(1 for c in cells if c["status"] == s)
             for s in ("pass", "fail", "insufficient", "undecided")}
    watch_list = [{"system": c["system"], "tf": c["tf"],
                   "gross_displacement_pct": c["gross_displacement_pct"],
                   "net_of_toll_pct": c["net_of_toll_pct"]}
                  for c in cells if c["status"] == "fail"]
    return {
        "prereg": PREREG,
        "floor_pct": round(floor, 4), "fee_pct_per_side": float(fee_side_pct),
        "bonferroni_m": m,
        "summary": summary,
        "cells": cells,
        "tally": tally,
        "watch_list": watch_list,
        "watch_list_note": "不达标（fail）仅进观察名单，不自动停用——停用决定留给用户（§T8）",
        "toll_ratio": toll_ratio_bands([t for t, _, _ in usable], float(fee_side_pct)),
    }
