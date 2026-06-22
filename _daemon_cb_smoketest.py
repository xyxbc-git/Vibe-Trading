"""Offline smoketest: jarvis_daemon.run_cycle drives circuit-breaker patrol. No network."""
import os
import tempfile

import jarvis_journal as jj

_d = tempfile.mkdtemp()
jj.DB_DIR = _d
jj.DB_PATH = os.path.join(_d, "test.db")

import jarvis_daemon as jd
import jarvis_circuit_breaker as cb

jd.STATUS_PATH = os.path.join(_d, "status.json")
jd.LOG_PATH = os.path.join(_d, "daemon.log")

# stub journal so run_cycle needs no network / brief build
jj.record = lambda sym: {"ok": True, "as_of_date": "2026-06-20",
                         "conviction_score": 0.0, "direction": "neutral"}
jj.evaluate = lambda sym: {"outcomes_filled": 0, "not_due": 0}

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# 1. healthy: no halt -> cb status recorded, no trip
cb.evaluate = lambda cfg=None: {"ok": True, "should_halt": False, "already_tripped": False,
                                "drawdown_pct": 0.0, "equity_usdt": 1000.0}
trip_calls = []
cb.trip = lambda *a, **k: (trip_calls.append(a), {"reason": "x"})[1]
c = jd.run_cycle(["BTCUSDT"], paper_trade=False)
check("healthy cb recorded", c.get("circuit_breaker", {}).get("should_halt") is False,
      str(c.get("circuit_breaker")))
check("healthy no trip", len(trip_calls) == 0)

# 2. breach: should_halt and not tripped -> trip called + cycle tripped
cb.evaluate = lambda cfg=None: {"ok": True, "should_halt": True, "already_tripped": False,
                                "drawdown_pct": -25.0,
                                "triggers": [{"type": "portfolio_drawdown",
                                              "value_pct": -25.0, "limit_pct": -20.0}]}
trip_calls.clear()
cb.trip = lambda reason, *a, **k: (trip_calls.append(reason), {"reason": reason})[1]
c = jd.run_cycle(["BTCUSDT"], paper_trade=False)
check("breach triggers trip", len(trip_calls) == 1, str(trip_calls))
check("breach cycle tripped", c.get("circuit_breaker", {}).get("tripped") is True,
      str(c.get("circuit_breaker")))

# 3. already tripped: do NOT trip again (idempotent), still recorded
cb.evaluate = lambda cfg=None: {"ok": True, "should_halt": True, "already_tripped": True,
                                "drawdown_pct": -25.0, "triggers": []}
trip_calls.clear()
c = jd.run_cycle(["BTCUSDT"], paper_trade=False)
check("no re-trip when already tripped", len(trip_calls) == 0, str(trip_calls))
check("recorded tripped true", c.get("circuit_breaker", {}).get("tripped") is True,
      str(c.get("circuit_breaker")))

# 4. evaluate raises -> isolated; main heartbeat survives
def _boom(cfg=None):
    raise RuntimeError("boom")


cb.evaluate = _boom
c = jd.run_cycle(["BTCUSDT"], paper_trade=False)
check("eval error isolated", "error" in c.get("circuit_breaker", {}), str(c.get("circuit_breaker")))
check("heartbeat survived", "BTCUSDT" in c.get("symbols", {}))

print("\n=== " + ("ALL PASS" if not fails else "FAILED %d: %s" % (len(fails), fails)) + " ===")
raise SystemExit(1 if fails else 0)
