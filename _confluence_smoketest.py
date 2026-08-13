"""离线冒烟：盘上合流仪表（jarvis_confluence.score_confluence 纯函数）。

全部打桩 bundle，不联网不碰库。覆盖：评分归一/方向判定/12 条目三态/CP1 四勾
映射/unavailable 不计分母/覆盖不足标记/冲突显式态/成本徽章三档/行动闸口透传/
展示字段字符串契约/预登记随身。
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
              funding=0.0001, cons=True, cost=None, gate=None):
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
        b["hunt"] = {"detected": True, "side": hunt,
                     "note": f"检出 {hunt} 扫单"}
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
    b["_seatbelt_stub"] = delta_status
    b["reversal"] = ({"satisfied": rev_sat, "verdict": "watch"}
                     if rev_sat is not None else None)
    b["event"] = {"available": True, "in_window": event_in,
                  "note": "CPI 还有 20 分钟" if event_in else "无临近高影响事件"}
    b["sentiment"] = {"funding": funding} if funding is not None else None
    b["cost"] = cost
    b["action_gate"] = gate or {"cooldown_until": None, "today_count": 2}
    b["orderflow_profile"] = None
    return b


# seatbelt 打桩：jc._judge_items 内部 import jarvis_seatbelt——monkeypatch evaluate
import jarvis_seatbelt as jsb

_orig_eval = jsb.evaluate


def _fake_eval(direction, payload):
    stub = _STUB.get("seatbelt", "confirm")
    if stub == "confirm":
        return {"status": "confirm", "grade": "medium", "note": "同向确认"}
    if stub == "conflict_strong":
        return {"status": "conflict", "grade": "strong", "note": "反向强背离"}
    if stub == "conflict_weak":
        return {"status": "conflict", "grade": "weak", "note": "反向弱背离"}
    return {"status": "idle", "grade": None, "note": "中性"}


_STUB = {"seatbelt": "confirm"}
jsb.evaluate = _fake_eval

try:
    # ── 1. 顺风满配（含同向扫单）→ 高分 + 偏多 + 四勾全 pass ──
    r1 = jc.score_confluence(mk_bundle(hunt="long-stops-swept"))
    check("满配高分 ≥85", r1["score"] >= 85, str(r1["score"]))
    check("方向偏多", r1["direction"] == "bullish" and r1["direction_cn"] == "偏多")
    check("四组齐全", [g["key"] for g in r1["groups"]]
          == ["direction", "structure", "micro", "environment"])
    check("CP1 四勾全 pass", all(v == "pass" for v in r1["cp1_align"].values()),
          str(r1["cp1_align"]))
    check("不 insufficient", not r1["insufficient"])

    # ── 2. 中性共识 → 方向组全 warn + direction=neutral ──
    r2 = jc.score_confluence(mk_bundle(cons_dir="neutral"))
    check("中性方向", r2["direction"] == "neutral")
    dir_items = next(g for g in r2["groups"] if g["key"] == "direction")["items"]
    check("中性时方向组无 pass", all(i["status"] in ("warn", "unavailable")
                                     for i in dir_items),
          str([i["status"] for i in dir_items]))

    # ── 3. HTF 勾映射：2 同向 pass / 1 warn / 0 fail ──
    tfs_2 = {"5m": tfc("bullish"), "30m": tfc("bullish"), "1h": tfc("bullish"),
             "4h": tfc("bearish")}
    r3a = jc.score_confluence(mk_bundle(tfs=tfs_2))
    check("HTF 2/3 同向 → pass", r3a["cp1_align"]["htf"] == "pass")
    tfs_1 = {"5m": tfc("bullish"), "30m": tfc("bullish"), "1h": tfc("neutral"),
             "4h": tfc("bearish")}
    r3b = jc.score_confluence(mk_bundle(tfs=tfs_1))
    check("HTF 1/3 同向 → warn", r3b["cp1_align"]["htf"] == "warn")
    tfs_0 = {"5m": tfc("bearish"), "30m": tfc("bearish"), "1h": tfc("bearish"),
             "4h": tfc("bearish")}
    r3c = jc.score_confluence(mk_bundle(tfs=tfs_0))
    check("HTF 0/3 同向 → fail（注意：共识仍 bullish 打桩）",
          r3c["cp1_align"]["htf"] == "fail")

    # ── 4. 反向 TF 条目 fail 且带人话 ──
    it_4h = next(i for g in r3a["groups"] for i in g["items"] if i["key"] == "tf_4h")
    check("反向 4h fail + 人话", it_4h["status"] == "fail" and "相反" in it_4h["evidence"])

    # ── 5. unavailable 不计分母：砍掉微观+环境 → 归一后仍可高分 ──
    r5 = jc.score_confluence(mk_bundle(delta_status=None, rev_sat=None,
                                       funding=None))
    r5_ev = mk_bundle(delta_status=None, rev_sat=None, funding=None)
    r5_ev["event"] = None
    r5 = jc.score_confluence(r5_ev)
    check("砍两组后归一", r5["avail_weight"] == 70 and r5["score"] > 80,
          f"avail={r5['avail_weight']} score={r5['score']}")

    # ── 6. 全 unavailable → score 0 + insufficient ──
    r6_ev = mk_bundle(cons=False, wyckoff=None, hunt=None, fvg=None,
                      delta_status=None, rev_sat=None, funding=None)
    r6_ev["event"] = None
    r6 = jc.score_confluence(r6_ev)
    check("全缺 insufficient", r6["insufficient"] and r6["score"] == 0.0,
          f"score={r6['score']} avail={r6['avail_weight']}")
    check("全缺 CP1 勾 unavailable", all(v == "unavailable"
                                          for v in r6["cp1_align"].values()))

    # ── 7. 扫单方向语义 ──
    r7a = jc.score_confluence(mk_bundle(hunt="long-stops-swept"))
    it7a = next(i for g in r7a["groups"] for i in g["items"] if i["key"] == "sweep")
    check("同向扫单 pass", it7a["status"] == "pass")
    r7b = jc.score_confluence(mk_bundle(hunt="short-stops-swept"))
    it7b = next(i for g in r7b["groups"] for i in g["items"] if i["key"] == "sweep")
    check("反向扫单 fail + 标注", it7b["status"] == "fail" and "相反" in it7b["evidence"])

    # ── 8. FVG/折溢价 ──
    it8 = next(i for g in r1["groups"] for i in g["items"] if i["key"] == "fvg_pd")
    check("同向FVG+discount pass", it8["status"] == "pass"
          and "discount" in it8["evidence"])
    r8b = jc.score_confluence(mk_bundle(fvg="bad_zone"))
    it8b = next(i for g in r8b["groups"] for i in g["items"] if i["key"] == "fvg_pd")
    check("无FVG+premium(做多) warn", it8b["status"] == "warn")

    # ── 9. 安全带三态 + ⚡冲突显式（主控边界裁决）──
    _STUB["seatbelt"] = "conflict_strong"
    r9 = jc.score_confluence(mk_bundle())
    it9 = next(i for g in r9["groups"] for i in g["items"] if i["key"] == "seatbelt")
    check("强背离 fail + ⚡冲突标注", it9["status"] == "fail"
          and "⚡冲突" in it9["evidence"], it9["evidence"][:40])
    _STUB["seatbelt"] = "conflict_weak"
    r9b = jc.score_confluence(mk_bundle())
    it9b = next(i for g in r9b["groups"] for i in g["items"] if i["key"] == "seatbelt")
    check("弱背离 warn", it9b["status"] == "warn")
    _STUB["seatbelt"] = "confirm"

    # ── 10. 反转四条件档位 ──
    r10 = jc.score_confluence(mk_bundle(rev_sat=1))
    it10 = next(i for g in r10["groups"] for i in g["items"] if i["key"] == "reversal")
    check("反转 1/4 fail", it10["status"] == "fail" and "1/4" in it10["evidence"])

    # ── 11. 事件窗口 ──
    r11 = jc.score_confluence(mk_bundle(event_in=True))
    it11 = next(i for g in r11["groups"] for i in g["items"]
                if i["key"] == "event_window")
    check("事件窗口内 warn", it11["status"] == "warn" and "CPI" in it11["evidence"])

    # ── 12. 资金费：追拥挤 warn / 不在拥挤侧 pass ──
    r12a = jc.score_confluence(mk_bundle(funding=0.0008))   # 多头拥挤 + 环境偏多
    it12a = next(i for g in r12a["groups"] for i in g["items"] if i["key"] == "funding")
    check("追拥挤方 warn", it12a["status"] == "warn" and "追拥挤" in it12a["evidence"])
    r12b = jc.score_confluence(mk_bundle(cons_dir="bearish", funding=0.0008))
    it12b = next(i for g in r12b["groups"] for i in g["items"] if i["key"] == "funding")
    check("不在拥挤侧 pass", it12b["status"] == "pass")

    # ── 13. 成本徽章三档（评分外挂不进分）──
    base = jc.score_confluence(mk_bundle()).get("score")
    for toll, lv in ((0.1, "ok"), (0.5, "warn"), (1.5, "hard")):
        r13 = jc.score_confluence(mk_bundle(
            cost={"typical_r_pct": 0.5, "toll_ratio_est": toll, "basis": "est"}))
        check(f"cost {toll} → {lv}", r13["cost_flag"]["level"] == lv
              and r13["score"] == base,   # 不改分数
              str(r13["cost_flag"]))
    r13d = jc.score_confluence(mk_bundle(cost=None))
    check("无成本数据 cost_flag=None", r13d["cost_flag"] is None)

    # ── 14. 行动闸口透传（行为不进分——D3）──
    r14 = jc.score_confluence(mk_bundle(gate={"cooldown_until": 9999.0,
                                              "today_count": 5}))
    check("action_gate 透传", r14["action_gate"]["cooldown_until"] == 9999.0
          and r14["action_gate"]["today_count"] == 5)
    check("闸口不影响分数", r14["score"] == base)

    # ── 15. 展示字段字符串契约 + JSON 可序列化 ──
    all_items = [i for g in r1["groups"] for i in g["items"]]
    check("12 条目齐", len(all_items) == 12, str(len(all_items)))
    check("evidence 全 str", all(isinstance(i["evidence"], str) for i in all_items))
    check("JSON 序列化", len(json.dumps(r1, ensure_ascii=False, default=str)) > 500)

    # ── 16. 预登记随身 ──
    check("预登记权重随身", r1["prereg"]["weights"]["direction"] == 40
          and r1["prereg"]["item_weights"]["seatbelt"] == 12)

    # ── 17. 威科夫语境方向 ──
    ev17 = mk_bundle()
    ev17["wyckoff"]["state"]["side"] = "dist"   # 派发段 vs 偏多环境
    r17 = jc.score_confluence(ev17)
    it17 = next(i for g in r17["groups"] for i in g["items"]
                if i["key"] == "wyckoff_ctx")
    check("威科夫相悖 fail", it17["status"] == "fail" and "相悖" in it17["evidence"])

    # ── 18. assess 异常兜底（gather 内部炸不拖垮）──
    r18 = jc.assess("TESTUSDT", "30m", consensus_provider=lambda: 1 / 0)
    check("assess 永不抛出", isinstance(r18, dict) and "ok" in r18)
finally:
    jsb.evaluate = _orig_eval

# ── 汇总 ──
print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS（18 组 / 评分归一、三态、CP1 勾、冲突显式、成本徽章、行动闸口、契约）")
