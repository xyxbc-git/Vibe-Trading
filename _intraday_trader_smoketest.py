#!/usr/bin/env python3
"""4h 自动模拟下单引擎离线 smoketest：临时 DB + 注入 predict/price，全离线。"""

from __future__ import annotations

import os
import tempfile
import time

import jarvis_intraday_trader as jit

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


TMP = tempfile.mkdtemp(prefix="jit_test_")
DB = os.path.join(TMP, "test.db")
NOW = int(time.time() * 1000)

# 屏蔽通知与熔断文件副作用
jit._notify = lambda text: None
jit.HALT_PATH = os.path.join(TMP, "halt.json")
jit.RESUME_PATH = os.path.join(TMP, "resume.json")

# [P1-3] 屏蔽真实组合级门禁（读用户真实钱包会引入环境依赖）；
# 拦截行为在场景 14 用注入的 fake guard 单独验证。
import jarvis_circuit_breaker as jcb
jcb.guard_new_order = lambda cfg=None: {"allow": True, "reason": "smoketest"}


def pred_up(sym, prob=0.75):
    return {"symbol": sym, "as_of_bar_ts": NOW - NOW % jit.BAR_MS,
            "direction": "涨", "prob": prob, "entry": 100.0, "stop": 98.0,
            "take": 103.0, "atr_pct": 1.5, "tradeable": True,
            "oos_hit_rate": 0.6, "p_value": 0.03}


def pred_blocked(sym):
    return {"symbol": sym, "as_of_bar_ts": NOW - NOW % jit.BAR_MS,
            "direction": "涨", "prob": 0.9, "entry": 100.0, "stop": 98.0,
            "take": 103.0, "tradeable": False, "reason": "门禁未过"}


