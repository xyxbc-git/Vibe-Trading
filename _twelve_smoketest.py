#!/usr/bin/env python3
"""十二套技术信号引擎 + 共识 + 降级推理 离线 smoketest：合成 K 线，不联网。

验收断言：
  - 12 套信号器全部返回且字段完整
  - direction 枚举合法、strength/confidence ∈ [0,1]
  - key_levels 结构 [{label, price}]
  - 共识输出 {direction, confidence, score, votes, layers, reasoning, key_levels}
  - 降级推理（无 LLM key）结构完整、degraded=True
"""

from __future__ import annotations

import math
import os
import random

import pandas as pd

import jarvis_reasoning as jr
import jarvis_twelve_systems as jts

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name} {extra}")


def synth_klines(n: int = 300, seed: int = 42, trend: float = 0.0,
                 gap_at: int | None = None, gap_pct: float = 0.03) -> pd.DataFrame:
    """合成 OHLCV K 线；可选在第 gap_at 根注入向上跳空缺口。"""
    rng = random.Random(seed)
    rows = []
    price = 100.0
    for i in range(n):
        drift = trend + rng.gauss(0, 0.015)
        if gap_at is not None and i == gap_at:
            price *= (1 + gap_pct)  # 跳空
        o = price
        c = max(1e-6, price * (1 + drift))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
        v = abs(rng.gauss(1000, 300))
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
        price = c
    return pd.DataFrame(rows)


EXPECTED_SYSTEMS = set(jts.SIGNAL_FUNCS.keys())
SIG_FIELDS = {"system", "name_cn", "direction", "strength", "reasoning", "key_levels",
              "trade_plan"}
CONS_FIELDS = {"direction", "confidence", "score", "votes", "layers", "reasoning",
               "key_levels", "trade_plan"}
REASON_FIELDS = {"direction", "confidence", "reasoning_chain", "risks", "suggestion",
                 "model", "degraded"}
SUG_FIELDS = {"action", "entry_zone", "stop_loss", "target", "position_pct"}
PLAN_FIELDS = {"entry", "entry_type", "stop_loss", "take_profit", "rr", "note"}
CONS_PLAN_FIELDS = {"entry_zone", "stop_loss", "take_profit_1", "take_profit_2",
                    "rr", "position_pct", "basis", "note"}


def validate_plan(tag: str, sysname: str, direction: str, plan) -> None:
    """信号级 trade_plan 断言：字段齐全、方向自洽、rr>0、entry_type 合法。"""
    if plan is None:
        return
    check(f"[{tag}] {sysname} plan 字段完整", PLAN_FIELDS.issubset(plan.keys()),
          str(PLAN_FIELDS - set(plan.keys())))
    check(f"[{tag}] {sysname} plan entry_type 合法",
          plan["entry_type"] in jts.ENTRY_TYPES, str(plan["entry_type"]))
    e, sl, tp = plan["entry"], plan["stop_loss"], plan["take_profit"]
    if direction == "bullish":
        check(f"[{tag}] {sysname} 多单 SL<entry<TP", sl < e < tp, f"sl={sl} e={e} tp={tp}")
    elif direction == "bearish":
        check(f"[{tag}] {sysname} 空单 TP<entry<SL", tp < e < sl, f"tp={tp} e={e} sl={sl}")
    else:
        check(f"[{tag}] {sysname} neutral 不应有 plan", False, str(plan))
    check(f"[{tag}] {sysname} plan rr>0", plan["rr"] > 0, str(plan["rr"]))


def validate_signals(signals: list, tag: str) -> None:
    check(f"[{tag}] 返回 12 套信号", len(signals) == 12, f"got {len(signals)}")
    check(f"[{tag}] system 覆盖全部 12 套",
          {s["system"] for s in signals} == EXPECTED_SYSTEMS,
          str({s["system"] for s in signals} ^ EXPECTED_SYSTEMS))
    for s in signals:
        sysname = s.get("system", "?")
        check(f"[{tag}] {sysname} 字段完整", SIG_FIELDS.issubset(s.keys()),
              str(SIG_FIELDS - set(s.keys())))
        check(f"[{tag}] {sysname} direction 合法",
              s["direction"] in jts.DIRECTIONS, s["direction"])
        check(f"[{tag}] {sysname} strength∈[0,1]",
              isinstance(s["strength"], float) and 0.0 <= s["strength"] <= 1.0
              and math.isfinite(s["strength"]), str(s["strength"]))
        check(f"[{tag}] {sysname} reasoning 非空",
              isinstance(s["reasoning"], str) and len(s["reasoning"]) > 0)
        lv_ok = isinstance(s["key_levels"], list) and all(
            isinstance(x, dict) and "label" in x and "price" in x
            and isinstance(x["price"], float) and math.isfinite(x["price"])
            for x in s["key_levels"])
        check(f"[{tag}] {sysname} key_levels 结构合法", lv_ok, str(s["key_levels"])[:100])
        if s["direction"] == "neutral":
            check(f"[{tag}] {sysname} neutral 无 plan", s["trade_plan"] is None,
                  str(s["trade_plan"])[:80])
        else:
            validate_plan(tag, sysname, s["direction"], s["trade_plan"])


def validate_cons_plan(cons: dict, tag: str) -> None:
    """共识级 trade_plan 断言。"""
    plan = cons.get("trade_plan")
    if cons["direction"] == "neutral":
        check(f"[{tag}] 中性共识无 plan", plan is None, str(plan)[:80])
        return
    if plan is None:
        return  # 有方向但无任何同向系统 plan → 允许为 None（宁缺毋滥）
    check(f"[{tag}] 共识 plan 字段齐全", CONS_PLAN_FIELDS.issubset(plan.keys()),
          str(CONS_PLAN_FIELDS - set(plan.keys())))
    ez = plan["entry_zone"]
    check(f"[{tag}] 共识 entry_zone 区间有序", isinstance(ez, list) and len(ez) == 2
          and ez[0] < ez[1], str(ez))
    mid = (ez[0] + ez[1]) / 2
    tp2 = plan["take_profit_2"]
    if cons["direction"] == "bullish":
        check(f"[{tag}] 共识多单 SL<zone<TP1",
              plan["stop_loss"] < ez[0] and plan["take_profit_1"] > ez[1],
              str(plan))
        if tp2 is not None:
            check(f"[{tag}] 共识 TP2>TP1（多单激进目标更远，重合应为 None）",
                  tp2 > plan["take_profit_1"], str(plan))
    else:
        check(f"[{tag}] 共识空单 TP1<zone<SL",
              plan["take_profit_1"] < ez[0] and plan["stop_loss"] > ez[1],
              str(plan))
        if tp2 is not None:
            check(f"[{tag}] 共识 TP2<TP1（空单激进目标更远，重合应为 None）",
                  tp2 < plan["take_profit_1"], str(plan))
    check(f"[{tag}] 共识 rr≥0.5（荒谬盈亏比已被门禁拦截）", plan["rr"] >= 0.5,
          str(plan["rr"]))
    check(f"[{tag}] 共识 position_pct∈(0,100]", 0 < plan["position_pct"] <= 100,
          str(plan["position_pct"]))
    check(f"[{tag}] 共识 basis 为非空系统列表", isinstance(plan["basis"], list)
          and len(plan["basis"]) >= 1
          and all(b in EXPECTED_SYSTEMS for b in plan["basis"]), str(plan.get("basis")))
    check(f"[{tag}] 共识 mid 在 zone 内", ez[0] <= mid <= ez[1])


