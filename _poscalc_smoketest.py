"""离线 smoketest：合约仓位与风控计算器（Task #3）。不联网。

验证（以用户实况 130 USDT 本金 / 100x 杠杆为主实例）：
  1) 风险法仓位：名义 = 风险额 / 止损距离，保证金 = 名义 / 杠杆，币数/张数换算
  2) 爆仓价：Binance USDT 本位逐仓官方公式
     LP = (隔仓保证金 + 速算额 − 方向×数量×入场价) / (数量×(MMR − 方向))，
     BTCUSDT 分层表内置；含 3 组独立手算对拍 + 分层表边界连续性检查
  3) 安全边距三级判定：ok / warning / danger（止损比爆仓远 = danger 必警）
  4) 分档止盈 1:1.5 / 1:2 / 1:3 价位与盈利额
  5) 本金×杠杆 封顶缩仓；最大安全杠杆（新口径逐仓解析反推）
  6) advice_from_plan：共识计划（entry_zone）与单信号计划（entry）两种结构
  7) 非法输入永不抛出
"""
import jarvis_position_calc as jpc

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ═══ 主实例：130U 本金 / 100x / 风险2% / BTC 多单 60000 入场、59400 止损（-1%）═══
a = jpc.compute_advice(capital_usdt=130, leverage=100, risk_pct=2,
                       side="long", entry=60000, stop_loss=59400, symbol="BTCUSDT")
check("ok", a["ok"] is True, str(a.get("error")))
check("风险额 = 130×2% = 2.6U", a["risk_usdt"] == 2.6, str(a["risk_usdt"]))
check("止损距离 1%", abs(a["sl"]["dist_pct"] - 1.0) < 1e-9, str(a["sl"]["dist_pct"]))
check("名义仓位 = 2.6/1% = 260U", abs(a["position"]["notional_usdt"] - 260.0) < 0.01,
      str(a["position"]["notional_usdt"]))
check("保证金 = 260/100 = 2.6U", abs(a["position"]["margin_usdt"] - 2.6) < 0.01,
      str(a["position"]["margin_usdt"]))
check("币数 ≈ 0.00433 BTC", abs(a["position"]["qty_coin"] - 260 / 60000) < 1e-6,
      str(a["position"]["qty_coin"]))
check("张数 ≈ 0.43 张（1张=0.01BTC）", a["position"]["contracts"] == 0.43,
      str(a["position"]["contracts"]))
check("不足1张有提示", any("不足 1 张" in w for w in a["warnings"]))
# 爆仓（Binance 逐仓 tier1：MMR 0.4%、速算额 0）：
#   LP = E×(1−1/L)/(1−MMR) = 60000×0.99/0.996 = 59638.55，距离 (1%−0.4%)/0.996 = 0.602%
check("爆仓距离 0.602%", abs(a["liquidation"]["dist_pct"] - 0.602) < 1e-9,
      str(a["liquidation"]["dist_pct"]))
check("爆仓价 59638.55", abs(a["liquidation"]["price"] - 59638.55) < 0.01,
      str(a["liquidation"]["price"]))
check("生效档位 BTC 官方表 0.4%", a["mm"]["mmr_pct"] == 0.4
      and a["mm"]["cum_usdt"] == 0 and a["mm"]["tier_source"] == "binance",
      str(a["mm"]))
# 关键场景：止损 1% 比爆仓 0.602% 更远 → danger + 醒目警告
check("先爆仓后止损 → danger", a["sl"]["safety"] == "danger", a["sl"]["safety"])
check("danger 有🚨警告", any("🚨" in w and "形同虚设" in w for w in a["warnings"]),
      "|".join(a["warnings"]))
check("100x 高杠杆警告", any("强烈建议降杠杆" in w for w in a["warnings"]))
# 最大安全杠杆（长仓解析式）= 1/(1.25%×(1−0.004)+0.004) = 60.79 → 60
check("最大安全杠杆 60x", a["max_safe_leverage"] == 60, str(a["max_safe_leverage"]))
# 分档止盈：风险距离 600 → TP 60900/61200/61800，盈利 = 2.6×RR
tps = a["take_profits"]
check("TP1 1:1.5 = 60900 / +3.9U", tps[0]["price"] == 60900.0 and tps[0]["profit_usdt"] == 3.9,
      str(tps[0]))
