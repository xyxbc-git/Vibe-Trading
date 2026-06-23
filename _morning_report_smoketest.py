"""离线 smoketest：T-14 每日晨报。不联网（打桩 radar/circuit/journal）。

验证：
  1) build 聚合三段，单段异常隔离不崩
  2) to_text 含日期/风控/雷达/信号行
  3) 无达标信号时给观望提示
  4) send dry-run 不真发
  5) daemon.morning_step 永不抛出且返回结构
"""
import sys
import types

import jarvis_morning_report as jmr

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ── 打桩 radar / circuit_breaker / journal ──
fake_radar = types.ModuleType("jarvis_radar")
fake_radar.scan = lambda symbols=None, min_conviction=0.8: {
    "scanned": 3, "actionable": [
        {"symbol": "BTCUSDT", "direction": "偏多（战术）", "conviction_score": 0.9,
         "position_pct": 20, "position_pct_adjusted": 15, "lesson_count": 1},
        {"symbol": "ETHUSDT", "direction": "偏多（战术）", "conviction_score": 0.85,
         "position_pct": 15, "lesson_count": 0},
    ],
    "portfolio": {"naive_total_pct": 35, "effective_before_pct": 30, "scaled": False},
}
sys.modules["jarvis_radar"] = fake_radar

fake_cb = types.ModuleType("jarvis_circuit_breaker")
fake_cb.evaluate = lambda: {"drawdown_pct": -5.2, "already_tripped": False,
                            "should_halt": False, "equity_usdt": 1000.0}
sys.modules["jarvis_circuit_breaker"] = fake_cb

fake_jj = types.ModuleType("jarvis_journal")
fake_jj.report = lambda sym: {"hit_rate_pct": 57.0, "evaluated": 40}
sys.modules["jarvis_journal"] = fake_jj

# ── 1. build 聚合 ──
rep = jmr.build(["BTCUSDT", "ETHUSDT"])
check("build 有 radar 段", rep["radar"]["ok"] is True)
check("build 有 circuit 段", rep["circuit"]["ok"] is True)
check("build 有 journal 段", rep["journal"]["ok"] is True)

# ── 2. to_text 内容 ──
txt = jmr.to_text(rep)
check("文本含日期", rep["date"] in txt)
check("文本含风控", "风控" in txt and "✅ 正常" in txt)
check("文本含雷达", "机会雷达" in txt)
check("文本含 BTC 信号", "BTCUSDT" in txt and "折算15%" in txt, txt)
check("文本含战绩", "命中率 57.0%" in txt)

# ── 3. 无信号观望 ──
fake_radar.scan = lambda symbols=None, min_conviction=0.8: {"scanned": 3, "actionable": [], "portfolio": None}
rep2 = jmr.build(["BTCUSDT"])
txt2 = jmr.to_text(rep2)
check("无信号给观望", "无达标信号" in txt2)

# ── 4. send dry-run ──
fake_notify = types.ModuleType("jarvis_notify")
sent = {}
fake_notify.notify = lambda text, dry_run=False: sent.update({"text": text, "dry_run": dry_run}) or {"dry_run": dry_run}
sys.modules["jarvis_notify"] = fake_notify
out = jmr.send(rep, dry_run=True)
check("send dry-run", out.get("dry_run") is True and sent.get("dry_run") is True)

# ── 5. 单段异常隔离 ──
def boom(*a, **k):
    raise RuntimeError("radar down")
fake_radar.scan = boom
rep3 = jmr.build(["BTCUSDT"])
check("radar 异常隔离", rep3["radar"]["ok"] is False and "error" in rep3["radar"])
check("异常时其它段仍在", rep3["circuit"]["ok"] is True)
# to_text 仍可生成
txt3 = jmr.to_text(rep3)
check("异常时文本仍生成", "机会雷达：不可用" in txt3)

# ── 6. daemon.morning_step 永不抛出 ──
import jarvis_daemon as jd
ms = jd.morning_step(["BTCUSDT"], notify=True, dry_run=True)
check("morning_step 返回结构", isinstance(ms, dict) and "finished_at" in ms)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
