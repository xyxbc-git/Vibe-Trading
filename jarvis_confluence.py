#!/usr/bin/env python3
"""贾维斯 JARVIS — 盘上实时合流仪表后端（Confluence HUD，方案 20260813 C-1）。

回答一个问题：**当前 symbol×TF 的环境合流度值得看吗？**（事前扫描视图）

与导师/军规的关系（《贾维斯-合流仪表-方案-20260813.md》§〇）：
  HUD 分 = **环境合流分**（setup quality，无计划无点位）；
  导师分 = 计划裁决分（plan quality，有 entry/SL/TP 与军规逐条）。
  两者共享证据源但**不共享分数**；HUD 点击预填导师计划表单进完整裁决。

────────────────────────── 预登记（跑数之前锁定） ──────────────────────────
权重（合计 100；unavailable 条目不计分母，按可用权重归一）：
  方向合流 40 = 30m 同向 10 + 1h 同向 10 + 4h 同向 10 + 5m 时机 5 + 威科夫语境 5
  结构证据 30 = BOS 结构 10 + 流动性扫单 10 + FVG/折溢价 10
  微观确认 20 = Delta 安全带 12 + 反转四条件 8
  环境健康 10 = 事件窗口外 5 + 资金费不极端 5

方向判定：多周期加权共识方向（consensus_multi_tf）。neutral 时方向合流组
  各条目一律 warn（「无方向共识」），其余组照常判——分数照算，前端置灰。

条目状态映射分数：pass=1.0 / warn=0.5 / fail=0.0 / skipped=不计分母。
响应字段名与前端契约锁定（desktop/src/api/confluence.ts，任务 C3 对齐）：
  条目键 c1_htf（30m/1h/4h 合并）/c2_ltf/c1_wyckoff_ctx/c3_bos/c4_sweep/
  c5_fvg/c6_delta/c7_reversal/c8_event/c9_funding；折叠四勾由前端从 items
  抽 c1_htf/c3_bos/c4_sweep/c5_fvg；direction=neutral 时 score=null。

纪律（主控四裁决落地）：
  D4 演示/mock/降级数据一律 unavailable，绝不进分（分数是决策用的）；
  行为与成本不进分：冷静期/当日单数走 action_gate 字段（行动闸口），
  fee/R 估算走环境组徽章行（评分外挂 cost_flag，>1.0 强制红标）；
  跨模块边界（主控补充裁决）：本模块只输出环境方向与合流分；引用的
  盘口/Delta 证据行保留其行为描述原句，与 HUD 方向矛盾时标 conflict
  计 warn/fail，绝不静默丢弃。
  滞回（<5 分不重绘）由前端处理，本模块返回原始分。

零新增出网：全部读各模块既有缓存/降级链（consensus 180s / wyckoff 指纹 /
delta TTL / stop_hunt kline TTL / fvg kline 缓存 / sentiment intel TTL /
event disk TTL / 军规 ctx 台账查询）。二期扩展位：盘口成交流画像
（agent-5 B-P0 契约 {action, confidence, ...}），本期恒 unavailable。

用法（CLI 调试）：python3 jarvis_confluence.py ETHUSDT --tf 30m
────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import json
import sys
import time

CONF_WEIGHTS = {"direction": 40, "structure": 30, "micro": 20, "environment": 10}
# 条目键名与前端契约锁定（desktop/src/api/confluence.ts）：折叠态四勾从 items
# 抽 c1_htf/c3_bos/c4_sweep/c5_fvg 四键——HTF 三周期合并为单条目 c1_htf(30)。
ITEM_WEIGHTS = {
    "c1_htf": 30, "c2_ltf": 5, "c1_wyckoff_ctx": 5,
    "c3_bos": 10, "c4_sweep": 10, "c5_fvg": 10,
    "c6_delta": 12, "c7_reversal": 8,
    "c8_event": 5, "c9_funding": 5,
}
ITEM_NAMES = {
    "c1_htf": "多周期", "c2_ltf": "入场时机", "c1_wyckoff_ctx": "阶段语境",
    "c3_bos": "结构突破", "c4_sweep": "流动性扫单", "c5_fvg": "FVG 折溢价",
    "c6_delta": "Delta", "c7_reversal": "反转四条件",
    "c8_event": "事件窗口", "c9_funding": "资金费",
}
GROUP_OF = {
    "c1_htf": "direction", "c2_ltf": "direction", "c1_wyckoff_ctx": "direction",
    "c3_bos": "structure", "c4_sweep": "structure", "c5_fvg": "structure",
    "c6_delta": "micro", "c7_reversal": "micro",
    "c8_event": "environment", "c9_funding": "environment",
}
GROUP_NAMES = {"direction": "方向合流", "structure": "结构证据",
               "micro": "微观确认", "environment": "环境健康"}
SCORE_MAP = {"pass": 1.0, "warn": 0.5, "fail": 0.0}   # skipped 不计分母
FUND_EXTREME_PCT = 0.05        # %/8h，对齐军规 R10 默认档
TOLL_WARN, TOLL_HARD = 0.20, 1.0   # 成本徽章档位，对齐引擎 T3 与军规 R02/红线 R2
REVERSAL_GOOD = 3              # 反转四条件加分门槛（对齐导师 REVERSAL_MIN_SCORE）

_DIR_CN = {"bullish": "偏多", "bearish": "偏空", "neutral": "无方向共识"}


# ═══════════════════════════ 取数层（全读既有缓存，零新增出网） ═══════════════════════════

def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:  # noqa: BLE001 — 任一路失败即降级，不拖垮 HUD
        return None


def gather(symbol: str, tf: str = "30m", *, consensus_provider=None) -> dict:
    """聚合各路证据源（每路独立容错；demo/mock 数据一律不产生——D4）。"""
    sym = symbol.upper()
    bundle: dict = {"symbol": sym, "tf": tf, "as_of": time.time()}

    # 1. 多周期共识（direction 的唯一来源；provider = dashboard 进程内缓存直读）
    cons = None
    if consensus_provider is not None:
        data = _safe(consensus_provider) or {}
        cons = data.get("consensus")
    if cons is None:
        def _direct():
            import jarvis_twelve_systems as jts
            tf_cons = {}
            for t in ("5m", "30m", "1h", "4h"):
                df = jts.fetch_klines_df(sym, t, 300)
                if df is not None and len(df) >= 30:
                    tf_cons[t] = jts.analyze(df)["consensus"]
            return jts.consensus_multi_tf(tf_cons) if tf_cons else None
        cons = _safe(_direct)
    bundle["consensus"] = cons

    # 2. 威科夫语境（1h）
    wk = _safe(lambda: __import__("jarvis_wyckoff").analyze(sym, "1h"))
    bundle["wyckoff"] = wk if (wk and wk.get("ok")) else None

    # 3. 扫单
    hunt = _safe(lambda: __import__("jarvis_stop_hunt").detect(sym, tf))
    bundle["hunt"] = hunt

    # 4. FVG + 折溢价（N2 契约：detect → {ok, zones, premium_discount}）
    def _fvg():
        import jarvis_fvg
        import jarvis_twelve_systems as jts
        df = jts.fetch_klines_df(sym, tf, 300)
        if df is None or len(df) < 30:
            return None
        out = jarvis_fvg.detect(df)
        return out if out.get("ok") else None
    bundle["fvg"] = _safe(_fvg)

    # 5. Delta → 安全带（方向依赖共识；共识缺失时仍取 payload 供 conflict 判读）
    def _delta():
        import jarvis_delta_flow as jdf
        got = jdf.analyze(sym, tf)
        return got if got.get("ok") else None
    bundle["delta"] = _safe(_delta)

    # 6. 反转四条件（delta + hunt，vp 缺省 None——条件自标 unavailable）
    bundle["reversal"] = _safe(
        lambda: __import__("jarvis_stop_hunt").aggregate_reversal_score(
            bundle["delta"], None, bundle["hunt"]))

    # 7. 情绪/资金费
    def _sent():
        import jarvis_sentiment as jst
        got = jst.assess(sym)
        if not got.get("ok"):
            return None
        funding = next((f.get("value") for f in (got.get("factors") or [])
                        if f.get("key") == "funding" and f.get("available")
                        and f.get("value") is not None), None)
        return {"funding": funding, "bias": got.get("bias")}
    bundle["sentiment"] = _safe(_sent)

    # 8. 事件窗口
    ev = _safe(lambda: __import__("jarvis_event_calendar").mentor_item(sym))
    bundle["event"] = ev if (ev and ev.get("available")) else None

    # 9. 行动闸口（行为不进分——冷静期/当日提交数，台账直读）
    def _gate():
        import jarvis_trade_mentor as jtm
        ctx = jtm._rules_context(sym)
        cooldown_until = None
        with jtm._conn() as conn:
            row = conn.execute(
                "SELECT MAX(cooldown_until) AS cu FROM mentor_plan "
                "WHERE symbol = ? AND cooldown_until > ?",
                (sym, time.time())).fetchone()
            if row is not None and row["cu"]:
                cooldown_until = float(row["cu"])
        return {"cooldown_until": cooldown_until,
                "today_count": int(ctx.get("today_submitted") or 0)}
    bundle["action_gate"] = _safe(_gate) or {"cooldown_until": None, "today_count": None}

    # 10. 成本徽章（评分外挂）：该 TF 的典型计划 R 下 fee/R 估算（advisor 分布）
    def _cost():
        import os
        import jarvis_config as jc
        fee = float(jc.get("twelve_sim_fee_pct") or 0.05)
        path = os.path.expanduser(f"~/.vibe-trading/tpsl_advisor/{sym}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # 该 TF 可判定格子的计划 R 中位（meta 里没有——用 cells 的 plan_tp_r 反推不可靠，
        # 直接读该 TF 全格子的 r_pct 中位不在 JSON 里；退而用 advisor 的 atr_pct_by_tf 近似）
        atr = ((data.get("meta") or {}).get("atr_pct_by_tf") or {}).get(tf)
        if not atr:
            return None
        # 典型计划 R ≈ 1×ATR（项目常规摆位量级），标「估算」
        toll = 2.0 * fee / float(atr)
        return {"typical_r_pct": round(float(atr), 4), "toll_ratio_est": round(toll, 3),
                "basis": "典型R≈1×ATR 估算口径"}
    bundle["cost"] = _safe(_cost)

    # 11. C11 格子战绩一句话（tpsl_advisor JSON；n<30 给「不可判定还差 k 笔」）
    def _grid():
        import os
        path = os.path.expanduser(f"~/.vibe-trading/tpsl_advisor/{sym}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            cells = [c for c in (json.load(f).get("cells") or [])
                     if c.get("tf") == tf]
        if not cells:
            return None
        ok_cells = [c for c in cells if c.get("verdict") == "OK"]
        if ok_cells:
            c = max(ok_cells, key=lambda x: x.get("n") or 0)
            mfe = (c.get("mfe_fix_r") or {}).get("p60")
            return (f"{c['system']}×{tf}（n={c['n']}）：行情 P60 给到 "
                    f"{mfe:.2f}R" + (f"，冤枉止损率 {c['unjust_sl_rate']:.0%}"
                                     if c.get("unjust_sl_rate") is not None else "")
                    + "（advisor 口径）")
        c = max(cells, key=lambda x: x.get("n") or 0)
        need = int(c.get("need_n") or 30) - int(c.get("n") or 0)
        return f"该格子 n<30，不可判定（最多样本 {c['system']} 还差 {need} 笔）"
    bundle["grid_stats"] = _safe(_grid)

    # 12. 二期扩展位：盘口成交流画像（agent-5 B-P0 契约 {action, confidence, ...}）
    bundle["orderflow_profile"] = None   # 本期恒 unavailable，接入时替换此行
    return bundle


# ═══════════════════════════ 评分层（纯函数，可离线冒烟） ═══════════════════════════

def _it(key: str, state: str, note: str, *, conflict: bool = False) -> dict:
    """条目（前端契约 ConfluenceItem）：state ∈ pass|warn|fail|skipped。"""
    out = {"key": key, "name": ITEM_NAMES[key], "group": GROUP_OF[key],
           "weight": ITEM_WEIGHTS[key], "state": state, "note": str(note)}
    if conflict:
        out["conflict"] = True
    return out


def _judge_items(bundle: dict) -> tuple[str, list[dict]]:
    """bundle → (HUD 方向, 10 条目判定)。全部人话字符串（R2 契约同源）。"""
    items: list[dict] = []
    cons = bundle.get("consensus")
    direction = (cons or {}).get("direction") or "neutral"
    want = direction if direction in ("bullish", "bearish") else None

    # ── 方向合流组 ──
    # c1_htf：30m/1h/4h 合并单条目（前端折叠四勾直接抽此键）：同向数 ≥2 pass / 1 warn / 0 fail
    tfs = (cons or {}).get("tfs") or {}
    htf_tfs = ("30m", "1h", "4h")
    if cons is None:
        items.append(_it("c1_htf", "skipped", "多周期共识不可用（取数降级中）"))
    else:
        visible = [(t, tfs[t]) for t in htf_tfs if tfs.get(t)]
        if not visible:
            items.append(_it("c1_htf", "skipped", "30m/1h/4h 周期无共识数据"))
        elif want is None:
            items.append(_it("c1_htf", "warn", "综合中性——30m/1h/4h 未形成方向共识"))
        else:
            agree = [t for t, c in visible if c.get("direction") == want]
            detail = "、".join(
                f"{t} {_DIR_CN.get(c.get('direction'), c.get('direction'))}"
                for t, c in visible)
            n = len(agree)
            state = "pass" if n >= 2 else ("warn" if n == 1 else "fail")
            items.append(_it("c1_htf", state,
                             f"{'/'.join(agree) if agree else '无周期'}与环境方向同向"
                             f"（{n}/{len(visible)}）：{detail}"))
    tf5 = tfs.get("5m")
    if cons is None or tf5 is None:
        items.append(_it("c2_ltf", "skipped", "5m 时机数据不可用"))
    elif want is None:
        items.append(_it("c2_ltf", "warn", "综合中性，5m 时机无从对照"))
    else:
        d5 = tf5.get("direction")
        items.append(_it("c2_ltf", "pass" if d5 == want else "warn",
                         f"5m 入场时机{'同向' if d5 == want else '不同向'}"
                         f"（{_DIR_CN.get(d5, d5)} {float(tf5.get('confidence') or 0):.0%}）"))
    wk = bundle.get("wyckoff")
    if wk is None:
        items.append(_it("c1_wyckoff_ctx", "skipped", "威科夫引擎不可用"))
    else:
        side = (wk.get("state") or {}).get("side")
        phase = (wk.get("state") or {}).get("phase")
        if want is None or side not in ("acc", "dist"):
            items.append(_it("c1_wyckoff_ctx", "warn",
                             f"1h 威科夫 {side or '无区间'}-{phase or '—'}，语境中性"))
        else:
            agree = (side == "acc") == (want == "bullish")
            items.append(_it("c1_wyckoff_ctx", "pass" if agree else "fail",
                             f"1h 威科夫 {side}-{phase} 段与环境方向"
                             f"{'一致' if agree else '相悖'}"))

    # ── 结构证据组 ──
    if wk is None:
        items.append(_it("c3_bos", "skipped", "结构引擎不可用"))
    else:
        events = wk.get("events") or []
        recent = events[-1] if events else None
        if recent:
            items.append(_it("c3_bos", "pass",
                             f"近期结构事件 {recent.get('type', '?')}"
                             f"（{recent.get('note') or '结构证据在场'}）"))
        else:
            items.append(_it("c3_bos", "warn", "区间内暂无 BOS/Spring/UT 级结构事件"))
    hunt = bundle.get("hunt")
    if hunt is None:
        items.append(_it("c4_sweep", "skipped", "扫单检测不可用（K线降级中）"))
    elif hunt.get("detected"):
        sweep_bull = hunt.get("side") == "long-stops-swept"
        if want is None:
            items.append(_it("c4_sweep", "warn",
                             str(hunt.get("note") or "检出扫单，方向待共识")))
        else:
            agree = sweep_bull == (want == "bullish")
            items.append(_it("c4_sweep", "pass" if agree else "fail",
                             str(hunt.get("note") or hunt.get("side"))
                             + ("" if agree else "——与环境方向相反"),
                             conflict=not agree))
    else:
        items.append(_it("c4_sweep", "warn", "近端无扫单信号（中性，不加分不扣分）"))
    fvg = bundle.get("fvg")
    if fvg is None:
        items.append(_it("c5_fvg", "skipped", "FVG/折溢价引擎不可用"))
    else:
        zones = [z for z in (fvg.get("zones") or []) if not z.get("mitigated")]
        pd = fvg.get("premium_discount") or {}
        zone_tag = pd.get("zone")   # premium / discount / equilibrium（N2 契约）
        mine = [z for z in zones if z.get("type") ==
                ("bullish" if want == "bullish" else "bearish")] if want else []
        parts = []
        state = "warn"
        if want and mine:
            parts.append(f"同向未回补 FVG {len(mine)} 个")
            state = "pass"
        elif want:
            parts.append("无同向未回补 FVG")
        if zone_tag:
            good_zone = ((zone_tag == "discount" and want == "bullish")
                         or (zone_tag == "premium" and want == "bearish"))
            parts.append(f"现价处于 {zone_tag} 区"
                         + ("（有利入场位）" if good_zone and want else ""))
            if want and good_zone and state != "pass":
                state = "pass"
        items.append(_it("c5_fvg", state, "；".join(parts) or "折溢价数据不足"))

    # ── 微观确认组（引用语义：保留行为描述原句，冲突显式标注——主控边界裁决）──
    delta = bundle.get("delta")
    if delta is None or want is None:
        items.append(_it("c6_delta", "skipped" if delta is None else "warn",
                         "Delta 引擎不可用" if delta is None
                         else "综合中性，Delta 无方向可确认"))
    else:
        try:
            import jarvis_seatbelt as jsb
            sb = jsb.evaluate(want, delta)
            st = sb.get("status")
            if st == "confirm":
                items.append(_it("c6_delta", "pass",
                                 f"Delta 同向确认（{sb.get('grade')}）"))
            elif st == "conflict":
                lvl = "fail" if sb.get("grade") == "strong" else "warn"
                items.append(_it("c6_delta", lvl,
                                 f"⚡冲突：{sb.get('note') or 'Delta 反向背离顶撞环境方向'}",
                                 conflict=True))
            else:
                items.append(_it("c6_delta", "warn", "Delta 中性（无背离证据）"))
        except Exception:  # noqa: BLE001
            items.append(_it("c6_delta", "skipped", "安全带判定层异常，已降级"))
    rev = bundle.get("reversal")
    if rev is None:
        items.append(_it("c7_reversal", "skipped", "反转四条件引擎不可用"))
    else:
        sat = int(rev.get("satisfied") or 0)
        st = "pass" if sat >= REVERSAL_GOOD else ("warn" if sat >= 2 else "fail")
        items.append(_it("c7_reversal", st,
                         f"反转四条件 {sat}/4（{rev.get('verdict', '—')}）"))

    # ── 环境健康组 ──
    ev = bundle.get("event")
    if ev is None:
        items.append(_it("c8_event", "skipped", "事件日历未配置/不可用"))
    elif ev.get("in_window"):
        items.append(_it("c8_event", "warn", str(ev.get("note") or "事件风险窗口内")))
    else:
        items.append(_it("c8_event", "pass", str(ev.get("note") or "不在高影响事件窗口")))
    snt = bundle.get("sentiment")
    funding = (snt or {}).get("funding")
    if snt is None or funding is None:
        items.append(_it("c9_funding", "skipped", "资金费数据不可用"))
    else:
        f_pct = float(funding) * 100.0
        if abs(f_pct) <= FUND_EXTREME_PCT:
            items.append(_it("c9_funding", "pass", f"8h 资金费 {f_pct:+.4f}% 温和不拥挤"))
        else:
            crowded = "bullish" if f_pct > 0 else "bearish"
            chasing = want == crowded
            items.append(_it("c9_funding", "warn" if chasing else "pass",
                             f"8h 资金费 {f_pct:+.4f}% 偏极端"
                             + ("——环境方向在追拥挤方" if chasing else "，但环境方向不在拥挤侧")))
    return direction, items


def score_confluence(bundle: dict) -> dict:
    """bundle → HUD 契约体（纯函数，字段名以 desktop/src/api/confluence.ts 为准）。

    分数按可用权重归一；skipped 不计分母；方向中性时 score=null（前端整体
    置灰）；行为/成本不进分（cooldownUntil/todayPlans/costWarning 独立字段）。
    """
    direction, items = _judge_items(bundle)
    groups_out = []
    total_earned = total_avail = 0.0
    for gkey in ("direction", "structure", "micro", "environment"):
        g_items = [it for it in items if it["group"] == gkey]
        avail = [it for it in g_items if it["state"] != "skipped"]
        earned = sum(it["weight"] * SCORE_MAP[it["state"]] for it in avail)
        avail_w = sum(it["weight"] for it in avail)
        total_earned += earned
        total_avail += avail_w
        groups_out.append({"key": gkey, "name": GROUP_NAMES[gkey],
                           "weight": CONF_WEIGHTS[gkey],
                           "earned": round(earned, 1), "available": avail_w,
                           "items": g_items})
    score = round(total_earned / total_avail * 100.0, 1) if total_avail else 0.0
    insufficient = total_avail < 50
    if direction == "neutral":
        score = None   # 前端契约：中性整体置灰「无方向共识」

    # 成本徽章（评分外挂）：costEstimate 恒给估算句；hard 档另给 costWarning 红标
    cost = bundle.get("cost")
    cost_estimate = cost_warning = None
    if cost and cost.get("toll_ratio_est") is not None:
        toll = float(cost["toll_ratio_est"])
        cost_estimate = (f"fee/R≈{toll:.2f}（典型 R≈1×ATR"
                         f"={cost.get('typical_r_pct')}% 估算）")
        if toll > TOLL_HARD:
            cost_warning = (f"该 TF 典型 R 下过路费占风险预算 {toll:.0%} > 100%"
                            "——成本不可行，任何方向都别开（估算口径）")
        elif toll > TOLL_WARN:
            cost_estimate += "，超过 20% 警戒档"

    gate = bundle.get("action_gate") or {}
    return {"ok": True, "symbol": bundle.get("symbol"), "tf": bundle.get("tf"),
            "direction": direction,
            "direction_cn": _DIR_CN.get(direction, direction),
            "score": score, "insufficient": insufficient,
            "availableWeight": round(total_avail, 1),
            "groups": groups_out,
            "costWarning": cost_warning, "costEstimate": cost_estimate,
            "cooldownUntil": gate.get("cooldown_until"),
            "todayPlans": gate.get("today_count"),
            "gridStats": bundle.get("grid_stats"),
            "updatedAt": bundle.get("as_of"),
            "orderflow_profile": bundle.get("orderflow_profile"),
            "prereg": {"weights": CONF_WEIGHTS, "item_weights": ITEM_WEIGHTS,
                       "fund_extreme_pct": FUND_EXTREME_PCT,
                       "reversal_good": REVERSAL_GOOD}}


def assess(symbol: str, tf: str = "30m", *, consensus_provider=None) -> dict:
    """取数 + 评分一步到位（/api/confluence 消费入口）。永不抛出。"""
    try:
        return score_confluence(gather(symbol, tf, consensus_provider=consensus_provider))
    except Exception as exc:  # noqa: BLE001 — HUD 层绝不拖垮 dashboard
        return {"ok": False, "symbol": symbol.upper(), "tf": tf,
                "error": repr(exc)[:200]}


def main() -> int:
    ap = argparse.ArgumentParser(description="盘上合流仪表（环境合流分，事前扫描）")
    ap.add_argument("symbol", nargs="?", default="ETHUSDT")
    ap.add_argument("--tf", default="30m")
    args = ap.parse_args()
    out = assess(args.symbol, args.tf)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
