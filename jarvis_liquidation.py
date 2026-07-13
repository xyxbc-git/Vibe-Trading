#!/usr/bin/env python3
"""贾维斯 JARVIS — 爆仓流实时面板引擎（M2 s5-liq-panel）。

消费 jarvis_ws_stream 的 forceOrder 流（实时回调 + SQLite 历史库），产出：
  - 滚动窗口多/空爆仓金额与笔数（signal.liq_window_min，默认 60 分钟）
  - 爆仓时间序列（按分钟分桶，前端画对比条/迷你图）
  - 大额爆仓事件列表（单笔名义 ≥ signal.liq_large_usd，默认 5 万 U）
  - 爆仓簇检测：liq_cluster_window_s 秒滑窗内同向爆仓 ≥ liq_cluster_min_count 笔
    视为一簇——短时同向密集强平常意味着行情加速 / 接近磁吸位（连环爆仓链）

爆仓方向语义（币安 forceOrder 的 side 是被强平订单的买卖方向）：
  side=SELL → 多头被强平（强制卖出平多）→ long 爆仓
  side=BUY  → 空头被强平（强制买入平空）→ short 爆仓

降级现状（见 jarvis_ws_stream 端点回退策略）：本地代理丢弃合约域
fstream.binance.com 数据帧时自动回退现货流，而 forceOrder 无现货对应流
→ health().degraded_streams 含 "forceOrder"。本模块 summary() 恒带
degraded / guidance 字段：降级期回退展示历史库已有数据，并引导用户把
fstream.binance.com 加入代理放行名单后自动恢复。

设计原则（与 jarvis_sentiment 同风格）：
  - 纯函数核心（normalize_event / window_stats / find_large / detect_clusters /
    build_summary）只吃 list[dict]，不联网不读库，smoketest 可 mock 锁定口径
  - summary() 门面负责取数（历史库 + 实时缓冲合并去重）再走纯函数
  - 任何异常降级为 ok=False / 空数据，绝不拖垮 dashboard 主进程
"""

from __future__ import annotations

import time

# 兜底默认（生效值走 jarvis_config：signal.liq_*；此处仅配置读取失败时使用）
WINDOW_MIN_DEFAULT = 60
LARGE_USD_DEFAULT = 50_000.0
CLUSTER_WINDOW_S_DEFAULT = 180
CLUSTER_MIN_COUNT_DEFAULT = 5

# summary 返回的大额事件/簇事件条数上限（防面板膨胀）
MAX_LARGE_EVENTS = 20
MAX_CLUSTERS = 10


def _cfg_get(key: str, fallback):
    try:
        import jarvis_config as jc
        v = jc.get(key)
        return fallback if v is None else v
    except Exception:  # noqa: BLE001 — 配置层异常用兜底默认
        return fallback


# ═══════════════════════════ 纯函数核心 ═══════════════════════════

def normalize_event(item: dict) -> dict | None:
    """把 forceOrder 实时消息 / SQLite 历史行归一成统一事件结构。

    实时消息（币安组合流 data）：{"o": {"s","S","ap"/"p","q","T",...}}
    历史行（force_orders 表）：{"symbol","side","price","qty","avg_price",
                               "trade_time","notional",...}
    返回 {symbol, side, side_liquidated, price, qty, notional, trade_time}
    （trade_time 毫秒）；无法识别/无效数据返回 None。
    """
    try:
        if not isinstance(item, dict):
            return None
        if isinstance(item.get("o"), dict):        # 实时消息形态
            o = item["o"]
            symbol = str(o.get("s") or "").upper()
            side = str(o.get("S") or "").upper()
            price = float(o.get("ap") or o.get("p") or 0)
            qty = float(o.get("q") or 0)
            trade_time = int(o.get("T") or 0)
            notional = round(price * qty, 4)
        else:                                       # 历史库行形态
            symbol = str(item.get("symbol") or "").upper()
            side = str(item.get("side") or "").upper()
            price = float(item.get("avg_price") or item.get("price") or 0)
            qty = float(item.get("qty") or 0)
            trade_time = int(item.get("trade_time") or 0)
            n = item.get("notional")
            notional = round(float(n), 4) if n not in (None, 0) else round(price * qty, 4)
        if not symbol or side not in ("BUY", "SELL") or trade_time <= 0 or notional <= 0:
            return None
        return {"symbol": symbol, "side": side,
                # SELL=多头被强平 / BUY=空头被强平
                "side_liquidated": "long" if side == "SELL" else "short",
                "price": price, "qty": qty, "notional": notional,
                "trade_time": trade_time}
    except Exception:  # noqa: BLE001 — 脏数据跳过，不拖垮统计
        return None


