#!/usr/bin/env python3
"""贾维斯 JARVIS — 成交流行为主体分类（驾驶舱需求 2b：散户/机构/做市商画像）。

whale tape（jarvis_whale_tape）只按金额分层；本模块进一步做**行为主体归因**：
消费同一条 aggTrade 流，给每笔成交打「指纹」并聚类，回答三个问题：
  1) 同一个「唯一标记」下了多少笔、每笔多少数值 → 指纹聚合表
  2) 当前成交由谁主导（散户 / 机构·大户 / 做市商·算法） → 份额分解
  3) 主力在干什么（砸盘/拉盘/吸筹/派发），力度是否在减弱 → 行为判定 + 入场提示

指纹定义（币安公开流无账户身份，这是**概率性启发式**，非真实账户）：
  完全相同的成交数量（qty 精确签名）在短窗口内反复出现 = 大概率同一算法/
  同一主体的拆单（iceberg/TWAP 的典型痕迹）。fp = qty 的规范化字符串。

主体分类规则（指纹组优先，散单按单笔金额分层）：
  组 n≥3 且 买卖两侧都活跃（min/max ≥ 0.35）        → maker   做市商/双边算法
  组 n≥3 且 单侧 且 组均额 ≥ mid 档（$10k）          → inst    机构拆单
  散单 单笔 ≥ tier1（默认 $100k，走 whale 配置）      → inst    机构/大户
  散单 $10k ~ tier1                                  → mid     中户
  其余                                               → retail  散户

架构与 jarvis_whale_tape 同款：ingest() 在 WS 线程内 O(1) 永不抛出；
summary() 时走一遍近期成交做窗口统计。纯函数核心离线可测。
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import deque

import jarvis_db as jdb

DISCLAIMER = ("行为主体分类基于公开 aggTrade 的数量指纹与金额分层启发式推断，"
              "币安不公开账户身份，结论为概率性画像，非投资建议。")

# 金额分层（tier1/tier2 走 whale 配置；mid 档为本模块内置）
RETAIL_MAX_USD = 10_000.0
# 指纹组判定
FP_MIN_N = 3                 # 同指纹 ≥ 3 笔才视为算法拆单
MAKER_BALANCE = 0.35         # 买卖两侧笔数 min/max ≥ 该值 → 双边做市
FP_TTL_S = 1800.0            # 指纹组 30 分钟无新成交即淘汰
FP_MAX = 600                 # 每币指纹组上限（防内存无界）
TRADES_MAX = 6000            # 每币近期成交环形缓冲（BTC 高频期约覆盖数分钟）
BUCKETS_MAX = 240            # 分钟桶上限
# 大额单突然涌入（burst）判定
BURST_RATIO = 3.0            # 最近 60 秒大单额 ≥ 窗口其余时段分钟均值 × 3
# 砸盘力度减弱判定
FADE_WINDOW_MIN = 6          # 观察最近 N 个整分钟
FADE_RATIO = 0.45            # 最近 2 分钟均值 ≤ 峰值 × 0.45
# 足迹图（Footprint）分钟×价格档聚合
FOOT_MINUTES_MAX = 240       # 足迹分钟桶上限（与 minutes deque 同命，仅内存）
FOOT_CELLS_MAX = 120         # 每分钟价格档上限（超出归并到边缘档，防内存爆）
FOOT_STEP_PCT = 0.0005       # 档宽自适应：nice_step(price × 0.05%)
FOOT_DRIFT_PCT = 0.25        # 价格相对锚点漂移 >25% 才重算 step（新分钟生效）
FOOT_IMB_RATIO = 3.0         # 失衡档：同档 买/卖 ≥ 3 倍
FOOT_IMB_VOL_X = 1.5         # 且强侧额 ≥ 该 bar 档均额 × 1.5


def _cfg_tiers(cfg: dict | None = None) -> tuple[float, float]:
    try:
        if cfg is None:
            import jarvis_config as jc
            cfg = jc.load()
    except Exception:  # noqa: BLE001
        cfg = {}
    def _f(key: str, dv: float) -> float:
        try:
            v = float((cfg or {}).get(key, dv))
            return v if v > 0 else dv
        except (TypeError, ValueError):
            return dv
    return _f("whale_tier1_usd", 100000.0), _f("whale_tier2_usd", 1000000.0)


# ─────────────────────────── 纯函数 ───────────────────────────


def parse_trade(data: dict) -> tuple[float, float, float, bool, int] | None:
    """aggTrade → (price, qty, usd, is_buy, ts_ms)；坏数据 None。"""
    try:
        price = float(data.get("p") or 0)
        qty = float(data.get("q") or 0)
        if price <= 0 or qty <= 0:
            return None
        is_buy = not bool(data.get("m"))
        ts_ms = int(data.get("T") or 0) or int(time.time() * 1000)
        return price, qty, round(price * qty, 4), is_buy, ts_ms
    except (TypeError, ValueError):
        return None


def fingerprint(qty: float) -> str:
    """数量精确签名（8 位有效数字，规避浮点尾差）。"""
    return f"{qty:.8g}"


def tier_of(usd: float, tier1: float) -> str:
    if usd >= tier1:
        return "whale"
    if usd >= RETAIL_MAX_USD:
        return "mid"
    return "retail"


def classify_group(g: dict) -> str:
    """指纹组 → maker / inst / None（散单归 tier）。"""
    n = g["buy_n"] + g["sell_n"]
    if n < FP_MIN_N:
        return ""
    lo, hi = sorted((g["buy_n"], g["sell_n"]))
    if hi > 0 and lo / hi >= MAKER_BALANCE:
        return "maker"
    avg = g["total_usd"] / n if n else 0.0
    if avg >= RETAIL_MAX_USD:
        return "inst"
    return ""


def classify_trade(usd: float, tier1: float, group_cls: str) -> str:
    """单笔归类：指纹组结论优先，其余按金额分层。"""
    if group_cls in ("maker", "inst"):
        return group_cls
    t = tier_of(usd, tier1)
    if t == "whale":
        return "inst"
    return t  # mid / retail


ACTOR_CN = {"retail": "散户", "mid": "中户", "inst": "机构/大户", "maker": "做市商/算法"}


def build_breakdown(trades: list[dict], tier1: float,
                    groups: dict[str, dict]) -> dict:
    """窗口成交 → 主体份额分解 {actor: {usd, buy_usd, sell_usd, n}}。"""
    acc = {k: {"usd": 0.0, "buy_usd": 0.0, "sell_usd": 0.0, "n": 0}
           for k in ACTOR_CN}
    for t in trades:
        g = groups.get(t["fp"])
        cls = classify_trade(t["usd"], tier1, classify_group(g) if g else "")
        a = acc[cls]
        a["usd"] += t["usd"]
        a["n"] += 1
        if t["is_buy"]:
            a["buy_usd"] += t["usd"]
        else:
            a["sell_usd"] += t["usd"]
    total = sum(a["usd"] for a in acc.values())
    for k, a in acc.items():
        a["pct"] = round(a["usd"] / total * 100, 2) if total > 0 else 0.0
        a["net_usd"] = round(a["buy_usd"] - a["sell_usd"], 2)
        for f in ("usd", "buy_usd", "sell_usd"):
            a[f] = round(a[f], 2)
        a["actor"] = k
        a["actor_cn"] = ACTOR_CN[k]
        # 多空倾向：主动买=做多倾向 / 主动卖=做空倾向（taker 方向启发式，非真实持仓）
        side_tot = a["buy_usd"] + a["sell_usd"]
        if side_tot > 0:
            lp = round(a["buy_usd"] / side_tot * 100.0, 1)
            a["long_pct"] = lp
            if lp >= 55:
                a["verdict_cn"] = f"主动买 {lp:.0f}%，做多倾向"
            elif lp <= 45:
                a["verdict_cn"] = f"主动卖 {100 - lp:.0f}%，做空倾向"
            else:
                a["verdict_cn"] = "买卖大致均衡"
        else:
            a["long_pct"] = None
            a["verdict_cn"] = "窗口内无成交"
    return {"total_usd": round(total, 2), "actors": acc}


def _fmt_usd(v: float) -> str:
    v = abs(v)
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


def build_verdict(breakdown: dict, minute_rows: list[dict],
                  price_chg_pct: float | None, tier1: float,
                  now_ms: int) -> dict:
    """主力行为判定：谁主导 / 在干什么 / 力度趋势 / 入场提示 / burst 警报。"""
    actors = breakdown["actors"]
    non_retail_usd = sum(actors[k]["usd"] for k in ("inst", "maker", "mid"))
    total = breakdown["total_usd"]
    nr_share = round(non_retail_usd / total * 100, 1) if total > 0 else 0.0
    inst_net = actors["inst"]["net_usd"] + actors["maker"]["net_usd"]

    dominant = max(actors.values(), key=lambda a: a["usd"])["actor"] if total > 0 else "retail"

    # 行为定性：主力净流方向 × 价格反应
    action, note = "中性", "窗口内主力买卖大致均衡"
    if total > 0 and abs(inst_net) >= tier1:
        if inst_net < 0:
            if price_chg_pct is not None and price_chg_pct > -0.05:
                action = "派发/出货"
                note = (f"主力净卖出 {_fmt_usd(inst_net)} 但价格未明显下跌"
                        f"——高位派发、散户接盘嫌疑")
            else:
                action = "砸盘"
                note = f"主力净卖出 {_fmt_usd(inst_net)}，价格同步下压——主动砸盘"
        else:
            if price_chg_pct is not None and price_chg_pct < 0.05:
                action = "吸筹"
                note = (f"主力净买入 {_fmt_usd(inst_net)} 但价格滞涨"
                        f"——低位吸筹、压价收集嫌疑")
            else:
                action = "拉盘/操盘"
                note = f"主力净买入 {_fmt_usd(inst_net)}，价格同步上行——主动拉抬"

    # burst：近 2 分钟非散户成交额（折算每分钟）vs 更早分钟的中位数基线。
    # 中位数抗离群——burst 本身若跨分钟不会污染基线（均值口径会）。
    burst = None
    if minute_rows:
        cur_min = now_ms // 60000
        recent = [r for r in minute_rows if r["minute"] >= cur_min - 1]
        base = sorted(r["nr_buy"] + r["nr_sell"] for r in minute_rows
                      if r["minute"] < cur_min - 1)
        if recent and base:
            n = len(base)
            base_med = (base[n // 2] if n % 2 else
                        (base[n // 2 - 1] + base[n // 2]) / 2.0)
            cur_buy = sum(r["nr_buy"] for r in recent)
            cur_sell = sum(r["nr_sell"] for r in recent)
            cur_total = cur_buy + cur_sell
            per_min = cur_total / max(1, len(recent))
            if (base_med > 0 and per_min >= base_med * BURST_RATIO
                    and cur_total >= tier1):
                side = "买入" if cur_buy >= cur_sell else "卖出"
                burst = {
                    "side": "buy" if side == "买入" else "sell",
                    "usd": round(cur_total, 2),
                    "note": (f"⚠️ 大批大额{side}单涌入：最近约 2 分钟非散户成交 "
                             f"{_fmt_usd(cur_total)}（约 {_fmt_usd(per_min)}/分钟），为此前"
                             f"分钟中位数的 {per_min / base_med:.1f} 倍——非散户行为主导"),
                }

    # 砸盘力度减弱 → 入场提示：非散户卖出额分钟序列 峰值→近 2 分钟均值
    entry_hint = None
    rows = [r for r in minute_rows if r["minute"] >= now_ms // 60000 - FADE_WINDOW_MIN]
    if action in ("砸盘", "派发/出货") and len(rows) >= 4:
        sells = [r["nr_sell"] for r in rows]
        peak = max(sells[:-2]) if len(sells) > 2 else 0.0
        recent_avg = sum(sells[-2:]) / 2.0
        if peak >= tier1 and recent_avg <= peak * FADE_RATIO:
            entry_hint = (f"📉 主力抛压减弱：卖出额从峰值 {_fmt_usd(peak)}/分钟 回落到 "
                          f"{_fmt_usd(recent_avg)}/分钟（-{(1 - recent_avg / peak) * 100:.0f}%）"
                          "——砸盘力度衰减，可关注企稳后的入场时机（非建议，需自行确认结构）")

    return {
        "dominant": dominant,
        "dominant_cn": ACTOR_CN[dominant],
        "non_retail_share_pct": nr_share,
        "inst_net_usd": round(inst_net, 2),
        "action": action,
        "note": note,
        "burst": burst,
        "entry_hint": entry_hint,
    }


# ─────────────────────────── 模块级运行时状态 ───────────────────────────

_LOCK = threading.Lock()
# {SYMBOL: {"trades": deque, "groups": {fp: {...}}, "minutes": deque}}
_STATE: dict[str, dict] = {}
_REGISTERED = False


def _sym_state(symbol: str) -> dict:
    st = _STATE.get(symbol)
    if st is None:
        st = {"trades": deque(maxlen=TRADES_MAX), "groups": {},
              "minutes": deque(maxlen=BUCKETS_MAX),
              "footprint": deque(maxlen=FOOT_MINUTES_MAX),
              "foot_step": None, "foot_anchor": None, "foot_pending": None}
        _STATE[symbol] = st
    return st


def _foot_step(price: float) -> float:
    """价格档宽：nice_step(price × 0.05%)，复用盘口深度的归整逻辑。"""
    try:
        import jarvis_depth_view as jdv
        s = float(jdv.nice_step(price * FOOT_STEP_PCT))
    except Exception:  # noqa: BLE001 — 兜底镜像 nice_step（1/2/5×10^k）
        raw = price * FOOT_STEP_PCT
        if not math.isfinite(raw) or raw <= 0:
            return 1.0
        exp = math.floor(math.log10(raw))
        frac = raw / (10 ** exp)
        nice = 1.0 if frac < 1.5 else 2.0 if frac < 3.5 else 5.0 if frac < 7.5 else 10.0
        s = nice * (10 ** exp)
    return s if s > 0 else 1.0


def _foot_bucket(price: float, step: float) -> float:
    """价格 → 档下沿（floor 对齐 step；round 消浮点尾差保证跨分钟键一致）。"""
    return round(math.floor(price / step + 1e-9) * step, 10)


def _prune_groups(groups: dict, now_ms: int) -> None:
    """淘汰过期指纹组；超上限时删最老的。"""
    cutoff = now_ms - FP_TTL_S * 1000
    stale = [fp for fp, g in groups.items() if g["last_ts"] < cutoff]
    for fp in stale:
        del groups[fp]
    if len(groups) > FP_MAX:
        for fp, _ in sorted(groups.items(), key=lambda kv: kv[1]["last_ts"])[
                :len(groups) - FP_MAX]:
            del groups[fp]


def ingest(symbol: str, data: dict, *, cfg: dict | None = None) -> None:
    """aggTrade 回调入口（WS 线程内同步调用——O(1) 轻量，永不抛出）。"""
    try:
        parsed = parse_trade(data)
        if parsed is None:
            return
        price, qty, usd, is_buy, ts_ms = parsed
        tier1, _tier2 = _cfg_tiers(cfg)
        fp = fingerprint(qty)
        sym = (symbol or "").upper()
        with _LOCK:
            st = _sym_state(sym)
            st["trades"].append({"ts_ms": ts_ms, "price": price, "qty": qty,
                                 "usd": usd, "is_buy": is_buy, "fp": fp,
                                 "tier": tier_of(usd, tier1)})
            g = st["groups"].get(fp)
            if g is None:
                g = {"fp": fp, "qty": qty, "buy_n": 0, "sell_n": 0,
                     "buy_usd": 0.0, "sell_usd": 0.0, "total_usd": 0.0,
                     "first_ts": ts_ms, "last_ts": ts_ms, "last_price": price}
                st["groups"][fp] = g
            if is_buy:
                g["buy_n"] += 1
                g["buy_usd"] += usd
            else:
                g["sell_n"] += 1
                g["sell_usd"] += usd
            g["total_usd"] += usd
            g["last_ts"] = ts_ms
            g["last_price"] = price
            if len(st["groups"]) > FP_MAX + 60:  # 攒一批再清，摊薄成本
                _prune_groups(st["groups"], ts_ms)

            # 分钟桶：总/非散户 买卖额 + 首末价（burst/fade/价格反应判定用）
            # + 高低价/笔数（tape_minute_bars 持久化与多周期 K 线聚合用）
            minute = ts_ms // 60000
            mins = st["minutes"]
            if not mins or mins[-1]["minute"] != minute:
                mins.append({"minute": minute, "buy": 0.0, "sell": 0.0,
                             "nr_buy": 0.0, "nr_sell": 0.0,
                             "first_price": price, "last_price": price,
                             "high": price, "low": price, "trades_n": 0})
            m = mins[-1]
            m["last_price"] = price
            # .get 兜底：进程热升级时旧桶可能缺新字段
            if price > m.get("high", 0.0):
                m["high"] = price
            lo = m.get("low")
            if lo is None or price < lo:
                m["low"] = price
            m["trades_n"] = m.get("trades_n", 0) + 1
            if is_buy:
                m["buy"] += usd
            else:
                m["sell"] += usd
            # 「非散户」在 ingest 时先按金额粗分（≥$10k），指纹组细分在 summary 做
            if usd >= RETAIL_MAX_USD:
                if is_buy:
                    m["nr_buy"] += usd
                else:
                    m["nr_sell"] += usd

            # ── 足迹（Footprint）：每分钟×价格档 买卖聚合（仅内存，O(1)）──
            fpq = st.get("footprint")
            if fpq is None:  # 热升级兜底：旧 st 无该键
                fpq = st["footprint"] = deque(maxlen=FOOT_MINUTES_MAX)
            step = st.get("foot_step")
            if step is None or step <= 0:
                step = _foot_step(price)
                st["foot_step"] = step
                st["foot_anchor"] = price
            else:
                anchor = st.get("foot_anchor") or price
                # 漂移超限只登记 pending，新分钟才生效（避免同分钟混档）
                if st.get("foot_pending") is None and anchor > 0 \
                        and abs(price / anchor - 1.0) > FOOT_DRIFT_PCT:
                    st["foot_pending"] = _foot_step(price)
                    st["foot_anchor"] = price
            if not fpq or fpq[-1]["minute"] != minute:
                pend = st.get("foot_pending")
                if pend:
                    step = st["foot_step"] = pend
                    st["foot_pending"] = None
                fpq.append({"minute": minute, "step": step, "cells": {},
                            "lo": None, "hi": None})
            fb = fpq[-1]
            cell_key = _foot_bucket(price, fb["step"])
            cells = fb["cells"]
            cell = cells.get(cell_key)
            if cell is None:
                if len(cells) >= FOOT_CELLS_MAX:
                    # 档满：归并到较近的边缘档（防内存爆，成交额不丢）
                    lo, hi = fb["lo"], fb["hi"]
                    cell_key = (hi if abs(cell_key - hi) <= abs(cell_key - lo)
                                else lo)
                    cell = cells[cell_key]
                else:
                    cell = cells[cell_key] = [0.0, 0.0, 0, 0]
                    if fb["lo"] is None or cell_key < fb["lo"]:
                        fb["lo"] = cell_key
                    if fb["hi"] is None or cell_key > fb["hi"]:
                        fb["hi"] = cell_key
            if is_buy:
                cell[0] += usd
                cell[2] += 1
            else:
                cell[1] += usd
                cell[3] += 1
    except Exception:  # noqa: BLE001 — WS 回调铁律：绝不向数据流抛出
        pass


def summary(symbol: str, cfg: dict | None = None, window_min: int = 15,
            now_ms: int | None = None) -> dict:
    """窗口画像（REST 消费入口）：份额分解 + 指纹表 + 判定 + 最近成交。"""
    tier1, _tier2 = _cfg_tiers(cfg)
    sym = (symbol or "").upper()
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    win = max(1, min(int(window_min), 240))
    cutoff = now - win * 60_000
    with _LOCK:
        st = _STATE.get(sym)
        if st is None or not st["trades"]:
            return {"symbol": sym, "active": False, "window_min": win,
                    "disclaimer": DISCLAIMER}
        trades = [t for t in st["trades"] if t["ts_ms"] >= cutoff]
        groups = {fp: dict(g) for fp, g in st["groups"].items()}
        minute_rows = [dict(m) for m in st["minutes"]
                       if m["minute"] >= cutoff // 60000]

    breakdown = build_breakdown(trades, tier1, groups)

    # 窗口价格变化（主力行为定性用）
    first_p = next((m["first_price"] for m in minute_rows if m.get("first_price")), None)
    last_p = next((m["last_price"] for m in reversed(minute_rows)
                   if m.get("last_price")), None)
    price_chg = (round((last_p / first_p - 1) * 100, 4)
                 if first_p and last_p and first_p > 0 else None)

    verdict = build_verdict(breakdown, minute_rows, price_chg, tier1, now)

    # 指纹聚合表：窗口内活跃（last_ts≥cutoff）且 n≥2，按总额取前 14
    fps = []
    for g in groups.values():
        n = g["buy_n"] + g["sell_n"]
        if g["last_ts"] < cutoff or n < 2:
            continue
        cls = classify_group(g) or ("inst" if g["total_usd"] / n >= tier1 else
                                    ("mid" if g["total_usd"] / n >= RETAIL_MAX_USD
                                     else "retail"))
        fps.append({
            "fp": g["fp"], "qty": g["qty"], "n": n,
            "buy_n": g["buy_n"], "sell_n": g["sell_n"],
            "avg_usd": round(g["total_usd"] / n, 2),
            "total_usd": round(g["total_usd"], 2),
            "net_usd": round(g["buy_usd"] - g["sell_usd"], 2),
            "cls": cls, "cls_cn": ACTOR_CN[cls],
            "first_ts": g["first_ts"], "last_ts": g["last_ts"],
            "last_price": g["last_price"],
        })
    fps.sort(key=lambda x: x["total_usd"], reverse=True)

    # 最近成交（新的在前），附单笔归类
    recent = []
    for t in trades[-80:][::-1]:
        g = groups.get(t["fp"])
        cls = classify_trade(t["usd"], tier1, classify_group(g) if g else "")
        recent.append({**t, "cls": cls, "cls_cn": ACTOR_CN[cls],
                       "fp_n": (g["buy_n"] + g["sell_n"]) if g else 1})

    # 分钟压力序列（前端迷你图）：时间升序
    series = [{"ts": m["minute"] * 60, "buy": round(m["buy"], 2),
               "sell": round(m["sell"], 2), "nr_buy": round(m["nr_buy"], 2),
               "nr_sell": round(m["nr_sell"], 2), "price": m["last_price"]}
              for m in minute_rows]

    return {
        "symbol": sym, "active": True, "window_min": win,
        "price_change_pct": price_chg,
        "breakdown": breakdown, "verdict": verdict,
        "fingerprints": fps[:14], "recent": recent, "series": series,
        "tier1_usd": tier1, "retail_max_usd": RETAIL_MAX_USD,
        "disclaimer": DISCLAIMER,
    }


def register() -> bool:
    """挂到 WS aggTrade 流（幂等）。WS 模块缺失/未启动返回 False。"""
    global _REGISTERED
    if _REGISTERED:
        return True
    try:
        import jarvis_ws_stream as jws
        ok = jws.register_callback("aggTrade", ingest)
        _REGISTERED = bool(ok)
        return _REGISTERED
    except Exception:  # noqa: BLE001
        return False


def reset_state() -> None:
    """清空运行时状态（smoketest 隔离用）。"""
    with _LOCK:
        _STATE.clear()


# ─────────────────────────── 分钟聚合持久化（盘口成交流 K 线复盘）───────────────────────────
#
# 内存分钟桶（deque 上限 240 个）重启即丢、最多覆盖 4 小时，撑不起 4h/1d 复盘。
# start_persist() 起后台 daemon 线程每 30s 把「已完结的分钟」upsert 进
# tape_minute_bars（经 jarvis_db 兼容层，pg 可切）；WS ingest 线程内绝不落盘。

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")

PERSIST_INTERVAL_S = 30.0    # 后台 flush 周期
RETENTION_DAYS = 14          # 分钟行保留期
_PRUNE_INTERVAL_S = 3600.0   # 保留期清理节流：每小时最多一次
_LAST_PRUNE = 0.0

# API 周期 → 秒（bars() 分桶聚合用；epoch 对齐即 UTC 对齐）
INTERVALS_S = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
               "1h": 3600, "4h": 14400, "1d": 86400}

_INITED = False
_PERSIST_STARTED = False


def _conn():
    os.makedirs(DB_DIR, exist_ok=True)
    return jdb.connect(DB_PATH)


def init_db() -> None:
    global _INITED
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tape_minute_bars (
                symbol      TEXT NOT NULL,
                minute      INTEGER NOT NULL,
                buy_usd     REAL,
                sell_usd    REAL,
                nr_buy_usd  REAL,
                nr_sell_usd REAL,
                open_price  REAL,
                close_price REAL,
                high_price  REAL,
                low_price   REAL,
                trades_n    INTEGER,
                PRIMARY KEY (symbol, minute)
            )
            """
        )
    _INITED = True


