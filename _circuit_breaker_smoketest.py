"""Offline smoketest for jarvis_circuit_breaker (T-09). Temp DB + monkeypatch, no network."""
import os
import tempfile

import jarvis_journal as jj

_d = tempfile.mkdtemp()
jj.DB_DIR = _d
jj.DB_PATH = os.path.join(_d, "test.db")

import jarvis_executor as jx
import jarvis_paper_trader as jpt
import jarvis_notify as jn
import jarvis_circuit_breaker as cb

cb.STATE_PATH = os.path.join(_d, "cb.json")
cb.LOG_PATH = os.path.join(_d, "cb.log")
cb._daily_change_pct = lambda cfg, sym: None       # no network by default
cb._cooldown_hours = lambda: 0.0                   # 旧场景禁用冷静期（新场景单测）
jn.notify = lambda *a, **k: {"skipped": True}      # no alert in test

cfg = jx.load_config()
cfg["account_equity_usdt"] = 1000.0
cfg["agent_token"] = ""                            # disable real kill-switch in trip

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def set_stats(equity, open_detail=None):
    jpt.stats = lambda c=None, symbol=None: {"equity_usdt": equity, "open_detail": open_detail or []}


# 0. clean state
cb._write_state({"tripped": False, "peak_equity": None, "reason": None})

# 1. healthy -> allow, peak recorded
set_stats(1000.0)
g = cb.guard_new_order(cfg)
check("healthy allow", g["allow"], str(g))
check("peak set 1000", cb._read_state().get("peak_equity") == 1000.0, str(cb._read_state()))

# 2. portfolio drawdown -25% (>=20%) -> block + trip
set_stats(750.0)
g = cb.guard_new_order(cfg)
check("drawdown blocks order", not g["allow"], str(g))
check("state tripped", cb.is_tripped())

# 3. stays blocked while tripped even if equity recovers
set_stats(1000.0)
g = cb.guard_new_order(cfg)
check("stays blocked while tripped", not g["allow"], str(g))

# 4. reset clears halt
cb.reset()
check("reset clears halt", not cb.is_tripped())
g = cb.guard_new_order(cfg)
check("allow after reset (dd 0)", g["allow"], str(g))

# 5. single position loss -30% (>=25%) -> should_halt
cb._write_state({"tripped": False, "peak_equity": 1000.0})
set_stats(950.0, [{"symbol": "ETHUSDT", "entry_price": 100.0, "cur_price": 70.0, "qty": 1.0}])
ev = cb.evaluate(cfg)
types = [t["type"] for t in ev["triggers"]]
check("position_loss trigger", "position_loss" in types, str(types))
check("should_halt on position loss", ev["should_halt"], str(ev))

# 6. price anomaly (-40% <= depeg 35%) -> skipped, NOT actionable
cb._daily_change_pct = lambda cfg, sym: -40.0
cb._write_state({"tripped": False, "peak_equity": 1000.0})
set_stats(990.0, [{"symbol": "BTCUSDT", "entry_price": 100.0, "cur_price": 99.0, "qty": 1.0}])
ev = cb.evaluate(cfg)
types = [t["type"] for t in ev["triggers"]]
check("anomaly skip present", "price_anomaly_skip" in types, str(types))
check("anomaly NOT actionable", not ev["should_halt"], str(ev))

# 7. flash crash -20% (between flash 15% and depeg 35%) -> trip
cb._daily_change_pct = lambda cfg, sym: -20.0
ev = cb.evaluate(cfg)
types = [t["type"] for t in ev["triggers"]]
check("flash_crash trigger", "flash_crash_24h" in types, str(types))
check("should_halt on flash crash", ev["should_halt"], str(ev))

# ── 8. [Sprint1 T1.5] 冷静期：trip 后锁单，reset 不清锁 ──
import time as _t
cb._daily_change_pct = lambda cfg, sym: None
cb._cooldown_hours = lambda: 4.0                   # 启用冷静期
cb._write_state({"tripped": False, "peak_equity": None, "reason": None})
set_stats(1000.0)
cb.guard_new_order(cfg)                            # 建 peak=1000
set_stats(750.0)
g = cb.guard_new_order(cfg)                        # 回撤 25% → trip + 冷静期
check("cooldown: trip blocks", not g["allow"])
cd = cb.cooldown_status()
check("cooldown active after trip", cd["active"] and not cd["expired"], str(cd))
check("cooldown ~4h remaining", 3.9 * 3600 <= cd["remaining_s"] <= 4 * 3600, str(cd["remaining_s"]))
cb.reset()                                         # 解除熔断 ≠ 解除冷静期
set_stats(1000.0)
g = cb.guard_new_order(cfg)
check("cooldown still blocks after reset", not g["allow"], str(g))
check("cooldown reason mentions 冷静期", "冷静期" in g["reason"], g["reason"])

# 9. 到期但未已阅 → 仍锁；已阅 → 放行
st = cb._read_state()
st["cooldown_until"] = _t.time() - 1               # 模拟到期
cb._write_state(st)
g = cb.guard_new_order(cfg)
check("expired but unacked still blocks", not g["allow"], str(g))
cb.acknowledge_cooldown()
g = cb.guard_new_order(cfg)
check("expired + acked allows", g["allow"], str(g))

# 10. 提前解锁（unlock_cooldown_early = 二次确认后调用）→ 立即放行
set_stats(750.0)
cb.guard_new_order(cfg)                            # 再次 trip（回撤）
cb.reset()
set_stats(1000.0)
check("locked again before unlock", not cb.guard_new_order(cfg)["allow"])
cb.unlock_cooldown_early()
g = cb.guard_new_order(cfg)
check("early unlock allows immediately", g["allow"], str(g))

# 11. 当日亏损归因摘要：结构完整、优雅降级
att = cb.today_loss_attribution()
check("attribution has keys",
      all(k in att for k in ("date", "closed_trades", "by_reason", "by_symbol", "worst_trades")),
      str(list(att.keys())))

# 12. cooldown_hours=0 → 完全禁用（trip 不产生锁单窗口）
cb._cooldown_hours = lambda: 0.0
cb._write_state({"tripped": False, "peak_equity": 1000.0, "reason": None,
                 "cooldown_until": None, "cooldown_acknowledged": False})
set_stats(700.0)
cb.guard_new_order(cfg)                            # trip
cb.reset()
set_stats(1000.0)
g = cb.guard_new_order(cfg)
check("cooldown disabled (0h): allow right after reset", g["allow"], str(g))

print("\n=== " + ("ALL PASS" if not fails else f"FAILED {len(fails)}: {fails}") + " ===")
raise SystemExit(1 if fails else 0)
