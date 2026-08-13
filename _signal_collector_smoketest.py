#!/usr/bin/env python3
"""[T10] 常驻信号采集器离线 smoketest：不联网、临时 SQLite。

覆盖：纯函数（TF 解析 / 收盘 bar 解析 / 缺口序列）/ 配置键三处登记 /
台账幂等与保留期清理 / 首事件种子 + 首记录 / 方向翻转记变更 /
重复事件与 state 新鲜度守卫 / WS 断线缺口回补（完整回放 + 截断重建两路径）/
回放不偷看未来 / 种子失败降级 / 队列满丢弃 / jws.dispatch 端到端接线 /
覆盖率报表结构与验收判定。

隔离手法与 _signal_history_smoketest 同款：先改 DB 路径再触发建表；
REST（种子/回补）与 12 系统计算全部 monkeypatch，零出网。
"""

from __future__ import annotations

import json
import os
import queue
import tempfile
import time

import pandas as pd

_TMP = tempfile.mkdtemp(prefix="jarvis_sigcol_")

import jarvis_signal_collector as jsc  # noqa: E402
import jarvis_signal_history as jsh  # noqa: E402

jsc.DB_PATH = os.path.join(_TMP, "test.db")
jsc._INITED = False
jsh.DB_PATH = jsc.DB_PATH  # 信号库与台账同库（与生产一致）
jsh._INITED = False

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


# ── 0. 测试桩：REST 取数 / 12 系统计算 ─────────────────────────────
TFMS = 300_000
T0 = 1_755_000_000_000  # 对齐 5m/15m 边界（/300000 与 /900000 均整除）

FETCH: dict = {"rows": None, "calls": 0}
DIR: dict = {"v": "bullish"}
SEEN_DF_LENS: list[int] = []
SEEN_DF_LAST_TIME: list[int] = []


def mk_bars(end_open_ms: int, n: int, tf_ms: int = TFMS) -> list[dict]:
    out = []
    for i in range(n):
        t = end_open_ms - (n - 1 - i) * tf_ms
        px = 100.0 + i * 0.1
        out.append({"time": t, "open": px, "high": px + 0.5, "low": px - 0.5,
                    "close": px + 0.2, "volume": 10.0})
    return out


def fake_fetch(sym, tf, limit):
    FETCH["calls"] += 1
    rows = FETCH["rows"]
    if rows is None:
        return None
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


def fake_run_all(df, basis_data=None, trade_history=None):
    SEEN_DF_LENS.append(len(df))
    SEEN_DF_LAST_TIME.append(int(df["time"].iloc[-1]))
    return [{"system": "turtle", "name_cn": "海龟", "direction": DIR["v"],
             "strength": 0.6, "reasoning": "测试", "key_levels": [],
             "trade_plan": None},
            {"system": "arbitrage", "name_cn": "套利", "direction": "neutral",
             "strength": 0.0, "reasoning": "数据不足", "key_levels": [],
             "trade_plan": None}]


jsc._paced_fetch_df = fake_fetch
import jarvis_twelve_systems as jts  # noqa: E402

jts.run_all = fake_run_all

# 采集器内部状态手工装配（不走 start()：start 会拉起真实 WS）
jsc._OPTS.update(backfill_bars=288, basis=False, tape=False,
                 queue_max=64, ledger_retention_days=45)
jsc._ACTIVE_SYMBOLS.update({"BTCUSDT"})
jsc._ACTIVE_TFS.update({"5m"})
jsc._Q = queue.Queue(maxsize=64)

# ── 1. 纯函数 ──────────────────────────────────────────────────────
check("parse_tfs 过滤非法+去重",
      jsc.parse_tfs("5m,15m,bogus,4h,5m") == ["5m", "15m", "4h"])
check("parse_tfs 空回默认集", jsc.parse_tfs("") == list(jsc.DEFAULT_TFS)
      and jsc.parse_tfs(None) == list(jsc.DEFAULT_TFS))
check("parse_tfs 列表输入", jsc.parse_tfs(["1h", "4h"]) == ["1h", "4h"])

