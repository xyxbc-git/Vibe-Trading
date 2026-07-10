"""主动提醒中心冒烟测试。使用临时目录 + 注入行情/信号获取器，全程离线不联网、不碰真实 ~/.vibe-trading。"""
from __future__ import annotations

import os
import tempfile

import jarvis_journal as jj

_d = tempfile.mkdtemp()
jj.DB_DIR = _d
jj.DB_PATH = os.path.join(_d, "test.db")

import jarvis_price_alert as jpa

# 隔离价位提醒旧 JSON 迁移逻辑，防止误动真实配置文件
jpa.CONFIG_DIR = _d
jpa.CONFIG_PATH = os.path.join(_d, "price_alert_config.json")
jpa._DB_INITIALIZED = False

import jarvis_alert_center as jac

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ── 1. 设置：默认值 / 更新夹紧 / 渠道开关 ─────────────────────────────
st = jac.get_settings()
check("默认轮询间隔", st["poll_interval_s"] == 30)
check("默认信号巡检间隔", st["signal_interval_s"] == 300)
check("默认渠道：页内+浏览器开，TG/邮件关",
      st["channels"]["inapp"] and st["channels"]["browser"]
      and not st["channels"]["telegram"] and not st["channels"]["email"])
st2 = jac.update_settings({"poll_interval_s": 3, "signal_interval_s": 10,
                           "channels": {"telegram": True}})
check("轮询间隔下限夹紧到 10", st2["poll_interval_s"] == 10)
check("信号间隔下限夹紧到 60", st2["signal_interval_s"] == 60)
check("渠道开关可更新", st2["channels"]["telegram"] is True)
jac.update_settings({"poll_interval_s": 30, "signal_interval_s": 300,
                     "channels": {"telegram": False}})

# ── 2. 规则 CRUD 与参数校验 ─────────────────────────────
bad = jac.add_rule({"kind": "price_level", "symbol": "BTC", "target_price": 0})
check("目标价 0 被拒", bad.get("ok") is False)
bad2 = jac.add_rule({"kind": "nope", "symbol": "BTC"})
check("未知 kind 被拒", bad2.get("ok") is False)
bad3 = jac.add_rule({"kind": "reentry", "symbol": "BTC", "exit_price": -1})
check("负平仓价被拒", bad3.get("ok") is False)

r_flip = jac.add_rule({"kind": "signal_flip", "symbol": "btc", "tf": "4h",
                       "min_confidence": 0})["rule"]
check("信号规则 symbol 规范化", r_flip["symbol"] == "BTCUSDT")
check("信号规则默认持续监控", r_flip["repeat"] is True)

r_price = jac.add_rule({"kind": "price_level", "symbol": "ETH",
                        "target_price": 100, "direction": "above",
                        "label": "道氏阻力"})["rule"]
check("价位规则默认一次性", r_price["repeat"] is False)
check("规则描述含涨破", "涨破" in r_price["desc"], r_price["desc"])

r_re = jac.add_rule({"kind": "reentry", "symbol": "SOL", "exit_price": 100,
                     "confirm_pct": 1.0, "note": "上次割肉点"})["rule"]
check("回升规则描述含平仓价", "平仓价" in r_re["desc"], r_re["desc"])

check("list_rules 共 3 条", len(jac.list_rules()) == 3)
check("按币种过滤", len(jac.list_rules("eth")) == 1)

upd = jac.update_rule(r_price["id"], {"target_price": 200, "note": "改价"})
check("update_rule 改目标价", upd["ok"] and upd["rule"]["params"]["target_price"] == 200)
check("参数变更重置基线", upd["rule"]["state"] == {})
jac.update_rule(r_price["id"], {"target_price": 100})

# ── 3. 价格关键位：穿越语义（首轮只建基线，穿越才触发，触发后一次性停用）──
prices = {"ETHUSDT": 98.0, "SOLUSDT": 95.0, "BTCUSDT": 50000.0}
get_price = lambda s: prices.get(s)  # noqa: E731
no_signal = lambda s, tf="4h": None  # noqa: E731 — 本段不测信号

sub = jac.subscribe()   # 顺带验证 SSE 广播

