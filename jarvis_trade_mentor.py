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
  6. E1 命中且结果优于黄 → 降为 yellow
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
        out["sentiment"] = ({"score": got.get("score"), "bias": got.get("bias"),
                             "warnings": got.get("warnings") or []}
                            if got.get("ok") else None)
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
        gaps = jarvis_fvg.detect(df)
        return _ev(True, gaps=[g for g in (gaps or []) if not g.get("mitigated")][:6])
    except Exception as exc:  # noqa: BLE001
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


def verdict(evidence: dict, plan: dict) -> dict:
    """确定性裁决（纯函数）。plan 需含 direction/entry/stop_loss/take_profit，
    可选 emotion_score(1-5)/reason。输出契约见任务书：
    {light, score, items, summary, cooldown_min}。"""
    plan_dir = plan.get("direction", "long")
    emotion = int(plan.get("emotion_score") or 3)

    risk_item, vetoes = _judge_risk(evidence, plan)
    items = [
        _judge_trend(evidence, plan_dir),
        risk_item,
        _judge_levels(evidence, plan),
        _judge_structure(evidence, plan_dir),
        _judge_micro(evidence, plan),
    ]

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

    # E1 情绪强制降档
    cooldown_min = 0
    emotion_note = None
    t = evidence.get("trend") or {}
    cons_dir = (t.get("consensus") or {}).get("direction") if t.get("available") else None
    if emotion >= EMOTION_HOT and cons_dir == _OPP_OF_PLAN[plan_dir]:
        cooldown_min = COOLDOWN_MIN
        emotion_note = (f"情绪自评 {emotion}/5 且计划与共识反向——上头时最容易做的就是"
                        f"逆势重仓。强制降档，建议冷静 {COOLDOWN_MIN} 分钟后重新提交裁决")
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
    if coverage_note:
        reasons.append("⚠ " + coverage_note)
    if emotion_note:
        reasons.append("⚠ " + emotion_note)
    summary = head + "。" + "；".join(reasons[:4])

    return {"light": light, "score": score, "items": items, "summary": summary,
            "cooldown_min": cooldown_min, "vetoes": vetoes,
            "coverage_note": coverage_note,
            "emotion_note": emotion_note, "as_of": time.time(),
            "prereg": {"weights": WEIGHTS, "rr_hard_min": RR_HARD_MIN,
                       "toll_hard_max": TOLL_HARD_MAX, "emotion_hot": EMOTION_HOT,
                       "cooldown_min": COOLDOWN_MIN,
                       "reversal_min_score": REVERSAL_MIN_SCORE}}


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


def stats(days: int = 90) -> dict:
    """信任回路统计：红/黄/绿各自胜率 + 听劝 vs 不听劝盈亏对比。"""
    ensure_schema()
    since = time.time() - days * 86400.0
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT light, status, followed, result, pnl_pct FROM mentor_plan "
            "WHERE created_ts >= ?", (since,)).fetchall()]

    def _bucket(sel: list[dict]) -> dict:
        closed = [r for r in sel if r["status"] == "closed" and r.get("result")]
        graded = [r for r in closed if r["result"] in ("win", "loss", "breakeven")]
        wins = sum(1 for r in graded if r["result"] == "win")
        pnls = [float(r["pnl_pct"]) for r in graded if r.get("pnl_pct") is not None]
        return {"n": len(sel), "closed": len(closed),
                "win_rate": round(wins / len(graded), 4) if graded else None,
                "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else None}

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
    return {"days": days, "total": len(rows), "by_light": by_light,
            "followed": followed, "ignored": ignored, "note": note}


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
        vd = verdict(ev, plan)
        print(json.dumps({k: vd[k] for k in ("light", "score", "summary", "cooldown_min")},
                         ensure_ascii=False, indent=2))
        for it in vd["items"]:
            print(f"  [{it['level']:>11}] {it['key']:<9} w={it['weight']:>2}  {it['evidence']}")
        if args.save:
            pid = save_plan(plan, vd)
            print(f"已落台账 mentor_plan id={pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
