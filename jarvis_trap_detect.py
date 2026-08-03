#!/usr/bin/env python3
"""贾维斯 JARVIS — 诱多/诱空陷阱检测（bull trap / bear trap）。

用户痛点：追高被套（诱多）、杀跌被轧（诱空）。本模块对已收盘 K 线序列做
四类规则检测，输出图表可标注、人话可解释的陷阱信号：

  A. 假突破回落  收盘突破前高/前低后，N 根内收回破位——突破没有下文
  B. 长影线插针  单根刺破关键位随即收回 + 长影线 + 量能异常（放量出货/无量假破）
  C. 量价背离    新高区放量滞涨（出货）/ 缩量拉升（无承接）；新低区完全镜像
  D. Delta 背离  价格新高但主动买盘（taker_buy）不济 / 新低但主动卖压衰竭

契约（B2 前端 desktop/src/lib/trapSignals.ts 已定死，字段名不可变更）：
  GET /api/trap-signals?symbol=&interval=
  {ok, symbol, interval, signals:[{id, ts(unix 秒·bar 开盘时间), price,
   type:'bull_trap'|'bear_trap', confidence(0-1), reasons:[中文], suggestion}]}
  语义：诱多 price = 冲高点（bar high）、诱空 price = 杀跌点（bar low）；
  同类型信号 DEDUP_BARS 根内去重、全窗口最多 MAX_SIGNALS 条（与前端本地
  mock 识别同口径，引擎就绪后前端零改动切真实信号）。

核心判定为纯函数（吃 OHLCV+taker_buy 升序列表），联网取数复用
jarvis_delta_flow.fetch_bars（项目里唯一保留 k[9] taker_buy 的取数口），
便于离线冒烟与 dashboard /api/trap-signals 复用。

用法（CLI）：
  python jarvis_trap_detect.py BTCUSDT --interval 15m
  python jarvis_trap_detect.py --mock
"""

from __future__ import annotations

import argparse
import json
import sys
import time

# ─────────────────────────── 参数（经验缺省，调用方可覆盖） ───────────────────────────

# 前高/前低参考窗口（根）
LOOKBACK_BARS = 20
# 规则 A：收盘突破后需在 N 根内收回才算假突破
CONFIRM_BARS = 3
# 规则 B：插针影线占整根 K 线最低比例（与前端本地识别同口径）
MIN_SHADOW_RATIO = 0.45
# 放量口径：成交量 ≥ 窗口均量 × 该倍数
VOL_SPIKE_MULT = 1.5
# 缩量/无量口径：成交量 ≤ 窗口均量 × 该倍数
VOL_DRY_MULT = 0.8
# 规则 C 滞涨/滞跌：实体 ≤ 当根全幅 × 该比例（放量却推不动）
STALL_BODY_RATIO = 0.3
# 规则 D：Delta 累计参考窗口（根）
DELTA_WINDOW = 5
# 邻近同类型去重（根）与全窗口信号上限（与前端 mock 同口径，防糊图）
DEDUP_BARS = 6
MAX_SIGNALS = 12
# 数据下限：窗口 + 少量确认根
MIN_BARS = LOOKBACK_BARS + 5

# 提醒节流：同 symbol+类型 30 分钟一条；仅最近 N 根内的新鲜信号才打扰
_ALERT_COOLDOWN_SEC = 1800
_FRESH_BARS = 2
_last_alert: dict[tuple, float] = {}

_INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                 "1h": 3600, "4h": 14400, "1d": 86400}

_TYPE_CN = {"bull_trap": "诱多陷阱", "bear_trap": "诱空陷阱"}


def _fmt(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4g}"
    return f"{v:.6g}"


