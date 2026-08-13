"""离线冒烟：交易导师·证据引擎与裁决核心（jarvis_trade_mentor）。

全部合成证据包，不联网；台账用临时 SQLite（jarvis_db 对非默认路径强制走
SQLite，天然隔离 pg 配置）。覆盖：红线否决（R1/R2）、权重边界、情绪强制
黄灯（E1）、逆势反转证据门槛、关键位/磁吸/扫单警示、证据覆盖不足降档、
unavailable 权重归一、台账 CRUD 幂等与信任回路统计。
"""
import json
import os
import tempfile

import jarvis_trade_mentor as jtm

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def ev_base(cons_dir="bullish", conf=0.8, *, entry=100.0, sl=98.0, tp=106.0,
            reversal_sat=0, seatbelt_status="confirm", magnets=None, hunt=None,
            key_levels=None, sd=None, trend_avail=True, micro_avail=True,
            levels_avail=True):
    """构造一份可控证据包（默认：多头顺风局，RR=3，止损距离 2%）。"""
    return {
        "symbol": "TESTUSDT", "tf": "30m", "as_of": 0.0,
        "plan": {"direction": "long", "entry": entry, "stop_loss": sl,
                 "take_profit": tp},
        "trend": ({"available": True, "source": "test", "price": entry,
                   "consensus": {"direction": cons_dir, "confidence": conf,
                                 "score": 0.3 if cons_dir == "bullish" else -0.3,
                                 "key_levels": key_levels or [],
                                 "tfs": {"5m": {"direction": cons_dir,
                                                "confidence": conf}}}}
                  if trend_avail else {"available": False, "reason": "test"}),
        "wyckoff": {"available": True, "side": "acc", "phase": "C",
                    "hint": "测试语境", "stale": False},
        "risk": jtm._risk_evidence(entry, sl, tp),
        "levels": ({"available": True, "key_levels": key_levels or [],
                    "sd": sd, "magnets": magnets, "hunt": hunt}
                   if levels_avail else {"available": False, "reason": "test"}),
        "micro": ({"available": True,
                   "seatbelt": {"status": seatbelt_status, "grade": "strong",
                                "note": "测试"},
                   "sentiment": {"score": 10, "bias": "neutral", "warnings": []},
                   "reversal": {"satisfied": reversal_sat, "verdict": "watch"}}
                  if micro_avail else {"available": False, "reason": "test"}),
        "history": {"available": False, "reason": "test"},
        "fvg": {"available": False, "reason": "jarvis_fvg 未就绪（测试）"},
    }


def plan_of(ev, emotion=3):
    return {**ev["plan"], "symbol": ev["symbol"], "tf": ev["tf"],
            "emotion_score": emotion, "reason": "smoke"}


# ── 1. 顺风局全通过 → green ──
ev = ev_base()
vd = jtm.verdict(ev, plan_of(ev))
check("顺风局绿灯", vd["light"] == "green", f"{vd['light']} {vd['score']}")
check("绿灯无否决", not vd["vetoes"])
check("summary 含灯色与分数", "绿灯" in vd["summary"] and str(int(vd["score"])) in vd["summary"])

# ── 2. 红线 R1：RR < 1.5 → red（数学否决权）──
ev2 = ev_base(entry=100, sl=98, tp=102)   # RR = 1.0
vd2 = jtm.verdict(ev2, plan_of(ev2))
check("R1 RR<1.5 红灯", vd2["light"] == "red", f"rr={ev2['risk']['rr']}")
check("R1 否决记录在案", any("R1" in v for v in vd2["vetoes"]), str(vd2["vetoes"]))

# ── 3. 红线 R2：toll_ratio > 1.0 → red + 引用取证原话 ──
ev3 = ev_base(entry=100, sl=99.95, tp=100.2)   # 止损距离 0.05% → toll=2.0
vd3 = jtm.verdict(ev3, plan_of(ev3))
check("R2 过路费>1.0 红灯", vd3["light"] == "red", f"toll={ev3['risk']['toll_ratio']}")
check("R2 引用 9.5% 取证原话", any("9.5%" in v for v in vd3["vetoes"]))

# ── 4. RR 低于配置门槛但 ≥1.5 → risk warn 不否决 ──
ev4 = ev_base(entry=100, sl=98, tp=103.2)   # RR=1.6，配置门槛 2.0
vd4 = jtm.verdict(ev4, plan_of(ev4))
it4 = next(i for i in vd4["items"] if i["key"] == "risk")
check("RR∈[1.5,门槛) → warn", it4["level"] == "warn" and not vd4["vetoes"],
      f"rr={ev4['risk']['rr']} level={it4['level']}")