def _ensure_init() -> None:
    if not _INITED:
        init_db()


def _mem_bar(m: dict) -> dict:
    """内存分钟桶 → 落库/聚合统一形状（旧桶缺新字段时兜底）。"""
    first = m.get("first_price")
    last = m.get("last_price")
    hi = m.get("high")
    lo = m.get("low")
    if hi is None:
        hi = max(v for v in (first, last) if v is not None) if (first or last) else None
    if lo is None:
        lo = min(v for v in (first, last) if v is not None) if (first or last) else None
    return {"minute": int(m["minute"]),
            "buy": float(m.get("buy") or 0.0), "sell": float(m.get("sell") or 0.0),
            "nr_buy": float(m.get("nr_buy") or 0.0),
            "nr_sell": float(m.get("nr_sell") or 0.0),
            "open": first, "close": last, "high": hi, "low": lo,
            "trades": int(m.get("trades_n") or 0)}


def _merge_minute(dst: dict, src: dict) -> None:
    """同一分钟出现两个桶（乱序迟到极罕见）时合并：额/笔数累加，OHLC 取极值与先后。"""
    dst["buy"] += src["buy"]
    dst["sell"] += src["sell"]
    dst["nr_buy"] += src["nr_buy"]
    dst["nr_sell"] += src["nr_sell"]
    dst["trades"] += src["trades"]
    dst["close"] = src["close"] if src["close"] is not None else dst["close"]
    if src["high"] is not None and (dst["high"] is None or src["high"] > dst["high"]):
        dst["high"] = src["high"]
    if src["low"] is not None and (dst["low"] is None or src["low"] < dst["low"]):
        dst["low"] = src["low"]
    # open 保留先出现的桶


