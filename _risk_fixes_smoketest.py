"""风控篇 P0×4 修复冒烟测试（任务 L）。临时 SQLite + 全离线打桩，不碰真实库不联网。

覆盖：
  P0-1 做空镜像：stats 浮盈亏/持仓市值/equity、钱包平仓回款、熔断单仓判定
       （含旧 open_detail 只有 'entry' 键的兼容修复断言）
  P0-2 成交摩擦：手续费+滑点数学、资金守恒、限价穿透成交 vs 碰价不成
  P0-3 平仓通知兜底：全局通讯录回退、notify_all_closes 全原因覆盖、
       每单配置勾选项仍生效、通讯录为空不炸
  P0-4 twelve 红线：单笔仓位/组合风险封顶、总持仓数上限、同币冷却拦截
"""
from __future__ import annotations

import json
import os
import tempfile
import time

import jarvis_journal as jj

_d = tempfile.mkdtemp()
jj.DB_DIR = _d
jj.DB_PATH = os.path.join(_d, "test.db")

import jarvis_price_alert as jpa

jpa.CONFIG_DIR = _d
jpa.CONFIG_PATH = os.path.join(_d, "price_alert_config.json")
jpa._DB_INITIALIZED = False

import jarvis_circuit_breaker as jcb

jcb.STATE_PATH = os.path.join(_d, "cb.json")
jcb.LOG_PATH = os.path.join(_d, "cb.log")
jcb._daily_change_pct = lambda cfg, sym: None   # 不联网
_orig_guard = jcb.guard_new_order
jcb.guard_new_order = lambda cfg=None: {"allow": True, "reason": "smoketest"}

import jarvis_config as jc_mod
import jarvis_executor as jx
import jarvis_order_notify as jon
import jarvis_paper_trader as jpt
import jarvis_wallet as jw

jpt._classify_regime = lambda s: "trending"

cfg = jx.load_config()
cfg["account_equity_usdt"] = 1000.0
cfg["agent_token"] = ""
cfg["paper_fee_pct"] = 0.0        # 基线摩擦归零；摩擦数学场景单独开
cfg["paper_slippage_pct"] = 0.0
cfg["twelve_max_open_positions"] = 99
cfg["twelve_reopen_cooldown_min"] = 0

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def _bear(pos_pct=20.0, sl=105.0, tp=80.0, tf="4h"):
    # 止损距离 5%：20%仓位×5%=1.0% 组合风险 ≤ 1.5% 红线 → 不触发缩仓，
    # 保持 qty=2 的确定性断言（红线封顶单独在 P0-4 场景验证）
    return {"direction": "bearish", "confidence": 0.9, "primary_tf": tf,
            "trade_plan": {"stop_loss": sl, "take_profit_1": tp,
                           "position_pct": pos_pct, "basis": ["dow"], "source_tf": tf}}


def _bull(pos_pct=20.0, sl=95.0, tp=110.0, tf="4h"):
    return {"direction": "bullish", "confidence": 0.9, "primary_tf": tf,
            "trade_plan": {"stop_loss": sl, "take_profit_1": tp,
                           "position_pct": pos_pct, "basis": ["turtle"], "source_tf": tf}}


# ═══════════ P0-1 · 做空账目镜像 ═══════════

jw.reset(1000.0)
jpt.latest_price = lambda c, s: 100.0
r = jpt.open_from_twelve("AAAUSDT", cfg, consensus=_bear())
check("空单开仓（entry=100 qty=2）", r.get("action") == "opened" and r.get("side") == "sell"
      and abs(r["qty"] - 2.0) < 1e-9, str(r))
check("开仓后现金 800", abs(jw.get_wallet()["cash_usdt"] - 800.0) < 1e-6)

# stats 镜像：价格跌到 90 → 空单浮盈 +20U
jpt.latest_price = lambda c, s: 90.0
stats_cfg = dict(cfg)
stats_cfg["agent_token"] = "x"   # 仅让 stats 取现价（打桩，不会真联网）
st = jpt.stats(stats_cfg)
check("空单价格跌 → 浮盈 +20U", abs(st["unrealized_pnl_usdt"] - 20.0) < 1e-6,
      f"unrealized={st['unrealized_pnl_usdt']}")
det = st["open_detail"][0]
check("open_detail 含 side/entry_price 键", det.get("side") == "sell"
      and abs(det.get("entry_price") - 100.0) < 1e-9, json.dumps(det))
check("空单市值=冻结名义+浮盈 220", abs(st["holdings_value_usdt"] - 220.0) < 1e-6,
      f"holdings={st['holdings_value_usdt']}")
check("equity=800+220=1020", abs(st["equity_usdt"] - 1020.0) < 1e-6,
      f"equity={st['equity_usdt']}")

