#!/usr/bin/env python3
"""中长线多周期预测头（jarvis_horizon）离线 smoketest：合成日线数据，不联网。"""

from __future__ import annotations

import random
from datetime import date, timedelta

import jarvis_horizon as jh

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name} {extra}")


def synth_daily(n: int = 600, seed: int = 7, trend: float = 0.0) -> tuple:
    """合成 (dates, prices, fng, funding)，与 fetch_* 输出结构一致。"""
    rng = random.Random(seed)
    d0 = date(2023, 1, 1)
    dates, prices, fng, funding = [], {}, {}, {}
    price = 100.0
    for i in range(n):
        d = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        price = max(1e-6, price * (1 + trend + rng.gauss(0, 0.02)))
        dates.append(d)
        prices[d] = price
        fng[d] = rng.randint(10, 90)
        funding[d] = rng.gauss(0, 1e-4)
    return dates, prices, fng, funding


# ── 1. 阈值随周期缩放 ────────────────────────────────────────────────────
check("15天阈值≈4.39%", abs(jh._range_thresh(15) - 4.39) < 0.02, str(jh._range_thresh(15)))
check("30天阈值≈6.21%", abs(jh._range_thresh(30) - 6.21) < 0.02, str(jh._range_thresh(30)))
check("阈值单调递增", jh._range_thresh(30) > jh._range_thresh(15) > jh._range_thresh(7))

# ── 2. predict_horizon 离线注入：结构完整 + 永不抛出 ─────────────────────
data = synth_daily(600)
r15 = jh.predict_horizon("TESTUSDT", 15, mc_iter=10, _data=data)
check("15天预测不抛出", isinstance(r15, dict), str(r15)[:120])
check("方向合法", r15.get("direction") in ("涨", "跌", "震荡") or not r15.get("tradeable"), str(r15)[:120])
if r15.get("target") is not None:
    check("目标区间有序 lo≤mid≤hi",
          r15["target_lo"] <= r15["target"] <= r15["target_hi"], str(r15)[:200])
    check("entry 为最新价", abs(r15["entry"] - data[1][data[0][-1]]) < 1e-9)
check("含门禁字段", all(k in r15 for k in ("tradeable", "oos_hit_rate", "p_value")), str(r15.keys()))

# ── 3. 纯噪声必须过不了门禁（p 大或命中率低） ────────────────────────────
check("纯噪声 tradeable=False", r15.get("tradeable") is False, str(r15)[:200])

# ── 4. 30 天周期同样可跑 ─────────────────────────────────────────────────
r30 = jh.predict_horizon("TESTUSDT", 30, mc_iter=10, _data=data)
check("30天预测不抛出", isinstance(r30, dict) and "tradeable" in r30, str(r30)[:120])

# ── 5. 数据不足优雅降级 ─────────────────────────────────────────────────
short = synth_daily(100)
rs = jh.predict_horizon("TESTUSDT", 15, mc_iter=10, _data=short)
check("数据不足返回 reason", rs.get("tradeable") is False and "数据不足" in rs.get("reason", ""), str(rs))

# ── 6. 幂等：同数据两次结果一致 ─────────────────────────────────────────
r15b = jh.predict_horizon("TESTUSDT", 15, mc_iter=10, _data=data)
check("同数据幂等", r15 == r15b)

# ── 7. 坏输入不抛出 ─────────────────────────────────────────────────────
bad = jh.predict_horizon("TESTUSDT", 15, _data=([], {}, {}, {}))
check("坏输入不抛出", bad.get("tradeable") is False)

print(f"\n{'='*40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
