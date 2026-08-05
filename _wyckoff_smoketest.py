#!/usr/bin/env python3
"""威科夫阶段引擎（jarvis_wyckoff）离线 smoketest。

不联网：detect_range / detect_events / resolve_phase / enrich 均为纯函数，
直接喂合成 bars；12 事件每个至少断言 1 例；含防前瞻一致性与 <50ms 性能断言。
"""

from __future__ import annotations

import time

import jarvis_wyckoff as jw

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name} {extra}")


NOW_MS = int(time.time() * 1000)
TF_MS = 3_600_000  # 1h


def _seal(rows: list[tuple]) -> list[dict]:
    """(o,h,l,c,v[,tb]) 元组序列 → bars dict（ts 递增，taker_buy 缺省 0.5×v）。"""
    out = []
    n = len(rows)
    for i, r in enumerate(rows):
        o, h, l, c, v = r[:5]
        tb = r[5] if len(r) > 5 else 0.5 * v
        out.append({"ts": NOW_MS - (n - i) * TF_MS, "open": float(o), "high": float(h),
                    "low": float(l), "close": float(c), "volume": float(v),
                    "taker_buy": float(tb)})
    return out


def _wave(closes: list[float], prev_close: float, vol: float = 100.0,
          wick: float = 0.4, tb_ratio: float = 0.5) -> list[tuple]:
    """连续波段：open=前收，高低=实体±wick。"""
    rows = []
    pc = prev_close
    for c in closes:
        o = pc
        rows.append((o, max(o, c) + wick, min(o, c) - wick, c, vol, tb_ratio * vol))
        pc = c
    return rows


def acc_bars(markup: bool = True, tail: int = 0) -> list[dict]:
    """吸筹全剧本：前段高位宽震 → SC(40) → AR(43) → ST(53) → 区间震荡 →
    Spring(119/120 确认) → Test(125) → SOS(128) → LPS(130) → markup 离场。
    tail>0 时在尾部追加区间内盘整（供回测样本窗口）。"""
    rows: list[tuple] = []
    # 0-39 高位宽幅震荡（inside-ratio 会淘汰含这段的候选区间）
    pre = [110, 111.5, 113, 114.5, 113.5, 112, 110.5, 109.5] * 5
    rows += _wave(pre[:40], 110.5)
    # 40 SC：大阴 + 巨量 + 收在下 1/3 之外（长下影承接）
    rows.append((109.5, 110.0, 93.0, 98.8, 320))
    # 41-44 AR 反弹（43 高点 101.6 ≥ SC.low+0.5×17）
    rows.append((98.8, 99.2, 96.0, 96.2, 110))
    rows.append((96.2, 99.0, 95.8, 98.5, 105))
    rows.append((98.5, 101.6, 98.3, 100.5, 120))
    rows.append((100.5, 100.8, 99.0, 99.5, 100))
    # 45-52 回落漂移
    rows += _wave([98.5, 97.8, 97.0, 96.4, 96.0, 95.7, 95.4, 95.0], 99.5)
    # 53 ST：回踩 SC 低点缩量
    rows.append((94.0, 94.9, 93.3, 94.5, 60))
    # 54-118 区间震荡 65 根（swing 高点簇 101.4 / 低点簇 94.6）
    cyc = [95.0, 96.5, 98.0, 99.5, 101.0, 99.5, 97.5, 95.5]
    wave = (cyc * 9)[:65]
    rows += _wave(wave, 94.5)
    # 119 Spring 下破（低量无供给跟随）→ 120 收回确认
    rows.append((94.8, 94.9, 90.7, 92.5, 70))
    rows.append((92.5, 95.3, 92.3, 95.0, 110))
    # 121-124 企稳
    rows += _wave([95.2, 95.6, 95.1, 94.9], 95.0)
    # 125 Test：缩量回踩不破 Spring 低点
    rows.append((94.9, 95.1, 93.6, 95.0, 45))
    # 126-127 抬升
    rows += _wave([95.8, 96.9], 95.0)
    # 128 SOS：放量穿越中轨收高位
    rows.append((97.0, 100.3, 96.9, 99.9, 180))
    # 129 缓回
    rows.append((99.9, 100.0, 98.6, 98.8, 95))
    # 130 LPS：缩量回踩不破中轨
    rows.append((98.8, 98.9, 97.5, 98.4, 45))
    rows.append((98.4, 98.8, 98.2, 98.6, 95))
    if markup:
        rows += _wave([100.6, 102.3, 103.6, 104.8, 105.6, 106.2], 98.6,
                      vol=140, wick=0.5)
    if tail:
        flat = [98.5, 99.2, 99.9, 100.4, 99.8, 99.0] * 6
        rows += _wave(flat[:tail], 98.6)
    return _seal(rows)


