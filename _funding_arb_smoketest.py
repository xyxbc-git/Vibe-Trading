#!/usr/bin/env python3
"""资金费率套利模拟盘（jarvis_funding_arb）离线 smoketest：临时库 + 打桩行情，不联网。"""

from __future__ import annotations

import os
import tempfile
import time

# 隔离测试库：改写 journal DB_PATH 到临时文件（jarvis_db 对非默认路径强制走 SQLite）
_TMP = tempfile.mkdtemp(prefix="jarvis_arb_test_")
import jarvis_journal as jj

jj.DB_PATH = os.path.join(_TMP, "test.db")

import jarvis_funding_arb as jfa

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name} {extra}")


# ── 行情打桩：全部外部请求走假数据 ──────────────────────────────────────
NOW = time.time()
FAKE_SPOT = 60000.0
FAKE_MARK = 60050.0
FAKE_RATE = 0.0001  # 单期 0.01%

jfa._spot_price = lambda sym: FAKE_SPOT
jfa._premium_index_one = lambda sym: {
    "symbol": sym, "markPrice": str(FAKE_MARK),
    "lastFundingRate": str(FAKE_RATE),
    "nextFundingTime": str(int((NOW + 3600) * 1000)),
}
jfa._premium_index_all = lambda: [
    {"symbol": s, "markPrice": str(FAKE_MARK), "lastFundingRate": str(FAKE_RATE),
     "nextFundingTime": str(int((NOW + 3600) * 1000))}
    for s in jfa.WATCHLIST
]


