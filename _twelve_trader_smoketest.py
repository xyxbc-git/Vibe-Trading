"""离线冒烟：12 系统信号矩阵 → 模拟下单 → 平仓 → 信号胜率归因统计。用临时 DB，不联网。"""
from __future__ import annotations

import json
import os
import tempfile

import jarvis_journal as jj

_d = tempfile.mkdtemp()
jj.DB_DIR = _d
jj.DB_PATH = os.path.join(_d, "test.db")

import jarvis_circuit_breaker as jcb
import jarvis_executor as jx
import jarvis_paper_trader as jpt
import jarvis_wallet as jw

# 熔断器隔离：状态文件指到临时目录 + 门禁恒放行（熔断链路有独立 smoketest，
# 这里若不隔离，测试的资金波动会误触发并污染真实 circuit_breaker.json）
jcb.STATE_PATH = os.path.join(_d, "cb.json")
jcb.guard_new_order = lambda cfg=None: {"allow": True, "reason": "smoketest"}

# 市场状态分类器打桩：默认 trending（覆盖生产自动打标路径），场景可用参数注入覆盖
jpt._classify_regime = lambda s: "trending"

cfg = jx.load_config()
cfg["account_equity_usdt"] = 1000.0
cfg["agent_token"] = ""   # 纯离线：不打真实网关
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


jw.reset(1000.0)

# 现价固定 100，绕过联网取价
jpt.latest_price = lambda c, s: 100.0

# 1. 旧库升级幂等：init 两次不抛错
jpt.init_positions_table()
jpt.init_positions_table()
check("归因列迁移幂等", True)

# 2. bullish 共识 → 自动开多仓（注入共识，绕过 K 线拉取）
cons_bull = {
    "direction": "bullish", "confidence": 0.72, "primary_tf": "4h",
    "trade_plan": {"entry_zone": [99.0, 101.0], "stop_loss": 95.0,
                   "take_profit_1": 110.0, "take_profit_2": 118.0,
                   "position_pct": 20.0, "basis": ["turtle", "dow"],
                   "source_tf": "4h"},
}
r = jpt.open_from_twelve("BTCUSDT", cfg, consensus=cons_bull)
check("共识开多仓", r.get("action") == "opened", str(r))
check("归因系统=turtle,dow", r.get("systems") == ["turtle", "dow"], str(r.get("systems")))
poss = jpt.open_positions("BTCUSDT")
check("持仓 side=buy", poss and poss[0]["side"] == "buy", str(poss[0] if poss else None))
check("落库 signal_source=twelve", poss[0].get("signal_source") == "twelve")
check("落库 signal_systems JSON", json.loads(poss[0].get("signal_systems") or "[]") == ["turtle", "dow"])
check("落库 signal_tf=4h", poss[0].get("signal_tf") == "4h")
check("落库 signal_regime=trending（自动打标路径）",
      poss[0].get("signal_regime") == "trending", str(poss[0].get("signal_regime")))
check("仓位=权益20% → qty=2", abs(poss[0]["qty"] - 2.0) < 1e-9, f"qty={poss[0]['qty']}")

# 3. 同币重复开仓拦截
r2 = jpt.open_from_twelve("BTCUSDT", cfg, consensus=cons_bull)
check("重复开仓拦截", r2.get("action") == "skip" and "未平仓" in r2.get("reason", ""), str(r2))

# 4. 置信度不足拦截
r3 = jpt.open_from_twelve("ETHUSDT", cfg,
                          consensus={**cons_bull, "confidence": 0.2})
check("低置信拦截", r3.get("action") == "skip" and "置信度" in r3.get("reason", ""), str(r3))

# 5. 中性共识拦截
r4 = jpt.open_from_twelve("ETHUSDT", cfg,
                          consensus={"direction": "neutral", "confidence": 0.9,
                                     "trade_plan": None})
check("中性共识拦截", r4.get("action") == "skip", str(r4))

# 6. 现价已出计划区拦截（多头计划但现价 100 ≥ TP 98）
r5 = jpt.open_from_twelve("ETHUSDT", cfg, consensus={
    "direction": "bullish", "confidence": 0.8, "primary_tf": "4h",
    "trade_plan": {"stop_loss": 90.0, "take_profit_1": 98.0,
                   "position_pct": 10.0, "basis": ["gap"], "source_tf": "4h"},
})
check("行情跑掉拦截", r5.get("action") == "skip" and "计划区" in r5.get("reason", ""), str(r5))

# 7. 止盈平仓：现价推到 111 > TP 110 → check_exits 触发 take
#    （twelve 持仓的反转判定走 _twelve_consensus，打桩为同向避免误触 signal 平仓）
jpt._twelve_consensus = lambda s: {"direction": "bullish", "confidence": 0.7}
jpt.latest_price = lambda c, s: 111.0
closed = jpt.check_exits(cfg)
check("止盈平仓 1 笔", len(closed) == 1 and closed[0]["reason"] == "take", str(closed))
check("平仓盈亏 +22U", abs((closed[0].get("pnl_usdt") or 0) - 22.0) < 1e-6,
      f"pnl={closed[0].get('pnl_usdt')}")

