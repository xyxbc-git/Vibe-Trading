"""离线 smoketest：T-15 统一交易配置中心 + Sprint0 配置中心化。不联网。

验证：
  1) 内置默认 = 各脚本改造前硬编码原值（零回归基线）
  2) load 缺失文件回退默认；损坏文件回退默认
  3) clamp 数值夹护栏 / 枚举校验 / 类型转换
  4) save→load 往返一致 + version 累加 + 原子写
  5) 未知键保留（前向兼容）
  6) brief.score_and_plan 接入配置后默认口径 = 原值（0.985/1.005/0.90/1.08/30/40%上限）
  7) brief 改配置后止损/止盈/入场带随配置变化
  8) [Sprint0] config.yaml 分组加载 / 拍平 / yaml 覆盖 json / env 最高优先
  9) [Sprint0] 热加载（mtime 变化后无需重启读到新值）
 10) [Sprint0] init 模板生成 + save_yaml 往返 + to_grouped 分组视图
 11) [Sprint0] 新收编键（熔断/守护进程/端口/通知超时）默认 = 原硬编码
"""
import os
import tempfile

import jarvis_config as jcfg
import jarvis_brief as jb

# 隔离全局 YAML/env：避免用户机器上已存在的 ~/.vibe-trading/config.yaml
# 或 JARVIS_CFG_* 环境变量污染测试结果（测试自身会另建临时 yaml 验证）。
_ORIG_YAML_PATH = jcfg.YAML_CONFIG_PATH
jcfg.YAML_CONFIG_PATH = os.path.join(tempfile.gettempdir(), "_jarvis_smoketest_no_such.yaml")
for _k in list(os.environ):
    if _k.startswith(jcfg.ENV_PREFIX):
        del os.environ[_k]

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
# 隔离全局 ~/.vibe-trading/jarvis_config.json：用户机器上若通过 UI 改过参数
# （如 time_stop_days），无隔离会污染「默认=原值」断言。
deriv = {"funding": {"funding_7d_avg_8h_pct": 0.0, "funding_regime": "neutral"}, "long_short": {}}
fac = {"price": 60000.0, "drawdown_from_ath_pct": -35.0, "dd30_signal_active": True,
       "above_ma200": False, "breakout_20d_active": True}
_orig_json_path = jcfg.CONFIG_PATH
jcfg.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "_jarvis_smoketest_no_such.json")
try:
    plan = jb.score_and_plan(deriv, fac, fng_now=18)
finally:
    jcfg.CONFIG_PATH = _orig_json_path
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

# ── 8. [Sprint0] YAML 分组加载 / 覆盖优先级 ──
with tempfile.TemporaryDirectory() as td:
    jp = os.path.join(td, "cfg.json")
    yp = os.path.join(td, "config.yaml")
    # json 层写 0.9；yaml 层写 0.95 → yaml 覆盖 json
    jcfg.save({"min_conviction": 0.9}, path=jp)
    with open(yp, "w", encoding="utf-8") as f:
        f.write("trading:\n  min_conviction: 0.95\nrisk:\n  stop_loss_drop_pct: 12\n")
    cfg = jcfg.load(jp, yaml_path=yp)
    check("yaml 覆盖 json（min_conviction=0.95）", cfg["min_conviction"] == 0.95, str(cfg["min_conviction"]))
    check("yaml 分组键拍平（stop_loss_drop_pct=12）", cfg["stop_loss_drop_pct"] == 12.0)
    check("yaml 未提及键回退 json/默认", cfg["take_profit_pct"] == 8.0)
    check("meta.source 含 json+yaml", "json" in cfg["meta"]["source"] and "yaml" in cfg["meta"]["source"],
          cfg["meta"]["source"])
    # 扁平形态容错（省略分组也认）
    with open(yp, "w", encoding="utf-8") as f:
        f.write("min_conviction: 0.85\n")
    cfg2 = jcfg.load(jp, yaml_path=yp)
    check("yaml 扁平形态容错", cfg2["min_conviction"] == 0.85)
    # 损坏 yaml 不拖垮（保留 json 层）
    with open(yp, "w", encoding="utf-8") as f:
        f.write("trading: [unclosed\n  bad yaml{{{")
    cfg3 = jcfg.load(jp, yaml_path=yp)
    check("损坏 yaml 回退 json 层", cfg3["min_conviction"] == 0.9, cfg3["meta"]["source"])

