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
import re
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


_BAN_UNTIL_RE = re.compile(r"banned until (\d{10,16})", re.I)


def _ban_hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def orderbook(symbol: str, limit: int = 500, bucket: float | None = None,
              max_buckets: int = 30) -> dict:
    """盘口深度快照 + 桶聚合（REST 合约优先、现货回退、旧快照兜底）。

    IP 封禁感知：合约域处于登记封禁期时跳过不撞（jarvis_net 多进程共享），
    并把「封禁至几点 / 现货无此交易对」等人话原因带给前端。
    """
    sym = (symbol or "BTCUSDT").upper()
    lim = max(50, min(int(limit), 1000))
    _jnet.ensure_proxy()
    raw, market = None, None
    errs: list[str] = []
    plans: list[tuple[str, str]] = []
    fapi_ban = _jnet.banned_until(FAPI_DEPTH)
    if fapi_ban:
        errs.append(f"合约行情接口 IP 限频封禁至 {_ban_hhmm(fapi_ban)}，暂不可用")
    else:
        plans.append((FAPI_DEPTH, "futures"))
    plans.append((SPOT_DEPTH, "spot"))
    # 深度权重随 limit 走（币安：≤50→2 / ≤100→5 / ≤500→10 / ≤1000→20）
    _cost = 2.0 if lim <= 50 else 5.0 if lim <= 100 else 10.0 if lim <= 500 else 20.0
    for url, mk in plans:
        try:
            _budget = getattr(_jnet, "budget_take", None)
            if callable(_budget):
                try:
                    _ok = _budget(url, 180, cost=_cost)
                except TypeError:  # 旧版无 cost 参数
                    _ok = _budget(url, 180)
                if not _ok:
                    errs.append(f"{mk} 分钟预算耗尽（防限频闸拦截）")
                    continue
            raw = _fetch(url, sym, lim)
            market = mk
            break
        except requests.HTTPError as exc:  # noqa: PERF203 — 逐级回退
            resp = exc.response
            code = resp.status_code if resp is not None else 0
            try:
                body_msg = str((resp.json() or {}).get("msg", "")) if resp is not None else ""
            except Exception:  # noqa: BLE001
                body_msg = ""
            m = _BAN_UNTIL_RE.search(body_msg)
            if mk == "futures" and m:
                ts = float(m.group(1))
                ts = ts / 1000.0 if ts > 1e12 else ts
                _jnet.report_ban(url, ts)
                errs.append(f"合约行情接口 IP 限频封禁至 {_ban_hhmm(ts)}，暂不可用")
            elif code in (418, 429):
                # [2026-08-09 加固] 429 / 无封禁文案的 418 也登记跨进程冷却，
                # 禁止各进程继续撞墙升级成 IP 封禁
                retry_after = 90.0
                try:
                    retry_after = max(retry_after, float(
                        resp.headers.get("Retry-After") or 0)) if resp is not None else retry_after
                except (TypeError, ValueError):
                    pass
                getattr(_jnet, "report_cooldown", lambda *_: None)(url, retry_after)
                errs.append(f"{mk} HTTP {code} 限流，已登记 {retry_after:.0f}s 全局冷却")
            elif mk == "spot" and code == 400:
                errs.append(f"现货市场无 {sym} 交易对（该品种仅合约有）")
            else:
                errs.append(f"{mk} HTTP {code}" + (f"：{body_msg[:80]}" if body_msg else ""))
        except Exception as exc:  # noqa: BLE001 — 连接/超时等逐级回退
            errs.append(f"{mk} {repr(exc)[:120]}")
    err = "；".join(errs) if errs else None
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


def futures_snapshot_book(symbol: str, *, bucket: float | None = None,
                          max_buckets: int = 30, limit: int = 500,
                          ttl: float = 2.5, max_age_s: float = 15.0
                          ) -> dict | None:
    """合约 REST 深度快照 → DOM 载荷（深度阶梯方案 A：现货 WS 簿口径纠偏）。

    背景：代理丢 fstream 数据帧时 WS 增量簿回退现货域，而行情主链路（顶栏价/
    K 线）是合约口径——本地簿 mid 与顶栏存在基差。本函数拉合约域快照，供
    /api/orderbook/live 在「WS 簿=spot 且合约域可用」时替换返回。

    取数纪律（2026-08-09 防封禁加固语义，绝不裸 requests）：
      - 走 jarvis_crypto_data._get 三道闸：ttl 秒内 TTL 直出（即本函数的限频闸）、
        封禁短路、跨进程分钟权重预算；响应头权重水位自动记账；
      - 出网前先查 jarvis_net 共享封禁登记，封禁期直接返回 None 零出网；
      - _get 失败回退磁盘缓存可能给出陈旧快照（对实时 DOM 是错误语义）——
        按响应 E 字段（事件毫秒时戳）做新鲜度门禁，龄 > max_age_s 视同失败。

    返回载荷形状与 orderbook() 一致（market 恒 "futures"）；封禁/失败/陈旧
    返回 None，调用方回退现货 WS 簿（封禁期内回退属预期行为）。
    """
    sym = (symbol or "BTCUSDT").upper()
    try:
        if _jnet.banned_until(FAPI_DEPTH):
            return None
        import jarvis_crypto_data as jcd
        raw = jcd._get(FAPI_DEPTH, {"symbol": sym, "limit": int(limit)},
                       fast=True, ttl=float(ttl))
    except Exception:  # noqa: BLE001 — 取数层异常一律回退调用方
        return None
    if (not isinstance(raw, dict) or "_error" in raw
            or not raw.get("bids") or not raw.get("asks")):
        return None
    try:
        ev_ms = float(raw.get("E") or raw.get("T") or 0)
    except (TypeError, ValueError):
        ev_ms = 0.0
    if ev_ms and time.time() - ev_ms / 1000.0 > max_age_s:
        return None
    return {
        "ok": True,
        "symbol": sym,
        "market": "futures",
        "ts": time.time(),
        **aggregate_book(raw.get("bids") or [], raw.get("asks") or [],
                         bucket=bucket, max_buckets=max_buckets),
    }


if __name__ == "__main__":
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    print(json.dumps(orderbook(sym), ensure_ascii=False, indent=2))