k_closed = {"k": {"x": True, "i": "5m", "t": T0, "o": "100", "h": "101",
                  "l": "99", "c": "100.5", "v": "12.3"}}
parsed = jsc.closed_bar_from_kline(k_closed)
check("收盘 bar 解析", parsed is not None and parsed[0] == "5m"
      and parsed[1]["time"] == T0 and parsed[1]["close"] == 100.5, str(parsed))
check("进行中 bar 丢弃",
      jsc.closed_bar_from_kline({"k": dict(k_closed["k"], x=False)}) is None)
check("未知周期丢弃",
      jsc.closed_bar_from_kline({"k": dict(k_closed["k"], i="2m")}) is None)
check("字段缺失不抛", jsc.closed_bar_from_kline({"k": {"x": True, "i": "5m"}}) is None
      and jsc.closed_bar_from_kline({}) is None)

check("缺口-无缺口", jsc.missing_opens(T0, T0 + TFMS, TFMS, 10) == ([], 0))
check("缺口-3根全回", jsc.missing_opens(T0, T0 + 4 * TFMS, TFMS, 10)
      == ([T0 + TFMS, T0 + 2 * TFMS, T0 + 3 * TFMS], 0))
check("缺口-超上限截断保最新", jsc.missing_opens(T0, T0 + 6 * TFMS, TFMS, 2)
      == ([T0 + 4 * TFMS, T0 + 5 * TFMS], 3))
check("缺口-cap0 全丢", jsc.missing_opens(T0, T0 + 4 * TFMS, TFMS, 0) == ([], 3))

# ── 2. 配置键三处登记 ─────────────────────────────────────────────
import jarvis_config as jc  # noqa: E402

_SIGCOL_KEYS = ("sigcol_enabled", "sigcol_tfs", "sigcol_backfill_bars",
                "sigcol_queue_max", "sigcol_ledger_retention_days",
                "sigcol_tape", "sigcol_basis")
check("sigcol 键 DEFAULTS 齐全", all(k in jc.DEFAULTS for k in _SIGCOL_KEYS),
      str([k for k in _SIGCOL_KEYS if k not in jc.DEFAULTS]))
check("sigcol 键 GROUPS 齐全", all(k in jc.GROUPS for k in _SIGCOL_KEYS))
check("sigcol 数值键 BOUNDS 齐全", all(k in jc.BOUNDS for k in
      ("sigcol_backfill_bars", "sigcol_queue_max", "sigcol_ledger_retention_days")))
check("流水上限键三处登记", "signal_history_max_rows" in jc.DEFAULTS
      and "signal_history_max_rows" in jc.GROUPS
      and "signal_history_max_rows" in jc.BOUNDS)
check("回补上限夹护栏", jc.clamp("sigcol_backfill_bars", 10 ** 6) == 480
      and jc.clamp("sigcol_backfill_bars", -5) == 0)

# ── 3. 台账：幂等 + 保留期清理 ────────────────────────────────────
jsc.init_db()
ok1 = jsc.ledger_record("BTCUSDT", "5m", T0 - 10 * TFMS, T0 - 9 * TFMS, "ws",
                        n_signals=2, n_changed=0, lag_ms=100)
ok2 = jsc.ledger_record("BTCUSDT", "5m", T0 - 10 * TFMS, T0 - 9 * TFMS, "ws",
                        n_signals=2, n_changed=0, lag_ms=100)
check("台账-首插成功", ok1 is True)
check("台账-重复幂等拒绝", ok2 is False)
check("台账-断点查询", jsc._ledger_last_open("BTCUSDT", "5m") == T0 - 10 * TFMS)
with jsc._conn() as _c:
    _c.execute("UPDATE signal_collect_log SET ts=?", (time.time() - 30 * 86400,))
removed = jsc.ledger_prune(7)
check("台账-保留期清理", removed == 1, f"removed={removed}")
check("台账-清后断点为空", jsc._ledger_last_open("BTCUSDT", "5m") is None)

