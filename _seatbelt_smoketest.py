"""离线冒烟：「安全带」确认层（纯函数，不联网）——三态判定 / 共识叠加 / 提醒节流。"""
import jarvis_seatbelt as jsb

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def payload(bull_active=False, bear_active=False, strength=0.7):
    return {
        "ok": True,
        "divergence": {
            "bullish": {"active": bull_active, "strength": strength,
                        "note": "价格新低但 CVD 抬高", "anchors": []},
            "bearish": {"active": bear_active, "strength": strength,
                        "note": "价格新高但 CVD 降低", "anchors": []},
        },
        "absorption": {"detected": bull_active or bear_active, "side": "buy" if bull_active else "sell"},
    }


# ─── 1. 三态判定 ───
r = jsb.evaluate("bullish", payload(bull_active=True))
check("看涨+同向吸收背离 → confirmed 加分", r["status"] == "confirmed" and r["confidence_delta"] > 0,
      f"delta={r['confidence_delta']}")
r = jsb.evaluate("bullish", payload())
check("看涨+无背离 → no-evidence 零分", r["status"] == "no-evidence" and r["confidence_delta"] == 0.0,
      r["note"][:30])
r = jsb.evaluate("bullish", payload(bear_active=True))
check("看涨+反向派发背离 → conflict 降级", r["status"] == "conflict" and r["confidence_delta"] < 0,
      f"delta={r['confidence_delta']}")
r = jsb.evaluate("bearish", payload(bear_active=True))
check("看跌+同向背离 → confirmed（镜像）", r["status"] == "confirmed")
r = jsb.evaluate("bearish", payload(bull_active=True))
check("看跌+看涨吸收背离 → conflict（镜像）", r["status"] == "conflict")
r = jsb.evaluate("bullish", payload(bull_active=True, bear_active=True))
check("双向背离并存 → 反向证据优先亮红灯", r["status"] == "conflict")
r = jsb.evaluate("neutral", payload(bull_active=True))
check("共识中性 → idle 不修正", r["status"] == "idle" and r["confidence_delta"] == 0.0)
r = jsb.evaluate("bullish", None)
check("引擎缺失 → unavailable", r["status"] == "unavailable" and r["confidence_delta"] == 0.0)

# ─── 2. 强度分档（数值与文字两种形态） ───
r = jsb.evaluate("bullish", payload(bull_active=True, strength=0.9))
check("strength 0.9 → strong 档 +0.10", r["grade"] == "strong" and r["confidence_delta"] == 0.10)
r = jsb.evaluate("bullish", payload(bull_active=True, strength="weak"))
check("strength 'weak' → weak 档 +0.04", r["grade"] == "weak" and r["confidence_delta"] == 0.04)
r = jsb.evaluate("bullish", payload(bear_active=True, strength=0.9))
check("反向 strong → -0.15", r["confidence_delta"] == -0.15)

# ─── 3. 共识叠加 ───
cons = {"direction": "bullish", "confidence": 0.6, "reasoning": "多周期共识看涨"}
merged = jsb.apply_to_consensus(cons, payload(bull_active=True, strength=0.9))
sb = merged["seatbelt"]
check("叠加后原 confidence 不变", merged["confidence"] == 0.6)
check("adjusted = 0.6+0.10", abs(sb["adjusted_confidence"] - 0.70) < 1e-9,
      f"adj={sb['adjusted_confidence']}")
check("confirmed 追加 reasoning 尾注", "安全带" in merged["reasoning"])
check("status_cn 中文态", sb["status_cn"] == "吸收证据确认")

merged2 = jsb.apply_to_consensus(cons, payload())
check("no-evidence 不追加 reasoning", "安全带" not in merged2["reasoning"]
      and merged2["seatbelt"]["status"] == "no-evidence")

merged3 = jsb.apply_to_consensus({"direction": "bullish", "confidence": 0.05}, 
                                 payload(bear_active=True, strength=0.9))
check("降级后置信度 clamp 到 0", merged3["seatbelt"]["adjusted_confidence"] == 0.0)

# ─── 4. 强背离提醒（节流 + alert_center 落库） ───
import os, tempfile
import jarvis_journal as jj
d = tempfile.mkdtemp()
jj.DB_DIR = d
jj.DB_PATH = os.path.join(d, "t.db")
import jarvis_alert_center as jac

jsb._last_alert.clear()
jsb.maybe_alert_strong_divergence("BTCUSDT", payload(bull_active=True, strength=0.9))
evts = jac.list_events(10)
check("强背离落一条页内提醒", len(evts) == 1 and evts[0]["kind"] == "delta_divergence",
      str(evts[:1]))
jsb.maybe_alert_strong_divergence("BTCUSDT", payload(bull_active=True, strength=0.9))
check("同币同向 30min 节流不重复", len(jac.list_events(10)) == 1)
jsb.maybe_alert_strong_divergence("BTCUSDT", payload(bull_active=True, strength=0.4))
check("moderate 级不触发提醒", len(jac.list_events(10)) == 1)

print("\n=== " + ("全部通过" if not fails else f"失败 {len(fails)}: {fails}") + " ===")
raise SystemExit(1 if fails else 0)
