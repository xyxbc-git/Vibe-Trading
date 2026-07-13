"""离线冒烟：Delta AI 解读卡（纯函数 + mock LLM，不联网）。

覆盖：上下文聚合 / 支撑压力聚类 / prompt 构建 / LLM JSON 解析容错 /
规则轨降级模板 / TTL 缓存（mock LLM 计数）/ 开关关闭 / force 刷新。
"""
import jarvis_delta_explain as jde

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ─── 1. smart_levels_from_ohlc：swing 聚类支撑压力 ───
# 构造：5 轮循环让 95 低点区与 105 高点区各形成 ≥2 个可检测 swing（首尾轮
# 落在 lookback 窗口外不参与检测），现价 100
highs, lows, closes = [], [], []
for cyc in range(5):
    for h, l in [(100, 95), (102, 96), (105, 99), (104, 98), (101, 95.3)]:
        highs.append(h + cyc * 0.1)
        lows.append(l + cyc * 0.1)
        closes.append((h + l) / 2)
closes[-1] = 100.0
lv = jde.smart_levels_from_ohlc(highs, lows, closes)
check("支撑压力聚类：现价上下各得强位",
      lv["support"] is not None and lv["resistance"] is not None
      and lv["support"]["level"] < 100 < lv["resistance"]["level"],
      f"sup={lv['support']} res={lv['resistance']}")
check("触碰次数 ≥2", lv["support"]["touches"] >= 2 and lv["resistance"]["touches"] >= 2)
check("数据不足 → 空位不抛", jde.smart_levels_from_ohlc([1], [1], [1])["support"] is None)
check("空数据 → price None", jde.smart_levels_from_ohlc([], [], [])["price"] is None)

# ─── 2. summarize_context：聚合口径 ───
delta_mock = {
    "ok": True, "symbol": "BTCUSDT", "timeframe": "15m",
    "bars": [{"t": f"T{i}", "delta": (1.0 if i % 3 else -0.5), "cvd": i * 0.8,
              "volume": 10} for i in range(30)],
    "divergence": {"bullish": {"active": True, "note": "价格新低 CVD 抬升", "anchors": []},
                   "bearish": {"active": False, "note": "", "anchors": []}},
    "absorption": {"detected": True, "side": "sell-absorption", "note": "卖压被吸收"},
}
predict_mock = {"ok": True, "probability": {"up": 0.52, "down": 0.18, "sideways": 0.30},
                "targetZone": {"low": 98.0, "high": 106.0}, "rationale": "测试依据"}
ctx = jde.summarize_context(delta_mock, predict_mock, lv, recent_n=24)
check("上下文：Delta 正负根数统计", ctx["delta_recent"]["n"] == 24
      and ctx["delta_recent"]["positive_bars"] + ctx["delta_recent"]["negative_bars"] == 24)
check("上下文：CVD 趋势 up", ctx["delta_recent"]["cvd_trend"] == "up",
      str(ctx["delta_recent"]))
check("上下文：背离/吸收透传", ctx["divergence"]["bullish_active"] is True
      and ctx["absorption"]["detected"] is True)
check("上下文：概率与目标区透传", ctx["predict"]["probability"]["up"] == 0.52
      and ctx["predict"]["target_zone"]["high"] == 106.0)
check("上下文：支撑压力距离已算", ctx["levels"]["support_dist_pct"] is not None
      and ctx["levels"]["resistance_dist_pct"] is not None,
      str(ctx["levels"]))

# ─── 3. build_prompt：结构与数据完整性 ───
msgs = jde.build_prompt(ctx)
check("prompt 双消息（system+user）", len(msgs) == 2 and msgs[0]["role"] == "system"
      and msgs[1]["role"] == "user")
check("system 含输出 JSON schema 指令", "headline" in msgs[0]["content"]
      and "suggestion" in msgs[0]["content"])
check("user 是纯数据 JSON（含币种）", "BTCUSDT" in msgs[1]["content"])

# ─── 4. parse_llm_json：解析容错 ───
good = '{"headline":"h","power":"p","signals":"s","suggestion":"g"}'
check("标准 JSON 解析", jde.parse_llm_json(good) is not None)
fenced = "```json\n" + good + "\n```"
check("```json 围栏容忍", jde.parse_llm_json(fenced) is not None)
check("缺字段 → None（触发降级）",
      jde.parse_llm_json('{"headline":"只有一个字段"}') is None)
check("非 JSON → None", jde.parse_llm_json("我不是JSON") is None)
check("空/None → None", jde.parse_llm_json(None) is None and jde.parse_llm_json("") is None)
check("超长字段截断 300", len(jde.parse_llm_json(
    '{"headline":"' + "长" * 500 + '","power":"p","signals":"s","suggestion":"g"}'
)["headline"]) == 300)

# ─── 5. rule_explain：降级模板三分支 ───
r = jde.rule_explain(ctx)
check("规则轨四字段齐全", all(r.get(k) for k in
                              ("headline", "power", "signals", "suggestion")))
