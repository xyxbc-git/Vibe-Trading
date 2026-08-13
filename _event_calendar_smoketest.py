#!/usr/bin/env python3
"""jarvis_event_calendar（任务 U 金十事件日历）冒烟测试。

全部用构造响应（红线：不出网、不依赖真实 secret-key）。跑法：
    python3 _event_calendar_smoketest.py
"""

from __future__ import annotations

import time

import jarvis_event_calendar as jec
import jarvis_trade_mentor as jtm

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), name, detail)


NOW = time.time()


def _inject(events: list[dict]) -> None:
    """把构造事件灌进模块进程内缓存（key 探测也一并打桩为已配置）。"""
    jec._mem_cache.update(ts=time.time(), events=events)
    jec._load_key = lambda: "smoke-test-key"  # type: ignore[assignment]


def _ev(minutes_from_now: float, star: int = 3, title: str = "CPI",
        country: str = "美国") -> dict:
    return {"ts": NOW + minutes_from_now * 60.0, "country": country,
            "title": title, "importance": star, "previous": "3.0%",
            "forecast": "2.9%", "actual": None, "source": "jin10"}


# ── 1. 归一化 parser（宽容多候选字段 + 坏行丢弃）──
rows = [
    {"pub_time": NOW + 600, "star": 3, "title": "非农就业人口", "country": "美国",
     "previous": "22.7万", "consensus": "20万", "actual": None},
    {"pub_time": (NOW + 1200) * 1000, "star": "2", "title": "毫秒时间戳事件"},
    {"pub_time": "2026-08-13 20:30:00", "star": 1, "title": "字符串时间事件"},
    {"pub_time": None, "star": 3, "title": "无时间坏行"},
    {"star": 3},                       # 无标题坏行
    "not-a-dict",                      # 非 dict 坏行
]
norm = jec._normalize(rows)
check("N1 归一化保留 3 条好行、丢 3 条坏行", len(norm) == 3, f"n={len(norm)}")
check("N2 epoch 秒字段齐全", norm[0]["title"] == "非农就业人口"
      and norm[0]["forecast"] == "20万" and norm[0]["importance"] == 3)
check("N3 毫秒时间戳被折算", abs(next(e for e in norm if "毫秒" in e["title"])["ts"]
                              - (NOW + 1200)) < 1.0)
check("N4 北京时间字符串可解析",
      any("字符串" in e["title"] and e["ts"] > 1e9 for e in norm))

# ── 2. 未配置态（诚实 not_configured，绝不伪造）──
jec._load_key = lambda: None  # type: ignore[assignment]
jec._mem_cache.update(ts=0.0, events=None)
box = jec.events()
check("C1 未配置 ok=False + configured=False + 空事件",
      box["ok"] is False and box["configured"] is False and box["events"] == [])
rw = jec.risk_window()
check("C2 未配置 risk_window available=False（没数据≠没风险）",
      rw["available"] is False and rw["in_window"] is False)
st = jec.status()
check("C3 status 未配置含引导路径", st["configured"] is False
      and "open.jin10.com" in (st["note"] or ""))

# ── 3. 风险窗口三态（配置桩 + 构造事件）──
_inject([_ev(20, star=3)])            # 高影响事件 20 分钟后（pre=30 窗口内）
rw = jec.risk_window()
check("R1 事件前 20min 在风险窗口（pre=30）",
      rw["available"] and rw["in_window"] and "还有 20 分钟" in rw["note"], rw["note"])
_inject([_ev(-10, star=3)])           # 已公布 10 分钟（post=15 窗口内）
rw = jec.risk_window()
check("R2 公布后 10min 仍在窗口（post=15）",
      rw["in_window"] and "10 分钟前公布" in rw["note"], rw["note"])
_inject([_ev(-30, star=3), _ev(120, star=3)])   # 都在窗口外
rw = jec.risk_window()
check("R3 窗口外 in_window=False 且报最近事件倒计时",
      not rw["in_window"] and "当前不在风险窗口" in rw["note"]
      and abs((rw["minutes_to"] or 0) - 120) < 1.5, rw["note"])