r1 = jac.evaluate_all(price_getter=get_price, consensus_getter=no_signal)
check("首轮只建基线不触发", r1["triggered"] == 0, str(r1))
prices["ETHUSDT"] = 99.5
r2 = jac.evaluate_all(price_getter=get_price, consensus_getter=no_signal)
check("未穿越不触发", r2["triggered"] == 0)
prices["ETHUSDT"] = 101.0
r3 = jac.evaluate_all(price_getter=get_price, consensus_getter=no_signal)
check("向上穿越触发", r3["triggered"] == 1, str(r3["results"]))
rule_after = [r for r in jac.list_rules("eth")][0]
check("一次性规则触发后停用", rule_after["enabled"] is False)
check("触发计数 +1", rule_after["triggered_count"] == 1)

evs = jac.list_events()
check("事件落库", len(evs) == 1 and "涨破" in evs[0]["title"], evs[0]["title"] if evs else "")
check("事件带标签", "道氏阻力" in evs[0]["title"])
check("事件严重级 warning", evs[0]["severity"] == "warning")
check("渠道结果：页内成功", evs[0]["channels"].get("inapp", {}).get("ok") is True)
check("渠道结果：TG/邮件跳过", evs[0]["channels"].get("telegram", {}).get("skipped") is True
      and evs[0]["channels"].get("email", {}).get("skipped") is True)
try:
    pushed = sub.get_nowait()
    check("SSE 订阅者收到广播", "涨破" in pushed["title"])
    check("浏览器渠道送达数 ≥1", evs[0]["channels"].get("browser", {}).get("subscribers", 0) >= 1)
except Exception:  # noqa: BLE001
    check("SSE 订阅者收到广播", False)
jac.unsubscribe(sub)

# ── 4. 割肉后回升：确认幅度 + 穿越触发 + critical 级 ─────────────────
prices["SOLUSDT"] = 100.5   # 未过确认线 101（=100×1.01）
r4 = jac.evaluate_all(price_getter=get_price, consensus_getter=no_signal)
check("回升未过确认线不触发", r4["triggered"] == 0)
prices["SOLUSDT"] = 102.0
r5 = jac.evaluate_all(price_getter=get_price, consensus_getter=no_signal)
check("站回平仓价触发", r5["triggered"] == 1, str(r5["results"]))
ev_re = jac.list_events()[0]
check("回升提醒为 critical", ev_re["severity"] == "critical")
check("回升提醒含再入场提示", "再入场" in ev_re["detail"])
check("回升提醒带备注", "上次割肉点" in ev_re["detail"])

# ── 5. 信号反转：基线→翻转→转中性→门槛降噪 ─────────────────────────
cons_seq: dict = {"val": {"direction": "bullish", "confidence": 0.8,
                          "reasoning": "测试推理", "price": 50000.0}}
get_cons = lambda s, tf="4h": cons_seq["val"]  # noqa: E731

r6 = jac.evaluate_all(price_getter=get_price, consensus_getter=get_cons, force_signal=True)
check("信号首轮只建基线", all("event_id" not in x for x in r6["results"]
                              if x.get("id") == r_flip["id"]))
r7 = jac.evaluate_all(price_getter=get_price, consensus_getter=get_cons, force_signal=True)
check("方向不变不触发", r7["triggered"] == 0)

cons_seq["val"] = {"direction": "bearish", "confidence": 0.75,
                   "reasoning": "空头推理", "price": 49000.0}
r8 = jac.evaluate_all(price_getter=get_price, consensus_getter=get_cons, force_signal=True)
check("看涨→看跌翻转触发", r8["triggered"] == 1, str(r8["results"]))
ev_flip = jac.list_events()[0]
check("翻转提醒为 critical", ev_flip["severity"] == "critical")
check("翻转标题含方向", "看涨 → 看跌" in ev_flip["title"], ev_flip["title"])

cons_seq["val"] = {"direction": "neutral", "confidence": 0.2,
                   "reasoning": "中性", "price": 49500.0}
r9 = jac.evaluate_all(price_getter=get_price, consensus_getter=get_cons, force_signal=True)
check("转中性触发 warning", r9["triggered"] == 1
      and jac.list_events()[0]["severity"] == "warning")

