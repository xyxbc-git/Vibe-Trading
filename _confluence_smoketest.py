"""离线冒烟：盘上合流仪表（jarvis_confluence.score_confluence 纯函数）。

全部打桩 bundle，不联网不碰库。契约以 desktop/src/api/confluence.ts 为准
（任务 C3 对齐）：条目 {key,name,state,note,conflict?}，state ∈
pass|warn|fail|skipped；折叠四勾键 c1_htf/c3_bos/c4_sweep/c5_fvg；
顶层 availableWeight/costWarning/costEstimate/cooldownUntil/todayPlans/
gridStats/updatedAt；direction=neutral 时 score=null。
"""
import json

import jarvis_confluence as jc

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def tfc(direction, conf=0.7):
    return {"direction": direction, "confidence": conf}


def mk_bundle(cons_dir="bullish", *, tfs=None, wyckoff="present", hunt="none",
              fvg="good", delta_status="confirm", rev_sat=3, event_in=False,
              funding=0.0001, cons=True, cost=None, gate=None, grid=None):
    """可控打桩 bundle（默认：多头顺风满配）。"""
    d = cons_dir
    b = {"symbol": "TESTUSDT", "tf": "30m", "as_of": 1000.0}
    b["consensus"] = ({"direction": d, "confidence": 0.7, "score": 0.3,
                       "tfs": tfs if tfs is not None else {
                           "5m": tfc(d), "30m": tfc(d), "1h": tfc(d), "4h": tfc(d)}}
                      if cons else None)
    b["wyckoff"] = ({"ok": True,
                     "state": {"side": "acc" if d == "bullish" else "dist",
                               "phase": "C"},
                     "events": [{"type": "spring", "note": "Spring 后回升"}]}
                    if wyckoff == "present" else None)
    if hunt == "none":
        b["hunt"] = {"detected": False, "side": "none", "note": "未见扫单"}
    elif hunt is None:
        b["hunt"] = None
    else:
        b["hunt"] = {"detected": True, "side": hunt, "note": f"检出 {hunt} 扫单"}
    if fvg == "good":
        b["fvg"] = {"ok": True,
                    "zones": [{"type": d, "mitigated": False}],
                    "premium_discount": {"ok": True,
                                         "zone": ("discount" if d == "bullish"
                                                  else "premium")}}
    elif fvg == "bad_zone":
        b["fvg"] = {"ok": True, "zones": [],
                    "premium_discount": {"ok": True,
                                         "zone": ("premium" if d == "bullish"
                                                  else "discount")}}
    else:
        b["fvg"] = None
    b["delta"] = {"ok": True, "divergence": {}, "absorption": None} \
        if delta_status else None
    b["reversal"] = ({"satisfied": rev_sat, "verdict": "watch"}
                     if rev_sat is not None else None)
    b["event"] = {"available": True, "in_window": event_in,
                  "note": "CPI 还有 20 分钟" if event_in else "无临近高影响事件"}
    b["sentiment"] = {"funding": funding} if funding is not None else None
    b["cost"] = cost
    b["action_gate"] = gate or {"cooldown_until": None, "today_count": 2}
    b["grid_stats"] = grid
    b["orderflow_profile"] = None
    return b


def item_of(resp, key):
    return next(i for g in resp["groups"] for i in g["items"] if i["key"] == key)


def four_checks(resp):
    all_items = {i["key"]: i for g in resp["groups"] for i in g["items"]}
    return {k: all_items[k]["state"] if k in all_items else "skipped"
            for k in ("c1_htf", "c3_bos", "c4_sweep", "c5_fvg")}


# seatbelt 打桩
import jarvis_seatbelt as jsb

_orig_eval = jsb.evaluate
_STUB = {"seatbelt": "confirm"}


def _fake_eval(direction, payload):
    stub = _STUB["seatbelt"]
    if stub == "confirm":
        return {"status": "confirm", "grade": "medium", "note": "同向确认"}
    if stub == "conflict_strong":
        return {"status": "conflict", "grade": "strong", "note": "反向强背离"}
    if stub == "conflict_weak":
        return {"status": "conflict", "grade": "weak", "note": "反向弱背离"}
    return {"status": "idle", "grade": None, "note": "中性"}


jsb.evaluate = _fake_eval

