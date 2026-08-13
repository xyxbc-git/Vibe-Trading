#!/usr/bin/env python3
"""贾维斯 JARVIS — 交易导师·证据引擎与裁决核心（情绪风控，确定性规则）。

背景：用户经常凭感觉情绪化下单且亏多赢少。本模块是「下单前先写计划 →
系统用实时证据裁决 → 红黄绿灯 + 逻辑说服 → 事后复盘建立信任」闭环的
**确定性裁决核心**：不依赖 LLM 也完整工作（AI 解释层由同事并行开发，
可在 verdict 输出之上做二次润色，但灯色与分数以本模块为准）。

组成：
  build_evidence()  证据包组装器——全部复用仓内现有模块**只读**调用，
                    每路独立容错：取不到就诚实标 available=False，不编造。
  verdict()         裁决核心——纯函数（吃证据包与计划，可离线冒烟）。
  mentor_plan 表    计划台账（提交计划、裁决留痕、事后回填结果）。
  stats()           信任回路——红/黄/绿灯各自胜率、听劝 vs 不听劝盈亏对比。

────────────────────────── 预登记（不许事后改） ──────────────────────────
权重（合计 100；证据缺失的项不计入分母，按可用权重归一）：
  trend      30  多周期趋势一致性（4h/1h/30m 共识 + 5m 时机 + 威科夫语境）
  risk       25  风险数学（RR、过路费占比 toll_ratio）
  levels     20  关键位关系（支撑压力/供需区/磁吸位/扫单区）
  structure  15  结构与反转证据（逆势计划必须有反转证据；FVG 结构）
  micro      10  微观确认 + 情绪（Delta 吸收三态、拥挤度/资金费）

红线（数学否决权，直接红灯，不参与加权）：
  R1  RR < 1.5                      —— 赔率数学上不成立
  R2  toll_ratio > 1.0              —— 过路费超过风险预算本身。取证原话：
      「止损距离 < 0.1% 的 95 笔，光过路费就是风险预算的 2 倍，实测胜率仅
      9.5%——下单那一刻就已注定亏损」（贾维斯-正期望重建-开发计划 §三）

情绪规则（强制降档，不参与加权）：
  E1  情绪自评 ≥ 4 且计划方向与多周期共识反向 → 最高只能给黄灯，
      cooldown_min=30（建议冷静 30 分钟再看一次裁决）

灯色规则（按顺序判定）：
  1. 任一红线命中 → red
  2. fail 项 ≥ 2 或 score < 40 → red
  3. score ≥ 70 且无 fail → green
  4. 其余 → yellow
  5. 可用证据权重 < 50/100 → 最高只能黄灯（证据不足不给绿灯——缺证据
     不是证据没问题；对齐 supply_demand 的 coverage 降置信哲学）
  6. 个人军规（V1）：R02/R03/R06 任一被违反 → 最高只能黄灯；
     R06 违反同时把冷静期加长到 extended_cooldown_min（默认 60）；
     其余军规违反只作 warn 叠加展示，不降灯
  7. E1 命中且结果优于黄 → 降为 yellow

个人军规引擎（V1 预登记；默认 8 条参数化、可关，用户可增删改）：
  R01 cooldown_red   上一条红灯裁决 < cooldown_min(30) 分钟 → fail（报复性交易信号）
  R02 toll_gate      toll_ratio > max_toll(0.20) → fail + 降灯（引 9.5% 胜率取证）
  R03 rr_gate        RR < min_rr(2.0) → fail + 降灯
  R04 mtf_align      30m/1h/4h 中与计划同向的 < min_agree(2) 个 → fail（5m 只做时机）
  R05 daily_cap      当日已提交 ≥ max_per_day(3) 单 → fail（过度交易提醒）
  R06 loss_streak    当日已连亏 ≥ streak(2) 单再提交 → fail + 降灯 + 冷静期 60min
  R07 event_window   高影响事件风险窗口内 → warn（事件日历未配置 → skipped）
  R08 risk_pct_cap   预亏 > 本金 × max_loss_pct(1.0)% → warn（未给本金 → skipped）
  ——V3 追加（全部 warn 级行为/时机提醒，不降灯；与 R05/R06 硬约束区分）——
  R09 session_liquidity  提交时刻落在低流动性时段 hours(UTC+8 0-6) → warn；
                         该时段用户自身 graded≥5 时引用真实胜率说话
  R10 funding_extreme    计划方向与极端资金费同向（|费率| > max_abs_funding
                         (0.05%/8h) 且追拥挤方）→ warn；复用 sentiment 的
                         funding 数值，零新增取数（数据缺 → skipped）
  R11 reentry_discipline 同 symbol 同方向 window_min(60) 分钟内有 loss 平仓
                         → 报复性再入场 warn；反转四条件 ≥3/4 视为新结构证据
                         则 pass（结构位收复/123 第二步的代理口径）
  R12 streak_hubris      当日连胜 ≥ win_streak(3) 且本单预亏占本金比高于
                         历史基准均值 → warn（连胜后过度自信；无本金或无
                         基准样本 → skipped）
  状态集合：pass=遵守 / warn=边缘触碰 / fail=违反 / skipped=数据缺失不判定；
  台账上下文（当日提交数/连亏连胜 streak/最近红灯/同向近损/时段胜率/预亏基准）
  由 _rules_context 查库，evaluate_rules 本身为纯函数（离线冒烟可构造）。
──────────────────────────────────────────────────────────────────────

数据纪律：pg/SQLite 只经 jarvis_journal._conn()（jarvis_db 兼容层）；
证据取数全走各模块自带的缓存与降级（封禁期自动 unavailable），本模块
零新增出网端点。FVG 依赖任务 N 的 jarvis_fvg.detect(df)，try-import
降级（契约：[{type, top, bottom, mitigated, age_bars}]）。

用法（CLI）：
  python3 jarvis_trade_mentor.py check --symbol ETHUSDT --direction long \
      --entry 4300 --sl 4250 --tp 4450 --emotion 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time

# ── 预登记常量（改动必须走评审，不许跑完数据后调） ──
WEIGHTS = {"trend": 30, "risk": 25, "levels": 20, "structure": 15, "micro": 10}
RR_HARD_MIN = 1.5           # R1
TOLL_HARD_MAX = 1.0         # R2
TOLL_WARN = 0.20            # 对齐引擎 T3 twelve_max_toll_ratio 默认档
EMOTION_HOT = 4             # E1：情绪自评 ≥4 判「上头」
COOLDOWN_MIN = 30           # E1 冷静期（分钟）
REVERSAL_MIN_SCORE = 3      # 逆势计划需要 反转四条件 ≥3/4
MAGNET_MIN_STRENGTH = 0.5   # 强磁吸位强度门槛（对齐 jarvis_liq_map.magnet_factor）

TOLL_QUOTE = ("取证：止损距离<0.1% 的 95 笔，光过路费就是风险预算的 2 倍，"
              "实测胜率仅 9.5%——下单那一刻就已注定亏损")

# V1 个人军规：内置默认 8 条（参数化、可关；用户可经 /api/mentor/rules 增改启停）
RULE_HARD_FAIL = ("R02", "R03", "R06")   # 违反即降灯（最高黄）的军规
DEFAULT_RULES = (
    ("R01", "红灯单必须过冷静期再提交", "cooldown_red", {"cooldown_min": 30}),
    ("R02", "过路费占风险预算超 20% 不开单", "toll_gate", {"max_toll": 0.20}),
    ("R03", "盈亏比低于 2 不开单", "rr_gate", {"min_rr": 2.0}),
    ("R04", "方向必须顺 30m/1h/4h 中至少 2 个周期（5m 只做入场时机）",
     "mtf_align", {"tfs": ["30m", "1h", "4h"], "min_agree": 2}),
    ("R05", "当日提交超过 3 单触发过度交易提醒", "daily_cap", {"max_per_day": 3}),
    ("R06", "当日连亏 2 单后再提交强制黄灯并加长冷静期", "loss_streak",
     {"streak": 2, "extended_cooldown_min": 60}),
    ("R07", "高影响事件风险窗口内不开新仓", "event_window", {}),
    ("R08", "单笔预亏不超过本金 1%", "risk_pct_cap", {"max_loss_pct": 1.0}),
    # V3 追加：行为/时机类提醒（全 warn 级不降灯）
    ("R09", "凌晨低流动性时段谨慎开单", "session_liquidity",
     {"hours": [0, 6]}),
    ("R10", "资金费极端时不追拥挤方向", "funding_extreme",
     {"max_abs_funding": 0.05}),
    ("R11", "被扫损后同方向再入场需等新结构", "reentry_discipline",
     {"window_min": 60}),
    ("R12", "连胜后仓位回归基准", "streak_hubris", {"win_streak": 3}),
)

_DIR_CN = {"long": "多单", "short": "空单"}
_CONS_OF_PLAN = {"long": "bullish", "short": "bearish"}
_OPP_OF_PLAN = {"long": "bearish", "short": "bullish"}


# ═══════════════════════════ 证据包组装器 ═══════════════════════════

def _ev(available: bool, **kw) -> dict:
    return {"available": bool(available), **kw}


def _trend_evidence(symbol: str, provider=None) -> dict:
    """多周期共识：provider 注入（dashboard 缓存链）优先，否则直连取数。"""
    try:
        if provider is not None:
            data = provider() or {}
            cons = data.get("consensus")
            if cons:
                return _ev(True, consensus=cons, price=data.get("price"),
                           source="dashboard-cache")
            # 缓存未命中 → 落到直连路径（直连再失败才 unavailable）
        import jarvis_twelve_systems as jts
        tf_cons: dict = {}
        price = None
        for tf in ("5m", "30m", "1h", "4h"):
            df = jts.fetch_klines_df(symbol, tf, 300)
            if df is None or len(df) < 30:
                continue
            out = jts.analyze(df)
            tf_cons[tf] = out["consensus"]
            if tf == "4h" or price is None:
                price = float(df["close"].iloc[-1])
        if not tf_cons:
            return _ev(False, reason="K线取数失败（限频/断网），趋势证据不可用")
        return _ev(True, consensus=jts.consensus_multi_tf(tf_cons), price=price,
                   source="direct")
    except Exception as exc:  # noqa: BLE001 — 证据缺失降级，不拖垮裁决
        return _ev(False, reason=repr(exc)[:120])


def _wyckoff_evidence(symbol: str) -> dict:
    try:
        import jarvis_wyckoff as jwk
        out = jwk.analyze(symbol, "1h")
        if not out.get("ok"):
            return _ev(False, reason=out.get("error", "威科夫引擎不可用"))
        st = out.get("state") or {}
        return _ev(True, side=st.get("side"), phase=st.get("phase"),
                   hint=out.get("verdict_hint"), stale=out.get("stale", False))
    except Exception as exc:  # noqa: BLE001
        return _ev(False, reason=repr(exc)[:120])


def _risk_evidence(entry: float, sl: float, tp: float) -> dict:
    """风险数学：纯计划算术，恒可用（这是数学否决权永远在场的原因）。"""
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
    fee_pct, min_rr = 0.05, 2.0
    try:
        import jarvis_config as jc
        fee_pct = float(jc.get("twelve_sim_fee_pct") or 0.05)
        min_rr = float(jc.get("plan_min_rr") or 2.0)
    except Exception:  # noqa: BLE001 — 配置读不到用引擎默认值
        pass
    sl_dist_pct = sl_dist / entry * 100.0 if entry > 0 else 0.0
    toll_ratio = (2.0 * fee_pct / sl_dist_pct) if sl_dist_pct > 0 else float("inf")
    return _ev(True, rr=round(rr, 3), sl_dist_pct=round(sl_dist_pct, 4),
               tp_dist_pct=round(tp_dist / entry * 100.0, 4) if entry > 0 else 0.0,
               toll_ratio=round(toll_ratio, 3), fee_pct=fee_pct, plan_min_rr=min_rr)


def _levels_evidence(symbol: str, tf: str, key_levels: list | None) -> dict:
    """关键位三路：共识 key_levels（由 trend 证据透传）+ 供需区 + 磁吸位 + 扫单。"""
    out = {"key_levels": key_levels or []}
    try:
        import jarvis_supply_demand as jsd
        got = jsd.analyze(symbol, tf)
        out["sd"] = ({"bias": got.get("bias"), "score": got.get("score"),
                      "confidence": got.get("confidence")}
                     if got.get("ok") else None)
    except Exception:  # noqa: BLE001
        out["sd"] = None
    try:
        import jarvis_liq_map as jlm
        got = jlm.assess(symbol, tf)
        out["magnets"] = ([m for m in (got.get("magnets") or [])
                           if (m.get("strength") or 0) >= MAGNET_MIN_STRENGTH]
                          if got.get("ok") else None)
    except Exception:  # noqa: BLE001
        out["magnets"] = None
    try:
        import jarvis_stop_hunt as jsh
        out["hunt"] = jsh.detect(symbol, tf)
    except Exception:  # noqa: BLE001
        out["hunt"] = None
    ok = bool(out["key_levels"]) or out["sd"] is not None \
        or out["magnets"] is not None or out["hunt"] is not None
    return _ev(ok, **out, reason=None if ok else "关键位三路证据全部不可用")


def _micro_evidence(symbol: str, tf: str, plan_dir: str) -> dict:
    """微观确认：Delta 安全带三态 + 情绪面 + 反转四条件评分。"""
    out: dict = {}
    delta_payload = None
    try:
        import jarvis_delta_flow as jdf
        got = jdf.analyze(symbol, tf)
        delta_payload = got if got.get("ok") else None
    except Exception:  # noqa: BLE001
        delta_payload = None
    try:
        import jarvis_seatbelt as jsb
        out["seatbelt"] = jsb.evaluate(_CONS_OF_PLAN[plan_dir], delta_payload)
    except Exception:  # noqa: BLE001
        out["seatbelt"] = None
    try:
        import jarvis_sentiment as jst
        got = jst.assess(symbol)
        if got.get("ok"):
            # V3/R10：顺带保留资金费原始数值（小数，×100=%/8h），零新增取数
            funding = next((f.get("value") for f in (got.get("factors") or [])
                            if f.get("key") == "funding" and f.get("available")
                            and f.get("value") is not None), None)
            out["sentiment"] = {"score": got.get("score"), "bias": got.get("bias"),
                                "warnings": got.get("warnings") or [],
                                "funding": funding}
        else:
            out["sentiment"] = None
    except Exception:  # noqa: BLE001
        out["sentiment"] = None
    try:
        import jarvis_stop_hunt as jsh
        hunt = None
        try:
            hunt = jsh.detect(symbol, tf)
        except Exception:  # noqa: BLE001
            hunt = None
        out["reversal"] = jsh.aggregate_reversal_score(delta_payload, None, hunt)
    except Exception:  # noqa: BLE001
        out["reversal"] = None
    ok = any(v is not None for v in out.values())
    return _ev(ok, **out, reason=None if ok else "微观证据全部不可用")


def _history_evidence(symbol: str, tf: str) -> dict:
    """历史战绩：tpsl_advisor 的 MFE/MAE 分布 + 信号层胜率回测缓存（净口径）。"""
    import os
    out: dict = {"tpsl_cells": None, "winrate": None}
    try:
        path = os.path.expanduser(f"~/.vibe-trading/tpsl_advisor/{symbol}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            cells = [c for c in data.get("cells", [])
                     if c.get("tf") == tf and c.get("verdict") == "OK"]
            if cells:
                out["tpsl_cells"] = [
                    {"system": c["system"], "n": c["n"],
                     "mfe_fix_p60": (c.get("mfe_fix_r") or {}).get("p60"),
                     "unjust_sl_rate": c.get("unjust_sl_rate"),
                     "tp_reach_rate": c.get("tp_reach_rate")} for c in cells]
    except Exception:  # noqa: BLE001
        pass
    try:
        import jarvis_signal_winrate as jsw
        out["winrate"] = jsw.get_cached(symbol, tf)
    except Exception:  # noqa: BLE001
        pass
    ok = out["tpsl_cells"] is not None or out["winrate"] is not None
    return _ev(ok, **out, reason=None if ok else "历史战绩缓存为空（先跑回测/advisor）")


def _fvg_evidence(symbol: str, tf: str) -> dict:
    """FVG（任务 N 并行开发）：jarvis_fvg.detect(df) → [{type,top,bottom,mitigated,age_bars}]。"""
    try:
        import jarvis_fvg  # noqa: F401 — 任务 N 交付前 import 失败即降级
    except Exception:
        return _ev(False, reason="jarvis_fvg 未就绪（任务 N 并行开发中），已降级")
    try:
        import jarvis_twelve_systems as jts
        df = jts.fetch_klines_df(symbol, tf, 300)
        if df is None or len(df) < 30:
            return _ev(False, reason="K线取数失败，FVG 证据不可用")
        # [N2 修] detect(df) 返回 dict 契约 {ok, zones, premium_discount, ...}
        # （原按 list 迭代必抛 AttributeError → 本证据路恒降级）；zones 才是缺口表
        out = jarvis_fvg.detect(df)
        zones = (out or {}).get("zones") or []
        return _ev(bool((out or {}).get("ok")),
                   gaps=[g for g in zones if not g.get("mitigated")][:6],
                   premium_discount=(out or {}).get("premium_discount"),
                   reason=(out or {}).get("reason"))
    except Exception as exc:  # noqa: BLE001
        return _ev(False, reason=repr(exc)[:120])


def _event_risk_evidence(symbol: str) -> dict:
    """事件风险窗口（任务 U）：jarvis_event_calendar.mentor_item 动态 import。

    未配置金十 key / 模块未就绪 → available=False（unavailable 不计分母，
    照 FVG 路接法）；不把「没数据」误读成「没风险」。
    """
    try:
        import jarvis_event_calendar as jec
    except Exception:
        return _ev(False, reason="jarvis_event_calendar 未就绪，已降级")
    try:
        item = jec.mentor_item(symbol)
        if not item.get("available"):
            return _ev(False, reason=item.get("note") or "事件日历未配置")
        return _ev(True, in_window=bool(item.get("in_window")),
                   note=item.get("note"), event=item.get("event"),
                   minutes_to=item.get("minutes_to"))
    except Exception as exc:  # noqa: BLE001 — 证据缺失降级，不拖垮裁决
        return _ev(False, reason=repr(exc)[:120])


def build_evidence(symbol: str, direction: str, entry: float, sl: float, tp: float,
                   *, tf: str = "30m", consensus_provider=None) -> dict:
    """组装一份证据包（全只读；每路独立容错，缺失诚实标 available=False）。

    Args:
        tf: 计划主判读周期（关键位/微观/历史战绩按此周期取证）
        consensus_provider: dashboard 进程内注入的共识取数闭包（复用 _cached 链）；
                            None 时直连模块取数（CLI/离线场景）
    """
    direction = direction if direction in ("long", "short") else "long"
    trend = _trend_evidence(symbol, provider=consensus_provider)
    key_levels = (trend.get("consensus") or {}).get("key_levels") if trend["available"] else None
    return {
        "symbol": symbol.upper(), "tf": tf, "as_of": time.time(),
        "plan": {"direction": direction, "entry": entry, "stop_loss": sl,
                 "take_profit": tp},
        "trend": trend,
        "wyckoff": _wyckoff_evidence(symbol),
        "risk": _risk_evidence(entry, sl, tp),
        "levels": _levels_evidence(symbol, tf, key_levels),
        "micro": _micro_evidence(symbol, tf, direction),
        "history": _history_evidence(symbol.upper(), tf),
        "fvg": _fvg_evidence(symbol, tf),
        "event_risk": _event_risk_evidence(symbol),
    }


# ═══════════════════════════ 裁决核心（纯函数） ═══════════════════════════

def _item(key: str, level: str, evidence: str, detail: str = "",
          raw: dict | None = None) -> dict:
    """裁决明细项。契约（R2 热修后）：evidence/detail 是**人话字符串**（前端可
    直接渲染）；结构化数据一律放 raw（前端不得直接当 child 渲染）。"""
    return {"key": key, "level": level, "weight": WEIGHTS.get(key, 0),
            "evidence": str(evidence), "detail": str(detail or ""),
            "raw": raw or {}}


def _judge_trend(ev: dict, plan_dir: str) -> dict:
    t = ev.get("trend") or {}
    if not t.get("available"):
        return _item("trend", "unavailable", f"多周期趋势证据不可用（{t.get('reason', '未知')}）")
    cons = t["consensus"]
    d, conf, score = cons.get("direction"), float(cons.get("confidence") or 0), cons.get("score")
    want = _CONS_OF_PLAN[plan_dir]
    dir_cn = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}.get(d, d)
    tfs = cons.get("tfs") or {}
    tf5 = tfs.get("5m") or {}
    timing = ""
    if tf5:
        d5 = tf5.get("direction")
        d5_cn = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}.get(d5, "?")
        timing = (f"；5m 入场时机{'同向' if d5 == want else '不同向'}"
                  f"（{d5_cn} {float(tf5.get('confidence') or 0):.0%}）")
    wk = ev.get("wyckoff") or {}
    wk_txt = ""
    if wk.get("available") and wk.get("side"):
        wk_txt = f"；1h 威科夫 {wk.get('side')}-{wk.get('phase')} 段"
    base = (f"多周期共识{dir_cn}（加权分 {score:+.3f}，置信度 {conf:.0%}），"
            f"你的{_DIR_CN[plan_dir]}")
    # R2 契约：detail 只放人话字符串；机器可读的方向/置信度进 raw
    detail_txt = f"共识{dir_cn}，置信度 {conf:.0%}"
    raw = {"direction": d, "confidence": conf}
    if d == want:
        if conf >= 0.5:
            return _item("trend", "pass", base + f"与共识同向{timing}{wk_txt}",
                         detail_txt, raw)
        return _item("trend", "warn", base + f"同向但共识置信度只有 {conf:.0%}，"
                     f"方向证据还不扎实{timing}{wk_txt}", detail_txt, raw)
    if d == "neutral":
        return _item("trend", "warn",
                     base + f"面对的是中性市——方向暂无共识支撑{timing}{wk_txt}",
                     detail_txt, raw)
    return _item("trend", "fail", base + f"与共识**反向**{timing}{wk_txt}",
                 detail_txt, raw)


def _judge_risk(ev: dict, plan: dict | None = None) -> tuple[dict, list[str]]:
    """风险数学 + 红线否决清单（红线独立于加权，见预登记）。

    plan 提供 principal（本金 USDT）与 leverage 时，追加一条「打到止损亏多少、
    占本金百分之几」的人话证据；预亏占本金 > 50% 时该项至少 warn（重仓提醒）。
    """
    r = ev["risk"]
    rr, toll = r["rr"], r["toll_ratio"]
    vetoes: list[str] = []
    if rr < RR_HARD_MIN:
        vetoes.append(f"RR={rr:.2f} < {RR_HARD_MIN}（红线 R1：赔率数学不成立）")
    if toll > TOLL_HARD_MAX:
        vetoes.append(f"toll_ratio={toll:.2f} > {TOLL_HARD_MAX}（红线 R2：过路费超过风险预算）。"
                      + TOLL_QUOTE)
    base = (f"RR={rr:.2f}（配置门槛 {r['plan_min_rr']}），止损距离 {r['sl_dist_pct']:.3f}%，"
            f"过路费占风险预算 {toll:.1%}")

    # principal/leverage 追加句（R2）：把抽象百分比翻译成用户钱包里的钱
    sizing_txt = ""
    heavy = False
    principal = float((plan or {}).get("principal") or 0)
    leverage = float((plan or {}).get("leverage") or 0)
    if principal > 0 and leverage > 0:
        notional = principal * leverage
        loss_usd = notional * r["sl_dist_pct"] / 100.0
        loss_pct_of_principal = leverage * r["sl_dist_pct"]
        heavy = loss_pct_of_principal > 50.0
        sizing_txt = (f"；本单名义 {notional:,.0f} U（本金 {principal:,.0f} U × "
                      f"{leverage:g}x），打到止损预计亏 {loss_usd:,.1f} U"
                      f"（占本金 {loss_pct_of_principal:.1f}%）")
        if heavy:
            sizing_txt += "——单笔风险超过本金一半，属于重仓豪赌，强烈建议缩仓"
    detail_txt = (f"RR={rr:.2f}，toll={toll:.1%}"
                  + (f"，预亏占本金 {leverage * r['sl_dist_pct']:.1f}%"
                     if principal > 0 and leverage > 0 else ""))
    raw = {**r, "principal": principal or None, "leverage": leverage or None}

    if vetoes:
        return _item("risk", "fail", base + sizing_txt + "——数学否决",
                     detail_txt, raw), vetoes
    if rr < r["plan_min_rr"]:
        return _item("risk", "warn",
                     base + f"——RR 低于配置门槛 {r['plan_min_rr']}" + sizing_txt,
                     detail_txt, raw), []
    if toll > TOLL_WARN:
        return _item("risk", "warn",
                     base + f"——过路费占比超过 {TOLL_WARN:.0%} 警戒档（引擎 T3 同口径），"
                            "考虑放宽止损并同比例缩仓" + sizing_txt,
                     detail_txt, raw), []
    if heavy:
        return _item("risk", "warn", base + sizing_txt, detail_txt, raw), []
    return _item("risk", "pass", base + "——赔率与成本结构健康" + sizing_txt,
                 detail_txt, raw), []


def _judge_levels(ev: dict, plan: dict) -> dict:
    lv = ev.get("levels") or {}
    if not lv.get("available"):
        return _item("levels", "unavailable", f"关键位证据不可用（{lv.get('reason', '未知')}）")
    entry, sl, tp = plan["entry"], plan["stop_loss"], plan["take_profit"]
    long_side = plan["direction"] == "long"
    warns: list[str] = []
    passes: list[str] = []

    magnets = lv.get("magnets")
    if magnets:
        lo, hi = (entry, tp) if long_side else (tp, entry)
        blocking = [m for m in magnets if lo < float(m.get("price_mid") or 0) < hi]
        if blocking:
            m = blocking[0]
            warns.append(f"TP 要穿过强磁吸位 {m.get('price_mid')}（{m.get('label', '清算簇')}，"
                         f"强度 {m.get('strength')}）——价格常在磁吸位回头，利润可能回吐")
        else:
            passes.append("entry→TP 路径无强磁吸位阻挡")

    hunt = lv.get("hunt")
    if hunt and hunt.get("detected"):
        swept = float(hunt.get("sweptLevel") or 0)
        sl_in_zone = (swept <= sl <= entry) if long_side else (entry <= sl <= swept)
        if sl_in_zone:
            warns.append(f"SL {sl} 挂在刚被扫过的流动性区（{hunt.get('side')} 扫单位 "
                         f"{swept}）——同类止损单容易再次被针扫掉")
        else:
            passes.append(f"检测到{hunt.get('side')}扫单（位 {swept}），SL 不在扫单区")

    sd = lv.get("sd")
    if sd and sd.get("bias") and sd["bias"] != "neutral":
        agree = (sd["bias"] == "accumulation") == long_side
        txt = (f"供需裁决 {sd['bias']}（score={sd.get('score')}，"
               f"conf={sd.get('confidence')}）")
        (passes if agree else warns).append(txt + ("与计划同向" if agree else "与计划反向"))

    kl = lv.get("key_levels") or []
    if kl:
        below = [float(k["price"]) for k in kl if float(k.get("price") or 0) < entry]
        above = [float(k["price"]) for k in kl if float(k.get("price") or 0) > entry]
        shield = max(below) if long_side and below else (min(above) if not long_side and above else None)
        if shield is not None:
            protected = (sl < shield) if long_side else (sl > shield)
            if protected:
                passes.append(f"SL 躲在最近关键位 {shield:g} 之外（关键位先挡一刀）")
            else:
                warns.append(f"SL {sl} 在最近关键位 {shield:g} 的内侧——关键位被测试时"
                             "你会先被打掉，护不住")

    if not warns and not passes:
        return _item("levels", "unavailable", "关键位证据不足以判读")
    detail_txt = f"警示 {len(warns)} 条 / 通过 {len(passes)} 条"
    raw = {"warns": warns, "passes": passes}
    if warns:
        level = "warn" if passes or len(warns) == 1 else "fail"
        return _item("levels", level, "；".join(warns + passes), detail_txt, raw)
    return _item("levels", "pass", "；".join(passes), detail_txt, raw)


def _judge_structure(ev: dict, plan_dir: str) -> dict:
    t = ev.get("trend") or {}
    micro = ev.get("micro") or {}
    fvg = ev.get("fvg") or {}
    cons_dir = ((t.get("consensus") or {}).get("direction")
                if t.get("available") else None)
    against = cons_dir == _OPP_OF_PLAN[plan_dir]

    rev = micro.get("reversal") if micro.get("available") else None
    rev_sat = int(rev.get("satisfied") or 0) if rev else None

    fvg_txt = ""
    if fvg.get("available"):
        want = "bullish" if plan_dir == "long" else "bearish"
        mine = [g for g in fvg.get("gaps", []) if g.get("type") == want]
        fvg_txt = (f"；同向未回补 FVG {len(mine)} 个" if mine else "；无同向未回补 FVG")

    detail_txt = (("逆势" if against else "顺势")
                  + (f"，反转四条件 {rev_sat}/4" if rev_sat is not None else ""))
    raw = {"against": against, "reversal": rev_sat}
    if against:
        if rev_sat is None:
            return _item("structure", "fail",
                         "计划与多周期共识反向（逆势），且反转证据引擎不可用——"
                         "没有证据支持的逆势是赌博" + fvg_txt, detail_txt, raw)
        if rev_sat >= REVERSAL_MIN_SCORE:
            return _item("structure", "pass",
                         f"逆势计划但反转四条件 {rev_sat}/4 达标（{rev.get('verdict')}）"
                         + fvg_txt, detail_txt, raw)
        return _item("structure", "fail",
                     f"逆势计划且反转证据不足：四条件仅 {rev_sat}/4（需 ≥{REVERSAL_MIN_SCORE}），"
                     f"缺口见反转评分明细" + fvg_txt, detail_txt, raw)
    if not t.get("available"):
        return _item("structure", "unavailable", "共识不可用，无法判定顺逆势" + fvg_txt)
    if rev_sat is not None and rev_sat >= REVERSAL_MIN_SCORE:
        return _item("structure", "pass",
                     f"顺势计划，另有反转四条件 {rev_sat}/4 的底部/顶部证据加持" + fvg_txt,
                     detail_txt, raw)
    return _item("structure", "pass", "顺势计划（与共识同向或共识中性）" + fvg_txt,
                 detail_txt, raw)


def _judge_micro(ev: dict, plan: dict) -> dict:
    m = ev.get("micro") or {}
    if not m.get("available"):
        return _item("micro", "unavailable", f"微观证据不可用（{m.get('reason', '未知')}）")
    parts: list[str] = []
    worst = "pass"
    sb = m.get("seatbelt")
    if sb:
        st = sb.get("status")
        if st == "conflict":
            worst = "fail" if sb.get("grade") == "strong" else "warn"
            parts.append(f"Delta 安全带：反向背离顶撞（{sb.get('grade')}）——{sb.get('note', '')}")
        elif st == "confirm":
            parts.append(f"Delta 安全带：同向确认（{sb.get('grade')}）")
        elif st == "unavailable":
            parts.append("Delta 引擎未就绪")
        else:
            parts.append("Delta 中性（无背离证据）")
    snt = m.get("sentiment")
    if snt:
        ws = snt.get("warnings") or []
        if ws:
            if worst == "pass":
                worst = "warn"
            parts.append("情绪面警示：" + "；".join(str(w) for w in ws[:2]))
        else:
            parts.append(f"情绪面 {snt.get('bias', '—')}（score={snt.get('score')}）无警示")
    hist = ev.get("history") or {}
    if hist.get("available") and hist.get("tpsl_cells"):
        c = hist["tpsl_cells"][0]
        parts.append(f"历史包袱：{c['system']}×{ev.get('tf')} 冤枉止损率 "
                     f"{(c.get('unjust_sl_rate') or 0):.0%}、TP 触达率 "
                     f"{(c.get('tp_reach_rate') or 0):.0%}（n={c['n']}，advisor 口径）")
    if not parts:
        return _item("micro", "unavailable", "微观证据不足以判读")
    return _item("micro", worst, "；".join(parts))


def _judge_event_risk(ev: dict) -> dict:
    """事件风险窗口（任务 U·第八路）：不在 WEIGHTS 内 → weight=0 不参与加权，
    只作明细警示——风险窗口内 warn，窗口外 pass，未配置 unavailable 不计分母。"""
    er = ev.get("event_risk") or {}
    if not er.get("available"):
        return _item("event_risk", "unavailable",
                     f"事件日历不可用（{er.get('reason', '未配置金十 key')}）")
    if er.get("in_window"):
        return _item("event_risk", "warn",
                     er.get("note") or "高影响宏观数据风险窗口内，数据瞬间插针风险高",
                     raw={"event": er.get("event"), "minutes_to": er.get("minutes_to")})
    return _item("event_risk", "pass",
                 er.get("note") or "未来无临近高影响宏观事件",
                 raw={"event": er.get("event"), "minutes_to": er.get("minutes_to")})


# ═══════════════════════════ 个人军规引擎（V1，纯函数） ═══════════════════════════

def _rule_params(rule: dict) -> dict:
    p = rule.get("params")
    if isinstance(p, str):
        try:
            p = json.loads(p or "{}")
        except Exception:  # noqa: BLE001
            p = {}
    return p if isinstance(p, dict) else {}


def evaluate_rules(evidence: dict, plan: dict, rules: list[dict],
                   ctx: dict | None = None) -> list[dict]:
    """逐条核对个人军规（纯函数，预登记见模块头）。

    ctx = _rules_context() 的台账上下文：{today_submitted, loss_streak_today,
    last_red_age_min}；离线冒烟可直接构造。
    返回 [{rule_id, title, status: pass|warn|fail|skipped, evidence}]。
    """
    ctx = ctx or {}
    out: list[dict] = []
    risk = evidence.get("risk") or {}
    plan_dir = plan.get("direction", "long")
    for rule in rules:
        if not rule.get("enabled", 1):
            continue
        rid, rtype = rule.get("rule_id", "?"), rule.get("rtype", "custom")
        title = rule.get("title", rid)
        p = _rule_params(rule)
        status, ev_txt = "pass", ""
        if rtype == "cooldown_red":
            cd = float(p.get("cooldown_min", 30))
            age = ctx.get("last_red_age_min")
            if age is None:
                ev_txt = "近期无红灯裁决，无需冷静期"
            elif age < cd:
                status = "fail"
                ev_txt = (f"上一条红灯裁决仅 {age:.0f} 分钟前（<{cd:.0f}），冷静期未过——"
                          "红灯后急着再提交，往往是报复性交易")
            else:
                ev_txt = f"上一条红灯已过 {age:.0f} 分钟（≥{cd:.0f}），冷静期已满"
        elif rtype == "toll_gate":
            mx = float(p.get("max_toll", 0.20))
            toll = float(risk.get("toll_ratio") or 0)
            if toll > mx:
                status = "fail"
                ev_txt = f"过路费占风险预算 {toll:.1%} > {mx:.0%}。" + TOLL_QUOTE
            else:
                ev_txt = f"过路费占比 {toll:.1%} ≤ {mx:.0%}，成本结构可接受"
        elif rtype == "rr_gate":
            mn = float(p.get("min_rr", 2.0))
            rr = float(risk.get("rr") or 0)
            if rr < mn:
                status = "fail"
                ev_txt = f"RR={rr:.2f} < {mn:g}——按你的军规这单赔率不够，不开"
            else:
                ev_txt = f"RR={rr:.2f} ≥ {mn:g}，赔率达标"
        elif rtype == "mtf_align":
            t = evidence.get("trend") or {}
            tfs = (t.get("consensus") or {}).get("tfs") or {} if t.get("available") else {}
            want_tfs = list(p.get("tfs", ["30m", "1h", "4h"]))
            need = int(p.get("min_agree", 2))
            want_dir = _CONS_OF_PLAN[plan_dir]
            visible = [tf for tf in want_tfs if tf in tfs]
            if not visible:
                status = "skipped"
                ev_txt = "30m/1h/4h 共识不可用（取数降级中），本条不判定"
            else:
                agree = [tf for tf in visible
                         if (tfs[tf] or {}).get("direction") == want_dir]
                if len(agree) >= need:
                    status = "pass"
                    ev_txt = (f"{'/'.join(agree)} 与计划同向（{len(agree)}/{len(visible)}"
                              f" ≥ {need}），5m 仅作入场时机")
                else:
                    status = "fail"
                    ev_txt = (f"{'/'.join(want_tfs)} 中仅 {len(agree)} 个周期与计划同向"
                              f"（需 ≥{need}）——方向没有多周期背书")
        elif rtype == "daily_cap":
            cap = int(p.get("max_per_day", 3))
            n = int(ctx.get("today_submitted") or 0)
            if n >= cap:
                status = "fail"
                ev_txt = (f"今天已提交 {n} 单（≥{cap}）——交易越多手续费越厚、"
                          "决策质量越差，这是过度交易信号")
            else:
                ev_txt = f"今天第 {n + 1} 单（军规上限 {cap}），频次健康"
        elif rtype == "loss_streak":
            need = int(p.get("streak", 2))
            got_streak = int(ctx.get("loss_streak_today") or 0)
            if got_streak >= need:
                status = "fail"
                ev_txt = (f"今天已连亏 {got_streak} 单（≥{need}）——连亏后最容易上头翻本，"
                          f"强制黄灯并把冷静期加长到 "
                          f"{int(p.get('extended_cooldown_min', 60))} 分钟")
            else:
                ev_txt = f"当日连亏 {got_streak} 单（<{need}），未触发翻本保护"
        elif rtype == "event_window":
            er = evidence.get("event_risk") or {}
            if not er.get("available"):
                status = "skipped"
                ev_txt = f"事件日历不可用（{er.get('reason', '未配置')}），本条不判定"
            elif er.get("in_window"):
                status = "warn"
                ev_txt = str(er.get("note") or "高影响事件风险窗口内，插针风险高")
            else:
                ev_txt = str(er.get("note") or "当前不在高影响事件窗口")
        elif rtype == "risk_pct_cap":
            principal = float(plan.get("principal") or 0)
            leverage = float(plan.get("leverage") or 0)
            if principal <= 0 or leverage <= 0:
                status = "skipped"
                ev_txt = "未提供本金/杠杆，本条不判定（在计划里填 principal+leverage 可启用）"
            else:
                mx = float(p.get("max_loss_pct", 1.0))
                loss_pct = leverage * float(risk.get("sl_dist_pct") or 0)
                if loss_pct > mx:
                    status = "warn"
                    ev_txt = (f"打到止损预计亏本金的 {loss_pct:.1f}%（军规日常档 ≤{mx:g}%）"
                              f"——{'已属重仓豪赌' if loss_pct > 50 else '超出你的日常风险档'}，建议缩仓")
                else:
                    ev_txt = f"预亏占本金 {loss_pct:.2f}% ≤ {mx:g}%，仓位在日常风险档内"
        elif rtype == "session_liquidity":
            hours = p.get("hours", [0, 6])
            lo_h, hi_h = int(hours[0]), int(hours[1])
            hour = ctx.get("now_hour_utc8")
            if hour is None:
                status = "skipped"
                ev_txt = "无提交时刻上下文，本条不判定"
            elif lo_h <= int(hour) < hi_h:
                status = "warn"
                ev_txt = (f"当前北京时间 {int(hour):02d} 点，处于凌晨低流动性时段"
                          f"（{lo_h:02d}-{hi_h:02d}）——点差和插针风险高，"
                          "新手胜率普遍最差的时段")
                ss = ctx.get("session_stats")
                if ss and ss.get("win_rate") is not None:
                    ev_txt += (f"；你自己在这个时段的真实胜率 {ss['win_rate']:.0%}"
                               f"（n={ss['n']}）——用你自己的数据说话")
            else:
                ev_txt = f"当前北京时间 {int(hour):02d} 点，不在低流动性时段"
        elif rtype == "funding_extreme":
            mx = float(p.get("max_abs_funding", 0.05))   # 单位 %/8h
            snt = (evidence.get("micro") or {}).get("sentiment") \
                if (evidence.get("micro") or {}).get("available") else None
            funding = (snt or {}).get("funding")
            if funding is None:
                status = "skipped"
                ev_txt = "资金费数据不可用，本条不判定"
            else:
                f_pct = float(funding) * 100.0
                crowd_long = f_pct > mx
                crowd_short = f_pct < -mx
                if crowd_long and plan_dir == "long":
                    status = "warn"
                    ev_txt = (f"8h 资金费 {f_pct:+.4f}% > +{mx:g}%，多头拥挤——"
                              "你在追拥挤方向，拥挤方易被收割")
                elif crowd_short and plan_dir == "short":
                    status = "warn"
                    ev_txt = (f"8h 资金费 {f_pct:+.4f}% < -{mx:g}%，空头拥挤——"
                              "你在追拥挤方向，谨防轧空")
                else:
                    ev_txt = f"8h 资金费 {f_pct:+.4f}%，与计划方向无拥挤冲突"
        elif rtype == "reentry_discipline":
            win = float(p.get("window_min", 60))
            age = ctx.get("last_loss_same_dir_age_min")
            rev = ((evidence.get("micro") or {}).get("reversal")
                   if (evidence.get("micro") or {}).get("available") else None)
            rev_sat = int(rev.get("satisfied") or 0) if rev else 0
            if age is None or age >= win:
                ev_txt = (f"同方向 {win:g} 分钟内无扫损记录，不属再入场场景"
                          if age is None else
                          f"上次同方向亏损已过 {age:.0f} 分钟（≥{win:g}），冷却充分")
            elif rev_sat >= REVERSAL_MIN_SCORE:
                ev_txt = (f"上次同方向亏损仅 {age:.0f} 分钟前，但反转四条件 "
                          f"{rev_sat}/4 达标——有新结构证据，允许再入场")
            else:
                status = "warn"
                ev_txt = (f"上次同方向亏损仅 {age:.0f} 分钟前（<{win:g}），且无新结构"
                          f"证据（反转四条件 {rev_sat}/4）——刚被扫损就同方向再入场，"
                          "是报复性交易高发区，等结构位收复或 123 第二步确认")
        elif rtype == "streak_hubris":
            need = int(p.get("win_streak", 3))
            streak = int(ctx.get("win_streak_today") or 0)
            base = ctx.get("avg_planned_risk_pct")
            principal = float(plan.get("principal") or 0)
            leverage = float(plan.get("leverage") or 0)
            if streak < need:
                ev_txt = f"当日连胜 {streak} 单（<{need}），未触发过度自信检查"
            elif principal <= 0 or leverage <= 0 or base is None:
                status = "skipped"
                ev_txt = (f"当日已连胜 {streak} 单，但缺本金/杠杆或历史基准样本(<3)，"
                          "仓位对比不判定")
            else:
                this_risk = leverage * float(risk.get("sl_dist_pct") or 0)
                if this_risk > base:
                    status = "warn"
                    ev_txt = (f"当日连胜 {streak} 单且本单预亏 {this_risk:.2f}% "
                              f"高于你的历史基准 {base:.2f}%——连胜后的过度自信"
                              "是连亏的前奏，建议仓位回归基准")
                else:
                    ev_txt = (f"当日连胜 {streak} 单，本单预亏 {this_risk:.2f}% "
                              f"未超历史基准 {base:.2f}%，仓位纪律保持")
        else:
            # 用户自定义文案军规：无自动判定逻辑，只展示提醒自查
            status = "pass"
            ev_txt = "自定义军规（不参与自动判定），提交前自行核对"
        out.append({"rule_id": rid, "title": title, "status": status,
                    "evidence": ev_txt})
    return out


def verdict(evidence: dict, plan: dict, rules: list[dict] | None = None,
            rules_ctx: dict | None = None) -> dict:
    """确定性裁决（纯函数）。plan 需含 direction/entry/stop_loss/take_profit，
    可选 emotion_score(1-5)/reason/principal/leverage。输出契约：
    {light, score, items, rules, summary, cooldown_min}。

    rules/rules_ctx（V1）：调用方加载启用的军规与台账上下文传入；
    None = 不评军规（向后兼容旧调用）。
    """
    plan_dir = plan.get("direction", "long")
    emotion = int(plan.get("emotion_score") or 3)

    risk_item, vetoes = _judge_risk(evidence, plan)
    items = [
        _judge_trend(evidence, plan_dir),
        risk_item,
        _judge_levels(evidence, plan),
        _judge_structure(evidence, plan_dir),
        _judge_micro(evidence, plan),
        _judge_event_risk(evidence),   # 任务 U：weight=0 仅明细警示，不动预登记权重
    ]
    # [任务 N2] 折价/溢价区提示（weight=0 纯人话证据，照任务 U 先例不动预登记
    # 权重；判定纯函数归 jarvis_fvg，动态 import 容错——缺失即跳过不占分母）
    try:
        import jarvis_fvg as _jfvg
        _pd = _jfvg.premium_discount_judge(
            (evidence.get("fvg") or {}).get("premium_discount"), plan_dir)
        if _pd:
            items.append(_item("premium_discount", _pd["level"], _pd["evidence"],
                               _pd["detail"], _pd.get("raw")))
    except Exception:  # noqa: BLE001 — 提示层缺失不拖裁决
        pass

    # 加权计分：unavailable 不计分母（证据不全不硬造分数，对齐 supply_demand 哲学）
    score_map = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    avail = [it for it in items if it["level"] != "unavailable"]
    got = sum(it["weight"] * score_map[it["level"]] for it in avail)
    denom = sum(it["weight"] for it in avail)
    score = round(got / denom * 100.0, 1) if denom else 0.0
    n_fail = sum(1 for it in avail if it["level"] == "fail")

    # 灯色（预登记顺序）
    if vetoes:
        light = "red"
    elif n_fail >= 2 or score < 40:
        light = "red"
    elif score >= 70 and n_fail == 0:
        light = "green"
    else:
        light = "yellow"
    coverage_note = None
    if light == "green" and denom < 50:
        light = "yellow"
        coverage_note = (f"可用证据权重仅 {denom}/100（多数引擎不可用）——"
                         "缺证据不等于没问题，最高只给黄灯")

    # V1 个人军规：R02/R03/R06 违反 → 最高黄灯；R06 另加长冷静期；其余违反仅展示
    cooldown_min = 0
    rules_out = evaluate_rules(evidence, plan, rules, rules_ctx) if rules else []
    rule_fails = [r for r in rules_out if r["status"] == "fail"]
    rules_note = None
    hard_hits = [r for r in rule_fails if r["rule_id"] in RULE_HARD_FAIL]
    if hard_hits and light == "green":
        light = "yellow"
    if rule_fails:
        rules_note = ("违反个人军规 " + "、".join(r["rule_id"] for r in rule_fails)
                      + (f"（{'、'.join(r['rule_id'] for r in hard_hits)} 触发降灯）"
                         if hard_hits else ""))
    for r in rule_fails:
        if r["rule_id"] == "R06":
            ext = 60
            for src in (rules or []):
                if src.get("rule_id") == "R06":
                    ext = int(_rule_params(src).get("extended_cooldown_min", 60))
            cooldown_min = max(cooldown_min, ext)

    # E1 情绪强制降档
    emotion_note = None
    t = evidence.get("trend") or {}
    cons_dir = (t.get("consensus") or {}).get("direction") if t.get("available") else None
    if emotion >= EMOTION_HOT and cons_dir == _OPP_OF_PLAN[plan_dir]:
        cooldown_min = max(cooldown_min, COOLDOWN_MIN)   # 不覆盖军规加长的冷静期
        emotion_note = (f"情绪自评 {emotion}/5 且计划与共识反向——上头时最容易做的就是"
                        f"逆势重仓。强制降档，建议冷静 {cooldown_min} 分钟后重新提交裁决")
        if light == "green":
            light = "yellow"

    dir_cn = _DIR_CN.get(plan_dir, plan_dir)
    head = {"green": f"绿灯（{score:.0f} 分）：这单{dir_cn}的证据结构成立，可以按计划执行",
            "yellow": f"黄灯（{score:.0f} 分）：这单{dir_cn}的证据还不足以放行，"
                      "建议缩仓一半或等待确认",
            "red": f"红灯（{score:.0f} 分）：证据不支持这单{dir_cn}，强烈建议放弃"}[light]
    reasons = []
    for it in items:
        if it["level"] == "fail":
            reasons.append("✗ " + it["evidence"])
    for v in vetoes:
        reasons.append("✗✗ " + v)
    if not reasons:
        reasons = ["✓ " + it["evidence"] for it in items if it["level"] == "pass"][:2]
    if rules_note:
        reasons.append("⚠ " + rules_note)
    if coverage_note:
        reasons.append("⚠ " + coverage_note)
    if emotion_note:
        reasons.append("⚠ " + emotion_note)
    summary = head + "。" + "；".join(reasons[:5])

    return {"light": light, "score": score, "items": items, "rules": rules_out,
            "summary": summary,
            "cooldown_min": cooldown_min, "vetoes": vetoes,
            "coverage_note": coverage_note, "rules_note": rules_note,
            "emotion_note": emotion_note, "as_of": time.time(),
            "prereg": {"weights": WEIGHTS, "rr_hard_min": RR_HARD_MIN,
                       "toll_hard_max": TOLL_HARD_MAX, "emotion_hot": EMOTION_HOT,
                       "cooldown_min": COOLDOWN_MIN,
                       "reversal_min_score": REVERSAL_MIN_SCORE,
                       "rule_hard_fail": list(RULE_HARD_FAIL)}}


# ═══════════════════════════ 计划台账（mentor_plan） ═══════════════════════════

def _conn():
    import jarvis_journal as jj
    return jj._conn()


def ensure_schema() -> None:
    """建 mentor_plan 表（幂等；jarvis_db 兼容层自动翻译 pg 方言）。"""
    import jarvis_journal as jj
    jj.init_db()
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mentor_plan (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts    REAL    NOT NULL,
                symbol        TEXT    NOT NULL,
                tf            TEXT,
                direction     TEXT    NOT NULL,
                entry         REAL    NOT NULL,
                stop_loss     REAL    NOT NULL,
                take_profit   REAL    NOT NULL,
                reason        TEXT,
                emotion_score INTEGER,
                light         TEXT,
                score         REAL,
                cooldown_until REAL,
                verdict_json  TEXT,
                status        TEXT    NOT NULL DEFAULT 'open',
                followed      INTEGER,
                result        TEXT,
                pnl_pct       REAL,
                note          TEXT,
                closed_ts     REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mentor_plan_sym "
                     "ON mentor_plan(symbol, created_ts)")
        # R2 升级列（幂等）：SQLite 重复加列抛错=已升级；jarvis_db 对 pg 自动
        # 翻译 ADD COLUMN IF NOT EXISTS。必须放在 CREATE TABLE 之后（同
        # twelve_trader 的教训：放前面全新库会静默缺列）。
        for _ddl in ("ALTER TABLE mentor_plan ADD COLUMN principal REAL",
                     "ALTER TABLE mentor_plan ADD COLUMN leverage REAL"):
            try:
                conn.execute(_ddl)
            except Exception:  # noqa: BLE001 — duplicate column = 已升级过
                pass
        # V1 个人军规表 + 默认 8 条 seed（不覆盖用户已改动的行，幂等）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mentor_rules (
                rule_id    TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                rtype      TEXT NOT NULL,
                params     TEXT,
                enabled    INTEGER NOT NULL DEFAULT 1,
                updated_ts REAL
            )
            """
        )
        for rid, title, rtype, params in DEFAULT_RULES:
            row = conn.execute("SELECT rule_id FROM mentor_rules WHERE rule_id = ?",
                               (rid,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO mentor_rules (rule_id, title, rtype, params, "
                    "enabled, updated_ts) VALUES (?,?,?,?,1,?)",
                    (rid, title, rtype, json.dumps(params, ensure_ascii=False),
                     time.time()))


def load_rules(enabled_only: bool = True) -> list[dict]:
    """读军规清单（params 反序列化为 dict）。"""
    ensure_schema()
    sql = "SELECT rule_id, title, rtype, params, enabled, updated_ts FROM mentor_rules"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY rule_id"
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    for r in rows:
        r["params"] = _rule_params(r)
        r["enabled"] = int(r.get("enabled") or 0)
    return rows


def upsert_rule(rule_id: str | None, *, title: str | None = None,
                rtype: str | None = None, params: dict | None = None,
                enabled: bool | None = None) -> dict:
    """增改军规：有 rule_id 且存在 → 局部更新；否则新增（缺 rule_id 自动生成 U-<ts>）。

    内置军规（R01-R08）只允许改 title/params/enabled，不允许改 rtype——
    判定语义由 rtype 锁定，改语义请新建自定义规则。
    """
    ensure_schema()
    now = time.time()
    with _conn() as conn:
        row = (conn.execute("SELECT rule_id, rtype FROM mentor_rules WHERE rule_id = ?",
                            (rule_id,)).fetchone() if rule_id else None)
        if row is not None:
            builtin = str(row["rule_id"]).startswith("R0")
            sets, vals = [], []
            if title is not None:
                sets.append("title = ?")
                vals.append(title)
            if params is not None:
                sets.append("params = ?")
                vals.append(json.dumps(params, ensure_ascii=False))
            if enabled is not None:
                sets.append("enabled = ?")
                vals.append(int(enabled))
            if rtype is not None and not builtin:
                sets.append("rtype = ?")
                vals.append(rtype)
            if not sets:
                return {"ok": False, "error": "没有可更新的字段"}
            sets.append("updated_ts = ?")
            vals.append(now)
            vals.append(rule_id)
            conn.execute(f"UPDATE mentor_rules SET {', '.join(sets)} WHERE rule_id = ?",
                         tuple(vals))
            return {"ok": True, "rule_id": rule_id, "created": False}
        rid = rule_id or f"U-{int(now)}"
        conn.execute(
            "INSERT INTO mentor_rules (rule_id, title, rtype, params, enabled, updated_ts) "
            "VALUES (?,?,?,?,?,?)",
            (rid, title or rid, rtype or "custom",
             json.dumps(params or {}, ensure_ascii=False),
             1 if enabled is None else int(enabled), now))
        return {"ok": True, "rule_id": rid, "created": True}


def _day_start_utc8(now: float | None = None) -> float:
    """当日（UTC+8 自然日）零点的 epoch 秒。"""
    now = time.time() if now is None else now
    return (int((now + 8 * 3600) // 86400)) * 86400.0 - 8 * 3600.0


def _rules_context(symbol: str, direction: str | None = None,
                   now: float | None = None) -> dict:
    """军规判定所需的台账上下文（R01/R05/R06 + V3 的 R09/R11/R12）。"""
    ensure_schema()
    now = time.time() if now is None else now
    day0 = _day_start_utc8(now)
    hour8 = int(((now + 8 * 3600) % 86400) // 3600)
    with _conn() as conn:
        today = [dict(r) for r in conn.execute(
            "SELECT status, result, closed_ts FROM mentor_plan "
            "WHERE symbol = ? AND created_ts >= ?", (symbol.upper(), day0)).fetchall()]
        last_red = conn.execute(
            "SELECT created_ts FROM mentor_plan WHERE symbol = ? AND light = 'red' "
            "ORDER BY created_ts DESC LIMIT 1", (symbol.upper(),)).fetchone()
        # R09：当前 UTC+8 小时所在 6h 时段的全量真实战绩（跨 symbol，行为属性；
        # 时段过滤在 Python 侧做，created_ts 需换算 UTC+8）
        sess_lo = hour8 // 6 * 6
        sess_rows = [dict(r) for r in conn.execute(
            "SELECT created_ts, result FROM mentor_plan WHERE status = 'closed' "
            "AND result IN ('win','loss','breakeven')").fetchall()]
        # R11：同 symbol 同方向最近一条 loss 平仓
        last_loss_row = (conn.execute(
            "SELECT closed_ts FROM mentor_plan WHERE symbol = ? AND direction = ? "
            "AND status = 'closed' AND result = 'loss' "
            "ORDER BY closed_ts DESC LIMIT 1",
            (symbol.upper(), direction)).fetchone() if direction else None)
        # R12：历史预亏基准（有本金/杠杆的单，n≥3 才给均值）
        risk_rows = [dict(r) for r in conn.execute(
            "SELECT entry, stop_loss, principal, leverage FROM mentor_plan "
            "WHERE principal IS NOT NULL AND leverage IS NOT NULL "
            "AND principal > 0 AND leverage > 0").fetchall()]
    graded = sorted((r for r in today if r["status"] == "closed"
                     and r.get("result") in ("win", "loss", "breakeven")),
                    key=lambda r: float(r.get("closed_ts") or 0))
    loss_streak = win_streak = 0
    for r in reversed(graded):
        if r["result"] == "loss" and win_streak == 0:
            loss_streak += 1
        elif r["result"] == "win" and loss_streak == 0:
            win_streak += 1
        else:
            break
    sess_graded = [r for r in sess_rows
                   if sess_lo <= int(((float(r["created_ts"]) + 8 * 3600) % 86400)
                                     // 3600) < sess_lo + 6]
    sess_stats = None
    if len(sess_graded) >= MIN_STAT_N:
        wins = sum(1 for r in sess_graded if r["result"] == "win")
        sess_stats = {"n": len(sess_graded),
                      "win_rate": round(wins / len(sess_graded), 4)}
    risk_pcts = [float(r["leverage"]) * abs(float(r["entry"]) - float(r["stop_loss"]))
                 / float(r["entry"]) * 100.0
                 for r in risk_rows if float(r.get("entry") or 0) > 0]
    return {"today_submitted": len(today), "loss_streak_today": loss_streak,
            "win_streak_today": win_streak,
            "last_red_age_min": ((now - float(last_red["created_ts"])) / 60.0
                                 if last_red else None),
            "now_hour_utc8": hour8,
            "session_stats": sess_stats,
            "last_loss_same_dir_age_min": (
                (now - float(last_loss_row["closed_ts"])) / 60.0
                if last_loss_row is not None and last_loss_row["closed_ts"] is not None
                else None),
            "avg_planned_risk_pct": (round(sum(risk_pcts) / len(risk_pcts), 4)
                                     if len(risk_pcts) >= 3 else None)}


def save_plan(plan: dict, vd: dict) -> int:
    """计划 + 裁决落库，返回 plan_id。"""
    ensure_schema()
    now = time.time()
    cooldown_until = now + vd["cooldown_min"] * 60 if vd.get("cooldown_min") else None
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO mentor_plan
              (created_ts, symbol, tf, direction, entry, stop_loss, take_profit,
               reason, emotion_score, light, score, cooldown_until, verdict_json,
               status, principal, leverage)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (now, plan["symbol"].upper(), plan.get("tf"), plan["direction"],
             float(plan["entry"]), float(plan["stop_loss"]), float(plan["take_profit"]),
             plan.get("reason"), int(plan.get("emotion_score") or 3),
             vd["light"], float(vd["score"]), cooldown_until,
             json.dumps(vd, ensure_ascii=False, default=str), "open",
             float(plan["principal"]) if plan.get("principal") else None,
             float(plan["leverage"]) if plan.get("leverage") else None))
        rid = cur.lastrowid
    return int(rid or 0)


def list_plans(symbol: str | None = None, days: int = 30, limit: int = 200) -> list[dict]:
    ensure_schema()
    since = time.time() - days * 86400.0
    sql = ("SELECT id, created_ts, symbol, tf, direction, entry, stop_loss, take_profit, "
           "reason, emotion_score, light, score, cooldown_until, status, followed, "
           "result, pnl_pct, note, closed_ts, principal, leverage "
           "FROM mentor_plan WHERE created_ts >= ?")
    params: list = [since]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol.upper())
    sql += " ORDER BY created_ts DESC LIMIT ?"
    params.append(int(limit))
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    return rows


def get_plan(plan_id: int) -> dict | None:
    ensure_schema()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM mentor_plan WHERE id = ?",
                           (int(plan_id),)).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["verdict"] = json.loads(out.pop("verdict_json") or "null")
    except Exception:  # noqa: BLE001
        out["verdict"] = None
    return out


def set_outcome(plan_id: int, *, result: str, pnl_pct: float | None = None,
                followed: bool | None = None, note: str = "") -> bool:
    """事后回填：result=win/loss/breakeven/skipped；followed=是否听从了裁决建议。

    这是信任回路的数据来源——「听劝 vs 不听劝的盈亏对比」全靠诚实回填。
    """
    ensure_schema()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE mentor_plan SET result=?, pnl_pct=?, followed=?, note=?, "
            "closed_ts=?, status='closed' WHERE id=?",
            (result, pnl_pct, None if followed is None else int(followed),
             note, time.time(), int(plan_id)))
        # rowcount=0 → 计划不存在（诚实报未找到，不静默成功）
        return bool(cur.rowcount)


MIN_STAT_N = 5   # V1：行为统计的样本充分性门槛（n<5 标不可判定）


def stats(days: int = 90) -> dict:
    """信任回路统计：红/黄/绿各自胜率 + 听劝 vs 不听劝盈亏对比。

    V1 行为维度扩展：
    - today：当日（UTC+8）提交数 / 执行数（closed 且有胜负）/ 当前连亏 streak
    - by_session：UTC+8 四时段（凌晨 00-06 / 早 06-12 / 午 12-18 / 晚 18-24）胜率
    - by_emotion：情绪自评 ≥4（上头单）vs ≤3（冷静单）盈亏对比
    样本 < MIN_STAT_N 的桶标 insufficient=true（不可判定，win_rate/avg 不给数字）。
    """
    ensure_schema()
    now = time.time()
    since = now - days * 86400.0
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT light, status, followed, result, pnl_pct, created_ts, "
            "closed_ts, emotion_score FROM mentor_plan "
            "WHERE created_ts >= ?", (since,)).fetchall()]

    def _graded(sel: list[dict]) -> list[dict]:
        return [r for r in sel if r["status"] == "closed"
                and r.get("result") in ("win", "loss", "breakeven")]

    def _bucket(sel: list[dict], *, min_n: int = 0) -> dict:
        closed = [r for r in sel if r["status"] == "closed" and r.get("result")]
        graded = _graded(sel)
        wins = sum(1 for r in graded if r["result"] == "win")
        pnls = [float(r["pnl_pct"]) for r in graded if r.get("pnl_pct") is not None]
        out = {"n": len(sel), "closed": len(closed),
               "win_rate": round(wins / len(graded), 4) if graded else None,
               "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else None}
        if min_n and len(graded) < min_n:
            out.update({"insufficient": True, "win_rate": None, "avg_pnl_pct": None,
                        "note": f"样本不足（已定胜负 {len(graded)} < {min_n}），不可判定"})
        return out

    by_light = {lt: _bucket([r for r in rows if r["light"] == lt])
                for lt in ("green", "yellow", "red")}
    followed = _bucket([r for r in rows if r.get("followed") == 1])
    ignored = _bucket([r for r in rows if r.get("followed") == 0])
    note = None
    f_avg, i_avg = followed.get("avg_pnl_pct"), ignored.get("avg_pnl_pct")
    if f_avg is not None and i_avg is not None:
        note = (f"听劝平均 {f_avg:+.2f}% vs 不听劝 {i_avg:+.2f}%——"
                + ("导师建议在你自己的台账上是赚钱的" if f_avg > i_avg
                   else "样本尚未体现优势，继续积累"))

    # V1 · 当日行为（UTC+8）
    day0 = _day_start_utc8(now)
    today_rows = [r for r in rows if float(r["created_ts"]) >= day0]
    today_graded = sorted(_graded(today_rows),
                          key=lambda r: float(r.get("closed_ts") or 0))
    streak = 0
    for r in reversed(today_graded):
        if r["result"] == "loss":
            streak += 1
        else:
            break
    today = {"submitted": len(today_rows), "executed": len(today_graded),
             "loss_streak": streak}

    # V1 · 分时段胜率（UTC+8 四段——看清自己哪个时段最容易亏）
    session_def = (("凌晨(00-06)", 0, 6), ("早盘(06-12)", 6, 12),
                   ("午后(12-18)", 12, 18), ("晚间(18-24)", 18, 24))
    by_session = {}
    for name, lo, hi in session_def:
        sel = [r for r in rows
               if lo <= int(((float(r["created_ts"]) + 8 * 3600) % 86400) // 3600) < hi]
        by_session[name] = _bucket(sel, min_n=MIN_STAT_N)

    # V1 · 情绪对比（≥4 上头单 vs ≤3 冷静单）
    hot = [r for r in rows if int(r.get("emotion_score") or 3) >= EMOTION_HOT]
    calm = [r for r in rows if int(r.get("emotion_score") or 3) < EMOTION_HOT]
    by_emotion = {"hot_ge4": _bucket(hot, min_n=MIN_STAT_N),
                  "calm_le3": _bucket(calm, min_n=MIN_STAT_N)}
    h_avg = by_emotion["hot_ge4"].get("avg_pnl_pct")
    c_avg = by_emotion["calm_le3"].get("avg_pnl_pct")
    emotion_note = None
    if h_avg is not None and c_avg is not None:
        emotion_note = (f"上头单（情绪≥4）平均 {h_avg:+.2f}% vs 冷静单 {c_avg:+.2f}%——"
                        + ("数据证明你上头时更亏，冷静期规则值得遵守"
                           if h_avg < c_avg else "当前样本未见情绪劣化，继续观察"))

    return {"days": days, "total": len(rows), "by_light": by_light,
            "followed": followed, "ignored": ignored, "note": note,
            "today": today, "by_session": by_session, "by_emotion": by_emotion,
            "emotion_note": emotion_note, "min_stat_n": MIN_STAT_N}


# ═══════════════════════════ CLI ═══════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description="交易导师·证据引擎与裁决核心（只读证据+确定性裁决）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="对一个交易计划出具裁决")
    p.add_argument("--symbol", default="ETHUSDT")
    p.add_argument("--direction", choices=("long", "short"), required=True)
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--sl", type=float, required=True)
    p.add_argument("--tp", type=float, required=True)
    p.add_argument("--tf", default="30m")
    p.add_argument("--emotion", type=int, default=3, help="情绪自评 1-5（≥4=上头）")
    p.add_argument("--reason", default="", help="下单理由（一句话）")
    p.add_argument("--principal", type=float, default=None, help="本金 USDT（可选）")
    p.add_argument("--leverage", type=float, default=None, help="杠杆倍数（可选）")
    p.add_argument("--save", action="store_true", help="裁决后落台账")
    args = ap.parse_args()

    if args.cmd == "check":
        plan = {"symbol": args.symbol.upper(), "tf": args.tf, "direction": args.direction,
                "entry": args.entry, "stop_loss": args.sl, "take_profit": args.tp,
                "emotion_score": args.emotion, "reason": args.reason,
                "principal": args.principal, "leverage": args.leverage}
        ev = build_evidence(args.symbol.upper(), args.direction, args.entry,
                            args.sl, args.tp, tf=args.tf)
        try:
            rules, ctx = load_rules(), _rules_context(args.symbol.upper(), args.direction)
        except Exception:  # noqa: BLE001 — 台账不可用时退回无军规裁决
            rules, ctx = None, None
        vd = verdict(ev, plan, rules=rules, rules_ctx=ctx)
        print(json.dumps({k: vd[k] for k in ("light", "score", "summary", "cooldown_min")},
                         ensure_ascii=False, indent=2))
        for it in vd["items"]:
            print(f"  [{it['level']:>11}] {it['key']:<9} w={it['weight']:>2}  {it['evidence']}")
        for r in vd.get("rules", []):
            print(f"  [{r['status']:>11}] {r['rule_id']:<9} 军规  {r['title']}：{r['evidence']}")
        if args.save:
            pid = save_plan(plan, vd)
            print(f"已落台账 mentor_plan id={pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