# ── 4. 首事件：种子 + 首记录（建 state 不记流水）───────────────────
FETCH["rows"] = mk_bars(T0 - TFMS, 60)  # 种子止于事件前一根
bar_t0 = {"time": T0, "open": 100.0, "high": 101.0, "low": 99.0,
          "close": 100.5, "volume": 12.0}
jsc._process_event("BTCUSDT", "5m", bar_t0)
check("种子-完成", ("BTCUSDT", "5m") in jsc._SEEDED
      and len(jsc._HIST[("BTCUSDT", "5m")]) == 61)
check("种子-REST 恰一次", FETCH["calls"] == 1, str(FETCH["calls"]))


def _ledger_rows():
    with jsc._conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM signal_collect_log ORDER BY bar_open_ms").fetchall()]


rows = _ledger_rows()
check("首事件-台账 1 行 ws", len(rows) == 1 and rows[0]["source"] == "ws"
      and rows[0]["n_signals"] == 1, str(rows))
check("首事件-arbitrage 无基差被剔除", rows[0]["n_signals"] == 1)
check("首事件-首见无变更", rows[0]["n_changed"] == 0
      and jsh.history("BTCUSDT", "5m")["total"] == 0)

# ── 5. 方向翻转：记变更流水 ───────────────────────────────────────
DIR["v"] = "bearish"
bar_t1 = dict(bar_t0, time=T0 + TFMS)
jsc._process_event("BTCUSDT", "5m", bar_t1)
h = jsh.history("BTCUSDT", "5m")
check("翻转-流水 1 条", h["total"] == 1, f"total={h['total']}")
check("翻转-ts=bar 收盘时刻", h["rows"][0]["ts"] == (T0 + 2 * TFMS) / 1000.0,
      str(h["rows"][0]["ts"]))
check("翻转-price=bar 收盘价", h["rows"][0]["price"] == 100.5)
rows = _ledger_rows()
check("翻转-台账 n_changed=1", rows[-1]["n_changed"] == 1, str(rows[-1]))

# ── 6. 重复事件：state 守卫 + 台账幂等，零新增 ────────────────────
jsc._process_event("BTCUSDT", "5m", dict(bar_t1))
check("重复-流水不增", jsh.history("BTCUSDT", "5m")["total"] == 1)
check("重复-台账不增", len(_ledger_rows()) == 2)
check("重复-守卫计数", jsc._STATS["skipped_state_fresher"] >= 1)

# ── 7. 守卫：旁路（dashboard）先写更新状态 → 采集器跳过写库 ────────
future_ts = (T0 + 10 * TFMS) / 1000.0
jsh.record_batch("BTCUSDT", "5m",
                 [{"system": "turtle", "name_cn": "海龟", "direction": "bearish",
                   "strength": 0.6, "reasoning": "旁路", "key_levels": [],
                   "trade_plan": None}], price=101.0, now=future_ts)
bar_t2 = dict(bar_t0, time=T0 + 2 * TFMS)
jsc._process_event("BTCUSDT", "5m", bar_t2)
rows = _ledger_rows()
check("守卫-台账记 state_fresher", rows[-1]["note"] == "state_fresher"
      and rows[-1]["bar_open_ms"] == T0 + 2 * TFMS, str(rows[-1]))
check("守卫-不写流水", jsh.history("BTCUSDT", "5m")["total"] == 1)
# 撤掉未来状态，避免影响后续用例（直接把 state 时间戳拨回）
with jsh._conn() as _c:
    _c.execute("UPDATE twelve_signal_state SET updated_ts=? WHERE symbol=? AND tf=?",
               ((T0 + 3 * TFMS) / 1000.0, "BTCUSDT", "5m"))

# ── 8. 缺口回补：完整回放路径（含回放不偷看未来）─────────────────
DIR["v"] = "bullish"
FETCH["rows"] = mk_bars(T0 + 7 * TFMS, 60)  # 覆盖缺口 3..6 + 事件前根
SEEN_DF_LAST_TIME.clear()
bar_t7 = dict(bar_t0, time=T0 + 7 * TFMS)
jsc._process_event("BTCUSDT", "5m", bar_t7)  # 上根 open=T0+2 → 缺 3,4,5,6
rows = _ledger_rows()
srcs = [r["source"] for r in rows]
check("回补-4 根 backfill + 1 根 ws", srcs.count("backfill") == 4
      and srcs.count("ws") == 4, str(srcs))