check("TP2 1:2 = 61200 / +5.2U", tps[1]["price"] == 61200.0 and tps[1]["profit_usdt"] == 5.2,
      str(tps[1]))
check("TP3 1:3 = 61800 / +7.8U", tps[2]["price"] == 61800.0 and tps[2]["profit_usdt"] == 7.8,
      str(tps[2]))

# ═══ 空单对称性：60000 入场、60600 止损（+1%）═══
s = jpc.compute_advice(capital_usdt=130, leverage=100, risk_pct=2,
                       side="short", entry=60000, stop_loss=60600, symbol="BTCUSDT")
check("空单 ok", s["ok"] is True, str(s.get("error")))
# 空单 LP = E×(1+1/L)/(1+MMR) = 60000×1.01/1.004 = 60358.57
check("空单爆仓在上方 60358.57", abs(s["liquidation"]["price"] - 60358.57) < 0.01,
      str(s["liquidation"]["price"]))
check("空单 TP 向下 1:2 = 58800", s["take_profits"][1]["price"] == 58800.0,
      str(s["take_profits"][1]))

# ═══ 安全案例：10x 杠杆 + 1% 止损 → 爆仓距离 (10%−0.4%)/0.996 = 9.639%，边距充足 ═══
b = jpc.compute_advice(capital_usdt=130, leverage=10, risk_pct=2,
                       side="long", entry=60000, stop_loss=59400, symbol="BTCUSDT")
check("10x 爆仓距离 9.639%", abs(b["liquidation"]["dist_pct"] - 9.639) < 1e-9,
      str(b["liquidation"]["dist_pct"]))
check("10x + 1%止损 → safety ok", b["sl"]["safety"] == "ok", b["sl"]["safety"])
check("10x 无🚨警告", not any("🚨" in w for w in b["warnings"]), "|".join(b["warnings"]))

# ═══ warning 档：爆仓 0.602%、止损 0.55%（0.482≤0.55<0.602，边距 < 1.25 倍）═══
w = jpc.compute_advice(capital_usdt=130, leverage=100, risk_pct=2,
                       side="long", entry=60000, stop_loss=60000 * (1 - 0.0055))
check("边距不足 → warning", w["sl"]["safety"] == "warning", w["sl"]["safety"])

# ═══ 本金×杠杆封顶：5x、止损 0.2%、风险3% → 需 1950U > 650U 上限 ═══
c = jpc.compute_advice(capital_usdt=130, leverage=5, risk_pct=3,
                       side="long", entry=60000, stop_loss=59880, symbol="BTCUSDT")
check("封顶到 650U", abs(c["position"]["notional_usdt"] - 650.0) < 0.01,
      str(c["position"]["notional_usdt"]))
check("封顶后风险缩为 1.3U", abs(c["risk_usdt"] - 1.3) < 0.01, str(c["risk_usdt"]))
check("封顶有提示", c["position"]["capped"] and any("上限" in x for x in c["warnings"]))

# ═══ advice_from_plan：共识计划（entry_zone 取中价）═══
plan = {"side": "long", "entry_zone": [59800, 60200], "stop_loss": 59400,
        "take_profit_1": 61200, "basis": ["turtle", "dow"], "source_tf": "4h"}
p = jpc.advice_from_plan(plan, capital_usdt=130, leverage=100, risk_pct=2, symbol="BTCUSDT")
check("plan ok", p["ok"] is True, str(p.get("error")))
check("入场取区间中价 60000", p["entry"] == 60000.0, str(p["entry"]))
check("带回信号止盈参考", p["plan_tp_ref"] == 61200.0, str(p.get("plan_tp_ref")))
check("带回 source_tf/basis", p.get("source_tf") == "4h" and p.get("basis") == ["turtle", "dow"])