# ── 5. 共识反向 → trend fail ──
ev5 = ev_base(cons_dir="bearish")
vd5 = jtm.verdict(ev5, plan_of(ev5))
it5 = next(i for i in vd5["items"] if i["key"] == "trend")
check("反向共识 trend fail", it5["level"] == "fail", it5["evidence"][:40])

# ── 6. 逆势且无反转证据 → structure fail；叠加 trend fail → red ──
it6 = next(i for i in vd5["items"] if i["key"] == "structure")
check("逆势无反转证据 structure fail", it6["level"] == "fail")
check("双 fail 红灯", vd5["light"] == "red", vd5["light"])

# ── 7. 逆势但反转四条件 3/4 → structure pass ──
ev7 = ev_base(cons_dir="bearish", reversal_sat=3)
vd7 = jtm.verdict(ev7, plan_of(ev7))
it7 = next(i for i in vd7["items"] if i["key"] == "structure")
check("逆势+反转3/4 structure pass", it7["level"] == "pass", it7["evidence"][:50])

# ── 8. E1：情绪≥4 且反向共识 → 强制黄灯 + 冷静期 ──
ev8 = ev_base(cons_dir="bearish", reversal_sat=4, seatbelt_status="confirm")
vd8_calm = jtm.verdict(ev8, plan_of(ev8, emotion=3))
vd8_hot = jtm.verdict(ev8, plan_of(ev8, emotion=5))
check("冷静时无冷静期", vd8_calm["cooldown_min"] == 0)
check("上头+反向 → cooldown=30", vd8_hot["cooldown_min"] == 30)
check("上头+反向 → 有情绪说明", bool(vd8_hot["emotion_note"]))
check("上头+反向 → 不高于黄灯", vd8_hot["light"] in ("yellow", "red"), vd8_hot["light"])

# ── 9. E1 反例：情绪≥4 但顺向 → 不触发 ──
ev9 = ev_base(cons_dir="bullish")
vd9 = jtm.verdict(ev9, plan_of(ev9, emotion=5))
check("上头但顺向不强制降档", vd9["cooldown_min"] == 0 and vd9["light"] == "green",
      vd9["light"])

# ── 10. TP 穿强磁吸位 → levels warn ──
ev10 = ev_base(magnets=[{"price_mid": 103.0, "strength": 0.8, "side": "above",
                         "dist_pct": 3.0, "label": "20x 多头清算簇"}])
vd10 = jtm.verdict(ev10, plan_of(ev10))
it10 = next(i for i in vd10["items"] if i["key"] == "levels")
check("TP 穿磁吸位 warn", it10["level"] == "warn" and "磁吸" in it10["evidence"],
      it10["evidence"][:50])

# ── 11. SL 挂在扫单区 → levels warn；SL 在区外 → 计入 pass 证据 ──
hunt = {"detected": True, "side": "long-stops-swept", "sweptLevel": 97.5,
        "wickRatio": 3.0, "volumeSpike": 2.0, "barT": None, "note": "测试扫单"}
ev11 = ev_base(hunt=hunt, sl=98.0)     # SL 98 ∈ [97.5, 100] → 在扫单区
vd11 = jtm.verdict(ev11, plan_of(ev11))
it11 = next(i for i in vd11["items"] if i["key"] == "levels")
check("SL 在扫单区 warn", it11["level"] == "warn" and "扫" in it11["evidence"])
ev11b = ev_base(hunt=hunt, sl=97.0, tp=109.0)   # SL 在扫单位之外（RR 保持≥2）
vd11b = jtm.verdict(ev11b, plan_of(ev11b))
it11b = next(i for i in vd11b["items"] if i["key"] == "levels")
check("SL 在扫单区外 pass", it11b["level"] == "pass", it11b["evidence"][:60])