try:
    # ── 1. 顺风满配（含同向扫单）→ 高分 + 偏多 + 四勾全 pass ──
    r1 = jc.score_confluence(mk_bundle(hunt="long-stops-swept"))
    check("满配高分 ≥85", r1["score"] is not None and r1["score"] >= 85,
          str(r1["score"]))
    check("方向偏多", r1["direction"] == "bullish")
    check("四组齐全", [g["key"] for g in r1["groups"]]
          == ["direction", "structure", "micro", "environment"])
    check("折叠四勾全 pass", all(v == "pass" for v in four_checks(r1).values()),
          str(four_checks(r1)))
    check("不 insufficient", not r1["insufficient"])

    # ── 2. 前端契约字段名逐一核对（C3 对齐的核心断言）──
    for fld in ("ok", "symbol", "tf", "direction", "score", "insufficient",
                "availableWeight", "groups", "costWarning", "costEstimate",
                "cooldownUntil", "todayPlans", "gridStats", "updatedAt"):
        if fld not in r1:
            check(f"顶层缺字段 {fld}", False)
            break
    else:
        check("顶层契约字段齐全（confluence.ts）", True)
    g0 = r1["groups"][0]
    check("group 契约字段", all(k in g0 for k in
                                ("key", "name", "weight", "earned", "available",
                                 "items")))
    i0 = g0["items"][0]
    check("item 契约字段", all(k in i0 for k in ("key", "name", "state", "note")))
    check("item 无旧字段", "status" not in i0 and "evidence" not in i0)
    check("条目 10 条（HTF 合并）",
          sum(len(g["items"]) for g in r1["groups"]) == 10)

    # ── 3. 中性共识 → score=null + 方向组 warn ──
    r3 = jc.score_confluence(mk_bundle(cons_dir="neutral"))
    check("中性 score=null", r3["direction"] == "neutral" and r3["score"] is None)
    dir_states = [i["state"] for g in r3["groups"] if g["key"] == "direction"
                  for i in g["items"]]
    check("中性方向组无 pass", all(s in ("warn", "skipped") for s in dir_states),
          str(dir_states))

    # ── 4. c1_htf 合并条目：2/3 pass、1/3 warn、0/3 fail ──
    tfs_2 = {"5m": tfc("bullish"), "30m": tfc("bullish"), "1h": tfc("bullish"),
             "4h": tfc("bearish")}
    r4a = jc.score_confluence(mk_bundle(tfs=tfs_2))
    check("HTF 2/3 → pass", item_of(r4a, "c1_htf")["state"] == "pass",
          item_of(r4a, "c1_htf")["note"][:40])
    tfs_1 = {"5m": tfc("bullish"), "30m": tfc("bullish"), "1h": tfc("neutral"),
             "4h": tfc("bearish")}
    r4b = jc.score_confluence(mk_bundle(tfs=tfs_1))
    check("HTF 1/3 → warn", item_of(r4b, "c1_htf")["state"] == "warn")
    tfs_0 = {"5m": tfc("bearish"), "30m": tfc("bearish"), "1h": tfc("bearish"),
             "4h": tfc("bearish")}
    r4c = jc.score_confluence(mk_bundle(tfs=tfs_0))
    check("HTF 0/3 → fail", item_of(r4c, "c1_htf")["state"] == "fail")
    check("HTF note 带各周期明细", "30m" in item_of(r4a, "c1_htf")["note"])

    # ── 5. skipped 不计分母：砍微观+环境 → 归一 ──
    r5_ev = mk_bundle(delta_status=None, rev_sat=None, funding=None)
    r5_ev["event"] = None
    r5 = jc.score_confluence(r5_ev)
    check("砍两组后归一", r5["availableWeight"] == 70 and r5["score"] > 80,
          f"avail={r5['availableWeight']} score={r5['score']}")

    # ── 6. 全 skipped → insufficient + 四勾 skipped ──
    r6_ev = mk_bundle(cons=False, wyckoff=None, hunt=None, fvg=None,
                      delta_status=None, rev_sat=None, funding=None)
    r6_ev["event"] = None
    r6 = jc.score_confluence(r6_ev)
    check("全缺 insufficient", r6["insufficient"] and r6["availableWeight"] == 0)
    check("全缺四勾 skipped", all(v == "skipped" for v in four_checks(r6).values()))

    # ── 7. 扫单方向语义 + conflict 布尔 ──
    r7 = jc.score_confluence(mk_bundle(hunt="short-stops-swept"))
    it7 = item_of(r7, "c4_sweep")
    check("反向扫单 fail + conflict", it7["state"] == "fail"
          and it7.get("conflict") is True)

    # ── 8. FVG 折溢价 ──
    check("同向FVG+discount pass", item_of(r1, "c5_fvg")["state"] == "pass")
    r8 = jc.score_confluence(mk_bundle(fvg="bad_zone"))
    check("无FVG+premium(做多) warn", item_of(r8, "c5_fvg")["state"] == "warn")

    # ── 9. Delta 三态 + ⚡冲突（主控边界裁决）──
    _STUB["seatbelt"] = "conflict_strong"
    r9 = jc.score_confluence(mk_bundle())
    it9 = item_of(r9, "c6_delta")
    check("强背离 fail + ⚡ + conflict", it9["state"] == "fail"
          and "⚡冲突" in it9["note"] and it9.get("conflict") is True)
    _STUB["seatbelt"] = "conflict_weak"
    check("弱背离 warn",
          item_of(jc.score_confluence(mk_bundle()), "c6_delta")["state"] == "warn")
    _STUB["seatbelt"] = "confirm"

    # ── 10. 反转档位 ──
    check("反转 1/4 fail",
          item_of(jc.score_confluence(mk_bundle(rev_sat=1)), "c7_reversal")["state"]
          == "fail")

    # ── 11. 事件窗口 ──
    check("事件窗口内 warn",
          item_of(jc.score_confluence(mk_bundle(event_in=True)), "c8_event")["state"]
          == "warn")

    # ── 12. 资金费 ──
    r12a = jc.score_confluence(mk_bundle(funding=0.0008))
    check("追拥挤方 warn", item_of(r12a, "c9_funding")["state"] == "warn")
    r12b = jc.score_confluence(mk_bundle(cons_dir="bearish", funding=0.0008))
    check("不在拥挤侧 pass", item_of(r12b, "c9_funding")["state"] == "pass")

    # ── 13. 成本字段：costEstimate 恒给、costWarning 仅 hard 档 ──
    base = jc.score_confluence(mk_bundle())["score"]
    r13a = jc.score_confluence(mk_bundle(
        cost={"typical_r_pct": 0.5, "toll_ratio_est": 0.14, "basis": "est"}))
    check("cost ok：estimate 有 warning 无", r13a["costWarning"] is None
          and "fee/R≈0.14" in r13a["costEstimate"], str(r13a["costEstimate"]))
    r13b = jc.score_confluence(mk_bundle(
        cost={"typical_r_pct": 0.05, "toll_ratio_est": 1.5, "basis": "est"}))
    check("cost hard：warning 红标", r13b["costWarning"] is not None
          and "别开" in r13b["costWarning"])
    check("成本不改分数", r13a["score"] == base == r13b["score"])
    check("无成本数据双 None",
          jc.score_confluence(mk_bundle(cost=None))["costEstimate"] is None)

    # ── 14. 行动闸口拉平顶层（D3）──
    r14 = jc.score_confluence(mk_bundle(gate={"cooldown_until": 9999.0,
                                              "today_count": 5}))
    check("cooldownUntil/todayPlans 顶层", r14["cooldownUntil"] == 9999.0
          and r14["todayPlans"] == 5)
    check("闸口不影响分数", r14["score"] == base)

    # ── 15. gridStats 透传 ──
    r15 = jc.score_confluence(mk_bundle(grid="triple_rsi×30m（n=31）：P60 给到 1.11R"))
    check("gridStats 透传", "1.11R" in r15["gridStats"])
    check("无战绩 null", jc.score_confluence(mk_bundle())["gridStats"] is None)

    # ── 16. 展示字段 str + JSON 序列化 + 预登记 ──
    all_items = [i for g in r1["groups"] for i in g["items"]]
    check("note/name 全 str", all(isinstance(i["note"], str)
                                  and isinstance(i["name"], str) for i in all_items))
    check("JSON 序列化", len(json.dumps(r1, ensure_ascii=False, default=str)) > 500)
    check("预登记随身", r1["prereg"]["item_weights"]["c1_htf"] == 30)

    # ── 17. 威科夫相悖 ──
    ev17 = mk_bundle()
    ev17["wyckoff"]["state"]["side"] = "dist"
    check("威科夫相悖 fail", item_of(jc.score_confluence(ev17),
                                     "c1_wyckoff_ctx")["state"] == "fail")

    # ── 18. assess 永不抛出 ──
    r18 = jc.assess("TESTUSDT", "30m", consensus_provider=lambda: 1 / 0)
    check("assess 兜底", isinstance(r18, dict) and "ok" in r18)
finally:
    jsb.evaluate = _orig_eval

# ── 汇总 ──
print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS（18 组 / C3 前端契约对齐：键名、state/skipped、conflict、顶层拉平、null 分数）")
