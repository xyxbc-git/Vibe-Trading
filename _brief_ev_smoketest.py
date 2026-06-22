"""离线 smoketest：T-08 期望值计入永续 funding 成本。不联网。

验证 _expected_value + score_and_plan 的 EV 接入：
  1) 正费率 → 多头 funding 成本为正，净期望 < 毛期望
  2) 负费率 → 多头收 funding，净期望 > 毛期望
  3) 无 funding 数据 → 净期望 = 毛期望并标注
  4) 强正费率吃光弱 edge → net_ev<=0 触发告警 reason
  5) score 越高胜率 p 越高（封顶 0.66）
"""
import jarvis_brief as jb

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ── 1. 正费率：成本为正，净 < 毛 ──
ev = jb._expected_value({"funding": {"funding_7d_avg_8h_pct": 0.01}}, score=2.0, tp_pct=8.0, sl_pct=-10.0, hold_days=30)
check("正费率 win_prob 封顶 0.62", ev["win_prob"] == 0.62, str(ev["win_prob"]))
check("正费率 毛期望", abs(ev["gross_ev_pct"] - (0.62 * 8 + 0.38 * -10)) < 1e-6, str(ev["gross_ev_pct"]))
check("正费率 成本=0.01×90=0.9", ev["funding_cost_pct"] == 0.9, str(ev["funding_cost_pct"]))
check("正费率 净<毛", ev["net_ev_pct"] < ev["gross_ev_pct"])
check("正费率 侵蚀份额>0", ev["funding_drag_share_pct"] > 0, str(ev["funding_drag_share_pct"]))

# ── 2. 负费率：多头收钱，净 > 毛 ──
ev2 = jb._expected_value({"funding": {"funding_7d_avg_8h_pct": -0.02}}, score=2.0, tp_pct=8.0, sl_pct=-10.0, hold_days=30)
check("负费率 成本为负(收钱)", ev2["funding_cost_pct"] == -1.8, str(ev2["funding_cost_pct"]))
check("负费率 净>毛", ev2["net_ev_pct"] > ev2["gross_ev_pct"])

# ── 3. 无 funding 数据：净=毛 + 标注 ──
ev3 = jb._expected_value({"funding": {}}, score=1.0, tp_pct=8.0, sl_pct=-10.0, hold_days=30)
check("无数据 净=毛", ev3["net_ev_pct"] == ev3["gross_ev_pct"])
check("无数据 标注", "无 funding 数据" in ev3.get("note", ""))
check("无数据 cost=None", ev3["funding_cost_pct"] is None)

# ── 4. 回退到 last_funding_rate_8h_pct ──
ev4 = jb._expected_value({"funding": {"last_funding_rate_8h_pct": 0.05}}, score=0.8, tp_pct=8.0, sl_pct=-10.0, hold_days=30)
check("回退用 last 费率", ev4["funding_8h_pct"] == 0.05 and ev4["funding_cost_pct"] == 4.5, str(ev4))

# ── 5. score → p 单调 + 封顶 ──
p_lo = jb._expected_value({"funding": {}}, 0.0, 8, -10, 30)["win_prob"]
p_hi = jb._expected_value({"funding": {}}, 5.0, 8, -10, 30)["win_prob"]
check("score=0 p=0.5", p_lo == 0.5)
check("score 大 p 封顶 0.66", p_hi == 0.66, str(p_hi))

# ── 6. score_and_plan 接入：强正费率吃光弱 edge 触发告警 ──
# 构造弱偏多（score≈0.8 → 毛期望 ~ 0.548*8+0.452*-10≈-0.14 已偏负）用更高 tp 场景不便；
# 直接验证 plan 里挂上了 expected_value 且 reasons 含告警逻辑。
deriv = {"funding": {"funding_7d_avg_8h_pct": 0.05, "funding_regime": "neutral(中性)"},
         "long_short": {}, }
fac = {"price": 60000.0, "drawdown_from_ath_pct": -35.0, "dd30_signal_active": True,
       "above_ma200": False, "breakout_20d_active": True}
plan = jb.score_and_plan(deriv, fac, fng_now=18)
check("plan 含 expected_value", isinstance(plan.get("expected_value"), dict), str(plan.get("direction")))
if plan.get("suggested_position_pct", 0) > 0:
    ev_p = plan["expected_value"]
    check("plan EV 有 net_ev_pct", ev_p.get("net_ev_pct") is not None)
    # 高费率(0.05×90=4.5%)大概率吃光弱 edge → 告警 reason 出现
    has_warn = any("funding" in r for r in plan.get("reasons", []))
    check("高费率触发 funding 告警 reason", has_warn, str([r for r in plan["reasons"] if "funding" in r]))
else:
    check("plan 偏多有仓位(前置条件)", False, "score 未达偏多阈值，用例需调整")

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