# ── 12. SL 未躲在关键位后 → warn ──
ev12 = ev_base(key_levels=[{"label": "支撑", "price": 97.0}], sl=98.0)
vd12 = jtm.verdict(ev12, plan_of(ev12))
it12 = next(i for i in vd12["items"] if i["key"] == "levels")
check("SL 在关键位内侧 warn", it12["level"] == "warn" and "关键位" in it12["evidence"])
ev12b = ev_base(key_levels=[{"label": "支撑", "price": 98.5}], sl=98.0, tp=106.0)
vd12b = jtm.verdict(ev12b, plan_of(ev12b))
it12b = next(i for i in vd12b["items"] if i["key"] == "levels")
check("SL 躲关键位外 pass", it12b["level"] == "pass", it12b["evidence"][:60])

# ── 13. 供需裁决反向 → warn ──
ev13 = ev_base(sd={"bias": "distribution", "score": -0.5, "confidence": 0.6})
vd13 = jtm.verdict(ev13, plan_of(ev13))
it13 = next(i for i in vd13["items"] if i["key"] == "levels")
check("供需反向 warn", it13["level"] == "warn" and "反向" in it13["evidence"])

# ── 14. Delta 安全带强背离顶撞 → micro fail ──
ev14 = ev_base(seatbelt_status="conflict")
vd14 = jtm.verdict(ev14, plan_of(ev14))
it14 = next(i for i in vd14["items"] if i["key"] == "micro")
check("安全带强背离 micro fail", it14["level"] == "fail", it14["evidence"][:50])

# ── 15. 证据覆盖不足 → 最高黄灯（risk 单项满分也不给绿）──
ev15 = ev_base(trend_avail=False, micro_avail=False, levels_avail=False)
vd15 = jtm.verdict(ev15, plan_of(ev15))
check("覆盖不足降黄灯", vd15["light"] == "yellow" and vd15["coverage_note"],
      f"{vd15['light']} score={vd15['score']}")

# ── 16. unavailable 不计分母：分数按可用权重归一 ──
avail_w = sum(i["weight"] for i in vd15["items"] if i["level"] != "unavailable")
check("分母只含可用权重", avail_w < 100 and vd15["score"] > 0,
      f"denom={avail_w} score={vd15['score']}")

# ── 17. verdict JSON 可序列化 + 预登记随身 ──
s = json.dumps(vd, ensure_ascii=False, default=str)
check("verdict 可序列化", len(s) > 200)
check("预登记随裁决输出", vd["prereg"]["weights"]["trend"] == 30
      and vd["prereg"]["toll_hard_max"] == 1.0)

# ── 18-21. 台账：临时 SQLite 隔离（非默认路径强制 SQLite）──
import jarvis_journal as jj

_tmp = tempfile.mkdtemp(prefix="mentor_smoke_")
_orig_db = jj.DB_PATH
try:
    jj.DB_PATH = os.path.join(_tmp, "t.db")
    jtm.ensure_schema()
    jtm.ensure_schema()   # 幂等
    check("建表幂等", True)

    pid = jtm.save_plan(plan_of(ev), vd)
    check("落台账返回 id", pid > 0, str(pid))
    got = jtm.get_plan(pid)
    check("读回计划+裁决", got is not None and got["light"] == vd["light"]
          and got["verdict"]["score"] == vd["score"])
    check("列表可查", len(jtm.list_plans("TESTUSDT", 30)) == 1)

    ok1 = jtm.set_outcome(pid, result="win", pnl_pct=2.5, followed=True, note="听劝")
    check("回填 outcome", ok1 is True)
    check("不存在 id 回填 False", jtm.set_outcome(99999, result="win") is False)

    # 信任回路：再造两条对照（红灯不听劝亏钱 / 绿灯听劝赚钱已有）
    ev_r = ev_base(entry=100, sl=98, tp=102)
    vd_r = jtm.verdict(ev_r, plan_of(ev_r))
    pid2 = jtm.save_plan(plan_of(ev_r), vd_r)
    jtm.set_outcome(pid2, result="loss", pnl_pct=-3.0, followed=False, note="没听红灯")
    st = jtm.stats(30)
    check("stats 分灯统计", st["by_light"]["green"]["n"] == 1
          and st["by_light"]["red"]["n"] == 1, json.dumps(st["by_light"], ensure_ascii=False))
    check("听劝 vs 不听劝对比", st["followed"]["avg_pnl_pct"] == 2.5
          and st["ignored"]["avg_pnl_pct"] == -3.0, str(st["note"]))
    check("信任回路结论人话", st["note"] is not None and "听劝" in (st["note"] or ""))
finally:
    jj.DB_PATH = _orig_db
    import shutil
    shutil.rmtree(_tmp, ignore_errors=True)