def _window_stats(bars: list[dict], i: int, lookback: int) -> tuple[float, float, float | None]:
    """bar i 之前 lookback 根的（前高, 前低, 量比）。量比 = 当根量 / 窗口均量。"""
    window = bars[i - lookback:i]
    prev_high = max(float(b["high"]) for b in window)
    prev_low = min(float(b["low"]) for b in window)
    vols = [float(b.get("volume") or 0) for b in window]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    v = float(bars[i].get("volume") or 0)
    vol_ratio = round(v / avg_vol, 2) if avg_vol > 0 else None
    return prev_high, prev_low, vol_ratio


def _delta_of(bar: dict) -> float | None:
    """单根 Delta = 2×taker_buy − volume；taker_buy 缺失返回 None（规则 D 静默关闭）。"""
    tb = bar.get("taker_buy")
    if tb is None:
        return None
    return 2.0 * float(tb) - float(bar.get("volume") or 0)


# ─────────────────────────── 四类规则（纯函数，各自全序列扫描） ───────────────────────────
# 每条命中：{"i": bar下标, "type", "confidence", "reasons": [中文], "level": 关键位|None}


def _rule_false_breakout(bars: list[dict], lookback: int, confirm: int) -> list[dict]:
    """规则 A · 假突破回落：收盘突破前高/前低后 N 根内收回破位。"""
    hits: list[dict] = []
    n = len(bars)
    for i in range(lookback, n):
        prev_high, prev_low, vol_ratio = _window_stats(bars, i, lookback)
        c = float(bars[i]["close"])

        def _episode(level: float, bullish_trap: bool) -> None:
            # 随后 confirm 根内找「收盘收回破位」的确认根
            for k in range(i + 1, min(i + 1 + confirm, n)):
                ck = float(bars[k]["close"])
                reclaimed = ck < level if bullish_trap else ck > level
                if not reclaimed:
                    continue
                took = k - i
                # 信号锚定在整段行情的极值根（诱多=冲高点 / 诱空=杀跌点）
                if bullish_trap:
                    anchor = max(range(i, k + 1), key=lambda j: float(bars[j]["high"]))
                else:
                    anchor = min(range(i, k + 1), key=lambda j: float(bars[j]["low"]))
                side_cn = "前高" if bullish_trap else "前低"
                back_cn = "跌回" if bullish_trap else "拉回"
                conf = 0.4 + 0.15 * (confirm - took) / confirm
                reasons = [
                    f"收盘{'突破' if bullish_trap else '跌破'}近 {lookback} 根{side_cn} {_fmt(level)} 后，"
                    f"仅 {took} 根就{back_cn}破位{'下' if bullish_trap else '上'}方（收盘 {_fmt(ck)}），突破没有下文"
                ]
                if vol_ratio is not None and vol_ratio <= VOL_DRY_MULT:
                    conf += 0.15
                    reasons.append(f"突破 K 量能仅均量 {vol_ratio} 倍，缺乏真实{'买盘' if bullish_trap else '卖压'}推动")
                elif vol_ratio is not None and vol_ratio >= VOL_SPIKE_MULT:
                    conf += 0.1
                    reasons.append(f"突破 K 放量至均量 {vol_ratio} 倍却无法站稳，{'放量出货' if bullish_trap else '放量诱空洗盘'}嫌疑")
                hits.append({
                    "i": anchor,
                    "type": "bull_trap" if bullish_trap else "bear_trap",
                    "confidence": conf, "reasons": reasons, "level": level,
                })
                return

        if c > prev_high:
            _episode(prev_high, bullish_trap=True)
        elif c < prev_low:
            _episode(prev_low, bullish_trap=False)
    return hits


