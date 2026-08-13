#!/usr/bin/env python3
"""jarvis_twelve_amplitude（T8 振幅准入门槛）冒烟测试。

纯函数模块用例：不连库、不出网、不落盘。跑法：
    python3 _twelve_amplitude_smoketest.py
"""

from __future__ import annotations

import jarvis_twelve_amplitude as jta

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), name, detail)


def _t(system="turtle", tf="1h", direction="long", entry=100.0, exit_px=101.0,
       sl=None, reason="tp", ts=0.0, qty=1.0, pnl=1.0) -> dict:
    return {"system": system, "tf": tf, "direction": direction,
            "entry_price": entry, "exit_price": exit_px, "stop_loss": sl,
            "exit_reason": reason, "entry_ts": ts, "qty": qty, "pnl": pnl}


# ── 1. T1 纠偏口径（A1）──
mv, corr = jta.corrected_move_pct(_t(direction="long", entry=100, exit_px=97.0,
                                     sl=99.0, reason="sl"))
check("T1a sl 单按计划止损位结算（-1% 而非台账 -3%）",
      corr and abs(mv - (-1.0)) < 1e-9, f"mv={mv}")
mv, corr = jta.corrected_move_pct(_t(direction="short", entry=100, exit_px=103.0,
                                     sl=101.0, reason="sl"))
check("T1b 空单 sl 纠偏方向正确（-1%）", corr and abs(mv - (-1.0)) < 1e-9, f"mv={mv}")
mv, corr = jta.corrected_move_pct(_t(direction="long", entry=100, exit_px=102.0,
                                     sl=99.0, reason="tp"))
check("T1c 非 sl 单不纠偏（按 exit_price +2%）",
      not corr and abs(mv - 2.0) < 1e-9, f"mv={mv}")
mv, corr = jta.corrected_move_pct(_t(entry=100, exit_px=97.0, sl=None, reason="sl"))
check("T1d sl 单缺 stop_loss 退回 exit_price 且标记未纠偏",
      not corr and abs(mv - (-3.0)) < 1e-9, f"mv={mv}")

# ── 2. toll_ratio（=2×费率÷SL距离%）──
r = jta.toll_ratio_of(_t(entry=100, sl=99.9), 0.05)   # SL 距离 0.1% → 0.1/0.1 = 1.0
check("T2a toll_ratio 计算", r is not None and abs(r - 1.0) < 1e-9, f"r={r}")
check("T2b stop_loss 缺失返回 None（归 no_sl 桶）",
      jta.toll_ratio_of(_t(sl=None), 0.05) is None)

# ── 3. 结算伪影金额 ──
a = jta._artifact_cost(_t(direction="long", entry=100, exit_px=98.5, sl=99.0,
                          reason="sl", qty=2.0))
check("T3a 多单劣化伪影 = (sl-exit)×qty", abs(a - 1.0) < 1e-9, f"a={a}")
a = jta._artifact_cost(_t(direction="long", entry=100, exit_px=99.5, sl=99.0,
                          reason="sl", qty=2.0))
check("T3b 结算优于计划位不计伪影", a == 0.0, f"a={a}")

# ── 4. 独立赌注聚类（A3：同小时×同方向归 1 注）──
trades = [_t(ts=100.0, direction="long"), _t(ts=200.0, direction="long"),
          _t(ts=100.0, direction="short"), _t(ts=3700.0, direction="long")]
rep = jta.amplitude_report(trades, 0.05)
check("T4a 4 笔归 3 注（同小时同方向合并）",
      rep["summary"]["independent_bets"] == 3,
      f"bets={rep['summary']['independent_bets']}")

# ── 5. 地板随费率动态走（A2，不写死 0.10）──
check("T5a fee=0.05 → floor=0.10", jta.amplitude_report([], 0.05)["floor_pct"] == 0.1)
check("T5b fee=0.02 → floor=0.04", jta.amplitude_report([], 0.02)["floor_pct"] == 0.04)