def flush_bars(now_ms: int | None = None) -> int:
    """把各币种「已完结的分钟」（minute < 当前分钟）upsert 到 tape_minute_bars。

    持锁只做内存快照，DB 写在锁外（绝不阻塞 WS ingest）；写成功后才推进
    per-symbol 落盘水位（失败下轮重试，upsert 幂等）。顺带按节流做保留期清理。
    Returns: 本轮写入（含更新）的行数；失败只记日志返回已写数。
    """
    global _LAST_PRUNE
    written = 0
    try:
        _ensure_init()
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        cur_min = now // 60000

        # ── 持锁快照：收集每币种待落盘分钟（水位 < minute < 当前分钟）──
        pending: dict[str, dict[int, dict]] = {}
        with _LOCK:
            for sym, st in _STATE.items():
                mark = int(st.get("flushed_min") or 0)
                rows: dict[int, dict] = {}
                for m in st["minutes"]:
                    mn = int(m["minute"])
                    if mn >= cur_min or mn <= mark:
                        continue
                    bar = _mem_bar(m)
                    if mn in rows:
                        _merge_minute(rows[mn], bar)
                    else:
                        rows[mn] = bar
                if rows:
                    pending[sym] = rows

        # ── 锁外写库 ──
        for sym, rows in pending.items():
            try:
                with _conn() as conn:
                    for mn in sorted(rows):
                        b = rows[mn]
                        conn.execute(
                            """
                            INSERT INTO tape_minute_bars
                              (symbol, minute, buy_usd, sell_usd, nr_buy_usd,
                               nr_sell_usd, open_price, close_price,
                               high_price, low_price, trades_n)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(symbol, minute) DO UPDATE SET
                              buy_usd=excluded.buy_usd,
                              sell_usd=excluded.sell_usd,
                              nr_buy_usd=excluded.nr_buy_usd,
                              nr_sell_usd=excluded.nr_sell_usd,
                              open_price=excluded.open_price,
                              close_price=excluded.close_price,
                              high_price=excluded.high_price,
                              low_price=excluded.low_price,
                              trades_n=excluded.trades_n
                            """,
                            (sym, mn, round(b["buy"], 4), round(b["sell"], 4),
                             round(b["nr_buy"], 4), round(b["nr_sell"], 4),
                             b["open"], b["close"], b["high"], b["low"],
                             b["trades"]))
                written += len(rows)
                # 写成功才推进水位（rows 非空才会进到这里）
                with _LOCK:
                    st = _STATE.get(sym)
                    if st is not None:
                        st["flushed_min"] = max(int(st.get("flushed_min") or 0),
                                                max(rows))
            except Exception as exc:  # noqa: BLE001 — 单币失败不影响其它币种
                print(f"[tape-persist] {sym} flush 失败: {exc!r}", flush=True)

        # ── 保留期清理（每小时最多一次）──
        if time.time() - _LAST_PRUNE > _PRUNE_INTERVAL_S:
            _LAST_PRUNE = time.time()
            prune_old(now)
        return written
    except Exception as exc:  # noqa: BLE001 — 持久化绝不向调用方抛出
        print(f"[tape-persist] flush 异常: {exc!r}", flush=True)
        return written