def _rule_pin_wick(bars: list[dict], lookback: int) -> list[dict]:
    """规则 B · 长影线插针 + 量能异常：单根刺破关键位随即收回。"""
    hits: list[dict] = []
    for i in range(lookback, len(bars)):
        b = bars[i]
        o, h, l, c = (float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]))
        rng = h - l
        if rng <= 0:
            continue
        prev_high, prev_low, vol_ratio = _window_stats(bars, i, lookback)
        if vol_ratio is None:
            continue
        spike = vol_ratio >= VOL_SPIKE_MULT
        dry = vol_ratio <= VOL_DRY_MULT
        if not (spike or dry):
            continue  # 量能异常是本规则的构成要件

        upper_shadow = (h - max(o, c)) / rng
        if h > prev_high and c < prev_high and upper_shadow >= MIN_SHADOW_RATIO:
            conf = 0.35 + 0.25 * upper_shadow + (0.1 if spike else 0.08)
            reasons = [
                f"长上影刺破{('前高 ' + _fmt(prev_high))}后当根即收回（收盘 {_fmt(c)}），"
                f"上影线占整根 K 线 {upper_shadow * 100:.0f}%，冲高买盘被完全吞没",
                (f"量能放大至均量 {vol_ratio} 倍，放量冲高回落常见于诱多出货" if spike
                 else f"冲高未伴随放量（量能仅均量 {vol_ratio} 倍），无量假突破、买盘未跟"),
            ]
            hits.append({"i": i, "type": "bull_trap", "confidence": conf,
                         "reasons": reasons, "level": prev_high})
            continue

        lower_shadow = (min(o, c) - l) / rng
        if l < prev_low and c > prev_low and lower_shadow >= MIN_SHADOW_RATIO:
            conf = 0.35 + 0.25 * lower_shadow + (0.1 if spike else 0.08)
            reasons = [
                f"长下影刺破{('前低 ' + _fmt(prev_low))}后当根即收回（收盘 {_fmt(c)}），"
                f"下影线占整根 K 线 {lower_shadow * 100:.0f}%，杀跌卖盘被完全承接",
                (f"量能放大至均量 {vol_ratio} 倍，放量下杀被吸收常见于诱空洗盘" if spike
                 else f"破位未伴随放量（量能仅均量 {vol_ratio} 倍），空头动能不足"),
            ]
            hits.append({"i": i, "type": "bear_trap", "confidence": conf,
                         "reasons": reasons, "level": prev_low})
    return hits


def _rule_volume_divergence(bars: list[dict], lookback: int) -> list[dict]:
    """规则 C · 量价背离：新高区放量滞涨 / 缩量拉升；新低区放量滞跌 / 缩量杀跌。"""
    hits: list[dict] = []
    for i in range(lookback, len(bars)):
        b = bars[i]
        o, h, l, c = (float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]))
        rng = h - l
        if rng <= 0:
            continue
        prev_high, prev_low, vol_ratio = _window_stats(bars, i, lookback)
        if vol_ratio is None:
            continue
        body = abs(c - o)
        prev_close = float(bars[i - 1]["close"])

        # C1 · 放量滞涨（新高区）：量爆但推不动，收平/收阴 → 出货嫌疑（诱多）
        if h >= prev_high and vol_ratio >= VOL_SPIKE_MULT and body <= rng * STALL_BODY_RATIO and c <= o:
            conf = 0.35 + 0.15 * min(vol_ratio / 3.0, 1.0)
            hits.append({
                "i": i, "type": "bull_trap", "confidence": conf, "level": prev_high,
                "reasons": [
                    f"新高区放量滞涨：量能放大至均量 {vol_ratio} 倍，K 线实体仅占全幅 "
                    f"{body / rng * 100:.0f}% 且收盘不涨（{_fmt(c)}）——大量成交换不来上涨，出货嫌疑"
                ],
            })
        # C2 · 缩量拉升：创新高的阳线量能持续萎缩 → 无承接的虚涨（诱多）
        elif h > prev_high and c > o and c > prev_close and vol_ratio <= VOL_DRY_MULT:
            conf = 0.35 + 0.15 * min((VOL_DRY_MULT - vol_ratio) / VOL_DRY_MULT + 0.3, 1.0)
            hits.append({
                "i": i, "type": "bull_trap", "confidence": conf, "level": prev_high,
                "reasons": [
                    f"缩量拉升创新高：量能仅均量 {vol_ratio} 倍，上涨缺乏真实买盘承接，"
                    "高位接盘意愿不足、易被反手砸回"
                ],
            })
        # C1' · 放量滞跌（新低区）：量爆但跌不动，收平/收阳 → 吸筹嫌疑（诱空）
        elif l <= prev_low and vol_ratio >= VOL_SPIKE_MULT and body <= rng * STALL_BODY_RATIO and c >= o:
            conf = 0.35 + 0.15 * min(vol_ratio / 3.0, 1.0)
            hits.append({
                "i": i, "type": "bear_trap", "confidence": conf, "level": prev_low,
                "reasons": [
                    f"新低区放量滞跌：量能放大至均量 {vol_ratio} 倍，K 线实体仅占全幅 "
                    f"{body / rng * 100:.0f}% 且收盘不跌（{_fmt(c)}）——恐慌抛盘被暗中承接，吸筹嫌疑"
                ],
            })
        # C2' · 缩量杀跌：创新低的阴线量能萎缩 → 没人真卖的虚跌（诱空）
        elif l < prev_low and c < o and c < prev_close and vol_ratio <= VOL_DRY_MULT:
            conf = 0.35 + 0.15 * min((VOL_DRY_MULT - vol_ratio) / VOL_DRY_MULT + 0.3, 1.0)
            hits.append({
                "i": i, "type": "bear_trap", "confidence": conf, "level": prev_low,
                "reasons": [
                    f"缩量杀跌创新低：量能仅均量 {vol_ratio} 倍，下跌缺乏真实卖压跟随，"
                    "更像洗盘挤止损而非趋势下跌"
                ],
            })
    return hits