# 门槛降噪：min_confidence=0.5，弱信号不报、走强后补报
jac.update_rule(r_flip["id"], {"min_confidence": 0.5})
cons_seq["val"] = {"direction": "neutral", "confidence": 0.2, "reasoning": "", "price": 1}
jac.evaluate_all(price_getter=get_price, consensus_getter=get_cons, force_signal=True)  # 重建基线
cons_seq["val"] = {"direction": "bullish", "confidence": 0.3, "reasoning": "", "price": 1}
r10 = jac.evaluate_all(price_getter=get_price, consensus_getter=get_cons, force_signal=True)
check("弱信号被门槛压下", r10["triggered"] == 0)
cons_seq["val"] = {"direction": "bullish", "confidence": 0.6, "reasoning": "", "price": 1}
r11 = jac.evaluate_all(price_getter=get_price, consensus_getter=get_cons, force_signal=True)
check("走强后补报信号建立", r11["triggered"] == 1
      and "信号建立" in jac.list_events()[0]["title"], jac.list_events()[0]["title"])
check("信号规则持续启用（repeat）", jac.list_rules("btc")[0]["enabled"] is True)

# ── 6. 信号巡检节流：非 force 下间隔内跳过 ─────────────────────────
r12 = jac.evaluate_all(price_getter=get_price, consensus_getter=get_cons)
flip_checked = [x for x in r12["results"] if x.get("id") == r_flip["id"]]
check("节流期内信号规则跳过", not flip_checked)

# ── 7. 事件已读 / 未读 ─────────────────────────────
total_unread = jac.unread_count()
check("未读数 = 事件数", total_unread == len(jac.list_events()) and total_unread >= 4,
      f"unread={total_unread}")
first_id = jac.list_events()[0]["id"]
out = jac.mark_read(ids=[first_id])
check("按 id 标已读", out["updated"] == 1 and jac.unread_count() == total_unread - 1)
out2 = jac.mark_read(mark_all=True)
check("全部标已读", jac.unread_count() == 0, f"updated={out2['updated']}")
check("unread_only 过滤", len(jac.list_events(unread_only=True)) == 0)

# ── 8. 重复型价位规则：触发后保持启用，可再次触发 ─────────────────
r_rep = jac.add_rule({"kind": "price_level", "symbol": "BTC", "target_price": 51000,
                      "direction": "above", "repeat": True})["rule"]
prices["BTCUSDT"] = 50000.0
jac.evaluate_all(price_getter=get_price, consensus_getter=no_signal)   # 基线
prices["BTCUSDT"] = 51500.0
ra = jac.evaluate_all(price_getter=get_price, consensus_getter=no_signal)
check("重复规则第一次触发", ra["triggered"] == 1)
check("重复规则保持启用", [r for r in jac.list_rules("btc") if r["id"] == r_rep["id"]][0]["enabled"])
prices["BTCUSDT"] = 50000.0
jac.evaluate_all(price_getter=get_price, consensus_getter=no_signal)   # 跌回另一侧
prices["BTCUSDT"] = 51200.0
rb = jac.evaluate_all(price_getter=get_price, consensus_getter=no_signal)
check("重复规则可再次触发", rb["triggered"] == 1)

# ── 9. 关键位建议（注入共识，验证支撑/阻力方向判定）──────────────
_orig = jac._default_consensus
jac._default_consensus = lambda s, tf="4h": {
    "direction": "bullish", "confidence": 0.7, "price": 100.0,
    "key_levels": [{"label": "海龟阻力", "price": 110.0, "source": "turtle"},
                   {"label": "道氏支撑", "price": 90.0, "source": "dow"}],
}
kl = jac.key_levels("BTC")
check("关键位建议 ok", kl["ok"] and len(kl["levels"]) == 2)
check("高于现价→建议涨破", kl["levels"][0]["suggest_direction"] == "above")
check("低于现价→建议跌破", kl["levels"][1]["suggest_direction"] == "below")
jac._default_consensus = _orig

# ── 10. 删除规则 / 无效删除 ─────────────────────────────
check("删除规则", jac.delete_rule(r_rep["id"])["ok"] is True)
check("删除不存在的规则", jac.delete_rule("nope")["ok"] is False)

# ── 11. 模拟重启：DB 持久化 ─────────────────────────────
jac._DB_INITIALIZED = False
rules_after = jac.list_rules()
check("重启后规则持久", len(rules_after) == 3)
check("重启后事件持久", len(jac.list_events()) >= 6)

print("\n=== " + ("全部通过" if not fails else f"失败 {len(fails)}: {fails}") + " ===")
raise SystemExit(1 if fails else 0)