# 8. bearish 共识 → 开空仓 → 共识反转平仓（signal）
cons_bear = {
    "direction": "bearish", "confidence": 0.66, "primary_tf": "1h",
    "trade_plan": {"stop_loss": 105.0, "take_profit_1": 88.0,
                   "position_pct": 10.0, "basis": ["oscillator", "triple_rsi"],
                   "source_tf": "1h"},
}
jpt.latest_price = lambda c, s: 100.0
r6 = jpt.open_from_twelve("ETHUSDT", cfg, consensus=cons_bear, regime="ranging")
check("共识开空仓", r6.get("action") == "opened" and r6.get("side") == "sell", str(r6))
check("空仓时间止损=1h→3天",
      jpt.open_positions("ETHUSDT")[0]["time_stop_days"] == 3)
check("落库 signal_regime=ranging（参数注入路径）",
      jpt.open_positions("ETHUSDT")[0].get("signal_regime") == "ranging")
# 共识翻多 → 空仓 signal 平仓（现价 100 未触 SL/TP）
jpt._twelve_consensus = lambda s: {"direction": "bullish", "confidence": 0.7}
closed2 = jpt.check_exits(cfg)
check("共识反转平空仓", len(closed2) == 1 and closed2[0]["reason"] == "signal", str(closed2))

# 9. 信号胜率归因统计
st = jpt.signal_stats()
ov = st["overall"]
check("统计:已平仓 2 笔", ov["closed_trades"] == 2, str(ov))
check("统计:胜 1 平/负 1", ov["wins"] == 1, str(ov))
sys_map = {s["system"]: s for s in st["systems"]}
check("turtle 归因 1 笔全胜",
      sys_map.get("turtle", {}).get("trades") == 1
      and sys_map.get("turtle", {}).get("win_rate_pct") == 100.0,
      str(sys_map.get("turtle")))
check("dow 同笔共振归因", sys_map.get("dow", {}).get("trades") == 1, str(sys_map.get("dow")))
check("oscillator 归因空单", sys_map.get("oscillator", {}).get("trades") == 1,
      str(sys_map.get("oscillator")))
check("name_cn 映射", sys_map.get("turtle", {}).get("name_cn") == "海龟交易")
check("低样本标记 low_sample=True", sys_map.get("turtle", {}).get("low_sample") is True)

# 9.5 多维筛选样本：再开两笔不同维度的单并平仓
#   C: ADAUSDT 多 15m breakout 单系统[gap]        → 止损平 pnl=-6
#   D: BNBUSDT 空 4h  trending 4系统               → 止盈平 pnl=+13
r_c = jpt.open_from_twelve("ADAUSDT", cfg, regime="breakout", consensus={
    "direction": "bullish", "confidence": 0.6, "primary_tf": "15m",
    "trade_plan": {"stop_loss": 95.0, "take_profit_1": 112.0,
                   "position_pct": 10.0, "basis": ["gap"], "source_tf": "15m"},
})
check("C 开多（15m/breakout/单系统）", r_c.get("action") == "opened", str(r_c))
r_d = jpt.open_from_twelve("BNBUSDT", cfg, regime="trending", consensus={
    "direction": "bearish", "confidence": 0.7, "primary_tf": "4h",
    "trade_plan": {"stop_loss": 106.0, "take_profit_1": 88.0, "position_pct": 10.0,
                   "basis": ["turtle", "chanlun", "rule123", "gap"], "source_tf": "4h"},
})
check("D 开空（4h/trending/4系统）", r_d.get("action") == "opened", str(r_d))
# 分币推价：C 触止损（94≤95），D 空单触止盈（87≤88）
jpt.latest_price = lambda c, s: {"ADAUSDT": 94.0, "BNBUSDT": 87.0}.get(s, 100.0)
closed3 = jpt.check_exits(cfg)
check("C/D 同轮平仓（stop+take）",
      sorted(x["reason"] for x in closed3) == ["stop", "take"], str(closed3))

# 9.6 多维筛选断言（A:多/4h/trending/2-3系统 +22；B:空/1h/ranging/2-3系统 0；
#                  C:多/15m/breakout/1系统 -6；D:空/4h/trending/4+系统 +13）
st_all = jpt.signal_stats()
check("无参=全量 4 笔（旧行为兼容）", st_all["overall"]["closed_trades"] == 4,
      str(st_all["overall"]["closed_trades"]))
check("filters 回显：维度全空 + source 默认 realtime",
      all(v is None for k, v in st_all["filters"].items() if k != "source")
      and st_all["filters"]["source"] == "realtime")
