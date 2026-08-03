"""离线冒烟：诱多/诱空陷阱检测（jarvis_trap_detect）。

全部合成数据，不联网不碰库：四类规则正负样本 + B2 契约字段 + 去重上限 +
mock 幂等 + 提醒中心链路（假模块注入，不写真实 DB）。
"""
import sys
import types

import jarvis_trap_detect as jtd

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def bars_flat(n, price=100.0, vol=100.0, tb_ratio=0.5):
    """横盘基底：小实体小波动（前高 100.7 / 前低 99.3），taker_buy 中性。"""
    out = []
    for i in range(n):
        o = price + (0.2 if i % 2 == 0 else -0.2)
        c = price - (0.2 if i % 2 == 0 else -0.2)
        out.append({"ts": 1_700_000_000_000 + i * 900_000,
                    "open": o, "high": max(o, c) + 0.5, "low": min(o, c) - 0.5,
                    "close": c, "volume": vol, "taker_buy": vol * tb_ratio})
    return out


def bar(i, **kw):
    b = {"ts": 1_700_000_000_000 + i * 900_000, "volume": 100.0, "taker_buy": 50.0}
    b.update(kw)
    return b


def by_type(signals, typ):
    return [s for s in signals if s["type"] == typ]


# ── 1. 规则 B · 诱多插针：长上影刺破前高收回 + 放量（叠加 Delta 负 → 多证据合并） ──
bars = bars_flat(30)
bars.append(bar(30, open=100.0, high=102.4, low=99.9, close=100.1,
                volume=300.0, taker_buy=90.0))  # delta = -120