# 熔断判定镜像：空单浮盈不触发；空单浮亏 30% 触发
ev = jcb.evaluate(stats_cfg)
check("空单浮盈不触发 position_loss",
      "position_loss" not in [t["type"] for t in ev["triggers"]], str(ev["triggers"]))
jpt.latest_price = lambda c, s: 130.0
ev2 = jcb.evaluate(stats_cfg)
hits = [t for t in ev2["triggers"] if t["type"] == "position_loss"]
check("空单价格涨 30% 触发 position_loss", len(hits) == 1
      and abs(hits[0]["value_pct"] + 30.0) < 0.01, str(ev2["triggers"]))

# 旧 open_detail 只有 'entry' 键（无 entry_price）→ 兼容修复后仍能触发
_orig_stats = jpt.stats
jpt.stats = lambda c=None, symbol=None: {
    "equity_usdt": 1000.0,
    "open_detail": [{"symbol": "XUSDT", "entry": 100.0, "cur_price": 70.0, "qty": 1.0}]}
ev3 = jcb.evaluate(cfg)
check("旧 'entry' 键兼容：多单 -30% 触发（修复前恒不触发）",
      "position_loss" in [t["type"] for t in ev3["triggers"]], str(ev3["triggers"]))
jpt.stats = _orig_stats

# 钱包回款镜像：空单 130 平仓（亏 60U）→ 回款 140，现金 800+140=940 = 1000+pnl
pos = jpt.open_positions("AAAUSDT")[0]
res = jpt._close_position(pos, 130.0, "manual", cfg)
check("空单平仓 pnl=-60U", abs(res["pnl_usdt"] + 60.0) < 1e-6, str(res["pnl_usdt"]))
check("空单回款镜像 140U（修复前会回 260）", abs(res["proceeds_usdt"] - 140.0) < 1e-6,
      str(res["proceeds_usdt"]))
check("资金守恒：现金=1000+pnl=940", abs(jw.get_wallet()["cash_usdt"] - 940.0) < 1e-6,
      f"cash={jw.get_wallet()['cash_usdt']}")

# 空单盈利方向：入场 100 → 75 平仓（赚 50U），回款 250
jw.reset(1000.0)
jpt.latest_price = lambda c, s: 100.0
jpt.open_from_twelve("ABBUSDT", cfg, consensus=_bear())
pos = jpt.open_positions("ABBUSDT")[0]
res = jpt._close_position(pos, 75.0, "manual", cfg)
check("空单盈利 pnl=+50U 回款 250", abs(res["pnl_usdt"] - 50.0) < 1e-6
      and abs(res["proceeds_usdt"] - 250.0) < 1e-6, str(res))
check("资金守恒：现金 1050", abs(jw.get_wallet()["cash_usdt"] - 1050.0) < 1e-6)

# 极端：空单价格翻三倍（亏 400U > 冻结 200U）→ 回款钳 0 不倒欠
jw.reset(1000.0)
jpt.latest_price = lambda c, s: 100.0
jpt.open_from_twelve("ACCUSDT", cfg, consensus=_bear())
pos = jpt.open_positions("ACCUSDT")[0]
res = jpt._close_position(pos, 300.0, "manual", cfg)
check("空单爆仓级亏损回款钳 0（逐仓不倒欠）", res["proceeds_usdt"] == 0.0
      and abs(jw.get_wallet()["cash_usdt"] - 800.0) < 1e-6, str(res["proceeds_usdt"]))

# ═══════════ P0-2 · 成交摩擦（手续费+滑点数学 / 资金守恒） ═══════════

jw.reset(1000.0)
fcfg = dict(cfg)
fcfg["paper_fee_pct"] = 0.05
fcfg["paper_slippage_pct"] = 0.02
jpt.latest_price = lambda c, s: 100.0
r = jpt.open_from_twelve("BBBUSDT", fcfg, consensus=_bull())
eff_entry = 100.0 * 1.0002
check("多单市价开仓滑点 +0.02%", abs(r["entry_price"] - eff_entry) < 1e-9,
      f"entry={r['entry_price']}")
qty = r["qty"]
check("qty 按滑点后价换算 200/100.02", abs(qty - round(200.0 / eff_entry, 8)) < 1e-12,
      f"qty={qty}")
notional_open = qty * eff_entry
fee_open = notional_open * 0.0005
cash_after_open = jw.get_wallet()["cash_usdt"]
check("开仓手续费已扣（现金≈1000-名义-费）",
      abs(cash_after_open - (1000.0 - notional_open - fee_open)) < 1e-6,
      f"cash={cash_after_open}")
led_types = [x["type"] for x in jw.ledger(5)]
check("钱包流水含 fee 类型", "fee" in led_types, str(led_types))