def dist_bars(tail: int = 10) -> list[dict]:
    """派发全剧本：低位宽震 → BC(40) → 区间 → UT(90) → UTAD(119/120 确认) →
    SOW(124) → LPSY(128) → markdown 离场 + tail 根低位盘整。"""
    rows: list[tuple] = []
    pre = [80, 81.5, 83, 84.5, 83.5, 82, 80.5, 79.5] * 5
    rows += _wave(pre[:40], 80.5)
    # 40 BC：大阳 + 巨量 + 长上影（收在上 1/3 之外）
    rows.append((81.5, 102.0, 81.0, 94.5, 320))
    # 41-52 自动回落 + 漂移
    rows += _wave([93.5, 94.5, 95.5, 94.8, 94.0, 93.5, 93.2, 94.0, 94.6, 95.2,
                   94.8, 94.2], 94.5)
    # 53-89 区间震荡（swing 高点簇 99.4 / 低点簇 92.6）
    cyc = [93.0, 94.5, 96.0, 97.5, 99.0, 97.5, 95.5, 93.5]
    rows += _wave((cyc * 5)[:37], 94.2, tb_ratio=0.7)
    # 90 UT：单根扫针上破即收回（低量无需求跟随）
    rows.append((98.8, 100.1, 98.5, 99.0, 90, 0.7 * 90))
    # 91-118 继续震荡（CVD 逐步转弱：taker_buy 降至 0.3）
    rows += _wave((cyc * 4)[:24], 99.0, tb_ratio=0.5)
    rows += _wave([94.5, 96.0, 97.8, 99.0], 93.5, tb_ratio=0.3)
    # 119 UTAD 上冲（放量滞涨 + CVD 背离）→ 120 收回确认
    rows.append((99.2, 100.9, 98.9, 99.8, 250, 0.3 * 250))
    rows.append((99.8, 99.9, 97.8, 98.0, 130, 0.3 * 130))
    # 121-123 反弹乏力
    rows += _wave([98.5, 98.2, 97.6], 98.0)
    # 124 SOW：放量跌破中轨收低位
    rows.append((96.3, 96.4, 94.7, 94.9, 190))
    # 125-127 弱反抽（高点递降）
    rows.append((94.9, 96.6, 94.8, 95.8, 95))
    rows.append((95.8, 96.3, 95.2, 95.5, 90))
    rows.append((95.5, 95.9, 95.0, 95.2, 85))
    # 128 LPSY：缩量反抽不过中轨且高点降低
    rows.append((95.2, 96.2, 95.0, 95.4, 45))
    # 129-134 markdown 离场
    rows += _wave([94.0, 92.2, 91.5, 91.0, 90.6, 90.2], 95.4, vol=150, wick=0.5)
    if tail:
        flat = [90.0, 90.4, 89.8, 90.2, 89.6, 90.0] * 4
        rows += _wave(flat[:tail], 90.2)
    return _seal(rows)


def trend_bars(n: int = 130) -> list[dict]:
    """单边趋势（无区间）：单调上行。"""
    return _seal([(100 + 0.5 * i, 100.9 + 0.5 * i, 99.9 + 0.5 * i,
                   100.6 + 0.5 * i, 100) for i in range(n)])


def flat_bars(n: int = 130) -> list[dict]:
    """有区间但无事件：小幅规律震荡，量恒定。"""
    cyc = [99.0, 99.6, 100.2, 100.8, 100.2, 99.6]
    return _seal(_wave((cyc * 30)[:n], 99.0, wick=0.2))


