#!/usr/bin/env python3
"""贾维斯 JARVIS — Delta 面板 AI 解读卡（M2 s7-ai-explain）。

用户看不懂 K 线页 Delta/CVD 订单流面板 → 聚合面板同源数据组结构化 prompt，
走已有 LLM 通道（jarvis_llm_config.chat，json_mode）生成大白话解读；
LLM 未配置/失败时降级为规则引擎模板文案（阈值判定，不抛错、不留白）。

聚合上下文（与前端面板同源）：
  - Delta bars 最近 N 根（正/负根数、最新值、CVD 首尾趋势）— jarvis_delta_flow
  - divergence anchors（吸收/派发背离锚点）+ absorption 现象   — 同上
  - 智能支撑/压力位（swing 聚类，与前端 drawings.computeSmartLevels 同思路）
  - AI 走势研判概率（rule 轨，不烧 token）— jarvis_trend_predict
  - 现价

输出结构（LLM 与规则轨同构，前端一套渲染）：
  {headline 一句话结论, power 买卖力量对比, signals 关键信号, suggestion 建议倾向}

缓存：同 symbol+tf 结果缓存 signal.ai_explain_cache_min（默认 5 分钟）——
模块内置 TTL 缓存（不依赖 dashboard._cached，便于 smoketest 直接锁定行为）。
开关：signal.ai_explain_enabled（默认 true；关=返回 disabled，前端隐藏入口）。

设计原则（与 jarvis_sentiment / jarvis_liquidation 同风格）：
  - 纯函数核心（summarize_context / build_prompt / parse_llm_json / rule_explain）
    只吃 dict/list，不联网，smoketest 可 mock 锁定口径
  - explain() 门面负责取数与 LLM 调用；任何异常降级规则轨，绝不 500
"""

from __future__ import annotations

import json
import time

# 兜底默认（生效值走 jarvis_config signal.ai_explain_*）
CACHE_MIN_DEFAULT = 5
ENABLED_DEFAULT = True

# 聚合最近 N 根 Delta bar 进 prompt（再多对解读无增益，徒增 token）
RECENT_BARS = 24

# swing 聚类参数（与前端 drawings.ts 同思路的轻量后端版）
SWING_LOOKBACK = 3          # 左右各 N 根更高/更低才算 swing 点
CLUSTER_TOL_PCT = 0.02      # 聚类容差 = 价格区间 × 该比例
MIN_TOUCHES = 2             # 至少 2 次触碰才算有效支撑/压力

DISCLAIMER = "AI 解读基于面板数据自动生成，为概率性参考而非确定性结论；不构成投资建议。"

_LLM_SYSTEM = (
    "你是一位耐心的加密货币交易助教，对象是完全没有订单流基础的新手。"
    "传入数据是某币种 Delta/CVD 订单流面板的聚合摘要："
    "Delta=每根K线主动买单减主动卖单的量差（正=买方主动，负=卖方主动）；"
    "CVD=Delta 的累计曲线（趋势向上=买方持续占优）；"
    "吸收背离=价格创新低但 CVD 抬升（卖压被大资金接走，可能见底）或反向；"
    "支撑/压力位=历史多次触碰的价格区域；走势概率=系统对后市方向的量化研判。"
    "请用大白话输出 JSON（且只输出 JSON），四个字段："
    '{"headline": "一句话结论（≤40字，先说人话再给方向）",'
    ' "power": "买卖力量对比（≤80字，基于Delta正负与CVD趋势）",'
    ' "signals": "关键信号（≤80字，有背离/吸收就重点讲，没有就说明当前无异常信号）",'
    ' "suggestion": "建议倾向（≤100字，明确三选一：观望/等回调/等突破，'
    "并提醒到支撑与压力的距离决定盈亏比是否划算）\"}"
    "。禁止编造传入数据里没有的数字；语气客观，不喊单。"
)


def _cfg_get(key: str, fallback):
    try:
        import jarvis_config as jc
        v = jc.get(key)
        return fallback if v is None else v
    except Exception:  # noqa: BLE001
        return fallback


# ═══════════════════════════ 纯函数核心 ═══════════════════════════

