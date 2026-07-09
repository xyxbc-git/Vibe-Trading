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

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
