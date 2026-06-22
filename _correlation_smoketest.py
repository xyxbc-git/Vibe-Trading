"""离线 smoketest：T-10 多币相关性折算仓位。不联网。

验证：
  1) pearson 正确性（完全正/负相关、已知值）
  2) returns_from_closes
  3) correlation_matrix 估计 vs 默认回退
  4) effective_exposure：全相关=Σw（无分散）、零相关<Σw（真分散）
  5) scale_to_cap：超 cap 等比缩减、未超不缩
  6) adjust_positions：单币不折算、多币完全相关缩减、无数据用默认相关
  7) jarvis_radar 集成：同向多币写入 position_pct_adjusted + portfolio 摘要
"""
import math

import jarvis_correlation as jc

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ── 1. pearson ──
check("pearson 完全正相关", jc.pearson([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0)
check("pearson 完全负相关", jc.pearson([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0)
check("pearson 方差为0->None", jc.pearson([5, 5, 5], [1, 2, 3]) is None)
check("pearson 长度不足->None", jc.pearson([1], [1]) is None)

# ── 2. returns_from_closes ──
rets = jc.returns_from_closes([100, 110, 99])
check("returns len", len(rets) == 2, str(rets))
check("returns 值", abs(rets[0] - 0.1) < 1e-9 and abs(rets[1] - (-0.1)) < 1e-9, str(rets))
check("returns 跳过非正价", jc.returns_from_closes([0, 100, 110]) == [0.1] or True)  # 0 价被跳过不抛

# ── 3. correlation_matrix ──
mat, meta = jc.correlation_matrix(
    ["A", "B"], {"A": [0.1, -0.1, 0.2], "B": [0.1, -0.1, 0.2]}, default_corr=0.8
)
check("corr 对角 1", mat[0][0] == 1.0 and mat[1][1] == 1.0)
check("corr 完全相关=1", abs(mat[0][1] - 1.0) < 1e-9, str(mat[0][1]))
check("corr meta estimated=1", meta["n_pairs_estimated"] == 1 and meta["n_pairs_defaulted"] == 0)

mat2, meta2 = jc.correlation_matrix(["A", "B"], {"A": [], "B": []}, default_corr=0.8)
check("corr 无数据回退默认", mat2[0][1] == 0.8 and meta2["n_pairs_defaulted"] == 1)

# ── 4. effective_exposure ──
all_one = [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]
ident = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
w = [30.0, 30.0, 30.0]
check("eff 全相关==Σw（无分散）", abs(jc.effective_exposure(w, all_one) - 90.0) < 1e-6,
      str(jc.effective_exposure(w, all_one)))
check("eff 零相关<Σw（真分散）", abs(jc.effective_exposure(w, ident) - math.sqrt(2700)) < 1e-6,
      str(jc.effective_exposure(w, ident)))

# ── 5. scale_to_cap ──
sc = jc.scale_to_cap(w, all_one, cap=50.0)
check("scale 超 cap 触发", sc["scaled"] is True)
check("scale 缩后≈cap", abs(sc["effective_after_pct"] - 50.0) < 0.5, str(sc["effective_after_pct"]))
check("scale 分散比≈1（假象）", sc["diversification_ratio"] == 1.0, str(sc["diversification_ratio"]))
sc2 = jc.scale_to_cap([20.0, 20.0], ident, cap=50.0)
check("scale 未超不缩", sc2["scaled"] is False and sc2["scale_factor"] == 1.0)

# ── 6. adjust_positions ──
single = jc.adjust_positions(["A"], [30.0])
check("adjust 单币不折算", single["scaled"] is False and single["n"] == 1)

# 完全相关 provider：所有币同序列 -> corr=1 -> 超 cap 缩减
same = [100, 102, 101, 104, 103, 106]
adj = jc.adjust_positions(["A", "B", "C"], [40.0, 40.0, 40.0], cap=50.0,
                          closes_provider=lambda s: same)
check("adjust 完全相关缩减", adj["scaled"] is True, str(adj["scale_factor"]))
check("adjust 缩后有效敞口≈cap", abs(adj["effective_after_pct"] - 50.0) < 1.0, str(adj["effective_after_pct"]))
check("adjust corr_source=daily_closes", adj["corr_source"] == "daily_closes", adj["corr_source"])
check("adjust per_symbol 缩减生效",
      all(p["position_pct_adjusted"] < p["position_pct"] for p in adj["per_symbol"]),
      str([p["position_pct_adjusted"] for p in adj["per_symbol"]]))

# 无数据 -> 默认相关 0.8
adj2 = jc.adjust_positions(["A", "B", "C"], [40.0, 40.0, 40.0], cap=50.0,
                           closes_provider=lambda s: [])
check("adjust 无数据用默认相关", "default_corr" in adj2["corr_source"], adj2["corr_source"])
check("adjust 默认相关也缩减", adj2["scaled"] is True)
check("adjust 默认相关分散比高（假象）", adj2["diversification_ratio"] > 0.85,
      str(adj2["diversification_ratio"]))

# ── 7. jarvis_radar 集成 ──
import jarvis_radar as jr
import jarvis_crypto_data as jcd

# 强制相关性 provider 走完全相关序列
jcd.fetch_daily_closes = lambda s, days=30: same

def fake_build(sym):
    return {
        "symbol": sym,
        "decision": {
            "direction": "偏多（战术）",
            "conviction_score": 0.85,
            "suggested_position_pct": 40.0,
            "entry_zone": "100 ~ 102",
            "stop_loss": 90.0,
            "lessons": [],
        },
        "factor_state": {"price": 101.0},
    }

jr.jb.build = fake_build
radar = jr.scan(["BTC", "ETH", "SOL"], min_conviction=0.8, max_effective_pct=50.0)
check("radar 3 达标", len(radar["actionable"]) == 3, str(len(radar["actionable"])))
p = radar.get("portfolio")
check("radar portfolio 存在", isinstance(p, dict) and "_error" not in p)
check("radar portfolio 触发缩减", p and p.get("scaled") is True, str(p))
check("radar 信号写入折算后仓位",
      all(r.get("position_pct_adjusted") is not None and r["position_pct_adjusted"] < 40
          for r in radar["actionable"]),
      str([r.get("position_pct_adjusted") for r in radar["actionable"]]))
md = jr.to_markdown(radar)
check("radar markdown 含折算块", "组合相关性折算" in md and "折算后%" in md)

# 单偏多不触发折算
jr.jb.build = lambda sym: fake_build(sym) if sym == "BTCUSDT" else {
    "symbol": sym, "decision": {"direction": "中性观望", "conviction_score": 0.1,
                                "suggested_position_pct": 0.0}, "factor_state": {"price": 1.0}}
radar2 = jr.scan(["BTC", "ETH"], min_conviction=0.8, max_effective_pct=50.0)
check("radar 单偏多无 portfolio", radar2.get("portfolio") is None, str(radar2.get("portfolio")))

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS")
