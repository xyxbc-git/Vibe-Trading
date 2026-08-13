#!/usr/bin/env python3
"""面板口径诚实化冒烟：bar_closed 判定 + 变更历史只记已收盘信号（幽灵拦截）。

不联网：合成 df + 打桩 analyze 验证 _last_bar_closed / _closed_signals_for_record
双路口径；临时 SQLite 端到端验证「盘中幽灵信号不落 twelve_signal_changes」。
"""

from __future__ import annotations

import os
import tempfile
import time
import types

import pandas as pd

# 先改 DB 路径再 import 业务函数（模块级常量在 import 时定型）
_TMP = tempfile.mkdtemp(prefix="jarvis_panel_honesty_")
import jarvis_signal_history as jsh  # noqa: E402

jsh.DB_PATH = os.path.join(_TMP, "test.db")
jsh._INITED = False

import jarvis_dashboard as jd  # noqa: E402

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def synth_df(n: int = 300, iv_ms: int = 900_000, last_closed: bool = False) -> pd.DataFrame:
    """合成 K 线 df（列结构与 jts.fetch_klines_df 一致，time=open time 毫秒）。"""
    now_ms = time.time() * 1000
    # 已收盘：open + iv ≤ now；未收盘：open = now - iv/2（bar 进行到一半）
    last_open = now_ms - (iv_ms * 1.5 if last_closed else iv_ms * 0.5)
    times = [last_open - iv_ms * (n - 1 - i) for i in range(n)]
    return pd.DataFrame({
        "time": times,
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.0 + i * 0.01 for i in range(n)],
        "volume": [1.0] * n,
    })


def mk_sig(direction: str) -> list[dict]:
    return [{"system": "turtle", "name_cn": "海龟交易", "direction": direction,
             "strength": 0.6, "reasoning": "冒烟", "key_levels": [],
             "trade_plan": None}]


# ── 1) _last_bar_closed：close_time 语义判定 ────────────────────────
check("末根已收盘→True", jd._last_bar_closed(synth_df(last_closed=True), "15m") is True)
check("末根未收盘→False", jd._last_bar_closed(synth_df(last_closed=False), "15m") is False)
check("df异常→False(保守示警)", jd._last_bar_closed(pd.DataFrame({"x": [1]}), "15m") is False)
check("未知周期→False(保守示警)", jd._last_bar_closed(synth_df(last_closed=True), "2h") is False)

# ── 2) _closed_signals_for_record：双路取数口径 ─────────────────────
calls: list[int] = []
stub = types.SimpleNamespace(analyze=lambda df, basis_data=None: (
    calls.append(len(df)) or {"signals": mk_sig("neutral"), "consensus": None}))

df = synth_df(last_closed=False)
live_out = {"signals": mk_sig("bullish"), "consensus": None}  # 实时口径：幽灵 bullish
live_px = round(float(df["close"].iloc[-1]), 6)

rec, px = jd._closed_signals_for_record(stub, df, "15m", None, live_out, live_px, True)
check("已收盘→复用实时信号零开销", rec is live_out["signals"] and px == live_px and calls == [])

rec, px = jd._closed_signals_for_record(stub, df, "15m", None, live_out, live_px, False)
check("未收盘→裁进行中bar重算", calls == [299], f"calls={calls}")
check("未收盘→入库用已收盘口径", rec is not None and rec[0]["direction"] == "neutral")
check("未收盘→价格取已收盘末根", px == round(float(df["close"].iloc[-2]), 6))

rec, _ = jd._closed_signals_for_record(stub, synth_df(n=30), "15m", None,
                                       live_out, live_px, False)
check("裁后不足30根→None跳过落库", rec is None)

# ── 3) 端到端幽灵拦截：盘中重绘不污染 twelve_signal_changes ──────────
# 旧逻辑：实时 bullish（插针幽灵）→ 收盘回 neutral 会写 2 条 changes 流水。
# 新逻辑：端点只把 _closed_signals_for_record 的返回交给 record_batch。
T0 = time.time()
ROUNDS = (
    ("bullish", "neutral"),   # 盘中插针：幽灵 bullish / 收盘口径 neutral
    ("neutral", "neutral"),   # 收盘回落：幽灵消失
    ("bearish", "neutral"),   # 再来一根反向插针
)
for i, (live_dir, closed_dir) in enumerate(ROUNDS):
    stub_i = types.SimpleNamespace(analyze=lambda df, basis_data=None, d=closed_dir: (
        {"signals": mk_sig(d), "consensus": None}))
    rec, rec_px = jd._closed_signals_for_record(
        stub_i, df, "15m", None, {"signals": mk_sig(live_dir), "consensus": None},
        live_px, False)
    if rec is not None:
        jsh.record_batch("SMOKEUSDT", "15m", rec, price=rec_px, now=T0 + i * 60)

hist = jsh.history("SMOKEUSDT", "15m")
st = jsh.state("SMOKEUSDT", "15m")
row = (st.get("rows") or [{}])[0]
check("三轮幽灵重绘→变更流水为0", hist["total"] == 0, f"total={hist['total']}")
check("state只承认已收盘口径", row.get("direction") == "neutral",
      f"direction={row.get('direction')}")

print()
if _FAILED:
    print(f"❌ {len(_FAILED)} 项失败: {_FAILED}")
    raise SystemExit(1)
print("✅ 面板口径诚实化冒烟全部通过")
