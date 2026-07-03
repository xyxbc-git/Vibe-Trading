"""离线 smoketest：T-18 TradingAgents 辩论增强层（jarvis_debate）。不联网、不起子进程。

验证：
  1) 默认配置 debate_enabled=False → review 返回 skipped 且不跑辩论（零回归）
  2) 结论映射：BUY→agree、HOLD→warn、SELL→warn 模式 warn / veto 模式 veto
  3) 不可用即放行：runner 失败 / 输出不可解析 / runner 抛异常 → skipped 放行
  4) gate 门禁：仅 veto 时 allow=False；其余（含异常）一律放行
  5) ta_symbol 币种映射与 classify_signal 抽取
  6) jarvis_config 新键：默认值 / 枚举校验 / 超时夹紧
"""
import jarvis_config as jc
import jarvis_debate as jd

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


CALLS = {"n": 0}


def runner_never(symbol, date, timeout):
    CALLS["n"] += 1
    raise AssertionError("disabled 时不应调用 runner")


def make_runner(ok=True, decision="", error="boom"):
    def _r(symbol, date, timeout):
        CALLS["n"] += 1
        return {"ok": True, "decision": decision} if ok else {"ok": False, "error": error}
    return _r


# ── 1. 默认关闭 = 零回归 ──
cfg_off = {"debate_enabled": False, "debate_mode": "warn", "debate_timeout_sec": 300}
r = jd.review("BTCUSDT", cfg=cfg_off, _runner=runner_never)
check("默认关闭 verdict=skipped", r["verdict"] == "skipped", r["summary"])
check("默认关闭不跑辩论", CALLS["n"] == 0)
check("默认关闭 enabled=False", r["enabled"] is False)
check("jarvis_config 默认 debate_enabled=False",
      jc.default_config().get("debate_enabled") is False)
check("jarvis_config 默认 debate_mode=warn",
      jc.default_config().get("debate_mode") == "warn")

# ── 2. 结论映射 ──
cfg_warn = {"debate_enabled": True, "debate_mode": "warn", "debate_timeout_sec": 60}
cfg_veto = {"debate_enabled": True, "debate_mode": "veto", "debate_timeout_sec": 60}

r = jd.review("BTCUSDT", cfg=cfg_warn, _runner=make_runner(decision="FINAL TRANSACTION PROPOSAL: **BUY**"))
check("BUY→agree", r["verdict"] == "agree" and r["ta_signal"] == "BUY", str(r["verdict"]))

r = jd.review("BTCUSDT", cfg=cfg_warn, _runner=make_runner(decision="HOLD"))
check("HOLD→warn", r["verdict"] == "warn" and r["ta_signal"] == "HOLD")

r = jd.review("BTCUSDT", cfg=cfg_warn, _runner=make_runner(decision="SELL"))
check("SELL+warn模式→warn 放行", r["verdict"] == "warn" and r["ta_signal"] == "SELL")

r = jd.review("BTCUSDT", cfg=cfg_veto, _runner=make_runner(decision="SELL"))
check("SELL+veto模式→veto", r["verdict"] == "veto")

# 摘要含多个词时取最后出现的最终结论
r = jd.review("BTCUSDT", cfg=cfg_veto, _runner=make_runner(decision="牛方主张 BUY，但风控最终裁定 SELL"))
check("多词取句尾结论 SELL", r["ta_signal"] == "SELL", str(r["ta_signal"]))

# ── 3. 不可用即放行 ──
r = jd.review("BTCUSDT", cfg=cfg_veto, _runner=make_runner(ok=False, error="无 LLM Key"))
check("runner 失败→skipped", r["verdict"] == "skipped" and "无 LLM Key" in r["summary"])

r = jd.review("BTCUSDT", cfg=cfg_veto, _runner=make_runner(decision="???无法解析???"))
check("输出不可解析→skipped", r["verdict"] == "skipped")


def runner_raise(symbol, date, timeout):
    raise RuntimeError("crash")


r = jd.review("BTCUSDT", cfg=cfg_veto, _runner=runner_raise)
check("runner 抛异常→skipped 不抛出", r["verdict"] == "skipped")

# ── 4. gate 门禁 ──
g = jd.gate("BTCUSDT", cfg=cfg_veto, _runner=make_runner(decision="SELL"))
check("gate veto 拦截", g["allow"] is False)
g = jd.gate("BTCUSDT", cfg=cfg_warn, _runner=make_runner(decision="SELL"))
check("gate warn 放行", g["allow"] is True)
g = jd.gate("BTCUSDT", cfg=cfg_veto, _runner=make_runner(ok=False))
check("gate 不可用放行（veto 模式也放）", g["allow"] is True)
g = jd.gate("BTCUSDT", cfg=cfg_off, _runner=runner_never)
check("gate 默认关闭放行", g["allow"] is True)

# ── 5. 符号映射 / 信号抽取 ──
check("BTCUSDT→BTC-USD", jd.ta_symbol("BTCUSDT") == "BTC-USD")
check("ETHUSD→ETH-USD", jd.ta_symbol("ETHUSD") == "ETH-USD")
check("NVDA 原样", jd.ta_symbol("NVDA") == "NVDA")
check("中文 卖出→SELL", jd.classify_signal("最终建议：卖出") == "SELL")
check("空文本→None", jd.classify_signal("") is None)

# ── 6. 配置护栏 ──
check("debate_mode 枚举拒非法值", jc.clamp("debate_mode", "yolo") == "warn")
check("debate_timeout_sec 下限夹紧", jc.clamp("debate_timeout_sec", 1) == 30)
check("debate_timeout_sec 上限夹紧", jc.clamp("debate_timeout_sec", 99999) == 1800)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
