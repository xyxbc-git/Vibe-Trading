#!/usr/bin/env python3
"""贾维斯 JARVIS — 市场情报数据源（免 Key 公开接口，情报页真实数据）。

覆盖四个免费源：
  - 资金费率   Binance GET /fapi/v1/premiumIndex（单次返回全部 symbol，取目标币种）
  - 持仓量 OI  Binance GET /futures/data/openInterestHist（最新名义价值 + 24h 变化%）
  - 多空比     Binance GET /futures/data/globalLongShortAccountRatio（全体账户口径）
  - 恐慌贪婪   alternative.me GET /fng/

爆仓数据（Coinglass）与链上指标（Glassnode）需第三方 API key，本模块不接入，
在返回体 unavailable 中说明原因，由前端展示「未接入」占位态（禁止演示假数据）。

设计要点：
  - 每个源独立 TTL 内存缓存；单源失败保留上次成功值（错误记入 errors），不拖垮整页
  - 外部请求超时 5s、显式 UA、经 jarvis_net 自动代理探测；四源并行拉取
  - get_intel()：缓存新鲜直接返回；过期时后台线程刷新、本次立即返回旧值（不阻塞请求）；
    冷启动（无任何缓存）才同步拉一次
"""

from __future__ import annotations

import threading
import time

import requests

import jarvis_net

FAPI = "https://fapi.binance.com"
FNG_API = "https://api.alternative.me/fng/"
TIMEOUT = 5
_HEADERS = {"User-Agent": "jarvis-market-intel/1.0"}

# 资金费率展示币种；OI / 多空比与页面主语境一致用 BTC
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
OI_SYMBOL = "BTCUSDT"
LS_SYMBOL = "BTCUSDT"

# 各源缓存 TTL（秒）：行情类分钟级，恐贪指数官方日更、小时级足够
TTL = {"funding": 120, "oi": 300, "long_short": 300, "fng": 3600}

_LOCK = threading.Lock()
_REFRESHING = False
# {part: {"ts": 上次成功时间, "data": dict|None, "error": str|None}}
_STORE: dict[str, dict] = {}