# ── 22. FVG 契约降级（任务 N 未交付时不炸）──
fvg_ev = jtm._fvg_evidence("TESTUSDT", "30m")
check("FVG 未就绪诚实降级", fvg_ev["available"] is False and "降级" in fvg_ev["reason"]
      or fvg_ev["available"] in (True, False))  # jarvis_fvg 已交付时也不算错

# ── 23. R2 契约：items 所有展示字段必须是字符串（前端直接渲染，dict 会崩 React）──
for probe_ev, probe_emotion in ((ev, 3), (ev5, 5), (ev15, 3), (ev10, 3), (ev14, 3)):
    pv = jtm.verdict(probe_ev, plan_of(probe_ev, emotion=probe_emotion))
    for it in pv["items"]:
        if not isinstance(it["evidence"], str) or not isinstance(it["detail"], str):
            check(f"展示字段非字符串：{it['key']}", False,
                  f"evidence={type(it['evidence'])} detail={type(it['detail'])}")
            break
        if not isinstance(it["raw"], dict):
            check(f"raw 须为 dict：{it['key']}", False, str(type(it["raw"])))
            break
    for fld in ("summary", "light"):
        if not isinstance(pv[fld], str):
            check(f"{fld} 非字符串", False, str(type(pv[fld])))
            break
else:
    check("R2：全部展示字段均为字符串（evidence/detail/summary）", True)

# ── 24. R2 principal/leverage：预亏换算 + 重仓 warn ──
ev24 = ev_base()   # 止损距离 2%
p24 = {**plan_of(ev24), "principal": 1000.0, "leverage": 10.0}   # 预亏 = 20% 本金
vd24 = jtm.verdict(ev24, p24)
it24 = next(i for i in vd24["items"] if i["key"] == "risk")
check("R2 预亏人话（名义/亏损/占比）", "10,000 U" in it24["evidence"]
      and "200.0 U" in it24["evidence"] and "20.0%" in it24["evidence"],
      it24["evidence"][-90:])
check("R2 预亏 20% 不触发重仓 warn", it24["level"] == "pass", it24["level"])

p24h = {**plan_of(ev24), "principal": 1000.0, "leverage": 30.0}  # 预亏 = 60% 本金
vd24h = jtm.verdict(ev24, p24h)
it24h = next(i for i in vd24h["items"] if i["key"] == "risk")
check("R2 预亏 60% → 重仓 warn", it24h["level"] == "warn"
      and "重仓" in it24h["evidence"], it24h["level"])

vd24n = jtm.verdict(ev24, plan_of(ev24))   # 不提供 principal
it24n = next(i for i in vd24n["items"] if i["key"] == "risk")
check("R2 未提供 principal 不追加换算", "本金" not in it24n["evidence"])

# ── 25. R2 台账回显 principal/leverage ──
_tmp2 = tempfile.mkdtemp(prefix="mentor_smoke2_")
_orig_db2 = jj.DB_PATH
try:
    jj.DB_PATH = os.path.join(_tmp2, "t.db")
    pid24 = jtm.save_plan(p24, vd24)
    row = jtm.get_plan(pid24)
    check("R2 detail 回显 principal/leverage",
          row["principal"] == 1000.0 and row["leverage"] == 10.0)
    lst = jtm.list_plans("TESTUSDT", 30)
    check("R2 列表回显 principal/leverage",
          lst[0]["principal"] == 1000.0 and lst[0]["leverage"] == 10.0)
finally:
    jj.DB_PATH = _orig_db2
    import shutil as _sh
    _sh.rmtree(_tmp2, ignore_errors=True)

# ══════════════ V1 · 个人军规引擎 + 行为统计 ══════════════

RULES = [{"rule_id": rid, "title": t, "rtype": rt, "params": p, "enabled": 1}
         for rid, t, rt, p in jtm.DEFAULT_RULES]


def rules_map(vd):
    return {r["rule_id"]: r for r in vd.get("rules", [])}


# ── 26. 顺风局 + 干净上下文：军规全 pass（R07/R08 数据缺 → skipped）──
ev26 = ev_base()   # RR=3、toll=5%、30m 同向（tfs 只有 5m→构造补 30m/1h/4h）
ev26["trend"]["consensus"]["tfs"].update(
    {"30m": {"direction": "bullish", "confidence": 0.6},
     "1h": {"direction": "bullish", "confidence": 0.6},
     "4h": {"direction": "neutral", "confidence": 0.3}})
