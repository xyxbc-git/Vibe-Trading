#!/usr/bin/env python3
"""贾维斯 JARVIS — 量价核对「主力底牌」裁决器（威科夫×订单流 P1 · T1.1）。

痛点：散户经常被假突破的量价表象骗进场。K 线量价是「影子」，订单流才是
「底牌」——本模块把仓内既有的五路微观证据聚成一次裁决：

  trap  诱多诱空四规则   （jarvis_trap_detect.detect_traps，纯函数复用）
  cvd   Delta/CVD 吸收背离（jarvis_delta_flow.compute_delta_cvd + detect_divergence）
  whale 大单分层净流     （jarvis_whale_tape.summary，WS 内存态，零出网）
  book  盘口买卖失衡     （jarvis_orderbook.book，WS 本地订单簿，零出网）
  vp    价值区位置       （jarvis_volume_profile.compute_profile，吃同一份 bars）

裁决语义：每路证据归一到 [-1, +1]（+ = 需求方/吸筹，- = 供给方/派发），
加权平均（仅在可用证据间归一，缺路不稀释方向、只降 coverage/confidence）：
  score >= +0.35 → bias = accumulation（主力吸筹）
  score <= -0.35 → bias = distribution（主力派发）
  其间           → bias = neutral
confidence = min(1, |score|) × coverage —— 证据不全自动降置信，不硬造结论。

突破核验（breakout_check，痛点的直接回答）：最近 3 根已收盘 bar 若突破
20 根前高/前低，用「量比 + 突破根 Delta 方向 + CVD 是否创同向极值 +
陷阱信号 + 反向大单净流」交叉核对，输出 confirmed / suspect / unknown
与人话 reasons（直接上前端底牌卡）。

数据纪律（与全仓一致）：
  - 取数只走 jarvis_delta_flow.fetch_bars（合约优先+现货回退，带 TTL 缓存
    与防限频三道闸）与 WS 内存态；本模块自身零新增出网端点。
  - 只用已收盘 bar（fetch_bars 已丢进行中最后一根），防前瞻。
  - 判定核心全部纯函数（吃打桩证据可离线冒烟：_supply_demand_smoketest.py）。

用法：
  python jarvis_supply_demand.py BTCUSDT --interval 15m [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time

DISCLAIMER = "供需裁决为统计证据聚合，非投资建议；证据覆盖度不足时结论自动降权。"

# 五路证据权重（配置键 sd_weights 可覆盖，JSON 字符串，如
# '{"trap":0.4,"cvd":0.3}' —— 只覆盖出现的键，其余用内置默认）
DEFAULT_WEIGHTS: dict[str, float] = {
    "trap": 0.30, "cvd": 0.25, "whale": 0.20, "book": 0.10, "vp": 0.15,
}

BIAS_THRESHOLD = 0.35        # |score| 达标线：吸筹/派发结论
BARS_LIMIT = 200             # 取数根数（fetch_bars 上限内）
MIN_BARS = 30                # 判定所需最少已收盘根数
TRAP_RECENT_BARS = 10        # trap 信号回看窗口（根）
CVD_SLOPE_BARS = 20          # CVD 斜率窗口（根）
VP_BARS = 120                # 价值区分布计算窗口（根）
BREAKOUT_LOOKBACK = 20       # 突破参考的前高/前低窗口（根）
BREAKOUT_RECENT_BARS = 3     # 突破发生在最近 N 根内才算 active
BREAKOUT_VOL_CONFIRM = 1.5   # 真突破量比下限
BREAKOUT_VOL_SUSPECT = 0.8   # 低于该量比 = 无承接嫌疑


# ═══════════════════════════ 证据采集（六路独立降级） ═══════════════════════════


def collect_evidence(symbol: str, interval: str = "15m",
                     limit: int = BARS_LIMIT) -> dict:
    """六路证据采集，全部复用现有模块；单路异常降级 None，绝不抛出。

    返回 {"symbol","interval","bars","trap","cvd","whale","book","vp","coverage"}。
    bars 依赖路（trap/cvd/vp）在 bars 缺失时同为 None。
    """
    sym = (symbol or "BTCUSDT").upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC")):
        sym += "USDT"
    ev: dict = {"symbol": sym, "interval": interval, "bars": None, "trap": None,
                "cvd": None, "whale": None, "book": None, "vp": None}

    bars = None
    try:
        import jarvis_delta_flow as jdf
        got = jdf.fetch_bars(sym, interval, limit)
        if got and len(got) >= MIN_BARS:
            bars = got
            ev["bars"] = bars
    except Exception:  # noqa: BLE001 — 取数失败走证据缺失降级
        pass

    if bars:
        try:
            import jarvis_trap_detect as jtd
            ev["trap"] = {"signals": jtd.detect_traps(bars)}
        except Exception:  # noqa: BLE001
            pass
        try:
            import jarvis_delta_flow as jdf
            rows = jdf.compute_delta_cvd(bars)
            det = jdf.detect_divergence(rows)
            ev["cvd"] = {"rows": rows, "absorption": det.get("absorption") or {}}
        except Exception:  # noqa: BLE001
            pass
        try:
            import jarvis_volume_profile as jvp
            vp_rows = [{"time": b["ts"], "open": b["open"], "high": b["high"],
                        "low": b["low"], "close": b["close"], "volume": b["volume"]}
                       for b in bars[-VP_BARS:]]
            prof = jvp.compute_profile(vp_rows)
            if prof:
                ev["vp"] = {"poc": prof["poc"], "vah": prof["vah"],
                            "val": prof["val"], "close": bars[-1]["close"]}
        except Exception:  # noqa: BLE001
            pass

    try:
        import jarvis_whale_tape as jwt
        s = jwt.summary(sym)
        if s.get("active"):
            ev["whale"] = s
    except Exception:  # noqa: BLE001
        pass

    try:
        import jarvis_orderbook as job
        b = job.book(sym)
        if b.get("ok"):
            ev["book"] = b
    except Exception:  # noqa: BLE001
        pass

    paths = (ev["bars"], ev["trap"], ev["cvd"], ev["whale"], ev["book"], ev["vp"])
    ev["coverage"] = round(sum(1 for p in paths if p is not None) / 6.0, 3)
    return ev


# ═══════════════════════════ 单路归一（纯函数，-1..+1） ═══════════════════════════


def _norm_trap(trap: dict | None, bars: list | None) -> tuple[float, str | None, str]:
    """诱多/诱空信号 → 方向分。诱空陷阱=空头被骗=看涨(+)；诱多镜像(-)。

    只认最近 TRAP_RECENT_BARS 根内的信号，按新鲜度衰减；无信号 = 0（已查无陷阱，
    是有效的中性证据，不是证据缺失）。
    """
    if trap is None or bars is None or not bars:
        return 0.0, None, "证据缺失"
    last_ts_s = bars[-1]["ts"] / 1000.0
    bar_sec = max(60.0, (bars[-1]["ts"] - bars[0]["ts"]) / 1000.0 / max(1, len(bars) - 1))
    best, best_score = None, 0.0
    for s in trap.get("signals") or []:
        age_bars = max(0.0, (last_ts_s - float(s.get("ts") or 0)) / bar_sec)
        if age_bars > TRAP_RECENT_BARS:
            continue
        fresh = 1.0 - age_bars / TRAP_RECENT_BARS
        signed = float(s.get("confidence") or 0.5) * fresh
        signed = signed if s.get("type") == "bear_trap" else -signed
        if abs(signed) > abs(best_score):
            best, best_score = s, signed
    if best is None:
        return 0.0, None, f"近 {TRAP_RECENT_BARS} 根无陷阱信号（中性）"
    label = "诱空陷阱（看涨）" if best_score > 0 else "诱多陷阱（看跌）"
    detail = f"{label}：{(best.get('reasons') or ['—'])[0]}"
    return max(-1.0, min(1.0, best_score)), best.get("type"), detail


def _norm_cvd(cvd: dict | None) -> tuple[float, str | None, str]:
    """吸收背离优先（强证据），否则用 CVD 近窗斜率（弱方向证据，封顶 ±0.5）。"""
    if cvd is None:
        return 0.0, None, "证据缺失"
    absorption = cvd.get("absorption") or {}
    strength_map = {"weak": 0.4, "moderate": 0.7, "strong": 1.0}
    if absorption.get("detected"):
        note = str(absorption.get("note") or "")
        mag = 0.7
        for key, v in strength_map.items():
            if {"weak": "初现", "moderate": "中等", "strong": "强"}[key] in note:
                mag = v
                break
        if absorption.get("side") == "sell-absorption":
            return mag, "sell_absorption", f"卖压被吸收：{note[:60]}"
        return -mag, "buy_distribution", f"买盘被派发：{note[:60]}"
    rows = cvd.get("rows") or []
    if len(rows) < CVD_SLOPE_BARS:
        return 0.0, None, "CVD 样本不足（中性）"
    window = rows[-CVD_SLOPE_BARS:]
    dcvd = window[-1]["cvd"] - window[0]["cvd"]
    rng = (max(r["cvd"] for r in rows) - min(r["cvd"] for r in rows)) or 1.0
    score = max(-0.5, min(0.5, dcvd / rng))
    trend = "净买入" if score > 0 else ("净卖出" if score < 0 else "均衡")
    return score, "cvd_slope", f"近 {CVD_SLOPE_BARS} 根 CVD {trend}（斜率 {score:+.2f}）"


def _norm_whale(whale: dict | None) -> tuple[float, str | None, str]:
    """大单净流 / tier1 量级归一：净流达 3 笔 tier1 大单记满分。"""
    if whale is None:
        return 0.0, None, "证据缺失（WS 离线或无大单数据）"
    net = float(whale.get("net_usd") or 0.0)
    tier1 = float(whale.get("tier1_usd") or 100000.0)
    score = max(-1.0, min(1.0, net / (3.0 * tier1)))
    win = whale.get("window_min")
    if abs(net) < tier1:
        return score, None, f"{win}min 大单净流接近均衡（{net:+,.0f} USDT，中性）"
    side = "净买入" if net > 0 else "净卖出"
    return score, "whale_flow", f"{win}min 大单{side} {abs(net):,.0f} USDT"


def _norm_book(book: dict | None) -> tuple[float, str | None, str]:
    """盘口前 10 档买卖失衡：log2(比值)/2 归一（4 倍失衡 = 满分）。"""
    if book is None:
        return 0.0, None, "证据缺失（本地订单簿未同步）"
    imb = book.get("imbalance") or {}
    ratio = imb.get("ratio")
    if not ratio or ratio <= 0:
        return 0.0, None, "盘口失衡比不可用（中性）"
    score = max(-1.0, min(1.0, math.log2(float(ratio)) / 2.0))
    side = "买方挂单厚" if score > 0 else ("卖方压单厚" if score < 0 else "均衡")
    return score, "book_imbalance", f"盘口前10档失衡比 {ratio:.2f}（{side}）"


def _fmt_price(v: float) -> str:
    """人话价格：≥1000 千分位整数；≥1 保留 4 位有效；小币种 6 位有效。"""
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:.4g}" if v >= 1 else f"{v:.6g}"


def _norm_vp(vp: dict | None) -> tuple[float, str | None, str]:
    """价值区位置（均值回归口径）：价高于价值区=派发倾向，低于=吸筹倾向。弱证据封顶 ±0.6。"""
    if vp is None:
        return 0.0, None, "证据缺失"
    vah, val, poc, close = vp["vah"], vp["val"], vp["poc"], vp["close"]
    span = (vah - val) or 1e-9
    pos = (close - poc) / span
    score = max(-0.6, min(0.6, -pos * 0.6))
    if close > vah:
        where = "价值区上方（溢价区，派发倾向）"
    elif close < val:
        where = "价值区下方（折价区，吸筹倾向）"
    else:
        where = "价值区内（均衡）"
    return score, "vp_position", f"现价位于{where}，POC {_fmt_price(poc)}"


# ═══════════════════════════ 突破核验（纯函数） ═══════════════════════════


def breakout_check(bars: list | None, cvd: dict | None,
                   trap: dict | None, whale: dict | None) -> dict:
    """最近 BREAKOUT_RECENT_BARS 根内的突破 → confirmed / suspect / unknown。

    confirmed：量比 ≥1.5 且突破根 Delta 同向 且 CVD 创同向极值 且 无陷阱/反向大单
    suspect  ：量比 <0.8 / CVD 背离 / 突破±3根内有陷阱信号 / 反向大单净流 ≥tier1
    unknown  ：无突破，或证据不足以确认/证伪
    """
    out = {"active": False, "direction": None, "verdict": "unknown", "reasons": []}
    if not bars or len(bars) < BREAKOUT_LOOKBACK + BREAKOUT_RECENT_BARS:
        return out
    n = len(bars)
    brk_idx, direction = None, None
    for i in range(n - BREAKOUT_RECENT_BARS, n):
        window = bars[i - BREAKOUT_LOOKBACK:i]
        prev_high = max(b["high"] for b in window)
        prev_low = min(b["low"] for b in window)
        c = bars[i]["close"]
        if c > prev_high:
            brk_idx, direction = i, "up"
        elif c < prev_low:
            brk_idx, direction = i, "down"
    if brk_idx is None:
        return out
    out["active"] = True
    out["direction"] = direction
    up = direction == "up"

    reasons_confirm: list[str] = []
    reasons_suspect: list[str] = []

    # 1) 量比
    vol_window = [b["volume"] for b in bars[brk_idx - BREAKOUT_LOOKBACK:brk_idx]]
    avg_vol = (sum(vol_window) / len(vol_window)) or 1e-9
    vol_ratio = bars[brk_idx]["volume"] / avg_vol
    if vol_ratio >= BREAKOUT_VOL_CONFIRM:
        reasons_confirm.append(f"突破根放量 {vol_ratio:.1f}× 均量")
    elif vol_ratio < BREAKOUT_VOL_SUSPECT:
        reasons_suspect.append(f"突破根量能仅 {vol_ratio:.1f}× 均量（无承接嫌疑）")

    # 2) 突破根 Delta 方向 + 3) CVD 同向极值
    delta_ok = cvd_extreme = None
    rows = (cvd or {}).get("rows") or []
    if len(rows) == n:
        delta = rows[brk_idx]["delta"]
        delta_ok = delta > 0 if up else delta < 0
        if delta_ok:
            reasons_confirm.append("突破根主动单与方向同向")
        else:
            reasons_suspect.append("突破根 Delta 与方向相反（推价的不是主动单）")
        cvd_win = [r["cvd"] for r in rows[max(0, brk_idx - BREAKOUT_LOOKBACK):brk_idx + 1]]
        cvd_extreme = (rows[brk_idx]["cvd"] >= max(cvd_win)) if up \
            else (rows[brk_idx]["cvd"] <= min(cvd_win))
        if cvd_extreme:
            reasons_confirm.append("CVD 同步创同向极值（买卖力量跟上了价格）")
        else:
            reasons_suspect.append("价格破位但 CVD 未创同向极值（量价背离）")

    # 4) 陷阱信号（突破 bar ±3 根内）
    brk_ts_s = bars[brk_idx]["ts"] / 1000.0
    bar_sec = max(60.0, (bars[-1]["ts"] - bars[0]["ts"]) / 1000.0 / max(1, n - 1))
    for s in (trap or {}).get("signals") or []:
        if abs(float(s.get("ts") or 0) - brk_ts_s) <= 3 * bar_sec:
            typ = "诱多" if s.get("type") == "bull_trap" else "诱空"
            reasons_suspect.append(f"突破附近触发{typ}陷阱信号：{(s.get('reasons') or ['—'])[0]}")
            break

    # 5) 反向大单净流
    if whale is not None:
        net = float(whale.get("net_usd") or 0.0)
        tier1 = float(whale.get("tier1_usd") or 100000.0)
        if abs(net) >= tier1 and ((up and net < 0) or (not up and net > 0)):
            reasons_suspect.append(
                f"大单净流与突破方向相反（{net:+,.0f} USDT，主力在对面）")

    if reasons_suspect:
        out["verdict"] = "suspect"
        out["reasons"] = reasons_suspect
    elif vol_ratio >= BREAKOUT_VOL_CONFIRM and delta_ok and cvd_extreme:
        out["verdict"] = "confirmed"
        out["reasons"] = reasons_confirm
    else:
        out["verdict"] = "unknown"
        out["reasons"] = reasons_confirm + ["证据不足以确认或证伪，等待跟随根"]
    return out


# ═══════════════════════════ 裁决（纯函数） ═══════════════════════════


_NORMALIZERS = {
    "trap": lambda ev: _norm_trap(ev.get("trap"), ev.get("bars")),
    "cvd": lambda ev: _norm_cvd(ev.get("cvd")),
    "whale": lambda ev: _norm_whale(ev.get("whale")),
    "book": lambda ev: _norm_book(ev.get("book")),
    "vp": lambda ev: _norm_vp(ev.get("vp")),
}


def verdict(ev: dict, weights: dict | None = None) -> dict:
    """证据 → 供需裁决（纯函数：不取数、不联网、不读配置）。"""
    w = dict(DEFAULT_WEIGHTS)
    for k, v in (weights or {}).items():
        if k in w:
            try:
                w[k] = max(0.0, float(v))
            except (TypeError, ValueError):
                pass

    chain: list[dict] = []
    weighted_sum, weight_avail = 0.0, 0.0
    for source in ("trap", "cvd", "whale", "book", "vp"):
        available = ev.get(source) is not None
        norm, signal, detail = _NORMALIZERS[source](ev)
        weight = w[source] if available else 0.0
        if available:
            weighted_sum += w[source] * norm
            weight_avail += w[source]
        chain.append({
            "source": source,
            "signal": signal,
            "direction": (1 if norm > 0.05 else (-1 if norm < -0.05 else 0)),
            "weight": round(weight, 3),
            "detail": detail,
        })

    score = (weighted_sum / weight_avail) if weight_avail > 0 else 0.0
    coverage = float(ev.get("coverage") or 0.0)
    if score >= BIAS_THRESHOLD:
        bias = "accumulation"
    elif score <= -BIAS_THRESHOLD:
        bias = "distribution"
    else:
        bias = "neutral"
    confidence = round(min(1.0, abs(score)) * coverage, 3)

    return {
        "ok": True,
        "symbol": ev.get("symbol"),
        "interval": ev.get("interval"),
        "ts": time.time(),
        "bias": bias,
        "score": round(score, 3),
        "confidence": confidence,
        "coverage": coverage,
        "breakout_check": breakout_check(ev.get("bars"), ev.get("cvd"),
                                         ev.get("trap"), ev.get("whale")),
        "evidence_chain": chain,
        "stale": False,
        "disclaimer": DISCLAIMER,
    }


# ═══════════════════════════ 门面（取数 + 配置权重 + 裁决） ═══════════════════════════


def _weights_from_config() -> dict | None:
    """配置键 sd_weights（JSON 字符串）→ 权重覆盖；异常/未配置返回 None。"""
    try:
        import jarvis_config as jc
        raw = jc.get("sd_weights")
        if isinstance(raw, str) and raw.strip():
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        if isinstance(raw, dict):
            return raw
    except Exception:  # noqa: BLE001 — 配置异常用内置默认
        pass
    return None


def analyze(symbol: str, interval: str = "15m") -> dict:
    """/api/sd-verdict 消费入口：采集 → 配置权重 → 裁决。永不抛出。"""
    try:
        ev = collect_evidence(symbol, interval)
        return verdict(ev, _weights_from_config())
    except Exception as exc:  # noqa: BLE001 — 裁决层绝不拖垮 dashboard
        return {"ok": False, "symbol": (symbol or "").upper(),
                "interval": interval, "error": repr(exc)[:200],
                "disclaimer": DISCLAIMER}


def main() -> int:
    ap = argparse.ArgumentParser(description="量价核对主力底牌裁决器")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = ap.parse_args()
    out = analyze(args.symbol, args.interval)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    if not out.get("ok"):
        print(f"❌ 裁决失败：{out.get('error')}")
        return 1
    bias_cn = {"accumulation": "🟢 主力吸筹", "distribution": "🔴 主力派发",
               "neutral": "⚪ 中性/均衡"}[out["bias"]]
    print(f"{out['symbol']} {out['interval']}  {bias_cn}  "
          f"score={out['score']:+.2f} 置信={out['confidence']:.0%} "
          f"证据覆盖={out['coverage']:.0%}")
    bc = out["breakout_check"]
    if bc["active"]:
        v_cn = {"confirmed": "✅ 真突破", "suspect": "⚠️ 假突破嫌疑",
                "unknown": "❓ 待确认"}[bc["verdict"]]
        print(f"突破核验（{'向上' if bc['direction'] == 'up' else '向下'}）：{v_cn}")
        for r in bc["reasons"]:
            print(f"  · {r}")
    for e in out["evidence_chain"]:
        arrow = {1: "↑", 0: "·", -1: "↓"}[e["direction"]]
        print(f"  [{e['source']:<5}] {arrow} {e['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