def _rule_delta_divergence(bars: list[dict], lookback: int) -> list[dict]:
    """规则 D · Delta 背离：价新高但主动买盘不济 / 价新低但主动卖压衰竭。

    依赖 bar["taker_buy"]（Binance k[9]）；字段缺失时本规则静默不产出。
    """
    hits: list[dict] = []
    for i in range(lookback, len(bars)):
        b = bars[i]
        d_i = _delta_of(b)
        if d_i is None:
            continue
        v = float(b.get("volume") or 0)
        if v <= 0:
            continue
        prev_high, prev_low, _ = _window_stats(bars, i, lookback)
        h, l = float(b["high"]), float(b["low"])
        recent = [_delta_of(x) for x in bars[max(0, i - DELTA_WINDOW + 1): i + 1]]
        dsum = sum(x for x in recent if x is not None)
        rel = abs(d_i) / v  # 当根 Delta 强度（0..1）

        if h > prev_high and (d_i < 0 or dsum < 0):
            conf = 0.4 + 0.2 * min(rel, 1.0)
            hits.append({
                "i": i, "type": "bull_trap", "confidence": conf, "level": prev_high,
                "reasons": [
                    f"Delta 背离：价格创近 {lookback} 根新高，但当根主动买卖差为 "
                    f"{'负' if d_i < 0 else '弱'}（Delta {_fmt(d_i)}，近 {DELTA_WINDOW} 根累计 {_fmt(dsum)}）"
                    "——上攻靠被动挂单推动、主动买盘不济，诱多嫌疑"
                ],
            })
        elif l < prev_low and (d_i > 0 or dsum > 0):
            conf = 0.4 + 0.2 * min(rel, 1.0)
            hits.append({
                "i": i, "type": "bear_trap", "confidence": conf, "level": prev_low,
                "reasons": [
                    f"Delta 背离：价格创近 {lookback} 根新低，但当根主动买卖差为 "
                    f"{'正' if d_i > 0 else '弱'}（Delta {_fmt(d_i)}，近 {DELTA_WINDOW} 根累计 {_fmt(dsum)}）"
                    "——杀跌中主动卖压已衰竭，诱空嫌疑"
                ],
            })
    return hits


# ─────────────────────────── 合并 / 去重 / 出契约（纯函数） ───────────────────────────


