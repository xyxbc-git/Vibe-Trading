"""离线冒烟：12系统×6时间轴 槽位级模拟交易引擎（jarvis_twelve_trader）。

打桩手法同 _twelve_trader_smoketest.py：临时 DB（jarvis_db 对非默认路径强制走
SQLite，天然隔离）、latest_price 打桩、直接向 twelve_signal_state 注入信号态。
全离线：不联网、不触发 12 信号重算、不碰生产库。
"""
from __future__ import annotations

import json
import os
import tempfile
import time

_d = tempfile.mkdtemp()

import jarvis_signal_history as jsh  # noqa: E402
import jarvis_twelve_trader as jtt  # noqa: E402

# 两模块共用同一个临时库：引擎只读 jsh 建的 twelve_signal_state
jsh.DB_DIR = _d
jsh.DB_PATH = os.path.join(_d, "test.db")
jtt.DB_DIR = _d
jtt.DB_PATH = jsh.DB_PATH
jtt.LOG_PATH = os.path.join(_d, "test.log")

fails: list[str] = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


SYM = "ETHUSDT"


def set_signal(tf: str, system: str, direction: str, plan: dict | None = None,
               strength: float = 0.6) -> None:
    """注入/覆盖一条槽位当前态信号（DELETE+INSERT，绕开方言差异）。"""
    with jsh._conn() as conn:
        conn.execute(
            "DELETE FROM twelve_signal_state WHERE symbol=? AND tf=? AND system=?",
            (SYM, tf, system))
        conn.execute(
            """
            INSERT INTO twelve_signal_state
              (symbol, tf, system, name_cn, direction, strength, reasoning,
               levels_json, plan_json, updated_ts, changed_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (SYM, tf, system, jtt.NAME_CN.get(system, system), direction,
             strength, "smoketest", "[]",
             json.dumps(plan, ensure_ascii=False), time.time(), time.time()))


# 现价打桩：全程无网络
_PRICE = {"v": 100.0}
jtt.latest_price = lambda cfg, s: _PRICE["v"]
jtt.mark_price_of = lambda cfg, s: None   # 离线：标记价缺失→爆仓判定回退成交价（旧口径）
# K 线打桩：默认取不到（引擎应优雅回退快照现价比对）；影线用例单独喂 bar
jtt._fetch_bars = lambda symbol, tf: None
# 手续费打桩：基础用例免手续费保持整数断言；手续费用例单独开 0.05
jtt._fee_pct = lambda: 0.0

# ═══════════ 1. 建表幂等 + 配置 upsert/合并 ═══════════
jsh.init_db()
jtt.init_db()
jtt.init_db()
check("建表幂等（init 两次不抛错）", True)

r = jtt.upsert_config(SYM)                                   # 币种级（启用币种）
check("币种级配置创建", r.get("ok") and r.get("created"), str(r))
r = jtt.upsert_config(SYM, principal=100.0)                   # 同 scope 再写=更新
check("同 scope 二次写入=更新不新增", r.get("ok") and not r.get("created"), str(r))
check("配置行数=1（NULL scope 去重）", len(jtt.list_configs(SYM)) == 1)

jtt.upsert_config(SYM, "1h", None, leverage=2.0, position_pct=15.0)     # tf 组级
jtt.upsert_config(SYM, "1h", "turtle", leverage=5.0, position_pct=20.0)  # 信号级
check("非法 scope_tf 拒绝", not jtt.upsert_config(SYM, "7h").get("ok"))
check("非法 scope_system 拒绝", not jtt.upsert_config(SYM, "1h", "foo").get("ok"))

eff = jtt.effective_config(SYM, "1h", "turtle")
check("信号级覆盖 tf 组级（lev=5/pos=20）",
      eff["leverage"] == 5.0 and eff["position_pct"] == 20.0, str(eff))
eff2 = jtt.effective_config(SYM, "1h", "dow")
check("tf 组级兜底（lev=2/pos=15）",
      eff2["leverage"] == 2.0 and eff2["position_pct"] == 15.0, str(eff2))
eff3 = jtt.effective_config(SYM, "4h", "dow")
check("币种级无字段 → None 交给 plan/默认", eff3["leverage"] is None
      and eff3["position_pct"] is None, str(eff3))
check("configured_symbols=[ETHUSDT]", jtt.configured_symbols() == [SYM])

# ═══════════ 2. 首轮：bullish 开多 / bearish 开空 / neutral 不动 ═══════════
set_signal("1h", "turtle", "bullish",
           {"side": "long", "entry": 100.0, "stop_loss": 95.0,
            "take_profit": 110.0, "rr": 2.0})
set_signal("4h", "dow", "bearish",
           {"side": "short", "entry": 100.0, "stop_loss": 105.0,
            "take_profit": 88.0, "rr": 2.4})
set_signal("5m", "gap", "neutral", None)
set_signal("15m", "elliott", "bullish", None)   # 有方向但无计划 → 宁缺毋滥不开

out = jtt.run_cycle(cfg={})
r_sym = out["symbols"][SYM]
check("首轮开仓 2 笔（neutral/无计划不开）", len(r_sym["opened"]) == 2, str(r_sym))
check("72 槽位钱包已建齐", len(jtt.get_wallets(SYM)) == 72)

poss = {(p["tf"], p["system"]): p for p in jtt.open_positions(SYM)}
lt = poss.get(("1h", "turtle"))
sd = poss.get(("4h", "dow"))
check("turtle 1h 开多", lt is not None and lt["direction"] == "long", str(lt))
check("dow 4h 开空", sd is not None and sd["direction"] == "short", str(sd))
check("信号级配置生效：margin=20U qty=1（20%×100U×5 倍 /100）",
      lt and abs(lt["margin"] - 20.0) < 1e-9 and abs(lt["qty"] - 1.0) < 1e-9,
      f"margin={lt and lt['margin']} qty={lt and lt['qty']}")
check("无配置槽位自动杠杆：SL 距 5% → lev=10（0.5/0.05），margin=10U qty=1",
      sd and abs(sd["leverage"] - 10.0) < 1e-9
      and abs(sd["margin"] - 10.0) < 1e-9 and abs(sd["qty"] - 1.0) < 1e-9,
      f"lev={sd and sd['leverage']} margin={sd and sd['margin']} qty={sd and sd['qty']}")
check("SL/TP 取自 plan_json", lt["stop_loss"] == 95.0 and lt["take_profit"] == 110.0)

# ═══════════ 3. 盯盘持有：刷新现价/浮盈，neutral 持仓不动 ═══════════
_PRICE["v"] = 104.0
out = jtt.run_cycle(cfg={})
r_sym = out["symbols"][SYM]
check("无触发轮：持有 2 / 不开不平", r_sym["holds"] == 2
      and not r_sym["opened"] and not r_sym["closed"], str(r_sym))
lt2 = {(p["tf"], p["system"]): p for p in jtt.open_positions(SYM)}[("1h", "turtle")]
check("浮盈刷新：turtle 多单 +4U（(104-100)×1）",
      abs(lt2["unrealized_pnl"] - 4.0) < 1e-9 and lt2["cur_price"] == 104.0,
      f"upnl={lt2['unrealized_pnl']}")
w_lt = [w for w in jtt.get_wallets(SYM) if w["tf"] == "1h" and w["system"] == "turtle"][0]
check("钱包 equity=balance+浮盈=104", abs(w_lt["equity"] - 104.0) < 1e-9,
      f"equity={w_lt['equity']}")

# ═══════════ 4. 止盈/爆仓平仓 + 钱包战绩 ═══════════
_PRICE["v"] = 111.0   # 多单触 TP110；空单(10倍)越过爆仓价 110（liq 优先于 SL105）
out = jtt.run_cycle(cfg={})
r_sym = out["symbols"][SYM]
reasons = sorted(c["exit_reason"] for c in r_sym["closed"])
check("同轮平 2 笔（tp+liq，liq 优先于 sl）", reasons == ["liq", "tp"], str(reasons))
check("平仓后行情已出计划区 → 不立刻重开", not r_sym["opened"], str(r_sym["opened"]))
by_slot = {(c["tf"], c["system"]): c for c in r_sym["closed"]}
c_lt = by_slot[("1h", "turtle")]
c_sd = by_slot[("4h", "dow")]
check("多单止盈按触发位 110 结算 pnl=+10U（(110-100)×1）",
      abs(c_lt["pnl"] - 10.0) < 1e-9 and abs(c_lt["exit_price"] - 110.0) < 1e-9,
      f"pnl={c_lt['pnl']} exit={c_lt['exit_price']}")
check("空单爆仓 exit_price=110 pnl=-margin=-10U",
      c_sd["exit_reason"] == "liq" and abs(c_sd["exit_price"] - 110.0) < 1e-9
      and abs(c_sd["pnl"] + 10.0) < 1e-9, f"pnl={c_sd['pnl']} exit={c_sd['exit_price']}")
check("balance_after 正确（110 / 90）",
      abs(c_lt["balance_after"] - 110.0) < 1e-9
      and abs(c_sd["balance_after"] - 90.0) < 1e-9)

w = {(x["tf"], x["system"]): x for x in jtt.get_wallets(SYM)}
w_lt, w_sd = w[("1h", "turtle")], w[("4h", "dow")]
check("turtle 钱包战绩：1 笔 100% 胜率 pnl=10",
      w_lt["total_trades"] == 1 and w_lt["win_trades"] == 1
      and w_lt["win_rate"] == 100.0 and abs(w_lt["total_pnl"] - 10.0) < 1e-9,
      str({k: w_lt[k] for k in ('total_trades', 'win_rate', 'total_pnl')}))
check("dow 钱包战绩：1 笔 0% 胜率 回撤 10%",
      w_sd["total_trades"] == 1 and w_sd["win_trades"] == 0
      and w_sd["win_rate"] == 0.0 and abs(w_sd["max_drawdown_pct"] - 10.0) < 1e-6,
      str({k: w_sd[k] for k in ('win_rate', 'max_drawdown_pct')}))
t_rows = jtt.trades(SYM, "1h", "turtle")
check("台账流水字段完整（rr=2.0 / holding_minutes≥0）",
      len(t_rows) == 1 and t_rows[0]["rr"] == 2.0
      and t_rows[0]["holding_minutes"] is not None
      and t_rows[0]["exit_reason"] == "tp", str(t_rows[0] if t_rows else None))

# ═══════════ 5. 方向翻转（R3 规则3）：已成交仓位独立，反向信号=新的独立计划 ═══════════
set_signal("1h", "turtle", "bearish",
           {"side": "short", "entry": 111.0, "stop_loss": 120.0,
            "take_profit": 90.0})
out = jtt.run_cycle(cfg={})   # 槽位已空 → 触达（做空 111≥111）直接开空 @111
r_sym = out["symbols"][SYM]
op_t = [o for o in r_sym["opened"] if (o["tf"], o["system"]) == ("1h", "turtle")]
check("翻转前置：开空 1 笔", len(op_t) == 1 and op_t[0]["direction"] == "short",
      str(r_sym["opened"]))

set_signal("1h", "turtle", "bullish",
           {"side": "long", "entry": 111.0, "stop_loss": 100.0,
            "take_profit": 125.0})
out = jtt.run_cycle(cfg={})   # 价不变 111：信号反向
r_sym = out["symbols"][SYM]
check("规则3：信号反向不平已成交仓位（无 flip 平仓）", not r_sym["closed"],
      str(r_sym["closed"]))
op_t = [o for o in r_sym["opened"] if (o["tf"], o["system"]) == ("1h", "turtle")]
check("反向信号=新的独立计划：触达（做多 111≤111）另开多单，原空单保留",
      len(op_t) == 1 and op_t[0]["direction"] == "long", str(r_sym["opened"]))
poss_t = [p for p in jtt.open_positions(SYM)
          if (p["tf"], p["system"]) == ("1h", "turtle")]
check("同槽位多空并存（1空+1多，互不影响）",
      sorted(p["direction"] for p in poss_t) == ["long", "short"],
      str([(p["direction"], p["entry_price"]) for p in poss_t]))
out = jtt.run_cycle(cfg={})   # 同方向信号持续 → 不重复建仓不重复挂计划
r_sym = out["symbols"][SYM]
check("同方向信号持续：不重复建仓/挂计划", not r_sym["opened"]
      and not r_sym["planned"] and r_sym["holds"] == 2, str(r_sym))

# ═══════════ 6. neutral 持仓不动（不平已有仓） ═══════════
set_signal("1h", "turtle", "neutral", None)
out = jtt.run_cycle(cfg={})
r_sym = out["symbols"][SYM]
check("信号转 neutral：已成交仓位不平不动（多空各1）",
      not r_sym["closed"] and r_sym["holds"] == 2, str(r_sym))

# ═══════════ 7. 槽位停用 + 爆仓停开 ═══════════
jtt.upsert_config(SYM, "4h", "dow", enabled=False)
set_signal("4h", "dow", "bearish",
           {"side": "short", "entry": 111.0, "stop_loss": 120.0,
            "take_profit": 90.0})
out = jtt.run_cycle(cfg={})
opened_slots = {(o["tf"], o["system"]) for o in out["symbols"][SYM]["opened"]}
check("信号级 enabled=0 → 该槽位不开仓", ("4h", "dow") not in opened_slots,
      str(opened_slots))

with jtt._conn() as conn:   # 人工把某槽位打成爆仓余额
    conn.execute("UPDATE twelve_sim_wallet SET balance=0.5, equity=0.5 "
                 "WHERE symbol=? AND tf=? AND system=?", (SYM, "30m", "gann"))
set_signal("30m", "gann", "bullish",
           {"side": "long", "entry": 111.0, "stop_loss": 100.0,
            "take_profit": 125.0})
out = jtt.run_cycle(cfg={})
opened_slots = {(o["tf"], o["system"]) for o in out["symbols"][SYM]["opened"]}
check("余额<1U 爆仓槽位停开", ("30m", "gann") not in opened_slots, str(opened_slots))

# ═══════════ 8. 逐仓亏损钳制（杠杆大亏不倒欠） ═══════════
jtt.upsert_config(SYM, "1d", "martingale", leverage=20.0, position_pct=50.0,
                  stop_loss_pct=90.0, take_profit_pct=95.0)   # 配置 pct 覆盖计划
set_signal("1d", "martingale", "bullish", None)   # 无计划，全靠配置 pct
out = jtt.run_cycle(cfg={})
mg = {(p["tf"], p["system"]): p for p in jtt.open_positions(SYM)}.get(("1d", "martingale"))
check("配置 pct 兜底开仓（无 plan 也能开）", mg is not None
      and abs(mg["stop_loss"] - 11.1) < 1e-9 and abs(mg["take_profit"] - 216.45) < 1e-6,
      str(mg and (mg['stop_loss'], mg['take_profit'])))
_PRICE["v"] = 11.0   # 深跌越过爆仓价 105.45（20倍）→ liq 强平
out = jtt.run_cycle(cfg={})
c_mg = [c for c in out["symbols"][SYM]["closed"]
        if (c["tf"], c["system"]) == ("1d", "martingale")]
check("深跌越过爆仓价 → exit_reason=liq 且亏损=−margin 不倒欠", len(c_mg) == 1
      and c_mg[0]["exit_reason"] == "liq"
      and abs(c_mg[0]["pnl"] + mg["margin"]) < 1e-6
      and c_mg[0]["balance_after"] >= 0, str(c_mg))

# ═══════════ 9. 新能力用例准备：人工清场（平掉全部在途/挂单 + 钱包复位 + 信号归中） ═══════════
with jtt._conn() as conn:
    conn.execute("UPDATE twelve_sim_position SET status='closed' WHERE status='open'")
    conn.execute("DELETE FROM twelve_sim_position WHERE status='pending'")
    conn.execute("UPDATE twelve_sim_wallet SET balance=100, equity=100")
for _tf, _sys in [("1h", "turtle"), ("4h", "dow"), ("5m", "gap"),
                  ("15m", "elliott"), ("30m", "gann"), ("1d", "martingale")]:
    set_signal(_tf, _sys, "neutral", None)

# ═══════════ 10. 影线盘中触发（同 bar 双触保守取 SL；快照价未触也要平） ═══════════
_PRICE["v"] = 100.0
T0 = time.time()
set_signal("5m", "gap", "bullish",
           {"side": "long", "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0})
out = jtt.run_cycle(cfg={}, now=T0)
gp = {(p["tf"], p["system"]): p for p in jtt.open_positions(SYM)}.get(("5m", "gap"))
check("影线用例开仓（自动杠杆 lev=10）", gp is not None
      and abs(gp["leverage"] - 10.0) < 1e-9 and abs(gp["qty"] - 1.0) < 1e-9,
      str(gp and (gp["leverage"], gp["qty"])))
set_signal("5m", "gap", "neutral", None)
# 已收盘 bar 影线：最低 94 触 SL95、最高 111 触 TP110（未及爆仓价 90）；快照价 100 不动
jtt._fetch_bars = lambda symbol, tf: [
    {"time": (T0 + 60) * 1000.0, "high": 111.0, "low": 94.0}]
out = jtt.run_cycle(cfg={}, now=T0 + 120)
c_gp = [c for c in out["symbols"][SYM]["closed"]
        if (c["tf"], c["system"]) == ("5m", "gap")]
check("影线双触保守取 SL（快照 100 本不触发）", len(c_gp) == 1
      and c_gp[0]["exit_reason"] == "sl", str(c_gp))
check("按触发位 95 结算 pnl=-5U（(95-100)×1）", c_gp
      and abs(c_gp[0]["exit_price"] - 95.0) < 1e-9
      and abs(c_gp[0]["pnl"] + 5.0) < 1e-9,
      str(c_gp and (c_gp[0]["exit_price"], c_gp[0]["pnl"])))
jtt._fetch_bars = lambda symbol, tf: None   # 恢复默认（回退快照比对）

# ═══════════ 11. 时间止损（TF 分档：15m=1 天） ═══════════
_PRICE["v"] = 100.0
T1 = time.time()
set_signal("15m", "elliott", "bullish",
           {"side": "long", "entry": 100.0, "stop_loss": 90.0, "take_profit": 120.0})
out = jtt.run_cycle(cfg={}, now=T1)
check("时间止损用例开仓", len(out["symbols"][SYM]["opened"]) == 1,
      str(out["symbols"][SYM]))
set_signal("15m", "elliott", "neutral", None)
out = jtt.run_cycle(cfg={}, now=T1 + 0.5 * 86400)   # 半天：未到 15m 档 1 天上限
r_sym = out["symbols"][SYM]
check("未到时限：持有不平", not r_sym["closed"] and r_sym["holds"] == 1, str(r_sym))
out = jtt.run_cycle(cfg={}, now=T1 + 1.01 * 86400)  # 超 1 天 → timeout
c_el = [c for c in out["symbols"][SYM]["closed"]
        if (c["tf"], c["system"]) == ("15m", "elliott")]
check("15m 档超 1 天 → exit_reason=timeout", len(c_el) == 1
      and c_el[0]["exit_reason"] == "timeout", str(c_el))
check("timeout 以现价结算（同价 pnl=0）", c_el
      and abs(c_el[0]["exit_price"] - 100.0) < 1e-9 and abs(c_el[0]["pnl"]) < 1e-9,
      str(c_el and (c_el[0]["exit_price"], c_el[0]["pnl"])))

# ═══════════ 12. 手续费（双边按名义单边 0.05% 折进净 pnl） ═══════════
import jarvis_config as jc  # noqa: E402
check("jarvis_config 登记 twelve_sim_fee_pct=0.05 且 sim_trader_* 已移除",
      jc.default_config().get("twelve_sim_fee_pct") == 0.05
      and not any(k.startswith("sim_trader_") for k in jc.default_config()))
jtt._fee_pct = lambda: 0.05
_PRICE["v"] = 100.0
set_signal("30m", "gann", "bullish",
           {"side": "long", "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0})
out = jtt.run_cycle(cfg={})
check("手续费用例开仓", len(out["symbols"][SYM]["opened"]) == 1,
      str(out["symbols"][SYM]))
set_signal("30m", "gann", "neutral", None)
_PRICE["v"] = 110.0   # 触 TP，qty=1：毛利 10 − 名义(100+110)×0.05% = 9.895
out = jtt.run_cycle(cfg={})
c_gn = [c for c in out["symbols"][SYM]["closed"]
        if (c["tf"], c["system"]) == ("30m", "gann")]
check("净 pnl 扣双边手续费：10 − 0.105 = 9.895", len(c_gn) == 1
      and abs(c_gn[0]["pnl"] - 9.895) < 1e-9, str(c_gn and c_gn[0]["pnl"]))
jtt._fee_pct = lambda: 0.0

# ═══════════ 13. 爆仓明确断言（liq 优先于 SL，以爆仓价结算） ═══════════
jtt.upsert_config(SYM, "4h", "dow", enabled=True, leverage=10.0)   # 重新启用该槽位
_PRICE["v"] = 100.0
set_signal("4h", "dow", "bearish",
           {"side": "short", "entry": 100.0, "stop_loss": 112.0, "take_profit": 80.0})
out = jtt.run_cycle(cfg={})
check("爆仓用例开空（lev=10 → 爆仓价 110 < SL112）",
      len(out["symbols"][SYM]["opened"]) == 1, str(out["symbols"][SYM]))
set_signal("4h", "dow", "neutral", None)
_PRICE["v"] = 115.0   # 同时越过爆仓价 110 与 SL112 → liq 优先
out = jtt.run_cycle(cfg={})
c_dw = [c for c in out["symbols"][SYM]["closed"]
        if (c["tf"], c["system"]) == ("4h", "dow")]
check("liq 优先于 sl，以爆仓价 110 结算 pnl=-margin=-10",
      len(c_dw) == 1 and c_dw[0]["exit_reason"] == "liq"
      and abs(c_dw[0]["exit_price"] - 110.0) < 1e-9
      and abs(c_dw[0]["pnl"] + 10.0) < 1e-9,
      str(c_dw and (c_dw[0]["exit_reason"], c_dw[0]["exit_price"], c_dw[0]["pnl"])))

# ═══════════ 13.5 R3 规则3：持仓期间信号点位变化只留痕（applied=0），绝不修改持仓 ═══════════
_PRICE["v"] = 100.0
set_signal("1h", "turtle", "bullish",
           {"side": "long", "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0})
out = jtt.run_cycle(cfg={})
check("留痕用例开多（SL95/TP110）", len(out["symbols"][SYM]["opened"]) == 1,
      str(out["symbols"][SYM]["opened"]))
n_logs0 = len(jtt.signal_logs(SYM, "1h", "turtle"))

# a) 信号点位实质变化（SL 95→97 / TP 110→115，均 >0.2%）→ 不应用，仅 applied=0 留痕
set_signal("1h", "turtle", "bullish",
           {"side": "long", "entry": 101.0, "stop_loss": 97.0, "take_profit": 115.0})
out = jtt.run_cycle(cfg={})
r_sym = out["symbols"][SYM]
check("规则3：信号点位变化仅留痕（applied=0 / 不平不开不改）",
      len(r_sym["sltp_updates"]) == 1
      and r_sym["sltp_updates"][0]["applied"] is False
      and not r_sym["closed"] and not r_sym["opened"],
      str(r_sym["sltp_updates"]))
pos_t = {(p["tf"], p["system"]): p for p in jtt.open_positions(SYM)}[("1h", "turtle")]
check("持仓 SL/TP 维持 95/110 不跟随（已成交仓位独立）",
      abs(pos_t["stop_loss"] - 95.0) < 1e-9 and abs(pos_t["take_profit"] - 110.0) < 1e-9,
      str((pos_t["stop_loss"], pos_t["take_profit"])))
logs = jtt.signal_logs(SYM, "1h", "turtle")
check("留痕日志 applied=0：前后值 + 关联 position_id + 新计划 entry + note",
      len(logs) == n_logs0 + 1 and logs[0]["applied"] == 0
      and abs(logs[0]["prev_sl"] - 95.0) < 1e-9 and abs(logs[0]["new_sl"] - 97.0) < 1e-9
      and abs(logs[0]["prev_tp"] - 110.0) < 1e-9 and abs(logs[0]["new_tp"] - 115.0) < 1e-9
      and logs[0]["position_id"] == pos_t["id"]
      and abs(logs[0]["new_entry"] - 101.0) < 1e-9
      and logs[0]["change_kinds"] == "sl,tp" and logs[0]["note"] is not None,
      str(logs[0] if logs else None))

# b) 相对上次留痕微小变化（SL 97→97.15 ≈0.15% < 0.2%）→ 防重不重复记录
set_signal("1h", "turtle", "bullish",
           {"side": "long", "entry": 101.0, "stop_loss": 97.15, "take_profit": 115.0})
out = jtt.run_cycle(cfg={})
check("微小变化(<0.2%)防重不重复留痕", not out["symbols"][SYM]["sltp_updates"]
      and len(jtt.signal_logs(SYM, "1h", "turtle")) == n_logs0 + 1,
      str(out["symbols"][SYM]["sltp_updates"]))

# c) 同一组点位下一轮防重：不再重复记录
out = jtt.run_cycle(cfg={})
check("同点位下一轮防重（日志数不变）",
      not out["symbols"][SYM]["sltp_updates"]
      and len(jtt.signal_logs(SYM, "1h", "turtle")) == n_logs0 + 1,
      str(out["symbols"][SYM]["sltp_updates"]))

# d) 再次实质变化 → 新留痕；持仓点位依旧纹丝不动
set_signal("1h", "turtle", "bullish",
           {"side": "long", "entry": 101.0, "stop_loss": 99.0, "take_profit": 118.0})
out = jtt.run_cycle(cfg={})
r_sym = out["symbols"][SYM]
pos_t2 = {(p["tf"], p["system"]): p for p in jtt.open_positions(SYM)}[("1h", "turtle")]
check("再次实质变化 → 新增留痕且持仓仍 95/110",
      len(r_sym["sltp_updates"]) == 1
      and r_sym["sltp_updates"][0]["applied"] is False
      and len(jtt.signal_logs(SYM, "1h", "turtle")) == n_logs0 + 2
      and abs(pos_t2["stop_loss"] - 95.0) < 1e-9
      and abs(pos_t2["take_profit"] - 110.0) < 1e-9,
      str((r_sym["sltp_updates"], pos_t2["stop_loss"], pos_t2["take_profit"])))

# ═══════════ 13.7 计划→成交语义（breakout/pullback 挂计划 · 触达成交 · 跟随 · 失效） ═══════════
# 清场：平掉全部在途/挂单 + 钱包复位 + 信号归中（复用既有 6 槽位，保持信号行数不变）
with jtt._conn() as conn:
    conn.execute("UPDATE twelve_sim_position SET status='closed' WHERE status='open'")
    conn.execute("DELETE FROM twelve_sim_position WHERE status='pending'")
    conn.execute("UPDATE twelve_sim_wallet SET balance=100, equity=100")
for _tf, _sys in [("1h", "turtle"), ("4h", "dow"), ("5m", "gap"),
                  ("15m", "elliott"), ("30m", "gann"), ("1d", "martingale")]:
    set_signal(_tf, _sys, "neutral", None)
jtt._fetch_bars = lambda symbol, tf: None
_PRICE["v"] = 100.0
T2 = time.time()
n_trades_before = len(jtt.trades(SYM, limit=500))

# a) breakout 未触达 → 只挂计划(pending)：不建仓、不动钱包、不写台账
set_signal("4h", "dow", "bearish",
           {"side": "short", "entry": 92.0, "entry_type": "breakout",
            "stop_loss": 98.0, "take_profit": 80.0})
out = jtt.run_cycle(cfg={}, now=T2)
r_sym = out["symbols"][SYM]
check("breakout 未触达 → 只挂计划不开仓", len(r_sym["planned"]) == 1
      and not r_sym["opened"] and r_sym["planned"][0]["entry_type"] == "breakout",
      str(r_sym["planned"]))
pends = jtt.pending_positions(SYM)
check("pending 行落库（entry=92 / qty=margin=0 / 不进 open_positions）",
      len(pends) == 1 and pends[0]["entry_price"] == 92.0
      and pends[0]["qty"] == 0 and pends[0]["margin"] == 0
      and not jtt.open_positions(SYM), str(pends))
check("挂计划不写 twelve_sim_trade",
      len(jtt.trades(SYM, limit=500)) == n_trades_before)
w_dw = [w for w in jtt.get_wallets(SYM) if w["tf"] == "4h" and w["system"] == "dow"][0]
check("挂计划不动钱包（balance/trades 不变）",
      abs(w_dw["balance"] - 100.0) < 1e-9, str(w_dw["balance"]))

# b) 计划未成交期间点位跟随：entry 92→91 / SL 98→97（>0.2%）→ 更新 + 日志
set_signal("4h", "dow", "bearish",
           {"side": "short", "entry": 91.0, "entry_type": "breakout",
            "stop_loss": 97.0, "take_profit": 80.0})
out = jtt.run_cycle(cfg={}, now=T2 + 60)
r_sym = out["symbols"][SYM]
check("计划点位跟随（entry+sl 实质变化）", len(r_sym["plan_updates"]) == 1
      and r_sym["plan_updates"][0]["change_kinds"] == "entry,sl"
      and not r_sym["filled"], str(r_sym["plan_updates"]))
pen = jtt.pending_positions(SYM)[0]
check("pending 行点位已更新为 91/97",
      pen["entry_price"] == 91.0 and pen["stop_loss"] == 97.0, str(pen))
logs_dw = jtt.signal_logs(SYM, "4h", "dow")
check("计划变更日志（position_id 关联 + prev/new entry）",
      logs_dw and logs_dw[0]["position_id"] == pen["id"]
      and abs(logs_dw[0]["prev_entry"] - 92.0) < 1e-9
      and abs(logs_dw[0]["new_entry"] - 91.0) < 1e-9
      and logs_dw[0]["applied"] == 1, str(logs_dw[0] if logs_dw else None))

# c) 影线触达 → 以计划 entry 价成交（快照价 95 未到位，bar low 90.5 触达 91）
_PRICE["v"] = 95.0
jtt._fetch_bars = lambda symbol, tf: [
    {"time": (T2 + 120) * 1000.0, "high": 96.0, "low": 90.5}]
out = jtt.run_cycle(cfg={}, now=T2 + 180)
r_sym = out["symbols"][SYM]
check("影线触达 → 计划成交（不算普通开仓）", len(r_sym["filled"]) == 1
      and not r_sym["opened"] and not jtt.pending_positions(SYM), str(r_sym["filled"]))
pos_dw = {(p["tf"], p["system"]): p for p in jtt.open_positions(SYM)}.get(("4h", "dow"))
check("成交价=计划 entry 91（非现价95），qty/margin 按 entry 价回填",
      pos_dw is not None and pos_dw["entry_price"] == 91.0
      and abs(pos_dw["margin"] - 10.0) < 1e-9
      and abs(pos_dw["qty"] - 10.0 * 10.0 / 91.0) < 1e-6, str(pos_dw))
check("entry_ts=成交时刻（非计划创建时刻）",
      pos_dw and abs(float(pos_dw["entry_ts"]) - (T2 + 180)) < 1e-6,
      str(pos_dw and pos_dw["entry_ts"]))
check("成交本身不写台账（只有平仓才写）",
      len(jtt.trades(SYM, limit=500)) == n_trades_before)
jtt._fetch_bars = lambda symbol, tf: None

# d) pullback 挂计划 + 信号反向撤销（flip 撤后按新方向重新评估）
set_signal("15m", "elliott", "bullish",
           {"side": "long", "entry": 92.0, "entry_type": "pullback",
            "stop_loss": 88.0, "take_profit": 105.0})
out = jtt.run_cycle(cfg={}, now=T2 + 240)
check("pullback 未触达（现价95>92）→ 挂计划",
      len(out["symbols"][SYM]["planned"]) == 1,
      str(out["symbols"][SYM]["planned"]))
set_signal("15m", "elliott", "bearish",
           {"side": "short", "entry": 90.0, "entry_type": "breakout",
            "stop_loss": 96.0, "take_profit": 80.0})
out = jtt.run_cycle(cfg={}, now=T2 + 300)
r_sym = out["symbols"][SYM]
cx = [c for c in r_sym["canceled"] if (c["tf"], c["system"]) == ("15m", "elliott")]
check("信号反向 → 旧计划失效（reason=flip）",
      len(cx) == 1 and cx[0]["reason"] == "flip", str(r_sym["canceled"]))
cxr = [c for c in jtt.canceled_positions(SYM)
       if (c["tf"], c["system"]) == ("15m", "elliott")]
check("失效行留痕（status=canceled + cancel_reason=flip + canceled_ts）",
      len(cxr) == 1 and cxr[0]["cancel_reason"] == "flip"
      and cxr[0]["canceled_ts"] is not None
      and abs(float(cxr[0]["entry_price"]) - 92.0) < 1e-9, str(cxr))
check("flip 撤销后同轮按新方向重挂（short breakout 90 未触达）",
      len(r_sym["planned"]) == 1 and r_sym["planned"][0]["direction"] == "short",
      str(r_sym["planned"]))
set_signal("15m", "elliott", "neutral", None)
out = jtt.run_cycle(cfg={}, now=T2 + 360)
cx = [c for c in out["symbols"][SYM]["canceled"]
      if (c["tf"], c["system"]) == ("15m", "elliott")]
check("信号转 neutral → 撤计划（reason=neutral）",
      len(cx) == 1 and cx[0]["reason"] == "neutral"
      and not [p for p in jtt.pending_positions(SYM)
               if (p["tf"], p["system"]) == ("15m", "elliott")], str(cx))

# e) 挂单超时撤销（5m 档 1 天）：超时轮不重挂，下一轮信号仍在才重新评估
T3 = T2 + 420
set_signal("5m", "gap", "bullish",
           {"side": "long", "entry": 110.0, "entry_type": "breakout",
            "stop_loss": 100.0, "take_profit": 130.0})
out = jtt.run_cycle(cfg={}, now=T3)
check("超时用例挂计划", len(out["symbols"][SYM]["planned"]) == 1,
      str(out["symbols"][SYM]["planned"]))
out = jtt.run_cycle(cfg={}, now=T3 + 1.01 * 86400)
r_sym = out["symbols"][SYM]
cx = [c for c in r_sym["canceled"] if (c["tf"], c["system"]) == ("5m", "gap")]
check("挂单超 5m 档 1 天 → 撤计划（reason=timeout），本轮不重挂",
      len(cx) == 1 and cx[0]["reason"] == "timeout" and not r_sym["planned"],
      str((r_sym["canceled"], r_sym["planned"])))
out = jtt.run_cycle(cfg={}, now=T3 + 1.01 * 86400 + 60)
check("下一轮信号仍在 → 重新挂计划（重新计时）",
      len(out["symbols"][SYM]["planned"]) == 1,
      str(out["symbols"][SYM]["planned"]))

# f) 现价已触达计划价 → 维持现状：按现价立即开仓（不挂计划）
set_signal("30m", "gann", "bullish",
           {"side": "long", "entry": 96.0, "entry_type": "pullback",
            "stop_loss": 90.0, "take_profit": 105.0})
out = jtt.run_cycle(cfg={}, now=T3 + 1.01 * 86400 + 120)
r_sym = out["symbols"][SYM]
op = [o for o in r_sym["opened"] if (o["tf"], o["system"]) == ("30m", "gann")]
check("现价已触达（95≤96 pullback）→ 立即按现价 95 开仓",
      len(op) == 1 and abs(op[0]["entry_price"] - 95.0) < 1e-9
      and not [p for p in r_sym["planned"]
               if (p["tf"], p["system"]) == ("30m", "gann")], str(op))
check("计划全周期未污染台账（trade 行数不变）",
      len(jtt.trades(SYM, limit=500)) == n_trades_before)

# ═══════════ 13.8 R1/R2 补充：market 限价点位语义 + 失效后触达不成交（时序例） ═══════════
T4 = T3 + 1.01 * 86400 + 180

# R1) market 带有效点位：未触达不立即开仓，挂 pending；触达按计划价成交
_PRICE["v"] = 100.0
set_signal("15m", "elliott", "bearish",
           {"side": "short", "entry": 104.0, "entry_type": "market",
            "stop_loss": 110.0, "take_profit": 92.0})
out = jtt.run_cycle(cfg={}, now=T4)
r_sym = out["symbols"][SYM]
pl = [p for p in r_sym["planned"] if (p["tf"], p["system"]) == ("15m", "elliott")]
check("R1：market 带点位未触达（做空104 现价100 未达）→ 挂计划不立即开仓",
      len(pl) == 1 and not [o for o in r_sym["opened"]
                            if (o["tf"], o["system"]) == ("15m", "elliott")],
      str((r_sym["planned"], r_sym["opened"])))
_PRICE["v"] = 104.5
out = jtt.run_cycle(cfg={}, now=T4 + 60)
fl = [f for f in out["symbols"][SYM]["filled"]
      if (f["tf"], f["system"]) == ("15m", "elliott")]
check("R1：现价 104.5 ≥ 点位 104（做空触发）→ 成交且成交价=计划价 104",
      len(fl) == 1 and abs(fl[0]["entry_price"] - 104.0) < 1e-9, str(fl))

# R2 时序) pending 期间信号变更 → 旧计划失效；之后价格触达旧点位也不成交
#   前置：5m gap 有一条 breakout 做多 @110 的 pending（来自 13.7e 重挂）
pends_gap = [p for p in jtt.pending_positions(SYM)
             if (p["tf"], p["system"]) == ("5m", "gap")]
check("R2 前置：5m gap 存在未成交挂单 @110", len(pends_gap) == 1
      and abs(float(pends_gap[0]["entry_price"]) - 110.0) < 1e-9, str(pends_gap))
set_signal("5m", "gap", "neutral", None)
out = jtt.run_cycle(cfg={}, now=T4 + 120)
cx = [c for c in out["symbols"][SYM]["canceled"]
      if (c["tf"], c["system"]) == ("5m", "gap")]
check("R2：信号转中性 → 旧计划失效（reason=neutral）",
      len(cx) == 1 and cx[0]["reason"] == "neutral", str(cx))
cxr = [c for c in jtt.canceled_positions(SYM)
       if (c["tf"], c["system"]) == ("5m", "gap")
       and c["cancel_reason"] == "neutral"]
check("R2：失效行留痕（cancel_reason=neutral + canceled_ts）",
      len(cxr) == 1 and cxr[0]["canceled_ts"] is not None, str(cxr))
_PRICE["v"] = 111.0   # 价格随后真正触达旧点位 110
out = jtt.run_cycle(cfg={}, now=T4 + 180)
r_sym = out["symbols"][SYM]
check("R2 时序：失效后价格触达旧点位 110 → 不成交不开仓（一切以最新信号为准）",
      not [f for f in r_sym["filled"] if (f["tf"], f["system"]) == ("5m", "gap")]
      and not [o for o in r_sym["opened"] if (o["tf"], o["system"]) == ("5m", "gap")]
      and not [p for p in jtt.open_positions(SYM)
               if (p["tf"], p["system"]) == ("5m", "gap")],
      str((r_sym["filled"], r_sym["opened"])))
logs_gap = [l for l in jtt.signal_logs(SYM, "5m", "gap")
            if l["change_kinds"] == "cancel"]
check("R2：失效落变更日志（change_kinds=cancel，note 说明不再成交）",
      len(logs_gap) >= 1 and "已失效" in str(logs_gap[0]["note"]), str(logs_gap[:1]))

# ═══════════ 14. 只读约束 + 状态汇总 ═══════════
with jsh._conn() as conn:
    n_sig = conn.execute("SELECT COUNT(*) AS n FROM twelve_signal_state").fetchone()["n"]
check("twelve_signal_state 只读（引擎未增删信号行）", n_sig == 6, str(n_sig))

st = jtt.status(SYM)
check("status 汇总：72 槽位 + 在途数/计划数正确",
      len(st["wallets"]) == 72
      and st["open_positions"] == len(jtt.open_positions(SYM))
      and st["pending_plans"] == len(jtt.pending_positions(SYM)),
      str((st["open_positions"], st["pending_plans"])))
md = jtt.to_markdown(st)
check("markdown 报表可渲染", md.startswith("#") and "|" in md)

# run_cycle 无配置币种的兜底提示
with jtt._conn() as conn:
    conn.execute("UPDATE twelve_sim_config SET enabled=0 "
                 "WHERE symbol=? AND scope_tf IS NULL AND scope_system IS NULL", (SYM,))
out_none = jtt.run_cycle(cfg={})
check("无启用币种 → note 提示不报错", "note" in out_none and not out_none["symbols"],
      str(out_none))

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS ✅")
raise SystemExit(1 if fails else 0)