def _types(events: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in events:
        out.setdefault(e["type"], e)
    return out


# ══════════════════ 1. 吸筹剧本：区间 + 7 事件 + 阶段 ══════════════════
BARS_A = acc_bars()
rng_a = jw.detect_range(BARS_A)
check("acc-区间成立", rng_a is not None, "detect_range 返回 None")
if rng_a:
    check("acc-区间边界（低边 93~95 / 高边 100.5~102）",
          93.0 <= rng_a["low"] <= 95.0 and 100.5 <= rng_a["high"] <= 102.0,
          f"low={rng_a['low']} high={rng_a['high']}")
    check("acc-区间宽度纪律 (high-low)/ATR ≤ 8",
          (rng_a["high"] - rng_a["low"]) / rng_a["atr"] <= 8.0,
          f"width/atr={(rng_a['high'] - rng_a['low']) / rng_a['atr']:.2f}")

EV_A = jw.detect_events(BARS_A, rng_a)
TY_A = _types(EV_A)
for typ, why in (("sc", "恐慌抛售"), ("ar", "自动反弹"), ("st", "二次测试"),
                 ("spring", "弹簧"), ("test", "弹簧后测试"), ("sos", "强势信号"),
                 ("lps", "最后支撑")):
    check(f"acc-事件 {typ}（{why}）", typ in TY_A, f"实际={sorted(TY_A)}")
check("acc-Spring 确认在收回根（防前瞻）",
      "spring" in TY_A and TY_A["spring"]["bar_idx"] == 120,
      f"bar_idx={TY_A.get('spring', {}).get('bar_idx')}")
check("acc-Spring 标注价为扫针低点", "spring" in TY_A
      and abs(TY_A["spring"]["price"] - 90.7) < 1e-6,
      f"price={TY_A.get('spring', {}).get('price')}")
check("acc-raw_score 全部在 [0.6, 1.0]",
      all(0.6 <= e["raw_score"] <= 1.0 for e in EV_A),
      str([(e['type'], e['raw_score']) for e in EV_A]))

st_a = jw.resolve_phase(EV_A, rng_a, last_close=BARS_A[-1]["close"])
check("acc-阶段 side=acc", st_a["side"] == "acc", str(st_a))
check("acc-阶段 phase=E（LPS 后已离开区间）", st_a["phase"] == "E", str(st_a))
check("acc-无跳档 confidence=high", st_a["confidence"] == "high",
      f"skipped={st_a['skipped']}")

st_a2 = jw.resolve_phase(EV_A, rng_a)  # 不给最新收盘价 → 不能确认离场
check("acc-缺 last_close 时 E 档回落 D（防前瞻）", st_a2["phase"] == "D", str(st_a2))

# ══════════════════ 2. 派发剧本：5 事件 + 阶段 ══════════════════
BARS_D = dist_bars()
rng_d = jw.detect_range(BARS_D)
check("dist-区间成立", rng_d is not None, "detect_range 返回 None")
EV_D = jw.detect_events(BARS_D, rng_d)
TY_D = _types(EV_D)
for typ, why in (("bc", "抢购高潮"), ("ut", "上冲回落"), ("utad", "派发上冲"),
                 ("sow", "弱势信号"), ("lpsy", "最后供给")):
    check(f"dist-事件 {typ}（{why}）", typ in TY_D, f"实际={sorted(TY_D)}")
check("dist-UTAD 含 CVD 背离加分（raw ≥ 0.8）",
      "utad" in TY_D and TY_D["utad"]["raw_score"] >= 0.8,
      f"raw={TY_D.get('utad', {}).get('raw_score')}")

st_d = jw.resolve_phase(EV_D, rng_d, last_close=BARS_D[-1]["close"])
check("dist-阶段 side=dist", st_d["side"] == "dist", str(st_d))
check("dist-阶段 phase=E（LPSY 后已离开区间）", st_d["phase"] == "E", str(st_d))

# ══════════════════ 3. 趋势段 / 无事件区间 ══════════════════
check("trend-无区间返回 None", jw.detect_range(trend_bars()) is None)
st_t = jw.resolve_phase([], None)
check("trend-resolve side=trend", st_t["side"] == "trend" and st_t["phase"] is None,
      str(st_t))

BARS_F = flat_bars()
rng_f = jw.detect_range(BARS_F)
EV_F = jw.detect_events(BARS_F, rng_f)
check("flat-区间成立但无事件", rng_f is not None and EV_F == [],
      f"rng={bool(rng_f)} events={[e['type'] for e in EV_F]}")
st_f = jw.resolve_phase(EV_F, rng_f, last_close=BARS_F[-1]["close"])
check("flat-side=unknown", st_f["side"] == "unknown", str(st_f))

# ══════════════════ 4. 历史不足 ══════════════════
check("样本不足（<120 根）区间返回 None",
      jw.detect_range(acc_bars()[:100]) is None)

# ══════════════════ 5. enrich 证据绑定（T2.4 公式） ══════════════════
ev_spring = {"type": "spring", "ts": 1, "price": 90.7, "bar_idx": 96,
             "raw_score": 0.6, "side": "acc"}
ev_utad = {"type": "utad", "ts": 2, "price": 100.9, "bar_idx": 97,
           "raw_score": 0.6, "side": "dist"}
ev_old = {"type": "sc", "ts": 0, "price": 93.0, "bar_idx": 10,
          "raw_score": 0.8, "side": "acc"}
SD_ACC = {"ok": True, "bias": "accumulation", "confidence": 0.6,
          "evidence_chain": [{"direction": 1, "detail": "CVD 吸收"},
                             {"direction": 0, "detail": "无大单数据"}],
          "breakout_check": {"active": False}}
SD_NEU = {"ok": True, "bias": "neutral", "confidence": 0.2,
          "evidence_chain": [], "breakout_check": {"active": False}}

rich = jw.enrich([ev_spring, ev_utad, ev_old], SD_ACC, last_bar_idx=100, window=5)
check("enrich-同向：conf = min(1, raw×(1+0.5×sd)) = 0.78",
      abs(rich[0]["confidence"] - 0.78) < 1e-9, f"conf={rich[0]['confidence']}")
check("enrich-相逆：conf = raw×0.4 = 0.24",
      abs(rich[1]["confidence"] - 0.24) < 1e-9, f"conf={rich[1]['confidence']}")
check("enrich-旧事件不绑定（conf=raw, sd_bias=None）",
      rich[2]["confidence"] == 0.8 and rich[2]["sd_bias"] is None, str(rich[2]))
check("enrich-同向事件带 reasons", rich[0]["reasons"] == ["CVD 吸收"],
      str(rich[0]["reasons"]))
rich_n = jw.enrich([ev_spring], SD_NEU, last_bar_idx=100, window=5)
check("enrich-中性证据不加不减（conf=raw）",
      rich_n[0]["confidence"] == 0.6 and rich_n[0]["sd_bias"] == "neutral",
      str(rich_n[0]))
rich_none = jw.enrich([ev_spring], None, last_bar_idx=100)
check("enrich-无 sd 证据降级（conf=raw）", rich_none[0]["confidence"] == 0.6,
      str(rich_none[0]))

# ══════════════════ 6. 防前瞻一致性（滚动重放口径） ══════════════════
# 全量检出的尾段事件，在「只喂到确认根为止的前缀」上必须同样检出（同 type 同 ts）
lookahead_ok, detail = True, ""
for typ in ("spring", "test", "sos", "lps"):
    e = TY_A[typ]
    seg = BARS_A[:e["bar_idx"] + 1]
    rng_p = jw.detect_range(seg)
    got = [x for x in jw.detect_events(seg, rng_p)
           if x["type"] == typ and x["ts"] == e["ts"]] if rng_p else []
    if not got:
        lookahead_ok, detail = False, f"{typ} 在前缀重放中丢失"
        break
check("防前瞻：尾段事件在前缀重放中可复现（acc×4）", lookahead_ok, detail)

lookahead_ok2, detail2 = True, ""
for typ in ("utad", "sow", "lpsy"):
    e = TY_D[typ]
    seg = BARS_D[:e["bar_idx"] + 1]
    rng_p = jw.detect_range(seg)
    got = [x for x in jw.detect_events(seg, rng_p)
           if x["type"] == typ and x["ts"] == e["ts"]] if rng_p else []
    if not got:
        lookahead_ok2, detail2 = False, f"{typ} 在前缀重放中丢失"
        break
check("防前瞻：尾段事件在前缀重放中可复现（dist×3）", lookahead_ok2, detail2)

# ══════════════════ 7. 性能纪律（bars=500 单次全链 < 50ms） ══════════════════
BARS_PERF = acc_bars(markup=False, tail=36)          # 176 根
while len(BARS_PERF) < 500:                          # 平移复制填满 500 根
    BARS_PERF = BARS_PERF + [
        {**b, "ts": BARS_PERF[-1]["ts"] + (i + 1) * TF_MS}
        for i, b in enumerate(flat_bars(60)[:min(60, 500 - len(BARS_PERF))])]
BARS_PERF = BARS_PERF[:500]
t0 = time.perf_counter()
rng_p = jw.detect_range(BARS_PERF)
ev_p = jw.detect_events(BARS_PERF, rng_p)
jw.resolve_phase(ev_p, rng_p, last_close=BARS_PERF[-1]["close"])
cost_ms = (time.perf_counter() - t0) * 1000
check(f"性能：500 根全链 {cost_ms:.1f}ms < 50ms", cost_ms < 50.0)

print(f"\n{'ALL PASS' if FAIL == 0 else 'FAILED'}  (pass={PASS} fail={FAIL})")
raise SystemExit(0 if FAIL == 0 else 1)
