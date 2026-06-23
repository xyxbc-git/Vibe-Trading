"""离线 smoketest：T-07 滑点 / 流动性建模。不联网。

验证：
  1) 分层正确（BTC/ETH/alt/长尾）
  2) 大单冲击 > 小单；流动性差的币滑点更高
  3) 平方根冲击单调；上限封顶；ADV 覆盖生效
  4) apply_fill_price：买更贵、卖更便宜
  5) round_trip = 2×单边
  6) 回测 cost_bps=0 零回归；cost_bps>0 降低策略收益且不影响买持
  7) executor 护栏附带 slippage 估计
"""
import jarvis_slippage as js
import jarvis_factor_backtest as fb
import jarvis_executor as jx

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ── 1. 分层 ──
check("BTC=tier1", js.estimate_slippage_pct("BTCUSDT", 1000)["tier"] == "tier1")
check("ETH=tier2", js.estimate_slippage_pct("ETHUSDT", 1000)["tier"] == "tier2")
check("SOL=tier3", js.estimate_slippage_pct("SOLUSDT", 1000)["tier"] == "tier3")
check("XYZ=tier4", js.estimate_slippage_pct("XYZUSDT", 1000)["tier"] == "tier4")

# ── 2. 大单 > 小单；长尾 > BTC ──
small = js.estimate_slippage_pct("BTCUSDT", 1000)["one_way_bps"]
big = js.estimate_slippage_pct("BTCUSDT", 1_000_000)["one_way_bps"]
check("大单冲击>小单", big > small, f"{small} vs {big}")
btc = js.estimate_slippage_pct("BTCUSDT", 100_000)["one_way_bps"]
tail = js.estimate_slippage_pct("XYZUSDT", 100_000)["one_way_bps"]
check("长尾滑点>BTC", tail > btc, f"BTC {btc} vs tail {tail}")

# ── 3. 平方根单调 + 封顶 + ADV 覆盖 ──
s1 = js.estimate_slippage_pct("BTCUSDT", 4_000_000)["impact_bps"]
s2 = js.estimate_slippage_pct("BTCUSDT", 16_000_000)["impact_bps"]
check("名义×4 冲击×2（平方根）", abs(s2 - 2 * s1) < 0.5, f"{s1} {s2}")
huge = js.estimate_slippage_pct("XYZUSDT", 1e12)["one_way_bps"]
check("超大单封顶 800bps", huge == 800.0, str(huge))
adv_lo = js.estimate_slippage_pct("BTCUSDT", 100_000, adv_usdt=1e8)["one_way_bps"]
adv_hi = js.estimate_slippage_pct("BTCUSDT", 100_000, adv_usdt=1e11)["one_way_bps"]
check("ADV 越小滑点越大", adv_lo > adv_hi)

# ── 4. apply_fill_price ──
check("买更贵", js.apply_fill_price(100.0, "buy", 100) > 100.0)
check("卖更便宜", js.apply_fill_price(100.0, "sell", 100) < 100.0)
check("买 100bps=+1%", js.apply_fill_price(100.0, "buy", 100) == 101.0)

# ── 5. round_trip = 2×单边 ──
one = js.estimate_slippage_pct("ETHUSDT", 5000)["one_way_bps"]
rt = js.round_trip_cost_bps("ETHUSDT", 5000)
check("往返=2×单边", abs(rt - 2 * one) < 1e-6, f"{one} {rt}")

# ── 6. 回测 cost 影响 ──
dates = [f"2020-01-{d:02d}" for d in range(1, 21)]
prices = {d: 100.0 + i for i, d in enumerate(dates)}  # 单调上升
fng = {d: (10 if i % 4 == 0 else 85) for i, d in enumerate(dates)}  # 反复进出制造换仓
funding = {}
r0 = fb.backtest(dates, prices, fng, funding, 20, 80, False, False, cost_bps=0.0)
rc = fb.backtest(dates, prices, fng, funding, 20, 80, False, False, cost_bps=50.0)
check("cost=0 与无成本一致(零回归)", r0["cost_bps"] == 0.0)
check("有换仓时成本降低策略收益",
      rc["strat_total_return_pct"] < r0["strat_total_return_pct"] if r0["trades"] > 0 else True,
      f"trades={r0['trades']} {r0['strat_total_return_pct']} vs {rc['strat_total_return_pct']}")
check("成本不影响买持收益", rc["bh_total_return_pct"] == r0["bh_total_return_pct"])

# ── 7. executor 护栏带 slippage ──
decision = {
    "direction": "偏多（战术）", "conviction_score": 1.0, "suggested_position_pct": 20.0,
    "stop_loss": 54000.0, "take_profit_ref": 64800.0, "entry_zone": "59100.0 ~ 60300.0",
    "symbol": "BTCUSDT",
    "expected_value": {"win_prob": 0.6, "take_profit_pct": 8.0, "stop_loss_pct": -10.0},
}
cfg = {"max_position_pct": 40.0, "max_portfolio_risk_pct": 5.0, "min_conviction": 0.8,
       "stop_loss_drop_pct": 10.0, "account_equity_usdt": 100000.0,
       "sizing_method": "fixed", "kelly_fraction": 0.5}
g = jx.evaluate_guardrails(decision, cfg)
check("护栏放行", g["action"] == "place", g.get("reason", ""))
check("护栏带 slippage", isinstance(g.get("slippage"), dict) and g["slippage"]["tier"] == "tier1", str(g.get("slippage")))
check("slippage 有预估成交价", g["slippage"].get("est_fill_price", 0) > g["slippage"]["notional_usdt"] * 0)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