def smart_levels_from_ohlc(highs: list[float], lows: list[float],
                           closes: list[float]) -> dict:
    """轻量支撑/压力：swing 高低点聚类，取现价上下最近的强位。

    与前端 drawings.computeSmartLevels 同思路（后端独立实现，避免跨端依赖）。
    数据不足返回 {price, support: None, resistance: None}。
    """
    n = len(closes)
    if n == 0:
        return {"price": None, "support": None, "resistance": None}
    price = float(closes[-1])
    if n < SWING_LOOKBACK * 2 + 1:
        return {"price": price, "support": None, "resistance": None}
    swings: list[float] = []
    for i in range(SWING_LOOKBACK, n - SWING_LOOKBACK):
        win_h = highs[i - SWING_LOOKBACK:i + SWING_LOOKBACK + 1]
        win_l = lows[i - SWING_LOOKBACK:i + SWING_LOOKBACK + 1]
        if highs[i] == max(win_h):
            swings.append(float(highs[i]))
        if lows[i] == min(win_l):
            swings.append(float(lows[i]))
    if not swings:
        return {"price": price, "support": None, "resistance": None}
    tol = (max(highs) - min(lows) or 1.0) * CLUSTER_TOL_PCT
    swings.sort()
    clusters: list[dict] = []
    for lv in swings:
        if clusters and lv - clusters[-1]["levels"][-1] <= tol:
            clusters[-1]["levels"].append(lv)
        else:
            clusters.append({"levels": [lv]})
    strong = [{"level": round(sum(c["levels"]) / len(c["levels"]), 6),
               "touches": len(c["levels"])}
              for c in clusters if len(c["levels"]) >= MIN_TOUCHES]
    support = max((c for c in strong if c["level"] < price),
                  key=lambda c: c["level"], default=None)
    resistance = min((c for c in strong if c["level"] >= price),
                     key=lambda c: c["level"], default=None)
    return {"price": price, "support": support, "resistance": resistance}


def summarize_context(delta: dict, predict: dict | None, levels: dict | None,
                      recent_n: int = RECENT_BARS) -> dict:
    """面板原始返回体 → 结构化解读上下文（prompt 的 user 数据 / 规则轨输入）。"""
    bars = list(delta.get("bars") or [])
    recent = bars[-max(1, int(recent_n)):]
    deltas = [float(b.get("delta") or 0) for b in recent]
    cvds = [float(b.get("cvd") or 0) for b in recent]
    pos = sum(1 for d in deltas if d > 0)
    neg = len(deltas) - pos
    cvd_change = round(cvds[-1] - cvds[0], 4) if len(cvds) >= 2 else 0.0
    cvd_trend = ("up" if cvd_change > 0 else "down" if cvd_change < 0 else "flat")

    div = delta.get("divergence") or {}
    bull, bear = div.get("bullish") or {}, div.get("bearish") or {}
    absorption = delta.get("absorption") or {}

    prob = dict((predict or {}).get("probability") or {})
    tz = (predict or {}).get("targetZone") or None

    lv = levels or {}
    sup, res = lv.get("support"), lv.get("resistance")
    price = lv.get("price")
    sup_dist_pct = (round((price - sup["level"]) / price * 100, 2)
                    if price and sup else None)
    res_dist_pct = (round((res["level"] - price) / price * 100, 2)
                    if price and res else None)

    return {
        "symbol": delta.get("symbol"), "timeframe": delta.get("timeframe"),
        "price": price,
        "delta_recent": {"n": len(recent), "positive_bars": pos, "negative_bars": neg,
                         "last_delta": round(deltas[-1], 4) if deltas else None,
                         "cvd_change": cvd_change, "cvd_trend": cvd_trend},
        "divergence": {
            "bullish_active": bool(bull.get("active")),
            "bullish_note": str(bull.get("note") or "")[:160],
            "bearish_active": bool(bear.get("active")),
            "bearish_note": str(bear.get("note") or "")[:160],
        },
        "absorption": {"detected": bool(absorption.get("detected")),
                       "side": absorption.get("side"),
                       "note": str(absorption.get("note") or "")[:160]},
        "predict": {"probability": {k: round(float(v), 3) for k, v in prob.items()},
                    "target_zone": tz,
                    "rationale": str((predict or {}).get("rationale") or "")[:200]},
        "levels": {"support": sup, "resistance": res,
                   "support_dist_pct": sup_dist_pct,
                   "resistance_dist_pct": res_dist_pct},
    }


