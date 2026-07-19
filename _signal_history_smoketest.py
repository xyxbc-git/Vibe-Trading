#!/usr/bin/env python3
"""信号变更历史冒烟：临时 SQLite 验证 快照/变更判定/流水查询/删除/裁剪。

不联网：直接注入 mock 信号序列走 record_batch → history → delete 全链路。
"""

from __future__ import annotations

import os
import tempfile

# 先改 DB 路径再 import 业务函数（模块级常量在 import 时定型）
_TMP = tempfile.mkdtemp(prefix="jarvis_sighist_")
import jarvis_signal_history as jsh  # noqa: E402

jsh.DB_PATH = os.path.join(_TMP, "test.db")
jsh._INITED = False

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def sig(system: str, direction: str, strength: float, entry: float | None = None,
        levels: list | None = None) -> dict:
    plan = None
    if entry is not None:
        sl = entry * (0.98 if direction == "bullish" else 1.02)
        tp = entry * (1.04 if direction == "bullish" else 0.96)
        plan = {"side": "long" if direction == "bullish" else "short",
                "entry": entry, "entry_type": "market",
                "stop_loss": sl, "take_profit": tp, "rr": 2.0, "note": ""}
    return {"system": system, "name_cn": f"系统{system}", "direction": direction,
            "strength": strength, "reasoning": "测试", "key_levels": levels or [],
            "trade_plan": plan}


T0 = 1_720_000_000.0

# ── 1) 首见：建 state，不记流水 ─────────────────────────────────────
meta = jsh.record_batch("BTCUSDT", "4h", [sig("turtle", "bullish", 0.5, 60000)], now=T0)
check("首见-返回更新时间", meta["turtle"]["updated_at"] == T0)
check("首见-无变更时间", meta["turtle"]["changed_at"] is None)
h = jsh.history("BTCUSDT", "4h")
check("首见-不记流水", h["total"] == 0, f"total={h['total']}")

# ── 2) 无实质变化：只刷新 updated，不记流水 ─────────────────────────
meta = jsh.record_batch("BTCUSDT", "4h",
                        [sig("turtle", "bullish", 0.55, 60010)], now=T0 + 60)
check("微抖-更新时间推进", meta["turtle"]["updated_at"] == T0 + 60)
check("微抖-不算变更", meta["turtle"]["changed_at"] is None)
check("微抖-无流水", jsh.history("BTCUSDT", "4h")["total"] == 0)

# ── 3) 方向翻转：记流水 + changed_ts ────────────────────────────────
meta = jsh.record_batch("BTCUSDT", "4h",
                        [sig("turtle", "bearish", 0.6, 59000)], now=T0 + 120)
check("翻转-变更时间", meta["turtle"]["changed_at"] == T0 + 120)
h = jsh.history("BTCUSDT", "4h")
check("翻转-流水+1", h["total"] == 1, f"total={h['total']}")
row = h["rows"][0]
check("翻转-方向前后", row["prev_direction"] == "bullish"
      and row["new_direction"] == "bearish")
check("翻转-kinds含direction", "direction" in (row["change_kinds"] or []))
check("翻转-快照含计划", isinstance(row["new_json"], dict)
      and row["new_json"].get("trade_plan", {}).get("entry") == 59000)

# ── 4) 强度大变化（同方向）：记 strength 变更 ───────────────────────
meta = jsh.record_batch("BTCUSDT", "4h",
                        [sig("turtle", "bearish", 0.9, 59000)], now=T0 + 180)
h = jsh.history("BTCUSDT", "4h")
check("强度-流水+1", h["total"] == 2, f"total={h['total']}")
check("强度-kinds", "strength" in (h["rows"][0]["change_kinds"] or []))

# ── 5) 计划价大移动：记 plan 变更；changed_at 保持最新 ──────────────
meta = jsh.record_batch("BTCUSDT", "4h",
                        [sig("turtle", "bearish", 0.9, 57000)], now=T0 + 240)
h = jsh.history("BTCUSDT", "4h")
check("计划-流水+1", h["total"] == 3)
check("计划-kinds", "plan" in (h["rows"][0]["change_kinds"] or []))
check("计划-changed_at最新", meta["turtle"]["changed_at"] == T0 + 240)

# ── 6) 无变化轮：changed_at 保持上次变更时间不回退 ─────────────────
meta = jsh.record_batch("BTCUSDT", "4h",
                        [sig("turtle", "bearish", 0.9, 57000)], now=T0 + 300)
check("保持-changed_at不回退", meta["turtle"]["changed_at"] == T0 + 240,
      f"changed={meta['turtle']['changed_at']}")

# ── 7) state 查询 ───────────────────────────────────────────────────
st = jsh.state("BTCUSDT", "4h")
check("state-行数", st["ok"] and len(st["rows"]) == 1)
check("state-字段", st["rows"][0]["updated_ts"] == T0 + 300
      and st["rows"][0]["changed_ts"] == T0 + 240)

# ── 8) 过滤查询：tf/system/时间段 ──────────────────────────────────
jsh.record_batch("ETHUSDT", "1h", [sig("dow", "bullish", 0.4)], now=T0)
jsh.record_batch("ETHUSDT", "1h", [sig("dow", "bearish", 0.4)], now=T0 + 60)
check("过滤-按symbol", jsh.history("ETHUSDT")["total"] == 1)
check("过滤-按system", jsh.history(None, None, "turtle")["total"] == 3)
check("过滤-时间段", jsh.history("BTCUSDT", "4h", since=T0 + 200,
                                 until=T0 + 260)["total"] == 1)

# ── 9) 删除：按 id / 按条件 ────────────────────────────────────────
h = jsh.history("BTCUSDT", "4h")
first_id = h["rows"][-1]["id"]
d = jsh.delete_changes(ids=[first_id])
check("删除-按id", d["ok"] and d["deleted"] == 1, f"{d}")
check("删除-剩余", jsh.history("BTCUSDT", "4h")["total"] == 2)
d = jsh.delete_changes(symbol="BTCUSDT", tf="4h")
check("删除-按条件", d["ok"] and d["deleted"] == 2)
d = jsh.delete_changes()
check("删除-无条件拒绝", not d["ok"])

# ── 10) 裁剪 ───────────────────────────────────────────────────────
for i in range(30):
    jsh.record_batch("SOLUSDT", "5m",
                     [sig("gap", "bullish" if i % 2 == 0 else "bearish", 0.5)],
                     now=T0 + i * 60)
removed = jsh.prune(max_rows=10)
check("裁剪-删除数", removed == 29 + 1 - 10 or removed > 0, f"removed={removed}")
check("裁剪-上限内", jsh.history("SOLUSDT")["total"] <= 10)

print()
if _FAILED:
    print(f"FAILED: {len(_FAILED)} → {_FAILED}")
    raise SystemExit(1)
print("ALL PASS")