_inject([_ev(10, star=2)])            # 低星级不触发（min_star=3）
rw = jec.risk_window()
check("R4 低星级事件不触发风险窗口", not rw["in_window"])

# ── 4. upcoming_events 过滤 ──
_inject([_ev(30, 3, "CPI"), _ev(600, 1, "低星"), _ev(60 * 30, 3, "超窗")])
up = jec.upcoming_events(within_minutes=24 * 60, min_star=1)
check("U1 24h 窗口过滤（超窗事件排除）",
      len(up["events"]) == 2 and all(e["title"] != "超窗" for e in up["events"]))
up3 = jec.upcoming_events(within_minutes=24 * 60, min_star=3)
check("U2 min_star=3 只留高影响", len(up3["events"]) == 1
      and up3["events"][0]["title"] == "CPI"
      and abs(up3["events"][0]["minutes_to"] - 30) < 1)

# ── 5. mentor 第八路证据（_judge_event_risk 三态 + 计分零影响）──
item = jtm._judge_event_risk({"event_risk": {"available": False, "reason": "未配置"}})
check("M1 unavailable 不计分母", item["level"] == "unavailable" and item["weight"] == 0)
item = jtm._judge_event_risk({"event_risk": {
    "available": True, "in_window": True,
    "note": "美国CPI（★★★）还有 22 分钟公布，数据瞬间插针风险极高，建议落地后再入场"}})
check("M2 风险窗口内 warn + 人话证据", item["level"] == "warn"
      and "还有 22 分钟" in item["evidence"], item["evidence"])
item = jtm._judge_event_risk({"event_risk": {"available": True, "in_window": False,
                                             "note": "未来无临近高影响事件"}})
check("M3 窗口外 pass", item["level"] == "pass")

# verdict 集成：event_risk warn 不改变分数（weight=0）
base_ev = {
    "trend": {"available": False, "reason": "smoke"},
    "wyckoff": {"available": False},
    # risk 证据用真实组装器（纯算术恒可用），字段契约不手搓
    "risk": jtm._risk_evidence(100.0, 98.0, 104.0),
    "levels": {"available": False, "reason": "smoke"},
    "micro": {"available": False, "reason": "smoke"},
    "history": {"available": False},
    "fvg": {"available": False},
    "event_risk": {"available": True, "in_window": False, "note": "无临近事件"},
}
plan = {"direction": "long", "entry": 100.0, "stop_loss": 98.0, "take_profit": 104.0}
v_pass = jtm.verdict(dict(base_ev), plan)
base_ev["event_risk"] = {"available": True, "in_window": True, "note": "CPI 还有 9 分钟"}
v_warn = jtm.verdict(dict(base_ev), plan)
check("M4 event_risk warn 不改变加权分（weight=0 预登记权重零改动）",
      v_pass["score"] == v_warn["score"],
      f"pass={v_pass['score']} warn={v_warn['score']}")
it = next(i for i in v_warn["items"] if i["key"] == "event_risk")
check("M5 verdict 明细含 event_risk warn 项", it["level"] == "warn"
      and "9 分钟" in it["evidence"])
import inspect  # noqa: E402

check("M6 build_evidence 已注册 event_risk 路",
      '"event_risk"' in inspect.getsource(jtm.build_evidence))

# ── 6. dashboard 端点（HTTP over ASGI，未配置态）──
jec._load_key = lambda: None  # type: ignore[assignment]
jec._mem_cache.update(ts=0.0, events=None)
from starlette.testclient import TestClient  # noqa: E402

import jarvis_dashboard as jd  # noqa: E402

client = TestClient(jd.app)
r = client.get("/api/events/status")
check("D1 /api/events/status 200 + not_configured 提示",
      r.status_code == 200 and r.json()["ok"] and not r.json()["configured"]
      and "open.jin10.com" in (r.json()["note"] or ""))
r = client.get("/api/events/upcoming?hours=24")
check("D2 /api/events/upcoming 未配置诚实空", r.status_code == 200
      and r.json()["ok"] and r.json()["configured"] is False
      and r.json()["events"] == [])

print(f"\nALL {'PASS' if not FAIL else 'FAIL'}  (pass={len(PASS)} fail={len(FAIL)})")
raise SystemExit(1 if FAIL else 0)