def prune_old(now_ms: int | None = None) -> int:
    """删除保留期（14 天）之外的分钟行；返回删除行数，失败返回 0。"""
    try:
        _ensure_init()
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        cutoff_min = now // 60000 - RETENTION_DAYS * 24 * 60
        with _conn() as conn:
            cur = conn.execute(
                "DELETE FROM tape_minute_bars WHERE minute < ?", (cutoff_min,))
            return cur.rowcount if cur.rowcount is not None else 0
    except Exception as exc:  # noqa: BLE001
        print(f"[tape-persist] 保留期清理失败: {exc!r}", flush=True)
        return 0


def start_persist(interval_s: float = PERSIST_INTERVAL_S) -> bool:
    """启动后台落盘 daemon 线程（幂等）。失败返回 False，绝不抛出。"""
    global _PERSIST_STARTED
    if _PERSIST_STARTED:
        return True
    try:
        def _loop() -> None:
            while True:
                time.sleep(max(5.0, float(interval_s)))
                try:
                    flush_bars()
                except Exception:  # noqa: BLE001 — 双保险，flush 本身不抛
                    pass

        threading.Thread(target=_loop, daemon=True,
                         name="jarvis-tape-persist").start()
        _PERSIST_STARTED = True
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[tape-persist] 启动失败: {exc!r}", flush=True)
        return False