# 单信号计划结构（entry 而非 entry_zone）
p2 = jpc.advice_from_plan({"side": "short", "entry": 60000, "stop_loss": 60600,
                           "take_profit": 58800},
                          capital_usdt=130, leverage=20, risk_pct=1)
check("单信号计划 ok", p2["ok"] is True and p2["side"] == "short", str(p2.get("error")))

# ═══ 非法输入永不抛出 ═══
check("plan=None 不抛", jpc.advice_from_plan(None, capital_usdt=130, leverage=100,
                                             risk_pct=2)["ok"] is False)
check("止损方向错 → error", jpc.compute_advice(capital_usdt=130, leverage=100, risk_pct=2,
                                               side="long", entry=60000,
                                               stop_loss=60600)["ok"] is False)
check("杠杆超限 → error", jpc.compute_advice(capital_usdt=130, leverage=200, risk_pct=2,
                                             side="long", entry=60000,
                                             stop_loss=59400)["ok"] is False)
check("本金非法 → error", jpc.compute_advice(capital_usdt=0, leverage=100, risk_pct=2,
                                             side="long", entry=60000,
                                             stop_loss=59400)["ok"] is False)
check("风险>3% 有激进提示", any("激进" in x for x in
                               jpc.compute_advice(capital_usdt=130, leverage=10, risk_pct=5,
                                                  side="long", entry=60000,
                                                  stop_loss=59400)["warnings"]))

# ═══ 保证金法口径（margin_pct）：130U × 30% × 100x = 3900U 名义 ═══
m = jpc.compute_advice(capital_usdt=130, leverage=100, risk_pct=1,
                       side="long", entry=60000, stop_loss=59400,
                       symbol="BTCUSDT", margin_pct=30)
check("margin ok", m["ok"] is True, str(m.get("error")))
check("sizing_mode=margin", m["sizing_mode"] == "margin", str(m.get("sizing_mode")))
check("保证金 = 130×30% = 39U", abs(m["position"]["margin_usdt"] - 39.0) < 0.01,
      str(m["position"]["margin_usdt"]))
check("名义 = 39×100 = 3900U", abs(m["position"]["notional_usdt"] - 3900.0) < 0.01,
      str(m["position"]["notional_usdt"]))
check("派生风险额 = 3900×1% = 39U", abs(m["risk_usdt"] - 39.0) < 0.01, str(m["risk_usdt"]))
check("派生风险% = 30%", abs(m["risk_pct"] - 30.0) < 0.01, str(m["risk_pct"]))
check("margin 高风险有激进提示", any("激进" in x for x in m["warnings"]))
check("本金占用 30%", abs(m["position"]["capital_used_pct"] - 30.0) < 0.01,
      str(m["position"]["capital_used_pct"]))
check("保证金%非法 → error", jpc.compute_advice(
    capital_usdt=130, leverage=100, risk_pct=1, side="long", entry=60000,
    stop_loss=59400, margin_pct=150)["ok"] is False)
check("风险法输出 sizing_mode=risk", a["sizing_mode"] == "risk", str(a.get("sizing_mode")))

# ═══ entry_override：用户手动入场价，止损随距离%平移 ═══
po = jpc.advice_from_plan(plan, capital_usdt=130, leverage=100, risk_pct=2,
                          symbol="BTCUSDT", entry_override=61000)
check("override ok", po["ok"] is True, str(po.get("error")))
check("入场价取用户值 61000", po["entry"] == 61000.0, str(po["entry"]))
check("止损距离%保持 1%", abs(po["sl"]["dist_pct"] - 1.0) < 1e-6, str(po["sl"]["dist_pct"]))
check("止损平移到 60390", abs(po["sl"]["price"] - 60390.0) < 0.01, str(po["sl"]["price"]))
check("标记 entry_overridden", po.get("entry_overridden") is True)
check("override 后区间坍缩为点", po["entry_zone"] == [61000.0, 61000.0],
      str(po["entry_zone"]))

# plan 的 margin_pct 透传
pm = jpc.advice_from_plan(plan, capital_usdt=130, leverage=100, risk_pct=2,
                          symbol="BTCUSDT", margin_pct=50)