st_long = jpt.signal_stats(direction="long")
check("筛选 direction=long → 2 笔（A/C）", st_long["overall"]["closed_trades"] == 2,
      str(st_long["overall"]["closed_trades"]))
st_4h = jpt.signal_stats(tf="4h")
check("筛选 tf=4h → 2 笔（A/D）", st_4h["overall"]["closed_trades"] == 2)
st_res1 = jpt.signal_stats(resonance="1")
check("筛选 resonance=1 → 1 笔（C）", st_res1["overall"]["closed_trades"] == 1)
st_res4 = jpt.signal_stats(resonance="4+")
check("筛选 resonance=4+ → 1 笔（D）", st_res4["overall"]["closed_trades"] == 1)
st_break = jpt.signal_stats(regime="breakout")
check("筛选 regime=breakout → 1 笔（C）", st_break["overall"]["closed_trades"] == 1)
st_combo = jpt.signal_stats(direction="short", regime="trending")
check("组合筛选 空+trending → 1 笔（D）", st_combo["overall"]["closed_trades"] == 1)

# 9.7 期望值：gap 系统命中 C(-6)/D(+13) → 胜率50% 期望=0.5*13+0.5*(-6)=3.5
gap_row = {s["system"]: s for s in st_all["systems"]}.get("gap", {})
check("gap 两笔归因", gap_row.get("trades") == 2, str(gap_row))
check("gap 期望值=3.5U/笔", abs((gap_row.get("expectancy_usdt") or 0) - 3.5) < 1e-6,
      str(gap_row.get("expectancy_usdt")))
check("gap 均盈/均亏", gap_row.get("avg_win_usdt") == 13.0
      and gap_row.get("avg_loss_usdt") == -6.0,
      f"win={gap_row.get('avg_win_usdt')} loss={gap_row.get('avg_loss_usdt')}")
check("系统按期望值降序排列",
      [s["expectancy_usdt"] for s in st_all["systems"]]
      == sorted([s["expectancy_usdt"] for s in st_all["systems"]], reverse=True),
      str([(s["system"], s["expectancy_usdt"]) for s in st_all["systems"]]))

# 9.8 存量兼容：B 的 regime 置 NULL → 归 unknown
with jj._conn() as conn:
    conn.execute("UPDATE paper_positions SET signal_regime=NULL "
                 "WHERE symbol='ETHUSDT' AND signal_source='twelve'")
st_unk = jpt.signal_stats(regime="unknown")
check("存量无标归 unknown → 1 笔（B）", st_unk["overall"]["closed_trades"] == 1,
      str(st_unk["overall"]["closed_trades"]))
st_rng = jpt.signal_stats(regime="ranging")
check("置 NULL 后 ranging 为 0 笔", st_rng["overall"]["closed_trades"] == 0)

# 10. 非 twelve 来源不进统计：手动限价单成交后平仓
jw.place_limit_order("SOLUSDT", "buy", 100.0, 1.0)
jpt.match_limit_orders(cfg)
for p in jpt.open_positions("SOLUSDT"):
    jpt._close_position(p, 120.0, "manual", cfg)
st2 = jpt.signal_stats()
check("manual/limit 不进 twelve 统计", st2["overall"]["closed_trades"] == 4,
      str(st2["overall"]))

# ═══════════ 11. 历史回放引擎（jarvis_signal_replay，全离线注入） ═══════════
import pandas as pd  # noqa: E402

import jarvis_signal_replay as jsr  # noqa: E402

_BASE_MS = 1_700_000_000_000
_TFMS = 900_000  # 15m


def _mk_df(n: int, spike_i: int | None = None) -> pd.DataFrame:
    """合成 K 线：OHLC 恒 100；spike_i 那根 high=111（触发 TP110）。"""
    rows = []
    for i in range(n):
        hi = 111.0 if i == spike_i else 100.0
        rows.append({"time": _BASE_MS + i * _TFMS, "open": 100.0, "high": hi,
                     "low": 100.0, "close": 100.0, "volume": 1.0})
    return pd.DataFrame(rows)


def _bar_i(window: pd.DataFrame) -> int:
    return int((int(window.iloc[-1]["time"]) - _BASE_MS) / _TFMS)


def _mk_analyze(open_at: int, sl: float, tp: float):
    def _fake(window: pd.DataFrame) -> dict:
        if _bar_i(window) == open_at:
            return {"consensus": {"direction": "bullish", "confidence": 0.7,
                    "trade_plan": {"stop_loss": sl, "take_profit_1": tp,
                                   "basis": ["turtle", "dow"]}}}
        return {"consensus": {"direction": "neutral", "confidence": 0.2,
                              "trade_plan": None}}
    return _fake


wal_before = jw.get_wallet()