# 平仓 110 市价（sell 滑点 -0.02%）→ 净 pnl = 毛 − 双边费；资金守恒
pos = jpt.open_positions("BBBUSDT")[0]
res = jpt._close_position(pos, 110.0, "take", fcfg)
eff_exit = 110.0 * (1 - 0.0002)
gross = (eff_exit - eff_entry) * qty
fee_close = eff_exit * qty * 0.0005
expect_pnl = round(gross - fee_open - fee_close, 4)
check("净 pnl=毛-双边费", abs(res["pnl_usdt"] - expect_pnl) < 0.001,
      f"pnl={res['pnl_usdt']} expect={expect_pnl}")
cash_final = jw.get_wallet()["cash_usdt"]
check("资金守恒：现金≈1000+净pnl", abs(cash_final - (1000.0 + expect_pnl)) < 0.001,
      f"cash={cash_final}")
check("pnl_pct=净值口径", abs(res["pnl_pct"] - round(expect_pnl / (eff_entry * qty) * 100, 2)) < 0.01,
      f"pct={res['pnl_pct']}")

# 摩擦归零开关：fee=slip=0 → 完全无摩擦（回归旧数学）
jw.reset(1000.0)
jpt.latest_price = lambda c, s: 100.0
r0 = jpt.open_from_twelve("BCCUSDT", cfg, consensus=_bull())
check("摩擦归零：entry 无滑点", abs(r0["entry_price"] - 100.0) < 1e-12, str(r0["entry_price"]))
pos = jpt.open_positions("BCCUSDT")[0]
res0 = jpt._close_position(pos, 110.0, "take", cfg)
check("摩擦归零：pnl=+20 整", abs(res0["pnl_usdt"] - 20.0) < 1e-9, str(res0["pnl_usdt"]))

# 限价穿透成交 vs 碰价不成
jw.reset(10000.0)
lo = jw.place_limit_order("CCCUSDT", "buy", 50000.0, 0.01)
jpt.latest_price = lambda c, s: 50000.0          # 碰价
m = jpt.match_limit_orders(cfg)
check("买单碰价不成交", m == [], str(m))
jpt.latest_price = lambda c, s: 49999.0          # 穿透
m = jpt.match_limit_orders(cfg)
check("买单穿透成交（按限价）", len(m) == 1 and m[0]["fill_price"] == 50000.0, str(m))
lo2 = jw.place_limit_order("CCCUSDT", "sell", 60000.0, 0.01)
jpt.latest_price = lambda c, s: 60000.0          # 碰价
m2 = jpt.match_limit_orders(cfg)
check("卖单碰价不成交", m2 == [], str(m2))
jpt.latest_price = lambda c, s: 60000.5          # 穿透
m2 = jpt.match_limit_orders(cfg)
check("卖单穿透成交 → 平仓（限价保护无滑点）",
      len(m2) == 1 and m2[0]["close"]["exit_price"] == 60000.0, str(m2))

# ═══════════ P0-4 · twelve 红线（封顶 / 总仓上限 / 冷却） ═══════════

jw.reset(1000.0)
jpt.latest_price = lambda c, s: 100.0
# plan 要 80% 仓位、止损距离 5% → 先钳到 40%（单笔上限），组合风险 2%>1.5% 再缩到 30%
r = jpt.open_from_twelve("DDDUSDT", cfg, consensus=_bull(pos_pct=80.0, sl=95.0))
check("红线封顶：80%→30%（40%上限+1.5%风险缩仓）",
      r.get("action") == "opened" and abs(r["position_pct"] - 30.0) < 1e-9 and r["capped"],
      f"pos_pct={r.get('position_pct')} capped={r.get('capped')}")
check("封顶后 qty=300/100=3", abs(r["qty"] - 3.0) < 1e-9, str(r["qty"]))

# 总持仓数上限（含全部未平仓，非仅 twelve）：当前 DDDUSDT 1 仓 open，上限 1 → 拦截
lcfg = dict(cfg)
lcfg["twelve_max_open_positions"] = 1
r = jpt.open_from_twelve("EEEUSDT", lcfg, consensus=_bull())
check("总持仓数触顶拦截", r.get("action") == "skip" and "上限" in r.get("reason", ""), str(r))

# 同币冷却：平掉 DDDUSDT 后 60 分钟内不许重开；冷却=0 立即可开
pos = jpt.open_positions("DDDUSDT")[0]
jpt._close_position(pos, 100.0, "manual", cfg)
ccfg = dict(cfg)
ccfg["twelve_reopen_cooldown_min"] = 60
r = jpt.open_from_twelve("DDDUSDT", ccfg, consensus=_bull())
check("同币平仓后冷却拦截", r.get("action") == "skip" and "冷却" in r.get("reason", ""), str(r))
r = jpt.open_from_twelve("DDDUSDT", cfg, consensus=_bull())   # cfg 冷却=0
check("冷却=0 立即可重开", r.get("action") == "opened", str(r))
# 冷却只看同币：FFFUSDT 从未平仓过，带冷却配置也能开
r = jpt.open_from_twelve("FFFUSDT", ccfg, consensus=_bull(pos_pct=5.0))
check("冷却不误伤其它币", r.get("action") == "opened", str(r))