check("plan margin 透传", pm["ok"] and pm["sizing_mode"] == "margin"
      and abs(pm["position"]["margin_usdt"] - 65.0) < 0.01,
      str(pm.get("position")))

# ═══ Binance 爆仓价对拍：3 组独立手算（公式手工代入，不复用模块函数）═══
# 官方公式：LP = (WB + cum − side×qty×EP) / (qty×(MMR − side))，side 多=+1 空=−1
#
# 对拍组 1（用户截图实况）：本金 130、保证金 10%（WB=13）、29x、多单 @64368
#   notional = 13×29 = 377（tier1：MMR 0.4%、cum 0）、qty = 377/64368
#   手算 LP = (13 + 0 − 377/64368×64368) / (377/64368 × (0.004−1))
_qty1 = 377.0 / 64368.0
_lp1 = (13.0 + 0.0 - 1 * _qty1 * 64368.0) / (_qty1 * (0.004 - 1))  # = 62398.006
g1 = jpc.compute_advice(capital_usdt=130, leverage=29, risk_pct=1, margin_pct=10,
                        side="long", entry=64368, stop_loss=63000, symbol="BTCUSDT")
check("对拍1 手算 62398.01", abs(_lp1 - 62398.006) < 0.01, f"{_lp1:.3f}")
check("对拍1 模块=手算(±0.01)", abs(g1["liquidation"]["price"] - round(_lp1, 2)) < 0.01,
      f"module={g1['liquidation']['price']} hand={_lp1:.4f}")
check("对拍1 旧口径已替换(≠62470.25)", abs(g1["liquidation"]["price"] - 62470.25) > 50,
      str(g1["liquidation"]["price"]))
check("对拍1 保证金 13U/名义 377U", abs(g1["position"]["margin_usdt"] - 13.0) < 0.01
      and abs(g1["position"]["notional_usdt"] - 377.0) < 0.01,
      str(g1["position"]))
#
# 对拍组 2（空单对称）：0.5 BTC 空 @60000、50x（notional 30000 tier1、WB=600）
#   手算 LP = (600 + 0 + 0.5×60000) / (0.5×(0.004+1)) = 30600/0.502 = 60956.175
_lp2 = (600.0 + 0.0 - (-1) * 0.5 * 60000.0) / (0.5 * (0.004 - (-1)))
lq2 = jpc.liquidation_price(entry=60000, qty=0.5, margin=600, side="short",
                            symbol="BTCUSDT")
check("对拍2 手算 60956.18", abs(_lp2 - 60956.175) < 0.01, f"{_lp2:.3f}")
check("对拍2 模块=手算(±0.01)", abs(lq2[0] - _lp2) < 0.01,
      f"module={lq2[0]:.4f} hand={_lp2:.4f}")
#
# 对拍组 3（tier2 跨档 + 速算额）：10 BTC 多 @60000（notional 600k → MMR 0.5%、cum 300）
#   20x → WB = 30000；手算 LP = (30000 + 300 − 600000)/(10×(0.005−1)) = 57256.281
_lp3 = (30000.0 + 300.0 - 1 * 10.0 * 60000.0) / (10.0 * (0.005 - 1))
lq3 = jpc.liquidation_price(entry=60000, qty=10, margin=30000, side="long",
                            symbol="BTCUSDT")
check("对拍3 手算 57256.28", abs(_lp3 - 57256.281) < 0.01, f"{_lp3:.3f}")
check("对拍3 模块=手算(±0.01)", abs(lq3[0] - _lp3) < 0.01,
      f"module={lq3[0]:.4f} hand={_lp3:.4f}")
check("对拍3 档位取 tier2 0.5%/300", lq3[2] == 0.005 and lq3[3] == 300.0,
      f"mmr={lq3[2]} cum={lq3[3]}")

