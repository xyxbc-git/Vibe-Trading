#!/usr/bin/env python3
"""SMC 结构检测冒烟（R8 追加）：BOS/CHoCH 语义 + 收盘口径 + 截断 + 容错。

不联网：合成 K 线（明确的上升结构→转跌场景）直调 detect_structure。
"""

from __future__ import annotations

import pandas as pd

import jarvis_smc_structure as smc

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def df_from(closes: list[float], wick: float = 0.4) -> pd.DataFrame:
    """close 序列 → OHLC df（高低点 = close±wick，无长影线）。"""
    t0 = 1_700_000_000_000
    return pd.DataFrame([
        {"time": t0 + i * 900_000, "open": c, "high": c + wick,
         "low": c - wick, "close": c, "volume": 1.0}
        for i, c in enumerate(closes)
    ])


def stairs_up_then_break(n_legs: int = 3) -> list[float]:
    """阶梯上升（swing 高低点逐级抬高）→ 末段放量跌破最近 higher-low。"""
    closes: list[float] = []
    base = 100.0
    for leg in range(n_legs):
        # 上冲 8 根
        for k in range(8):
            closes.append(base + k * 1.0)
        peak = base + 7.0
        # 回撤 6 根（higher-low 于 peak-3 附近）
        for k in range(6):
            closes.append(peak - k * 0.5)
        base = peak - 2.0
    # 转跌段：连续下杀击穿最近 swing low
    last = closes[-1]
    for k in range(1, 12):
        closes.append(last - k * 1.2)
    return closes


# ── 1) 上升结构中的 BOS + 转跌 CHoCH ────────────────────────────────
df = df_from(stairs_up_then_break())
out = smc.detect_structure(df, window=3, max_events=20)
check("detect ok", out["ok"] is True, str(out.get("reason")))
events = out["events"]
kinds = [(e["kind"], e["direction"]) for e in events]
check("检出事件", len(events) >= 2, f"n={len(events)} kinds={kinds}")
check("含看涨 BOS（趋势延续确认）", ("bos", "bullish") in kinds)
check("转跌首个逆结构突破= CHoCH", ("choch", "bearish") in kinds,
      f"kinds={kinds}")
first_bear = next((e for e in events if e["direction"] == "bearish"), None)
check("首个看跌事件是 CHoCH 而非 BOS", first_bear is not None and first_bear["kind"] == "choch")
check("扫描结束结构方向=down", out["direction"] == "down")
check("事件字段齐全", all({"kind", "direction", "level", "swing_i", "break_i"} <= set(e) for e in events))
check("break_i 在 swing_i 之后", all(e["break_i"] > e["swing_i"] for e in events))

# ── 2) 只认收盘突破：影线触碰 swing 高不触发 ────────────────────────
flat = [100.0] * 40
df_wick = df_from(flat, wick=0.4)
# 中段一根长上影线（high 远超前 swing 高，但收盘不破）
df_wick.loc[30, "high"] = 130.0
out2 = smc.detect_structure(df_wick, window=3)
check("影线假突破不触发事件", all(e["break_i"] != 30 for e in out2["events"]),
      f"events={out2['events']}")

# ── 3) max_events 截断 + 数据不足容错 ───────────────────────────────
out3 = smc.detect_structure(df, window=3, max_events=2)
check("max_events 截断保留最近", len(out3["events"]) == 2
      and out3["events"] == events[-2:])
out4 = smc.detect_structure(df_from([100.0] * 10))
check("数据不足 → ok:False 不抛", out4["ok"] is False and out4["events"] == [])
out5 = smc.detect_structure(None)  # type: ignore[arg-type]
check("None 输入容错", out5["ok"] is False)

print()
if _FAILED:
    print(f"❌ {len(_FAILED)} 项失败: {_FAILED}")
    raise SystemExit(1)
print("✅ SMC 结构检测冒烟全部通过")
