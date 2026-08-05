#!/usr/bin/env python3
"""量价核对「主力底牌」裁决器（jarvis_supply_demand）离线 smoketest。

不联网：verdict / breakout_check 均为纯函数，直接喂打桩证据；
collect_evidence 的降级路径用 monkeypatch fetch_bars 验证。
"""

from __future__ import annotations

import time

import jarvis_delta_flow as jdf
import jarvis_supply_demand as jsd

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
TF_MS = 900_000  # 15m


def make_bars(n: int = 40, base: float = 100.0) -> list[dict]:
    """横盘合成序列：close=base，量恒定 1000，taker_buy 均衡（delta=0）。"""
    out = []
    for i in range(n):
        ts = NOW_MS - (n - i) * TF_MS
        out.append({"ts": ts, "open": base, "high": base + 0.5, "low": base - 0.5,
                    "close": base, "volume": 1000.0, "taker_buy": 500.0})
    return out


def full_ev(**overrides) -> dict:
    """六路全可用的吸筹倾向证据模板（各路可被 overrides 替换/置 None）。"""
    bars = make_bars()
    ev = {
        "symbol": "TESTUSDT", "interval": "15m",
        "bars": bars,
        "trap": {"signals": [{"type": "bear_trap", "ts": bars[-1]["ts"] / 1000.0,
                              "price": 99.0, "confidence": 0.9,
                              "reasons": ["合成诱空信号"]}]},
        "cvd": {"rows": [], "absorption": {"detected": True, "side": "sell-absorption",
                                           "note": "卖压被吸收（吸收信号强）"}},
        "whale": {"net_usd": 400_000.0, "tier1_usd": 100_000.0, "window_min": 15},
        "book": {"imbalance": {"bid_usd_10": 4000.0, "ask_usd_10": 1000.0, "ratio": 4.0}},
        "vp": {"poc": 100.0, "vah": 101.0, "val": 99.0, "close": 98.0},
    }
    ev.update(overrides)
    paths = (ev["bars"], ev["trap"], ev["cvd"], ev["whale"], ev["book"], ev["vp"])
    ev["coverage"] = round(sum(1 for p in paths if p is not None) / 6.0, 3)
    return ev


# ── 1. 吸筹裁决（六路全正向） ──────────────────────────────────────────
v = jsd.verdict(full_ev())
check("吸筹-bias", v["bias"] == "accumulation", str(v["bias"]))
check("吸筹-score>0.35", v["score"] > 0.35, str(v["score"]))
check("吸筹-置信=|score|×coverage",
      abs(v["confidence"] - min(1.0, abs(v["score"])) * 1.0) < 1e-6,
      f"conf={v['confidence']} score={v['score']}")
check("吸筹-证据链五路", len(v["evidence_chain"]) == 5)
check("吸筹-链方向全为正",
      all(e["direction"] == 1 for e in v["evidence_chain"]),
      str([(e["source"], e["direction"]) for e in v["evidence_chain"]]))

# ── 2. 派发裁决（镜像） ───────────────────────────────────────────────
bars2 = make_bars()
ev_dist = full_ev(
    trap={"signals": [{"type": "bull_trap", "ts": bars2[-1]["ts"] / 1000.0,
                       "price": 101.0, "confidence": 0.9, "reasons": ["合成诱多信号"]}]},
    cvd={"rows": [], "absorption": {"detected": True, "side": "buy-distribution",
                                    "note": "买盘被派发（派发信号强）"}},
    whale={"net_usd": -400_000.0, "tier1_usd": 100_000.0, "window_min": 15},
    book={"imbalance": {"bid_usd_10": 1000.0, "ask_usd_10": 4000.0, "ratio": 0.25}},
    vp={"poc": 100.0, "vah": 101.0, "val": 99.0, "close": 102.0},
)
v2 = jsd.verdict(ev_dist)
check("派发-bias", v2["bias"] == "distribution", str(v2["bias"]))
check("派发-score<-0.35", v2["score"] < -0.35, str(v2["score"]))

# ── 3. 中性（无信号证据） ─────────────────────────────────────────────
ev_neu = full_ev(
    trap={"signals": []},
    cvd={"rows": [], "absorption": {"detected": False, "side": "none", "note": "无"}},
    whale={"net_usd": 10_000.0, "tier1_usd": 100_000.0, "window_min": 15},
    book={"imbalance": {"ratio": 1.0}},
    vp={"poc": 100.0, "vah": 101.0, "val": 99.0, "close": 100.0},
)
v3 = jsd.verdict(ev_neu)
check("中性-bias", v3["bias"] == "neutral", str(v3["bias"]))
check("中性-|score|小", abs(v3["score"]) < 0.2, str(v3["score"]))