sigs = jtd.detect_traps(bars)
bulls = by_type(sigs, "bull_trap")
check("诱多插针命中", len(bulls) == 1, f"bull={len(bulls)}")
s = bulls[0] if bulls else {}
check("price=冲高点", s.get("price") == 102.4, str(s.get("price")))
check("ts=bar开盘秒", s.get("ts") == (1_700_000_000_000 + 30 * 900_000) // 1000, str(s.get("ts")))
check("id 前缀", str(s.get("id", "")).startswith("trap-bull-"), str(s.get("id")))
check("插针+Delta 多证据合并", len(s.get("reasons") or []) >= 3,
      f"reasons={len(s.get('reasons') or [])}")
check("证据含长上影", any("上影" in r for r in s.get("reasons") or []))
check("证据含 Delta 背离", any("Delta" in r for r in s.get("reasons") or []))

# ── 2. 规则 B · 诱空插针（镜像）：长下影刺破前低收回 + 放量 + Delta 正 ──
bars2 = bars_flat(30)
bars2.append(bar(30, open=100.0, high=100.1, low=97.6, close=99.9,
                 volume=300.0, taker_buy=210.0))  # delta = +120
sigs2 = jtd.detect_traps(bars2)
bears2 = by_type(sigs2, "bear_trap")
check("诱空插针命中", len(bears2) == 1, f"bear={len(bears2)}")
check("诱空 price=杀跌点", bears2 and bears2[0]["price"] == 97.6, str(bears2 and bears2[0]["price"]))
check("诱空建议提示不追空", bears2 and "追空" in bears2[0]["suggestion"])

# ── 3. 规则 A · 假突破回落：收盘突破前高、量能不足，2 根内收回 ──
bars3 = bars_flat(30)
bars3.append(bar(30, open=100.2, high=101.2, low=100.0, close=101.0,
                 volume=80.0, taker_buy=40.0))   # 收盘站上前高 100.7，量仅 0.8 倍
bars3.append(bar(31, open=100.8, high=100.9, low=99.9, close=100.0,
                 volume=100.0, taker_buy=50.0))  # 收盘收回破位下方
sigs3 = jtd.detect_traps(bars3)
bulls3 = by_type(sigs3, "bull_trap")
check("假突破回落命中", len(bulls3) >= 1, f"bull={len(bulls3)}")
check("证据含突破后收回", bulls3 and any("收回" in r or "没有下文" in r for r in bulls3[0]["reasons"]))
check("证据含量能不足", bulls3 and any("量能仅均量" in r for r in bulls3[0]["reasons"]))

# ── 4. 规则 C1 · 放量滞涨（新高区量爆推不动） ──
bars4 = bars_flat(30)
bars4.append(bar(30, open=100.8, high=100.9, low=100.0, close=100.6,
                 volume=300.0, taker_buy=150.0))
sigs4 = jtd.detect_traps(bars4)
bulls4 = by_type(sigs4, "bull_trap")
check("放量滞涨命中", len(bulls4) == 1, f"bull={len(bulls4)}")
check("证据含放量滞涨", bulls4 and any("放量滞涨" in r for r in bulls4[0]["reasons"]))

# ── 5. 规则 C2 · 缩量拉升（创新高但无承接） ──
bars5 = bars_flat(30)
bars5.append(bar(30, open=100.4, high=101.0, low=100.3, close=100.9,
                 volume=60.0, taker_buy=30.0))
sigs5 = jtd.detect_traps(bars5)
bulls5 = by_type(sigs5, "bull_trap")
check("缩量拉升命中", len(bulls5) == 1, f"bull={len(bulls5)}")
check("证据含缩量拉升", bulls5 and any("缩量拉升" in r for r in bulls5[0]["reasons"]))

# ── 6. 规则 D · Delta 背离单独成立（无插针/无量能异常/未收盘突破） ──
bars6 = bars_flat(30)
bars6.append(bar(30, open=100.0, high=101.5, low=100.0, close=100.65,
                 volume=100.0, taker_buy=20.0))  # delta = -60，量能常规
sigs6 = jtd.detect_traps(bars6)
bulls6 = by_type(sigs6, "bull_trap")
check("Delta 背离单独命中", len(bulls6) == 1, f"bull={len(bulls6)}")
check("Delta 证据措辞", bulls6 and any("Delta 背离" in r for r in bulls6[0]["reasons"]))

# ── 7. 负样本：放量强突破 + 后续站稳 = 真突破，不得误报 ──
bars7 = bars_flat(30)
bars7.append(bar(30, open=100.2, high=101.8, low=100.1, close=101.7,
                 volume=250.0, taker_buy=200.0))
bars7.append(bar(31, open=101.6, high=102.0, low=101.4, close=101.9,
                 volume=150.0, taker_buy=100.0))
bars7.append(bar(32, open=101.9, high=102.3, low=101.7, close=102.1,
                 volume=150.0, taker_buy=100.0))
sigs7 = jtd.detect_traps(bars7)
check("真突破不误报", len(sigs7) == 0, str([s["type"] for s in sigs7]))

# ── 8. 数据不足降级 ──
check("数据不足返回空", jtd.detect_traps(bars_flat(10)) == [])

# ── 9. taker_buy 缺失：规则 D 静默关闭，其余规则照常 ──
bars9 = bars_flat(30)
bars9.append(bar(30, open=100.0, high=102.4, low=99.9, close=100.1, volume=300.0))
for b in bars9:
    b.pop("taker_buy", None)
sigs9 = jtd.detect_traps(bars9)
check("无 taker_buy 仍可检测插针", len(by_type(sigs9, "bull_trap")) == 1)
check("无 taker_buy 不产 Delta 证据",
      all("Delta" not in r for s in sigs9 for r in s["reasons"]))

# ── 10. 邻近同类型去重（6 根内保留置信度最高的一个） ──
bars10 = bars_flat(30)
bars10.append(bar(30, open=100.0, high=102.4, low=99.9, close=100.1,
                  volume=300.0, taker_buy=90.0))
bars10.append(bar(31))  # 横盘间隔
bars10[-1].update(open=100.2, high=100.7, low=99.7, close=99.8)
bars10.append(bar(32, open=100.0, high=103.5, low=99.9, close=100.2,
                  volume=320.0, taker_buy=95.0))
sigs10 = jtd.detect_traps(bars10)
check("邻近同类型去重", len(by_type(sigs10, "bull_trap")) == 1,
      f"bull={len(by_type(sigs10, 'bull_trap'))}")

# ── 11. 契约字段（B2 前端 trapSignals.ts 口径） ──
REQUIRED = {"id", "ts", "price", "type", "confidence", "reasons", "suggestion"}
all_sigs = sigs + sigs2 + sigs3 + sigs4 + sigs5 + sigs6
check("契约字段齐备", all(REQUIRED <= set(s.keys()) for s in all_sigs))
check("confidence ∈ (0, 0.95]",
      all(0 < s["confidence"] <= 0.95 for s in all_sigs))
check("type 枚举合法", all(s["type"] in ("bull_trap", "bear_trap") for s in all_sigs))
check("reasons 非空中文列表",
      all(isinstance(s["reasons"], list) and s["reasons"] for s in all_sigs))
check("信号按 ts 升序", all(list(x["ts"] for x in ss) == sorted(x["ts"] for x in ss)
                        for ss in (sigs3, sigs10)))

# ── 12. mock：确定性 + 封套契约 + 双向样例 ──
m1 = jtd.mock_signals("BTCUSDT", "15m")
m2 = jtd.mock_signals("BTCUSDT", "15m")
check("mock 幂等", m1 == m2)
check("mock 封套字段", all(k in m1 for k in ("ok", "symbol", "interval", "signals", "mock")))
check("mock ok/mock 标记", m1["ok"] is True and m1["mock"] is True)
check("mock 回声", m1["symbol"] == "BTCUSDT" and m1["interval"] == "15m")
check("mock 含诱多+诱空",
      by_type(m1["signals"], "bull_trap") and by_type(m1["signals"], "bear_trap"),
      str([s["type"] for s in m1["signals"]]))

# ── 13. 提醒中心链路（注入假模块，不写真实 DB） ──
calls = {"add": [], "dispatch": []}
fake = types.ModuleType("jarvis_alert_center")
fake.add_event = lambda **kw: (calls["add"].append(kw) or {"id": 1, **kw})
fake.dispatch = lambda event, settings=None, dry_run=False: calls["dispatch"].append(event)
sys.modules["jarvis_alert_center"] = fake

fresh_sig = dict(sigs[0])
NOW = 1_800_000_000.0
fresh_sig["ts"] = int(NOW - 100)  # 最近一根内 = 新鲜
jtd._last_alert.clear()
ev = jtd.maybe_alert_traps("BTCUSDT", "15m", [fresh_sig], now=NOW)
check("新鲜信号落提醒", ev is not None and len(calls["add"]) == 1)
check("事件 kind=trap_signal", calls["add"] and calls["add"][0]["kind"] == "trap_signal")
check("事件 severity=warning", calls["add"] and calls["add"][0]["severity"] == "warning")
check("标题含诱多陷阱", calls["add"] and "诱多陷阱" in calls["add"][0]["title"])
check("渠道分发已触发", len(calls["dispatch"]) == 1)

ev2 = jtd.maybe_alert_traps("BTCUSDT", "15m", [fresh_sig], now=NOW + 60)
check("冷却期内节流", ev2 is None and len(calls["add"]) == 1)

jtd._last_alert.clear()
stale_sig = dict(fresh_sig)
stale_sig["ts"] = int(NOW - 10_000)  # 远早于最近 2 根
ev3 = jtd.maybe_alert_traps("BTCUSDT", "15m", [stale_sig], now=NOW)
check("历史信号不打扰", ev3 is None and len(calls["add"]) == 1)
check("空信号不打扰", jtd.maybe_alert_traps("BTCUSDT", "15m", [], now=NOW) is None)

print("---")
print("ALL PASS" if not fails else f"FAILED: {fails}")
raise SystemExit(0 if not fails else 1)
