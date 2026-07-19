#!/usr/bin/env python3
"""成交流分钟聚合持久化冒烟：临时 SQLite 验证 flush 幂等 / 多周期聚合 / 内存+库合并 / 保留期 / 容错。

不联网：直接注入 mock aggTrade 走 ingest → flush_bars → bars 全链路。
"""

from __future__ import annotations

import os
import tempfile
import time

# 先改 DB 路径再触发建表（jarvis_db 对非默认路径强制走 SQLite，天然隔离 pg 配置）
_TMP = tempfile.mkdtemp(prefix="jarvis_tapebars_")
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


def trade(price: float, qty: float, is_buy: bool, ts_ms: int) -> dict:
    """构造 aggTrade 原始消息（m=True 表示买方为 maker = 主动卖出）。"""
    return {"p": str(price), "q": str(qty), "m": (not is_buy), "T": ts_ms}


def feed(symbol: str, price: float, usd: float, is_buy: bool, ts_ms: int) -> None:
    """按目标美元额折算数量注入一笔成交（cfg={} 隔离 jarvis_config 依赖）。"""
    jtc.ingest(symbol, trade(price, usd / price, is_buy, ts_ms), cfg={})


def db_rows(sym: str) -> list[dict]:
    with jtc._conn() as conn:
        cur = conn.execute(
            "SELECT * FROM tape_minute_bars WHERE symbol=? ORDER BY minute ASC",
            (sym,))
        return [dict(r) for r in cur.fetchall()]


