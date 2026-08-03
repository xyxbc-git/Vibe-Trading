#!/usr/bin/env python3
"""信号提醒邮件渲染冒烟：_format_mail 三场景 + record_batch 事件携带快照。

不联网不发信：
  1. 有完整 plan 变更 → 主题带「（含点位变更）」，正文含止盈止损「前 → 后 (±%)」
  2. 无 plan → 友好降级「该信号当前无自洽交易计划」
  3. 仅方向变更（plan 未动）→ 主题无后缀，计划各项只展示当前值（无箭头）
  4. 旧版事件（无快照字段）→ 按旧模板渲染不报错（向后兼容）
  5. 集成：record_batch 产出的 changed_events 确已携带
     change_kinds / prev_snapshot / new_snapshot，且能直接渲染成邮件
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="jarvis_sigalert_")

import jarvis_signal_history as jsh  # noqa: E402

jsh.DB_PATH = os.path.join(_TMP, "test.db")
jsh._INITED = False

import jarvis_signal_alert as jsa  # noqa: E402

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def base_ev(**over) -> dict:
    ev = {
        "symbol": "ETHUSDT", "tf": "1h", "system": "dow", "name_cn": "道氏理论",
        "summary": "计划调整（止损 1860.5→1872.3）",
        "prev_direction": "bullish", "new_direction": "bullish",
        "prev_strength": 0.6, "new_strength": 0.65, "price": 1880.0,
    }
    ev.update(over)
    return ev


def plan_line(body: str, label: str) -> str:
    """取计划块内的字段行（两空格缩进，区别于顶部同名字段如「方向：」）。"""
    hits = [ln for ln in body.splitlines() if ln.startswith(f"  {label}：")]
    return hits[0] if hits else ""


# ── 1) 有完整 plan 变更 ────────────────────────────────────────────
plan_prev = {"side": "long", "entry": 1850.0, "entry_type": "market",
             "stop_loss": 1860.5, "take_profit": 1900.0}
plan_new = {"side": "long", "entry": 1855.0, "entry_type": "market",
            "stop_loss": 1872.3, "take_profit": 1920.0}
ev = base_ev(change_kinds=["plan", "levels"],
             prev_snapshot={"trade_plan": plan_prev, "key_levels": []},
             new_snapshot={"trade_plan": plan_new,
                           "key_levels": [{"label": "关键支撑", "price": 1800.0},
                                          {"label": "强阻力", "price": 1950.0}]})
subject, body = jsa._format_mail(ev)
check("1.主题含点位变更后缀", subject.endswith("（含点位变更）"), subject)
check("1.止损前后对比+幅度", "止损：1,860.50 → 1,872.30 (+0.63%)" in body,
      plan_line(body, "止损"))
check("1.止盈前后对比+幅度", "止盈1：1,900.00 → 1,920.00 (+1.05%)" in body,
      plan_line(body, "止盈1"))
check("1.入场前后对比+幅度", "入场：1,850.00 → 1,855.00 (+0.27%)" in body,
      plan_line(body, "入场"))
check("1.计划方向未变只展示当前值", plan_line(body, "方向").strip() == "方向：做多")
check("1.关键点位齐全", "关键支撑：1,800.00" in body and "强阻力：1,950.00" in body)
check("1.保留原字段", "变化内容：" in body and "当前价：1,880.00" in body
      and "强度：60% → 65%" in body and "提示：单一系统信号变化不构成买卖建议" in body)

# ── 2) 无 plan ─────────────────────────────────────────────────────
ev2 = base_ev(summary="方向 中性→看涨", prev_direction="neutral",
              new_direction="bullish", change_kinds=["direction"],
              prev_snapshot={"trade_plan": None, "key_levels": []},
              new_snapshot={"trade_plan": None, "key_levels": []})
subject2, body2 = jsa._format_mail(ev2)
check("2.主题无后缀", not subject2.endswith("（含点位变更）"), subject2)
check("2.无计划友好降级", "该信号当前无自洽交易计划" in body2)
check("2.方向变更展示", "方向：中性 → 看涨" in body2)

# ── 3) 仅方向变更（plan 未动）──────────────────────────────────────
plan_same = {"side": "short", "entry": 1900.0, "entry_type": "market",
             "stop_loss": 1930.0, "take_profit": 1840.0}
ev3 = base_ev(summary="方向 看涨→看跌", new_direction="bearish",
              change_kinds=["direction"],
              prev_snapshot={"trade_plan": plan_same, "key_levels": []},
              new_snapshot={"trade_plan": plan_same, "key_levels": []})
subject3, body3 = jsa._format_mail(ev3)
check("3.主题无后缀", not subject3.endswith("（含点位变更）"), subject3)
check("3.顶部方向翻转", "方向：看涨 → 看跌" in body3)
sl_line = plan_line(body3, "止损")
check("3.止损未变无箭头", sl_line.strip() == "止损：1,930.00", sl_line)
check("3.止盈未变无箭头", plan_line(body3, "止盈1").strip() == "止盈1：1,840.00")
check("3.计划方向展示做空", plan_line(body3, "方向").strip() == "方向：做空")

# ── 4) 旧版事件（无快照字段）向后兼容 ──────────────────────────────
ev4 = base_ev()
subject4, body4 = jsa._format_mail(ev4)
check("4.旧事件不渲染计划块", "交易计划" not in body4 and "关键点位" not in body4)
check("4.旧事件主题正常", subject4 == "【贾维斯信号提醒】ETHUSDT 1h 道氏理论 信号变化")

# ── 5) 集成：record_batch 事件携带快照并可直接渲染 ─────────────────
def _mk_sig(direction: str, entry: float) -> dict:
    sl = entry * (0.98 if direction == "bullish" else 1.02)
    tp = entry * (1.04 if direction == "bullish" else 0.96)
    return {"system": "turtle", "name_cn": "海龟交易", "direction": direction,
            "strength": 0.6, "reasoning": "测试",
            "key_levels": [{"label": "支撑", "price": entry * 0.97}],
            "trade_plan": {"side": "long" if direction == "bullish" else "short",
                           "entry": entry, "entry_type": "market",
                           "stop_loss": sl, "take_profit": tp}}


captured: list[dict] = []
_real_notify = jsa.maybe_notify
jsa.maybe_notify = lambda evs: (captured.extend(evs), {"matched": 0})[1]
try:
    T0 = 1_720_000_000.0
    jsh.record_batch("BTCUSDT", "4h", [_mk_sig("bullish", 60000.0)], price=60000.0, now=T0)
    jsh.record_batch("BTCUSDT", "4h", [_mk_sig("bearish", 59000.0)], price=59000.0, now=T0 + 60)
finally:
    jsa.maybe_notify = _real_notify

check("5.变更事件产出", len(captured) == 1, f"n={len(captured)}")
if captured:
    e = captured[0]
    check("5.事件携带change_kinds", "direction" in (e.get("change_kinds") or []))
    check("5.事件携带前快照计划", (e.get("prev_snapshot") or {}).get("trade_plan", {})
          .get("entry") == 60000.0)
    check("5.事件携带后快照计划", (e.get("new_snapshot") or {}).get("trade_plan", {})
          .get("entry") == 59000.0)
    s5, b5 = jsa._format_mail(e)
    check("5.真实事件可渲染对比", "入场：60,000.00 → 59,000.00 (-1.67%)" in b5,
          plan_line(b5, "入场"))
    check("5.真实事件关键位", "支撑：" in b5)

print()
if _FAILED:
    print(f"FAILED: {len(_FAILED)} → {_FAILED}")
    raise SystemExit(1)
print("ALL PASS")