check("吸收现象进 signals", "吸收" in r["signals"], r["signals"][:40])
ctx_bear = jde.summarize_context({
    **delta_mock,
    "bars": [{"t": f"T{i}", "delta": -1.0, "cvd": -i * 0.8, "volume": 10}
             for i in range(30)],
    "divergence": {"bullish": {"active": False, "note": "", "anchors": []},
                   "bearish": {"active": True, "note": "价格新高 CVD 走弱", "anchors": []}},
    "absorption": {"detected": False, "side": None, "note": ""},
}, {"ok": True, "probability": {"up": 0.15, "down": 0.55, "sideways": 0.30},
    "targetZone": None, "rationale": ""}, lv)
r_bear = jde.rule_explain(ctx_bear)
check("卖方占优 → 观望/等反弹口径", "卖方" in r_bear["power"]
      and ("观望" in r_bear["suggestion"] or "反弹" in r_bear["suggestion"]),
      r_bear["suggestion"][:50])
ctx_flat = jde.summarize_context({
    **delta_mock,
    "bars": [{"t": f"T{i}", "delta": (1 if i % 2 else -1), "cvd": 0.0, "volume": 10}
             for i in range(30)],
    "divergence": {"bullish": {"active": False, "note": "", "anchors": []},
                   "bearish": {"active": False, "note": "", "anchors": []}},
    "absorption": {"detected": False, "side": None, "note": ""},
}, None, {"price": 100.0, "support": None, "resistance": None})
r_flat = jde.rule_explain(ctx_flat)
check("多空拉锯 → 观望", "观望" in r_flat["suggestion"], r_flat["suggestion"][:40])
check("无异常信号文案", "无" in r_flat["signals"], r_flat["signals"][:40])

# ─── 6. explain() 门面：mock LLM + 缓存 + 降级 + force ───
calls = {"n": 0}


def mock_llm_ok(msgs):
    calls["n"] += 1
    return good


def mock_llm_fail(msgs):
    calls["n"] += 1
    return None


# monkeypatch 数据源（离线原则：不联网）：delta 用确定性 mock；
# predict / K 线拉取直接短路（predict 返回 mock、fetch 返回 None → levels 降级）
import jarvis_delta_flow as jdf
import jarvis_trend_predict as jtp
import jarvis_twelve_systems as jts

_orig_analyze = jdf.analyze
_orig_predict = jtp.predict
_orig_fetch = jts.fetch_klines_df
jdf.analyze = lambda s, tf, n: delta_mock
jtp.predict = lambda s, tf, h, use_llm=False: predict_mock
jts.fetch_klines_df = lambda s, tf, n: None

try:
    jde.clear_cache()
    calls["n"] = 0
    out1 = jde.explain("BTCUSDT", "15m", llm_chat=mock_llm_ok)
    check("LLM 轨成功：source=llm", out1["ok"] and out1["source"] == "llm"
          and out1["cached"] is False, f"src={out1.get('source')}")
    out2 = jde.explain("BTCUSDT", "15m", llm_chat=mock_llm_ok)
    check("TTL 缓存命中：LLM 只调 1 次", out2.get("cached") is True and calls["n"] == 1,
          f"calls={calls['n']}")
    out3 = jde.explain("BTCUSDT", "15m", force=True, llm_chat=mock_llm_ok)
    check("force=1 跳过缓存重算", out3.get("cached") is False and calls["n"] == 2,
          f"calls={calls['n']}")

    jde.clear_cache()
    out4 = jde.explain("BTCUSDT", "15m", llm_chat=mock_llm_fail)
    check("LLM 失败 → 规则轨降级 source=rule", out4["ok"] and out4["source"] == "rule",
          f"src={out4.get('source')}")
    check("降级解读四字段齐全", all(out4["explain"].get(k) for k in
                                    ("headline", "power", "signals", "suggestion")))

    # 开关关闭：monkeypatch 配置读取
    _orig_cfg = jde._cfg_get
    jde._cfg_get = lambda k, fb: (False if k == "ai_explain_enabled"
                                  else _orig_cfg(k, fb))
    out5 = jde.explain("BTCUSDT", "15m", llm_chat=mock_llm_ok)
    check("开关关闭 → disabled 不抛错", out5.get("ok") is False
          and out5.get("disabled") is True)
    jde._cfg_get = _orig_cfg

    # delta 未就绪：ok=False 透传错误
    jdf.analyze = lambda s, tf, n: {"ok": False, "error": "K线拉取失败"}
    jde.clear_cache()
    out6 = jde.explain("BTCUSDT", "15m", llm_chat=mock_llm_ok)
    check("Delta 未就绪 → ok=False 带错误", out6.get("ok") is False
          and "K线" in (out6.get("error") or ""))
finally:
    jdf.analyze = _orig_analyze
    jtp.predict = _orig_predict
    jts.fetch_klines_df = _orig_fetch
    jde.clear_cache()

# ─── 7. 配置键三处落位 ───
import jarvis_config as jc

check("ai_explain_enabled 键落位", jc.get("ai_explain_enabled") is True
      and "ai_explain_enabled" in jc.GROUPS)
check("ai_explain_cache_min 键落位（默认5+夹紧）", jc.get("ai_explain_cache_min") == 5
      and jc.clamp("ai_explain_cache_min", 999) == 120
      and jc.clamp("ai_explain_cache_min", 0) == 1)

print("\n=== " + ("全部通过" if not fails else f"失败 {len(fails)}: {fails}") + " ===")
raise SystemExit(1 if fails else 0)
