"""离线冒烟：MFE/MAE 止盈止损调优分析器（jarvis_tpsl_advisor）。

全部合成数据，不联网不碰库。覆盖：MFE/MAE 计算（多空镜像）、R 归一、
开仓 bar 排除、自然/固定视界边界、先后重放（P5 悲观口径）、分位数聚合、
不可判定门槛（P4）、Wilson CI、ATR 缓冲聚合、措辞纪律（P7）、
页缓存断点续跑（不可变缓存 + offline 降级）。
"""
import json
import os
import shutil
import tempfile

import jarvis_tpsl_advisor as jta

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def mk_bars(start_min, rows):
    """rows: [(o,h,l,c), ...] → {start_min+i: (o,h,l,c)}"""
    return {start_min + i: tuple(map(float, r)) for i, r in enumerate(rows)}


FLAT = (100.0, 100.1, 99.9, 100.0)

# ── 1. MFE/MAE 多头：已知路径 ──
bars = mk_bars(1001, [FLAT, (100, 100.2, 98.0, 99.0), (99, 103.0, 98.9, 102.0),
                      FLAT, FLAT])
mfe, mae, n, mae_pre = jta._excursion(100.0, "long", bars, 1001, 1005)
check("多头 MFE=3%", abs(mfe - 3.0) < 1e-9, f"{mfe}")
check("多头 MAE=2%", abs(mae - 2.0) < 1e-9, f"{mae}")
check("覆盖 bar 数", n == 5, str(n))
check("多头峰前 MAE=2%（回撤在峰值前）", abs(mae_pre - 2.0) < 1e-9, f"{mae_pre}")

# ── 1b. 峰前 MAE：深回撤发生在峰值之后 → 不计入护损口径 ──
bars_pp = mk_bars(1001, [(100, 100.3, 99.5, 100.0),   # 峰前小回撤 0.5%
                         (100, 103.0, 99.9, 102.5),   # 冲高峰值 3%
                         (102, 102.1, 95.0, 96.0)])   # 峰后深回撤 5%
mfe_pp, mae_pp, _, pre_pp = jta._excursion(100.0, "long", bars_pp, 1001, 1003)
check("全视界 MAE=5%（含峰后）", abs(mae_pp - 5.0) < 1e-9, f"{mae_pp}")
check("峰前 MAE=0.5%（不含峰后暴跌）", abs(pre_pp - 0.5) < 1e-9, f"{pre_pp}")
check("MFE 不受影响=3%", abs(mfe_pp - 3.0) < 1e-9)

# ── 2. 空头镜像：同一路径 ──
mfe_s, mae_s, _, _ = jta._excursion(100.0, "short", bars, 1001, 1005)
check("空头 MFE=2%（向下）", abs(mfe_s - 2.0) < 1e-9, f"{mfe_s}")
check("空头 MAE=3%（向上）", abs(mae_s - 3.0) < 1e-9, f"{mae_s}")

# ── 2b. 空头峰前 MAE 镜像：峰值=最低点，峰后反弹不计入 ──
bars_sp = mk_bars(1001, [(100, 100.5, 99.8, 100.0),   # 峰前不利 0.5%
                         (100, 100.1, 97.0, 97.5),    # 下探峰值（空头有利）
                         (97.5, 104.0, 97.4, 103.0)])  # 峰后反弹 4%
_, mae_sp, _, pre_sp = jta._excursion(100.0, "short", bars_sp, 1001, 1003)
check("空头全视界 MAE=4%", abs(mae_sp - 4.0) < 1e-9, f"{mae_sp}")
check("空头峰前 MAE=0.5%", abs(pre_sp - 0.5) < 1e-9, f"{pre_sp}")

# ── 3. 窗口内无 bar → n=0 ──
mfe0, mae0, n0, pre0 = jta._excursion(100.0, "long", bars, 2000, 2010)
check("无 bar 窗口 n=0", n0 == 0 and mfe0 == 0.0 and mae0 == 0.0 and pre0 == 0.0)

