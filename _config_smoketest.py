"""离线 smoketest：T-15 统一交易配置中心。不联网。

验证：
  1) 内置默认 = 各脚本改造前硬编码原值（零回归基线）
  2) load 缺失文件回退默认；损坏文件回退默认
  3) clamp 数值夹护栏 / 枚举校验 / 类型转换
  4) save→load 往返一致 + version 累加 + 原子写
  5) 未知键保留（前向兼容）
  6) brief.score_and_plan 接入配置后默认口径 = 原值（0.985/1.005/0.90/1.08/30/40%上限）
  7) brief 改配置后止损/止盈/入场带随配置变化
"""
import os
import tempfile

import jarvis_config as jcfg
import jarvis_brief as jb

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ── 1. 内置默认 = 历史硬编码原值 ──
d = jcfg.DEFAULTS
check("默认 min_conviction=0.8", d["min_conviction"] == 0.8)
check("默认 max_position_pct=40", d["max_position_pct"] == 40.0)
check("默认 max_portfolio_risk_pct=1.5", d["max_portfolio_risk_pct"] == 1.5)
check("默认 stop_loss_drop_pct=10", d["stop_loss_drop_pct"] == 10.0)
check("默认 take_profit_pct=8", d["take_profit_pct"] == 8.0)
check("默认 time_stop_days=30", d["time_stop_days"] == 30)
check("默认 entry_band 1.5/0.5", d["entry_band_below_pct"] == 1.5 and d["entry_band_above_pct"] == 0.5)
check("默认 sizing_method=fixed", d["sizing_method"] == "fixed")
check("默认 watchlist 7 币含 BTCUSDT", "BTCUSDT" in d["watchlist"] and len(d["watchlist"]) == 7)

# ── 2. 缺失/损坏文件回退默认 ──
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "cfg.json")
    cfg = jcfg.load(p)
    check("缺失文件 source=builtin-default", cfg["meta"]["source"] == "builtin-default")
    check("缺失文件 min_conviction=默认", cfg["min_conviction"] == 0.8)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ this is not json ")
    cfg2 = jcfg.load(p)
    check("损坏文件回退默认", cfg2["min_conviction"] == 0.8 and "default" in cfg2["meta"]["source"])

# ── 3. clamp 护栏 / 枚举 / 类型 ──
check("clamp max_position_pct 超上限夹到100", jcfg.clamp("max_position_pct", 999) == 100.0)
check("clamp min_conviction 负值夹到0", jcfg.clamp("min_conviction", -5) == 0.0)
check("clamp time_stop_days 取整", jcfg.clamp("time_stop_days", 12.9) == 12)
check("clamp 枚举非法回退默认", jcfg.clamp("sizing_method", "wild") == "fixed")
check("clamp 枚举合法保留", jcfg.clamp("sizing_method", "kelly") == "kelly")
check("clamp kelly_fraction 夹[0,1]", jcfg.clamp("kelly_fraction", 2.0) == 1.0)
check("coerce watchlist 字符串→列表大写", jcfg.clamp("watchlist", "btc,eth") == ["BTC", "ETH"])

# ── 4. save→load 往返 + version 累加 ──
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "cfg.json")
    c1 = jcfg.save({"min_conviction": 0.9, "max_position_pct": 25}, path=p, note="t")
    check("save version=1", c1["meta"]["version"] == 1)
    c1b = jcfg.load(p)
    check("往返 min_conviction=0.9", c1b["min_conviction"] == 0.9)
    check("往返 max_position_pct=25", c1b["max_position_pct"] == 25.0)
    c2 = jcfg.save({"sizing_method": "kelly"}, path=p)
    check("第二次 save version=2", c2["meta"]["version"] == 2)
    check("第二次 save 保留前值", c2["min_conviction"] == 0.9)
    # save 时越界自动夹紧
    c3 = jcfg.save({"max_portfolio_risk_pct": 999}, path=p)
    check("save 夹护栏 risk<=20", c3["max_portfolio_risk_pct"] == 20.0)

# ── 5. 未知键保留 ──
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "cfg.json")
    jcfg.save({"my_future_key": "x"}, path=p)
    check("未知键保留", jcfg.load(p).get("my_future_key") == "x")

# ── 6. brief 默认口径 = 原硬编码（零回归）──
deriv = {"funding": {"funding_7d_avg_8h_pct": 0.0, "funding_regime": "neutral"}, "long_short": {}}
fac = {"price": 60000.0, "drawdown_from_ath_pct": -35.0, "dd30_signal_active": True,
       "above_ma200": False, "breakout_20d_active": True}
plan = jb.score_and_plan(deriv, fac, fng_now=18)
if plan.get("suggested_position_pct", 0) > 0:
    check("brief 默认止损=price*0.90", plan["stop_loss"] == round(60000 * 0.90, 2), str(plan["stop_loss"]))
    check("brief 默认止盈=price*1.08", plan["take_profit_ref"] == round(60000 * 1.08, 2), str(plan["take_profit_ref"]))
    check("brief 默认入场带 0.985~1.005",
          plan["entry_zone"] == f"{round(60000*0.985,2)} ~ {round(60000*1.005,2)}", plan["entry_zone"])
    check("brief 默认时间止损=30", plan["time_stop_days"] == 30)
    check("brief 仓位上限<=40%", plan["suggested_position_pct"] <= 40)
else:
    check("brief 偏多有仓位(前置)", False, "score 未达阈值")

# ── 7. brief 改配置后随配置变化（用临时配置路径模拟）──
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "cfg.json")
    jcfg.save({"stop_loss_drop_pct": 20, "take_profit_pct": 15}, path=p)
    orig = jcfg.CONFIG_PATH
    jcfg.CONFIG_PATH = p
    try:
        plan2 = jb.score_and_plan(deriv, fac, fng_now=18)
        if plan2.get("suggested_position_pct", 0) > 0:
            check("改配置后止损=price*0.80", plan2["stop_loss"] == round(60000 * 0.80, 2), str(plan2["stop_loss"]))
            check("改配置后止盈=price*1.15", plan2["take_profit_ref"] == round(60000 * 1.15, 2), str(plan2["take_profit_ref"]))
        else:
            check("改配置 brief 偏多有仓位(前置)", False)
    finally:
        jcfg.CONFIG_PATH = orig

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
