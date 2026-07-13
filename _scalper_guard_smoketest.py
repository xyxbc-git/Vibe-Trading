#!/usr/bin/env python3
"""[Sprint1 P1-3] 15m 短线引擎统一门禁 smoketest：_open_position 接入
jarvis_circuit_breaker.guard_new_order（组合熔断 + 冷静期），拦截开仓、
平仓不受限、门禁异常放行不拖垮循环。全离线（monkeypatch 注入）。"""

from __future__ import annotations

import jarvis_circuit_breaker as jcb
import jarvis_scalper_trader as jst

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


CFG = {"risk": {"single_trade_risk": 0.01}}


def fresh_state() -> dict:
    return {"positions": [], "daily_trades": 0, "total_trades": 0}


# ── 1. 门禁放行 → 正常开仓 ──
jcb.guard_new_order = lambda cfg=None: {"allow": True, "reason": "ok"}
st = fresh_state()
pos = jst._open_position(st, CFG, "long", 100.0, 1000.0, "BTCUSDT")
check("门禁放行正常开仓", pos is not None and len(st["positions"]) == 1, str(pos))

# ── 2. 冷静期锁单 → 拦截开仓，不抛异常 ──
jcb.guard_new_order = lambda cfg=None: {
    "allow": False, "reason": "冷静期锁单中（smoketest 注入）"}
st = fresh_state()
pos = jst._open_position(st, CFG, "long", 100.0, 1000.0, "BTCUSDT")
check("冷静期门禁拦截开仓", pos is None and len(st["positions"]) == 0, str(pos))
check("拦截不计入交易次数", st["daily_trades"] == 0 and st["total_trades"] == 0)

# ── 3. 锁单期间平仓不受限（_close_position 不经门禁）──
jcb.guard_new_order = lambda cfg=None: {"allow": True, "reason": "ok"}
st = fresh_state()
pos = jst._open_position(st, CFG, "long", 100.0, 1000.0, "ETHUSDT")
jcb.guard_new_order = lambda cfg=None: {
    "allow": False, "reason": "冷静期锁单中（smoketest 注入）"}
pnl = jst._close_position(st, pos, 105.0, "take_profit")
check("锁单期间平仓不受限", pos["status"] == "closed" and pnl > 0,
      f"status={pos['status']} pnl={pnl}")

# ── 4. 门禁自身异常 → 放行不拖垮引擎（paper-only 语义）──
def _guard_boom(cfg=None):
    raise RuntimeError("guard broken")


jcb.guard_new_order = _guard_boom
st = fresh_state()
pos = jst._open_position(st, CFG, "short", 100.0, 1000.0, "SOLUSDT")
check("门禁异常放行不拖垮", pos is not None and len(st["positions"]) == 1, str(pos))

# ── 5. 仓位过小仍走原有拒绝路径（门禁之后的原逻辑不受影响）──
jcb.guard_new_order = lambda cfg=None: {"allow": True, "reason": "ok"}
st = fresh_state()
pos = jst._open_position(st, CFG, "long", 100.0, 50.0, "BTCUSDT")  # 50×1%=0.5U < 1U
check("原有仓位过小拒绝逻辑不受影响", pos is None, str(pos))

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