# ── 4. analyze_trade：R 归一 + 开仓 bar 排除 ──
# entry 所在分钟 1000 放一根剧烈 bar（h=110,l=90），必须被排除（P2）
big = {1000: (100.0, 110.0, 90.0, 100.0)}
big.update(mk_bars(1001, [FLAT, (100, 100.2, 98.0, 99.0), (99, 103.0, 98.9, 102.0)]
                   + [FLAT] * 200))
trade = {"system": "osc", "tf": "5m", "direction": "long", "entry_price": 100.0,
         "entry_ts": 1000 * 60 + 5, "exit_ts": 1005 * 60,
         "stop_loss": 99.0, "take_profit": 102.0, "exit_reason": "sl"}
e = jta.analyze_trade(trade, big, data_end_min=1000 + 300)
check("R 归一 MFE_nat=3R（R=1%）", e is not None and abs(e.mfe_nat_r - 3.0) < 1e-9,
      f"{e.mfe_nat_r if e else None}")
check("R 归一 MAE_nat=2R", abs(e.mae_nat_r - 2.0) < 1e-9, f"{e.mae_nat_r}")
check("开仓 bar 被排除（MFE≠10R）", e.mfe_nat_r < 9.0)
check("计划 TP 归一 tp_r=2.0", abs(e.tp_r - 2.0) < 1e-9, f"{e.tp_r}")
check("固定视界=24根×5m=120min", e.bars_fix > e.bars_nat and e.bars_fix <= 120,
      f"nat={e.bars_nat} fix={e.bars_fix}")

# ── 5. 开平同分钟 → 自然窗口不可测，固定视界照算 ──
t5 = dict(trade, exit_ts=1000 * 60 + 30)
e5 = jta.analyze_trade(t5, big, data_end_min=1300)
check("同分钟开平：自然窗口 None", e5.mfe_nat_r is None and e5.bars_nat == 0)
check("同分钟开平：固定视界照算", e5.mfe_fix_r is not None and e5.bars_fix > 0)

# ── 6. 固定视界尾部截断 ──
e6 = jta.analyze_trade(trade, big, data_end_min=1050)
check("固定视界截断到数据末端", e6.bars_fix <= 50, f"fix={e6.bars_fix}")

# ── 7. R≤0 剔除（P3）──
check("entry==sl 剔除", jta.analyze_trade(dict(trade, stop_loss=100.0), big, 1300) is None)

# ── 8. TP 无效侧 / 零距离 → tp_r=None ──
e8a = jta.analyze_trade(dict(trade, take_profit=99.5), big, 1300)   # long 的 TP 在下方
e8b = jta.analyze_trade(dict(trade, take_profit=100.0), big, 1300)  # 距离 0
check("TP 错侧 → tp_r=None", e8a.tp_r is None)
check("TP 零距离 → tp_r=None", e8b.tp_r is None)

# ── 9. 先后重放：先 SL 后 TP（冤枉止损证据）──
rb = mk_bars(1001, [(100, 100.5, 98.8, 99.0),   # 触 SL(99)
                    (99, 102.5, 98.9, 102.0),   # 后达 TP(102)
                    FLAT])
first, after = jta._replay_first_touch("long", 99.0, 102.0, rb, 1001, 1003)
check("先 SL 后 TP：first=sl", first == "sl")
check("先 SL 后 TP：tp_after_sl=True", after is True)

# ── 10. 同 bar 双触 → 先 SL（P5 悲观）──
rb2 = mk_bars(1001, [(100, 102.5, 98.5, 100.0)])
first2, _ = jta._replay_first_touch("long", 99.0, 102.0, rb2, 1001, 1001)
check("同 bar 双触判先 SL", first2 == "sl")

# ── 11. 只触 TP ──
rb3 = mk_bars(1001, [(100, 102.5, 99.5, 102.0), FLAT])
first3, after3 = jta._replay_first_touch("long", 99.0, 102.0, rb3, 1001, 1002)
check("只触 TP：first=tp 且无 after", first3 == "tp" and after3 is False)

# ── 12. 空头重放镜像 ──
rb4 = mk_bars(1001, [(100, 101.2, 99.8, 101.0),   # short SL=101 触
                     (101, 101.4, 97.9, 98.0)])   # 后达 TP=98
first4, after4 = jta._replay_first_touch("short", 101.0, 98.0, rb4, 1001, 1002)
check("空头先 SL 后 TP 镜像", first4 == "sl" and after4 is True)