check("回补-回放次序不偷看未来",
      SEEN_DF_LAST_TIME == [T0 + i * TFMS for i in (3, 4, 5, 6, 7)],
      str(SEEN_DF_LAST_TIME))
check("回补-方向翻转恰记 1 次",
      jsh.history("BTCUSDT", "5m")["total"] == 2)
hist_times = [b["time"] for b in jsc._HIST[("BTCUSDT", "5m")]]
check("回补-内存史连续", all(b - a == TFMS for a, b in
      zip(hist_times[:-1], hist_times[1:])) and hist_times[-1] == T0 + 7 * TFMS)

# ── 9. 缺口回补：超上限截断 → 重建路径 ────────────────────────────
jsc._OPTS["backfill_bars"] = 2
FETCH["rows"] = mk_bars(T0 + 13 * TFMS, 60)
bar_t14 = dict(bar_t0, time=T0 + 14 * TFMS)
jsc._process_event("BTCUSDT", "5m", bar_t14)  # 缺 8..13 共 6 根 > cap2
rows = _ledger_rows()
check("截断-只回放最新 2 根", [r["bar_open_ms"] for r in rows if r["source"] == "backfill"][-2:]
      == [T0 + 12 * TFMS, T0 + 13 * TFMS], str([r["bar_open_ms"] for r in rows]))
check("截断-丢失计数", jsc._STATS["gap_bars_lost"] >= 4,
      str(jsc._STATS["gap_bars_lost"]))
hist_times = [b["time"] for b in jsc._HIST[("BTCUSDT", "5m")]]
check("截断-重建后史连续尾对齐", hist_times[-1] == T0 + 14 * TFMS
      and all(b - a == TFMS for a, b in zip(hist_times[:-1], hist_times[1:])))
jsc._OPTS["backfill_bars"] = 288

# ── 10. 种子失败：纯 WS 降级起窗（史不足如实记，不算已采）───────────
FETCH["rows"] = None
jsc._ACTIVE_SYMBOLS.add("ETHUSDT")
jsc._process_event("ETHUSDT", "5m", dict(bar_t0))
eth_rows = [r for r in _ledger_rows() if r["symbol"] == "ETHUSDT"]
check("种子失败-降级起窗仍记台账", len(eth_rows) == 1
      and eth_rows[0]["note"] == "insufficient_history"
      and eth_rows[0]["n_signals"] is None, str(eth_rows))
check("种子失败-计数+降级登记", jsc._STATS["seed_fails"] >= 1
      and ("ETHUSDT", "5m") in jsc._SEED_DEGRADED)
check("种子失败-已标记种子（WS 累积中）", ("ETHUSDT", "5m") in jsc._SEEDED
      and len(jsc._HIST[("ETHUSDT", "5m")]) == 1)

# ── 10b. 陈旧缓存种子：新鲜度检查识破，同样降级（封禁期实测场景）────
jsc._ACTIVE_SYMBOLS.add("XRPUSDT")
FETCH["rows"] = mk_bars(T0 - 2000 * TFMS, 300)  # 一周前的缓存窗
jsc._process_event("XRPUSDT", "5m", dict(bar_t0))
check("陈旧种子-拒用降级", ("XRPUSDT", "5m") in jsc._SEED_DEGRADED
      and len(jsc._HIST[("XRPUSDT", "5m")]) == 1
      and jsc._STATS["seed_degraded"] >= 1)
xrp_rows = [r for r in _ledger_rows() if r["symbol"] == "XRPUSDT"]
check("陈旧种子-史不足如实记", len(xrp_rows) == 1
      and xrp_rows[0]["note"] == "insufficient_history", str(xrp_rows))

