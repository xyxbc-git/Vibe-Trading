#!/usr/bin/env python3
"""走势预测引擎（jarvis_trend_predict）离线 smoketest：合成 K 线，不联网。"""

from __future__ import annotations

import math
import random
import time

import pandas as pd

import jarvis_trend_predict as jtp

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


def synth_klines(n: int = 400, seed: int = 7, trend: float = 0.0,
                 step_sec: int = 900) -> pd.DataFrame:
    """合成 fetch_klines_df 同构 DataFrame（time/open/high/low/close/volume）。"""
    rng = random.Random(seed)
    t0 = int(time.time() // step_sec * step_sec - n * step_sec) * 1000
    rows, price = [], 60000.0
    for i in range(n):
        o = price
        price = max(1.0, price * (1 + trend + rng.gauss(0, 0.004)))
        c = price
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.001)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.001)))
        rows.append({"time": t0 + i * step_sec * 1000, "open": o, "high": hi,
                     "low": lo, "close": c, "volume": abs(rng.gauss(100, 30))})
    return pd.DataFrame(rows)


# ── 1. 概率映射：对称 / 求和 / 单调 ─────────────────────────────────────
p0 = jtp.probability_from_score(0.0)
check("score=0 对称", abs(p0["up"] - p0["down"]) < 1e-6, str(p0))
check("score=0 概率和=1", abs(sum(p0.values()) - 1.0) < 1e-3, str(p0))
pu, pd_ = jtp.probability_from_score(0.8), jtp.probability_from_score(-0.8)
check("正分偏涨", pu["up"] > pu["down"] and pu["up"] > pu["sideways"], str(pu))
check("负分偏跌", pd_["down"] > pd_["up"], str(pd_))
check("镜像对称", abs(pu["up"] - pd_["down"]) < 1e-6)
check("强分横盘概率更低",
      jtp.probability_from_score(0.9)["sideways"] < p0["sideways"])

# ── 2. rule_predict 契约完整性 ──────────────────────────────────────────
df = synth_klines(400, trend=0.001)
r = jtp.rule_predict(df, "BTCUSDT", "15m", 16)
check("规则轨 ok", r.get("ok") is True, str(r)[:150])
CONTRACT = ("symbol", "timeframe", "generatedAt", "horizon", "direction",
            "probability", "targetZone", "path", "confidence", "rationale",
            "signals", "disclaimer", "engine", "mock")
check("契约字段齐全", all(k in r for k in CONTRACT),
      str([k for k in CONTRACT if k not in r]))
check("direction 合法", r["direction"] in ("up", "down", "sideways"), r.get("direction"))
check("probability 和=1", abs(sum(r["probability"].values()) - 1.0) < 1e-3,
      str(r["probability"]))
check("path 长度=horizon", len(r["path"]) == 16, str(len(r["path"])))
check("path 元素含 t/price",
      all(("t" in p and "price" in p and p["price"] > 0) for p in r["path"]))
check("targetZone 有序", r["targetZone"]["low"] < r["targetZone"]["high"],
      str(r["targetZone"]))
check("confidence ∈ [0,1]", 0.0 <= r["confidence"] <= 1.0, str(r["confidence"]))
check("免责声明非空", bool(r["disclaimer"]))
check("engine=rule 且非 mock", r["engine"] == "rule" and r["mock"] is False)
check("path 时间为 ISO8601Z", r["path"][0]["t"].endswith("Z"), r["path"][0]["t"])

# ── 3. 上升趋势数据 → 不应显著看跌 ──────────────────────────────────────
df_up = synth_klines(400, seed=11, trend=0.003)
r_up = jtp.rule_predict(df_up, "BTCUSDT", "15m", 16)
check("强上升趋势 up≥down", r_up["probability"]["up"] >= r_up["probability"]["down"],
      str(r_up["probability"]))

# ── 4. 数据不足优雅降级 ─────────────────────────────────────────────────
r_short = jtp.rule_predict(synth_klines(30), "BTCUSDT", "15m", 16)
check("数据不足 ok=False + error", r_short.get("ok") is False and "不足" in r_short.get("error", ""),
      str(r_short))
r_none = jtp.rule_predict(None, "BTCUSDT", "15m", 16)
check("df=None 不抛出", r_none.get("ok") is False)

