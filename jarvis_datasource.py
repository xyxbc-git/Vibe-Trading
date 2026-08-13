#!/usr/bin/env python3
"""贾维斯数据源健康/切换器（任务 J2，2026-08-13）。

职责（只做三件事，不碰采数主链路）：
  1. probe(source)   —— 轻量真探目标源（server time 端点，权重 1，经 jcd._get
     三道闸）；币安处于真实封禁/冷却期时**短路不真发**（读 banned_until），
     返回 short_circuit=True + 人话原因——防止探测本身延长封禁。
  2. status()        —— 当前模式 / 各源健康（封禁截止、最近探测、权重水位）/
     策略屏蔽范围 / 数据类型×源 能力矩阵（含「该源不支持的类型仍用主源」标注）。
  3. switch(mode)    —— 「先探测后生效」：目标源探测通过才写模式（jarvis_net.
     set_source_mode，跨进程 ≤5s 热生效）；探测失败**不切换**并返回原因。
     auto 模式为自愈缺省，无单一目标源，直接生效不探测。

模式对采数链路的实际效果（与 jarvis_net 策略层、status 标注三方一致）：
  auto     现状零变化（币安主源，故障自动回退 OKX——crypto_data T-06 链）；
  binance  锁定币安：OKX 全端点被策略屏蔽，币安不可用时降级走缓存（stale）；
  okx      费率/OI/最新价的币安端点被屏蔽 → 无新鲜缓存时 T-06 链切 OKX；
           多周期K线/深度/多空比/aggTrades 无 OKX 等价数据，仍走币安（如实标注）。
           注意：屏蔽端点若存在历史缓存，_get 封禁短路路径会先回缓存（与真实
           封禁期行为一致）——缓存老化后才切 OKX，status 已标注该语义。

零出网承诺：status() 纯读本地状态文件，绝不出网；probe() 只打 server time。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import jarvis_net

FAPI_HOST = "fapi.binance.com"
SPOT_HOST = "api.binance.com"
OKX_HOST = "www.okx.com"
FAPI_TIME_URL = "https://fapi.binance.com/fapi/v1/time"
OKX_TIME_URL = "https://www.okx.com/api/v5/public/time"

_PROBE_STATE_PATH = os.path.expanduser("~/.vibe-trading/datasource_probe.json")
_WEIGHT_PATH = os.path.expanduser("~/.vibe-trading/net_weight.json")

MODES = ("auto", "binance", "okx")

# 数据类型 × 源 能力矩阵（V1 静态声明，与 jarvis_crypto_data T-06 实现逐条核对：
# okx=True 的类型在 crypto_data 有现成 OKX 取数函数；False=无等价备源仍用币安）
CAPABILITY: dict[str, dict[str, bool]] = {
    "klines_multi_tf":  {"binance": True, "okx": False},  # fetch_klines_df 仅币安
    "daily_closes":     {"binance": True, "okx": True},   # fetch_daily_closes→OKX candles
    "latest_price":     {"binance": True, "okx": True},   # _okx_swap_price/_okx_spot_price
    "funding_rate":     {"binance": True, "okx": True},   # _okx_funding
    "open_interest":    {"binance": True, "okx": True},   # _okx_oi（仅当前值，无历史）
    "long_short_ratio": {"binance": True, "okx": False},  # /futures/data/* 仅币安
    "orderbook_depth":  {"binance": True, "okx": False},  # jarvis_orderbook 仅币安
    "agg_trades":       {"binance": True, "okx": False},  # 桌面足迹回填仅币安
}


def _now_hms(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001 — 缺失/损坏视为空
        return {}


def _save_probe(source: str, result: dict) -> None:
    """最近一次探测结果落盘（跨进程共享给 status；写失败静默）。"""
    try:
        data = _load_json(_PROBE_STATE_PATH)
        data[source] = dict(result, at=time.time())
        tmp = _PROBE_STATE_PATH + ".tmp"
        os.makedirs(os.path.dirname(_PROBE_STATE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _PROBE_STATE_PATH)
    except Exception:  # noqa: BLE001
        pass


def probe(source: str) -> dict:
    """轻量真探：server time 端点测「可达 + 延迟」。

    币安封禁/冷却期短路不真发（红线：封禁期继续请求会延长封禁）；OKX 无
    封禁登记时真实探测。探测经 jcd._get 三道闸（预算权重 1 计账）。
    """
    src = str(source or "").lower()
    if src not in ("binance", "okx"):
        return {"ok": False, "source": src, "reason": f"未知源 {source!r}（可选 binance/okx）"}
    url = FAPI_TIME_URL if src == "binance" else OKX_TIME_URL
    ban = jarvis_net.banned_until(url)
    if ban:
        out = {"ok": False, "source": src, "short_circuit": True, "banned_until": ban,
               "reason": f"源处于封禁/冷却期至 {_now_hms(ban)}，已短路未真发请求"}
        _save_probe(src, out)
        return out
    import jarvis_crypto_data as jcd  # 懒导入：探测才需要，status 纯本地不触发
    t0 = time.time()
    data = jcd._get(url, fast=True, ttl=0.0)
    latency_ms = round((time.time() - t0) * 1000.0, 1)
    ok = False
    if isinstance(data, dict) and "_error" not in data:
        # binance: {"serverTime": ms}；okx: {"code":"0","data":[{"ts":...}]}
        ok = bool(data.get("serverTime") or str(data.get("code", "")) == "0")
    out: dict[str, Any] = {"ok": ok, "source": src, "latency_ms": latency_ms}
    if not ok:
        err = data.get("_error") if isinstance(data, dict) else None
        out["reason"] = str(err or "响应结构异常")[:200]
    _save_probe(src, out)
    return out


def _weight_view(host: str) -> Optional[dict]:
    """net_weight.json 中该主机最近回报的 IP 已用权重（75s 内有效口径）。"""
    rec = _load_json(_WEIGHT_PATH).get(host)
    if not isinstance(rec, dict):
        return None
    try:
        return {"used_1m": float(rec.get("w") or 0),
                "age_s": round(time.time() - float(rec.get("ts") or 0), 1)}
    except (TypeError, ValueError):
        return None


def _source_health(host: str, probe_key: str) -> dict:
    """单源健康视图：封禁截止（含策略合成）+ 最近探测 + 权重水位。纯本地读。"""
    ban = jarvis_net.banned_until(f"https://{host}/")
    out: dict[str, Any] = {
        "host": host,
        "banned_until": ban or 0,
        "banned_until_hms": _now_hms(ban) if ban else None,
    }
    lp = _load_json(_PROBE_STATE_PATH).get(probe_key)
    if isinstance(lp, dict):
        out["last_probe"] = lp
    w = _weight_view(host)
    if w:
        out["used_weight_1m"] = w
    return out


def status() -> dict:
    """数据源全景（纯读本地状态，零出网）：模式/策略/各源健康/能力矩阵。"""
    policy = jarvis_net.source_policy()
    mode = policy.get("mode", "auto")
    # 各类型生效源：auto=主源+故障回退；手动=锁定源，该源不支持的类型仍用主源
    effective: dict[str, str] = {}
    for dtype, cap in CAPABILITY.items():
        if mode == "okx":
            effective[dtype] = ("okx（币安侧策略屏蔽，缓存老化后生效）"
                                if cap.get("okx") else "binance（OKX 无等价数据，仍用主源）")
        elif mode == "binance":
            effective[dtype] = "binance（已锁定，OKX 备源屏蔽）"
        else:
            effective[dtype] = ("binance 主源，故障自动回退 okx"
                                if cap.get("okx") else "binance（无备源，故障走缓存降级）")
    return {
        "ok": True,
        "mode": mode,
        "modes": list(MODES),
        "policy": policy,
        "sources": {
            "binance_futures": _source_health(FAPI_HOST, "binance"),
            "binance_spot": _source_health(SPOT_HOST, "binance"),
            "okx": _source_health(OKX_HOST, "okx"),
        },
        "capability": CAPABILITY,
        "effective_source": effective,
        "degrade_semantics": ("锁定源完全不可用时：有历史缓存回缓存（stale），"
                              "无缓存返回 _error 由面板显示占位——不会崩溃、不会伪造数据"),
        "ts": time.time(),
    }


def switch(mode: str, by: str = "api") -> dict:
    """「先探测后生效」切换：目标源探测失败不切换（返回 ok=False + 原因）。

    auto 为自愈缺省（币安主源+自动回退），无单一目标源——直接生效不探测。
    """
    m = str(mode or "").lower()
    if m not in MODES:
        return {"ok": False, "reason": f"未知模式 {mode!r}，可选 {list(MODES)}"}
    if m == "auto":
        state = jarvis_net.set_source_mode(m, by=by)
        return {"ok": True, "mode": m, "state": state,
                "note": "auto=币安主源+故障自动回退 OKX（未探测：自愈缺省模式）"}
    p = probe(m)
    if not p.get("ok"):
        return {"ok": False, "mode_unchanged": jarvis_net.get_source_mode(),
                "reason": f"目标源 {m} 探测失败，已保持原源：" + str(p.get("reason", ""))[:200],
                "probe": p}
    state = jarvis_net.set_source_mode(m, by=by)
    return {"ok": True, "mode": m, "state": state, "probe": p}
