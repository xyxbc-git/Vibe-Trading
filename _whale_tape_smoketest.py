#!/usr/bin/env python3
"""whale tape 冒烟：mock aggTrade 序列验证 分层聚合 / 滚动窗口 / 异常事件。

不联网、不依赖 WS：直接调 ingest()/summary()/build_summary()/whale_check() 纯路径。
"""

from __future__ import annotations

import jarvis_whale_tape as jwt

# 固定配置注入：tier1=100k tier2=1M window=15min（不读用户真实 config.yaml）
CFG = {"whale_tier1_usd": 100000.0, "whale_tier2_usd": 1000000.0, "whale_window_min": 15}

BASE_MS = 1_720_000_000_000  # 任意对齐分钟的基准时间戳

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def trade(price: float, qty: float, is_buy: bool, ts_ms: int) -> dict:
    """构造 aggTrade 消息体（m=true 为主动卖）。"""
    return {"e": "aggTrade", "p": str(price), "q": str(qty), "T": ts_ms,
            "m": (not is_buy)}


# ── 1) 分层聚合：小单只进总量，tier1 进大单，tier2 记巨单事件 ──────────────
jwt.reset_state()
jwt.ingest("BTCUSDT", trade(60000, 0.001, True, BASE_MS), cfg=CFG)          # $60 小单
jwt.ingest("BTCUSDT", trade(60000, 2.0, True, BASE_MS + 1000), cfg=CFG)     # $120k tier1 买
jwt.ingest("BTCUSDT", trade(60000, 20.0, False, BASE_MS + 2000), cfg=CFG)   # $1.2M tier2 卖
s = jwt.summary("BTCUSDT", cfg=CFG, now_ms=BASE_MS + 3000)
check("分层-总量含小单", s["total_usd"] == 60 + 120000 + 1200000, f"total={s['total_usd']}")
check("分层-大单笔数只计 tier1+", s["whale_n"] == 2, f"whale_n={s['whale_n']}")
check("分层-买卖分边", s["buy_usd"] == 120000 and s["sell_usd"] == 1200000,
      f"buy={s['buy_usd']} sell={s['sell_usd']}")
check("分层-净流=买-卖", s["net_usd"] == 120000 - 1200000, f"net={s['net_usd']}")
supers = [e for e in s["events"] if e["kind"] == "single_super"]
check("事件-单笔超tier2触发", len(supers) == 1 and supers[0]["side"] == "sell",
      f"supers={supers}")
check("分层-占比口径", abs(s["whale_share_pct"] - (1320000 / 1320060 * 100)) < 0.1,
      f"share={s['whale_share_pct']}")

# ── 2) 滚动窗口：window_min 之前的桶被剔除 ──────────────────────────────
jwt.reset_state()
old_ms = BASE_MS
new_ms = BASE_MS + 20 * 60_000  # 20 分钟后（超出 15min 窗口）
jwt.ingest("ETHUSDT", trade(3000, 50, True, old_ms), cfg=CFG)    # $150k（将过期）
jwt.ingest("ETHUSDT", trade(3000, 40, False, new_ms), cfg=CFG)   # $120k（窗口内）
buckets = list(jwt._STATE["ETHUSDT"]["buckets"])
s_now = jwt.build_summary(buckets, [], [], tier1=CFG["whale_tier1_usd"],
                          tier2=CFG["whale_tier2_usd"], window_min=15, now_ms=new_ms)
check("窗口-过期桶剔除", s_now["buy_usd"] == 0 and s_now["sell_usd"] == 120000,
      f"buy={s_now['buy_usd']} sell={s_now['sell_usd']}")
s_full = jwt.build_summary(buckets, [], [], tier1=CFG["whale_tier1_usd"],
                           tier2=CFG["whale_tier2_usd"], window_min=30, now_ms=new_ms)
check("窗口-拉长窗口两桶都在", s_full["buy_usd"] == 150000 and s_full["sell_usd"] == 120000,
      f"buy={s_full['buy_usd']} sell={s_full['sell_usd']}")

# ── 3) 连续同向大单事件：≥5 笔同向触发一次，被反向打断后重置 ────────────────
jwt.reset_state()
for i in range(4):
    jwt.ingest("SOLUSDT", trade(150, 1000, True, BASE_MS + i * 1000), cfg=CFG)  # $150k ×4
s = jwt.summary("SOLUSDT", cfg=CFG)
check("事件-4笔同向未触发", not any(e["kind"] == "consecutive" for e in s["events"]))
jwt.ingest("SOLUSDT", trade(150, 1000, True, BASE_MS + 5000), cfg=CFG)  # 第 5 笔
s = jwt.summary("SOLUSDT", cfg=CFG)
consec = [e for e in s["events"] if e["kind"] == "consecutive"]
check("事件-第5笔同向触发", len(consec) == 1 and consec[0]["side"] == "buy",
      f"consec={consec}")
jwt.ingest("SOLUSDT", trade(150, 1000, True, BASE_MS + 6000), cfg=CFG)  # 第 6 笔（已 fired 不重复）
s = jwt.summary("SOLUSDT", cfg=CFG)
check("事件-同向持续不重复触发",
      len([e for e in s["events"] if e["kind"] == "consecutive"]) == 1)
