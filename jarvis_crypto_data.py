#!/usr/bin/env python3
"""贾维斯 JARVIS - 真实加密衍生品数据拉取工具。

数据源（全部免费、免 Key）：
  - Binance Futures 公开 API：资金费率、未平仓量(OI)、多空账户比、主动买卖量
  - Binance Spot 公开 API：现货价（用于计算 期现基差 spot-perp basis）
  - alternative.me：恐慌贪婪指数(Fear & Greed)
  - CoinGecko 公开 API：BTC/ETH 市占率、全市场总市值、24h 成交量
  - blockchain.info + mempool.space：BTC 链上基本面（算力、难度、内存池、手续费）

用途：替代 LLM 幻觉，给贾维斯提供真实的加密专属另类数据(edge)。

用法：
  python jarvis_crypto_data.py BTCUSDT
  python jarvis_crypto_data.py ETHUSDT --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Optional

import requests

import jarvis_net

FAPI = "https://fapi.binance.com"
SPOT_API = "https://api.binance.com"
FNG_API = "https://api.alternative.me/fng/"
CG_API = "https://api.coingecko.com/api/v3"
BLOCKCHAIN_Q = "https://blockchain.info/q"
MEMPOOL_API = "https://mempool.space/api"
OKX_API = "https://www.okx.com"  # T-06 备用源：Binance 主源失败时切 OKX（免 Key 公开行情）
# T-13 扩展数据源（需 Key；无 Key 时优雅降级为 _skipped，绝不报错）。
COINGLASS_API = "https://open-api-v4.coinglass.com"   # 爆仓热力图 / 清算数据
GLASSNODE_API = "https://api.glassnode.com"           # 链上：交易所净流 / 大额转账
TIMEOUT = 15
_HEADERS = {"User-Agent": "jarvis-crypto-data/1.0"}

# T-13 凭据不硬编码：优先 env，其次 ~/.vibe-trading/data_keys.json。
DATA_KEYS_PATH = os.path.expanduser("~/.vibe-trading/data_keys.json")


def _data_key(env: str, file_key: str) -> str:
    """取扩展数据源 API Key：env > data_keys.json > 空串（空串=未配置→降级）。"""
    v = os.getenv(env)
    if v:
        return v.strip()
    try:
        if os.path.exists(DATA_KEYS_PATH):
            with open(DATA_KEYS_PATH, encoding="utf-8") as f:
                return str(json.load(f).get(file_key, "") or "").strip()
    except Exception:  # noqa: BLE001 — Key 文件异常视为未配置
        pass
    return ""


def _coin(symbol: str) -> str:
    """合约代码 -> 币种基名，如 BTCUSDT -> BTC。"""
    s = (symbol or "").upper().replace("-", "").replace("/", "")
    for suffix in ("USDT", "USDC", "USD"):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s

# T-06 数据源单点故障降级：主源成功即落缓存，主源彻底失败时回退最近缓存（透明兜底）。
CACHE_DIR = os.path.expanduser("~/.vibe-trading/cache")
DEGRADE_LOG = os.path.expanduser("~/.vibe-trading/jarvis_data_degrade.log")


def _cache_key(url: str, params: Optional[dict]) -> str:
    raw = url + "?" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_write(key: str, data: Any) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, key + ".json"), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": data}, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — 缓存写失败绝不影响主流程
        pass


def _cache_read(key: str) -> Optional[dict]:
    try:
        with open(os.path.join(CACHE_DIR, key + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _degrade_log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(DEGRADE_LOG), exist_ok=True)
        with open(DEGRADE_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _is_ok(data: Any) -> bool:
    """主源响应是否可用（非空、非 _error）。"""
    if data is None:
        return False
    if isinstance(data, dict):
        return "_error" not in data and bool(data)
    if isinstance(data, list):
        return len(data) > 0
    return bool(data)


def _get(url: str, params: Optional[dict] = None, retries: int = 4) -> Any:
    """带指数退避的 GET，优雅处理 binance 限流(-1003/418/429)，主源失败回退缓存。"""
    key = _cache_key(url, params)
    delay = 1.5
    last_err = None
    jarvis_net.ensure_proxy()
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=TIMEOUT)
            if r.status_code in (418, 429):
                last_err = f"HTTP {r.status_code} rate-limited"
                time.sleep(delay)
                delay *= 2
                continue
            data = r.json()
            if isinstance(data, dict) and data.get("code") == -1003:
                last_err = data.get("msg", "rate-limited")
                time.sleep(delay)
                delay *= 2
                continue
            if _is_ok(data):
                _cache_write(key, data)
            return data
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)[:200]
            # 连接类失败可能是代理进程启停造成的，强制重探本地代理后再试
            jarvis_net.ensure_proxy(force=True)
            time.sleep(delay)
            delay *= 2
    cached = _cache_read(key)
    if cached is not None:
        age = int(time.time() - cached.get("ts", 0))
        _degrade_log(f"GET 降级用缓存 url={url} age={age}s err={last_err}")
        return cached.get("data")
    return {"_error": last_err}


def _get_text(url: str, retries: int = 3) -> Optional[str]:
    """拉取纯文本数字型接口(如 blockchain.info/q/*)，主源失败回退缓存。"""
    key = _cache_key(url, None)
    delay = 1.2
    for _ in range(retries):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and r.text.strip():
                txt = r.text.strip()
                _cache_write(key, txt)
                return txt
        except Exception:  # noqa: BLE001
            pass
        time.sleep(delay)
        delay *= 2
    cached = _cache_read(key)
    if cached is not None:
        age = int(time.time() - cached.get("ts", 0))
        _degrade_log(f"GET_TEXT 降级用缓存 url={url} age={age}s")
        return cached.get("data")
    return None


# ───────────────────────── T-06 备用源：OKX 公开行情 ─────────────────────────
# Binance 主源被墙/限频/宕机且无可用缓存时，切 OKX 取核心字段，保证关键字段不为空。
# OKX 返回形如 {"code":"0","msg":"","data":[{...}]}，仅当 data 非空才视为可用。

def _okx_swap_inst(symbol: str) -> str:
    """Binance 合约代码 -> OKX 永续 instId，如 BTCUSDT -> BTC-USDT-SWAP。"""
    s = symbol.upper().replace("-", "").replace("/", "")
    if s.endswith("USDT"):
        return s[:-4] + "-USDT-SWAP"
    if s.endswith("USD"):
        return s[:-3] + "-USD-SWAP"
    return s


def _okx_spot_inst(symbol: str) -> str:
    """Binance 合约代码 -> OKX 现货 instId，如 BTCUSDT -> BTC-USDT。"""
    s = symbol.upper().replace("-", "").replace("/", "")
    if s.endswith("USDT"):
        return s[:-4] + "-USDT"
    if s.endswith("USD"):
        return s[:-3] + "-USD"
    return s


def _okx_rows(resp: Any) -> Optional[list]:
    """从 OKX 响应里取 data 列表，非可用结构返回 None。"""
    if isinstance(resp, dict) and "_error" not in resp:
        data = resp.get("data")
        if isinstance(data, list) and data:
            return data
    return None


def _okx_funding(symbol: str) -> dict:
    """OKX 备用源：标记价 + 当前资金费率（字段与 fetch_funding 同形子集）。"""
    out: dict = {}
    inst = _okx_swap_inst(symbol)
    mk = _get(f"{OKX_API}/api/v5/public/mark-price", {"instType": "SWAP", "instId": inst})
    rows = _okx_rows(mk)
    if rows:
        mp = float(rows[0].get("markPx", 0) or 0)
        if mp:
            out["mark_price"] = mp
    fr = _get(f"{OKX_API}/api/v5/public/funding-rate", {"instId": inst})
    frows = _okx_rows(fr)
    if frows:
        rate = float(frows[0].get("fundingRate", 0) or 0)  # OKX 单期(8h)费率，与 binance 口径一致
        out["last_funding_rate_8h_pct"] = round(rate * 100, 5)
        out["annualized_pct"] = round(rate * 3 * 365 * 100, 2)
    if out:
        out["_source"] = "okx"
    return out


def _okx_oi(symbol: str) -> dict:
    """OKX 备用源：未平仓量（张数）。"""
    out: dict = {}
    inst = _okx_swap_inst(symbol)
    r = _get(f"{OKX_API}/api/v5/public/open-interest", {"instType": "SWAP", "instId": inst})
    rows = _okx_rows(r)
    if rows:
        oi = float(rows[0].get("oi", 0) or 0)
        if oi:
            out["open_interest_contracts"] = oi
            out["_source"] = "okx"
    return out


def _okx_spot_price(symbol: str) -> Optional[float]:
    """OKX 备用源：现货最新价（用于期现基差兜底）。"""
    inst = _okx_spot_inst(symbol)
    r = _get(f"{OKX_API}/api/v5/market/ticker", {"instId": inst})
    rows = _okx_rows(r)
    if rows:
        last = float(rows[0].get("last", 0) or 0)
        if last:
            return last
    return None


def fetch_daily_closes(symbol: str, days: int = 30) -> list[float]:
    """日线收盘价序列（由旧到新），供相关性/波动等计算。

    主源 Binance USDⓈ-M klines，失败切 OKX candles，再失败回退缓存（经 _get）。
    返回空列表表示彻底拿不到（调用方需自行兜底，绝不抛出）。
    """
    limit = max(2, int(days) + 1)
    kl = _get(f"{FAPI}/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": limit})
    if isinstance(kl, list) and len(kl) >= 2:
        try:
            return [float(row[4]) for row in kl]  # index 4 = 收盘价
        except (ValueError, IndexError, TypeError):
            pass
    # T-06 备用源：OKX 日线 candles（data 为最新在前，需反转为由旧到新）。
    inst = _okx_swap_inst(symbol)
    oc = _get(f"{OKX_API}/api/v5/market/candles", {"instId": inst, "bar": "1D", "limit": str(limit)})
    rows = _okx_rows(oc)
    if rows:
        try:
            closes = [float(row[4]) for row in rows]  # OKX index 4 = 收盘价
            closes.reverse()
            if len(closes) >= 2:
                _degrade_log(f"fetch_daily_closes 切备用源 OKX symbol={symbol}")
                return closes
        except (ValueError, IndexError, TypeError):
            pass
    return []


_KLINE_INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def fetch_kline(symbol: str, interval: str = "4h", total: int = 1500) -> list[dict]:
    """Binance Spot K 线分页拉取（单次上限 1000，用 endTime 往前翻页凑够 total 根）。

    返回升序 [{"ts": 开盘毫秒, "open", "high", "low", "close", "volume"}, ...]。
    复用 _get（自带限流退避 + 缓存降级）；彻底失败返回 []，绝不抛出。
    注意：返回包含「进行中的最后一根」，预测侧需自行丢弃未收盘 bar。
    """
    iv_ms = _KLINE_INTERVAL_MS.get(interval)
    if iv_ms is None:
        return []
    sym = (symbol or "").upper().replace("-", "").replace("/", "")
    if not sym.endswith(("USDT", "USDC", "USD")):
        sym += "USDT"
    total = max(2, min(int(total), 5000))
    out: list[dict] = []
    end_time: Optional[int] = None
    while len(out) < total:
        want = min(1000, total - len(out))
        params: dict = {"symbol": sym, "interval": interval, "limit": want}
        if end_time is not None:
            params["endTime"] = end_time
        raw = _get(SPOT_API + "/api/v3/klines", params)
        if not isinstance(raw, list) or not raw:
            break
        try:
            page = [{"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                     "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                    for r in raw]
        except (ValueError, IndexError, TypeError):
            break
        out = page + out
        if len(raw) < want:  # 历史到头了
            break
        end_time = page[0]["ts"] - 1  # 再往前翻一页
    # 去重（分页边界可能重叠）+ 升序
    seen: set = set()
    dedup = []
    for row in out:
        if row["ts"] not in seen:
            seen.add(row["ts"])
            dedup.append(row)
    dedup.sort(key=lambda r: r["ts"])
    return dedup


def fetch_funding(symbol: str) -> dict:
    """当前资金费率 + 标记价 + 近 7 日资金费率历史。"""
    out: dict = {}
    prem = _get(f"{FAPI}/fapi/v1/premiumIndex", {"symbol": symbol})
    if isinstance(prem, dict) and "_error" not in prem and "lastFundingRate" in prem:
        rate8h = float(prem["lastFundingRate"])
        out["last_funding_rate_8h_pct"] = round(rate8h * 100, 5)
        out["annualized_pct"] = round(rate8h * 3 * 365 * 100, 2)
        out["mark_price"] = float(prem.get("markPrice", 0) or 0)
        out["index_price"] = float(prem.get("indexPrice", 0) or 0)
        out["_source"] = "binance"
    else:
        out["_funding_error"] = prem.get("_error") if isinstance(prem, dict) else "no data"

    hist = _get(f"{FAPI}/fapi/v1/fundingRate", {"symbol": symbol, "limit": 21})
    if isinstance(hist, list) and hist:
        rates = [float(h["fundingRate"]) for h in hist]
        avg = sum(rates) / len(rates)
        out["funding_7d_avg_8h_pct"] = round(avg * 100, 5)
        last3 = rates[-3:]
        out["last3_all_positive"] = all(r > 0 for r in last3)
        out["last3_all_negative"] = all(r < 0 for r in last3)
        out["funding_regime"] = _funding_regime(avg, last3)

    # T-06 备用源回退：binance 主源拿不到标记价时切 OKX，保证 mark_price 不为空。
    if not out.get("mark_price"):
        okx = _okx_funding(symbol)
        if okx.get("mark_price"):
            out.pop("_funding_error", None)
            for k, v in okx.items():
                out.setdefault(k, v)
            _degrade_log(f"fetch_funding 切备用源 OKX symbol={symbol}")
    return out


def _funding_regime(avg: float, last3: list) -> str:
    pos = all(r > 0 for r in last3)
    neg = all(r < 0 for r in last3)
    if avg > 0.0003 and pos:
        return "overheated_long(挤压风险)"
    if avg > 0.0001 and pos:
        return "bullish_carry(温和看多)"
    if avg < -0.0002 and neg:
        return "overheated_short(空头挤压临近)"
    if avg < -0.00005 and neg:
        return "bearish_carry(温和看空)"
    return "neutral(中性)"


def fetch_oi(symbol: str) -> dict:
    """当前未平仓量 + 近 30 日 OI 历史(24h 变化)。"""
    out: dict = {}
    cur = _get(f"{FAPI}/fapi/v1/openInterest", {"symbol": symbol})
    if isinstance(cur, dict) and "openInterest" in cur:
        out["open_interest_contracts"] = float(cur["openInterest"])
        out["_source"] = "binance"
    hist = _get(f"{FAPI}/futures/data/openInterestHist", {"symbol": symbol, "period": "1d", "limit": 8})
    if isinstance(hist, list) and len(hist) >= 2:
        oi_vals = [float(h["sumOpenInterest"]) for h in hist]
        chg = (oi_vals[-1] - oi_vals[-2]) / oi_vals[-2] * 100 if oi_vals[-2] else 0
        out["oi_change_24h_pct"] = round(chg, 2)
        out["oi_7d_trend_pct"] = round((oi_vals[-1] - oi_vals[0]) / oi_vals[0] * 100, 2) if oi_vals[0] else 0

    # T-06 备用源回退：binance 拿不到当前 OI 时切 OKX（注：OKX 无等价的 24h/7d 历史接口，仅补当前值）。
    if not out.get("open_interest_contracts"):
        okx = _okx_oi(symbol)
        if okx.get("open_interest_contracts"):
            for k, v in okx.items():
                out.setdefault(k, v)
            _degrade_log(f"fetch_oi 切备用源 OKX symbol={symbol}")
    return out


def fetch_long_short(symbol: str) -> dict:
    """全网多空账户比 + 大户多空比。"""
    out: dict = {}
    g = _get(f"{FAPI}/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "1d", "limit": 7})
    if isinstance(g, list) and g:
        out["global_long_short_ratio"] = round(float(g[-1]["longShortRatio"]), 4)
        out["global_ls_7d"] = [round(float(x["longShortRatio"]), 3) for x in g]
    t = _get(f"{FAPI}/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": "1d", "limit": 3})
    if isinstance(t, list) and t:
        out["top_trader_long_short_ratio"] = round(float(t[-1]["longShortRatio"]), 4)
    return out


def fetch_taker(symbol: str) -> dict:
    """主动买卖量比(taker buy/sell)。"""
    out: dict = {}
    d = _get(f"{FAPI}/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "1d", "limit": 3})
    if isinstance(d, list) and d:
        out["taker_buy_sell_ratio"] = round(float(d[-1]["buySellRatio"]), 4)
    return out


def fetch_fng(limit: int = 14) -> dict:
    """恐慌贪婪指数。"""
    out: dict = {}
    d = _get(FNG_API, {"limit": limit})
    if isinstance(d, dict) and d.get("data"):
        rows = d["data"]
        out["fng_value"] = int(rows[0]["value"])
        out["fng_class"] = rows[0]["value_classification"]
        vals = [int(r["value"]) for r in rows]
        out["fng_14d_avg"] = round(sum(vals) / len(vals), 1)
        out["fng_14d_min"] = min(vals)
        out["fng_14d_max"] = max(vals)
    return out


def fetch_basis(symbol: str, mark_price: float) -> dict:
    """期现基差：永续标记价 vs 现货价。"""
    out: dict = {}
    sp = _get(f"{SPOT_API}/api/v3/ticker/price", {"symbol": symbol})
    spot = None
    if isinstance(sp, dict) and "price" in sp:
        spot = float(sp["price"])
        out["_source"] = "binance"
    # T-06 备用源回退：binance 现货价拿不到时切 OKX 现货行情。
    if not spot:
        okx_spot = _okx_spot_price(symbol)
        if okx_spot:
            spot = okx_spot
            out["_source"] = "okx"
            _degrade_log(f"fetch_basis 切备用源 OKX symbol={symbol}")
    if spot:
        out["spot_price"] = spot
        if mark_price and spot:
            out["perp_spot_basis_pct"] = round((mark_price - spot) / spot * 100, 4)
            out["basis_state"] = (
                "premium(期货升水/多头付费)" if mark_price > spot else "discount(期货贴水/空头付费)"
            )
    return out


def fetch_global() -> dict:
    """全市场结构：BTC/ETH 市占率、总市值、24h 成交量。"""
    out: dict = {}
    d = _get(f"{CG_API}/global")
    data = d.get("data") if isinstance(d, dict) else None
    if isinstance(data, dict):
        mc = data.get("market_cap_percentage", {}) or {}
        out["btc_dominance_pct"] = round(float(mc.get("btc", 0)), 2)
        out["eth_dominance_pct"] = round(float(mc.get("eth", 0)), 2)
        tmc = (data.get("total_market_cap", {}) or {}).get("usd")
        if tmc:
            out["total_market_cap_usd_b"] = round(float(tmc) / 1e9, 1)
        tv = (data.get("total_volume", {}) or {}).get("usd")
        if tv:
            out["total_volume_24h_usd_b"] = round(float(tv) / 1e9, 1)
        out["market_cap_change_24h_pct"] = round(
            float(data.get("market_cap_change_percentage_24h_usd", 0)), 2
        )
    return out


def fetch_onchain() -> dict:
    """BTC 链上基本面：算力、难度、内存池、手续费（仅 BTC 有意义）。"""
    out: dict = {}
    hr = _get_text(f"{BLOCKCHAIN_Q}/hashrate")
    if hr:
        try:
            out["hashrate_eh_s"] = round(float(hr) / 1e9, 1)  # GH/s -> EH/s
        except ValueError:
            pass
    diff = _get_text(f"{BLOCKCHAIN_Q}/getdifficulty")
    if diff:
        try:
            out["difficulty_t"] = round(float(diff) / 1e12, 2)
        except ValueError:
            pass
    fees = _get(f"{MEMPOOL_API}/v1/fees/recommended")
    if isinstance(fees, dict) and "fastestFee" in fees:
        out["fee_fastest_sat_vb"] = fees.get("fastestFee")
        out["fee_hour_sat_vb"] = fees.get("hourFee")
    mp = _get(f"{MEMPOOL_API}/mempool")
    if isinstance(mp, dict) and "count" in mp:
        out["mempool_tx_count"] = mp.get("count")
    return out


def fetch_liquidations(symbol: str) -> dict:
    """[T-13] Coinglass 爆仓/清算数据（需 Key）。无 Key → 优雅降级 _skipped。

    免 Key 时不报错、不阻塞主链路；配置 `JARVIS_COINGLASS_KEY`（或 data_keys.json
    的 `coinglass`）后返回 24h 多空清算额等。任何网络/格式异常都降级为 _error 但不抛出。
    """
    key = _data_key("JARVIS_COINGLASS_KEY", "coinglass")
    if not key:
        return {"_skipped": True, "reason": "未配置 COINGLASS_KEY（链上/清算数据降级为 —）"}
    out: dict = {"_source": "coinglass"}
    try:
        url = f"{COINGLASS_API}/api/futures/liquidation/history"
        r = requests.get(url, params={"symbol": _coin(symbol), "interval": "1d", "limit": 1},
                         headers={**_HEADERS, "CG-API-KEY": key}, timeout=TIMEOUT)
        body = r.json()
        rows = body.get("data") if isinstance(body, dict) else None
        if isinstance(rows, list) and rows:
            last = rows[-1] if isinstance(rows[-1], dict) else {}
            long_liq = last.get("longLiquidationUsd") or last.get("long_liquidation_usd")
            short_liq = last.get("shortLiquidationUsd") or last.get("short_liquidation_usd")
            if long_liq is not None:
                out["long_liquidation_usd_24h"] = float(long_liq)
            if short_liq is not None:
                out["short_liquidation_usd_24h"] = float(short_liq)
            if long_liq is not None and short_liq is not None and (float(long_liq) + float(short_liq)) > 0:
                tot = float(long_liq) + float(short_liq)
                out["long_liq_share_pct"] = round(float(long_liq) / tot * 100, 1)
        else:
            out["_error"] = (body.get("msg") if isinstance(body, dict) else "no data") or "no data"
    except Exception as exc:  # noqa: BLE001 — 扩展源失败不影响主链路
        out["_error"] = repr(exc)[:160]
    return out


def fetch_exchange_flows(symbol: str) -> dict:
    """[T-13] Glassnode 链上：交易所净流（需 Key）。无 Key → 优雅降级 _skipped。

    净流为负=资金流出交易所（偏多，囤币）；为正=流入（潜在抛压）。配置
    `JARVIS_GLASSNODE_KEY`（或 data_keys.json 的 `glassnode`）后生效。
    """
    key = _data_key("JARVIS_GLASSNODE_KEY", "glassnode")
    if not key:
        return {"_skipped": True, "reason": "未配置 GLASSNODE_KEY（交易所净流/大额转账降级为 —）"}
    out: dict = {"_source": "glassnode"}
    try:
        url = f"{GLASSNODE_API}/v1/metrics/transactions/transfers_volume_exchanges_net"
        r = requests.get(url, params={"a": _coin(symbol), "i": "24h", "api_key": key},
                         headers=_HEADERS, timeout=TIMEOUT)
        body = r.json()
        if isinstance(body, list) and body:
            last = body[-1]
            if isinstance(last, dict) and "v" in last:
                net = float(last["v"])
                out["exchange_netflow_native_24h"] = round(net, 4)
                out["netflow_state"] = "outflow(流出/偏多)" if net < 0 else "inflow(流入/潜在抛压)"
        else:
            out["_error"] = (body.get("message") if isinstance(body, dict) else "no data") or "no data"
    except Exception as exc:  # noqa: BLE001
        out["_error"] = repr(exc)[:160]
    return out


def collect(symbol: str) -> dict:
    funding = fetch_funding(symbol)
    mark = funding.get("mark_price", 0) if isinstance(funding, dict) else 0
    data = {
        "symbol": symbol,
        "fetched_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "funding": funding,
        "basis": fetch_basis(symbol, mark),
        "open_interest": fetch_oi(symbol),
        "long_short": fetch_long_short(symbol),
        "taker": fetch_taker(symbol),
        "fear_greed": fetch_fng(),
        "market_structure": fetch_global(),
    }
    if symbol.upper().startswith("BTC"):
        data["onchain"] = fetch_onchain()
    # T-13 扩展数据源（Coinglass 清算 + Glassnode 交易所净流）：无 Key 时 _skipped 降级。
    data["liquidations"] = fetch_liquidations(symbol)
    data["exchange_flows"] = fetch_exchange_flows(symbol)
    # T-06 数据源透明度：汇总各核心字段实际来源（binance / okx），便于排障与降级感知。
    data["_sources"] = {
        "funding": funding.get("_source") if isinstance(funding, dict) else None,
        "open_interest": data["open_interest"].get("_source"),
        "basis": data["basis"].get("_source"),
    }
    return data


def to_markdown(d: dict) -> str:
    f = d["funding"]
    ba = d.get("basis", {})
    oi = d["open_interest"]
    ls = d["long_short"]
    tk = d["taker"]
    fng = d["fear_greed"]
    ms = d.get("market_structure", {})
    oc = d.get("onchain", {})
    lines = [
        f"## 真实加密数据 — {d['symbol']}  (UTC {d['fetched_at_utc']})",
        "",
        "### 资金费率 / 期现基差",
        f"- 当前 8h 资金费率: {f.get('last_funding_rate_8h_pct', 'N/A')}%  (年化 {f.get('annualized_pct', 'N/A')}%)",
        f"- 标记价: {f.get('mark_price', 'N/A')} | 指数价: {f.get('index_price', 'N/A')} | 现货价: {ba.get('spot_price', 'N/A')}",
        f"- 期现基差: {ba.get('perp_spot_basis_pct', 'N/A')}% | {ba.get('basis_state', 'N/A')}",
        f"- 7 日均资金费率: {f.get('funding_7d_avg_8h_pct', 'N/A')}% | 状态: {f.get('funding_regime', 'N/A')}",
        "",
        "### 未平仓量 (OI)",
        f"- 当前 OI: {oi.get('open_interest_contracts', 'N/A')} 张",
        f"- 24h 变化: {oi.get('oi_change_24h_pct', 'N/A')}% | 7日趋势: {oi.get('oi_7d_trend_pct', 'N/A')}%",
        "",
        "### 多空持仓",
        f"- 全网多空账户比: {ls.get('global_long_short_ratio', 'N/A')}  (7日: {ls.get('global_ls_7d', 'N/A')})",
        f"- 大户多空比: {ls.get('top_trader_long_short_ratio', 'N/A')}",
        f"- 主动买卖量比(taker): {tk.get('taker_buy_sell_ratio', 'N/A')}",
        "",
        "### 市场结构",
        f"- BTC 市占率: {ms.get('btc_dominance_pct', 'N/A')}% | ETH 市占率: {ms.get('eth_dominance_pct', 'N/A')}%",
        f"- 全市场总市值: {ms.get('total_market_cap_usd_b', 'N/A')} 十亿美元 | 24h 成交量: {ms.get('total_volume_24h_usd_b', 'N/A')} 十亿美元",
        f"- 总市值 24h 变化: {ms.get('market_cap_change_24h_pct', 'N/A')}%",
        "",
        "### 市场情绪",
        f"- 恐慌贪婪指数: {fng.get('fng_value', 'N/A')} ({fng.get('fng_class', 'N/A')})",
        f"- 14日: 均{fng.get('fng_14d_avg', 'N/A')} 区间[{fng.get('fng_14d_min', 'N/A')}, {fng.get('fng_14d_max', 'N/A')}]",
    ]
    if oc:
        lines += [
            "",
            "### BTC 链上基本面",
            f"- 全网算力: {oc.get('hashrate_eh_s', 'N/A')} EH/s | 挖矿难度: {oc.get('difficulty_t', 'N/A')} T",
            f"- 内存池待确认交易: {oc.get('mempool_tx_count', 'N/A')} 笔 | 最快手续费: {oc.get('fee_fastest_sat_vb', 'N/A')} sat/vB (1小时档: {oc.get('fee_hour_sat_vb', 'N/A')})",
        ]
    # T-13 扩展数据源（清算 / 交易所净流）：无 Key 时显示「—（未配置 Key）」。
    liq = d.get("liquidations", {}) or {}
    fl = d.get("exchange_flows", {}) or {}
    if liq or fl:
        lines += ["", "### 扩展数据源（T-13：需 Key）"]
        if liq.get("_skipped"):
            lines.append("- 清算(Coinglass): —（未配置 JARVIS_COINGLASS_KEY）")
        elif liq.get("_error"):
            lines.append(f"- 清算(Coinglass): 取数失败 {liq.get('_error')}")
        else:
            lines.append(
                f"- 24h 清算: 多 {liq.get('long_liquidation_usd_24h', 'N/A')}U / "
                f"空 {liq.get('short_liquidation_usd_24h', 'N/A')}U（多头占比 {liq.get('long_liq_share_pct', 'N/A')}%）"
            )
        if fl.get("_skipped"):
            lines.append("- 交易所净流(Glassnode): —（未配置 JARVIS_GLASSNODE_KEY）")
        elif fl.get("_error"):
            lines.append(f"- 交易所净流(Glassnode): 取数失败 {fl.get('_error')}")
        else:
            lines.append(
                f"- 交易所净流(24h): {fl.get('exchange_netflow_native_24h', 'N/A')} | {fl.get('netflow_state', 'N/A')}"
            )
    lines += [
        "",
        "> 数据来源: Binance Futures/Spot + alternative.me + CoinGecko + blockchain.info/mempool.space（真实拉取，非估算）。",
    ]
    srcs = d.get("_sources", {}) or {}
    if "okx" in srcs.values():
        degraded = [k for k, v in srcs.items() if v == "okx"]
        lines.append(f"> ⚠ 数据降级：以下字段由备用源 OKX 提供（Binance 主源不可用）：{', '.join(degraded)}。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯真实加密衍生品数据拉取")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT", help="期货合约代码, 如 BTCUSDT / ETHUSDT")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非 Markdown")
    args = ap.parse_args()
    symbol = args.symbol.upper().replace("-", "").replace("/", "")
    data = collect(symbol)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