def validate_consensus(cons: dict, tag: str) -> None:
    check(f"[{tag}] 共识字段完整", CONS_FIELDS.issubset(cons.keys()),
          str(CONS_FIELDS - set(cons.keys())))
    check(f"[{tag}] 共识 direction 合法", cons["direction"] in jts.DIRECTIONS)
    check(f"[{tag}] 共识 confidence∈[0,1]", 0.0 <= cons["confidence"] <= 1.0,
          str(cons["confidence"]))
    check(f"[{tag}] 共识 score∈[-1,1]", -1.0 <= cons["score"] <= 1.0, str(cons["score"]))
    votes = cons["votes"]
    check(f"[{tag}] votes 三项计数=12",
          sum(votes.get(k, 0) for k in ("bullish", "bearish", "neutral")) == 12, str(votes))
    check(f"[{tag}] layers 含四层",
          set(cons["layers"].keys()) == {"layer1_filter", "layer2_main",
                                         "layer3_resonance", "layer4_adaptive"},
          str(cons["layers"].keys()))


# ── 1. 上涨趋势合成盘 ────────────────────────────────────────────────
df_up = synth_klines(300, seed=1, trend=0.004)
out_up = jts.analyze(df_up)
validate_signals(out_up["signals"], "上涨盘")
validate_consensus(out_up["consensus"], "上涨盘")
validate_cons_plan(out_up["consensus"], "上涨盘")

# ── 2. 下跌趋势合成盘 ────────────────────────────────────────────────
df_dn = synth_klines(300, seed=2, trend=-0.004)
out_dn = jts.analyze(df_dn)
validate_signals(out_dn["signals"], "下跌盘")
validate_consensus(out_dn["consensus"], "下跌盘")
validate_cons_plan(out_dn["consensus"], "下跌盘")

# ── 3. 震荡盘 + 缺口注入 ────────────────────────────────────────────
df_rng = synth_klines(300, seed=3, trend=0.0, gap_at=280, gap_pct=0.05)
out_rng = jts.analyze(df_rng)
validate_signals(out_rng["signals"], "震荡缺口盘")
validate_consensus(out_rng["consensus"], "震荡缺口盘")
validate_cons_plan(out_rng["consensus"], "震荡缺口盘")

# trade_plan 存在性：上涨/下跌强趋势盘至少各有一个方向信号带 plan
n_plans_up = sum(1 for s in out_up["signals"]
                 if s["direction"] != "neutral" and s["trade_plan"])
n_plans_dn = sum(1 for s in out_dn["signals"]
                 if s["direction"] != "neutral" and s["trade_plan"])
check("上涨盘至少 1 个方向信号带 plan", n_plans_up >= 1, str(n_plans_up))
check("下跌盘至少 1 个方向信号带 plan", n_plans_dn >= 1, str(n_plans_dn))
gap_sig = next(s for s in out_rng["signals"] if s["system"] == "gap")
check("缺口盘 gap 信号非零强度或有回补说明",
      gap_sig["strength"] > 0 or "回补" in gap_sig["reasoning"] or "缺口" in gap_sig["reasoning"],
      gap_sig["reasoning"][:80])

# ── 4. 趋势方向 sanity：强上涨盘共识不应看跌（允许 neutral） ─────────
check("强上涨盘共识非 bearish", out_up["consensus"]["direction"] != "bearish",
      str(out_up["consensus"])[:150])
check("强下跌盘共识非 bullish", out_dn["consensus"]["direction"] != "bullish",
      str(out_dn["consensus"])[:150])

# ── 5. 数据不足优雅降级 ──────────────────────────────────────────────
df_tiny = synth_klines(10, seed=4)
out_tiny = jts.analyze(df_tiny)
validate_signals(out_tiny["signals"], "数据不足盘")
validate_consensus(out_tiny["consensus"], "数据不足盘")
check("数据不足全部 neutral",
      all(s["direction"] == "neutral" for s in out_tiny["signals"]),
      str([(s["system"], s["direction"]) for s in out_tiny["signals"] if s["direction"] != "neutral"]))

# ── 6. 马丁/套利：无数据输出 neutral + 说明，不硬造 ──────────────────
mart = next(s for s in out_up["signals"] if s["system"] == "martingale")
arb = next(s for s in out_up["signals"] if s["system"] == "arbitrage")
check("马丁无数据 neutral+说明", mart["direction"] == "neutral" and "资金管理" in mart["reasoning"])
check("套利无数据 neutral+说明", arb["direction"] == "neutral" and "数据不足" in arb["reasoning"])

# 马丁有交易历史时的序列逻辑
mart2 = jts.signal_martingale(df_up, trade_history=[{"pnl": 5}, {"pnl": -3}, {"pnl": -2}])
check("马丁连亏2笔提示4x", "连亏 2 笔" in mart2["reasoning"] and "4x" in mart2["reasoning"],
      mart2["reasoning"])
# 套利有基差数据时启用
arb2 = jts.signal_arbitrage(df_up, basis_data={"basis_pct": 0.8})
check("套利有基差数据出信号", "基差" in arb2["reasoning"] and arb2["strength"] > 0,
      arb2["reasoning"])

# ── 6.1 套利 v2：基差序列统计口径（z-score + 绝对基差双门槛） ─────────
import jarvis_crypto_data as jcd

# _basis_stats：50 样本均值 0、末值 3σ 偏离
_bvals = [0.01 * ((-1) ** i) for i in range(49)] + [0.30]
_bs = jcd._basis_stats(_bvals)
check("arb2 _basis_stats 字段完整",
      {"basis_pct", "basis_mean_pct", "basis_std_pct", "zscore",
       "percentile", "n_samples"}.issubset(_bs.keys()), str(_bs))
check("arb2 _basis_stats 样本不足返回空", jcd._basis_stats([0.1] * 19) == {})
check("arb2 _basis_stats z 显著偏离", _bs["zscore"] >= 2.0, str(_bs["zscore"]))

# 双门槛触发：|z|≥2 且 |基差|≥0.05% → 强信号 + 文案带实际数值与方向中性说明
arb_hit = jts.signal_arbitrage(df_up, basis_data=_bs)
check("arb2 双门槛触发强度≥0.5", arb_hit["strength"] >= 0.5, str(arb_hit["strength"]))
check("arb2 触发文案带实际基差与σ", "z=" in arb_hit["reasoning"]
      and "中性" in arb_hit["reasoning"], arb_hit["reasoning"][:120])
