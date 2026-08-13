#!/usr/bin/env python3
"""12 系统周期适配标注冒烟（任务S）：配置默认矩阵 + _annotate_tf_suitability 判定。

不联网：直接调 dashboard 的标注函数与 jarvis_config 默认值。
验收断言：
  - 配置默认矩阵 12 系统全登记，TF 值全部合法
  - 方向系统按矩阵判定（dow 5m 不适用 / 4h 适用）
  - 非方向系统（martingale/arbitrage，[]）恒适用
  - 未登记系统不拦（tf_suitable=True，suitable_tfs=None）
  - 5m 周期方向系统全不适用（用户病根场景：5m 上道氏/三重RSI 给点位）
  - 配置异常时静默降级不抛出、不加字段
"""

from __future__ import annotations

import jarvis_config as jc
import jarvis_dashboard as jd

_FAILED: list[str] = []
ALL_TFS = {"5m", "15m", "30m", "1h", "4h", "1d"}
SYSTEMS = ("turtle", "dow", "elliott", "volatility", "gann", "chanlun",
           "rule123", "gap", "martingale", "oscillator", "triple_rsi", "arbitrage")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def sig(system: str) -> dict:
    return {"system": system, "name_cn": system, "direction": "bearish",
            "strength": 0.6, "reasoning": "冒烟", "trade_plan": {"entry": 1.0}}


# ── 1) 配置默认矩阵完整性 ───────────────────────────────────────────
m = jc.get("twelve_suitable_tfs")
check("配置为 dict", isinstance(m, dict))
check("12 系统全登记", set(m.keys()) == set(SYSTEMS), f"keys={sorted(m.keys())}")
check("TF 值全部合法", all(set(v) <= ALL_TFS for v in m.values()))
check("非方向系统 TF 无关", m["martingale"] == [] and m["arbitrage"] == [])

# ── 2) 判定语义 ────────────────────────────────────────────────────
rows = [sig("dow"), sig("martingale"), sig("unknown_system")]
jd._annotate_tf_suitability(rows, "5m")
check("dow 5m 不适用", rows[0]["tf_suitable"] is False)
check("dow 携带适用周期表", rows[0]["suitable_tfs"] == m["dow"])
check("马丁 5m 恒适用（TF 无关）", rows[1]["tf_suitable"] is True)
check("未登记系统不拦", rows[2]["tf_suitable"] is True and rows[2]["suitable_tfs"] is None)

rows = [sig("dow"), sig("triple_rsi"), sig("chanlun")]
jd._annotate_tf_suitability(rows, "4h")
check("dow 4h 适用", rows[0]["tf_suitable"] is True)
check("triple_rsi 4h 适用", rows[1]["tf_suitable"] is True)
check("chanlun 4h 适用", rows[2]["tf_suitable"] is True)

rows = [sig("chanlun"), sig("oscillator"), sig("triple_rsi")]
jd._annotate_tf_suitability(rows, "15m")
check("chanlun 15m 适用", rows[0]["tf_suitable"] is True)
check("oscillator 15m 适用", rows[1]["tf_suitable"] is True)
check("triple_rsi 15m 不适用（T8 判死）", rows[2]["tf_suitable"] is False)

# ── 3) 用户病根场景：5m 上全部方向系统隐藏 ──────────────────────────
directional = [s for s in SYSTEMS if m[s]]
rows = [sig(s) for s in directional]
jd._annotate_tf_suitability(rows, "5m")
hidden = [r["system"] for r in rows if r["tf_suitable"] is False]
check("5m 方向系统全不适用", len(hidden) == len(directional),
      f"hidden={len(hidden)}/{len(directional)}")

# ── 4) 配置异常降级：不抛出、不加字段 ───────────────────────────────
orig = jc.get
try:
    jc.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore
    rows = [sig("dow")]
    jd._annotate_tf_suitability(rows, "5m")   # 不得抛出
    check("配置异常→静默跳过", "tf_suitable" not in rows[0])
finally:
    jc.get = orig

print()
if _FAILED:
    print(f"❌ {len(_FAILED)} 项失败: {_FAILED}")
    raise SystemExit(1)
print("✅ 周期适配标注冒烟全部通过")
