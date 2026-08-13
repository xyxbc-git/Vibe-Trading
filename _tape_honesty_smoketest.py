#!/usr/bin/env python3
"""成交流画像 P0 诚实化冒烟（任务 D1，方案 20260813 §三 A1-A3+B3）。

构造成交流零出网零 WS：窗口真实性 / coverage / 样本门禁 / 身份降格 /
置信度 / L0 边界 / 翻转阻尼 / 混沌态 / HUD 契约。
"""

from __future__ import annotations

import jarvis_tape_classify as jtc

CFG = {"whale_tier1_usd": 100000.0, "whale_tier2_usd": 1000000.0}
BASE_MS = 1_720_000_000_000

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("PASS" if ok else "FAIL"), name, detail)


def trade(price: float, qty: float, is_buy: bool, ts_ms: int) -> dict:
    return {"e": "aggTrade", "p": str(price), "q": str(qty), "T": ts_ms,
            "m": (not is_buy)}


# ── H1 窗口真实性：5min 跨度成交，标称 240min → actual 如实报 ~5min ──
jtc.reset_state()
for i in range(6):   # 每分钟 1 笔小买单，跨 0..5 分钟
    jtc.ingest("BTCUSDT", trade(60000, 0.001, True, BASE_MS + i * 60_000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, window_min=240, now_ms=BASE_MS + 5 * 60_000 + 30_000)
check("H1 actual_window_min 如实（标称240 实际~5.5）",
      s["window_min"] == 240 and 5.0 <= s["actual_window_min"] <= 6.0,
      f"actual={s['actual_window_min']}")

# ── H2 coverage 连续：每分钟都有成交 → continuous ──
check("H2 coverage=continuous 无缺口",
      s["coverage"]["grade"] == "continuous" and s["coverage"]["gaps"] == [],
      f"cov={s['coverage']['pct']}%")

# ── H3 coverage 断档：中间挖 4 分钟洞 → 降档 + gaps 列表 ──
jtc.reset_state()
for i in (0, 1, 6, 7):   # 分钟 2-5 无成交
    jtc.ingest("BTCUSDT", trade(60000, 0.001, True, BASE_MS + i * 60_000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, window_min=240, now_ms=BASE_MS + 7 * 60_000 + 30_000)
check("H3 断档降档 + gaps 记录",
      s["coverage"]["grade"] in ("gapped", "severe_gaps")
      and len(s["coverage"]["gaps"]) == 1
      and s["coverage"]["gaps"][0]["missing_min"] == 4,
      f"cov={s['coverage']}")

# ── H4 actor 样本门禁：n<10 且额小 → insufficient，pct 不给数 ──
jtc.reset_state()
for i in range(3):   # 3 笔 $60 散户单
    jtc.ingest("BTCUSDT", trade(60000, 0.001, True, BASE_MS + i * 1000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 5000)
r = s["breakdown"]["actors"]["retail"]
check("H4 门禁：3笔$180 → insufficient + pct=None + 人话",
      r["insufficient"] and r["pct"] is None and r["long_pct"] is None
      and "样本不足" in r["verdict_cn"], f"{r['verdict_cn']}")

# ── H5 大额豁免：1 笔 $180k 大单 → 金额过 tier1 不判 insufficient ──
inst = s["breakdown"]["actors"]["inst"]
jtc.reset_state()
jtc.ingest("BTCUSDT", trade(60000, 3.0, False, BASE_MS), cfg=CFG)   # $180k
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 5000)
inst = s["breakdown"]["actors"]["inst"]
check("H5 大额豁免：1笔$180k 不判 insufficient（大额=强证据）",
      not inst["insufficient"] and inst["pct"] == 100.0, f"pct={inst['pct']}")

# ── H6 verdict 总门禁：少笔小额 → 不可判定 + L0 降级文案 ──
jtc.reset_state()
jtc.ingest("BTCUSDT", trade(60000, 0.001, True, BASE_MS), cfg=CFG)   # 1 笔 $60
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 5000)
v = s["verdict"]
check("H6 总门禁：1笔$60 → action=不可判定 + confidence=样本不足",
      v["action"] == "不可判定" and v["insufficient"]
      and v["confidence"] == "样本不足", f"action={v['action']}")
check("H6b L0 降级文案", "样本不足，暂不判定" in v["l0_text"], v["l0_text"])

# ── H7 大额豁免（verdict 级）：5 笔 $200k 买单（qty 各异不成指纹组）──
jtc.reset_state()
for i in range(5):
    jtc.ingest("BTCUSDT", trade(60000 + (i % 2) * 5, 3.34 + i * 0.013, True,
                                BASE_MS + i * 60_000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 4 * 60_000 + 30_000)
v = s["verdict"]
check("H7 verdict 大额豁免：5笔$1M → 正常判定（吸筹）",
      not v["insufficient"] and v["action"] == "吸筹", f"action={v['action']}")

# ── H8 身份降格：size_cn 主标签 + label_note 推断声明 ──
a = s["breakdown"]["actors"]["inst"]
check("H8 主标签=单笔规模（大单）+ 副标签注明推断",
      a["size_cn"] == "大单" and a["actor_cn"] == "机构/大户"
      and "推断" in a["label_note"], f"{a['size_cn']}/{a['label_note']}")

# ── H9 置信度：纯金额分层 vs 指纹组行为证据 ──
check("H9a 纯金额分层（qty 各异不成组）→ 仅金额分层",
      v["confidence"] == "仅金额分层", v["confidence"])
jtc.reset_state()
for i in range(12):   # 同 qty 单侧组 12 笔 × $30k：指纹组证据
    jtc.ingest("BTCUSDT", trade(60000, 0.5, False, BASE_MS + i * 5000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 70_000)
v = s["verdict"]
check("H9b 指纹组证据（12笔同qty单侧）→ 证据充分",
      v["confidence"] == "证据充分"
      and s["breakdown"]["actors"]["inst"]["confidence"] == "证据充分",
      f"verdict={v['confidence']}")

# ── H10 L0 边界：只描述盘口行为，不含方向/开单建议词 ──
banned = ("追多", "追空", "建议入场", "可以买", "可以卖", "别追", "等回踩")
check("H10 L0 无方向建议词（边界三原则）",
      all(w not in v["l0_text"] for w in banned), v["l0_text"])

# ── H11 阻尼：首见直采；新判定 3 轮（间隔≥20s）才切换 ──
damp = {"displayed": None, "candidate": None, "cand_n": 0, "last_ts": 0.0, "flips": []}
d1, c1 = jtc.apply_damping(damp, "中性", 1000.0)
d2, _ = jtc.apply_damping(damp, "砸盘", 1030.0)     # 第 1 轮
d3, _ = jtc.apply_damping(damp, "砸盘", 1035.0)     # 间隔<20s 不算轮
d4, _ = jtc.apply_damping(damp, "砸盘", 1055.0)     # 第 2 轮
check("H11a 首见直采 + 未满 3 轮不切换",
      d1 == "中性" and d2 == "中性" and d3 == "中性" and d4 == "中性",
      f"{d1}/{d2}/{d3}/{d4}")
d5, _ = jtc.apply_damping(damp, "砸盘", 1080.0)     # 第 3 轮 → 切换
check("H11b 连续 3 轮同向切换", d5 == "砸盘", d5)
d6, _ = jtc.apply_damping(damp, "中性", 1100.0)     # 新候选 1 轮
d7, _ = jtc.apply_damping(damp, "砸盘", 1130.0)     # 回到已展示值 → 候选清零
d8, _ = jtc.apply_damping(damp, "中性", 1160.0)     # 重新 1 轮
check("H11c 候选中断后重新计数", d6 == "砸盘" and d7 == "砸盘" and d8 == "砸盘")

# ── H12 混沌：5min 内切换 ≥3 次 → chaotic ──
damp2 = {"displayed": None, "candidate": None, "cand_n": 0, "last_ts": 0.0, "flips": []}
t = 1000.0
jtc.apply_damping(damp2, "中性", t)
chaotic_seen = False
for target in ("砸盘", "拉盘/操盘", "吸筹"):    # 3 次完整切换
    for _ in range(3):
        t += 25.0
        _, ch = jtc.apply_damping(damp2, target, t)
        chaotic_seen = chaotic_seen or ch
check("H12 5min 内 3 次切换 → 混沌态", chaotic_seen and len(damp2["flips"]) >= 3,
      f"flips={len(damp2['flips'])}")

# ── H13 HUD 契约字段齐全（方案 §五 输出契约）──
need = ("action", "confidence", "nr_share_pct", "actual_window_min",
        "coverage", "burst", "entry_hint", "l0_text")
check("H13 hud 契约字段齐全", all(k in s["hud"] for k in need),
      f"keys={sorted(s['hud'])}")

# ── H14 summary 级阻尼：同 symbol 连续调用，判定翻转被压住 + l0 注明待确认 ──
jtc.reset_state()
for i in range(12):
    jtc.ingest("ETHUSDT", trade(3000, 10.0, False, BASE_MS + i * 5000), cfg=CFG)  # 砸
s = jtc.summary("ETHUSDT", cfg=CFG, now_ms=BASE_MS + 70_000)
first_action = s["verdict"]["action"]
for i in range(12, 24):   # 转为大额买入 → raw 变，但 1 次调用不足以切换
    jtc.ingest("ETHUSDT", trade(3000, 10.0, True, BASE_MS + 80_000 + (i - 12) * 5000),
               cfg=CFG)
s2 = jtc.summary("ETHUSDT", cfg=CFG, now_ms=BASE_MS + 150_000)
v2 = s2["verdict"]
check("H14 summary 级阻尼：raw 已变但展示值未切 + action_raw 如实",
      v2["action"] == first_action and v2["action_raw"] != first_action
      and "待连续确认" in v2["l0_text"],
      f"displayed={v2['action']} raw={v2['action_raw']}")

print(f"\nALL {'PASS' if not FAIL else 'FAIL'}  (pass={len(PASS)} fail={len(FAIL)})")
raise SystemExit(1 if FAIL else 0)