# 11.1 止盈闭环：第 310 根开多（SL95/TP110），第 320 根 high=111 → take@110
r_rp = jsr.replay_df("BTCUSDT", "15m", _mk_df(431, spike_i=320),
                     analyze=_mk_analyze(310, 95.0, 110.0),
                     classify_regime=lambda w: "trending")
check("回放开1平1（take）", r_rp["opened"] == 1 and r_rp["closed"] == 1
      and r_rp["trades"][0]["reason"] == "take", str(r_rp["trades"]))
check("回放盈亏 +10U（TP价成交，名义100U）",
      abs(r_rp["trades"][0]["pnl_usdt"] - 10.0) < 1e-6, str(r_rp["trades"][0]))
rp_rows = [p for p in jpt.all_positions("BTCUSDT")
           if (p.get("signal_source") or "") == "replay"]
check("回放落库 source=replay + 全归因",
      len(rp_rows) == 1 and rp_rows[0]["signal_tf"] == "15m"
      and rp_rows[0]["signal_regime"] == "trending"
      and json.loads(rp_rows[0]["signal_systems"]) == ["turtle", "dow"],
      str(rp_rows[0] if rp_rows else None))
check("回放 opened_ts=历史bar时间（防未来）",
      abs(rp_rows[0]["opened_ts"] - (_BASE_MS + 310 * _TFMS) / 1000.0) < 1,
      str(rp_rows[0]["opened_ts"]))

# 11.2 时间止损 + 幂等清旧：价格永不触 SL90/TP120 → 96根（15m=1天）后 time 平仓
r_rp2 = jsr.replay_df("BTCUSDT", "15m", _mk_df(431),
                      analyze=_mk_analyze(310, 90.0, 120.0),
                      classify_regime=lambda w: None)
check("重复回放先清旧（幂等）", r_rp2["cleared"] == 1, str(r_rp2["cleared"]))
check("时间止损平仓", r_rp2["closed"] == 1 and r_rp2["trades"][0]["reason"] == "time",
      str(r_rp2["trades"]))
check("regime 打标失败归 None 落库", [p for p in jpt.all_positions("BTCUSDT")
      if (p.get("signal_source") or "") == "replay"][0]["signal_regime"] is None)

# 11.3 尾部未走完的单直接删除（不留 open replay 污染实时台账）
r_rp3 = jsr.replay_df("ETHUSDT", "15m", _mk_df(431),
                      analyze=_mk_analyze(425, 90.0, 120.0),
                      classify_regime=lambda w: None)
check("尾部 open 单已删（skipped_open=1）",
      r_rp3["opened"] == 1 and r_rp3["closed"] == 0 and r_rp3["skipped_open"] == 1,
      str(r_rp3))
check("库里无 ETHUSDT replay 残留", not [
    p for p in jpt.all_positions("ETHUSDT")
    if (p.get("signal_source") or "") == "replay"])

# 11.4 隔离性：钱包分文未动 + source 筛选 + 台账/在途不受污染
wal_after = jw.get_wallet()
check("回放不动钱包", abs(wal_before["cash_usdt"] - wal_after["cash_usdt"]) < 1e-9
      and abs(wal_before["frozen_usdt"] - wal_after["frozen_usdt"]) < 1e-9,
      f"before={wal_before['cash_usdt']} after={wal_after['cash_usdt']}")
check("source=realtime 默认不含回放", jpt.signal_stats()["overall"]["closed_trades"] == 4)
check("source=replay 只见回放", jpt.signal_stats(source="replay")["overall"]["closed_trades"] == 1)
check("source=all 合并", jpt.signal_stats(source="all")["overall"]["closed_trades"] == 5)
check("回放样本可按维度筛选（tf=15m & replay）",
      jpt.signal_stats(tf="15m", source="replay")["overall"]["closed_trades"] == 1)
check("台账 stats 排除 replay", jpt.stats(cfg)["closed_trades"] == 5,
      str(jpt.stats(cfg)["closed_trades"]))

# 11.5 check_exits 跳过 replay open 单（实时盯盘不碰历史样本）
_rp_pid = jpt._insert_position("DOGEUSDT", 1.0, "2026-01-01", 100.0, None,
                               99.0, 120.0, 7, 0.5, side="buy",
                               signal_source="replay", signal_tf="15m")
jpt.latest_price = lambda c, s: 50.0   # 若不跳过必触发止损
closed_guard = jpt.check_exits(cfg)
still_open = [p for p in jpt.open_positions("DOGEUSDT")
              if (p.get("signal_source") or "") == "replay"]
check("check_exits 跳过 replay 持仓", len(closed_guard) == 0 and len(still_open) == 1,
      f"closed={closed_guard} open={len(still_open)}")
jsr._delete_position(_rp_pid)

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS ✅")
raise SystemExit(1 if fails else 0)