def build_prompt(ctx: dict) -> list[dict]:
    """上下文 → LLM messages（system 指令 + user 数据 JSON）。prompt 只在后端。"""
    return [{"role": "system", "content": _LLM_SYSTEM},
            {"role": "user",
             "content": json.dumps(ctx, ensure_ascii=False, default=str)}]


_REQUIRED_FIELDS = ("headline", "power", "signals", "suggestion")


def parse_llm_json(text: str | None) -> dict | None:
    """LLM 输出 → 校验四字段齐全的 dict；解析失败/缺字段返回 None（触发降级）。"""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):     # 容忍 ```json 围栏（模型常见输出习惯）
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    out = {}
    for k in _REQUIRED_FIELDS:
        v = obj.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
        out[k] = v.strip()[:300]
    return out


def rule_explain(ctx: dict) -> dict:
    """规则引擎降级解读：基于阈值的模板文案（LLM 失败/未配置时兜底）。"""
    d = ctx.get("delta_recent") or {}
    pos, neg = int(d.get("positive_bars") or 0), int(d.get("negative_bars") or 0)
    total = max(1, pos + neg)
    cvd_trend = d.get("cvd_trend") or "flat"
    buy_ratio = pos / total

    if buy_ratio >= 0.6 and cvd_trend == "up":
        power = (f"最近 {total} 根 K 线中 {pos} 根买方主动，且累计买卖差（CVD）持续抬升"
                 "——买方力量占优。")
        lean = 1
    elif buy_ratio <= 0.4 and cvd_trend == "down":
        power = (f"最近 {total} 根 K 线中 {neg} 根卖方主动，且 CVD 持续下行"
                 "——卖方力量占优。")
        lean = -1
    else:
        power = (f"最近 {total} 根 K 线买卖方各有 {pos}/{neg} 根主动，CVD "
                 f"{'走平' if cvd_trend == 'flat' else '方向与K线分布不一致'}"
                 "——多空拉锯，方向未分。")
        lean = 0

    div = ctx.get("divergence") or {}
    ab = ctx.get("absorption") or {}
    if ab.get("detected"):
        signals = f"检测到吸收现象：{ab.get('note') or '大资金在被动接单'}"
        lean += 1 if str(ab.get("side") or "").startswith("sell") else -1
    elif div.get("bullish_active"):
        signals = f"看涨背离信号：{div.get('bullish_note') or '价格新低但 CVD 抬升'}"
        lean += 1
    elif div.get("bearish_active"):
        signals = f"看跌背离信号：{div.get('bearish_note') or '价格新高但 CVD 走弱'}"
        lean -= 1
    else:
        signals = "当前无吸收/背离异常信号，订单流处于常态。"

    prob = (ctx.get("predict") or {}).get("probability") or {}
    up, down = float(prob.get("up") or 0), float(prob.get("down") or 0)
    lv = ctx.get("levels") or {}
    sup_d, res_d = lv.get("support_dist_pct"), lv.get("resistance_dist_pct")
    rr_hint = ""
    if sup_d is not None and res_d is not None and sup_d > 0:
        rr = round(res_d / sup_d, 2)
        rr_hint = (f"现价距下方支撑 {sup_d}%、距上方压力 {res_d}%（盈亏比约 1:{rr}），"
                   + ("比价合适。" if rr >= 1.5 else "空间不划算，别追。"))

    if lean >= 2 or (lean >= 1 and up >= 0.45):
        suggestion = f"倾向等回调低吸：买方占优但不宜追高，回踩支撑不破再进。{rr_hint}"
    elif lean <= -2 or (lean <= -1 and down >= 0.45):
        suggestion = f"倾向观望或等反弹减仓：卖方占优，别急着抄底。{rr_hint}"
    else:
        suggestion = f"倾向观望：多空未分胜负，等突破支撑/压力区间再跟。{rr_hint}"

    dir_cn = {1: "偏买方", -1: "偏卖方", 0: "多空均衡"}
    headline = (f"订单流{dir_cn.get(max(-1, min(1, lean)), '多空均衡')}，"
                + ("有反转信号需重点关注" if (ab.get("detected")
                   or div.get("bullish_active") or div.get("bearish_active"))
                   else "暂无异常信号")
                + "。")
    return {"headline": headline, "power": power,
            "signals": signals, "suggestion": suggestion}


