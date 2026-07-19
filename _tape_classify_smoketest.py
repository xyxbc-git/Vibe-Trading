#!/usr/bin/env python3
"""成交流主体分类冒烟：mock aggTrade 序列验证 指纹聚类/分层/判定/入场提示。

不联网、不依赖 WS：直接调 ingest()/summary() 纯路径。
"""

from __future__ import annotations

import jarvis_tape_classify as jtc

CFG = {"whale_tier1_usd": 100000.0, "whale_tier2_usd": 1000000.0}
BASE_MS = 1_720_000_000_000  # 对齐分钟的基准

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def trade(price: float, qty: float, is_buy: bool, ts_ms: int) -> dict:
    return {"e": "aggTrade", "p": str(price), "q": str(qty), "T": ts_ms,
            "m": (not is_buy)}


# ── 1) 指纹聚类：同 qty 反复出现 → 同组；单侧大额组 = 机构 ──────────────
jtc.reset_state()
for i in range(5):  # 同一数量 0.5 BTC × $60k... 每笔 $30k，单侧卖出
    jtc.ingest("BTCUSDT", trade(60000, 0.5, False, BASE_MS + i * 5000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, window_min=15, now_ms=BASE_MS + 60_000)
fp = next((f for f in s["fingerprints"] if f["qty"] == 0.5), None)
check("指纹-成组", fp is not None and fp["n"] == 5, f"fp={fp}")
check("指纹-单侧大额=机构", fp is not None and fp["cls"] == "inst", f"cls={fp and fp['cls']}")
check("指纹-净卖出", fp is not None and fp["net_usd"] == -150000, f"net={fp and fp['net_usd']}")

# ── 2) 双边平衡组 = 做市商 ────────────────────────────────────────────
jtc.reset_state()
for i in range(8):  # 同数量 0.3，买卖交替（4买4卖，每笔 $18k）
    jtc.ingest("BTCUSDT", trade(60000, 0.3, i % 2 == 0, BASE_MS + i * 3000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 60_000)
fp = next((f for f in s["fingerprints"] if f["qty"] == 0.3), None)
check("做市-双边组", fp is not None and fp["cls"] == "maker",
      f"cls={fp and fp['cls']} buy={fp and fp['buy_n']} sell={fp and fp['sell_n']}")

# ── 3) 金额分层：散户/中户/机构 ──────────────────────────────────────
jtc.reset_state()
jtc.ingest("BTCUSDT", trade(60000, 0.001, True, BASE_MS), cfg=CFG)       # $60 散户
jtc.ingest("BTCUSDT", trade(60000, 0.5, True, BASE_MS + 1000), cfg=CFG)  # $30k 中户
jtc.ingest("BTCUSDT", trade(60000, 3.0, False, BASE_MS + 2000), cfg=CFG)  # $180k 机构
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 5000)
actors = s["breakdown"]["actors"]
check("分层-散户", actors["retail"]["usd"] == 60, f"{actors['retail']}")
check("分层-中户", actors["mid"]["usd"] == 30000, f"{actors['mid']}")
check("分层-机构", actors["inst"]["usd"] == 180000, f"{actors['inst']}")
check("分层-总额", s["breakdown"]["total_usd"] == 60 + 30000 + 180000)
rec = s["recent"][0]
check("最近成交-新在前", rec["ts_ms"] == BASE_MS + 2000 and rec["cls"] == "inst")

# ── 4) 主力砸盘判定 + burst 警报 ─────────────────────────────────────
jtc.reset_state()
# 前 5 分钟每分钟一笔 $120k 卖（基线），价格阶梯下行
for i in range(5):
    jtc.ingest("BTCUSDT", trade(60000 - i * 100, 2.0, False,
                                BASE_MS + i * 60_000), cfg=CFG)
# 第 6 分钟突然 6 笔大额卖单涌入（qty 各异，不成指纹组，靠金额分层）
for j in range(6):
    jtc.ingest("BTCUSDT", trade(59400 - j * 50, 2.0 + j * 0.31, False,
                                BASE_MS + 5 * 60_000 + j * 5000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 5 * 60_000 + 40_000)
check("判定-砸盘", s["verdict"]["action"] == "砸盘", f"action={s['verdict']['action']}")
check("判定-burst触发", s["verdict"]["burst"] is not None
      and s["verdict"]["burst"]["side"] == "sell",
      f"burst={s['verdict']['burst']}")
check("判定-主导非散户", s["verdict"]["dominant"] in ("inst", "maker"),
      f"dominant={s['verdict']['dominant']}")

# ── 5) 砸盘力度减弱 → 入场提示 ───────────────────────────────────────
jtc.reset_state()
# 分钟 0-2：重砸（每分钟 $600k 卖）；分钟 3-5：抛压骤减（每分钟 $60k 卖）
for minute in range(6):
    per_min_sell = 600_000 if minute < 3 else 60_000
    qty = per_min_sell / 60000.0
    jtc.ingest("BTCUSDT", trade(60000 - minute * 50, qty, False,
                                BASE_MS + minute * 60_000 + 1000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 5 * 60_000 + 30_000)
check("衰减-入场提示", s["verdict"]["entry_hint"] is not None,
      f"hint={s['verdict']['entry_hint']}")
check("衰减-动作仍偏空", s["verdict"]["action"] in ("砸盘", "派发/出货"),
      f"action={s['verdict']['action']}")

# ── 6) 吸筹判定：大买单但价格滞涨 ───────────────────────────────────
jtc.reset_state()
for i in range(5):
    # 每分钟 $200k 买入，价格几乎不动（60000 ± 5）
    jtc.ingest("BTCUSDT", trade(60000 + (i % 2) * 5, 3.34, True,
                                BASE_MS + i * 60_000), cfg=CFG)
s = jtc.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 4 * 60_000 + 30_000)
check("吸筹-判定", s["verdict"]["action"] == "吸筹", f"action={s['verdict']['action']}")

# ── 7) 空态 ─────────────────────────────────────────────────────────
jtc.reset_state()
s = jtc.summary("PEPEUSDT", cfg=CFG)
check("空态-active=false", s["active"] is False)

print()
if _FAILED:
    print(f"FAILED: {len(_FAILED)} → {_FAILED}")
    raise SystemExit(1)
print("ALL PASS")