# ── 13. 分位数 ──
p = jta._pcts(list(range(1, 101)))
check("分位数键齐全", set(p) == {"p25", "p50", "p60", "p75", "p90"})
check("P50 合理", abs(p["p50"] - 50.5) < 0.6, str(p["p50"]))

# ── 14. Wilson CI ──
lo, hi = jta.wilson_ci(5, 10)
check("Wilson CI 含真值", lo < 0.5 < hi, f"[{lo:.3f},{hi:.3f}]")
lo0, _ = jta.wilson_ci(0, 10)
check("Wilson CI 下界≥0", lo0 == 0.0)
check("Wilson n=0 全区间", jta.wilson_ci(0, 0) == (0.0, 1.0))

# ── 15. 聚合：不可判定门槛（P4）──
def mk_exc(i, n_total, reason="sl"):
    ex = jta.TradeExcursion(system="osc", tf="5m", direction="long",
                            exit_reason=reason, r_pct=1.0, tp_r=2.0)
    ex.mfe_nat_r, ex.mae_nat_r = 1.0 + i / n_total, 0.5
    ex.mfe_fix_r, ex.mae_fix_r = 1.5 + i / n_total * 2.0, 0.6 + i / n_total
    ex.mae_fix_prepeak_r = 0.5 * ex.mae_fix_r   # 峰前恒小于全视界
    ex.bars_nat = ex.bars_fix = 10
    ex.cov_fix = 1.0
    if reason == "sl":
        ex.first_touch = "sl"
        ex.tp_after_sl = (i % 3 == 0)
    return ex


c29 = jta.aggregate_cell([mk_exc(i, 29) for i in range(29)])
check("n=29 → 不可判定", c29.verdict == "INSUFFICIENT")
check("不可判定禁止给推荐数字", c29.suggest is None and c29.mfe_fix_r is None)
check("不可判定说明还差几笔", any("不可判定" in s and "还差 1 笔" in s for s in c29.notes),
      str(c29.notes))

c30 = jta.aggregate_cell([mk_exc(i, 30) for i in range(30)], atr_pct=0.2)
check("n=30 → OK", c30.verdict == "OK")
check("OK 格子有分位数", c30.mfe_fix_r is not None and "p60" in c30.mfe_fix_r)
check("OK 格子有峰前 MAE 分位", c30.mae_prepeak_r is not None)
check("OK 格子有推荐", c30.suggest is not None and c30.suggest["tp_r_mid"] > 0)
check("推荐 SL 含 ATR 缓冲", c30.suggest["atr_buffer_r"] > 0, str(c30.suggest["atr_buffer_r"]))
check("推荐区间有序", c30.suggest["tp_r_lo"] <= c30.suggest["tp_r_mid"] <= c30.suggest["tp_r_hi"])
# 峰前口径：SL 基线必须 ≤ 用全视界 MAE 算出的基线（护损只看峰前回撤）
import numpy as _np
_full_p90 = float(_np.percentile([mk_exc(i, 30).mae_fix_r for i in range(30)], 90))
check("SL 基线用峰前口径（≤全视界口径）",
      c30.suggest["sl_r"] - c30.suggest["atr_buffer_r"] <= _full_p90 + 1e-9,
      f"base={c30.suggest['sl_r'] - c30.suggest['atr_buffer_r']:.3f} full={_full_p90:.3f}")
check("冤枉止损率∈[0,1] 带 CI", c30.unjust_sl_rate is not None and c30.unjust_ci is not None
      and 0 <= c30.unjust_sl_rate <= 1)
check("TP 触达率有 Wilson CI", c30.tp_reach_ci is not None
      and c30.tp_reach_ci[0] <= c30.tp_reach_rate <= c30.tp_reach_ci[1])

# ── 16. ATR 聚合：平稳序列的量纲 ──
# 每根 1m bar (100,100.5,99.5,100)：聚成 5m 后 h-l=1 → ATR≈1 → 1% of 100
atr_bars = {m: (100.0, 100.5, 99.5, 100.0) for m in range(0, 5 * 40)}
atr = jta.tf_atr_pct_median(atr_bars, 5)
check("ATR%≈1.0", atr is not None and abs(atr - 1.0) < 0.05, str(atr))
check("ATR 数据不足 → None", jta.tf_atr_pct_median({0: FLAT}, 5) is None)