# ── 5. 参数规范化 ───────────────────────────────────────────────────────
r_bad = jtp.rule_predict(df, "eth-usdt", "7m", 9999)
check("symbol 规范化", r_bad["symbol"] == "ETHUSDT", r_bad["symbol"])
check("非法 tf 回退 15m", r_bad["timeframe"] == "15m", r_bad["timeframe"])
check("horizon 夹紧", r_bad["horizon"] == jtp.HORIZON_MAX, str(r_bad["horizon"]))

# ── 6. mock：确定性 + 契约一致 ──────────────────────────────────────────
m1 = jtp.mock_predict("BTCUSDT", "15m", 16)
m2 = jtp.mock_predict("BTCUSDT", "15m", 16)
check("mock 标记", m1["mock"] is True and m1["engine"] == "mock")
check("mock 契约字段齐全", all(k in m1 for k in CONTRACT))
m1b = {k: v for k, v in m1.items() if k != "generatedAt"}
m2b = {k: v for k, v in m2.items() if k != "generatedAt"}
check("mock 确定性（同参同输出）", m1b == m2b)
check("mock 概率和=1", abs(sum(m1["probability"].values()) - 1.0) < 1e-3)
check("mock path 长度", len(m1["path"]) == 16)

# ── 7. AI 轨护栏：monkeypatch 验证夹紧与降级 ────────────────────────────
import jarvis_llm_config as jlc

_orig_chat = jlc.chat
base = jtp.rule_predict(df, "BTCUSDT", "15m", 16)
digest = jtp._market_digest(df, __import__("jarvis_twelve_systems").analyze(df), base)

jlc.chat = lambda *a, **k: None  # 未配置/失败 → None
check("LLM 失败降级 None", jtp.llm_refine(base, digest) is None)

jlc.chat = lambda *a, **k: '{"probability": {"up": 0.99, "down": 0.005, "sideways": 0.005}, "rationale": "测试研判理由，引用道氏结构与ATR通道，概率仅供参考。"}'
ref = jtp.llm_refine(base, digest)
check("LLM 修正返回结构", ref is not None and "probability" in ref and "rationale" in ref)
if ref:
    drift_ok = all(abs(ref["probability"][k] - base["probability"][k]) <= 0.15 + 0.02
                   for k in ("up", "down", "sideways"))
    check("概率修正夹在 ±0.15 内", drift_ok,
          f"base={base['probability']} refined={ref['probability']}")
    check("修正后概率和=1", abs(sum(ref["probability"].values()) - 1.0) < 1e-3)

jlc.chat = lambda *a, **k: "这不是JSON"
check("坏 JSON 降级 None", jtp.llm_refine(base, digest) is None)

jlc.chat = _orig_chat

# ── 8. 回测：注入合成数据（不联网）────────────────────────────────────
bt = jtp.backtest("BTCUSDT", "15m", 16, windows=12, df=synth_klines(480, seed=3))
check("回测 ok", bt.get("ok") is True, str(bt)[:150])
if bt.get("ok"):
    check("回测窗口>0", bt["windows"] > 0, str(bt["windows"]))
    check("命中率 ∈ [0,1]", 0.0 <= bt["direction_hit_rate"] <= 1.0,
          str(bt["direction_hit_rate"]))
    check("覆盖率 ∈ [0,1]", 0.0 <= bt["zone_coverage"] <= 1.0, str(bt["zone_coverage"]))
    check("分方向明细结构", all(k in bt["by_prediction"] for k in ("up", "down", "sideways")))
    check("回测含口径说明", "basis" in bt and bool(bt["basis"]))

bt_short = jtp.backtest("BTCUSDT", "15m", 16, windows=12, df=synth_klines(60))
check("回测数据不足降级", bt_short.get("ok") is False, str(bt_short)[:100])

# ── 9. 实际三分类死区 ───────────────────────────────────────────────────
check("大涨判 up", jtp._actual_class(0.05, 0.004, 16) == "up")
check("大跌判 down", jtp._actual_class(-0.05, 0.004, 16) == "down")
check("微动判 sideways", jtp._actual_class(0.001, 0.004, 16) == "sideways")

print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
raise SystemExit(1 if FAIL else 0)