# ── 10c. 降级窗重播种：REST 恢复后自动回全窗 ──────────────────────
jsc._SEED_DEGRADED[("XRPUSDT", "5m")] = time.time() - 1  # 冷却已到
FETCH["rows"] = mk_bars(T0, 300)  # 新鲜窗（贴到事件前一根 T0）
jsc._process_event("XRPUSDT", "5m", dict(bar_t0, time=T0 + TFMS))
check("重播种-恢复全窗", ("XRPUSDT", "5m") not in jsc._SEED_DEGRADED
      and len(jsc._HIST[("XRPUSDT", "5m")]) >= 300,
      f"hist={len(jsc._HIST[('XRPUSDT', '5m')])}")
xrp_rows = [r for r in _ledger_rows() if r["symbol"] == "XRPUSDT"]
check("重播种-本根正常计算", xrp_rows[-1]["bar_open_ms"] == T0 + TFMS
      and xrp_rows[-1]["n_signals"] == 1, str(xrp_rows[-1]))

# ── 10d. 小缺口回补陈旧/失败：本根缓期占位，回补成功后台账升级 ──────
jsc._ACTIVE_SYMBOLS.add("BNBUSDT")
FETCH["rows"] = mk_bars(T0, 300)
jsc._process_event("BNBUSDT", "5m", dict(bar_t0))          # 正常种子+首记录
FETCH["rows"] = mk_bars(T0 - 1000 * TFMS, 60)              # 回补拉到陈旧缓存
jsc._process_event("BNBUSDT", "5m", dict(bar_t0, time=T0 + 3 * TFMS))  # 缺 1,2
bnb = [r for r in _ledger_rows() if r["symbol"] == "BNBUSDT"]
check("缓期-占位行 gap_unfilled", bnb[-1]["note"] == "gap_unfilled"
      and bnb[-1]["n_signals"] is None
      and bnb[-1]["bar_open_ms"] == T0 + 3 * TFMS, str(bnb[-1]))
check("缓期-断层窗未入史未计算",
      [b["time"] for b in jsc._HIST[("BNBUSDT", "5m")]][-1] == T0
      and jsc._STATS["bars_deferred"] >= 1)
jsc._BACKFILL_FAIL.clear()                                  # 跳过 60s 冷却
FETCH["rows"] = mk_bars(T0 + 4 * TFMS, 60)                  # 新鲜窗覆盖缺口
jsc._process_event("BNBUSDT", "5m", dict(bar_t0, time=T0 + 5 * TFMS))  # 缺 1..4
bnb = {r["bar_open_ms"]: r for r in _ledger_rows() if r["symbol"] == "BNBUSDT"}
check("缓期-回补后占位升级为真实行",
      bnb[T0 + 3 * TFMS]["n_signals"] == 1
      and bnb[T0 + 3 * TFMS]["source"] == "backfill"
      and bnb[T0 + 3 * TFMS]["note"] is None, str(bnb.get(T0 + 3 * TFMS)))
check("缓期-缺口全部补齐", all(T0 + i * TFMS in bnb for i in range(6)),
      str(sorted(bnb)))
bnb_hist = [b["time"] for b in jsc._HIST[("BNBUSDT", "5m")]]
check("缓期-补后史连续", bnb_hist[-1] == T0 + 5 * TFMS
      and all(b - a == TFMS for a, b in zip(bnb_hist[:-1], bnb_hist[1:])))

# ── 10e. 大缺口 + 回补不可用：弃断层旧史改纯 WS 重建 ───────────────
jsc._ACTIVE_SYMBOLS.add("DOGEUSDT")
FETCH["rows"] = mk_bars(T0, 300)
jsc._process_event("DOGEUSDT", "5m", dict(bar_t0))
FETCH["rows"] = None                                        # REST 全挂
big_jump = T0 + 400 * TFMS                                  # 缺 399 根 > cap 288
jsc._process_event("DOGEUSDT", "5m", dict(bar_t0, time=big_jump))
doge_hist = [b["time"] for b in jsc._HIST[("DOGEUSDT", "5m")]]
check("大缺口-弃旧史纯 WS 重建", doge_hist == [big_jump]
      and ("DOGEUSDT", "5m") in jsc._SEED_DEGRADED, str(doge_hist[-3:]))