# ── 4. 证据缺失降级（whale/book None → coverage 4/6，方向不稀释） ────────
ev_deg = full_ev(whale=None, book=None)
v4 = jsd.verdict(ev_deg)
check("降级-coverage=4/6", abs(v4["coverage"] - round(4 / 6, 3)) < 1e-6, str(v4["coverage"]))
check("降级-缺失路 weight=0",
      all(e["weight"] == 0.0 for e in v4["evidence_chain"] if e["source"] in ("whale", "book")))
check("降级-bias 仍为吸筹（可用证据间归一）", v4["bias"] == "accumulation", str(v4["bias"]))
check("降级-置信按覆盖度打折", v4["confidence"] < v["confidence"],
      f"{v4['confidence']} vs {v['confidence']}")

# ── 5. 全缺失（数据源全挂） ───────────────────────────────────────────
ev_none = {"symbol": "TESTUSDT", "interval": "15m", "bars": None, "trap": None,
           "cvd": None, "whale": None, "book": None, "vp": None, "coverage": 0.0}
v5 = jsd.verdict(ev_none)
check("全缺失-中性零置信", v5["bias"] == "neutral" and v5["confidence"] == 0.0, str(v5))
check("全缺失-突破 unknown 不激活",
      v5["breakout_check"]["active"] is False and v5["breakout_check"]["verdict"] == "unknown")

# ── 6. 突破核验：confirmed（放量+Delta 同向+CVD 同向极值） ───────────────
bars_up = make_bars(40)
brk = dict(bars_up[-1])
brk.update({"close": 105.0, "high": 105.5, "volume": 3000.0, "taker_buy": 2700.0})
bars_up[-1] = brk
rows_up = jdf.compute_delta_cvd(bars_up)
bc = jsd.breakout_check(bars_up, {"rows": rows_up}, {"signals": []}, None)
check("突破-active+方向 up", bc["active"] and bc["direction"] == "up", str(bc))
check("突破-confirmed", bc["verdict"] == "confirmed", str(bc))
check("突破-理由非空", len(bc["reasons"]) >= 2, str(bc["reasons"]))

# ── 7. 突破核验：suspect（缩量 + Delta 反向） ────────────────────────────
bars_fake = make_bars(40)
fake = dict(bars_fake[-1])
fake.update({"close": 105.0, "high": 105.5, "volume": 500.0, "taker_buy": 100.0})
bars_fake[-1] = fake
rows_fake = jdf.compute_delta_cvd(bars_fake)
bc2 = jsd.breakout_check(bars_fake, {"rows": rows_fake}, {"signals": []}, None)
check("假突破-suspect", bc2["verdict"] == "suspect", str(bc2))
check("假突破-含量能/Delta 理由",
      any("量能" in r or "Delta" in r for r in bc2["reasons"]), str(bc2["reasons"]))

# 7b. 反向大单净流 → suspect
bc2b = jsd.breakout_check(bars_up, {"rows": rows_up}, {"signals": []},
                          {"net_usd": -200_000.0, "tier1_usd": 100_000.0})
check("假突破-反向大单一票嫌疑", bc2b["verdict"] == "suspect", str(bc2b["reasons"]))

# ── 8. 无突破 → 不激活 ───────────────────────────────────────────────
bc3 = jsd.breakout_check(make_bars(40), None, None, None)
check("无突破-不激活", bc3["active"] is False and bc3["verdict"] == "unknown")

# ── 9. 权重覆盖生效 ──────────────────────────────────────────────────
ev_w = full_ev(
    trap={"signals": [{"type": "bull_trap", "ts": make_bars()[-1]["ts"] / 1000.0,
                       "price": 101.0, "confidence": 0.9, "reasons": ["诱多"]}]})
v_default = jsd.verdict(ev_w)
v_trapheavy = jsd.verdict(ev_w, {"trap": 10.0})
check("权重覆盖-trap 加权后 score 下移", v_trapheavy["score"] < v_default["score"],
      f"{v_trapheavy['score']} vs {v_default['score']}")
check("权重覆盖-非法值忽略",
      jsd.verdict(ev_w, {"cvd": "abc"})["ok"] is True)

# ── 10. collect_evidence 降级：取数失败 → 全 None、coverage 0 ────────────
_orig_fetch = jdf.fetch_bars
jdf.fetch_bars = lambda *a, **k: None
ev_c = jsd.collect_evidence("TESTUSDT", "15m")
check("采集-取数失败 coverage 低", ev_c["coverage"] <= round(2 / 6, 3), str(ev_c["coverage"]))
check("采集-bars 依赖路为 None",
      ev_c["bars"] is None and ev_c["trap"] is None and ev_c["cvd"] is None and ev_c["vp"] is None)
jdf.fetch_bars = _orig_fetch

# ── 11. analyze 门面永不抛出 ─────────────────────────────────────────
jdf.fetch_bars = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
out = jsd.analyze("TESTUSDT", "15m")
check("门面-异常不抛出", isinstance(out, dict) and "ok" in out)
jdf.fetch_bars = _orig_fetch

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