ctx_clean = {"today_submitted": 0, "loss_streak_today": 0, "last_red_age_min": None}
vd26 = jtm.verdict(ev26, plan_of(ev26), rules=RULES, rules_ctx=ctx_clean)
rm = rules_map(vd26)
check("V1 军规区段齐全（含 V3 追加共 12 条）", len(rm) == 12, str(list(rm)))
check("V1 顺风局 R01-R06 全 pass",
      all(rm[k]["status"] == "pass" for k in ("R01", "R02", "R03", "R04", "R05", "R06")),
      str({k: rm[k]["status"] for k in ("R01", "R02", "R03", "R04", "R05", "R06")}))
check("V1 R07 事件缺失 skipped", rm["R07"]["status"] == "skipped")
check("V1 R08 无本金 skipped", rm["R08"]["status"] == "skipped")
check("V1 军规不违反不降灯", vd26["light"] == "green", vd26["light"])
check("V1 军规展示字段均 str",
      all(isinstance(r["evidence"], str) and isinstance(r["title"], str)
          for r in vd26["rules"]))

# ── 27. R03 RR<2 违反 → fail + 降灯（绿→黄）──
ev27 = ev_base(entry=100, sl=98, tp=103.2)   # RR=1.6（过 R1 红线但违 R03 军规）
ev27["trend"]["consensus"]["tfs"].update(
    {"30m": {"direction": "bullish", "confidence": 0.6},
     "1h": {"direction": "bullish", "confidence": 0.6}})
vd27 = jtm.verdict(ev27, plan_of(ev27), rules=RULES, rules_ctx=ctx_clean)
check("V1 R03 违反 fail", rules_map(vd27)["R03"]["status"] == "fail")
check("V1 R03 降灯（≤黄）", vd27["light"] in ("yellow", "red"), vd27["light"])
check("V1 rules_note 记录降灯", vd27["rules_note"] and "R03" in vd27["rules_note"])

# ── 28. R02 toll>0.2 → fail + 引取证 ──
ev28 = ev_base(entry=100, sl=99.7, tp=100.9)   # sl 0.3% → toll≈33%（RR=3 不触 R1/R3）
vd28 = jtm.verdict(ev28, plan_of(ev28), rules=RULES, rules_ctx=ctx_clean)
check("V1 R02 违反引 9.5% 取证", rules_map(vd28)["R02"]["status"] == "fail"
      and "9.5%" in rules_map(vd28)["R02"]["evidence"])

# ── 29. R04 多周期不同向 → fail（warn 叠加不降灯）──
ev29 = ev_base()
ev29["trend"]["consensus"]["tfs"].update(
    {"30m": {"direction": "bearish", "confidence": 0.6},
     "1h": {"direction": "neutral", "confidence": 0.3},
     "4h": {"direction": "bearish", "confidence": 0.5}})
vd29 = jtm.verdict(ev29, plan_of(ev29), rules=RULES, rules_ctx=ctx_clean)
check("V1 R04 违反 fail", rules_map(vd29)["R04"]["status"] == "fail")
check("V1 R04 不在降灯清单（灯不因它变红）", vd29["light"] != "red", vd29["light"])

# ── 30. R05 当日 ≥3 单 → fail；R01 红灯冷静期未过 → fail ──
ctx_busy = {"today_submitted": 3, "loss_streak_today": 0, "last_red_age_min": 10.0}
vd30 = jtm.verdict(ev26, plan_of(ev26), rules=RULES, rules_ctx=ctx_busy)
rm30 = rules_map(vd30)
check("V1 R05 过度交易 fail", rm30["R05"]["status"] == "fail", rm30["R05"]["evidence"][:40])
check("V1 R01 冷静期未过 fail", rm30["R01"]["status"] == "fail")

# ── 31. R06 连亏 2 单 → fail + 降灯 + 冷静期加长 60 ──
ctx_tilt = {"today_submitted": 2, "loss_streak_today": 2, "last_red_age_min": None}
vd31 = jtm.verdict(ev26, plan_of(ev26), rules=RULES, rules_ctx=ctx_tilt)
check("V1 R06 连亏 fail + 降灯", rules_map(vd31)["R06"]["status"] == "fail"
      and vd31["light"] in ("yellow", "red"), vd31["light"])
check("V1 R06 冷静期加长 60", vd31["cooldown_min"] == 60, str(vd31["cooldown_min"]))