# ═══ 分层表边界连续性：MM = 名义×MMR − cum 在每个档位边界处必须连续 ═══
_tiers = jpc.BINANCE_MM_TIERS["BTC"]
_cont = all(
    abs((_tiers[i][0] * _tiers[i][1] - _tiers[i][2])
        - (_tiers[i][0] * _tiers[i + 1][1] - _tiers[i + 1][2])) < 1e-6
    for i in range(len(_tiers) - 1) if _tiers[i][0] != float("inf")
)
check("BTC 分层表边界连续", _cont)

# ═══ 无表币种走通用保守档 + 低杠杆多单价格归零不爆仓 ═══
lq_eth = jpc.liquidation_price(entry=3000, qty=1, margin=300, side="long",
                               symbol="ETHUSDT")
check("ETH 走通用档 1%", lq_eth[2] == 0.01 and lq_eth[4] == "generic",
      f"mmr={lq_eth[2]} src={lq_eth[4]}")
lq_1x = jpc.liquidation_price(entry=60000, qty=1, margin=60000, side="long",
                              symbol="BTCUSDT")
check("1x 多单爆仓价钳为 0（价格归零不爆）", lq_1x[0] == 0.0 and lq_1x[1] == 100.0,
      f"lp={lq_1x[0]} dist={lq_1x[1]}")

# ═══ 配置键落位（jarvis_config）═══
# [Sprint1 T1.1] 杠杆安全化：默认杠杆 100→10、保证金 100%→10%（告别全押）
import jarvis_config as jc
check("配置默认 130/10x/风险1%/保证金10%", jc.DEFAULTS["poscalc_capital_usdt"] == 130.0
      and jc.DEFAULTS["poscalc_leverage"] == 10.0
      and jc.DEFAULTS["poscalc_risk_pct"] == 1.0
      and jc.DEFAULTS["poscalc_margin_pct"] == 10.0)
check("杠杆护栏夹紧 1~125", jc.clamp("poscalc_leverage", 300) == 125.0
      and jc.clamp("poscalc_leverage", 0) == 1.0)
check("风险护栏夹紧 0.1~10", jc.clamp("poscalc_risk_pct", 99) == 10.0)
check("保证金护栏夹紧 1~100", jc.clamp("poscalc_margin_pct", 500) == 100.0
      and jc.clamp("poscalc_margin_pct", 0) == 1.0)
check("[T1.1] 新键默认 10x/确认阈 20x", jc.DEFAULTS["default_leverage"] == 10.0
      and jc.DEFAULTS["max_leverage_no_confirm"] == 20.0)

# ═══ [Sprint1 T1.2] 止损隐蔽化 stealth_stop_loss ═══
# 多单 SL 恰在整数关口 60000 → 应下移（远离扫单区），带说明
_sl, _note = jpc.stealth_stop_loss(60000.0, "bullish", 600.0,
                                   enabled=True, buffer_mult=0.3)
check("[T1.2] 多单避开 60000 关口向下", _sl < 60000.0 and _note is not None,
      f"sl={_sl} note={_note}")
check("[T1.2] 偏移量=0.3xATR", abs(_sl - (60000.0 - 0.3 * 600.0)) < 0.01, str(_sl))
# 空单 SL 在关口 → 向上避让
_sl2, _n2 = jpc.stealth_stop_loss(3000.0, "bearish", 30.0,
                                  enabled=True, buffer_mult=0.3)
check("[T1.2] 空单避开 3000 关口向上", _sl2 > 3000.0 and _n2 is not None,
      f"sl={_sl2} note={_n2}")
# 远离关口的 SL 不动
_sl3, _n3 = jpc.stealth_stop_loss(59712.0, "bullish", 600.0,
                                  enabled=True, buffer_mult=0.3)
check("[T1.2] 非关口 SL 不动", _sl3 == 59712.0 and _n3 is None, f"sl={_sl3}")
# 开关关闭 → 关口也不动
_sl4, _n4 = jpc.stealth_stop_loss(60000.0, "bullish", 600.0,
                                  enabled=False, buffer_mult=0.3)
check("[T1.2] 开关关闭不调整", _sl4 == 60000.0 and _n4 is None)
# 摆动锚点过近 → 挪到锚点外侧
_sl5, _n5 = jpc.stealth_stop_loss(59310.0, "bullish", 600.0, anchor=59300.0,
                                  enabled=True, buffer_mult=0.3)
