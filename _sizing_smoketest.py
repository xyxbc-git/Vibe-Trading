"""离线 smoketest：T-11 动态仓位（分数凯利 / 固定比例）。不联网。

验证：
  1) kelly_star 公式正确（正/负 edge）
  2) 分数凯利缩放 + 负 edge 归零 + cap 封顶
  3) suggest_position_pct：kelly 输入不全回退固定
  4) executor.evaluate_guardrails：
     - 默认 fixed 行为不变（零回归，受组合风险红线封顶）
     - kelly 方法重算更保守仓位，且仍受红线封顶
     - kelly 负 edge → skip
"""
import jarvis_sizing as js
import jarvis_executor as jx

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ── 1. kelly_star 公式 ──
# p=0.6, b=8/10=0.8 → f*=(0.6*0.8-0.4)/0.8 = 0.1
f = js.kelly_star(0.6, 8, -10)
check("kelly_star p0.6 b0.8 = 0.1", abs(f - 0.1) < 1e-9, str(f))
# 负 edge: p=0.5,b=0.8 → (0.4-0.5)/0.8 = -0.125
fneg = js.kelly_star(0.5, 8, -10)
check("kelly_star 负 edge<0", fneg < 0, str(fneg))
check("kelly_star 非法输入 None", js.kelly_star(1.5, 8, -10) is None)

# ── 2. 分数凯利缩放 + 归零 + 封顶 ──
# f*=0.1, frac=0.5 → 0.05 → 5%
check("分数凯利 5%", js.kelly_position_pct(0.6, 8, -10, 0.5, 40) == 5.0,
      str(js.kelly_position_pct(0.6, 8, -10, 0.5, 40)))
check("负 edge 归零", js.kelly_position_pct(0.5, 8, -10, 0.5, 40) == 0.0)
# 高胜率全凯利但 cap=10 封顶
check("cap 封顶", js.kelly_position_pct(0.9, 20, -10, 1.0, 10) == 10.0,
      str(js.kelly_position_pct(0.9, 20, -10, 1.0, 10)))

# ── 3. suggest 回退固定 ──
sug = js.suggest_position_pct(None, None, None, method="kelly", fixed_pct=12.0, cap_pct=40)
check("kelly 缺输入回退固定", "fixed" in sug["method"] and sug["position_pct"] == 12.0, str(sug))
sug2 = js.suggest_position_pct(0.6, 8, -10, method="kelly", kelly_fraction=0.5, cap_pct=40)
check("kelly 正常应用", sug2["method"] == "kelly" and sug2["position_pct"] == 5.0, str(sug2))

# ── 4. executor.evaluate_guardrails ──
base_decision = {
    "direction": "偏多（战术）",
    "conviction_score": 1.0,
    "suggested_position_pct": 40.0,
    "stop_loss": 54000.0,
    "take_profit_ref": 64800.0,
    "entry_zone": "59100.0 ~ 60300.0",  # 中点 59700
    "expected_value": {"win_prob": 0.6, "take_profit_pct": 8.0, "stop_loss_pct": -10.0},
}


def cfg(method="fixed", kf=0.5):
    return {
        "max_position_pct": 40.0, "max_portfolio_risk_pct": 1.5, "min_conviction": 0.8,
        "stop_loss_drop_pct": 10.0, "account_equity_usdt": 1000.0,
        "sizing_method": method, "kelly_fraction": kf,
    }


# 固定：受组合风险红线封顶。stop_drop=(59700-54000)/59700≈9.55% → pos=1.5/9.55*100≈15.71
g_fixed = jx.evaluate_guardrails(dict(base_decision), cfg("fixed"))
check("fixed 放行", g_fixed["action"] == "place", g_fixed.get("reason", ""))
check("fixed 未启用凯利", g_fixed["sizing"]["applied"] is False)
check("fixed 受红线封顶≈15.7%", 15.0 <= g_fixed["position_pct"] <= 16.0, str(g_fixed["position_pct"]))
check("fixed 风险≈1.5", abs(g_fixed["projected_risk_pct"] - 1.5) < 0.05, str(g_fixed["projected_risk_pct"]))

# 凯利：f*=0.1×0.5=5% < 红线 → 保持 5%，比 fixed 更保守
g_kelly = jx.evaluate_guardrails(dict(base_decision), cfg("kelly"))
check("kelly 放行", g_kelly["action"] == "place", g_kelly.get("reason", ""))
check("kelly 已启用", g_kelly["sizing"]["applied"] is True, str(g_kelly["sizing"]))
check("kelly 仓位=5%", g_kelly["position_pct"] == 5.0, str(g_kelly["position_pct"]))
check("kelly 比 fixed 更保守", g_kelly["position_pct"] < g_fixed["position_pct"])
check("kelly 风险不超红线", g_kelly["projected_risk_pct"] <= 1.5 + 1e-9, str(g_kelly["projected_risk_pct"]))

# 凯利负 edge → skip
neg = dict(base_decision)
neg["expected_value"] = {"win_prob": 0.5, "take_profit_pct": 8.0, "stop_loss_pct": -10.0}
g_neg = jx.evaluate_guardrails(neg, cfg("kelly"))
check("kelly 负 edge → skip", g_neg["action"] == "skip", g_neg.get("reason", ""))

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