doge = [r for r in _ledger_rows() if r["symbol"] == "DOGEUSDT"]
check("大缺口-本根史不足如实记", doge[-1]["bar_open_ms"] == big_jump
      and doge[-1]["note"] == "insufficient_history", str(doge[-1]))

# ── 11. WS 回调：过滤 + 队列满丢弃 ────────────────────────────────
small_q = queue.Queue(maxsize=2)
jsc._Q = small_q
seen0 = jsc._STATS["events_seen"]
jsc._on_kline("BTCUSDT", {"k": dict(k_closed["k"], x=False)})  # 进行中：忽略
check("回调-进行中 bar 忽略", jsc._STATS["events_seen"] == seen0)
jsc._on_kline("SOLUSDT", k_closed)  # 非活跃币：忽略
check("回调-非活跃币忽略", jsc._STATS["events_seen"] == seen0)
jsc._on_kline("BTCUSDT", k_closed)
jsc._on_kline("BTCUSDT", {"k": dict(k_closed["k"], t=T0 + TFMS)})
jsc._on_kline("BTCUSDT", {"k": dict(k_closed["k"], t=T0 + 2 * TFMS)})  # 满→丢
check("回调-入队 2 丢 1", small_q.qsize() == 2
      and jsc._STATS["events_dropped"] >= 1, f"q={small_q.qsize()}")

# ── 12. jws.dispatch 端到端接线（组合流消息 → 回调 → 队列）────────
import jarvis_ws_stream as jws  # noqa: E402

jsc._Q = queue.Queue(maxsize=64)
jws.register_callback("kline", jsc._on_kline)
msg = json.dumps({"stream": "btcusdt@kline_5m",
                  "data": {"e": "kline", "k": dict(k_closed["k"], t=T0 + 3 * TFMS)}})
st, sym = jws.dispatch(msg, {"ws_buffer_size": 10})
check("接线-dispatch 分类", (st, sym) == ("kline", "BTCUSDT"))
check("接线-事件入队", jsc._Q.qsize() == 1
      and jsc._Q.get_nowait()[2]["time"] == T0 + 3 * TFMS)

# ── 13. 覆盖率报表：结构 + 验收判定 ───────────────────────────────
rep = jsc.coverage_report(days=7, symbols=["BTCUSDT"], tfs=["5m"],
                          now=(T0 + 15 * TFMS) / 1000.0)
check("报表-顶层结构", all(k in rep for k in
      ("window", "ledger", "tape", "changes_hist", "acceptance")),
      str(list(rep.keys())))
ov = rep["ledger"]["overall"]
check("报表-台账口径（应有>已采>0）", ov["expected"] > 0
      and 0 < ov["covered"] <= ov["expected"], str(ov))
pair = rep["ledger"]["by_pair"].get("BTCUSDT:5m")
check("报表-分对明细", pair is not None and pair["covered"] == ov["covered"], str(pair))
check("报表-历史基线条数=流水数", rep["changes_hist"]["total"]
      == jsh.history("BTCUSDT", "5m")["total"], str(rep["changes_hist"]))
acc = rep["acceptance"]
check("报表-验收：窗口不足如实标注", acc["window_ok"] is False
      and acc["pass"] is False and len(acc["notes"]) >= 1, str(acc))
check("报表-tape 缺表降级不炸", "error" in rep["tape"] or rep["tape"]["overall_rate"] is None)

# ── 14. 台账覆盖口径：n_signals 为空（史不足）不算已采 ────────────
jsc.ledger_record("BTCUSDT", "5m", T0 + 20 * TFMS, T0 + 21 * TFMS, "ws",
                  n_signals=None, note="insufficient_history")
rep2 = jsc.coverage_report(days=7, symbols=["BTCUSDT"], tfs=["5m"],
                           now=(T0 + 21 * TFMS) / 1000.0)
check("报表-史不足 bar 不算已采",
      rep2["ledger"]["overall"]["covered"] == ov["covered"],
      f"{rep2['ledger']['overall']} vs {ov}")

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