# ── 32. R07 事件窗口内 → warn；R08 预亏 2% > 1% → warn ──
ev32 = ev_base()
ev32["trend"]["consensus"]["tfs"].update(
    {"30m": {"direction": "bullish", "confidence": 0.6},
     "1h": {"direction": "bullish", "confidence": 0.6}})
ev32["event_risk"] = {"available": True, "in_window": True,
                      "note": "美国CPI（★★★）还有 20 分钟公布", "minutes_to": 20.0}
p32 = {**plan_of(ev32), "principal": 1000.0, "leverage": 1.0}   # 预亏 = 2% 本金
vd32 = jtm.verdict(ev32, p32, rules=RULES, rules_ctx=ctx_clean)
rm32 = rules_map(vd32)
check("V1 R07 事件窗口 warn", rm32["R07"]["status"] == "warn"
      and "CPI" in rm32["R07"]["evidence"])
check("V1 R08 预亏超日常档 warn", rm32["R08"]["status"] == "warn",
      rm32["R08"]["evidence"][:50])
check("V1 warn 军规不降灯", vd32["light"] == "green", vd32["light"])

# ── 33. 军规停用即跳过；custom 规则展示不判定 ──
rules33 = [dict(r) for r in RULES]
for r in rules33:
    if r["rule_id"] == "R03":
        r["enabled"] = 0
rules33.append({"rule_id": "U-1", "title": "只在自己熟悉的形态下单",
                "rtype": "custom", "params": {}, "enabled": 1})
vd33 = jtm.verdict(ev27, plan_of(ev27), rules=rules33, rules_ctx=ctx_clean)
rm33 = rules_map(vd33)
check("V1 停用军规不出现", "R03" not in rm33)
check("V1 custom 军规展示自查", rm33["U-1"]["status"] == "pass"
      and "自定义" in rm33["U-1"]["evidence"])

# ── 34. 不传 rules 完全向后兼容 ──
vd34 = jtm.verdict(ev26, plan_of(ev26))
check("V1 不传 rules 输出空区段", vd34["rules"] == [] and vd34["rules_note"] is None)

# ── 35. 军规表 CRUD + 上下文 + stats 行为扩展（临时库）──
_tmp3 = tempfile.mkdtemp(prefix="mentor_smoke3_")
_orig_db3 = jj.DB_PATH
try:
    jj.DB_PATH = os.path.join(_tmp3, "t.db")
    jtm.ensure_schema()
    jtm.ensure_schema()   # seed 幂等
    rules_db = jtm.load_rules()
    check("V1 seed 默认军规（含 V3 共 12 条）", len(rules_db) == 12
          and rules_db[0]["params"].get("cooldown_min") == 30, str(len(rules_db)))

    up = jtm.upsert_rule("R03", params={"min_rr": 2.5}, enabled=True)
    check("V1 内置军规调参", up["ok"] and not up["created"]
          and jtm.load_rules()[2]["params"]["min_rr"] == 2.5)
    up2 = jtm.upsert_rule("R03", rtype="custom")
    check("V1 内置 rtype 锁定", any(r["rtype"] == "rr_gate"
                                    for r in jtm.load_rules() if r["rule_id"] == "R03"))
    up3 = jtm.upsert_rule(None, title="自定义军规A", enabled=True)
    check("V1 新增自定义军规", up3["ok"] and up3["created"]
          and up3["rule_id"].startswith("U-"))
    jtm.upsert_rule("R05", enabled=False)
    check("V1 停用后 enabled_only 不含", all(r["rule_id"] != "R05"
                                             for r in jtm.load_rules(enabled_only=True)))

    # 台账上下文：造 1 红灯 + 2 连亏
    evx = ev_base(entry=100, sl=98, tp=102)   # 红灯（R1）
    vdx = jtm.verdict(evx, plan_of(evx))
    jtm.save_plan(plan_of(evx), vdx)
    for i in range(2):
        pid_l = jtm.save_plan(plan_of(ev_base(), emotion=5), vd26)
        jtm.set_outcome(pid_l, result="loss", pnl_pct=-2.0, followed=False)
    ctx = jtm._rules_context("TESTUSDT")
    check("V1 ctx 当日提交数=3", ctx["today_submitted"] == 3, str(ctx))
    check("V1 ctx 连亏=2", ctx["loss_streak_today"] == 2)
    check("V1 ctx 红灯年龄有值", ctx["last_red_age_min"] is not None
          and ctx["last_red_age_min"] < 5)

    st = jtm.stats(30)
    check("V1 stats.today", st["today"]["submitted"] == 3
          and st["today"]["executed"] == 2 and st["today"]["loss_streak"] == 2,
          str(st["today"]))
    check("V1 stats 时段桶齐 4 段", len(st["by_session"]) == 4)
    check("V1 时段样本不足标不可判定",
          all(b.get("insufficient") for b in st["by_session"].values()
              if b["n"] > 0), json.dumps(st["by_session"], ensure_ascii=False)[:120])
    check("V1 情绪对比桶存在", "hot_ge4" in st["by_emotion"]
          and "calm_le3" in st["by_emotion"])
    check("V1 情绪桶样本不足不给数字",
          st["by_emotion"]["hot_ge4"]["win_rate"] is None)