check("[T1.2] 贴锚点多单挪到锚点下方缓冲", _sl5 <= 59300.0 - 0.3 * 600.0 + 0.01,
      f"sl={_sl5} note={_n5}")
# 非法输入优雅回退
check("[T1.2] 非法方向原样返回", jpc.stealth_stop_loss(100.0, "neutral", 1.0,
      enabled=True, buffer_mult=0.3) == (100.0, None))
# [P1-1] 安全兜底三场景：极端 ATR 不得把 SL 推成负数/越过入场价
# 场景 A：多单 SL=100 恰在关口，ATR=500 → 直接调整会得 100-150=-50 → 必须回退原 SL
_slx, _nx = jpc.stealth_stop_loss(100.0, "bullish", 500.0,
                                  enabled=True, buffer_mult=0.3)
check("[P1-1] 极端 ATR 多单不产生负 SL（回退原值）", _slx == 100.0 and _nx is None,
      f"sl={_slx} note={_nx}")
# 场景 B：多单调整后越过入场价 → 回退。SL=100(关口) 入场 101，ATR=20 →
# 调整候选 100-6=94 合法；改用空单验证越界：空单 SL=100，入场 102，ATR=20 →
# 候选 100+6=106>102 合法不越界；构造真正越界：空单 SL=100 入场 105.5，
# ATR=20 → 106>105.5 合法。换多单：SL=100 入场 96 → 候选 94 < 96 合法。
# 越入场价的构造：多单 SL=100 入场 99.5，ATR=2 → near_zone=0.3，
# 100 是关口，候选 100-0.6=99.4 < 99.5 合法；反向：ATR=-? 不行。
# 直接构造：空单 SL=100（关口）入场 100.3，ATR=2 → 候选 100+0.6=100.6 > 100.3 ✓合法
# 多单想越界需 buf > sl-entry 距离为负——多单 SL 本在入场下方且向下挪，
# 越界只可能发生在「原 SL 已在入场价上方」的脏数据：SL=100 入场 99，候选 99.4>99 → 回退
_sly, _ny = jpc.stealth_stop_loss(100.0, "bullish", 2.0, entry=99.0,
                                  enabled=True, buffer_mult=0.3)
check("[P1-1] 多单调整后仍≥入场价（脏数据）→ 回退", _sly == 100.0 and _ny is None,
      f"sl={_sly} note={_ny}")
# 场景 C：空单调整后仍≤入场价（脏数据：原 SL 在入场下方）→ 回退
_slz, _nz = jpc.stealth_stop_loss(100.0, "bearish", 2.0, entry=101.0,
                                  enabled=True, buffer_mult=0.3)
check("[P1-1] 空单调整后仍≤入场价（脏数据）→ 回退", _slz == 100.0 and _nz is None,
      f"sl={_slz} note={_nz}")
# 场景 D：正常入场价传入不影响合法调整（多单 60000 关口，入场 61000）
_slw, _nw = jpc.stealth_stop_loss(60000.0, "bullish", 600.0, entry=61000.0,
                                  enabled=True, buffer_mult=0.3)
check("[P1-1] 合法调整不受 entry 兜底影响", _slw == 59820.0 and _nw is not None,
      f"sl={_slw}")
# 场景 E：无 entry 时幅度兜底——调整幅度>50% 原价视为异常回退
_slv, _nv = jpc.stealth_stop_loss(100.0, "bearish", 500.0,
                                  enabled=True, buffer_mult=0.3)
check("[P1-1] 无 entry 幅度>50% 回退（空单 100+150=250）", _slv == 100.0 and _nv is None,
      f"sl={_slv} note={_nv}")
check("[T1.2] 配置键落位", jc.DEFAULTS["sl_avoid_round_levels"] is True
      and jc.DEFAULTS["sl_atr_buffer_mult"] == 0.3
      and jc.DEFAULTS["cooldown_hours"] == 4.0)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