check("arb2 方向恒中性（不赌方向）", arb_hit["direction"] == "neutral")
# 统计显著但绝对基差不够覆盖成本 → 弱提示 0.2
arb_thin = jts.signal_arbitrage(df_up, basis_data={
    "basis_pct": 0.03, "basis_mean_pct": 0.0, "basis_std_pct": 0.01,
    "zscore": 3.0, "percentile": 99.0, "n_samples": 96, "window": "1hx96"})
check("arb2 绝对基差不足→0.2 弱提示", arb_thin["strength"] == 0.2
      and "不够覆盖" in arb_thin["reasoning"], arb_thin["reasoning"][:100])
# 正常区间 → 0.1
arb_norm = jts.signal_arbitrage(df_up, basis_data={
    "basis_pct": 0.005, "basis_mean_pct": 0.0, "basis_std_pct": 0.01,
    "zscore": 0.5, "percentile": 60.0, "n_samples": 96, "window": "1hx96"})
check("arb2 正常区间→0.1 无套利空间", arb_norm["strength"] == 0.1
      and "正常波动" in arb_norm["reasoning"], arb_norm["reasoning"][:100])

# ── 7. 幂等：同数据两次结果一致 ──────────────────────────────────────
check("同数据幂等", jts.analyze(df_up) == out_up)