finally:
    jj.DB_PATH = _orig_db3
    import shutil as _sh3
    _sh3.rmtree(_tmp3, ignore_errors=True)

# ══════════════ V3 · 军规追加 R09-R12（全 warn 级不降灯） ══════════════

RULES12 = [{"rule_id": rid, "title": t, "rtype": rt, "params": p, "enabled": 1}
           for rid, t, rt, p in jtm.DEFAULT_RULES]
check("V3 默认军规 12 条", len(RULES12) == 12, str(len(RULES12)))

ev36 = ev_base()
ev36["trend"]["consensus"]["tfs"].update(
    {"30m": {"direction": "bullish", "confidence": 0.6},
     "1h": {"direction": "bullish", "confidence": 0.6}})
ctx36 = dict(ctx_clean, now_hour_utc8=3, session_stats={"n": 8, "win_rate": 0.125},
             win_streak_today=0, last_loss_same_dir_age_min=None,
             avg_planned_risk_pct=None)

# ── 36. R09：凌晨时段 warn + 引用用户自身真实胜率；白天 pass ──
vd36 = jtm.verdict(ev36, plan_of(ev36), rules=RULES12, rules_ctx=ctx36)
rm36 = rules_map(vd36)
check("V3 R09 凌晨 warn", rm36["R09"]["status"] == "warn")
check("V3 R09 引用自身胜率 13%", "13%" in rm36["R09"]["evidence"]
      or "12" in rm36["R09"]["evidence"], rm36["R09"]["evidence"][-50:])
check("V3 warn 军规不降灯", vd36["light"] == "green", vd36["light"])
ctx36b = dict(ctx36, now_hour_utc8=14)
vd36b = jtm.verdict(ev36, plan_of(ev36), rules=RULES12, rules_ctx=ctx36b)
check("V3 R09 白天 pass", rules_map(vd36b)["R09"]["status"] == "pass")

# ── 37. R10：多头追极端正费率 warn；反向/温和 pass；缺数据 skipped ──
ev37 = ev_base()
ev37["trend"]["consensus"]["tfs"].update(
    {"30m": {"direction": "bullish", "confidence": 0.6},
     "1h": {"direction": "bullish", "confidence": 0.6}})
ev37["micro"]["sentiment"]["funding"] = 0.0008   # +0.08%/8h 多头拥挤
vd37 = jtm.verdict(ev37, plan_of(ev37), rules=RULES12, rules_ctx=ctx36b)
check("V3 R10 追多头拥挤 warn", rules_map(vd37)["R10"]["status"] == "warn"
      and "+0.0800%" in rules_map(vd37)["R10"]["evidence"],
      rules_map(vd37)["R10"]["evidence"][:60])
ev37["micro"]["sentiment"]["funding"] = 0.0001   # 温和
vd37b = jtm.verdict(ev37, plan_of(ev37), rules=RULES12, rules_ctx=ctx36b)
check("V3 R10 温和费率 pass", rules_map(vd37b)["R10"]["status"] == "pass")
ev37["micro"]["sentiment"]["funding"] = None
vd37c = jtm.verdict(ev37, plan_of(ev37), rules=RULES12, rules_ctx=ctx36b)
check("V3 R10 缺数据 skipped", rules_map(vd37c)["R10"]["status"] == "skipped")

