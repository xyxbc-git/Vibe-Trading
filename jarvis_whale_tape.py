#!/usr/bin/env python3
"""贾维斯 JARVIS — 大单流监控 whale tape（M2 s5，庄家审计 P0-2 落地）。

K 线级 Delta（jarvis_delta_flow）看的是 bar 粒度的「谁在成交」；本模块下沉到
**逐笔粒度**：消费 jarvis_ws_stream 的 aggTrade 流，按成交额分层聚合大单行为，
让「主力真实进出」从散户噪声里显形。

三层能力：
  1) 分层聚合   单笔成交额 ≥ tier1（默认 $100k）计入大单；≥ tier2（默认 $1M）
                记巨单。低于 tier1 的只累计总量（算大单占比的分母）。
  2) 滚动窗口   分钟桶聚合（内存 O(窗口分钟数)），窗口长走配置 whale_window_min
                （默认 15 分钟）：大单净流（买-卖）、大单买卖比、大单占总成交比。
  3) 异常事件   ① single_super：单笔 ≥ tier2 巨单
                ② consecutive：窗口内同向大单连续 ≥ N 笔（吸筹/派发嫌疑）
                ③ divergence：大单净流方向与价格变化背离（大买单价不涨 =
                  出货吸收；大卖单价不跌 = 接货吸收）

架构（与 jarvis 家族同风格）：
  - 纯函数核心：_parse_trade / _bucket_update / build_summary 只吃 dict/list，
    离线可测（_whale_tape_smoketest.py 注入 mock aggTrade 序列）。
  - 模块级状态：register() 幂等挂到 WS aggTrade 回调；ingest() 在 WS 线程内
    同步执行，只做 O(1) 解析+入桶+计数，绝不联网/落盘/抛出。
  - REST：GET /api/whale/summary（jarvis_dashboard 暴露）。

aggTrade 契约（币安合约/现货同构，现货降级模式数据等价）：
  {"e":"aggTrade","s":"BTCUSDT","p":"64230.5","q":"0.5","T":1720000000000,"m":false}
  m=false → 主动买（taker buy）；m=true → 主动卖。

配置（全部走 jarvis_config 配置中心，Settings UI 可改）：
  signal.whale_tier1_usd / whale_tier2_usd / whale_window_min /
  whale_seatbelt_enabled（安全带可选因子开关，消费方为 dashboard 共识端点）
"""

from __future__ import annotations

import threading
import time
from collections import deque

DISCLAIMER = ("大单流基于币安 aggTrade 逐笔归集成交（现货降级模式数据等价），"
              "分层阈值与异常事件为概率性痕迹识别，非投资建议。")

# 同向大单连续 ≥ 该笔数触发 consecutive 事件（被反向大单打断即重置）
CONSEC_N = 5
# divergence 判定：窗口价格变化绝对值 < 该百分比视为「价格没动」
FLAT_PRICE_PCT = 0.05
# 同类事件（consecutive/divergence）同币种最小间隔秒数（去重节流）
EVENT_COOLDOWN_S = 600
# 每币种事件环形缓冲长度
EVENTS_MAX = 50
# 分钟桶保留上限（防配置窗口被调大后内存无界；240 对齐 whale_window_min 上限）
BUCKETS_MAX = 240


def _cfg_vals(cfg: dict | None = None) -> dict:
    """取本模块用到的配置键（缺失回退内置默认，永不抛出）。"""
    if cfg is None:
        try:
            import jarvis_config as jc
            cfg = jc.load()
        except Exception:  # noqa: BLE001
            cfg = {}
    def _f(key: str, dv: float) -> float:
        try:
            v = float(cfg.get(key, dv))
            return v if v > 0 else dv
        except (TypeError, ValueError):
            return dv
    return {
        "tier1": _f("whale_tier1_usd", 100000.0),
        "tier2": _f("whale_tier2_usd", 1000000.0),
        "window_min": max(1, min(240, int(_f("whale_window_min", 15)))),
    }


# ─────────────────────────── 纯函数：解析 / 分桶 ───────────────────────────