def bars(symbol: str, interval: str = "1m", limit: int = 200,
         now_ms: int | None = None) -> dict:
    """多周期成交流 K 线（REST 消费入口）：库内历史 + 内存未落盘分钟合并聚合。

    Returns: {ok, symbol, interval, bars:[{ts, buy, sell, net, nr_buy, nr_sell,
    nr_net, open, high, low, close, trades}], source}；ts 为桶起点 epoch 秒
    （整除对齐 = UTC 对齐），时间升序，最多 limit 根（含当前未完结桶）。
    """
    sym = (symbol or "").upper()
    try:
        itv = INTERVALS_S.get(interval)
        if itv is None:
            return {"ok": False, "symbol": sym, "interval": interval,
                    "bars": [], "source": "none",
                    "error": f"interval 无效：{interval}（可选 {'/'.join(INTERVALS_S)}）"}
        lim = max(1, min(int(limit), 500))
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        end_bucket = now // 1000 // itv * itv
        start_bucket = end_bucket - (lim - 1) * itv
        min_minute = start_bucket // 60

        # 1) 库内历史（窗口内）
        merged: dict[int, dict] = {}
        _ensure_init()
        with _conn() as conn:
            cur = conn.execute(
                "SELECT minute, buy_usd, sell_usd, nr_buy_usd, nr_sell_usd, "
                "open_price, close_price, high_price, low_price, trades_n "
                "FROM tape_minute_bars WHERE symbol=? AND minute>=? "
                "ORDER BY minute ASC",
                (sym, min_minute))
            for r in cur.fetchall():
                merged[int(r["minute"])] = {
                    "minute": int(r["minute"]),
                    "buy": float(r["buy_usd"] or 0.0),
                    "sell": float(r["sell_usd"] or 0.0),
                    "nr_buy": float(r["nr_buy_usd"] or 0.0),
                    "nr_sell": float(r["nr_sell_usd"] or 0.0),
                    "open": r["open_price"], "close": r["close_price"],
                    "high": r["high_price"], "low": r["low_price"],
                    "trades": int(r["trades_n"] or 0),
                }
        from_db = len(merged)

        # 2) 内存分钟覆盖（未落盘 + 当前未完结分钟；同分钟以内存为准）
        with _LOCK:
            st = _STATE.get(sym)
            mem = [dict(m) for m in st["minutes"]] if st else []
        from_mem = 0
        for m in mem:
            mn = int(m["minute"])
            if mn < min_minute:
                continue
            merged[mn] = _mem_bar(m)  # 同分钟已落库也覆盖：同源同值且内存更新鲜
            from_mem += 1

        # 3) 按 interval 分桶聚合（分钟升序保证 open/close 先后正确）
        buckets: dict[int, dict] = {}
        for mn in sorted(merged):
            b = merged[mn]
            ts = mn * 60 // itv * itv
            if ts < start_bucket or ts > end_bucket:
                continue
            k = buckets.get(ts)
            if k is None:
                buckets[ts] = k = {"ts": ts, "buy": 0.0, "sell": 0.0,
                                   "nr_buy": 0.0, "nr_sell": 0.0,
                                   "open": b["open"], "close": b["close"],
                                   "high": b["high"], "low": b["low"],
                                   "trades": 0}
            k["buy"] += b["buy"]
            k["sell"] += b["sell"]
            k["nr_buy"] += b["nr_buy"]
            k["nr_sell"] += b["nr_sell"]
            k["trades"] += b["trades"]
            if k["open"] is None:
                k["open"] = b["open"]
            if b["close"] is not None:
                k["close"] = b["close"]
            if b["high"] is not None and (k["high"] is None or b["high"] > k["high"]):
                k["high"] = b["high"]
            if b["low"] is not None and (k["low"] is None or b["low"] < k["low"]):
                k["low"] = b["low"]

        out = []
        for ts in sorted(buckets):
            k = buckets[ts]
            out.append({"ts": ts,
                        "buy": round(k["buy"], 2), "sell": round(k["sell"], 2),
                        "net": round(k["buy"] - k["sell"], 2),
                        "nr_buy": round(k["nr_buy"], 2),
                        "nr_sell": round(k["nr_sell"], 2),
                        "nr_net": round(k["nr_buy"] - k["nr_sell"], 2),
                        "open": k["open"], "high": k["high"],
                        "low": k["low"], "close": k["close"],
                        "trades": k["trades"]})
        out = out[-lim:]
        source = ("db+mem" if from_db and from_mem else
                  "db" if from_db else "mem" if from_mem else "empty")
        return {"ok": True, "symbol": sym, "interval": interval,
                "bars": out, "source": source}
    except Exception as exc:  # noqa: BLE001 — 查询失败返回错误封套，不抛 500
        return {"ok": False, "symbol": sym, "interval": interval,
                "bars": [], "source": "none", "error": repr(exc)[:200]}