# ── 1. 达标开仓 ──────────────────────────────────────────────────────────
r = jit.cycle(["AAAUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=NOW, db_path=DB)
check("达标开多仓", len(r["opened"]) == 1 and r["opened"][0]["side"] == "long", str(r))
st = jit.status(DB)
check("status 见 1 持仓", len(st["open_positions"]) == 1)
check("仓位按风险反推（1%权益/2%止损=50%→夹到40%上限）",
      abs(st["open_positions"][0]["notional_usdt"] - 400.0) < 1e-6,
      str(st["open_positions"][0]["notional_usdt"]))

# ── 2. 同币不重复开 + 幂等 ───────────────────────────────────────────────
r = jit.cycle(["AAAUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.5,
              now_ms=NOW + 1000, db_path=DB)
check("持仓中不重复开", len(r["opened"]) == 0)

# ── 3. 门禁未过不开仓 ────────────────────────────────────────────────────
r = jit.cycle(["BBBUSDT"], predict_fn=pred_blocked, price_fn=lambda s: 100.0,
              now_ms=NOW + 2000, db_path=DB)
check("tradeable=False 不开仓", len(r["opened"]) == 0, str(r["predictions"]))

# ── 4. 低概率不开仓 ──────────────────────────────────────────────────────
r = jit.cycle(["CCCUSDT"], predict_fn=lambda s: pred_up(s, prob=0.55),
              price_fn=lambda s: 100.0, now_ms=NOW + 3000, db_path=DB)
check("prob<0.60 不开仓", len(r["opened"]) == 0)

# ── 5. 止损平仓 ──────────────────────────────────────────────────────────
r = jit.cycle(["AAAUSDT"], predict_fn=pred_up, price_fn=lambda s: 97.0,
              now_ms=NOW + 4000, db_path=DB)
check("跌破止损平仓", len(r["closed"]) == 1 and r["closed"][0]["reason"] == "stop", str(r["closed"]))
check("止损亏损为负", r["closed"][0]["pnl_usdt"] < 0)

# ── 6. 冷却期不开 ────────────────────────────────────────────────────────
r = jit.cycle(["AAAUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=NOW + 5000, db_path=DB)
check("冷却期(1根)内不再开", len(r["opened"]) == 0)

# ── 7. 冷却过后可再开 → 止盈平仓 ─────────────────────────────────────────
t2 = NOW + jit.BAR_MS + 10_000
r = jit.cycle(["AAAUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=t2, db_path=DB)
check("冷却过后可再开", len(r["opened"]) == 1)
r = jit.cycle(["AAAUSDT"], predict_fn=pred_up, price_fn=lambda s: 104.0,
              now_ms=t2 + 1000, db_path=DB)
check("触及止盈平仓", len(r["closed"]) == 1 and r["closed"][0]["reason"] == "take")
check("止盈盈利为正", r["closed"][0]["pnl_usdt"] > 0)

# ── 8. 时间止损 ──────────────────────────────────────────────────────────
t3 = t2 + 2 * jit.BAR_MS
r = jit.cycle(["AAAUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=t3, db_path=DB)
check("再开一仓", len(r["opened"]) == 1)
r = jit.cycle(["AAAUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.2,
              now_ms=t3 + 7 * jit.BAR_MS, db_path=DB)
check("6根(24h)时间止损", len(r["closed"]) == 1 and r["closed"][0]["reason"] == "time",
      str(r["closed"]))

# ── 9. 连亏熔断：先制造 3 连亏 ───────────────────────────────────────────
t4 = t3 + 9 * jit.BAR_MS
for k in range(3):
    tk = t4 + k * 2 * jit.BAR_MS
    jit.cycle(["DDDUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=tk, db_path=DB)
    jit.cycle(["DDDUSDT"], predict_fn=pred_up, price_fn=lambda s: 97.0,
              now_ms=tk + 1000, db_path=DB)
r = jit.cycle(["EEEUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=t4 + 8 * jit.BAR_MS, db_path=DB)
check("连亏3笔触发熔断", r["halted"] is True, str(jit.status(DB)))
check("熔断后拒绝开仓", len(r["opened"]) == 0)

# ── 10. resume 解除熔断后恢复开仓 ────────────────────────────────────────
jit.resume(DB)
r = jit.cycle(["EEEUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=t4 + 9 * jit.BAR_MS, db_path=DB)
check("resume 后恢复开仓", len(r["opened"]) == 1)

# ── 11. 预测落库 + 回填 ──────────────────────────────────────────────────
bar0 = (NOW - NOW % jit.BAR_MS) - 10 * jit.BAR_MS
bars = [{"ts": bar0 + i * jit.BAR_MS, "close": 100.0 + i} for i in range(12)]


def pred_hist(sym):
    p = pred_up(sym)
    p["as_of_bar_ts"] = bar0 + 5 * jit.BAR_MS  # 下一根 close=107 > entry → 涨命中
    p["entry"] = 100.0 + 5
    return p


with jit._conn(DB) as conn:
    jit._record_prediction(conn, pred_hist("FFFUSDT"))
    filled = jit._backfill(conn, "FFFUSDT", bars=bars)
    conn.commit()
    row = conn.execute("SELECT * FROM intraday_predictions WHERE symbol='FFFUSDT'").fetchone()
check("回填 1 条", filled == 1)
check("涨预测且真涨 → hit=1", row["hit"] == 1 and row["outcome_ret"] > 0, str(dict(row)))

# ── 12. 预测异常兜底 ─────────────────────────────────────────────────────
def boom(sym):
    raise RuntimeError("network down")

r = jit.cycle(["GGGUSDT"], predict_fn=boom, price_fn=lambda s: 100.0,
              now_ms=t4 + 10 * jit.BAR_MS, db_path=DB)
check("预测抛异常不拖垮 cycle", "error" not in r and not r["opened"],
      str(r.get("predictions")))

# ── 13. report 可渲染 ────────────────────────────────────────────────────
rep = jit.report(days=365, db_path=DB)
check("report 输出表格", "| 币种 |" in rep and "熔断" in rep)

# ── 14. [P1-3] 统一门禁（组合熔断/冷静期）拦截开仓，平仓不受限 ────────────
jcb.guard_new_order = lambda cfg=None: {
    "allow": False, "reason": "冷静期锁单中（smoketest 注入）"}
t5 = t4 + 12 * jit.BAR_MS
r = jit.cycle(["HHHUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=t5, db_path=DB)
check("[P1-3] 冷静期门禁拦截开仓", len(r["opened"]) == 0, str(r["opened"]))
check("[P1-3] 门禁拦截不报错（循环继续）", "error" not in r)
# 门禁拦截期间已有持仓仍可平仓：先放行开一仓，再锁门禁验证平仓
jcb.guard_new_order = lambda cfg=None: {"allow": True, "reason": "ok"}
r = jit.cycle(["IIIUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=t5 + 1000, db_path=DB)
check("[P1-3] 放行后正常开仓", len(r["opened"]) == 1)
jcb.guard_new_order = lambda cfg=None: {
    "allow": False, "reason": "冷静期锁单中（smoketest 注入）"}
r = jit.cycle(["IIIUSDT"], predict_fn=pred_up, price_fn=lambda s: 97.0,
              now_ms=t5 + 2000, db_path=DB)
# 注意：场景 10 的 EEEUSDT 遗留仓可能同时触发止损，故按 symbol 断言而非计数
_iii_closed = [c for c in r["closed"] if c["symbol"] == "IIIUSDT"]
check("[P1-3] 锁单期间平仓不受限", len(_iii_closed) == 1
      and _iii_closed[0]["reason"] == "stop", str(r["closed"]))
# 门禁自身抛异常 → 放行不拖垮引擎
def _guard_boom(cfg=None):
    raise RuntimeError("guard broken")
jcb.guard_new_order = _guard_boom
r = jit.cycle(["JJJUSDT"], predict_fn=pred_up, price_fn=lambda s: 100.0,
              now_ms=t5 + 3000, db_path=DB)
check("[P1-3] 门禁异常放行不拖垮", len(r["opened"]) == 1 and "error" not in r)
jcb.guard_new_order = lambda cfg=None: {"allow": True, "reason": "smoketest"}

print(f"\n{'='*40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