# ── 8. 降级推理（确保无 LLM key 环境） ───────────────────────────────
for k in ("DEEPSEEK_API_KEY", "JARVIS_LLM_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(k, None)
market = {"symbol": "TESTUSDT", "price": float(df_up["close"].iloc[-1]),
          "atr": float(jts._atr(df_up).iloc[-1])}
res = jr.reason(market, out_up["signals"], out_up["consensus"])
check("推理字段完整", REASON_FIELDS.issubset(res.keys()), str(REASON_FIELDS - set(res.keys())))
check("推理 degraded=True（无 key）", res["degraded"] is True)
check("推理 direction 合法", res["direction"] in jts.DIRECTIONS, res["direction"])
check("推理 confidence∈[0,1]", 0.0 <= res["confidence"] <= 1.0, str(res["confidence"]))
check("推理链非空且为中文步骤", len(res["reasoning_chain"]) >= 3,
      str(len(res["reasoning_chain"])))
check("风险列表非空", len(res["risks"]) >= 1)
check("suggestion 字段完整", SUG_FIELDS.issubset(res["suggestion"].keys()),
      str(SUG_FIELDS - set(res["suggestion"].keys())))
check("action 合法", res["suggestion"]["action"] in ("long", "short", "wait"))
check("position_pct∈[0,100]", 0.0 <= res["suggestion"]["position_pct"] <= 100.0)

# 方向-动作一致性
if res["direction"] == "bullish":
    check("看涨→long", res["suggestion"]["action"] == "long")
elif res["direction"] == "bearish":
    check("看跌→short", res["suggestion"]["action"] == "short")

# ── 9. 推理引擎坏输入不抛出 ──────────────────────────────────────────
bad = jr.reason({}, [], {"direction": "neutral", "confidence": 0, "score": 0, "votes": {}})
check("坏输入不抛出且结构完整", REASON_FIELDS.issubset(bad.keys()), str(bad)[:120])

# ── 10. 多时间框架共识融合（M2：votes=主周期12系统 / tf_votes / layers） ──
mtf = jts.consensus_multi_tf({
    "15m": out_up["consensus"],
    "1h": out_up["consensus"],
    "4h": out_up["consensus"],
})
MTF_FIELDS = {"direction", "confidence", "score", "votes", "tf_votes", "layers",
              "primary_tf", "trade_plan", "reasoning", "key_levels", "tfs"}
check("MTF 字段完整", MTF_FIELDS.issubset(mtf.keys()), str(MTF_FIELDS - set(mtf.keys())))
check("MTF direction 合法", mtf["direction"] in jts.DIRECTIONS)
check("MTF confidence∈[0,1]", 0.0 <= mtf["confidence"] <= 1.0, str(mtf["confidence"]))
check("MTF votes=主周期12系统投票（和=12）",
      sum(mtf["votes"].get(k, 0) for k in ("bullish", "bearish", "neutral")) == 12,
      str(mtf["votes"]))
check("MTF tf_votes 和=TF数(3)",
      sum(mtf["tf_votes"].get(k, 0) for k in ("bullish", "bearish", "neutral")) == 3,
      str(mtf["tf_votes"]))
check("MTF 主周期为 4h", mtf["primary_tf"] == "4h", str(mtf["primary_tf"]))
check("MTF layers 为主周期四层明细",
      set(mtf["layers"].keys()) == {"layer1_filter", "layer2_main",
                                    "layer3_resonance", "layer4_adaptive"},
      str(list(mtf["layers"].keys())))
check("MTF votes 与 4h 单TF votes 一致", mtf["votes"] == out_up["consensus"]["votes"],
      f"{mtf['votes']} vs {out_up['consensus']['votes']}")
check("MTF 同向输入方向一致", mtf["direction"] == out_up["consensus"]["direction"]
      or out_up["consensus"]["direction"] == "neutral",
      f"{mtf['direction']} vs {out_up['consensus']['direction']}")
mtf_empty = jts.consensus_multi_tf({})
check("MTF 空输入优雅降级", mtf_empty["direction"] == "neutral"
      and mtf_empty["confidence"] == 0.0
      and "tf_votes" in mtf_empty and mtf_empty["layers"] == {})
mtf_conflict = jts.consensus_multi_tf({"15m": out_up["consensus"], "4h": out_dn["consensus"]})
check("MTF 冲突输入不抛出", MTF_FIELDS.issubset(mtf_conflict.keys()))
# 4h 缺席时主周期落到最长可用 TF
mtf_no4h = jts.consensus_multi_tf({"15m": out_up["consensus"], "1h": out_dn["consensus"]})
check("MTF 无4h时主周期=1h", mtf_no4h["primary_tf"] == "1h", str(mtf_no4h["primary_tf"]))
# MTF trade_plan：综合有方向且主周期同向有 plan → 带 source_tf；综合中性 → None
if mtf["direction"] in ("bullish", "bearish") and out_up["consensus"]["trade_plan"]:
    check("MTF plan 继承主周期并带 source_tf",
          mtf["trade_plan"] is not None and mtf["trade_plan"].get("source_tf") == "4h",
          str(mtf.get("trade_plan"))[:100])
if mtf_empty["direction"] == "neutral":
    check("MTF 空输入 plan=None", mtf_empty["trade_plan"] is None)

# ── 10.1 m1：置信度按方向票样本量打折（单调性） ──────────────────────
def _mk_sig(system, name, direction, strength):
    return {"system": system, "name_cn": name, "direction": direction,
            "strength": strength, "reasoning": "t", "key_levels": []}

_SYS_ORDER = list(jts.SIGNAL_FUNCS.keys())

def _synth_consensus(n_bull: int) -> dict:
    sigs = []
    for i, sk in enumerate(_SYS_ORDER):
        if i < n_bull:
            sigs.append(_mk_sig(sk, sk, "bullish", 0.8))
        else:
            sigs.append(_mk_sig(sk, sk, "neutral", 0.0))
    return jts.consensus(sigs)

c2 = _synth_consensus(2)   # 2 票方向一致（agree=1，样本少）
c8 = _synth_consensus(8)   # 8 票方向一致（agree=1，样本多）
check("m1 样本多的一致共识置信度更高", c8["confidence"] > c2["confidence"],
      f"c2={c2['confidence']} c8={c8['confidence']}")
check("m1 置信度仍∈[0,1]", 0.0 <= c2["confidence"] <= 1.0 and 0.0 <= c8["confidence"] <= 1.0)

# ── 10.2 m4：翻转事件判定（纯函数，无网络） ──────────────────────────
import jarvis_daemon as jd

def _cons(direction, conf):
    return {"direction": direction, "confidence": conf, "reasoning": "r"}

ev = jd._twelve_events("X", {"direction": "bearish", "confidence": 0.6}, _cons("bullish", 0.8))
check("m4 互翻→consensus_flip(critical)", len(ev) == 1 and ev[0]["kind"] == "consensus_flip"
      and ev[0]["severity"] == "critical", str(ev))
ev = jd._twelve_events("X", {"direction": "bearish", "confidence": 0.6}, _cons("bullish", 0.5))
check("m4 互翻弱共识→warning", ev and ev[0]["severity"] == "warning", str(ev))
ev = jd._twelve_events("X", {"direction": "neutral", "confidence": 0.2}, _cons("bullish", 0.8))
check("m4 中性→强方向=共识建立", ev and ev[0]["kind"] == "consensus_established", str(ev))
ev = jd._twelve_events("X", {"direction": "neutral", "confidence": 0.2}, _cons("bullish", 0.5))
check("m4 中性→弱方向=静默", ev == [], str(ev))
ev = jd._twelve_events("X", {"direction": "bullish", "confidence": 0.8}, _cons("neutral", 0.3))
check("m4 强方向→中性=共识消失", ev and ev[0]["kind"] == "consensus_lost", str(ev))
ev = jd._twelve_events("X", {"direction": "bullish", "confidence": 0.5}, _cons("neutral", 0.3))
check("m4 弱方向→中性=静默", ev == [], str(ev))
ev = jd._twelve_events("X", {"direction": "bullish", "confidence": 0.5}, _cons("bullish", 0.8))
check("m4 同向首破阈值=strong_signal", ev and ev[0]["kind"] == "strong_signal", str(ev))
ev = jd._twelve_events("X", {"direction": "bullish", "confidence": 0.8}, _cons("bullish", 0.85))
check("m4 同向持续强共识=去重静默", ev == [], str(ev))
ev = jd._twelve_events("X", {}, _cons("bullish", 0.8))
check("m4 首轮即强=strong_signal", ev and ev[0]["kind"] == "strong_signal", str(ev))
check("m4 事件 severity 均为合法枚举",
      all(e[0]["severity"] in ("info", "warning", "critical")
          for e in [jd._twelve_events("X", {"direction": "bearish", "confidence": .6}, _cons("bullish", .8))]))

# ── 10.2.1 f4 修复项：聚合门禁 / TP2 退化 / 微价精度 / neutral 不变量 ──

def _mk_plan_sig(system, direction, entry, sl, tp):
    return {"system": system, "name_cn": system, "direction": direction,
            "strength": 0.6, "reasoning": "t", "key_levels": [],
            "trade_plan": {"entry": entry, "entry_type": "market",
                           "stop_loss": sl, "take_profit": tp, "rr": 1.0, "note": ""}}

# M1a 薄中枢：SL 距入场中位 < 0.3xATR（落入 entry_zone 内）→ 必须返回 None
thin = jts._aggregate_trade_plan("bullish", [
    _mk_plan_sig("dow", "bullish", 100.0, 99.9, 105.0),
    _mk_plan_sig("chanlun", "bullish", 100.0, 99.85, 106.0),
], atr=1.0)
check("M1 薄中枢 SL 落入 zone → None", thin is None, str(thin))

# M1b 荒谬盈亏比：rr < 0.5 → None（SL 经 2xATR 兜底=98，TP 100.8 → rr=0.4）
absurd = jts._aggregate_trade_plan("bullish", [
    _mk_plan_sig("dow", "bullish", 100.0, 90.0, 100.8),
], atr=1.0)
check("M1 rr<0.5 荒谬盈亏比 → None", absurd is None, str(absurd))

# M1c 正常计划不被误杀：SL/TP 都在 zone 外侧、rr 合理
ok_plan = jts._aggregate_trade_plan("bullish", [
    _mk_plan_sig("dow", "bullish", 100.0, 95.0, 110.0),
    _mk_plan_sig("turtle", "bullish", 100.5, 96.0, 112.0),
], atr=1.0)
check("M1 正常计划不被门禁误杀", ok_plan is not None, str(ok_plan))
if ok_plan:
    check("M1 SL 在 zone 下侧外", ok_plan["stop_loss"] < ok_plan["entry_zone"][0])
    check("M1 TP1 在 zone 上侧外", ok_plan["take_profit_1"] > ok_plan["entry_zone"][1])
# 空头镜像：SL 在 zone 上侧外、TP 在下侧外
ok_short = jts._aggregate_trade_plan("bearish", [
    _mk_plan_sig("dow", "bearish", 100.0, 105.0, 90.0),
], atr=1.0)
check("M1 空头正常计划通过", ok_short is not None and
      ok_short["stop_loss"] > ok_short["entry_zone"][1]
      and ok_short["take_profit_1"] < ok_short["entry_zone"][0], str(ok_short))
short_thin = jts._aggregate_trade_plan("bearish", [
    _mk_plan_sig("dow", "bearish", 100.0, 100.1, 90.0),
], atr=1.0)
check("M1 空头 SL 落入 zone → None", short_thin is None, str(short_thin))

# m3 TP2==TP1 重合 → take_profit_2 输出 None（单计划聚合必触发）
single = jts._aggregate_trade_plan("bullish", [
    _mk_plan_sig("dow", "bullish", 100.0, 95.0, 110.0),
], atr=1.0)
check("m3 单计划 TP2 重合输出 None", single is not None
      and single["take_profit_2"] is None, str(single))

# ── 10.2.2 v3：止损结构锚定 + RR 门槛 + 级别透明化 ──────────────────

def _mk_struct_df(n: int = 40, dip_at: int = 30, dip_low: float = 97.0,
                  spike_at: int | None = None, spike_high: float = 103.0) -> pd.DataFrame:
    """平坦盘 + 指定位置摆动低点（可选摆动高点）：结构锚定的受控输入。"""
    rows = []
    for i in range(n):
        lo = dip_low if i == dip_at else 100.2
        hi = spike_high if (spike_at is not None and i == spike_at) else 101.0
        rows.append({"open": 100.5, "high": hi, "low": lo, "close": 100.5,
                     "volume": 1.0})
    return pd.DataFrame(rows)


_struct_df = _mk_struct_df()
# v3a 结构锚定：swing 低点 97 → SL = 97 - 0.5x1.0(ATR) = 96.5，sl_basis 写明锚点
v3a = jts._aggregate_trade_plan("bullish", [
    _mk_plan_sig("dow", "bullish", 100.0, 99.0, 105.0),
    _mk_plan_sig("turtle", "bullish", 100.0, 98.0, 112.0),
], atr=1.0, df=_struct_df, min_rr=2.0)
check("v3a 结构锚定 SL=摆动低点-0.5xATR", v3a is not None
      and abs(v3a["stop_loss"] - 96.5) < 1e-6, str(v3a))
check("v3a sl_basis 写明摆动低点锚点", v3a is not None
      and "摆动低点 97" in (v3a.get("sl_basis") or ""), str(v3a and v3a.get("sl_basis")))
check("v3a 计划携带级别透明化字段",
      v3a is not None and all(v3a.get(k) for k in ("entry_basis", "sl_basis", "tp_basis"))
      and v3a.get("min_rr") == 2.0, str(v3a))
check("v3a rr ≥ 门槛", v3a is not None and v3a["rr"] >= 2.0, str(v3a and v3a["rr"]))
# 结构目标中位 (105+112)/2=108.5 ≥ 100+2x3.5=107 → 直接采用结构目标
check("v3a 结构目标达标直接采用", v3a is not None
      and abs(v3a["take_profit_1"] - 108.5) < 1e-6, str(v3a and v3a["take_profit_1"]))

# v3b RR 推导止盈：结构中位不足、最远目标撑得住 → TP1=RR 门槛推导价（结构验证）
v3b = jts._aggregate_trade_plan("bullish", [
    _mk_plan_sig("dow", "bullish", 100.0, 99.0, 105.0),
    _mk_plan_sig("turtle", "bullish", 100.0, 98.0, 108.0),
], atr=1.0, df=_struct_df, min_rr=2.0)
check("v3b TP1=RR 门槛推导价 107", v3b is not None
      and abs(v3b["take_profit_1"] - 107.0) < 1e-6, str(v3b and v3b["take_profit_1"]))
check("v3b tp_basis 写明 RR 推导+结构验证", v3b is not None
      and "RR" in (v3b.get("tp_basis") or "") and "验证" in (v3b.get("tp_basis") or ""),
      str(v3b and v3b.get("tp_basis")))

# v3c RR 不达标 → 观望不硬造：最远结构目标 105 < 107
v3c_plan, v3c_st = jts._aggregate_trade_plan_ex("bullish", [
    _mk_plan_sig("dow", "bullish", 100.0, 99.0, 104.0),
    _mk_plan_sig("turtle", "bullish", 100.0, 98.0, 105.0),
], atr=1.0, df=_struct_df, min_rr=2.0)
check("v3c RR 不达标 plan=None", v3c_plan is None, str(v3c_plan))
check("v3c plan_status=watch+观望原因", v3c_st.get("state") == "watch"
      and "观望" in (v3c_st.get("reason") or ""), str(v3c_st))

# v3d 空头镜像结构锚定：swing 高点 103 → SL = 103 + 0.5xATR = 103.5
v3d = jts._aggregate_trade_plan("bearish", [
    _mk_plan_sig("dow", "bearish", 100.0, 101.5, 92.0),
], atr=1.0, df=_mk_struct_df(spike_at=30, spike_high=103.0), min_rr=2.0)
check("v3d 空头结构锚定 SL=摆动高点+0.5xATR", v3d is not None
      and abs(v3d["stop_loss"] - 103.5) < 1e-6, str(v3d))
check("v3d 空头 rr ≥ 门槛", v3d is not None and v3d["rr"] >= 2.0,
      str(v3d and v3d["rr"]))

# v3e 无 df（兜底口径）：sl_basis 注明未检出结构位；门槛仍生效
v3e = jts._aggregate_trade_plan("bullish", [
    _mk_plan_sig("dow", "bullish", 100.0, 95.0, 110.0),
], atr=1.0, min_rr=2.0)
check("v3e 兜底 sl_basis 注明未检出结构位", v3e is not None
      and "未检出" in (v3e.get("sl_basis") or ""), str(v3e and v3e.get("sl_basis")))

# v3f consensus() 输出 plan_status 字段（ok/watch/neutral 三态之一）
_c_up = out_up["consensus"]
check("v3f consensus 携带 plan_status", isinstance(_c_up.get("plan_status"), dict)
      and _c_up["plan_status"].get("state") in ("ok", "watch", "neutral"),
      str(_c_up.get("plan_status")))
check("v3f plan 与 plan_status 自洽",
      (_c_up.get("trade_plan") is not None) == (_c_up["plan_status"]["state"] == "ok"),
      str(_c_up.get("plan_status")))
# MTF 融合结果同样携带 plan_status
check("v3f MTF 携带 plan_status", isinstance(mtf.get("plan_status"), dict)
      and mtf["plan_status"].get("state") in ("ok", "watch", "neutral"),
      str(mtf.get("plan_status")))

# m2 微价资产精度：PEPE 级价格不归零
micro = jts._plan("bullish", 0.00001234, "market", 0.00001111, 0.00001456, "t")
check("m2 微价 plan 不归零", micro is not None
      and 0 < micro["stop_loss"] < micro["entry"] < micro["take_profit"], str(micro))
check("m2 _round_price 有效数字",
      jts._round_price(0.000012345678) == 1.23457e-05
      and jts._round_price(61971.0912345) == 61971.091235
      or abs(jts._round_price(0.000012345678) - 1.23457e-05) < 1e-12,
      str(jts._round_price(0.000012345678)))

# m4 不变量：方向非法收敛 neutral 时 trade_plan 同步置 None
bogus = jts._sig("x", "x", "bogus", 0.5, "r",
                 trade_plan={"entry": 1, "entry_type": "market", "stop_loss": 0.9,
                             "take_profit": 1.2, "rr": 2.0, "note": ""})
check("m4 非法方向→neutral 且 plan=None",
      bogus["direction"] == "neutral" and bogus["trade_plan"] is None, str(bogus)[:100])

# ── 10.3 m2：LLM 输出 confidence NaN/inf 防护 ────────────────────────
_base_raw = {"direction": "bullish", "reasoning_chain": ["a"], "risks": [],
             "suggestion": {"action": "long", "position_pct": 10}}
r_nan = jr._sanitize_llm_result({**_base_raw, "confidence": float("nan")}, "m")
check("m2 confidence=NaN 落 0.5", r_nan is not None and r_nan["confidence"] == 0.5, str(r_nan))
r_inf = jr._sanitize_llm_result({**_base_raw, "confidence": float("inf")}, "m")
check("m2 confidence=inf 落 0.5", r_inf is not None and r_inf["confidence"] == 0.5, str(r_inf))
r_ok = jr._sanitize_llm_result({**_base_raw, "confidence": 0.9}, "m")
check("m2 正常 confidence 不受影响", r_ok is not None and r_ok["confidence"] == 0.9, str(r_ok))

# ── 11. 洞察落库 roundtrip（临时库隔离，不碰生产 DB） ────────────────
import tempfile

with tempfile.TemporaryDirectory() as tmpd:
    _orig_db = jr.DB_PATH
    jr.DB_PATH = os.path.join(tmpd, "test_insights.db")
    try:
        w = jr.add_insight("TESTUSDT", "strong_signal", "测试强信号",
                           detail="detail-abc", severity="warning")
        check("insight 写入 ok", w.get("ok") is True, str(w))
        items = jr.list_insights(limit=5)
        check("insight 可读回", len(items) == 1, str(items))
        if items:
            it = items[0]
            check("insight 字段完整",
                  {"ts", "symbol", "kind", "title", "detail", "severity"}.issubset(it.keys()),
                  str(it))
            check("insight 内容一致",
                  it["symbol"] == "TESTUSDT" and it["kind"] == "strong_signal"
                  and it["severity"] == "warning" and it["detail"] == "detail-abc", str(it))
        w2 = jr.add_insight("TESTUSDT", "consensus_flip", "翻转", severity="warn")
        check("M1 旧枚举 warn 收敛为 info（只认 info/warning/critical）", w2.get("ok") is True
              and jr.list_insights(limit=1)[0]["severity"] == "info")
        check("按 symbol 过滤", len(jr.list_insights(limit=10, symbol="TESTUSDT")) == 2
              and len(jr.list_insights(limit=10, symbol="OTHER")) == 0)
    finally:
        jr.DB_PATH = _orig_db

# ── 12. [信号篇 P1-3] 摆动系逆势门控 ────────────────────────────────

# 门控纯函数：逆势打折撤计划、顺势/震荡原样放行
_g_s, _g_p, _g_note = jts._apply_counter_trend_gate("bullish", 0.8, {"x": 1}, "down")
check("P1-3 门控：下跌市逆势多单 强度×0.4+撤计划",
      abs(_g_s - 0.32) < 1e-9 and _g_p is None and "已降级" in _g_note,
      f"s={_g_s} p={_g_p} note={_g_note}")
_g_s, _g_p, _g_note = jts._apply_counter_trend_gate("bearish", 0.8, {"x": 1}, "up")
check("P1-3 门控：上涨市逆势空单 强度×0.4+撤计划",
      abs(_g_s - 0.32) < 1e-9 and _g_p is None, f"s={_g_s} p={_g_p}")
_g_s, _g_p, _g_note = jts._apply_counter_trend_gate("bullish", 0.8, {"x": 1}, "up")
check("P1-3 门控：顺势原样放行", _g_s == 0.8 and _g_p == {"x": 1} and _g_note == "")
_g_s, _g_p, _g_note = jts._apply_counter_trend_gate("bearish", 0.8, {"x": 1}, "range")
check("P1-3 门控：震荡市不门控", _g_s == 0.8 and _g_p == {"x": 1} and _g_note == "")


def _mono_df(step: float, n: int = 90, start: float = 200.0) -> pd.DataFrame:
    """单边趋势合成盘（无 swing 摆动点，走 MA50 斜率兜底路径）。"""
    rows, p = [], start
    for _ in range(n):
        o, c = p, p + step
        rows.append({"open": o, "high": max(o, c) + 0.2, "low": min(o, c) - 0.2,
                     "close": c, "volume": 1000.0})
        p = c
    return pd.DataFrame(rows)


df_mono_dn = _mono_df(-0.5)
df_mono_up = _mono_df(+0.5)
check("P1-3 单边下跌 regime=down", jts._trend_regime(df_mono_dn) == "down",
      jts._trend_regime(df_mono_dn))
check("P1-3 单边上涨 regime=up", jts._trend_regime(df_mono_up) == "up",
      jts._trend_regime(df_mono_up))

def _tri_df(n: int = 96, period: int = 12, amp: float = 3.0) -> pd.DataFrame:
    """对称三角波震荡盘（swing 高低点等高等低、MA50 斜率≈0）：受控 range 输入。"""
    rows = []
    for i in range(n):
        phase = i % period
        half = period // 2
        c = 100.0 + amp * (phase if phase <= half else period - phase) / half
        rows.append({"open": c, "high": c + 0.2, "low": c - 0.2,
                     "close": c, "volume": 1000.0})
    return pd.DataFrame(rows)


check("P1-3 震荡盘 regime=range", jts._trend_regime(_tri_df()) == "range",
      jts._trend_regime(_tri_df()))

osc_dn = jts.signal_oscillator(df_mono_dn)
check("P1-3 下跌市超卖信号：方向保留但撤计划",
      osc_dn["direction"] == "bullish" and osc_dn["trade_plan"] is None, str(osc_dn)[:150])
check("P1-3 下跌市超卖信号：强度≤0.4", osc_dn["strength"] <= 0.4 + 1e-9,
      str(osc_dn["strength"]))
check("P1-3 降级说明写入 reasoning", "已降级" in osc_dn["reasoning"],
      osc_dn["reasoning"][:120])
osc_up = jts.signal_oscillator(df_mono_up)
check("P1-3 上涨市超买信号同理降级",
      osc_up["direction"] == "bearish" and osc_up["trade_plan"] is None
      and osc_up["strength"] <= 0.4 + 1e-9 and "已降级" in osc_up["reasoning"],
      str(osc_up)[:150])

# ── 13. [信号篇 P1-4] 突破量能确认 ──────────────────────────────────


def _turtle_breakout_df(breakout_vol: float) -> pd.DataFrame:
    """59 根窄幅震荡（量恒 1000）+ 末根放/缩量向上突破：海龟量能确认受控输入。"""
    rows = [{"open": 100.0, "high": 100.6, "low": 99.4,
             "close": 100.0 + (i % 5) * 0.1, "volume": 1000.0} for i in range(59)]
    rows.append({"open": 100.4, "high": 106.0, "low": 100.2, "close": 105.8,
                 "volume": breakout_vol})
    return pd.DataFrame(rows)


t_hi = jts.signal_turtle(_turtle_breakout_df(2000.0))   # 2.0x 均量 ≥1.2 → 保持
t_mid = jts.signal_turtle(_turtle_breakout_df(1000.0))  # 1.0x → 线性 0.75
t_low = jts.signal_turtle(_turtle_breakout_df(500.0))   # 0.5x ≤0.8 → ×0.5
check("P1-4 海龟放量突破强度保持（无假突破标注）",
      t_hi["direction"] == "bullish" and "假突破" not in t_hi["reasoning"]
      and "放量" in t_hi["reasoning"], t_hi["reasoning"][:120])
check("P1-4 海龟缩量突破强度×0.5",
      abs(t_low["strength"] - t_hi["strength"] * 0.5) < 0.002,
      f"low={t_low['strength']} hi={t_hi['strength']}")
check("P1-4 缩量标注「缩量突破，假突破风险」",
      "缩量突破，假突破风险" in t_low["reasoning"], t_low["reasoning"][:150])
check("P1-4 中间量能线性过渡（1.0x→×0.75）",
      abs(t_mid["strength"] - t_hi["strength"] * 0.75) < 0.002,
      f"mid={t_mid['strength']} hi={t_hi['strength']}")
check("P1-4 只调强度不改方向",
      t_low["direction"] == t_hi["direction"] == "bullish")
check("P1-4 缩量不撤计划（方向仍有 plan）", t_low["trade_plan"] is not None)


def _gap_vol_df(gap_vol: float) -> pd.DataFrame:
    """40 根平盘（量恒 1000）+ 放/缩量向上跳空 + 3 根高位持稳：缺口量能受控输入。"""
    rows = [{"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
             "volume": 1000.0} for _ in range(40)]
    rows.append({"open": 105.0, "high": 106.0, "low": 104.8, "close": 105.5,
                 "volume": gap_vol})
    rows += [{"open": 105.5, "high": 106.2, "low": 105.0, "close": 105.8,
              "volume": 1000.0} for _ in range(3)]
    return pd.DataFrame(rows)


g_hi = jts.signal_gap(_gap_vol_df(2000.0))
g_low = jts.signal_gap(_gap_vol_df(500.0))
check("P1-4 跳空量能取缺口 bar：缩量强度×0.5",
      g_hi["direction"] == "bullish" and g_low["direction"] == "bullish"
      and abs(g_low["strength"] - g_hi["strength"] * 0.5) < 0.002,
      f"low={g_low['strength']} hi={g_hi['strength']}")
check("P1-4 跳空缩量标注假突破风险", "缩量突破，假突破风险" in g_low["reasoning"],
      g_low["reasoning"][:150])

# 量能数据不足（<10 根历史）→ 不惩罚不加注
_vk, _vn = jts._breakout_volume_factor(_turtle_breakout_df(500.0).iloc[-8:].reset_index(drop=True))
check("P1-4 量能历史不足不惩罚", _vk == 1.0 and _vn == "", f"k={_vk} note={_vn}")

# ── 14. [信号篇 P2-5] 江恩时间窗方向降级 ────────────────────────────

# 显著低点恰在 21 根前（斐波那契窗口）→ 时间窗命中；方向应降级为 neutral
_gann_rows = [{"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
               "volume": 1000.0} for _ in range(60)]
_gann_rows[60 - 1 - 21] = {"open": 100.0, "high": 100.5, "low": 90.0,
                           "close": 100.0, "volume": 1000.0}
g_hit = jts.signal_gann(pd.DataFrame(_gann_rows))
check("P2-5 时间窗命中（reasoning 带斐波那契提示）", "斐波那契" in g_hit["reasoning"]
      and "变盘" in g_hit["reasoning"], g_hit["reasoning"][:120])
check("P2-5 方向降级为 neutral 不参与投票", g_hit["direction"] == "neutral",
      g_hit["direction"])
check("P2-5 中性不变量：无交易计划", g_hit["trade_plan"] is None)
check("P2-5 变盘敏感强度保留 0.4", g_hit["strength"] == 0.4, str(g_hit["strength"]))
check("P2-5 倾向说明保留（人工参考）", "倾向" in g_hit["reasoning"],
      g_hit["reasoning"][:150])

# ── 15. [信号篇 P2-1] layer4 恒中性成员稀释修复 ─────────────────────


def _synth_sigs(dir_by_system: dict) -> list:
    """按 {system: (direction, strength)} 生成 12 套合成信号（缺省 neutral 0.0）。"""
    out = []
    for sk in _SYS_ORDER:
        d, s = dir_by_system.get(sk, ("neutral", 0.0))
        out.append(_mk_sig(sk, sk, d, s))
    return out


# 仅 oscillator 有方向（0.8），layer4 其余 4 成员恒中性 → 层得分应为 0.8 而非 0.16
_c_dilute = jts.consensus(_synth_sigs({"oscillator": ("bullish", 0.8)}))
check("P2-1 layer4 只计方向性成员（0.8 不被稀释为 0.16）",
      abs(_c_dilute["layers"]["layer4_adaptive"]["score"] - 0.8) < 1e-9,
      str(_c_dilute["layers"]["layer4_adaptive"]["score"]))
check("P2-1 无方向成员的层得分为 0",
      _c_dilute["layers"]["layer3_resonance"]["score"] == 0.0
      and _c_dilute["layers"]["layer2_main"]["score"] == 0.0,
      str({k: v["score"] for k, v in _c_dilute["layers"].items()}))
# 层内两个方向性成员对冲：均值只除以 2（不是 5）
_c_mix = jts.consensus(_synth_sigs({"oscillator": ("bullish", 0.8),
                                    "gap": ("bearish", 0.4)}))
check("P2-1 方向性成员间均值口径（(0.8-0.4)/2=0.2）",
      abs(_c_mix["layers"]["layer4_adaptive"]["score"] - 0.2) < 1e-9,
      str(_c_mix["layers"]["layer4_adaptive"]["score"]))
check("P2-1 members 明细仍含全部成员（含中性）",
      len(_c_dilute["layers"]["layer4_adaptive"]["members"]) == 5,
      str(len(_c_dilute["layers"]["layer4_adaptive"]["members"])))
check("P2-1 共识 score 仍∈[-1,1] 且结构完整",
      -1.0 <= _c_dilute["score"] <= 1.0 and CONS_FIELDS.issubset(_c_dilute.keys()))

# ── 16. [信号篇 P1-2] 单系统 SL 隐蔽化（_plan 统一出口） ─────────────

# 环境变量固定配置（测试确定性，不受用户 config.yaml 影响），跑完即恢复
import jarvis_config as _jc_t

os.environ["JARVIS_CFG_SL_AVOID_ROUND_LEVELS"] = "1"
os.environ["JARVIS_CFG_SL_SINGLE_ATR_BUFFER_MULT"] = "1.0"
_jc_t._LOAD_CACHE.clear()
try:
    # 价位 ~95 的关口步长为 2.0（{1,2,5}×10^n 心理刻度）→ 关口在偶数位；
    # SL=94.2 贴关口 94（|Δ|=0.2 < near_zone=0.5×ATR）→ 向下避让 1×ATR 到 93
    p_st = jts._plan("bullish", 100.0, "market", 94.2, 110.0, "t", atr=1.0)
    check("P1-2 多单 SL 挪出整数关口扫单区（94.2→93）",
          p_st is not None and abs(p_st["stop_loss"] - 93.0) < 1e-9,
          str(p_st and p_st["stop_loss"]))
    check("P1-2 note 写明隐蔽化依据", p_st is not None and "隐蔽化" in p_st["note"]
          and "缓冲 1×ATR" in p_st["note"], str(p_st and p_st["note"])[:120])
    check("P1-2 RR 按调整后 SL 重算（10/7≈1.43）",
          p_st is not None and abs(p_st["rr"] - round(10.0 / 7.0, 2)) < 1e-9,
          str(p_st and p_st["rr"]))
    # 空单镜像：SL=105.8 贴关口 106 → 向上避让到 107
    p_sh = jts._plan("bearish", 100.0, "market", 105.8, 90.0, "t", atr=1.0)
    check("P1-2 空单 SL 向上避让（105.8→107）",
          p_sh is not None and abs(p_sh["stop_loss"] - 107.0) < 1e-9,
          str(p_sh and p_sh["stop_loss"]))
    # 方向自洽仍强校验：调整后仍多单 SL<entry<TP
    check("P1-2 调整后自洽（SL<entry<TP）",
          p_st is not None and p_st["stop_loss"] < p_st["entry"] < p_st["take_profit"])
    # 远离关口的 SL 原样不动（95.0 距关口 94/96 均 1.0 ≥ near_zone）
    p_far = jts._plan("bullish", 100.0, "market", 95.0, 110.0, "t", atr=1.0)
    check("P1-2 远离关口的 SL 不动（95 原样）",
          p_far is not None and abs(p_far["stop_loss"] - 95.0) < 1e-9
          and "隐蔽化" not in p_far["note"], str(p_far and p_far["stop_loss"]))
    # 主开关关闭 → 贴关口的 SL 也零调整
    os.environ["JARVIS_CFG_SL_AVOID_ROUND_LEVELS"] = "0"
    _jc_t._LOAD_CACHE.clear()
    p_off = jts._plan("bullish", 100.0, "market", 94.2, 110.0, "t", atr=1.0)
    check("P1-2 主开关关闭时零调整（SL 原样 94.2）",
          p_off is not None and abs(p_off["stop_loss"] - 94.2) < 1e-9
          and "隐蔽化" not in p_off["note"], str(p_off and p_off["stop_loss"]))
finally:
    os.environ.pop("JARVIS_CFG_SL_AVOID_ROUND_LEVELS", None)
    os.environ.pop("JARVIS_CFG_SL_SINGLE_ATR_BUFFER_MULT", None)
    _jc_t._LOAD_CACHE.clear()

# 配置读取兜底：缺键/异常回退默认 1.0 且夹紧 [0,2]
check("P1-2 缓冲配置读取（默认 1.0，夹紧范围内）",
      0.0 <= jts._single_sl_buffer_from_config() <= 2.0,
      str(jts._single_sl_buffer_from_config()))

# ── 17. [S3 蓄势雷达] 中性信号触发位 trigger_levels ──────────────────

_df_range = _tri_df()   # 对称三角波：海龟区间内 / dow 交织 → 双双中性
t_neu = jts.signal_turtle(_df_range)
check("S3 海龟中性带双向触发位", t_neu["direction"] == "neutral"
      and t_neu["trigger_levels"] is not None and len(t_neu["trigger_levels"]) == 2,
      str(t_neu.get("trigger_levels")))
if t_neu["trigger_levels"]:
    _sides = {t["side"] for t in t_neu["trigger_levels"]}
    _hh20 = float(_df_range["high"].iloc[-21:-1].max())
    _ll20 = float(_df_range["low"].iloc[-21:-1].min())
    check("S3 海龟触发位=20根区间上下沿",
          _sides == {"bullish", "bearish"}
          and any(abs(t["price"] - _hh20) < 1e-6 for t in t_neu["trigger_levels"])
          and any(abs(t["price"] - _ll20) < 1e-6 for t in t_neu["trigger_levels"]),
          str(t_neu["trigger_levels"]))
    check("S3 触发位结构完整且按距离升序",
          all({"side", "price", "dist_pct", "desc"}.issubset(t.keys())
              and t["dist_pct"] >= 0 and t["desc"]
              for t in t_neu["trigger_levels"])
          and t_neu["trigger_levels"][0]["dist_pct"]
          <= t_neu["trigger_levels"][-1]["dist_pct"],
          str(t_neu["trigger_levels"]))

d_neu = jts.signal_dow(_df_range)
check("S3 道氏交织中性带结构触发位", d_neu["direction"] == "neutral"
      and d_neu["trigger_levels"] and len(d_neu["trigger_levels"]) == 2
      and {t["side"] for t in d_neu["trigger_levels"]} == {"bullish", "bearish"},
      str(d_neu.get("trigger_levels")))

# 全量信号：方向性信号不带 trigger_levels；中性带的结构全部合法
for _tag, _df in (("上涨盘", df_up), ("震荡盘", _df_range)):
    for s in jts.run_all(_df):
        if s["direction"] != "neutral":
            check(f"S3 [{_tag}] {s['system']} 方向信号无触发位",
                  s.get("trigger_levels") is None, str(s.get("trigger_levels")))
        elif s.get("trigger_levels") is not None:
            check(f"S3 [{_tag}] {s['system']} 中性触发位结构合法",
                  isinstance(s["trigger_levels"], list) and len(s["trigger_levels"]) >= 1
                  and all(t["side"] in ("bullish", "bearish")
                          and math.isfinite(t["price"]) and t["price"] > 0
                          and t["dist_pct"] is not None and t["dist_pct"] >= 0
                          and isinstance(t["desc"], str) and t["desc"]
                          for t in s["trigger_levels"]),
                  str(s["trigger_levels"])[:160])

# 中性不变量回归：trigger_levels 不影响 trade_plan 恒 None
check("S3 中性信号仍无交易计划", t_neu["trade_plan"] is None and d_neu["trade_plan"] is None)
# 方向信号强行传 trigger_levels 会被剥除（_sig 不变量）
_bogus = jts._sig("x", "x", "bullish", 0.5, "r",
                  trade_plan={"entry": 1, "entry_type": "market", "stop_loss": 0.9,
                              "take_profit": 1.2, "rr": 2.0, "note": ""},
                  trigger_levels=[jts._trigger("bullish", 1.1, 1.0, "t")])
check("S3 方向信号触发位被剥除（不变量）", _bogus["trigger_levels"] is None)

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