# ─────────────────────────── 足迹图（Footprint）查询 ───────────────────────────

FOOT_INTERVALS_S = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800}


def _foot_overall_verdict(buy: float, sell: float, actors: dict) -> str:
    """整体多空判词：买卖占比定调，机构/散户方向相反时追加接盘/派发提示。"""
    tot = buy + sell
    if tot <= 0:
        return "窗口内无成交数据"
    lp = buy / tot * 100.0
    if lp >= 55:
        base = f"当前主动买盘占优（{lp:.0f}%），做多情绪主导"
    elif lp <= 45:
        base = f"当前主动卖盘占优（{100 - lp:.0f}%），做空情绪主导"
    else:
        base = "多空拉锯，买卖大致均衡"
    inst_lp = (actors.get("inst") or {}).get("long_pct")
    retail_lp = (actors.get("retail") or {}).get("long_pct")
    if inst_lp is not None and retail_lp is not None:
        if inst_lp >= 55 and retail_lp <= 45:
            base += "——机构偏买、散户偏卖，疑似机构在接散户筹码"
        elif inst_lp <= 45 and retail_lp >= 55:
            base += "——散户偏买、机构偏卖，疑似机构在向散户派发"
    return base


def footprint(symbol: str, interval: str = "1m", limit: int = 30,
              buckets: int = 40, cfg: dict | None = None,
              now_ms: int | None = None) -> dict:
    """足迹图数据（REST 消费入口）：每 bar 按价格档拆开的买卖聚合 + 主体多空统计。

    Returns: {ok, symbol, interval, bucket(档宽), bars:[{ts, open, high, low,
    close, total, buy, sell, delta, cvd, rows:[{price 降序, buy, sell,
    flag:"buy_imb"|"sell_imb"|None}]}], actors:{retail/mid/inst/maker:{...,
    long_pct, verdict_cn}, overall:{buy, sell, delta, verdict_cn}}, active,
    source, disclaimer}。仅内存（约近 4 小时），CVD 自窗口起点累计。
    """
    sym = (symbol or "").upper()
    try:
        itv = FOOT_INTERVALS_S.get(interval)
        if itv is None:
            return {"ok": False, "symbol": sym, "interval": interval,
                    "bucket": None, "bars": [], "actors": None, "active": False,
                    "source": "none", "disclaimer": DISCLAIMER,
                    "error": f"interval 无效：{interval}"
                             f"（可选 {'/'.join(FOOT_INTERVALS_S)}）"}
        lim = max(1, min(int(limit), 60))
        bkt = max(5, min(int(buckets), 60))
        tier1, _tier2 = _cfg_tiers(cfg)
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        end_bucket = now // 1000 // itv * itv
        start_bucket = end_bucket - (lim - 1) * itv
        min_minute = start_bucket // 60

        # ── 持锁快照（cells 深拷到值层，锁外聚合不受 WS 并发影响）──
        with _LOCK:
            st = _STATE.get(sym)
            if st is None:
                fp_rows: list[dict] = []
                min_rows: list[dict] = []
                trades: list[dict] = []
                groups: dict[str, dict] = {}
            else:
                fp_rows = [{"minute": int(f["minute"]), "step": f["step"],
                            "cells": {p: list(v) for p, v in f["cells"].items()}}
                           for f in (st.get("footprint") or [])
                           if f["minute"] >= min_minute]
                min_rows = [dict(m) for m in st["minutes"]
                            if m["minute"] >= min_minute]
                trades = [t for t in st["trades"]
                          if t["ts_ms"] >= start_bucket * 1000]
                groups = {fp: dict(g) for fp, g in st["groups"].items()}

        # ── 主体多空统计（窗口与足迹图一致；复用 breakdown 的 long_pct/判词）──
        breakdown = build_breakdown(trades, tier1, groups)
        actors_out: dict[str, dict] = {}
        for k, a in breakdown["actors"].items():
            actors_out[k] = {"actor_cn": a["actor_cn"], "buy": a["buy_usd"],
                             "sell": a["sell_usd"], "net": a["net_usd"],
                             "long_pct": a["long_pct"],
                             "verdict_cn": a["verdict_cn"]}
        o_buy = round(sum(a["buy"] for a in actors_out.values()), 2)
        o_sell = round(sum(a["sell"] for a in actors_out.values()), 2)
        actors_out["overall"] = {
            "buy": o_buy, "sell": o_sell, "delta": round(o_buy - o_sell, 2),
            "verdict_cn": _foot_overall_verdict(o_buy, o_sell, actors_out)}

        if not fp_rows:
            return {"ok": True, "symbol": sym, "interval": interval,
                    "bucket": None, "bars": [], "actors": actors_out,
                    "active": False, "source": "empty",
                    "disclaimer": DISCLAIMER}

        # ── 统一重桶：bar 内 step 混用时以最新 step 归一 ──
        step = float(fp_rows[-1]["step"] or 1.0)
        bars_cells: dict[int, dict[float, list]] = {}
        for f in fp_rows:
            ts = f["minute"] * 60 // itv * itv
            if ts < start_bucket or ts > end_bucket:
                continue
            cells = bars_cells.setdefault(ts, {})
            for p, v in f["cells"].items():
                rb = _foot_bucket(float(p), step)
                c = cells.get(rb)
                if c is None:
                    cells[rb] = c = [0.0, 0.0, 0, 0]
                c[0] += v[0]
                c[1] += v[1]
                c[2] += v[2]
                c[3] += v[3]

        # ── OHLC：从分钟桶聚合（与 bars() 同口径）──
        ohlc: dict[int, dict] = {}
        for m in min_rows:
            ts = m["minute"] * 60 // itv * itv
            if ts < start_bucket or ts > end_bucket:
                continue
            mb = _mem_bar(m)
            o = ohlc.get(ts)
            if o is None:
                ohlc[ts] = {"open": mb["open"], "close": mb["close"],
                            "high": mb["high"], "low": mb["low"]}
                continue
            if o["open"] is None:
                o["open"] = mb["open"]
            if mb["close"] is not None:
                o["close"] = mb["close"]
            if mb["high"] is not None and (o["high"] is None or mb["high"] > o["high"]):
                o["high"] = mb["high"]
            if mb["low"] is not None and (o["low"] is None or mb["low"] < o["low"]):
                o["low"] = mb["low"]

        # ── 逐 bar 输出：buckets 上限边缘归并 + 失衡 flag + CVD 累计 ──
        out_bars: list[dict] = []
        cvd = 0.0
        for ts in sorted(bars_cells):
            rows_sorted = sorted(bars_cells[ts].items(),
                                 key=lambda kv: kv[0], reverse=True)  # 价降序
            # 超出 buckets 上限：把额较小的边缘档向内归并（保住量能密集区）
            while len(rows_sorted) > bkt:
                top_amt = rows_sorted[0][1][0] + rows_sorted[0][1][1]
                bot_amt = rows_sorted[-1][1][0] + rows_sorted[-1][1][1]
                if top_amt <= bot_amt:
                    src, dst = rows_sorted[0][1], rows_sorted[1][1]
                    rows_sorted.pop(0)
                else:
                    src, dst = rows_sorted[-1][1], rows_sorted[-2][1]
                    rows_sorted.pop()
                for i in range(4):
                    dst[i] += src[i]

            n_rows = len(rows_sorted)
            avg_amt = (sum(v[0] + v[1] for _, v in rows_sorted) / n_rows
                       if n_rows else 0.0)
            rows_out: list[dict] = []
            bar_buy = bar_sell = 0.0
            trades_n = 0
            for p, v in rows_sorted:
                buy, sell = v[0], v[1]
                bar_buy += buy
                bar_sell += sell
                trades_n += v[2] + v[3]
                flag = None
                if avg_amt > 0 and max(buy, sell) >= avg_amt * FOOT_IMB_VOL_X:
                    if buy >= sell * FOOT_IMB_RATIO and buy > 0:
                        flag = "buy_imb"
                    elif sell >= buy * FOOT_IMB_RATIO and sell > 0:
                        flag = "sell_imb"
                rows_out.append({"price": p, "buy": round(buy, 2),
                                 "sell": round(sell, 2), "flag": flag})
            o = ohlc.get(ts) or {"open": None, "close": None,
                                 "high": None, "low": None}
            delta = round(bar_buy - bar_sell, 2)
            cvd = round(cvd + delta, 2)
            out_bars.append({"ts": ts, "open": o["open"], "high": o["high"],
                             "low": o["low"], "close": o["close"],
                             "total": round(bar_buy + bar_sell, 2),
                             "buy": round(bar_buy, 2),
                             "sell": round(bar_sell, 2),
                             "delta": delta, "cvd": cvd, "trades": trades_n,
                             "rows": rows_out})

        return {"ok": True, "symbol": sym, "interval": interval,
                "bucket": step, "bars": out_bars, "actors": actors_out,
                "active": bool(out_bars), "source": "mem",
                "disclaimer": DISCLAIMER}
    except Exception as exc:  # noqa: BLE001 — 查询失败返回错误封套，不抛 500
        return {"ok": False, "symbol": sym, "interval": interval,
                "bucket": None, "bars": [], "actors": None, "active": False,
                "source": "none", "disclaimer": DISCLAIMER,
                "error": repr(exc)[:200]}


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="成交流行为主体分类")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--seconds", type=int, default=30)
    args = ap.parse_args()
    import jarvis_ws_stream as jws
    register()
    jws.start([args.symbol.upper()])
    print(f"收流 {args.seconds}s 后输出 {args.symbol} 画像…")
    time.sleep(max(5, args.seconds))
    print(json.dumps(summary(args.symbol), ensure_ascii=False, indent=2))
    jws.stop()
