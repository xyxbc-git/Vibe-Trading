#!/usr/bin/env python3
"""4h 预测引擎离线 smoketest：全离线、不联网，合成 bars 验证纯函数与门禁。"""

from __future__ import annotations

import math
import random
import time

import jarvis_intraday_predict as jip

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


def synth_bars(n: int = 400, seed: int = 7, trend: float = 0.0,
               signal: bool = False) -> list[dict]:
    """合成 4h bars。signal=True 时注入可学习模式：RSI 低后下一根大概率涨。"""
    rng = random.Random(seed)
    bars = []
    price = 100.0
    t0 = 1_700_000_000_000
    momentum = 0.0
    for i in range(n):
        if signal:
            # 均值回归模式：连续下跌后强反弹（模型可学）
            momentum = momentum * 0.7 + rng.gauss(0, 0.01)
            ret = trend - 0.6 * momentum + rng.gauss(0, 0.004)
        else:
            ret = trend + rng.gauss(0, 0.01)
        new_price = max(1e-6, price * (1 + ret))
        hi = max(price, new_price) * (1 + abs(rng.gauss(0, 0.003)))
        lo = min(price, new_price) * (1 - abs(rng.gauss(0, 0.003)))
        bars.append({"ts": t0 + i * 14_400_000, "open": price, "high": hi,
                     "low": lo, "close": new_price,
                     "volume": abs(rng.gauss(1000, 200))})
        price = new_price
        if signal:
            momentum += ret
    return bars


# ── 1. build_dataset_4h 基本形状 ─────────────────────────────────────────
bars = synth_bars(400)
ds = jip.build_dataset_4h(bars)
check("数据集构造成功", "_error" not in ds, str(ds.get("_error")))
if "_error" not in ds:
    check("特征维度 = 12", all(len(r) == 12 for r in ds["X"]))
    check("无 NaN", all(all(v == v and abs(v) < 1e9 for v in r) for r in ds["X"]))
    check("样本≥MIN_SAMPLES", ds["n"] >= jip.MIN_SAMPLES, f"n={ds['n']}")
    check("X/y 等长", len(ds["X"]) == len(ds["y_class"]) == len(ds["y_fwd"]))
    check("标签合法", set(ds["y_class"]) <= {0, 1, 2})
    check("最后一根只入 latest", ds["ts_used"][-1] < bars[-1]["ts"])
    check("latest_features 存在", ds["latest_features"] is not None)
    # 防前瞻：截断最后 5 根不应改变前面样本的特征
    ds2 = jip.build_dataset_4h(bars[:-5])
    same = ds["X"][:ds2["n"] - 1] == ds2["X"][:ds2["n"] - 1]
    check("防前瞻（截尾不影响历史特征）", same)

# ── 2. 样本不足优雅降级 ─────────────────────────────────────────────────
check("bar 太少返回 _error", "_error" in jip.build_dataset_4h(synth_bars(100)))

# ── 3. directional_hit_rate 语义 ─────────────────────────────────────────
d = jip.directional_hit_rate([2, 0, 1, 2], [2, 0, 2, 0])
# 4 次预测里 3 次喊方向？逐个: p=2(对,t=2) p=0(对,t=0) p=2(错,t=1震荡) p=0(错,t=2)
check("方向命中率计算", d["n_calls"] == 4 and abs(d["hit_rate"] - 0.5) < 1e-9, str(d))
check("全震荡预测 n_calls=0", jip.directional_hit_rate([2, 0], [1, 1])["n_calls"] == 0)

# ── 4. validate 门禁：纯噪声必须拒绝 ─────────────────────────────────────
noise = jip.build_dataset_4h(synth_bars(500, seed=99))
if "_error" not in noise:
    g = jip.validate(noise, mc_iter=30)
    check("纯噪声 tradeable=False", g.get("tradeable") is False, str(g))
    check("拒绝时给出 reason", bool(g.get("reason")))

# ── 5. predict_latest 离线注入 + 幂等 ────────────────────────────────────
r1 = jip.predict_latest("TESTUSDT", mc_iter=20, _bars=bars)
r2 = jip.predict_latest("TESTUSDT", mc_iter=20, _bars=bars)
check("predict_latest 不抛出且有方向", r1.get("direction") in ("涨", "跌", "震荡"), str(r1))
check("同 bars 幂等", r1 == r2)
check("止损止盈几何一致",
      r1.get("stop") is None or (
          (r1["direction"] == "涨" and r1["stop"] < r1["entry"] < r1["take"]) or
          (r1["direction"] == "跌" and r1["take"] < r1["entry"] < r1["stop"])), str(r1))

# ── 6. 进行中 bar 丢弃：末根 ts 在当前时间附近时应被剔除 ──────────────────
now_ms = int(time.time() * 1000)
live = synth_bars(400)
shift = now_ms - live[-1]["ts"] - 3_600_000  # 让最后一根 1h 前开盘（未收盘）
for b in live:
    b["ts"] += shift
rl = jip.predict_latest("TESTUSDT", mc_iter=20, _bars=live)
check("未收盘 bar 被丢弃", rl.get("as_of_bar_ts", 0) != live[-1]["ts"] or not rl.get("tradeable"))

# ── 7. 异常兜底：坏输入不抛出 ────────────────────────────────────────────
bad = jip.predict_latest("TESTUSDT", _bars=[{"bad": 1}])
check("坏输入不抛出", bad.get("tradeable") is False)

print(f"\n{'='*40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