def dedupe_events(events: list[dict]) -> list[dict]:
    """按 (symbol, trade_time, notional) 去重（实时缓冲与历史库来源重叠），
    并按 trade_time 升序返回。"""
    seen: set = set()
    out: list[dict] = []
    for e in events:
        key = (e["symbol"], e["trade_time"], round(e["notional"], 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda x: x["trade_time"])
    return out


def window_stats(events: list[dict], window_min: int, now_ms: int) -> dict:
    """滚动窗口统计：多/空爆仓金额、笔数、按分钟分桶的时间序列。

    dominance ∈ [-1, 1]：+1=全是多头爆仓（下跌加速信号），-1=全是空头爆仓。
    """
    cutoff = now_ms - int(window_min) * 60_000
    inwin = [e for e in events if e["trade_time"] >= cutoff]
    long_usd = sum(e["notional"] for e in inwin if e["side_liquidated"] == "long")
    short_usd = sum(e["notional"] for e in inwin if e["side_liquidated"] == "short")
    long_count = sum(1 for e in inwin if e["side_liquidated"] == "long")
    short_count = len(inwin) - long_count
    total = long_usd + short_usd
    buckets: dict[int, dict] = {}
    for e in inwin:
        ts_min = e["trade_time"] // 60_000 * 60      # 桶起点（秒）
        b = buckets.setdefault(ts_min, {"ts": ts_min, "long_usd": 0.0, "short_usd": 0.0})
        b[f"{e['side_liquidated']}_usd"] = round(
            b[f"{e['side_liquidated']}_usd"] + e["notional"], 4)
    return {"long_usd": round(long_usd, 2), "short_usd": round(short_usd, 2),
            "long_count": long_count, "short_count": short_count,
            "total_usd": round(total, 2),
            "dominance": round((long_usd - short_usd) / total, 3) if total > 0 else 0.0,
            "series": sorted(buckets.values(), key=lambda b: b["ts"])}


def find_large(events: list[dict], threshold_usd: float,
               limit: int = MAX_LARGE_EVENTS) -> list[dict]:
    """大额爆仓事件（单笔名义 ≥ 阈值），按时间倒序取最近 limit 条。"""
    hits = [e for e in events if e["notional"] >= float(threshold_usd)]
    hits.sort(key=lambda x: x["trade_time"], reverse=True)
    return hits[:max(1, int(limit))]


def detect_clusters(events: list[dict], window_s: int, min_count: int,
                    limit: int = MAX_CLUSTERS) -> list[dict]:
    """爆仓簇：window_s 秒滑窗内同向爆仓 ≥ min_count 笔 → 合并为一簇。

    实现：按方向分组时间升序扫描，相邻事件间隔 ≤ window_s 归入同簇（链式扩展，
    等价于把重叠滑窗合并），成簇后按笔数门槛过滤。簇=行情加速/磁吸位信号。
    """
    out: list[dict] = []
    win_ms = int(window_s) * 1000
    for side in ("long", "short"):
        seq = sorted((e for e in events if e["side_liquidated"] == side),
                     key=lambda x: x["trade_time"])
        cluster: list[dict] = []
        for e in seq:
            if cluster and e["trade_time"] - cluster[-1]["trade_time"] > win_ms:
                if len(cluster) >= int(min_count):
                    out.append(_cluster_summary(side, cluster))
                cluster = []
            cluster.append(e)
        if len(cluster) >= int(min_count):
            out.append(_cluster_summary(side, cluster))
    out.sort(key=lambda c: c["end_ts"], reverse=True)
    return out[:max(1, int(limit))]


def _cluster_summary(side: str, cluster: list[dict]) -> dict:
    total = round(sum(e["notional"] for e in cluster), 2)
    side_cn = "多头" if side == "long" else "空头"
    accel_cn = "下跌" if side == "long" else "上涨"
    return {"side_liquidated": side, "count": len(cluster), "total_usd": total,
            "start_ts": cluster[0]["trade_time"] // 1000,
            "end_ts": cluster[-1]["trade_time"] // 1000,
            "symbols": sorted({e["symbol"] for e in cluster}),
            "note": (f"{len(cluster)} 笔{side_cn}爆仓密集出现（共 ${total:,.0f}）——"
                     f"{accel_cn}行情加速中，警惕连环强平/磁吸位效应")}


def build_summary(events: list[dict], *, window_min: int, large_usd: float,
                  cluster_window_s: int, cluster_min_count: int,
                  now_ms: int | None = None) -> dict:
    """事件列表 → 完整面板数据（纯函数，smoketest 入口）。"""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    cutoff = now - int(window_min) * 60_000
    inwin = [e for e in events if e["trade_time"] >= cutoff]
    return {"window_min": int(window_min),
            "as_of": now // 1000,
            "stats": window_stats(events, window_min, now),
            "large": find_large(inwin, large_usd),
            "clusters": detect_clusters(inwin, cluster_window_s, cluster_min_count),
            "thresholds": {"large_usd": float(large_usd),
                           "cluster_window_s": int(cluster_window_s),
                           "cluster_min_count": int(cluster_min_count)}}


# ═══════════════════════════ 门面（带 IO） ═══════════════════════════

# forceOrder 降级期给用户的引导文案（任务口径：加代理放行名单后自动恢复四流）
DEGRADED_GUIDANCE = (
    "实时爆仓流（forceOrder）处于降级态：本地代理正在丢弃币安合约域数据帧。"
    "请在代理工具的规则/放行名单中添加 fstream.binance.com 后，"
    "系统会自动恢复实时接收；降级期间下方展示的是历史库已有数据。"
)


def _stream_degraded() -> bool:
    """forceOrder 实时流是否降级/不可用（WS 未启用、未运行、或已降级到现货流）。"""
    try:
        import jarvis_ws_stream as jws
        h = jws.health()
        if not h.get("running") or not h.get("connected"):
            return True
        return "forceOrder" in (h.get("degraded_streams") or [])
    except Exception:  # noqa: BLE001
        return True


def summary(symbol: str | None = None) -> dict:
    """面板数据门面：历史库 + 实时缓冲合并去重 → 纯函数统计。永不抛出。"""
    try:
        window_min = int(_cfg_get("liq_window_min", WINDOW_MIN_DEFAULT))
        large_usd = float(_cfg_get("liq_large_usd", LARGE_USD_DEFAULT))
        cluster_win = int(_cfg_get("liq_cluster_window_s", CLUSTER_WINDOW_S_DEFAULT))
        cluster_min = int(_cfg_get("liq_cluster_min_count", CLUSTER_MIN_COUNT_DEFAULT))

        raw: list[dict] = []
        try:
            import jarvis_ws_stream as jws
            # 历史库：多拉一些防窗口边缘截断（窗口过滤在纯函数层做）
            raw.extend(jws.force_orders_recent(symbol, limit=5000))
            # 实时缓冲：ws_force_order_persist 关闭时的兜底源（开启时与库重叠→去重）
            syms = [symbol.upper()] if symbol else (jws.health().get("symbols") or [])
            for s in syms:
                raw.extend(jws.latest("forceOrder", s))
        except Exception:  # noqa: BLE001 — ws 模块不可用时输出空数据 + 降级引导
            pass

        events = dedupe_events([e for e in (normalize_event(x) for x in raw) if e])
        out = build_summary(events, window_min=window_min, large_usd=large_usd,
                            cluster_window_s=cluster_win,
                            cluster_min_count=cluster_min)
        degraded = _stream_degraded()
        out.update({"ok": True, "symbol": symbol.upper() if symbol else None,
                    "degraded": degraded,
                    "guidance": DEGRADED_GUIDANCE if degraded else None,
                    "history_rows": len(events)})
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:200],
                "degraded": True, "guidance": DEGRADED_GUIDANCE}


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(summary(), ensure_ascii=False, indent=2))
