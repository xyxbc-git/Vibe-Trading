#!/usr/bin/env python3
"""贾维斯 JARVIS — 盘口深度透视（驾驶舱需求 2a：新页面「左卖右买」DOM 阶梯）。

REST 快照口径（比维护 depth 增量流的本地订单簿简单可靠得多）：
  合约 GET /fapi/v1/depth  → 失败回退现货 GET /api/v3/depth（与 WS 回退策略一致）
  快照默认 500 档，聚合成价格桶后返回前端渲染 DOM 阶梯。

聚合规则：
  bucket 宽度自适应现价量级（mid × 0.02% 归整到 1/2/5×10^k 的「好看步长」），
  也可由调用方显式指定。买盘向下取整、卖盘向上取整，桶内累加数量与名义额，
  同时输出累计深度（cum_usd）供前端画深度曲线/阶梯。

纯函数核心：nice_step / aggregate_book 离线可测（_depth_view_smoketest.py）。
"""

from __future__ import annotations

import math
import time
from typing import Any

import requests

import jarvis_net as _jnet

FAPI_DEPTH = "https://fapi.binance.com/fapi/v1/depth"
SPOT_DEPTH = "https://api.binance.com/api/v3/depth"
TIMEOUT = 10
_HEADERS = {"User-Agent": "jarvis-depth-view/1.0"}

# 快照失败时可回退的上一次成功结果（进程内，每币一份）
_LAST_GOOD: dict[str, dict] = {}


def nice_step(raw: float) -> float:
    """把任意正数归整到 1/2/5×10^k 的「好看步长」（0.0007→0.0005、37→50）。"""
    if not math.isfinite(raw) or raw <= 0:
        return 1.0
    exp = math.floor(math.log10(raw))
    frac = raw / (10 ** exp)
    if frac < 1.5:
        nice = 1.0
    elif frac < 3.5:
        nice = 2.0
    elif frac < 7.5:
        nice = 5.0
    else:
        nice = 10.0
    return nice * (10 ** exp)


def _bucket_side(levels: list, step: float, is_bid: bool,
                 max_buckets: int) -> list[dict]:
    """单侧订单簿 → 价格桶列表（买盘价降序 / 卖盘价升序），带累计额。"""
    agg: dict[float, dict] = {}
    for lv in levels or []:
        try:
            p, q = float(lv[0]), float(lv[1])
        except (TypeError, ValueError, IndexError):
            continue
        if p <= 0 or q <= 0:
            continue
        # 买盘向下取整、卖盘向上取整，保证桶边界不跨越 mid
        b = (math.floor(p / step) if is_bid else math.ceil(p / step)) * step
        b = round(b, 10)
        slot = agg.setdefault(b, {"price": b, "qty": 0.0, "usd": 0.0})
        slot["qty"] += q
        slot["usd"] += p * q
    rows = sorted(agg.values(), key=lambda r: r["price"], reverse=is_bid)
    rows = rows[:max_buckets]
    cum = 0.0
    for r in rows:
        cum += r["usd"]
        r["qty"] = round(r["qty"], 6)
        r["usd"] = round(r["usd"], 2)
        r["cum_usd"] = round(cum, 2)
    return rows


def _first_valid_price(levels: list) -> float | None:
    for lv in levels or []:
        try:
            p, q = float(lv[0]), float(lv[1])
            if p > 0 and q > 0:
                return p
        except (TypeError, ValueError, IndexError):
            continue
    return None


def aggregate_book(bids: list, asks: list, *, bucket: float | None = None,
                   max_buckets: int = 30) -> dict:
    """原始订单簿 → DOM 阶梯载荷（纯函数，smoketest 直测入口）。"""
    best_bid = _first_valid_price(bids)
    best_ask = _first_valid_price(asks)
    mid = ((best_bid + best_ask) / 2.0
           if best_bid and best_ask else best_bid or best_ask or 0.0)
    step = float(bucket) if bucket and bucket > 0 else nice_step(mid * 0.0002)
    b_rows = _bucket_side(bids, step, True, max_buckets)
    a_rows = _bucket_side(asks, step, False, max_buckets)

    # 前 10 桶买卖失衡：>1 买方挂单厚（下方接盘强），<1 卖方压单厚
    bid10 = sum(r["usd"] for r in b_rows[:10])
    ask10 = sum(r["usd"] for r in a_rows[:10])
    ratio = round(bid10 / ask10, 3) if ask10 > 0 else None

    return {
        "mid": round(mid, 8),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": (round((best_ask - best_bid) / mid * 100, 5)
                       if best_bid and best_ask and mid else None),
        "bucket": step,
        "bids": b_rows,
        "asks": a_rows,
        "imbalance": {"bid_usd_10": round(bid10, 2), "ask_usd_10": round(ask10, 2),
                      "ratio": ratio},
    }


def _fetch(url: str, symbol: str, limit: int) -> Any:
    r = requests.get(url, params={"symbol": symbol, "limit": limit},
                     headers=_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def orderbook(symbol: str, limit: int = 500, bucket: float | None = None,
              max_buckets: int = 30) -> dict:
    """盘口深度快照 + 桶聚合（REST 合约优先、现货回退、旧快照兜底）。"""
    sym = (symbol or "BTCUSDT").upper()
    lim = max(50, min(int(limit), 1000))
    _jnet.ensure_proxy()
    raw, market, err = None, None, None
    for url, mk in ((FAPI_DEPTH, "futures"), (SPOT_DEPTH, "spot")):
        try:
            raw = _fetch(url, sym, lim)
            market = mk
            break
        except Exception as exc:  # noqa: BLE001 — 逐级回退
            err = repr(exc)[:200]
    if not isinstance(raw, dict) or not raw.get("bids"):
        last = _LAST_GOOD.get(sym)
        if last:
            return {**last, "stale": True, "error": err}
        return {"ok": False, "symbol": sym, "error": err or "订单簿为空"}

    out = {
        "ok": True,
        "symbol": sym,
        "market": market,
        "ts": time.time(),
        **aggregate_book(raw.get("bids") or [], raw.get("asks") or [],
                         bucket=bucket, max_buckets=max_buckets),
    }
    _LAST_GOOD[sym] = out
    return out


if __name__ == "__main__":
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    print(json.dumps(orderbook(sym), ensure_ascii=False, indent=2))
