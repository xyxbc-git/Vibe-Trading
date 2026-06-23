#!/usr/bin/env python3
"""jarvis_retrain 重训引擎离线单测（合成数据，不联网、不读真实 DB）。

验证三重护栏与调权方向：
  - 样本门槛 min_n
  - OOS 同号一致性（防过拟合，P4 教训）
  - 步长 + 边界双夹
  - 正/负向因子的调权方向正确

跑法：python _retrain_smoketest.py
"""

from __future__ import annotations

import sys

import jarvis_retrain as jr
import jarvis_weights as jw

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {('— ' + detail) if detail else ''}")


def _row(date, fng, above, dd30, ret):
    return {"as_of_date": date, "fng": fng, "above_ma200": above, "dd30_active": dd30, "fwd_ret_pct": ret}


def test_factor_active():
    print("1) factor_active 因子触发重建")
    s = _row("2020-01-01", 10, 0, 1, 5.0)
    check("dd30_dip 触发", jr.factor_active(s, "dd30_dip") is True)
    check("fear_in_downtrend 触发(fng<20 & 价<200MA)", jr.factor_active(s, "fear_in_downtrend") is True)
    check("fear_in_uptrend 不触发", jr.factor_active(s, "fear_in_uptrend") is False)
    check("ma200_below 触发", jr.factor_active(s, "ma200_below") is True)
    check("ma200_above 不触发", jr.factor_active(s, "ma200_above") is False)
    s2 = _row("2020-01-01", 50, 1, 0, 1.0)
    check("fng=50 时 fear 因子不触发", jr.factor_active(s2, "fear_in_downtrend") is False)
    check("fng 缺失→fear 因子 None", jr.factor_active({"above_ma200": 1}, "fear_in_downtrend") is None)


def test_min_n_guard():
    print("2) 样本门槛：触发样本不足不调")
    # 只有 3 条 dd30 触发（且收益高），但 min_n=8 → 不采纳
    rows = [_row(f"2020-01-{i:02d}", 50, 1, 1, 10.0) for i in range(1, 4)]
    rows += [_row(f"2020-02-{i:02d}", 50, 1, 0, 0.0) for i in range(1, 12)]
    props = {p["factor"]: p for p in jr.propose(rows, min_n=8)}
    check("dd30_dip 因样本不足未采纳", props["dd30_dip"]["adopted"] is False)
    check("理由含『样本』", "样本" in props["dd30_dip"]["reason"])


def test_oos_inconsistency_blocks():
    print("3) OOS 不一致：训练段正、验证段负 → 不采纳")
    # 前半段 dd30 触发收益高(+10)，后半段 dd30 触发收益负(-10) → full 可能≈0或正，但 OOS 非同号
    rows = []
    for i in range(1, 11):  # train half: dd30 触发 → +10
        rows.append(_row(f"2020-01-{i:02d}", 50, 1, 1, 10.0))
        rows.append(_row(f"2020-01-{i:02d}b", 50, 1, 0, 0.0))
    for i in range(1, 11):  # test half: dd30 触发 → -10
        rows.append(_row(f"2020-12-{i:02d}", 50, 1, 1, -10.0))
        rows.append(_row(f"2020-12-{i:02d}b", 50, 1, 0, 0.0))
    props = {p["factor"]: p for p in jr.propose(rows, min_n=8)}
    check("dd30_dip OOS 不一致未采纳", props["dd30_dip"]["adopted"] is False)
    check("理由含『OOS』", "OOS" in props["dd30_dip"]["reason"])


def test_positive_factor_strengthen():
    print("4) 正向因子稳定有效 → 权重上调（OOS 同号）")
    rows = []
    # 全程 dd30 触发都比未触发收益高(+6 vs 0)，训练/验证段一致
    for i in range(1, 16):
        rows.append(_row(f"2020-01-{i:02d}", 50, 1, 1, 6.0))
        rows.append(_row(f"2020-01-{i:02d}b", 50, 1, 0, 0.0))
    for i in range(1, 16):
        rows.append(_row(f"2020-12-{i:02d}", 50, 1, 1, 6.0))
        rows.append(_row(f"2020-12-{i:02d}b", 50, 1, 0, 0.0))
    props = {p["factor"]: p for p in jr.propose(rows, min_n=8, learning_rate=0.05, max_step=0.1)}
    p = props["dd30_dip"]
    check("dd30_dip 被采纳", p["adopted"] is True, p["reason"])
    check("权重上调（delta>0）", p["delta"] > 0, str(p["delta"]))
    check("不超过 max_step", abs(p["delta"]) <= 0.1 + 1e-9)
    check("新权重在 bounds 内", jw.WEIGHT_BOUNDS["dd30_dip"][0] <= p["proposed_weight"] <= jw.WEIGHT_BOUNDS["dd30_dip"][1])


def test_negative_factor_strengthen():
    print("5) 负向因子触发后确实偏弱 → 负权重增强（更负）")
    rows = []
    # ma200_below 触发(价<200MA)时收益为负(-5)，未触发(价>200MA)为正(+2)；负向因子意图正确
    for i in range(1, 16):
        rows.append(_row(f"2020-01-{i:02d}", 50, 0, 0, -5.0))   # below MA → 跌
        rows.append(_row(f"2020-01-{i:02d}b", 50, 1, 0, 2.0))   # above MA → 涨
    for i in range(1, 16):
        rows.append(_row(f"2020-12-{i:02d}", 50, 0, 0, -5.0))
        rows.append(_row(f"2020-12-{i:02d}b", 50, 1, 0, 2.0))
    props = {p["factor"]: p for p in jr.propose(rows, min_n=8)}
    pb = props["ma200_below"]
    check("ma200_below 被采纳", pb["adopted"] is True, pb["reason"])
    check("负向因子更负（delta<0）", pb["delta"] < 0, str(pb["delta"]))
    check("仍在 bounds 内(≥下界)", pb["proposed_weight"] >= jw.WEIGHT_BOUNDS["ma200_below"][0])


def test_empty_rows():
    print("6) 空样本：全不采纳，不抛出")
    props = jr.propose([], min_n=8)
    check("空样本返回全因子建议", len(props) == len(jw.PRICE_ONLY_FACTORS))
    check("空样本无任何采纳", all(not p["adopted"] for p in props))


def main() -> int:
    print("=" * 60)
    print("jarvis_retrain 重训引擎离线单测")
    print("=" * 60)
    test_factor_active()
    test_min_n_guard()
    test_oos_inconsistency_blocks()
    test_positive_factor_strengthen()
    test_negative_factor_strengthen()
    test_empty_rows()
    print("-" * 60)
    print(f"结果：{PASS} PASS / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
