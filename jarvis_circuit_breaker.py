#!/usr/bin/env python3
"""JARVIS circuit breaker (T-09): portfolio-level drawdown + flash-crash / anomaly halt.

Paper-only safety valve. Existing guardrails only act on a *single* order at placement
time; there is no *portfolio-level* halt. This module fills that gap:

  - Monitors portfolio equity (via jarvis_paper_trader.stats) against a persisted peak.
  - Trips a GLOBAL halt when any of these breach thresholds:
      1) portfolio drawdown from peak  (drawdown_halt_pct, default 20%)
      2) any single open position loss  (position_loss_halt_pct, default 25%)
      3) any held symbol 24h crash      (flash_crash_24h_pct, default 15%)
  - A price-anomaly guard (depeg_deviation_pct) treats absurd moves as data glitches and
    does NOT auto-trip on them alone (avoids false halts from bad ticks / depeg artifacts).
  - On trip: cancel open orders (kill-switch + local pending), alert, and block new orders.

Thresholds come from ~/.vibe-trading/executor_config.json (or env via jarvis_executor),
never hardcoded secrets. State is persisted in ~/.vibe-trading/circuit_breaker.json.

NOTE: "flash crash / pin-bar" is approximated here with daily (24h) klines. True
intraday pin-bar / depeg detection needs minute data (left as a documented follow-up).

Usage:
  python jarvis_circuit_breaker.py status      # show current evaluation, no side effects
  python jarvis_circuit_breaker.py check       # evaluate and trip if breached
  python jarvis_circuit_breaker.py reset       # clear halt (manual recovery)
  python jarvis_circuit_breaker.py guard       # gate decision for an order entry point
"""
from __future__ import annotations

import json
import os
import time

import jarvis_executor as jx
import jarvis_paper_trader as jpt

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
STATE_PATH = os.path.join(CONFIG_DIR, "circuit_breaker.json")
LOG_PATH = os.path.join(CONFIG_DIR, "jarvis_circuit_breaker.log")

DEFAULTS = {
    "drawdown_halt_pct": 20.0,
    "position_loss_halt_pct": 25.0,
    "flash_crash_24h_pct": 15.0,
    "depeg_deviation_pct": 35.0,
}