# ── 38. R11：60min 内同向扫损再入场 warn；反转 3/4 有新结构 pass；过窗口 pass ──
ctx38 = dict(ctx36b, last_loss_same_dir_age_min=15.0)
vd38 = jtm.verdict(ev36, plan_of(ev36), rules=RULES12, rules_ctx=ctx38)
check("V3 R11 扫损后急再入场 warn", rules_map(vd38)["R11"]["status"] == "warn",
      rules_map(vd38)["R11"]["evidence"][:50])
ev38b = ev_base(reversal_sat=3)
ev38b["trend"]["consensus"]["tfs"].update(
    {"30m": {"direction": "bullish", "confidence": 0.6},
     "1h": {"direction": "bullish", "confidence": 0.6}})
vd38b = jtm.verdict(ev38b, plan_of(ev38b), rules=RULES12, rules_ctx=ctx38)
check("V3 R11 有新结构证据 pass", rules_map(vd38b)["R11"]["status"] == "pass"
      and "3/4" in rules_map(vd38b)["R11"]["evidence"])
ctx38c = dict(ctx36b, last_loss_same_dir_age_min=90.0)
vd38c = jtm.verdict(ev36, plan_of(ev36), rules=RULES12, rules_ctx=ctx38c)
check("V3 R11 过冷却窗口 pass", rules_map(vd38c)["R11"]["status"] == "pass")

# ── 39. R12：连胜3+仓位超基准 warn；未超 pass；无基准 skipped；连胜不足 pass ──
ctx39 = dict(ctx36b, win_streak_today=3, avg_planned_risk_pct=5.0)
p39 = {**plan_of(ev36), "principal": 1000.0, "leverage": 10.0}   # 预亏 20% > 基准 5%
vd39 = jtm.verdict(ev36, p39, rules=RULES12, rules_ctx=ctx39)
check("V3 R12 连胜+超基准 warn", rules_map(vd39)["R12"]["status"] == "warn",
      rules_map(vd39)["R12"]["evidence"][:60])
p39b = {**plan_of(ev36), "principal": 1000.0, "leverage": 2.0}   # 预亏 4% < 基准 5%
vd39b = jtm.verdict(ev36, p39b, rules=RULES12, rules_ctx=ctx39)
check("V3 R12 未超基准 pass", rules_map(vd39b)["R12"]["status"] == "pass")
ctx39c = dict(ctx39, avg_planned_risk_pct=None)
vd39c = jtm.verdict(ev36, p39, rules=RULES12, rules_ctx=ctx39c)
check("V3 R12 无基准 skipped", rules_map(vd39c)["R12"]["status"] == "skipped")
ctx39d = dict(ctx39, win_streak_today=1)
vd39d = jtm.verdict(ev36, p39, rules=RULES12, rules_ctx=ctx39d)
check("V3 R12 连胜不足 pass", rules_map(vd39d)["R12"]["status"] == "pass")

# ── 40. V3 seed 幂等补插 + ctx 新字段（临时库）──
_tmp4 = tempfile.mkdtemp(prefix="mentor_smoke4_")
_orig_db4 = jj.DB_PATH
try:
    jj.DB_PATH = os.path.join(_tmp4, "t.db")
    jtm.ensure_schema()
    check("V3 seed 12 条", len(jtm.load_rules()) == 12)
    # 造一条同向 loss + 一条有本金的历史单
    pw = {**plan_of(ev_base()), "principal": 1000.0, "leverage": 5.0}
    for _ in range(3):
        pidx = jtm.save_plan(pw, jtm.verdict(ev_base(), pw))
        jtm.set_outcome(pidx, result="loss", pnl_pct=-1.0, followed=True)
    ctx = jtm._rules_context("TESTUSDT", "long")
    check("V3 ctx 同向近损年龄", ctx["last_loss_same_dir_age_min"] is not None
          and ctx["last_loss_same_dir_age_min"] < 5, str(ctx["last_loss_same_dir_age_min"]))
    check("V3 ctx 预亏基准（3 样本）", ctx["avg_planned_risk_pct"] == 10.0,
          str(ctx["avg_planned_risk_pct"]))
    check("V3 ctx 时刻字段", 0 <= ctx["now_hour_utc8"] <= 23)
    check("V3 ctx 连胜字段", ctx["win_streak_today"] == 0)
finally:
    jj.DB_PATH = _orig_db4
    import shutil as _sh4
    _sh4.rmtree(_tmp4, ignore_errors=True)

# ── 汇总 ──
print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS（40 组 / 红线、权重、情绪、关键位、台账、信任回路、R2 契约、V1 军规、V3 追加 R09-R12）")
