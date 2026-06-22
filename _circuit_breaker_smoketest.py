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

print("\n=== " + ("ALL PASS" if not fails else f"FAILED {len(fails)}: {fails}") + " ===")
raise SystemExit(1 if fails else 0)
