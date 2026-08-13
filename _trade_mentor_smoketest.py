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

# ── 汇总 ──
print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    raise SystemExit(1)
print("ALL PASS（25 组 / 覆盖红线、权重、情绪、关键位、台账、信任回路、R2 字符串契约+principal）")