# 基准时间：对齐到 5 分钟桶起点，保证 5 个 1m 桶落进同一个 5m bar
T0_MIN = (1_720_000_000 // 300) * 300 // 60   # epoch 分钟，且被 5 整除
NOW_MS = (T0_MIN + 5) * 60_000                # 「当前」= 第 6 分钟起点

SYM = "BTCUSDT"

# ── 造数：5 个完结分钟（m0..m4）+ 1 个当前未完结分钟（m5）───────────────
# m0: 买 30k(非散户) + 卖 5k(散户)   价格 100 → 110（high 110 low 100）
# m1..m4: 每分钟 买 1k 卖 2k（散户），价格 105/104/103/102
feed(SYM, 100.0, 30_000, True, T0_MIN * 60_000 + 1_000)
feed(SYM, 110.0, 5_000, False, T0_MIN * 60_000 + 30_000)
for i in range(1, 5):
    px = 105.0 - i
    feed(SYM, px, 1_000, True, (T0_MIN + i) * 60_000 + 1_000)
    feed(SYM, px, 2_000, False, (T0_MIN + i) * 60_000 + 30_000)
# 当前未完结分钟 m5：买 7k
feed(SYM, 99.0, 7_000, True, (T0_MIN + 5) * 60_000 + 5_000)

# ── 1) flush 幂等：两次 flush 不重复、不写当前未完结分钟 ────────────────
n1 = jtc.flush_bars(now_ms=NOW_MS)
check("flush-首轮写5行", n1 == 5, f"written={n1}")
rows = db_rows(SYM)
check("flush-库内5行", len(rows) == 5, f"rows={len(rows)}")
check("flush-未完结分钟不落盘", all(r["minute"] < T0_MIN + 5 for r in rows))
n2 = jtc.flush_bars(now_ms=NOW_MS)
check("flush-二轮无新增(幂等)", n2 == 0, f"written={n2}")
check("flush-二轮后仍5行", len(db_rows(SYM)) == 5)

# ── 2) 落库字段正确性（m0 行）──────────────────────────────────────────
r0 = rows[0]
check("字段-买卖额", abs(r0["buy_usd"] - 30_000) < 1e-6
      and abs(r0["sell_usd"] - 5_000) < 1e-6, f"{r0['buy_usd']}/{r0['sell_usd']}")
check("字段-非散户额", abs(r0["nr_buy_usd"] - 30_000) < 1e-6
      and abs(r0["nr_sell_usd"] - 0.0) < 1e-6)
check("字段-OHLC", r0["open_price"] == 100.0 and r0["close_price"] == 110.0
      and r0["high_price"] == 110.0 and r0["low_price"] == 100.0)
check("字段-笔数", r0["trades_n"] == 2)

# ── 3) 聚合正确性：5 个 1m 桶 → 一个 5m bar ────────────────────────────
res = jtc.bars(SYM, "5m", 10, now_ms=NOW_MS)
check("聚合-ok", res["ok"] and res["interval"] == "5m")
b5 = [b for b in res["bars"] if b["ts"] == T0_MIN * 60]
check("聚合-5m桶存在", len(b5) == 1, f"ts列表={[b['ts'] for b in res['bars']]}")
if b5:
    b = b5[0]
    # 额：买 30k+4×1k=34k；卖 5k+4×2k=13k；净 +21k
    check("聚合-买卖净额", abs(b["buy"] - 34_000) < 0.01
          and abs(b["sell"] - 13_000) < 0.01 and abs(b["net"] - 21_000) < 0.01,
          f"buy={b['buy']} sell={b['sell']} net={b['net']}")
    check("聚合-非散户净额", abs(b["nr_buy"] - 30_000) < 0.01
          and abs(b["nr_net"] - 30_000) < 0.01)
    # OHLC：open=首分钟首价100；close=末分钟末价101（m4 价 101）；high=110（m0）；low=100（m0）
    check("聚合-OHLC", b["open"] == 100.0 and b["close"] == 101.0
          and b["high"] == 110.0 and b["low"] == 100.0,
          f"o={b['open']} h={b['high']} l={b['low']} c={b['close']}")
    check("聚合-笔数", b["trades"] == 10, f"trades={b['trades']}")

# ── 4) 内存+库合并去重：已落库分钟仍在内存，bars 不得双计 ──────────────
res1 = jtc.bars(SYM, "1m", 10, now_ms=NOW_MS)
check("合并-source标记", res1["source"] == "db+mem", f"source={res1['source']}")
bar_m0 = [b for b in res1["bars"] if b["ts"] == T0_MIN * 60]
check("合并-m0不双计", bar_m0 and abs(bar_m0[0]["buy"] - 30_000) < 0.01,
      f"buy={bar_m0[0]['buy'] if bar_m0 else None}")
bar_m5 = [b for b in res1["bars"] if b["ts"] == (T0_MIN + 5) * 60]
check("合并-含当前未完结分钟", bar_m5 and abs(bar_m5[0]["buy"] - 7_000) < 0.01)

# ── 5) 重启场景：内存清空后 bars 仍能从库读历史 ───────────────────────
jtc.reset_state()
res2 = jtc.bars(SYM, "1m", 10, now_ms=NOW_MS)
check("重启-纯库读", res2["ok"] and res2["source"] == "db"
      and len(res2["bars"]) == 5, f"source={res2['source']} n={len(res2['bars'])}")
check("重启-未完结分钟随内存丢失",
      all(b["ts"] != (T0_MIN + 5) * 60 for b in res2["bars"]))

# ── 6) 保留期：14 天前的行被 prune_old 清掉 ────────────────────────────
old_min = T0_MIN - jtc.RETENTION_DAYS * 24 * 60 - 10
feed(SYM, 50.0, 1_000, True, old_min * 60_000 + 1_000)
jtc.flush_bars(now_ms=NOW_MS)
check("保留期-旧行已落库", any(r["minute"] == old_min for r in db_rows(SYM)))
removed = jtc.prune_old(now_ms=NOW_MS)
check("保留期-清理1行", removed == 1, f"removed={removed}")
check("保留期-窗口内不误删", len(db_rows(SYM)) == 5)

# ── 7) 容错：空币种 / 无效 interval / 空库空桶 ─────────────────────────
jtc.reset_state()
res3 = jtc.bars("NOSUCHUSDT", "1h", 100, now_ms=NOW_MS)
check("容错-空数据ok", res3["ok"] and res3["bars"] == [] and res3["source"] == "empty")
res4 = jtc.bars(SYM, "2h", 100, now_ms=NOW_MS)
check("容错-无效interval", not res4["ok"] and "interval" in res4.get("error", ""))
check("容错-空桶flush为0", jtc.flush_bars(now_ms=NOW_MS) == 0)
res5 = jtc.bars(SYM, "1d", 3, now_ms=NOW_MS)
check("容错-1d聚合可用", res5["ok"] and len(res5["bars"]) >= 1)

# ── 8) limit 窗口截断：锚定当前时间往回 limit 个桶，只返回窗口内数据 ────
res6 = jtc.bars(SYM, "1m", 3, now_ms=NOW_MS)
check("limit-窗口只覆盖最新3桶", [b["ts"] for b in res6["bars"]]
      == [(T0_MIN + 3) * 60, (T0_MIN + 4) * 60],
      f"ts={[b['ts'] for b in res6['bars']]}")

print()
if _FAILED:
    print(f"FAILED: {len(_FAILED)} → {_FAILED}")
    raise SystemExit(1)
print("ALL PASS")