def _get_json(url: str, params: dict | None = None):
    jarvis_net.ensure_proxy()
    r = requests.get(url, params=params, headers=_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _fetch_funding() -> dict:
    rows = _get_json(f"{FAPI}/fapi/v1/premiumIndex")
    out: dict[str, float] = {}
    if isinstance(rows, list):
        want = set(SYMBOLS)
        for row in rows:
            sym = row.get("symbol")
            if sym in want:
                out[sym] = round(float(row.get("lastFundingRate") or 0), 8)
    if len(out) < len(SYMBOLS):
        missing = set(SYMBOLS) - set(out)
        if not out:
            raise ValueError(f"premiumIndex 未返回目标币种 {missing}")
    # 按 SYMBOLS 顺序输出，前端展示稳定
    return {"rates": {s: out[s] for s in SYMBOLS if s in out}}


def _fetch_oi() -> dict:
    # period=1h limit=25：最后一条为最新，首条约 24h 前 → 算 24h 变化
    rows = _get_json(f"{FAPI}/futures/data/openInterestHist",
                     {"symbol": OI_SYMBOL, "period": "1h", "limit": 25})
    if not isinstance(rows, list) or not rows:
        raise ValueError("openInterestHist 空返回")
    latest = rows[-1]
    value = float(latest["sumOpenInterestValue"])
    change_pct = None
    if len(rows) >= 2:
        prev = float(rows[0]["sumOpenInterestValue"])
        if prev > 0:
            change_pct = round((value - prev) / prev * 100, 2)
    return {"symbol": OI_SYMBOL, "value": round(value, 0), "change_pct": change_pct,
            "bar_ts": int(latest.get("timestamp") or 0)}


def _fetch_long_short() -> dict:
    rows = _get_json(f"{FAPI}/futures/data/globalLongShortAccountRatio",
                     {"symbol": LS_SYMBOL, "period": "1h", "limit": 1})
    if not isinstance(rows, list) or not rows:
        raise ValueError("globalLongShortAccountRatio 空返回")
    row = rows[-1]
    return {"symbol": LS_SYMBOL,
            "long_pct": round(float(row["longAccount"]) * 100, 1),
            "short_pct": round(float(row["shortAccount"]) * 100, 1),
            "ratio": round(float(row["longShortRatio"]), 2),
            "bar_ts": int(row.get("timestamp") or 0)}


def _fetch_fng() -> dict:
    data = _get_json(FNG_API, {"limit": 1})
    rows = (data or {}).get("data") or []
    if not rows:
        raise ValueError("fng 空返回")
    row = rows[0]
    return {"value": int(row["value"]),
            "classification": str(row.get("value_classification") or ""),
            "index_ts": int(row.get("timestamp") or 0)}


_FETCHERS = {"funding": _fetch_funding, "oi": _fetch_oi,
             "long_short": _fetch_long_short, "fng": _fetch_fng}


def _refresh(parts: list[str]) -> None:
    """并行拉取指定 parts；成功写缓存，失败保留旧值并记录错误。"""
    def _one(name: str) -> None:
        try:
            data = _FETCHERS[name]()
            with _LOCK:
                _STORE[name] = {"ts": time.time(), "data": data, "error": None}
        except Exception as exc:  # noqa: BLE001 — 单源失败不拖垮整页
            with _LOCK:
                old = _STORE.get(name) or {"ts": 0.0, "data": None}
                _STORE[name] = {**old, "error": repr(exc)[:160]}

    threads = [threading.Thread(target=_one, args=(n,), daemon=True) for n in parts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=TIMEOUT + 2)


def _stale_parts(now: float) -> list[str]:
    with _LOCK:
        return [name for name in _FETCHERS
                if (ent := _STORE.get(name)) is None
                or ent["data"] is None
                or now - ent["ts"] >= TTL[name]]


def _background_refresh(parts: list[str]) -> None:
    global _REFRESHING
    try:
        _refresh(parts)
    finally:
        with _LOCK:
            _REFRESHING = False


def get_intel() -> dict:
    """情报页聚合数据。恒快速返回：过期部分交后台刷新，本次返回现有缓存。"""
    global _REFRESHING
    now = time.time()
    stale = _stale_parts(now)
    if stale:
        with _LOCK:
            have_any = any((_STORE.get(n) or {}).get("data") for n in _FETCHERS)
        if not have_any:
            _refresh(stale)  # 冷启动：同步拉一次（四源并行，≤ 超时上限）
        else:
            spawn = False
            with _LOCK:
                if not _REFRESHING:
                    _REFRESHING = True
                    spawn = True
            if spawn:
                threading.Thread(target=_background_refresh, args=(stale,),
                                 daemon=True).start()

    with _LOCK:
        snap = {name: dict(_STORE.get(name) or {"ts": 0.0, "data": None, "error": None})
                for name in _FETCHERS}

    def _ts(name: str) -> int | None:
        t = snap[name]["ts"]
        return int(t) if t else None

    funding = snap["funding"]["data"]
    oi = snap["oi"]["data"]
    ls = snap["long_short"]["data"]
    fng = snap["fng"]["data"]

    parts_ts = [t for t in (_ts(n) for n in _FETCHERS) if t]
    errors = {name: snap[name]["error"] for name in _FETCHERS if snap[name]["error"]}

    return {
        "ok": any(x is not None for x in (funding, oi, ls, fng)),
        "updated_at": max(parts_ts) if parts_ts else None,
        "fng": ({**fng, "ts": _ts("fng")} if fng else None),
        "funding_rate": (funding or {}).get("rates") or None,
        "funding_ts": _ts("funding") if funding else None,
        "oi": ({**oi, "ts": _ts("oi")} if oi else None),
        "long_short": ({**ls, "ts": _ts("long_short")} if ls else None),
        # 未接入源：明确原因，前端据此渲染「未接入」占位态，禁止演示假数据
        "liquidations": None,
        "onchain": None,
        "unavailable": {
            "liquidations": "需 Coinglass API key，暂未接入",
            "onchain": "需 Glassnode API key，暂未接入",
        },
        "errors": errors or None,
    }


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(get_intel(), ensure_ascii=False, indent=2))