def _parse_trade(data: dict) -> tuple[float, float, float, bool, int] | None:
    """aggTrade dict → (price, qty, notional, is_buy, ts_ms)；坏数据返回 None。"""
    try:
        price = float(data.get("p") or 0)
        qty = float(data.get("q") or 0)
        if price <= 0 or qty <= 0:
            return None
        is_buy = not bool(data.get("m"))  # m=true 买方是 maker → 主动卖
        ts_ms = int(data.get("T") or 0) or int(time.time() * 1000)
        return price, qty, round(price * qty, 4), is_buy, ts_ms
    except (TypeError, ValueError):
        return None


def _new_bucket(minute: int) -> dict:
    return {"minute": minute, "total_usd": 0.0, "total_n": 0,
            "whale_buy_usd": 0.0, "whale_sell_usd": 0.0,
            "whale_buy_n": 0, "whale_sell_n": 0,
            "first_price": None, "last_price": None}


def _bucket_update(buckets: deque, price: float, notional: float,
                   is_buy: bool, ts_ms: int, tier1: float) -> None:
    """把一笔成交并入分钟桶序列（末桶追加或开新桶）。纯数据结构操作。"""
    minute = int(ts_ms // 60000)
    if not buckets or buckets[-1]["minute"] != minute:
        buckets.append(_new_bucket(minute))
    b = buckets[-1]
    b["total_usd"] += notional
    b["total_n"] += 1
    if b["first_price"] is None:
        b["first_price"] = price
    b["last_price"] = price
    if notional >= tier1:
        if is_buy:
            b["whale_buy_usd"] += notional
            b["whale_buy_n"] += 1
        else:
            b["whale_sell_usd"] += notional
            b["whale_sell_n"] += 1


def _window_buckets(buckets: list[dict], window_min: int, now_ms: int) -> list[dict]:
    """截取窗口内的分钟桶（含当前分钟）。"""
    cutoff = int(now_ms // 60000) - window_min + 1
    return [b for b in buckets if b["minute"] >= cutoff]


def build_summary(buckets: list[dict], recent_whales: list[dict],
                  events: list[dict], *, tier1: float, tier2: float,
                  window_min: int, now_ms: int | None = None) -> dict:
    """窗口统计 + 背离检测（纯函数，smoketest 直测入口）。

    Args:
        buckets:       分钟桶 list（时间升序）
        recent_whales: 最近大单明细 [{ts_ms, price, usd, is_buy}]（展示/佐证用）
        events:        已检测到的异常事件 list（时间升序，divergence 在本函数补充）
    Returns:
        {net_usd, buy_usd, sell_usd, buy_sell_ratio, whale_share_pct,
         whale_n, price_change_pct, divergence, events, ...}
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    win = _window_buckets(buckets, window_min, now)
    buy = sum(b["whale_buy_usd"] for b in win)
    sell = sum(b["whale_sell_usd"] for b in win)
    total = sum(b["total_usd"] for b in win)
    n = sum(b["whale_buy_n"] + b["whale_sell_n"] for b in win)
    net = round(buy - sell, 2)
    ratio = round(buy / sell, 3) if sell > 0 else (None if buy == 0 else float("inf"))
    share = round((buy + sell) / total * 100, 2) if total > 0 else 0.0

    # 窗口价格变化：首桶首价 → 末桶末价
    first_p = next((b["first_price"] for b in win if b["first_price"]), None)
    last_p = next((b["last_price"] for b in reversed(win) if b["last_price"]), None)
    price_chg = (round((last_p / first_p - 1.0) * 100, 4)
                 if first_p and last_p and first_p > 0 else None)

    # ③ 背离检测：净流显著（|net| ≥ tier2）且价格不同向
    divergence = None
    if price_chg is not None and abs(net) >= tier2:
        if net > 0 and price_chg <= FLAT_PRICE_PCT:
            divergence = {
                "side": "sell_into_buys",
                "note": (f"窗口大单净买入 {_fmt_usd(net)} 但价格{'下跌' if price_chg < -FLAT_PRICE_PCT else '滞涨'}"
                         f"（{price_chg:+.2f}%）——大买单被吸收，出货嫌疑"),
            }
        elif net < 0 and price_chg >= -FLAT_PRICE_PCT:
            divergence = {
                "side": "buy_into_sells",
                "note": (f"窗口大单净卖出 {_fmt_usd(abs(net))} 但价格{'上涨' if price_chg > FLAT_PRICE_PCT else '未跌'}"
                         f"（{price_chg:+.2f}%）——大卖单被接走，吸筹嫌疑"),
            }

    if ratio == float("inf"):
        ratio = None  # 全买无卖：ratio 无定义，前端按 net 方向展示

    return {
        "window_min": window_min,
        "net_usd": net,
        "buy_usd": round(buy, 2),
        "sell_usd": round(sell, 2),
        "buy_sell_ratio": ratio,
        "whale_share_pct": share,
        "whale_n": n,
        "total_usd": round(total, 2),
        "price_change_pct": price_chg,
        "divergence": divergence,
        "recent_whales": recent_whales[-8:][::-1],   # 最近在前
        "events": events[-10:][::-1],
        "tier1_usd": tier1,
        "tier2_usd": tier2,
    }


def _fmt_usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


# ─────────────────────────── 模块级运行时状态 ───────────────────────────

_LOCK = threading.Lock()
# {SYMBOL: {"buckets": deque, "whales": deque, "events": deque,
#           "consec_side": str|None, "consec_n": int, "consec_fired": bool,
#           "last_event_ts": {kind: unix_s}}}
_STATE: dict[str, dict] = {}
_REGISTERED = False


def _sym_state(symbol: str) -> dict:
    st = _STATE.get(symbol)
    if st is None:
        st = {"buckets": deque(maxlen=BUCKETS_MAX), "whales": deque(maxlen=100),
              "events": deque(maxlen=EVENTS_MAX), "consec_side": None,
              "consec_n": 0, "consec_fired": False, "last_event_ts": {}}
        _STATE[symbol] = st
    return st


def _push_event(st: dict, kind: str, side: str, note: str, usd: float,
                price: float, ts_ms: int, *, cooldown: bool = True) -> bool:
    """事件入列（consecutive/divergence 类同币同类节流；super 单不节流）。"""
    now_s = ts_ms / 1000.0
    if cooldown and now_s - st["last_event_ts"].get(kind, 0.0) < EVENT_COOLDOWN_S:
        return False
    st["last_event_ts"][kind] = now_s
    st["events"].append({"ts_ms": ts_ms, "kind": kind, "side": side,
                         "note": note, "usd": round(usd, 2), "price": price})
    return True


def ingest(symbol: str, data: dict, *, cfg: dict | None = None) -> None:
    """aggTrade 回调入口（WS 线程内同步调用——O(1) 轻量，永不抛出）。

    分层入桶 + 巨单/连续同向事件即时检测；divergence 在 summary 时算
    （需要窗口级价格对比，不适合逐笔算）。
    """
    try:
        parsed = _parse_trade(data)
        if parsed is None:
            return
        price, _qty, notional, is_buy, ts_ms = parsed
        c = _cfg_vals(cfg)
        sym = (symbol or "").upper()
        with _LOCK:
            st = _sym_state(sym)
            _bucket_update(st["buckets"], price, notional, is_buy, ts_ms, c["tier1"])
            if notional < c["tier1"]:
                return
            side = "buy" if is_buy else "sell"
            st["whales"].append({"ts_ms": ts_ms, "price": price,
                                 "usd": round(notional, 2), "is_buy": is_buy})
            # ① 单笔巨单（≥ tier2）：即时事件，不节流（每笔都值得记）
            if notional >= c["tier2"]:
                _push_event(st, "single_super", side,
                            f"单笔{'买入' if is_buy else '卖出'}巨单 {_fmt_usd(notional)} @ {price}",
                            notional, price, ts_ms, cooldown=False)
            # ② 同向大单连续计数（被反向大单打断重置；达阈值只发一次直到被打断）
            if st["consec_side"] == side:
                st["consec_n"] += 1
            else:
                st["consec_side"] = side
                st["consec_n"] = 1
                st["consec_fired"] = False
            if st["consec_n"] >= CONSEC_N and not st["consec_fired"]:
                if _push_event(
                        st, "consecutive", side,
                        (f"窗口内连续 {st['consec_n']} 笔同向大单"
                         f"（{'持续买入，吸筹嫌疑' if is_buy else '持续卖出，派发嫌疑'}）"),
                        notional, price, ts_ms):
                    st["consec_fired"] = True
    except Exception:  # noqa: BLE001 — WS 回调铁律：绝不向数据流抛出
        pass


def summary(symbol: str, cfg: dict | None = None,
            now_ms: int | None = None) -> dict:
    """单币种窗口统计（REST 消费入口）。divergence 检测在此完成并顺带入事件列。

    now_ms 仅供测试注入模拟时间；生产不传用当前时间。
    """
    c = _cfg_vals(cfg)
    sym = (symbol or "").upper()
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    with _LOCK:
        st = _STATE.get(sym)
        if st is None:
            return {"symbol": sym, "active": False,
                    **build_summary([], [], [], tier1=c["tier1"],
                                    tier2=c["tier2"], window_min=c["window_min"],
                                    now_ms=now)}
        out = build_summary(list(st["buckets"]), list(st["whales"]),
                            list(st["events"]), tier1=c["tier1"],
                            tier2=c["tier2"], window_min=c["window_min"],
                            now_ms=now)
        # divergence 是窗口级结论：检出时落事件列（同币 10 分钟节流）
        div = out.get("divergence")
        if div:
            last_p = out["recent_whales"][0]["price"] if out["recent_whales"] else 0.0
            if _push_event(st, "divergence",
                           "sell" if div["side"] == "sell_into_buys" else "buy",
                           div["note"], abs(out["net_usd"]), last_p, now):
                out["events"] = list(st["events"])[-10:][::-1]
        return {"symbol": sym, "active": True, **out}


def summary_all(symbols: list[str] | None = None, cfg: dict | None = None) -> dict:
    """多币种汇总（/api/whale/summary 缺省口径 = 配置 watchlist）。"""
    if symbols is None:
        try:
            import jarvis_config as jc
            symbols = [str(s).upper() for s in (jc.get("watchlist") or ["BTCUSDT"])]
        except Exception:  # noqa: BLE001
            symbols = ["BTCUSDT"]
    return {sym: summary(sym, cfg) for sym in symbols}


def register() -> bool:
    """把 ingest 挂到 WS aggTrade 流（幂等）。WS 模块缺失/未启动返回 False。"""
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


# ─────────────────────────── 安全带可选因子 ───────────────────────────


def whale_check(direction: str, whale_summary: dict | None) -> dict | None:
    """开仓方向 × 窗口大单净流：反向时提醒（纯函数，不改置信度、不阻断）。

    Args:
        direction: 共识/开仓方向 bullish | bearish | neutral
        whale_summary: summary() 输出；None/无数据返回 None（不参与判定）
    Returns:
        {status: "against"|"aligned"|"idle", note, net_usd, window_min} | None
    """
    if not whale_summary or not whale_summary.get("active"):
        return None
    if direction not in ("bullish", "bearish"):
        return None
    net = float(whale_summary.get("net_usd") or 0.0)
    tier1 = float(whale_summary.get("tier1_usd") or 100000.0)
    if abs(net) < tier1:  # 净流不足一笔大单量级：无信号价值
        return {"status": "idle", "net_usd": net,
                "window_min": whale_summary.get("window_min"),
                "note": "窗口大单净流接近均衡，不构成方向证据"}
    flow_dir = "bullish" if net > 0 else "bearish"
    win = whale_summary.get("window_min")
    if flow_dir == direction:
        return {"status": "aligned", "net_usd": net, "window_min": win,
                "note": (f"{win}min 大单净{'买入' if net > 0 else '卖出'} "
                         f"{_fmt_usd(abs(net))}，与开仓方向同向")}
    return {"status": "against", "net_usd": net, "window_min": win,
            "note": (f"⚠️ {win}min 大单净{'买入' if net > 0 else '卖出'} "
                     f"{_fmt_usd(abs(net))}，与开仓方向相反——主力资金在对面，"
                     "谨慎追单（提醒不阻断）")}


# ─────────────────────────── CLI ───────────────────────────


def main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="贾维斯大单流监控 whale tape")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--seconds", type=int, default=30, help="实测收流秒数")
    args = ap.parse_args()

    import jarvis_ws_stream as jws
    register()
    jws.start([args.symbol.upper()])
    print(f"收流 {args.seconds}s 后输出 {args.symbol} 窗口统计…")
    time.sleep(max(5, args.seconds))
    print(json.dumps(summary(args.symbol), ensure_ascii=False, indent=2))
    jws.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