def _log(msg: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _thresholds(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = cfg.get(k)
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def _read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"tripped": False, "peak_equity": None, "reason": None, "ts": None}


def _write_state(st: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def is_tripped() -> bool:
    return bool(_read_state().get("tripped"))


def _daily_change_pct(cfg: dict, symbol: str):
    """24h change via last 2 daily klines. Returns pct or None (graceful degrade)."""
    sym = symbol if symbol.endswith("USDT") else symbol + "USDT"
    try:
        import jarvis_factor_backtest as fb
        kl = fb._get(f"{fb.SPOT_API}/api/v3/klines",
                     {"symbol": sym, "interval": "1d", "limit": 2})
        if isinstance(kl, list) and len(kl) >= 2:
            prev_close = float(kl[-2][4])
            last_close = float(kl[-1][4])
            if prev_close > 0:
                return round((last_close / prev_close - 1.0) * 100, 2)
    except Exception:  # noqa: BLE001
        pass
    return None


def _summarize(triggers: list) -> str:
    parts = []
    for t in triggers:
        if t["type"] == "portfolio_drawdown":
            parts.append(f"drawdown {t['value_pct']}% (limit {t['limit_pct']}%)")
        elif t["type"] == "position_loss":
            parts.append(f"{t['symbol']} pos-loss {t['value_pct']}% (limit {t['limit_pct']}%)")
        elif t["type"] == "flash_crash_24h":
            parts.append(f"{t['symbol']} 24h crash {t['value_pct']}% (limit {t['limit_pct']}%)")
    return "; ".join(parts) if parts else "none"


def evaluate(cfg: dict | None = None) -> dict:
    """Compute breaker triggers WITHOUT side effects."""
    cfg = cfg or jx.load_config()
    th = _thresholds(cfg)
    st = _read_state()
    triggers: list = []
    try:
        s = jpt.stats(cfg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:200], "triggers": [], "should_halt": False}

    equity = s.get("equity_usdt")
    peak = st.get("peak_equity")
    if equity is not None and (peak is None or equity > peak):
        peak = equity
    drawdown_pct = round((equity / peak - 1.0) * 100, 2) if peak else 0.0

    if peak and equity is not None and drawdown_pct <= -th["drawdown_halt_pct"]:
        triggers.append({"type": "portfolio_drawdown",
                         "value_pct": drawdown_pct, "limit_pct": -th["drawdown_halt_pct"]})

    for d in s.get("open_detail", []):
        entry, cur = d.get("entry_price"), d.get("cur_price")
        if entry and cur:
            chg = round((cur / entry - 1.0) * 100, 2)
            if chg <= -th["position_loss_halt_pct"]:
                triggers.append({"type": "position_loss", "symbol": d.get("symbol"),
                                 "value_pct": chg, "limit_pct": -th["position_loss_halt_pct"]})

    for d in s.get("open_detail", []):
        sym = d.get("symbol")
        chg24 = _daily_change_pct(cfg, sym)
        if chg24 is None:
            continue
        if chg24 <= -th["depeg_deviation_pct"]:
            triggers.append({"type": "price_anomaly_skip", "symbol": sym, "value_pct": chg24})
            continue
        if chg24 <= -th["flash_crash_24h_pct"]:
            triggers.append({"type": "flash_crash_24h", "symbol": sym,
                             "value_pct": chg24, "limit_pct": -th["flash_crash_24h_pct"]})

    actionable = [t for t in triggers if t["type"] != "price_anomaly_skip"]
    return {"ok": True, "equity_usdt": equity, "peak_equity": peak,
            "drawdown_pct": drawdown_pct, "triggers": triggers,
            "should_halt": bool(actionable), "thresholds": th,
            "already_tripped": bool(st.get("tripped"))}


def trip(reason: str, cfg: dict | None = None,
         do_kill: bool = True, do_notify: bool = True) -> dict:
    """Set tripped state, cancel open orders (kill-switch + local), alert."""
    cfg = cfg or jx.load_config()
    st = _read_state()
    st.update({"tripped": True, "reason": reason, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    _write_state(st)
    _log("TRIPPED: " + reason)
    result = {"tripped": True, "reason": reason}

    if do_kill:
        try:
            if cfg.get("agent_token"):
                result["kill_switch"] = jx.kill_switch(cfg)
        except Exception as exc:  # noqa: BLE001
            _log("kill-switch error: " + repr(exc)[:160])
        try:
            import jarvis_wallet as jw
            cancelled = 0
            for o in jw.pending_orders():
                oid = o.get("id", o.get("order_id"))
                if oid is not None:
                    jw.cancel_limit_order(oid)
                    cancelled += 1
            result["local_orders_cancelled"] = cancelled
        except Exception as exc:  # noqa: BLE001
            _log("local cancel error: " + repr(exc)[:160])

    if do_notify:
        try:
            import jarvis_notify as jn
            jn.notify("[JARVIS CIRCUIT BREAKER] global halt -> " + reason)
        except Exception as exc:  # noqa: BLE001
            _log("notify error: " + repr(exc)[:160])

    return result


def reset() -> dict:
    """Clear halt (manual recovery). Keep peak equity so drawdown stays meaningful."""
    st = _read_state()
    st.update({"tripped": False, "reason": None, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    _write_state(st)
    _log("RESET: halt cleared")
    return {"tripped": False}


def guard_new_order(cfg: dict | None = None) -> dict:
    """Gate for order entry points. Returns {'allow': bool, 'reason': str, ...}."""
    cfg = cfg or jx.load_config()
    if is_tripped():
        st = _read_state()
        return {"allow": False, "reason": "circuit breaker tripped: " + str(st.get("reason"))}
    ev = evaluate(cfg)
    if not ev.get("ok"):
        # evaluation failed (e.g. no data); fail-safe is to allow but log, since paper-only
        _log("guard: evaluate failed, allowing (paper-only): " + str(ev.get("error")))
        return {"allow": True, "reason": "evaluate failed, allowed (paper-only)"}
    if ev.get("should_halt"):
        reason = _summarize(ev["triggers"])
        trip(reason, cfg)
        return {"allow": False, "reason": "circuit breaker tripped now: " + reason,
                "drawdown_pct": ev.get("drawdown_pct")}
    st = _read_state()
    st.update({"peak_equity": ev.get("peak_equity"), "tripped": False})
    _write_state(st)
    return {"allow": True, "reason": "ok", "drawdown_pct": ev.get("drawdown_pct"),
            "equity_usdt": ev.get("equity_usdt"), "peak_equity": ev.get("peak_equity")}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="JARVIS circuit breaker (T-09)")
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["status", "check", "reset", "guard"])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = jx.load_config()

    if args.cmd == "reset":
        out = reset()
    elif args.cmd == "check":
        ev = evaluate(cfg)
        if ev.get("should_halt"):
            trip(_summarize(ev["triggers"]), cfg)
        out = ev
    elif args.cmd == "guard":
        out = guard_new_order(cfg)
    else:
        out = evaluate(cfg)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if args.cmd in ("status", "check"):
            print(f"equity={out.get('equity_usdt')} peak={out.get('peak_equity')} "
                  f"drawdown={out.get('drawdown_pct')}% should_halt={out.get('should_halt')} "
                  f"tripped={out.get('already_tripped')}")
            for t in out.get("triggers", []):
                print("  trigger:", t)
        else:
            print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
