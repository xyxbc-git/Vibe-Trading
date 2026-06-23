#!/usr/bin/env python3
"""jarvis_weights 地基回归单测（离线、零依赖、不联网、不碰真实配置）。

核心保证：把权重外置后，「配置缺失」时的决策口径与改造前硬编码**完全一致**，
并验证 load/save/reset/clamp/diff 的关键不变量。

跑法：python _weights_smoketest.py
"""

from __future__ import annotations

import os
import sys
import tempfile

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


# ── 改造前 jarvis_journal._price_only_decision 的原始硬编码副本（黄金参照）──────
def _legacy_price_only(dd: float, fng_val, above_ma200: bool):
    score = 0.0
    if dd <= -0.30:
        score += 0.5
    if fng_val is not None and fng_val < 20:
        if not above_ma200:
            score += 0.6
        else:
            score -= 0.3
    if above_ma200:
        score += 0.3
    else:
        score -= 0.2
    score = round(max(-2.0, min(2.0, score)), 2)
    if score >= 0.8:
        direction, pos = "偏多（战术）", min(0.4, 0.2 + score * 0.1)
    elif score <= -0.8:
        direction, pos = "偏空/观望", 0.0
    else:
        direction, pos = "中性观望", 0.1 if score > 0 else 0.0
    return score, direction, round(pos * 100, 0)


def _new_price_only_via_weights(dd, fng_val, above_ma200):
    """用 weights 默认值复算（等价于 journal 改造后逻辑）。"""
    W = jw.DEFAULT_WEIGHTS
    TH = jw.DEFAULT_THRESHOLDS
    score = 0.0
    if dd <= -0.30:
        score += W["dd30_dip"]
    if fng_val is not None and fng_val < 20:
        if not above_ma200:
            score += W["fear_in_downtrend"]
        else:
            score += W["fear_in_uptrend"]
    score += W["ma200_above"] if above_ma200 else W["ma200_below"]
    score = round(max(-2.0, min(2.0, score)), 2)
    if score >= TH["long"]:
        direction, pos = "偏多（战术）", min(0.4, 0.2 + score * 0.1)
    elif score <= TH["short"]:
        direction, pos = "偏空/观望", 0.0
    else:
        direction, pos = "中性观望", 0.1 if score > 0 else 0.0
    return score, direction, round(pos * 100, 0)


def test_zero_regression_grid():
    print("1) 默认权重决策口径 == 改造前硬编码（网格穷举）")
    mismatches = 0
    for dd in (-0.10, -0.30, -0.45, -0.55):
        for fng in (None, 5, 19, 20, 50, 85):
            for above in (True, False):
                legacy = _legacy_price_only(dd, fng, above)
                new = _new_price_only_via_weights(dd, fng, above)
                if legacy != new:
                    mismatches += 1
                    print(f"     mismatch dd={dd} fng={fng} above={above}: {legacy} vs {new}")
    check("全网格 4×6×2=48 组决策完全一致", mismatches == 0, f"{mismatches} 处不一致")


def test_default_values():
    print("2) 内置默认 == 文档锚定的硬编码原值")
    W = jw.DEFAULT_WEIGHTS
    check("dd30_dip=0.5", W["dd30_dip"] == 0.5)
    check("fear_in_downtrend=0.6", W["fear_in_downtrend"] == 0.6)
    check("fear_in_uptrend=-0.3", W["fear_in_uptrend"] == -0.3)
    check("funding_overheated_short=0.4", W["funding_overheated_short"] == 0.4)
    check("funding_overheated_long=-0.4", W["funding_overheated_long"] == -0.4)
    check("ma200_above=0.3", W["ma200_above"] == 0.3)
    check("ma200_below=-0.2", W["ma200_below"] == -0.2)
    check("breakout_20d=0.6", W["breakout_20d"] == 0.6)
    check("阈值 long=0.8/short=-0.8",
          jw.DEFAULT_THRESHOLDS["long"] == 0.8 and jw.DEFAULT_THRESHOLDS["short"] == -0.8)


def test_load_missing_returns_default():
    print("3) 配置缺失/损坏 → 回退默认，永不抛出")
    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, "nope.json")
        cfg = jw.load(missing)
        check("缺失文件 load 返回默认权重", cfg["weights"] == jw.DEFAULT_WEIGHTS)
        check("缺失文件 source=builtin-default", cfg["meta"]["source"] == "builtin-default")
        bad = os.path.join(d, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{ this is not json ]]]")
        cfg2 = jw.load(bad)
        check("损坏 JSON 回退默认", cfg2["weights"] == jw.DEFAULT_WEIGHTS)
        check("损坏 JSON source 标记 load-error", "load-error" in cfg2["meta"]["source"])


def test_save_load_roundtrip_and_clamp():
    print("4) save→load 往返 + 护栏夹取 + version 累加")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "w.json")
        cfg1 = jw.save({"dd30_dip": 0.7}, source="test", note="bump", path=p)
        check("version 0→1", cfg1["meta"]["version"] == 1)
        check("dd30_dip 落盘=0.7", cfg1["weights"]["dd30_dip"] == 0.7)
        check("未触及的键保持默认", cfg1["weights"]["breakout_20d"] == 0.6)
        loaded = jw.load(p)
        check("load 读回 0.7", loaded["weights"]["dd30_dip"] == 0.7)
        check("load source=test", loaded["meta"]["source"] == "test")
        # 越界夹取：dd30_dip 上界 1.2
        cfg2 = jw.save({"dd30_dip": 99.0}, source="test", path=p)
        check("越界 99→夹到上界 1.2", cfg2["weights"]["dd30_dip"] == 1.2)
        check("version 累加到 2", cfg2["meta"]["version"] == 2)
        # 反号护栏：fear_in_uptrend 是负向因子，上界 0.0
        cfg3 = jw.save({"fear_in_uptrend": 0.9}, source="test", path=p)
        check("负向因子被夹到 ≤0", cfg3["weights"]["fear_in_uptrend"] == 0.0)


def test_reset_and_diff():
    print("5) reset 恢复默认 + diff 只列变化")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "w.json")
        jw.save({"dd30_dip": 0.9}, source="test", path=p)
        dif = jw.diff_from_default(p)
        check("diff 捕获 dd30_dip 变化", "dd30_dip" in dif["weights"])
        check("diff delta=+0.4", dif["weights"]["dd30_dip"]["delta"] == 0.4)
        check("diff 不列未变因子", "breakout_20d" not in dif["weights"])
        removed = jw.reset(p)
        check("reset 删除文件返回 True", removed is True)
        check("reset 后 load 回默认", jw.load(p)["weights"] == jw.DEFAULT_WEIGHTS)
        check("再次 reset 返回 False（已无文件）", jw.reset(p) is False)


def main() -> int:
    print("=" * 60)
    print("jarvis_weights 地基回归单测")
    print("=" * 60)
    test_zero_regression_grid()
    test_default_values()
    test_load_missing_returns_default()
    test_save_load_roundtrip_and_clamp()
    test_reset_and_diff()
    print("-" * 60)
    print(f"结果：{PASS} PASS / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