def _fake_history(symbol, start_ms=None, end_ms=None, limit=100):
    """8h 网格上的费率历史：覆盖 start~end 的所有结算点。"""
    if start_ms is None:
        start_ms = int((NOW - 30 * 86400) * 1000)
    if end_ms is None:
        end_ms = int(NOW * 1000)
    out = []
    t = (start_ms // (jfa.SETTLE_INTERVAL * 1000) + 1) * jfa.SETTLE_INTERVAL * 1000
    while t <= end_ms:
        out.append({"fundingTime": t, "fundingRate": str(FAKE_RATE),
                    "markPrice": str(FAKE_MARK)})
        t += jfa.SETTLE_INTERVAL * 1000
    return out[:limit]


jfa._funding_history = _fake_history

# ── 1. 结算点计算 ───────────────────────────────────────────────────────
pts = jfa._settle_points_between(0, jfa.SETTLE_INTERVAL * 3)
check("3个完整周期=3个结算点", len(pts) == 3, str(pts))
check("结算点在8h网格上", all(p % jfa.SETTLE_INTERVAL == 0 for p in pts))
check("空区间无结算点", jfa._settle_points_between(100, 200) == [])

# ── 2. 机会列表（打桩行情）─────────────────────────────────────────────
opps = jfa.fetch_opportunities(force=True)
check("机会列表 ok", opps.get("ok") is True, str(opps)[:120])
check("watchlist 全覆盖", len(opps["opportunities"]) == len(jfa.WATCHLIST))
o0 = opps["opportunities"][0]
check("机会字段齐全", all(k in o0 for k in
      ("symbol", "mark_price", "funding_rate", "apr_now", "apr_7d",
       "next_funding_ts", "break_even_days", "warning")), str(o0.keys()))
check("年化口径 = rate×3×365", abs(o0["apr_now"] - FAKE_RATE * 3 * 365 * 100) < 1e-6,
      str(o0["apr_now"]))
check("回本天数>0", o0["break_even_days"] is not None and o0["break_even_days"] > 0)
check("正费率无预警", o0["warning"] is None)
check("含免责声明", bool(opps.get("disclaimer")))

# ── 3. 建仓 ─────────────────────────────────────────────────────────────
r = jfa.open_position("btc-usdt", 1000.0, note="测试")
check("建仓 ok", r.get("ok") is True, str(r)[:150])
check("symbol 规范化", r["symbol"] == "BTCUSDT", r.get("symbol"))
# qty 落库前 round(10 位小数)，容差按存储精度放宽
check("qty=本金一半/现货价", abs(r["qty"] - 500.0 / FAKE_SPOT) < 1e-9, str(r["qty"]))
check("开仓手续费>0", r["open_fee_usdt"] > 0)
pid = r["position_id"]
check("返回持仓 id", isinstance(pid, int) and pid >= 1, str(pid))

r_bad = jfa.open_position("BTCUSDT", 1.0)
check("本金过小拒单", r_bad.get("ok") is False and "本金" in r_bad.get("error", ""))
r_bad2 = jfa.open_position("BTCUSDT", "abc")
check("本金非法拒单", r_bad2.get("ok") is False)

# ── 4. 懒结算：回拨 opened_ts 模拟持仓 25 小时 ──────────────────────────
with jj._conn() as conn:
    conn.execute("UPDATE funding_arb_positions SET opened_ts = ? WHERE id = ?",
                 (NOW - 25 * 3600, pid))
s = jfa.settle_positions()
check("结算 ok", s.get("ok") is True, str(s))
check("25小时=3个结算期", s["settled"] == 3, str(s))
expected_income = (500.0 / FAKE_SPOT) * FAKE_MARK * FAKE_RATE * 3
with jj._conn() as conn:
    pos = dict(conn.execute("SELECT * FROM funding_arb_positions WHERE id = ?",
                            (pid,)).fetchone())
check("累计费率收益符合口径", abs(pos["funding_accrued_usdt"] - expected_income) < 1e-9,
      f"got={pos['funding_accrued_usdt']} want={expected_income}")
check("结算期数=3", pos["settle_count"] == 3)

s2 = jfa.settle_positions()
check("重复结算幂等（UNIQUE 防重）", s2["settled"] == 0, str(s2))

# ── 5. 持仓列表 enrich ──────────────────────────────────────────────────
lst = jfa.list_positions("open")
check("持仓列表 ok", lst.get("ok") is True)
p0 = lst["positions"][0]
check("enrich 实时字段", all(k in p0 for k in
      ("held_days", "funding_apr_pct", "net_exposure_pct",
       "current_funding_rate", "warning")), str(p0.keys()))
check("等量对冲净敞口=0", p0["net_exposure_pct"] == 0.0, str(p0["net_exposure_pct"]))
check("正费率无平仓预警", p0["warning"] is None)

# ── 6. 费率转负预警 ─────────────────────────────────────────────────────
_neg = {"symbol": "BTCUSDT", "markPrice": str(FAKE_MARK),
        "lastFundingRate": "-0.0002",
        "nextFundingTime": str(int((NOW + 3600) * 1000))}
jfa._premium_index_all = lambda: [_neg]
lst_neg = jfa.list_positions("open")
check("费率转负触发预警", lst_neg["positions"][0]["warning"] is not None,
      str(lst_neg["positions"][0].get("warning")))
neg_opp = dict(_neg)
jfa._premium_index_all = lambda: [
    {**neg_opp, "symbol": s} for s in jfa.WATCHLIST]
opps_neg = jfa.fetch_opportunities(force=True)
check("负费率机会带预警+无回本天数",
      opps_neg["opportunities"][0]["warning"] is not None
      and opps_neg["opportunities"][0]["break_even_days"] is None)

# ── 7. pnl 总览 ─────────────────────────────────────────────────────────
pnl = jfa.get_pnl()
check("pnl ok", pnl.get("ok") is True)
check("open 组含 1 笔", pnl["open"]["count"] == 1, str(pnl["open"]))
check("open 年化>0", pnl["open"]["funding_apr_pct"] > 0, str(pnl["open"]))

# ── 8. 平仓 ─────────────────────────────────────────────────────────────
c = jfa.close_position(pid)
check("平仓 ok", c.get("ok") is True, str(c)[:150])
qty = 500.0 / FAKE_SPOT
want_basis = qty * (FAKE_SPOT - FAKE_SPOT) + qty * (FAKE_MARK - FAKE_MARK)
check("基差损益口径（价格未动=0）", abs(c["basis_pnl_usdt"] - want_basis) < 1e-9,
      str(c["basis_pnl_usdt"]))
check("总盈亏=费率-手续费", abs(c["total_pnl_usdt"]
      - (c["funding_accrued_usdt"] + c["basis_pnl_usdt"] - c["fees_usdt"])) < 1e-6)
check("含已实现年化", "realized_apr_pct" in c)

c2 = jfa.close_position(pid)
check("重复平仓拒绝", c2.get("ok") is False and "已平仓" in c2.get("error", ""))
c3 = jfa.close_position(99999)
check("不存在持仓拒绝", c3.get("ok") is False)

pnl2 = jfa.get_pnl()
check("平仓后 open=0 closed=1",
      pnl2["open"]["count"] == 0 and pnl2["closed"]["count"] == 1, str(pnl2))

# ── 9. 行情失败降级 ─────────────────────────────────────────────────────
jfa._spot_price = lambda sym: None
r_fail = jfa.open_position("ETHUSDT", 1000.0)
check("行情失败建仓拒单", r_fail.get("ok") is False and "行情" in r_fail.get("error", ""))
jfa._premium_index_all = lambda: []
opps_fail = jfa.fetch_opportunities(force=True)
check("行情失败保留旧缓存或报错",
      opps_fail.get("ok") is True or "error" in opps_fail, str(opps_fail)[:100])

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