def _suggestion(typ: str, bar: dict, level: float | None) -> str:
    if typ == "bull_trap":
        lv = _fmt(level if level is not None else float(bar["high"]))
        return (f"疑似诱多陷阱：不宜在此追多。已持多单建议把止损收紧至该 K 线低点 "
                f"{_fmt(float(bar['low']))} 下方；等价格重新放量站稳 {lv} 上方，再考虑恢复多头思路。")
    lv = _fmt(level if level is not None else float(bar["low"]))
    return (f"疑似诱空陷阱：不宜在此追空。已持空单建议把止损收紧至该 K 线高点 "
            f"{_fmt(float(bar['high']))} 上方；价格若再次有效跌破 {lv}，才重新考虑空头思路。")


def detect_traps(
    bars: list[dict],
    *,
    lookback: int = LOOKBACK_BARS,
    confirm: int = CONFIRM_BARS,
    dedup_bars: int = DEDUP_BARS,
    max_signals: int = MAX_SIGNALS,
) -> list[dict]:
    """对 OHLCV(+taker_buy) 升序序列跑四类规则 → 契约 signals 列表（纯函数）。

    bars: [{ts(毫秒), open, high, low, close, volume, taker_buy?}, ...]
    返回按时间升序的信号；同根多规则命中合并（证据叠加、置信度加成）。
    """
    if len(bars) < max(lookback + 2, MIN_BARS):
        return []

    raw = (
        _rule_false_breakout(bars, lookback, confirm)
        + _rule_pin_wick(bars, lookback)
        + _rule_volume_divergence(bars, lookback)
        + _rule_delta_divergence(bars, lookback)
    )
    if not raw:
        return []

    # 同 (bar, type) 合并：置信度取最大 + 每多一条规则 +0.07；证据去重保序
    merged: dict[tuple, dict] = {}
    for hit in raw:
        key = (hit["i"], hit["type"])
        slot = merged.setdefault(key, {"confidence": 0.0, "reasons": [], "level": None, "rules": 0})
        slot["confidence"] = max(slot["confidence"], hit["confidence"])
        slot["rules"] += 1
        if slot["level"] is None:
            slot["level"] = hit.get("level")
        for r in hit["reasons"]:
            if r not in slot["reasons"]:
                slot["reasons"].append(r)

    ordered = sorted(merged.items(), key=lambda kv: kv[0][0])

    # 邻近同类型去重：dedup_bars 根内只保留置信度最高的一个（与前端同口径）
    deduped: list[tuple] = []
    for key, slot in ordered:
        conf = min(0.95, slot["confidence"] + 0.07 * (slot["rules"] - 1))
        slot["confidence"] = conf
        if deduped:
            (last_i, last_type), last_slot = deduped[-1]
            if last_type == key[1] and key[0] - last_i < dedup_bars:
                if conf > last_slot["confidence"]:
                    deduped[-1] = (key, slot)
                continue
        deduped.append((key, slot))

    signals = []
    for (i, typ), slot in deduped[-max_signals:]:
        bar = bars[i]
        ts = int(int(bar["ts"]) // 1000)
        price = float(bar["high"]) if typ == "bull_trap" else float(bar["low"])
        signals.append({
            "id": f"trap-{'bull' if typ == 'bull_trap' else 'bear'}-{ts}",
            "ts": ts,
            "price": round(price, 8),
            "type": typ,
            "confidence": round(slot["confidence"], 2),
            "reasons": slot["reasons"],
            "suggestion": _suggestion(typ, bar, slot["level"]),
        })
    signals.sort(key=lambda s: s["ts"])
    return signals


# ─────────────────────────── 联网取数入口 / mock ───────────────────────────


def detect(symbol: str = "BTCUSDT", interval: str = "15m", limit: int = 150) -> dict:
    """取数 + 检测一步到位（/api/trap-signals 消费）。取数失败抛错交上层降级。"""
    import jarvis_delta_flow as jdf

    sym = jdf._norm_symbol(symbol)
    iv = jdf._norm_tf(interval)
    bars = jdf.fetch_bars(symbol, iv, max(MIN_BARS, min(int(limit), 500)))
    if bars is None:
        raise RuntimeError("kline fetch failed（Binance 不可达或字段缺损）")
    return {"ok": True, "symbol": sym, "interval": iv, "mock": False,
            "signals": detect_traps(bars)}


def _mock_bars() -> list[dict]:
    """确定性合成序列：横盘基底 + 一根诱多插针 + 一根诱空插针（跑真实管线）。"""
    out: list[dict] = []
    base_ts = 1_700_000_000_000
    for i in range(60):
        price = 100.0
        o = price + (0.2 if i % 2 == 0 else -0.2)
        c = price - (0.2 if i % 2 == 0 else -0.2)
        bar = {"ts": base_ts + i * 900_000,
               "open": o, "high": max(o, c) + 0.5, "low": min(o, c) - 0.5,
               "close": c, "volume": 100.0, "taker_buy": 50.0}
        if i == 40:  # 诱多：长上影刺破前高 + 放量 + 主动买不济
            bar.update({"open": 100.0, "high": 102.6, "low": 99.8, "close": 100.0,
                        "volume": 300.0, "taker_buy": 90.0})
        if i == 52:  # 诱空：长下影刺破前低 + 放量 + 主动卖衰竭
            bar.update({"open": 100.0, "high": 100.3, "low": 97.6, "close": 100.1,
                        "volume": 280.0, "taker_buy": 190.0})
        out.append(bar)
    return out


def mock_signals(symbol: str = "BTCUSDT", interval: str = "15m") -> dict:
    """确定性演示数据（?mock=1 联调用）：合成 K 线跑真实检测管线，幂等无随机。"""
    return {"ok": True, "symbol": symbol.upper(), "interval": interval, "mock": True,
            "signals": detect_traps(_mock_bars())}


# ─────────────────────────── 提醒中心接入（失败静默，不拖垮主链路） ───────────────────────────


def maybe_alert_traps(symbol: str, interval: str, signals: list[dict],
                      *, now: float | None = None) -> dict | None:
    """最新一根附近的新鲜陷阱信号 → 页内提醒中心（add_event）+ 渠道分发（dispatch）。

    历史信号不打扰；同 symbol+类型 30 分钟节流；alert_center 不可用时静默。
    返回落库的事件 dict（未触发/被节流/失败返回 None），便于冒烟断言。
    """
    if not signals:
        return None
    latest = signals[-1]
    iv_sec = _INTERVAL_SEC.get(interval, 900)
    now_ts = time.time() if now is None else float(now)
    if now_ts - float(latest["ts"]) > iv_sec * (_FRESH_BARS + 1):
        return None  # 只提醒「刚发生」的陷阱，轮询旧数据不轰炸
    key = (symbol.upper(), latest["type"])
    if now_ts - _last_alert.get(key, 0.0) < _ALERT_COOLDOWN_SEC:
        return None
    _last_alert[key] = now_ts
    try:
        import jarvis_alert_center as jac
        cn = _TYPE_CN.get(latest["type"], "陷阱")
        event = jac.add_event(
            kind="trap_signal", symbol=symbol.upper(),
            title=f"{symbol.upper()} {interval} 出现{cn}（置信 {int(float(latest['confidence']) * 100)}%）",
            detail="；".join(latest.get("reasons") or []) + f"。建议：{latest.get('suggestion', '')}",
            severity="warning", price=latest.get("price"),
        )
        try:
            jac.dispatch(event)
        except Exception:  # noqa: BLE001 — 渠道分发失败不影响事件落库
            pass
        return event
    except Exception:  # noqa: BLE001 — 提醒失败绝不拖垮检测主链路
        return None


# ─────────────────────────── CLI ───────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="诱多/诱空陷阱检测")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--mock", action="store_true", help="确定性演示数据（不联网）")
    args = ap.parse_args()
    out = mock_signals(args.symbol, args.interval) if args.mock \
        else detect(args.symbol, args.interval, args.limit)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