# ═══════════════════════════ TTL 缓存 + 门面（带 IO） ═══════════════════════════

# {(SYMBOL, tf): (expires_ts, result_dict)}
_CACHE: dict[tuple, tuple] = {}


def _cache_get(key: tuple) -> dict | None:
    hit = _CACHE.get(key)
    if hit and time.time() < hit[0]:
        return hit[1]
    return None


def _cache_put(key: tuple, val: dict, ttl_s: float) -> None:
    _CACHE[key] = (time.time() + max(1.0, ttl_s), val)


def clear_cache() -> None:
    """smoketest / 前端「刷新」强制重算用。"""
    _CACHE.clear()


def explain(symbol: str = "BTCUSDT", timeframe: str = "15m", *,
            force: bool = False, llm_chat=None) -> dict:
    """门面：开关检查 → 缓存 → 聚合上下文 → LLM（失败降级规则轨）。永不抛出。

    force=True 跳过缓存（前端刷新按钮）；llm_chat 依赖注入供 smoketest mock
    （签名同 jarvis_llm_config.chat：messages → str|None）。
    """
    try:
        if not bool(_cfg_get("ai_explain_enabled", ENABLED_DEFAULT)):
            return {"ok": False, "disabled": True,
                    "error": "AI 解读已在配置中关闭（signal.ai_explain_enabled）"}
        sym = (symbol if symbol.upper().endswith(("USDT", "USDC"))
               else symbol + "USDT").upper()
        tf = str(timeframe)
        key = (sym, tf)
        if not force:
            hit = _cache_get(key)
            if hit is not None:
                return {**hit, "cached": True}

        # ── 聚合面板同源数据（delta 必需；predict/levels 可缺省降级）──
        import jarvis_delta_flow as jdf
        delta = jdf.analyze(sym, tf, 200)
        if not delta.get("ok"):
            return {"ok": False, "error": delta.get("error") or "Delta 数据未就绪",
                    "symbol": sym, "timeframe": tf}
        predict = None
        try:
            import jarvis_trend_predict as jtp
            predict = jtp.predict(sym, tf, 16, use_llm=False)  # 规则轨，不烧 token
            if not predict.get("ok"):
                predict = None
        except Exception:  # noqa: BLE001
            predict = None
        levels = None
        try:
            import jarvis_twelve_systems as jts
            df = jts.fetch_klines_df(sym, tf, 120)
            if df is not None and len(df) >= 10:
                levels = smart_levels_from_ohlc(
                    [float(x) for x in df["high"]], [float(x) for x in df["low"]],
                    [float(x) for x in df["close"]])
        except Exception:  # noqa: BLE001
            levels = None

        ctx = summarize_context(delta, predict, levels)

        # ── LLM 轨（json_mode；未配置/失败返回 None）→ 规则轨兜底 ──
        chat = llm_chat
        if chat is None:
            try:
                import jarvis_llm_config as jlc
                chat = lambda msgs: jlc.chat(msgs, json_mode=True, timeout=60,  # noqa: E731
                                             module="delta-explain")
            except Exception:  # noqa: BLE001
                chat = lambda msgs: None  # noqa: E731
        parsed = None
        try:
            parsed = parse_llm_json(chat(build_prompt(ctx)))
        except Exception:  # noqa: BLE001 — LLM 通道异常一律走规则轨
            parsed = None

        out = {"ok": True, "symbol": sym, "timeframe": tf,
               "source": "llm" if parsed else "rule",
               "explain": parsed or rule_explain(ctx),
               "context_digest": {
                   "price": ctx.get("price"),
                   "cvd_trend": (ctx.get("delta_recent") or {}).get("cvd_trend"),
                   "has_divergence": bool(
                       (ctx.get("divergence") or {}).get("bullish_active")
                       or (ctx.get("divergence") or {}).get("bearish_active")),
                   "absorption": (ctx.get("absorption") or {}).get("detected"),
               },
               "generated_at": int(time.time()), "cached": False,
               "disclaimer": DISCLAIMER}
        ttl_s = float(_cfg_get("ai_explain_cache_min", CACHE_MIN_DEFAULT)) * 60.0
        _cache_put(key, out, ttl_s)
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:200],
                "symbol": symbol, "timeframe": timeframe}


if __name__ == "__main__":
    print(json.dumps(explain(), ensure_ascii=False, indent=2))