jwt.ingest("SOLUSDT", trade(150, 1000, False, BASE_MS + 7000), cfg=CFG)  # 反向打断
check("事件-反向打断重置计数",
      jwt._STATE["SOLUSDT"]["consec_side"] == "sell"
      and jwt._STATE["SOLUSDT"]["consec_n"] == 1)

# ── 4) 量价背离：净买入显著但价格滞涨 → sell_into_buys ──────────────────
jwt.reset_state()
# 6 笔大买单共 $1.5M（≥tier2），价格全程横盘 60000 → 60005（+0.008% < 0.05%）
for i in range(6):
    jwt.ingest("BTCUSDT", trade(60000 + i, 4.2, True, BASE_MS + i * 60_000), cfg=CFG)
buckets = list(jwt._STATE["BTCUSDT"]["buckets"])
s_div = jwt.build_summary(buckets, [], [], tier1=CFG["whale_tier1_usd"],
                          tier2=CFG["whale_tier2_usd"], window_min=15,
                          now_ms=BASE_MS + 6 * 60_000)
check("背离-大买单价滞涨检出", s_div["divergence"] is not None
      and s_div["divergence"]["side"] == "sell_into_buys",
      f"div={s_div['divergence']}")
# 反例：净买入且价格同步上涨（+2%）→ 不算背离
jwt.reset_state()
for i in range(6):
    jwt.ingest("BTCUSDT", trade(60000 * (1 + 0.004 * i), 4.2, True,
                                BASE_MS + i * 60_000), cfg=CFG)
buckets = list(jwt._STATE["BTCUSDT"]["buckets"])
s_ok = jwt.build_summary(buckets, [], [], tier1=CFG["whale_tier1_usd"],
                         tier2=CFG["whale_tier2_usd"], window_min=15,
                         now_ms=BASE_MS + 6 * 60_000)
check("背离-价涨同向不误报", s_ok["divergence"] is None, f"div={s_ok['divergence']}")
# 镜像：净卖出显著但价格不跌 → buy_into_sells
jwt.reset_state()
for i in range(6):
    jwt.ingest("BTCUSDT", trade(60000, 4.2, False, BASE_MS + i * 60_000), cfg=CFG)
buckets = list(jwt._STATE["BTCUSDT"]["buckets"])
s_div2 = jwt.build_summary(buckets, [], [], tier1=CFG["whale_tier1_usd"],
                           tier2=CFG["whale_tier2_usd"], window_min=15,
                           now_ms=BASE_MS + 6 * 60_000)
check("背离-大卖单价不跌检出(吸筹)", s_div2["divergence"] is not None
      and s_div2["divergence"]["side"] == "buy_into_sells",
      f"div={s_div2['divergence']}")

# ── 5) 坏数据与空态：不抛出、返回中性 ───────────────────────────────────
jwt.reset_state()
jwt.ingest("BTCUSDT", {"p": "abc", "q": None}, cfg=CFG)   # 坏数据
jwt.ingest("BTCUSDT", {}, cfg=CFG)
s_empty = jwt.summary("NOSUCHUSDT", cfg=CFG)
check("空态-未知币返回inactive", s_empty["active"] is False and s_empty["whale_n"] == 0)
check("坏数据-不入桶不抛出", jwt.summary("BTCUSDT", cfg=CFG)["total_usd"] == 0)

# ── 6) whale_check 安全带因子：逆流提醒 / 同向 / 均衡 / 无数据 ────────────
ws_active = {"active": True, "net_usd": 500000.0, "tier1_usd": 100000.0, "window_min": 15}
r = jwt.whale_check("bearish", ws_active)
check("安全带-逆大单净流提醒", r is not None and r["status"] == "against", f"r={r}")
r2 = jwt.whale_check("bullish", ws_active)
check("安全带-同向aligned", r2 is not None and r2["status"] == "aligned")
r3 = jwt.whale_check("bullish", {"active": True, "net_usd": 5000.0,
                                 "tier1_usd": 100000.0, "window_min": 15})
check("安全带-净流均衡idle", r3 is not None and r3["status"] == "idle")
check("安全带-中性方向不判定", jwt.whale_check("neutral", ws_active) is None)
check("安全带-无数据返回None", jwt.whale_check("bullish", None) is None)
check("安全带-inactive返回None",
      jwt.whale_check("bullish", {"active": False}) is None)

# ── 7) 事件节流：同类事件 10 分钟内不重复 ────────────────────────────────
jwt.reset_state()
st = jwt._sym_state("BTCUSDT")
ok1 = jwt._push_event(st, "divergence", "sell", "x", 1.0, 1.0, BASE_MS)
ok2 = jwt._push_event(st, "divergence", "sell", "x", 1.0, 1.0, BASE_MS + 60_000)
ok3 = jwt._push_event(st, "divergence", "sell", "x", 1.0, 1.0,
                      BASE_MS + 11 * 60_000)
check("节流-10分钟内同类去重", ok1 and not ok2 and ok3)

jwt.reset_state()
print()
if _FAILED:
    print(f"FAILED: {_FAILED}")
    raise SystemExit(1)
print("ALL PASS")