# ── 9. [Sprint0] env 最高优先 + 热加载 ──
with tempfile.TemporaryDirectory() as td:
    jp = os.path.join(td, "cfg.json")
    yp = os.path.join(td, "config.yaml")
    with open(yp, "w", encoding="utf-8") as f:
        f.write("trading:\n  min_conviction: 0.7\n")
    os.environ["JARVIS_CFG_MIN_CONVICTION"] = "0.99"
    try:
        cfg = jcfg.load(jp, yaml_path=yp)
        check("env 覆盖 yaml（0.99）", cfg["min_conviction"] == 0.99, str(cfg["min_conviction"]))
        check("meta.source 标注 env", "env:" in cfg["meta"]["source"], cfg["meta"]["source"])
    finally:
        del os.environ["JARVIS_CFG_MIN_CONVICTION"]
    # 热加载：改文件 mtime 后无需重启读到新值
    cfg_a = jcfg.load(jp, yaml_path=yp)
    check("热加载前 yaml=0.7", cfg_a["min_conviction"] == 0.7)
    import time as _t
    _t.sleep(0.02)
    with open(yp, "w", encoding="utf-8") as f:
        f.write("trading:\n  min_conviction: 0.6\n")
    os.utime(yp, (_t.time() + 2, _t.time() + 2))  # 确保 mtime 前进（低精度文件系统兜底）
    cfg_b = jcfg.load(jp, yaml_path=yp)
    check("热加载后读到新值 0.6", cfg_b["min_conviction"] == 0.6, str(cfg_b["min_conviction"]))

# ── 10. [Sprint0] init 模板 + save_yaml 往返 + 分组视图 ──
with tempfile.TemporaryDirectory() as td:
    yp = os.path.join(td, "config.yaml")
    out = jcfg.init_yaml_template(yp)
    check("init 生成模板", os.path.exists(out))
    cfg = jcfg.load(os.path.join(td, "no.json"), yaml_path=yp)
    check("模板加载后=内置默认（零回归）",
          cfg["min_conviction"] == 0.8 and cfg["cb_drawdown_halt_pct"] == 20.0)
    saved = jcfg.save_yaml({"cb_drawdown_halt_pct": 15, "dashboard_port": 8899}, yaml_path=yp)
    check("save_yaml 夹护栏+写入", saved["cb_drawdown_halt_pct"] == 15.0 and saved["dashboard_port"] == 8899)
    cfg2 = jcfg.load(os.path.join(td, "no.json"), yaml_path=yp)
    check("save_yaml 往返一致", cfg2["cb_drawdown_halt_pct"] == 15.0 and cfg2["dashboard_port"] == 8899)
    check("save_yaml version 累加", int(cfg2["meta"]["version"]) >= 1)
    g = jcfg.to_grouped(cfg2)
    check("to_grouped 六组齐全",
          all(k in g for k in ("trading", "risk", "signal", "data", "notify", "system")))
    check("to_grouped 键归组正确",
          g["risk"].get("cb_drawdown_halt_pct") == 15.0 and g["system"].get("dashboard_port") == 8899)

# ── 11. [Sprint0] 新收编键默认 = 原硬编码（零回归）──
check("默认 cb_drawdown_halt_pct=20", d["cb_drawdown_halt_pct"] == 20.0)
check("默认 cb_position_loss_halt_pct=25", d["cb_position_loss_halt_pct"] == 25.0)
check("默认 cb_flash_crash_24h_pct=15", d["cb_flash_crash_24h_pct"] == 15.0)
check("默认 daemon_interval_hours=24", d["daemon_interval_hours"] == 24.0)
check("默认 dashboard 127.0.0.1:7899",
      d["dashboard_host"] == "127.0.0.1" and d["dashboard_port"] == 7899)
check("默认 notify_timeout_s=15", d["notify_timeout_s"] == 15)
check("每个键都有分组", all(k in jcfg.GROUPS for k in d if k != "meta"),
      str([k for k in d if k != "meta" and k not in jcfg.GROUPS]))

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