# ── 6. 三态判定（A5 判定次序：终局优先）──
# fail：40 注全部毛位移 ≈ -1%（远低于地板 0.1%，调整后 CI 上界 < 地板）
lose = [_t(ts=i * 3600.0, entry=100, exit_px=99.0 + (i % 7) * 0.01,
           reason="tp", pnl=-1.0) for i in range(40)]
rep = jta.amplitude_report(lose, 0.05)
cell = next(c for c in rep["cells"] if c["system"] == "turtle")
check("T6a 全负样本判 fail（不达标）", cell["status"] == "fail", cell["verdict_cn"])
check("T6b fail 进观察名单（不自动停用）",
      any(w["system"] == "turtle" for w in rep["watch_list"]))
# pass：40 注全部毛位移 ≈ +3%（远高于地板）
win = [_t(ts=i * 3600.0, entry=100, exit_px=103.0 + (i % 7) * 0.01,
          reason="tp", pnl=3.0) for i in range(40)]
rep = jta.amplitude_report(win, 0.05)
cell = next(c for c in rep["cells"] if c["system"] == "turtle")
check("T6c 全正样本判 pass（达标）", cell["status"] == "pass", cell["verdict_cn"])
# insufficient：3 注、均值贴地板、方差大 → 不可判定 + 功效标注
few = [_t(ts=i * 3600.0, entry=100, exit_px=100.0 + (i - 1) * 0.5, reason="tp")
       for i in range(3)]
rep = jta.amplitude_report(few, 0.05)
cell = next(c for c in rep["cells"] if c["system"] == "turtle")
check("T6d 小样本判 insufficient 且展示「不可判定」",
      cell["status"] == "insufficient" and "不可判定" in cell["verdict_cn"],
      f"need={cell['need_trades_80pct_power']}")
check("T6e 功效标注存在（80% 功效所需笔数 > 现有）",
      (cell["need_trades_80pct_power"] or 0) > cell["trades"])
# 单注格子：如实曝光不可判定，不进 Bonferroni 除数
one = [_t(ts=0.0)]
rep = jta.amplitude_report(one, 0.05)
cell = rep["cells"][0]
check("T6f 独立赌注<2 → insufficient（无法估计标准误）",
      cell["status"] == "insufficient" and rep["bonferroni_m"] == 0, cell["note"])

# ── 7. toll_ratio 五档聚合 ──
bands_in = [
    _t(entry=100, sl=98.0, exit_px=99.0, reason="tp", pnl=-1.0),   # 距离2% → toll 0.05
    _t(entry=100, sl=99.95, exit_px=99.9, reason="sl", pnl=-0.1),  # 距离0.05% → toll 2.0
    _t(entry=100, sl=None, exit_px=101, reason="tp", pnl=1.0),     # no_sl 桶
]
tr = jta.toll_ratio_bands(bands_in, 0.05)
lo = next(b for b in tr["bands"] if b["toll_band"] == "<0.1")
hi = next(b for b in tr["bands"] if b["toll_band"] == ">1.0")
check("T7a 档位归属正确", lo["trades"] == 1 and hi["trades"] == 1)
check("T7b stop_loss 缺失笔显式曝光", tr["no_sl_trades"] == 1)
check("T7c 总额比口径字段齐全",
      all(k in lo for k in ("toll_r", "net_r", "ex_friction_r", "win_rate_pct")))

# ── 8. 坏行防御 ──
rep = jta.amplitude_report([_t(entry=0.0), _t(entry=None)], 0.05)
check("T8a entry 非法行计入 skipped 不崩", rep["summary"]["skipped_bad_rows"] == 2)

print(f"\nALL {'PASS' if not FAIL else 'FAIL'}  (pass={len(PASS)} fail={len(FAIL)})")
raise SystemExit(1 if FAIL else 0)