# ── 17. 措辞纪律（P7）──
try:
    jta._discipline_check("本方案能提高期望值")
    check("禁词必须抛错", False)
except ValueError:
    check("禁词必须抛错", True)
check("正常措辞通过", jta._discipline_check("减少结构性浪费") == "减少结构性浪费")

# ── 18. 报告渲染：不抛错、不含禁词、含关键栏目 ──
meta = {"symbol": "TESTUSDT", "days": 30, "n_trades_analyzed": 30, "n_trades_total": 31,
        "bars": {"pages_cached": 1, "pages_fetched": 0, "pages_failed": 0,
                 "pages_deferred": 0}}
txt = jta.render_report([c30, c29], meta)
check("报告含对照表", "对照表" in txt and "冤枉止损率" in txt)
check("报告含不可判定栏", "不可判定" in txt)
check("报告含措辞纪律声明", "裁决3" in txt)
for w in jta.FORBIDDEN_PHRASES:
    if w in txt:
        check(f"报告含禁词 {w}", False)
        break
else:
    check("报告无禁词", True)

# ── 19. top_waste 榜单 ──
tw = jta.top_waste([c30, c29])
check("top_waste 只收可判定格子", all(x["n"] >= 30 for x in tw))
check("top_waste 冤枉止损带 CI", any(x["type"] == "冤枉止损型" and x["ci"] for x in tw))

# ── 20. JSON 可序列化（若依端契约）──
payload = {"meta": meta, "cells": [{**c30.__dict__}, {**c29.__dict__}],
           "top_waste": tw}
s = json.dumps(payload, ensure_ascii=False, default=str)
check("JSON 序列化", len(s) > 100)

# ── 21. 页缓存：落盘 → 命中不出网 → offline 降级（断点续跑契约）──
tmpdir = tempfile.mkdtemp(prefix="tpsl_smoke_")
orig_dir, orig_fetch = jta.KLINE_CACHE_DIR, jta._fetch_page
try:
    jta.KLINE_CACHE_DIR = tmpdir
    calls = {"n": 0}

    def fake_fetch(symbol, page_start):
        calls["n"] += 1
        return {page_start + i: (100.0, 100.5, 99.5, 100.0)
                for i in range(jta.PAGE_MINUTES)}

    jta._fetch_page = fake_fetch
    # 用很早的历史区间（分钟 0..999 → 2 页），保证是「完整历史页」可落盘
    bars1, st1 = jta.load_minute_bars("TESTUSDT", 0.0, 999 * 60.0)
    check("首轮出网 2 页", st1["pages_fetched"] == 2 and calls["n"] == 2, str(st1))
    check("bar 载入量", len(bars1) == 1000, str(len(bars1)))

    def bomb(symbol, page_start):
        raise AssertionError("缓存命中后不应再出网")

    jta._fetch_page = bomb
    bars2, st2 = jta.load_minute_bars("TESTUSDT", 0.0, 999 * 60.0)
    check("次轮全走缓存零出网", st2["pages_cached"] == 2 and st2["pages_fetched"] == 0)
    check("缓存数据一致", len(bars2) == 1000)

    bars3, st3 = jta.load_minute_bars("OTHERUSDT", 0.0, 999 * 60.0, offline=True)
    check("offline 无缓存 → 页延后", st3["pages_deferred"] == 2 and not bars3, str(st3))

    jta._fetch_page = fake_fetch
    calls["n"] = 0
    _, st4 = jta.load_minute_bars("CAPUSDT", 0.0, 2999 * 60.0, max_requests=2)
    check("max_requests 限流生效", st4["pages_fetched"] == 2 and st4["pages_deferred"] == 4,
          str(st4))
finally:
    jta.KLINE_CACHE_DIR = orig_dir
    jta._fetch_page = orig_fetch
    shutil.rmtree(tmpdir, ignore_errors=True)

# ── 汇总 ──
print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print(f"ALL PASS（共 {21} 组用例）")
