#!/usr/bin/env python3
"""足迹历史持久化与区间回看冒烟：临时 SQLite 验证 足迹落库 / 重启后回看 /
降级纯OHLC柱 / 内存+库合并 / 区间截断 / 保留期配置 / 容错。

不联网：mock aggTrade 走 ingest → flush_bars → footprint(start/end) /
bars(start/end) 全链路（模拟「隔天回看历史足迹」场景）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time

# 先改 DB 路径再触发建表（jarvis_db 对非默认路径强制走 SQLite，天然隔离 pg 配置）
_TMP = tempfile.mkdtemp(prefix="jarvis_tapehist_")
import jarvis_tape_classify as jtc  # noqa: E402

jtc.DB_PATH = os.path.join(_TMP, "test.db")
jtc._INITED = False
jtc._LAST_PRUNE = time.time()  # 关掉 flush 内的自动保留期清理，测试里显式调 prune_old
jtc.reset_state()

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        _FAILED.append(name)


def feed(symbol: str, price: float, usd: float, is_buy: bool, ts_ms: int) -> None:
    """按目标美元额折算数量注入一笔成交（cfg={} 隔离 jarvis_config 依赖）。"""
    jtc.ingest(symbol, {"p": str(price), "q": str(usd / price),
                        "m": (not is_buy), "T": ts_ms}, cfg={})


def bar_at(res: dict, ts: int) -> dict | None:
    return next((b for b in res["bars"] if b["ts"] == ts), None)


def row_at(bar: dict, price: float) -> dict | None:
    return next((r for r in bar["rows"] if abs(r["price"] - price) < 1e-9), None)


# 基准时间：对齐 5m 桶起点；「当前」= T0 + 101 分钟（历史块 M0..M9 全部已完结）
T0_MIN = (1_720_000_000 // 300) * 300 // 60
NOW_MS = (T0_MIN + 101) * 60_000
SYM = "HISTUSDT"
M0_S = T0_MIN * 60            # 区间起点（epoch 秒）
M9_S = (T0_MIN + 9) * 60      # 区间终点

# ── 造数（价格 ~100 → step = 0.05）：M0 档位样本 / M1 失衡样本 / M2 高低价 ──
feed(SYM, 100.00, 100, True, T0_MIN * 60_000 + 1_000)       # M0 桶 100.00
feed(SYM, 100.02, 50, False, T0_MIN * 60_000 + 2_000)       # M0 桶 100.00（同档）
feed(SYM, 100.07, 200, True, T0_MIN * 60_000 + 3_000)       # M0 桶 100.05
m1 = (T0_MIN + 1) * 60_000
feed(SYM, 100.01, 3_000, True, m1 + 1_000)   # M1 A 桶 100.00 强买
feed(SYM, 100.01, 500, False, m1 + 2_000)    # M1 A 桶 100.00（比 6 倍）
feed(SYM, 100.06, 600, True, m1 + 3_000)     # M1 B 桶 100.05
feed(SYM, 100.06, 500, False, m1 + 4_000)    # M1 B 均衡
feed(SYM, 100.11, 100, True, m1 + 5_000)     # M1 C 桶 100.10
feed(SYM, 100.11, 400, False, m1 + 6_000)    # M1 C 量小不触发
m2 = (T0_MIN + 2) * 60_000
feed(SYM, 99.50, 1_000, True, m2 + 1_000)    # M2 低点
feed(SYM, 101.00, 800, False, m2 + 30_000)   # M2 高点/收盘

# ── 1) flush：分钟行 + 足迹行一起落库，幂等 ─────────────────────────────
n1 = jtc.flush_bars(now_ms=NOW_MS)
check("flush-分钟行3", n1 == 3, f"written={n1}")
with jtc._conn() as conn:
    frows = [dict(r) for r in conn.execute(
        "SELECT * FROM tape_footprint_minutes WHERE symbol=? ORDER BY minute",
        (SYM,)).fetchall()]
check("flush-足迹行3", len(frows) == 3, f"n={len(frows)}")
check("flush-足迹step", frows and abs(frows[0]["step"] - 0.05) < 1e-12,
      f"step={frows[0]['step'] if frows else None}")
cells0 = json.loads(frows[0]["cells"]) if frows else []
check("flush-cells往返", len(cells0) == 2
      and abs(cells0[0][0] - 100.00) < 1e-9 and abs(cells0[0][1] - 100) < 0.01
      and abs(cells0[0][2] - 50) < 0.01 and cells0[0][3] == 1 and cells0[0][4] == 1,
      f"cells0={cells0}")
n2 = jtc.flush_bars(now_ms=NOW_MS)
check("flush-二轮幂等", n2 == 0, f"written={n2}")

# ── 2) 降级样本：只有分钟聚合、没有足迹档位的历史分钟（模拟旧数据）──────
m5 = T0_MIN + 5
with jtc._conn() as conn:
    conn.execute(
        "INSERT INTO tape_minute_bars (symbol, minute, buy_usd, sell_usd, "
        "nr_buy_usd, nr_sell_usd, open_price, close_price, high_price, "
        "low_price, trades_n) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (SYM, m5, 500.0, 700.0, 0.0, 0.0, 100.6, 100.4, 100.7, 100.3, 4))

# ── 3) 重启后区间回看（核心场景：内存清空，纯库读历史足迹）─────────────
jtc.reset_state()
res = jtc.footprint(SYM, "1m", cfg={}, now_ms=NOW_MS, start_s=M0_S, end_s=M9_S)
check("回看-ok且source=db", res["ok"] and res["source"] == "db"
      and res["active"], f"source={res.get('source')}")
check("回看-bucket=0.05", abs((res["bucket"] or 0) - 0.05) < 1e-12)
check("回看-4根bar", len(res["bars"]) == 4,
      f"ts={[b['ts'] for b in res['bars']]}")
check("回看-range元数据", res.get("range", {}).get("start") == M0_S
      and res["range"]["end"] == M9_S and res["range"]["truncated"] is False)
b0 = bar_at(res, T0_MIN * 60)
check("回看-M0两档", b0 is not None and len(b0["rows"]) == 2)
if b0:
    r_lo = row_at(b0, 100.00)
    r_hi = row_at(b0, 100.05)
    check("回看-M0档位数值", r_lo and r_hi is not None
          and abs(r_lo["buy"] - 100) < 0.01 and abs(r_lo["sell"] - 50) < 0.01
          and abs(r_hi["buy"] - 200) < 0.01 and r_hi["sell"] == 0,
          f"lo={r_lo} hi={r_hi}")
    check("回看-M0-OHLC", b0["open"] == 100.00 and b0["close"] == 100.07)
b1 = bar_at(res, (T0_MIN + 1) * 60)
if b1:
    ra = row_at(b1, 100.00)
    rb = row_at(b1, 100.05)
    rc = row_at(b1, 100.10)
    check("回看-M1失衡flag还原", ra and ra["flag"] == "buy_imb"
          and rb and rb["flag"] is None and rc and rc["flag"] is None,
          f"A={ra} B={rb} C={rc}")
else:
    check("回看-M1bar存在", False)
b5 = bar_at(res, m5 * 60)
check("回看-降级bar无档位", b5 is not None and b5["rows"] == [],
      f"rows={len(b5['rows']) if b5 else None}")
check("回看-降级bar额/笔数取分钟行", b5 is not None
      and abs(b5["buy"] - 500) < 0.01 and abs(b5["sell"] - 700) < 0.01
      and b5["trades"] == 4 and b5["open"] == 100.6,
      f"buy={b5['buy'] if b5 else None}")
# CVD 连续性：M0 +250 / M1 +2300 / M2 +200 / M5(降级) -200 → 2550
check("回看-CVD跨降级bar连续", b5 is not None and abs(b5["cvd"] - 2_550) < 0.01,
      f"cvd={b5['cvd'] if b5 else None}")
# actors 二分口径：全部散单（<$10k）→ retail 全额，inst 0
acts = res["actors"]
check("回看-actors散户全额", abs(acts["retail"]["buy"] - 5_500) < 0.01
      and abs(acts["retail"]["sell"] - 2_950) < 0.01
      and acts["inst"]["buy"] == 0.0, f"retail={acts['retail']}")
check("回看-actors-overall", abs(acts["overall"]["delta"] - 2_550) < 0.01
      and "做多" in acts["overall"]["verdict_cn"],
      f"{acts['overall']['verdict_cn']}")
check("回看-mid并入注明", "并入" in acts["mid"]["verdict_cn"])

# ── 3.5) 实时模式（无 start/end）重启后也能从库补深度 ───────────────────
# 场景：重启后内存为空，5m 聚合此前只剩「当前几分钟」→ 现在并入库内历史。
res_live = jtc.footprint(SYM, "5m", limit=10, cfg={},
                         now_ms=(T0_MIN + 6) * 60_000)
check("实时补深-source=db", res_live["ok"] and res_live["source"] == "db",
      f"source={res_live.get('source')}")
check("实时补深-2根5m柱", len(res_live["bars"]) == 2,
      f"ts={[b['ts'] for b in res_live['bars']]}")
lb0 = bar_at(res_live, T0_MIN * 60)
lb5 = bar_at(res_live, (T0_MIN + 5) * 60)
check("实时补深-历史柱带档位", lb0 is not None and len(lb0["rows"]) >= 3,
      f"rows={len(lb0['rows']) if lb0 else None}")
check("实时补深-降级柱并入", lb5 is not None and lb5["rows"] == []
      and abs(lb5["buy"] - 500) < 0.01)

# ── 4) 内存+库合并：新到未落盘分钟并入区间（同分钟内存优先）────────────
m8 = (T0_MIN + 8) * 60_000
feed(SYM, 100.20, 900, True, m8 + 1_000)
res2 = jtc.footprint(SYM, "1m", cfg={}, now_ms=NOW_MS, start_s=M0_S, end_s=M9_S)
check("合并-source=db+mem", res2["source"] == "db+mem",
      f"source={res2.get('source')}")
b8 = bar_at(res2, (T0_MIN + 8) * 60)
check("合并-内存bar带档位", b8 is not None and len(b8["rows"]) == 1
      and abs(b8["buy"] - 900) < 0.01, f"b8={b8 and b8['buy']}")

# ── 5) bars() 区间模式：窗口边界 + 窗口外内存分钟排除 ───────────────────
feed(SYM, 100.30, 1_000, True, (T0_MIN + 200) * 60_000 + 1_000)  # 窗口外
resb = jtc.bars(SYM, "1m", now_ms=NOW_MS, start_s=M0_S, end_s=M9_S)
check("bars区间-ok", resb["ok"] and resb.get("range", {}).get("end") == M9_S)
ts_list = [b["ts"] for b in resb["bars"]]
check("bars区间-5根且窗口外排除", len(ts_list) == 5
      and (T0_MIN + 200) * 60 not in ts_list
      and (T0_MIN + 8) * 60 in ts_list, f"ts={ts_list}")

# ── 6) 区间截断：超 RANGE_BARS_MAX 截尾保留近端 ────────────────────────
wide_end = M0_S + jtc.RANGE_BARS_MAX * 2 * 60
res3 = jtc.footprint(SYM, "1m", cfg={}, now_ms=NOW_MS,
                     start_s=M0_S, end_s=wide_end)
check("截断-truncated标记", res3["ok"] and res3["range"]["truncated"] is True)
span_bars = (res3["range"]["end"] - res3["range"]["start"]) // 60 + 1
check("截断-窗口=RANGE_BARS_MAX", span_bars == jtc.RANGE_BARS_MAX,
      f"span={span_bars}")

# ── 7) 保留期配置：tape_retention_days 生效，两表一起清 ─────────────────
jtc.reset_state()
old_min = NOW_MS // 60_000 - 2 * 24 * 60  # 2 天前
feed(SYM, 50.0, 1_000, True, old_min * 60_000 + 1_000)
jtc.flush_bars(now_ms=NOW_MS)
with jtc._conn() as conn:
    n_before = conn.execute(
        "SELECT COUNT(*) AS n FROM tape_footprint_minutes WHERE minute=?",
        (old_min,)).fetchone()["n"]
check("保留期-旧足迹行已落库", n_before == 1)
removed = jtc.prune_old(now_ms=NOW_MS, cfg={"tape_retention_days": 1})
check("保留期-清理旧分钟行", removed == 1, f"removed={removed}")
with jtc._conn() as conn:
    n_after = conn.execute(
        "SELECT COUNT(*) AS n FROM tape_footprint_minutes WHERE minute=?",
        (old_min,)).fetchone()["n"]
    n_keep = conn.execute(
        "SELECT COUNT(*) AS n FROM tape_minute_bars WHERE symbol=?",
        (SYM,)).fetchone()["n"]
check("保留期-旧足迹行同步清理", n_after == 0)
# 留库 = M0/M1/M2/M5（M8 在 reset_state 前未 flush，随内存丢弃——符合设计）
check("保留期-窗口内不误删", n_keep == 4, f"keep={n_keep}")

# ── 8) 配置读取与容错 ──────────────────────────────────────────────────
check("配置-默认30", jtc._retention_days({}) == 30)
check("配置-显式7", jtc._retention_days({"tape_retention_days": 7}) == 7)
check("配置-非法回退", jtc._retention_days({"tape_retention_days": "x"}) == 30
      and jtc._retention_days({"tape_retention_days": 0}) == 30)
check("容错-坏cells返回空", jtc._cells_from_json("not json") == {}
      and jtc._cells_from_json(None) == {})
res4 = jtc.footprint("NOSUCHUSDT", "1m", cfg={}, now_ms=NOW_MS,
                     start_s=M0_S, end_s=M9_S)
check("容错-空币种区间ok", res4["ok"] and res4["bars"] == []
      and res4["active"] is False and res4["source"] == "empty")
res5 = jtc.footprint(SYM, "1m", cfg={}, now_ms=NOW_MS,
                     start_s=M9_S, end_s=M0_S)  # end < start → 收敛到单桶
check("容错-倒序区间不崩", res5["ok"], f"err={res5.get('error')}")
res6 = jtc.footprint(SYM, "1h", cfg={}, now_ms=NOW_MS,
                     start_s=M0_S, end_s=M9_S)
check("容错-非法interval", not res6["ok"] and "interval" in res6["error"])

print()
if _FAILED:
    print(f"FAILED: {len(_FAILED)} → {_FAILED}")
    raise SystemExit(1)
print("ALL PASS")