# ═══════════ P0-3 · 平仓通知兜底 ═══════════

SENT: list[dict] = []


def _fake_send(subject, body, to_list, cfg=None, dry_run=False):
    SENT.append({"subject": subject, "to": list(to_list)})
    return {"ok": True, "to": list(to_list)}


jpa.send_email = _fake_send
_orig_load = jpa.load_config


def _load_with_recipients():
    c = _orig_load()
    c["recipients"] = ["global@example.com"]
    return c


jpa.load_config = _load_with_recipients

# 强制 notify_all_closes 走默认 True（隔离用户本机 config.yaml 可能的覆盖）
_orig_get = jc_mod.get
jc_mod.get = lambda k, d=None, path=None: (
    True if k == "notify_all_closes" else _orig_get(k, d, path))

posd = {"id": 424242, "symbol": "GGGUSDT", "side": "buy", "qty": 1.0,
        "entry_price": 100.0, "stop_loss": 90.0, "take_profit": 120.0}

SENT.clear()
r = jon.notify_position_closed(posd, 90.0, "stop", dry_run=True)
check("无每单配置 → 全局通讯录兜底发送", r.get("sent") and r.get("fallback")
      and r.get("to") == ["global@example.com"], str(r))

r = jon.notify_position_closed(posd, 100.0, "manual", dry_run=True)
check("manual 平仓也发（notify_all_closes=True）", r.get("sent"), str(r))
r = jon.notify_position_closed(posd, 100.0, "signal", dry_run=True)
check("signal 平仓也发", r.get("sent"), str(r))
subj = SENT[-1]["subject"]
check("非止盈止损主题含「已平仓」", "已平仓" in subj and "信号反转" in subj, subj)

# notify_all_closes=False → 仅 take/stop
jc_mod.get = lambda k, d=None, path=None: (
    False if k == "notify_all_closes" else _orig_get(k, d, path))
r = jon.notify_position_closed(posd, 100.0, "manual", dry_run=True)
check("开关关闭时 manual 不发", not r.get("sent") and "不触发" in str(r.get("skipped")), str(r))
r = jon.notify_position_closed(posd, 90.0, "stop", dry_run=True)
check("开关关闭时 stop 仍发", r.get("sent"), str(r))
jc_mod.get = lambda k, d=None, path=None: (
    True if k == "notify_all_closes" else _orig_get(k, d, path))

# 每单配置勾选项仍生效：sl 未勾选 → stop 跳过；time 视为需要知情直接发到该邮箱
jon.set_config("pos-424242", "per-order@example.com",
               notify_take_profit=True, notify_stop_loss=False)
r = jon.notify_position_closed(posd, 90.0, "stop", dry_run=True)
check("每单配置未勾选止损 → 跳过（原行为保留）",
      not r.get("sent") and "未勾选" in str(r.get("skipped")), str(r))
r = jon.notify_position_closed(posd, 100.0, "time", dry_run=True)
check("每单配置 + time 平仓 → 发到该单邮箱", r.get("sent")
      and r.get("to") == "per-order@example.com", str(r))
jon.delete_config("pos-424242")

# 全局通讯录为空 + 无每单配置 → 跳过不炸
jpa.load_config = _orig_load   # 临时库 recipients 为空
r = jon.notify_position_closed(posd, 90.0, "stop", dry_run=True)
check("通讯录为空优雅跳过", not r.get("sent") and "通讯录为空" in str(r.get("skipped")), str(r))
jpa.load_config = _load_with_recipients

# 集成：_close_position 全 reason 都尝试通知（time 平仓也发邮件）
SENT.clear()
jw.reset(1000.0)
jpt.latest_price = lambda c, s: 100.0
jpt.open_from_twelve("HHHUSDT", cfg, consensus=_bull())
pos = jpt.open_positions("HHHUSDT")[0]
jpt._close_position(pos, 100.0, "time", cfg)
check("集成：time 平仓经全局兜底发信", len(SENT) == 1 and "已平仓" in SENT[0]["subject"],
      str([s['subject'] for s in SENT]))

# 还原补丁
jc_mod.get = _orig_get
jpa.load_config = _orig_load
jcb.guard_new_order = _orig_guard

print()
if fails:
    print(f"FAILED: {len(fails)} 项 → {fails}")
    raise SystemExit(1)
print("风控篇 P0×4 修复冒烟测试全部通过 ✅")
