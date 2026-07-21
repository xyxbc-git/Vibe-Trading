#!/usr/bin/env python3
"""足迹图（Footprint）冒烟：mock aggTrade 走 ingest → footprint 全链路。

不联网不落盘（足迹仅内存）：验证 档位归属 / 买卖分边 / 失衡 flag / 5m 聚合
OHLC·总额·CVD / cells 上限归并 / buckets 上限归并 / actors 多空口径 / 空态容错 /
step 漂移重算。
"""

from __future__ import annotations

import jarvis_tape_classify as jtc

jtc.reset_state()

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def feed(symbol: str, price: float, usd: float, is_buy: bool, ts_ms: int) -> None:
    """按目标美元额折算数量注入一笔成交（cfg={} 隔离 jarvis_config，tier1=10万）。"""
    jtc.ingest(symbol, {"p": str(price), "q": str(usd / price),
                        "m": (not is_buy), "T": ts_ms}, cfg={})


def bar_at(res: dict, ts: int) -> dict | None:
    return next((b for b in res["bars"] if b["ts"] == ts), None)


def row_at(bar: dict, price: float) -> dict | None:
    return next((r for r in bar["rows"] if abs(r["price"] - price) < 1e-9), None)


# 基准时间：对齐 5m 桶起点（epoch 分钟被 5 整除）
T0_MIN = (1_720_000_000 // 300) * 300 // 60
NOW_MS = (T0_MIN + 5) * 60_000 + 5_000
SYM = "TESTUSDT"

# ── 造数（价格 ~100 → step = nice_step(0.05) = 0.05）─────────────────────
# m0：档位归属/买卖分边样本
feed(SYM, 100.00, 100, True, T0_MIN * 60_000 + 1_000)      # 桶 100.00
feed(SYM, 100.02, 50, False, T0_MIN * 60_000 + 2_000)      # 桶 100.00（同档）
feed(SYM, 100.07, 200, True, T0_MIN * 60_000 + 3_000)      # 桶 100.05
# m1：失衡 flag 样本（A 强买失衡 / B 均衡 / C 比例够但量不够）
m1 = (T0_MIN + 1) * 60_000
feed(SYM, 100.01, 3_000, True, m1 + 1_000)   # A 桶 100.00 buy
feed(SYM, 100.01, 500, False, m1 + 2_000)    # A 桶 100.00 sell（比 6 倍）
feed(SYM, 100.06, 600, True, m1 + 3_000)     # B 桶 100.05
feed(SYM, 100.06, 500, False, m1 + 4_000)    # B 均衡
feed(SYM, 100.11, 100, True, m1 + 5_000)     # C 桶 100.10
feed(SYM, 100.11, 400, False, m1 + 6_000)    # C 比 4 倍但量小，不得误触
# m2 低点 / m3 高点 / m4 收盘 / m5 当前分钟（CVD 第二根 bar）
feed(SYM, 99.50, 1_000, True, (T0_MIN + 2) * 60_000 + 1_000)
feed(SYM, 101.00, 800, False, (T0_MIN + 3) * 60_000 + 1_000)
feed(SYM, 100.50, 500, True, (T0_MIN + 4) * 60_000 + 1_000)
feed(SYM, 100.60, 300, True, (T0_MIN + 5) * 60_000 + 2_000)

# ── 1) 档位归属 + 买卖分边（1m·m0）────────────────────────────────────
res = jtc.footprint(SYM, "1m", 10, 40, cfg={}, now_ms=NOW_MS)
check("足迹-ok", res["ok"] and res["active"]
      and abs(res["bucket"] - 0.05) < 1e-12, f"bucket={res.get('bucket')}")
b0 = bar_at(res, T0_MIN * 60)
check("档位-m0两档", b0 is not None and len(b0["rows"]) == 2,
      f"rows={len(b0['rows']) if b0 else None}")
if b0:
    check("档位-价降序", b0["rows"][0]["price"] > b0["rows"][1]["price"])
    r_hi = row_at(b0, 100.05)
    r_lo = row_at(b0, 100.00)
    check("档位-归属正确", r_hi and r_lo is not None)
    check("分边-买卖各归位", abs(r_lo["buy"] - 100) < 0.01
          and abs(r_lo["sell"] - 50) < 0.01 and abs(r_hi["buy"] - 200) < 0.01
          and r_hi["sell"] == 0, f"lo={r_lo} hi={r_hi}")

# ── 2) 失衡 flag：触发与不误触（1m·m1）───────────────────────────────
b1 = bar_at(res, (T0_MIN + 1) * 60)
if b1:
    ra = row_at(b1, 100.00)
    rb = row_at(b1, 100.05)
    rc = row_at(b1, 100.10)
    check("失衡-A触发buy_imb", ra and ra["flag"] == "buy_imb", f"A={ra}")
    check("失衡-B均衡不触发", rb and rb["flag"] is None, f"B={rb}")
    check("失衡-C量不足不误触", rc and rc["flag"] is None, f"C={rc}")
else:
    check("失衡-m1bar存在", False)

# ── 3) 5m 聚合：OHLC / 总额 / delta / CVD 累计 ────────────────────────
res5 = jtc.footprint(SYM, "5m", 10, 40, cfg={}, now_ms=NOW_MS)
b5 = bar_at(res5, T0_MIN * 60)
check("聚合-5m桶存在", b5 is not None)
if b5:
    check("聚合-OHLC", b5["open"] == 100.00 and b5["high"] == 101.00
          and b5["low"] == 99.50 and b5["close"] == 100.50,
          f"o={b5['open']} h={b5['high']} l={b5['low']} c={b5['close']}")
    # buy=100+200+3000+600+100+1000+500=5500；sell=50+500+500+400+800=2250
    check("聚合-总额", abs(b5["buy"] - 5_500) < 0.01
          and abs(b5["sell"] - 2_250) < 0.01 and abs(b5["total"] - 7_750) < 0.01,
          f"buy={b5['buy']} sell={b5['sell']}")
    check("聚合-delta", abs(b5["delta"] - 3_250) < 0.01)
    # 跨分钟同档合并：100.00 档 = m0(100/50) + m1A(3000/500)
    r_merge = row_at(b5, 100.00)
    check("聚合-跨分钟同档合并", r_merge and abs(r_merge["buy"] - 3_100) < 0.01
          and abs(r_merge["sell"] - 550) < 0.01, f"row={r_merge}")
b5b = bar_at(res5, (T0_MIN + 5) * 60)
check("CVD-第二根bar累计", b5b is not None and abs(b5b["cvd"] - 3_550) < 0.01,
      f"cvd={b5b['cvd'] if b5b else None}（bar1 3250 + bar2 300)")

# ── 4) cells 上限：ingest 侧归并到边缘档（额不丢）────────────────────
_orig_cells_max = jtc.FOOT_CELLS_MAX
jtc.FOOT_CELLS_MAX = 5
m6 = (T0_MIN + 6) * 60_000
for i in range(10):  # 10 个相邻档，仅容 5 档
    feed(SYM, 100.00 + i * 0.05, 100, True, m6 + 1_000 + i * 100)
jtc.FOOT_CELLS_MAX = _orig_cells_max
res6 = jtc.footprint(SYM, "1m", 30, 40, cfg={}, now_ms=(T0_MIN + 7) * 60_000)
b6 = bar_at(res6, (T0_MIN + 6) * 60)
check("cells上限-只留5档", b6 is not None and len(b6["rows"]) == 5,
      f"rows={len(b6['rows']) if b6 else None}")
check("cells上限-总额不丢", b6 is not None and abs(b6["buy"] - 1_000) < 0.01,
      f"buy={b6['buy'] if b6 else None}")

# ── 5) buckets 上限：API 侧边缘归并（额不丢）─────────────────────────
m7 = (T0_MIN + 7) * 60_000
for i in range(8):
    feed(SYM, 100.00 + i * 0.05, 100, True, m7 + 1_000 + i * 100)
res7 = jtc.footprint(SYM, "1m", 30, 5, cfg={}, now_ms=(T0_MIN + 8) * 60_000)
b7 = bar_at(res7, (T0_MIN + 7) * 60)
check("buckets上限-归并到5档", b7 is not None and len(b7["rows"]) == 5,
      f"rows={len(b7['rows']) if b7 else None}")
check("buckets上限-总额不丢", b7 is not None and abs(b7["buy"] - 800) < 0.01,
      f"buy={b7['buy'] if b7 else None}")

# ── 6) actors 多空口径：机构全买 / 散户全卖 / overall 接筹码判词 ──────
SYM2 = "ACTUSDT"
m9 = (T0_MIN + 9) * 60_000
feed(SYM2, 100.0, 150_000, True, m9 + 1_000)   # 单笔 ≥ tier1(10万) → inst
feed(SYM2, 100.0, 1_000, False, m9 + 2_000)    # 散户卖 ×3
feed(SYM2, 100.1, 1_000, False, m9 + 3_000)
feed(SYM2, 99.9, 1_000, False, m9 + 4_000)
res_a = jtc.footprint(SYM2, "1m", 10, 40, cfg={}, now_ms=(T0_MIN + 10) * 60_000)
acts = res_a["actors"]
check("actors-inst全买", acts["inst"]["long_pct"] == 100.0
      and "做多倾向" in acts["inst"]["verdict_cn"], f"{acts['inst']}")
check("actors-retail全卖", acts["retail"]["long_pct"] == 0.0
      and "做空倾向" in acts["retail"]["verdict_cn"], f"{acts['retail']}")
check("actors-overall接筹码判词", "机构在接散户筹码" in acts["overall"]["verdict_cn"]
      and "做多情绪主导" in acts["overall"]["verdict_cn"],
      f"{acts['overall']['verdict_cn']}")
check("actors-overall数值", abs(acts["overall"]["buy"] - 150_000) < 0.01
      and abs(acts["overall"]["sell"] - 3_000) < 0.01
      and abs(acts["overall"]["delta"] - 147_000) < 0.01)
# summary() 的 breakdown 同步带 long_pct/verdict_cn（任务 3 口径）
sm = jtc.summary(SYM2, cfg={}, window_min=15,
                 now_ms=(T0_MIN + 10) * 60_000)
check("summary-breakdown带多空字段",
      sm["breakdown"]["actors"]["inst"].get("long_pct") == 100.0
      and "verdict_cn" in sm["breakdown"]["actors"]["retail"])

# ── 7) 空态 / 无效参数容错 ────────────────────────────────────────────
res_e = jtc.footprint("NOSUCHUSDT", "1m", 30, 40, cfg={}, now_ms=NOW_MS)
check("容错-空币种ok", res_e["ok"] and res_e["bars"] == []
      and res_e["active"] is False and res_e["source"] == "empty")
check("容错-空币种actors兜底", res_e["actors"]["overall"]["verdict_cn"] == "窗口内无成交数据")
res_i = jtc.footprint(SYM, "1h", 30, 40, cfg={}, now_ms=NOW_MS)
check("容错-1h非法interval", not res_i["ok"] and "interval" in res_i["error"])

# ── 8) step 漂移重算：同分钟不混档，新分钟切新 step ───────────────────
SYM3 = "DRFUSDT"
m11 = (T0_MIN + 11) * 60_000
feed(SYM3, 100.0, 100, True, m11 + 1_000)    # step=0.05 锚点 100
feed(SYM3, 300.0, 100, True, m11 + 2_000)    # 漂移 200% → pending，本分钟仍 0.05
feed(SYM3, 300.1, 100, True, m11 + 60_000 + 1_000)  # 新分钟 → step=0.2 生效
fpq = jtc._STATE[SYM3]["footprint"]
check("漂移-同分钟保持旧step", abs(fpq[0]["step"] - 0.05) < 1e-12,
      f"step0={fpq[0]['step']}")
# nice_step(300×0.0005=0.15)：0.15/0.1 浮点=1.4999…<1.5 → 归 1×10^-1=0.1
check("漂移-新分钟切新step", abs(fpq[1]["step"] - 0.1) < 1e-12,
      f"step1={fpq[1]['step']}")
res_d = jtc.footprint(SYM3, "1m", 10, 40, cfg={}, now_ms=m11 + 120_000)
total_d = sum(b["buy"] for b in res_d["bars"])
check("漂移-重桶后总额不丢", abs(total_d - 300) < 0.01, f"total={total_d}")

print()
if _FAILED:
    print(f"FAILED: {len(_FAILED)} → {_FAILED}")
    raise SystemExit(1)
print("ALL PASS")
