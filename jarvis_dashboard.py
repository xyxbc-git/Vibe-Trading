#!/usr/bin/env python3
"""贾维斯 JARVIS - 可视化仪表盘（独立 FastAPI 应用）。

把前面跑通的 4 个脚本（真实数据 / 因子回测 / 样本外验证 / 闭环简报）
封装成一个网页仪表盘，浏览器里一眼看清：
  - 实时决策简报（信心分 / 仓位 / 止损 / 时间止损）
  - 真实数据卡片（资金费率/OI/多空比/情绪/链上/市场结构）
  - 恐慌贪婪 & 回撤历史曲线（echarts）
  - 已验证因子事件研究表（含 P4 过拟合警示）

独立端口运行，不影响 Vibe-Trading 主服务。
用法：
  ./.venv/bin/python jarvis_dashboard.py            # 默认 127.0.0.1:7899
  ./.venv/bin/python jarvis_dashboard.py --port 7899
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import jarvis_brief as jb
import jarvis_crypto_data as jcd
import jarvis_executor as jx
import jarvis_journal as jj
import jarvis_paper_trader as jpt
import jarvis_wallet as jw
from jarvis_factor_backtest import _build_series, event_study, fetch_fng_all, fetch_price_daily

def _load_env_file() -> None:
    """零依赖加载脚本同目录下的 .env（KEY=VALUE，每行一条，# 开头为注释）。
    已存在于真实环境变量的不覆盖；让用户有一个文件可填 DeepSeek/OpenAI key。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_env_file()

app = FastAPI(title="贾维斯仪表盘")

# 简单内存缓存：{key: (ts, value)}，TTL 控制刷新频率
_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: int, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


@app.get("/api/snapshot")
def snapshot(symbol: str = "BTCUSDT"):
    sym = symbol.upper().replace("-", "").replace("/", "")
    data = _cached(f"snap:{sym}", 300, lambda: jb.build(sym))
    return JSONResponse(data)


@app.get("/api/series")
def series(symbol: str = "BTCUSDT", days: int = 365):
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"

    def _calc():
        prices = fetch_price_daily(spot)
        fng = fetch_fng_all()
        dates = sorted(prices)
        closes, _ma, dd = _build_series(dates, prices)
        sl = slice(max(0, len(dates) - days), len(dates))
        ds = dates[sl]
        return {
            "dates": ds,
            "close": [round(c, 2) for c in closes[sl]],
            "drawdown_pct": [round(x * 100, 2) for x in dd[sl]],
            "fng": [fng.get(d) for d in ds],
        }

    return JSONResponse(_cached(f"series:{sym}:{days}", 600, _calc))


@app.get("/api/kline")
def kline(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 200):
    """Binance 公开 K线（免 Key），供 echarts 自绘蜡烛图 + 叠加决策信号。"""
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    allowed = {"1m", "5m", "15m", "1h", "4h", "1d"}
    iv = interval if interval in allowed else "1h"
    lim = max(20, min(int(limit), 500))

    def _calc():
        raw = jcd._get(jcd.SPOT_API + "/api/v3/klines", {"symbol": spot, "interval": iv, "limit": lim})
        if isinstance(raw, dict):
            return {"error": raw.get("_error", "kline fetch failed"), "rows": []}
        fmt = "%m-%d" if iv in ("1d",) else "%m-%d %H:%M"
        rows = []
        for k in raw:
            ts = time.localtime(k[0] / 1000)
            rows.append({
                "t": time.strftime(fmt, ts),
                "ts": int(k[0]),
                "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                "c": float(k[4]), "v": round(float(k[5]), 2),
            })
        return {"symbol": spot, "interval": iv, "rows": rows}

    return JSONResponse(_cached(f"kline:{spot}:{iv}:{lim}", 60, _calc))


@app.get("/api/factor")
def factor(symbol: str = "BTCUSDT"):
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"

    def _calc():
        prices = fetch_price_daily(spot)
        fng = fetch_fng_all()
        dates = sorted(set(prices) & set(fng))
        return event_study(dates, prices, fng)

    return JSONResponse(_cached(f"factor:{sym}", 1800, _calc))


@app.get("/api/calibration")
def calibration(symbol: str = "BTCUSDT", horizon: int = 30):
    """置信度校准：贾维斯的信心 vs 实际兑现率 + Brier 分。

    口径（透明可复核）：
      - 把信心分 score 的绝对值映射为「方向判断把握度」implied_prob = 0.5 + min(|score|,2)/2 * 0.45（0.5~0.95）。
      - 只统计已评估且方向可判定（偏多/偏空）的快照；中性观望不计入。
      - 实际命中 y = correct(1/0)。Brier = mean((p-y)^2)，越低越好；
        基线 Brier = mean((0.5-y)^2)；Brier 技巧分 BSS = 1 - Brier/基线（>0 说明比瞎猜强）。
    """
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    h = 7 if int(horizon) == 7 else 30
    ckey, rkey = (f"c{h}", f"r{h}")

    def _calc():
        rows = jj.list_recent(spot, 100000)
        pts = []  # (implied_prob, y, score)
        for r in rows:
            score = r.get("conviction_score")
            y = r.get(ckey)
            if score is None or y is None:
                continue
            conf = min(2.0, abs(float(score))) / 2.0
            p = round(0.5 + conf * 0.45, 4)
            pts.append((p, int(y), float(score)))
        n = len(pts)
        if n == 0:
            return {"symbol": spot, "horizon": h, "n": 0, "buckets": [], "brier": None,
                    "brier_baseline": None, "bss": None}
        edges = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]
        labels = ["50-60%", "60-70%", "70-80%", "80-90%", "90-100%"]
        buckets = []
        for i in range(len(labels)):
            lo, hi = edges[i], edges[i + 1]
            grp = [t for t in pts if lo <= t[0] < hi]
            if not grp:
                continue
            buckets.append({
                "label": labels[i],
                "n": len(grp),
                "pred_pct": round(100 * sum(t[0] for t in grp) / len(grp), 1),
                "actual_pct": round(100 * sum(t[1] for t in grp) / len(grp), 1),
            })
        brier = sum((p - y) ** 2 for p, y, _ in pts) / n
        base = sum((0.5 - y) ** 2 for _, y, _ in pts) / n
        bss = (1 - brier / base) if base > 0 else None
        return {
            "symbol": spot, "horizon": h, "n": n,
            "overall_hit_pct": round(100 * sum(y for _, y, _ in pts) / n, 1),
            "buckets": buckets,
            "brier": round(brier, 4),
            "brier_baseline": round(base, 4),
            "bss": round(bss, 3) if bss is not None else None,
        }

    return JSONResponse(_cached(f"calib:{spot}:{h}", 120, _calc))


@app.get("/api/track")
def track(symbol: str = "BTCUSDT"):
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"

    def _calc():
        return {
            "report": jj.report(spot),
            "recent": jj.list_recent(spot, 30),
        }

    return JSONResponse(_cached(f"track:{sym}", 300, _calc))


@app.post("/api/track/record")
def track_record(symbol: str = "BTCUSDT"):
    """落一条今日快照并回填到期收益，然后清缓存。"""
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    rec = jj.record(spot)
    ev = jj.evaluate(spot)
    _CACHE.pop(f"track:{sym}", None)
    return JSONResponse({"record": rec, "evaluate": ev})


# ─────────────────────────── 模拟交易台账 API ───────────────────────────

def _trader_cfg():
    return jx.load_config()


@app.get("/api/wallet")
def api_wallet():
    cfg = _trader_cfg()
    return JSONResponse(jw.ensure_account(cfg.get("account_equity_usdt", 1000.0)))


@app.get("/api/trader/status")
def api_trader_status(symbol: str | None = None):
    """跟盘账户总览：钱包总权益 + 盈亏比 + 持仓明细。"""
    return JSONResponse(jpt.stats(_trader_cfg(), symbol))


@app.get("/api/positions")
def api_positions(symbol: str | None = None, status: str = "all"):
    rows = jpt.all_positions(symbol)
    if status == "open":
        rows = [r for r in rows if r["status"] == "open"]
    elif status == "closed":
        rows = [r for r in rows if r["status"] == "closed"]

    # 给所有行补 direction（前端按 long/short 渲染，DB 里只有 buy/sell）
    # 同时给 open 持仓补 current_price / pnl_pct / pnl_usdt
    # （latest_price 走 60s 缓存，避免每次轮询都打外部 API；取价失败 → 字段为 None，前端按缺失态显示）。
    cfg = _trader_cfg()
    for r in rows:
        side = (r.get("side") or "buy").lower()
        r["direction"] = "long" if side == "buy" else "short"
        if r.get("status") != "open":
            continue
        sym = r.get("symbol")
        entry = r.get("entry_price")
        if not sym or entry in (None, 0):
            continue
        try:
            price = _cached(f"pos_price:{sym}", 60, lambda s=sym: jpt.latest_price(cfg, s))
        except Exception:  # noqa: BLE001
            price = None
        if price is None:
            continue
        sign = 1 if side == "buy" else -1
        qty = r.get("qty") or 0
        r["current_price"] = round(float(price), 8)
        r["pnl_pct"] = round((price / entry - 1.0) * 100 * sign, 2)
        r["pnl_usdt"] = round((price - entry) * qty * sign, 4)
    return JSONResponse(rows)


@app.get("/api/orders")
def api_orders(symbol: str | None = None, all: bool = False):
    return JSONResponse(jw.all_limit_orders(symbol) if all else jw.pending_orders(symbol))


@app.get("/api/ledger")
def api_ledger(limit: int = 50, symbol: str | None = None):
    return JSONResponse(jw.ledger(limit, symbol))


@app.post("/api/wallet/deposit")
def api_deposit(amount: float):
    if amount <= 0:
        return JSONResponse({"ok": False, "reason": "金额必须 > 0"}, status_code=400)
    return JSONResponse(jw.deposit(amount))


@app.post("/api/orders/place")
def api_place_order(symbol: str, side: str, price: float, qty: float,
                    stop_loss: float | None = None, take_profit: float | None = None):
    cfg = _trader_cfg()
    jw.ensure_account(cfg.get("account_equity_usdt", 1000.0))
    return JSONResponse(jw.place_limit_order(symbol, side, price, qty,
                                             stop_loss=stop_loss, take_profit=take_profit))


@app.post("/api/orders/cancel")
def api_cancel_order(order_id: int):
    return JSONResponse(jw.cancel_limit_order(order_id))


@app.post("/api/orders/match")
def api_match_orders():
    return JSONResponse({"matched": jpt.match_limit_orders(_trader_cfg())})


@app.post("/api/positions/close")
def api_close_position(symbol: str):
    cfg = _trader_cfg()
    out = []
    for p in jpt.open_positions(symbol):
        price = jpt.latest_price(cfg, p["symbol"]) or p.get("entry_price")
        out.append(jpt._close_position(p, price, "manual", cfg))
    return JSONResponse({"closed": out})


@app.post("/api/trader/cycle")
def api_trader_cycle(symbols: str = "BTC,ETH,SOL", dry_run: bool = False):
    """跑一轮自动跟盘：撮合限价单 → 盯平仓 → 按决策找开仓。"""
    cfg = _trader_cfg()
    syms = [s.strip() for s in symbols.split(",") if s.strip()] or ["BTC"]
    return JSONResponse(jpt.run_cycle(syms, cfg, dry_run=dry_run))


# ─────────────────────────── 事件流 + 智能问答 API ───────────────────────────

_EXIT_REASON_CN = {
    "stop_loss": "触发止损离场",
    "take_profit": "触发止盈落袋",
    "time_stop": "持有到期离场",
    "signal_flip": "信号反转离场",
    "manual": "手动平仓",
}


def _market_events(spot: str) -> list[dict]:
    """从真实行情数据（恐慌贪婪/资金费率/持仓量/多空比/市值/动量）+ K 线急涨急跌，
    生成真实市场事件，而非模拟盘动作。全部基于 jcd.collect / kline 实拉数据，不编造。"""
    evs: list[dict] = []
    now = time.time()

    def _add(ts, title, detail, tone):
        evs.append({"ts": float(ts), "kind": "market", "symbol": spot,
                    "title": title, "detail": detail, "tone": tone})

    # 1) 市场状态事件（来自 snapshot 实时真实数据，时间戳=快照生成时刻）
    try:
        snap = _cached(f"snap:{spot}", 300, lambda: jb.build(spot))
        rd = snap.get("real_data", {}) or {}
        fac = snap.get("factor_state", {}) or {}
        fng = rd.get("fear_greed", {}) or {}
        fund = rd.get("funding", {}) or {}
        oi = rd.get("open_interest", {}) or {}
        ls = rd.get("long_short", {}) or {}
        ms = rd.get("market_structure", {}) or {}

        v = fng.get("fng_value")
        if v is not None:
            tone = "loss" if v <= 25 else ("win" if v >= 75 else "info")
            _add(now, f"恐慌贪婪指数 {v} · {fng.get('fng_class', '')}",
                 "市场情绪极端，常是反向参考" if (v <= 25 or v >= 75) else "市场情绪中性", tone)

        fr = fund.get("last_funding_rate_8h_pct")
        if fr is not None:
            reg = fund.get("funding_regime", "")
            tone = "loss" if fr >= 0.05 else ("win" if fr <= -0.01 else "info")
            _add(now, f"资金费率 {fr}% / 8h · {reg}",
                 "多头拥挤·留意回调" if fr >= 0.05 else ("空头付费·偏多有利" if fr <= -0.01 else "费率平稳"), tone)

        oic = oi.get("oi_change_24h_pct")
        if oic is not None:
            tone = "info"
            _add(now, f"持仓量 24h {'+' if oic >= 0 else ''}{oic}%",
                 "杠杆增加·波动可能放大" if oic >= 5 else ("杠杆撤离·去风险" if oic <= -5 else "持仓平稳"),
                 "loss" if oic >= 8 else tone)

        lsr = ls.get("global_long_short_ratio")
        if lsr is not None:
            _add(now, f"全网多空比 {lsr}",
                 "散户偏多·注意反向" if lsr >= 1.5 else ("散户偏空·注意反向" if lsr <= 0.7 else "多空均衡"),
                 "info")

        mcc = ms.get("market_cap_change_24h_pct")
        if mcc is not None:
            _add(now, f"总市值 24h {'+' if mcc >= 0 else ''}{mcc}%",
                 "大盘整体走强" if mcc >= 2 else ("大盘整体走弱" if mcc <= -2 else "大盘平稳"),
                 "win" if mcc >= 2 else ("loss" if mcc <= -2 else "info"))

        dd = fac.get("drawdown_from_ath_pct")
        if dd is not None and dd <= -25:
            _add(now, f"距历史高点回撤 {dd}%", "进入深度回撤区·弱抄底因子关注", "win")

        mom = fac.get("momentum_30d_pct")
        if mom is not None:
            if mom >= 10:
                _add(now, f"30 日动量 +{mom}%", "中期趋势偏强", "win")
            elif mom <= -10:
                _add(now, f"30 日动量 {mom}%", "中期趋势偏弱", "loss")
    except Exception:  # noqa: BLE001
        pass

    # 2) 价格异动事件（来自真实 1h K 线，单根 |涨跌| ≥ 2% 记一条，用真实 bar 时间戳）
    try:
        kraw = jcd._get(jcd.SPOT_API + "/api/v3/klines",
                        {"symbol": spot, "interval": "1h", "limit": 120})
        if not isinstance(kraw, dict):
            cnt = 0
            for k in reversed(kraw[-48:]):
                o, c = float(k[1]), float(k[4])
                if o <= 0:
                    continue
                chg = (c - o) / o * 100
                if abs(chg) >= 2.0:
                    cnt += 1
                    _add(k[0] / 1000,
                         f"{'急涨' if chg > 0 else '急跌'} {'+' if chg > 0 else ''}{round(chg, 2)}%（1h）",
                         f"收于 {round(c, 2)}", "win" if chg > 0 else "loss")
                if cnt >= 8:
                    break
    except Exception:  # noqa: BLE001
        pass

    return evs


@app.get("/api/events")
def api_events(symbol: str | None = None, limit: int = 40):
    """聚合模拟盘开仓/平仓/止盈止损/信号反转 + 真实市场事件，按时间倒序滚动给前端。"""
    sym = None
    if symbol:
        s = symbol.upper().replace("-", "").replace("/", "")
        sym = s if s.endswith("USDT") else s + "USDT"

    def _calc():
        evs: list[dict] = []
        if sym:
            evs.extend(_market_events(sym))
        for p in jpt.all_positions(sym):
            side_cn = "开多" if (p.get("side") or "buy") == "buy" else "开空"
            if p.get("opened_ts"):
                evs.append({
                    "ts": float(p["opened_ts"]),
                    "kind": "open",
                    "symbol": p["symbol"],
                    "title": f"{side_cn} {p['symbol']}",
                    "detail": f"入场 {p.get('entry_price','—')} · 数量 {p.get('qty','—')}"
                              + (f" · 止损 {p['stop_loss']}" if p.get("stop_loss") else "")
                              + (f" · 止盈 {p['take_profit']}" if p.get("take_profit") else ""),
                    "tone": "buy",
                })
            if p.get("status") == "closed" and p.get("closed_ts"):
                pnl = p.get("realized_pnl_usdt")
                pnl_pct = p.get("realized_pnl_pct")
                reason = _EXIT_REASON_CN.get(p.get("exit_reason"), p.get("exit_reason") or "平仓")
                tone = "win" if (pnl or 0) > 0 else ("loss" if (pnl or 0) < 0 else "flat")
                pnl_str = ""
                if pnl is not None:
                    pnl_str = f"{'+' if pnl >= 0 else ''}{round(pnl, 2)}U"
                    if pnl_pct is not None:
                        pnl_str += f"（{'+' if pnl_pct >= 0 else ''}{round(pnl_pct, 2)}%）"
                evs.append({
                    "ts": float(p["closed_ts"]),
                    "kind": "close",
                    "symbol": p["symbol"],
                    "title": f"平仓 {p['symbol']} · {reason}",
                    "detail": f"出场 {p.get('exit_price','—')}"
                              + (f" · 盈亏 {pnl_str}" if pnl_str else ""),
                    "tone": tone,
                })
        evs.sort(key=lambda e: e["ts"], reverse=True)
        for e in evs:
            e["time"] = time.strftime("%m-%d %H:%M", time.localtime(e["ts"]))
        return {"events": evs[: max(1, min(int(limit), 200))]}

    return JSONResponse(_cached(f"events:{sym}:{limit}", 20, _calc))


def _answer_question(q: str, snap: dict, st: dict) -> str:
    """根据当前决策快照 + 模拟盘战绩，把问题用人话回答（无需外部 LLM）。"""
    d = (snap or {}).get("decision", {}) or {}
    fac = (snap or {}).get("factor_state", {}) or {}
    sym = (snap or {}).get("symbol", "该币")
    if "_error" in d or "_error" in fac:
        return f"现在 {sym} 取数暂时不可用（{d.get('_error') or fac.get('_error')}），稍后再问我一次。"

    score = d.get("conviction_score", 0) or 0
    pos = d.get("suggested_position_pct", 0) or 0
    price = fac.get("price")
    direction = d.get("direction", "中性观望")
    entry = d.get("entry_zone")
    sl = d.get("stop_loss")
    tp = d.get("take_profit_ref")
    days = d.get("time_stop_days")
    reasons = d.get("reasons", []) or []
    ql = (q or "").lower()

    def buy_line():
        if pos > 0:
            return (f"现在 {sym} 偏多（信心分 {score}），可以小仓试多，建议仓位约 {pos}%。"
                    f"入场区间 {entry}，进场前先把止损 {sl} 设好。")
        if score <= -0.6:
            return f"现在 {sym} 偏空（信心分 {score}），别追多，建议空仓观望，等信号转好再说。"
        return f"现在 {sym} 信号偏中性（信心分 {score}），建议先观望，不值得为了买而买。"

    # 关键词路由
    if any(k in q for k in ("止盈", "卖", "出货", "落袋", "什么时候卖")):
        if pos > 0 and tp:
            return f"{sym} 参考止盈在 {tp}（约 +8%），价格到了就可以分批落袋；最多持有 {days} 天，到期没走也建议离场。"
        return f"现在 {sym} 没有建议持仓，谈不上卖点；等出现偏多信号、开了仓再设止盈。"
    if any(k in q for k in ("止损", "风险", "亏", "守不住", "跌")):
        if pos > 0 and sl:
            return f"{sym} 硬止损设在 {sl}（约 -10%），跌破就无条件离场，别扛单。当前组合最大风险敞口约 {d.get('max_risk_pct','—')}%。"
        return f"现在 {sym} 没有建议持仓，先不用设止损；真要买，记住硬止损是纪律，不设不进场。"
    if any(k in q for k in ("仓位", "买多少", "投多少", "多少钱", "多少仓")):
        if pos > 0:
            return f"{sym} 建议仓位约 {pos}%（弱因子刻意保守）。入场 {entry}，止损 {sl}，止盈 {tp}。"
        return f"现在 {sym} 建议仓位 0%——信号不够强，空仓也是一种持仓。"
    if any(k in q for k in ("为什么", "原因", "理由", "凭什么", "依据")):
        rs = reasons[:3]
        body = "；".join(rs) if rs else "当前因子偏中性，没有特别强的方向依据。"
        return f"{sym} 之所以判 {direction}（信心分 {score}），主要因为：{body}。"
    if any(k in q for k in ("现价", "价格", "多少钱一个", "报价")):
        return f"{sym} 现价约 {price}。距历史高点回撤 {fac.get('drawdown_from_ath_pct','—')}%，30 日动量 {fac.get('momentum_30d_pct','—')}%。"
    if any(k in q for k in ("战绩", "胜率", "赚", "盈亏", "表现", "准不准")):
        if st and st.get("closed_trades"):
            return (f"模拟盘到目前：已平仓 {st['closed_trades']} 笔，胜率 {st.get('win_rate_pct','—')}%，"
                    f"盈亏比 {st.get('profit_factor','—')}，账户总权益 {st.get('equity_usdt','—')}U"
                    f"（较起始 {st.get('equity_change_pct','—')}%）。还在攒样本，多跑一阵更有说服力。")
        return "模拟盘还没有已平仓记录，先让它自动跟盘跑一阵子，再回来看胜率和盈亏比。"
    if any(k in q for k in ("买", "该买", "能买", "进场", "入场", "做多", "操作", "怎么办", "建议")):
        return buy_line()

    # 默认：给一份一句话总览
    base = buy_line()
    if pos > 0:
        base += f" 止盈 {tp}、止损 {sl}，最多持有 {days} 天。"
    return base


def _llm_config() -> dict | None:
    """从环境变量读取 LLM 配置；未配置返回 None（此时走规则问答兜底）。
    支持任意 OpenAI 兼容服务（OpenAI / DeepSeek / 国产中转等）：
      - JARVIS_LLM_API_KEY 或 OPENAI_API_KEY 或 DEEPSEEK_API_KEY
      - JARVIS_LLM_BASE_URL（DeepSeek 用 https://api.deepseek.com）
      - JARVIS_LLM_MODEL（DeepSeek 默认 deepseek-chat，OpenAI 默认 gpt-4o-mini）"""
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    key = os.environ.get("JARVIS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ds_key
    if not key:
        return None
    base = os.environ.get("JARVIS_LLM_BASE_URL")
    if not base:
        base = "https://api.deepseek.com" if ds_key else "https://api.openai.com/v1"
    base = base.rstrip("/")
    is_deepseek = "deepseek" in base.lower()
    model = os.environ.get("JARVIS_LLM_MODEL") or ("deepseek-chat" if is_deepseek else "gpt-4o-mini")
    return {"key": key, "base": base, "model": model}


def _llm_answer(question: str, snap: dict, st: dict, sym: str) -> str | None:
    """接入真实 LLM 回答问题；失败/未配置返回 None 让上层走规则兜底。"""
    cfg = _llm_config()
    if not cfg:
        return None
    d = (snap or {}).get("decision", {}) or {}
    fac = (snap or {}).get("factor_state", {}) or {}
    rd = (snap or {}).get("real_data", {}) or {}
    context = {
        "symbol": sym,
        "price": fac.get("price"),
        "conviction_score": d.get("conviction_score"),
        "direction": d.get("direction"),
        "suggested_position_pct": d.get("suggested_position_pct"),
        "entry_zone": d.get("entry_zone"),
        "stop_loss": d.get("stop_loss"),
        "take_profit_ref": d.get("take_profit_ref"),
        "time_stop_days": d.get("time_stop_days"),
        "reasons": d.get("reasons", []),
        "expected_value": d.get("expected_value"),
        "drawdown_from_ath_pct": fac.get("drawdown_from_ath_pct"),
        "momentum_30d_pct": fac.get("momentum_30d_pct"),
        "fear_greed": rd.get("fear_greed"),
        "funding": rd.get("funding"),
        "paper_stats": {
            "win_rate_pct": st.get("win_rate_pct"),
            "profit_factor": st.get("profit_factor"),
            "closed_trades": st.get("closed_trades"),
            "equity_usdt": st.get("equity_usdt"),
        } if st else {},
    }
    system = (
        "你是『贾维斯』加密交易助手，面向新手用户。只依据下面 JSON 给出的真实决策与数据回答，"
        "不得编造数据或价位。用简体中文、口语化、3 句以内说清：该不该买/仓位/止盈止损/理由。"
        "始终提醒这是模拟盘研究、不构成投资建议。若数据缺失就如实说明。"
    )
    payload = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"决策与数据(JSON)：{json.dumps(context, ensure_ascii=False)}\n\n用户问题：{question}"},
        ],
        "temperature": 0.4,
        "max_tokens": 400,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions", data=payload, method="POST",
        headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ans = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return ans or None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return None


@app.post("/api/ask")
def api_ask(symbol: str = "BTCUSDT", q: str = ""):
    """小白问答：优先接真实 LLM（按环境变量配置），失败/未配置则用规则问答兜底。"""
    sym = symbol.upper().replace("-", "").replace("/", "")
    spot = sym if sym.endswith("USDT") else sym + "USDT"
    question = (q or "").strip()
    if not question:
        return JSONResponse({"ok": False, "answer": "你想问点啥？比如「现在该买吗」「止盈止损在哪」「最近战绩怎样」。"})
    snap = _cached(f"snap:{spot}", 300, lambda: jb.build(spot))
    try:
        st = jpt.stats(_trader_cfg(), spot)
    except Exception:  # noqa: BLE001
        st = {}
    llm = _llm_answer(question, snap, st, spot)
    answer = llm if llm else _answer_question(question, snap, st)
    return JSONResponse({"ok": True, "symbol": spot, "question": question,
                         "answer": answer, "engine": "llm" if llm else "rule"})


# ─────────────────────────── 短线自动交易 API ───────────────────────────

try:
    import jarvis_scalper_trader as jst
    _HAS_SCALPER_TRADER = True
except ImportError:
    _HAS_SCALPER_TRADER = False

try:
    import jarvis_scalper_evolve as jse
    _HAS_SCALPER_EVOLVE = True
except ImportError:
    _HAS_SCALPER_EVOLVE = False

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

_SCALPER_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scalper_config.yaml")


@app.get("/api/scalper/status")
def api_scalper_status():
    if not _HAS_SCALPER_TRADER:
        return JSONResponse({"error": "jarvis_scalper_trader 模块未安装"}, status_code=503)
    try:
        report = jst.get_report()
        cfg = jst.load_config()
        best = None
        if _HAS_SCALPER_EVOLVE:
            best = jse.get_best_strategy()
        return JSONResponse({
            "running": False,
            "strategy": best.get("name") if best else None,
            "symbol": cfg.get("symbol", "BTCUSDT"),
            "timeframe": cfg.get("timeframe", "15m"),
            "report": report,
            "config": {
                "confidence_threshold": cfg.get("trading", {}).get("confidence_threshold", 0.6),
                "max_positions": cfg.get("risk", {}).get("max_concurrent_positions", 3),
                "aggressive_mode": cfg.get("trading", {}).get("aggressive_mode", False),
            },
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/scalper/report")
def api_scalper_report():
    if not _HAS_SCALPER_TRADER:
        return JSONResponse({"error": "jarvis_scalper_trader 模块未安装"}, status_code=503)
    try:
        return JSONResponse(jst.get_report())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/scalper/log")
def api_scalper_log(limit: int = 50):
    log_path = os.path.expanduser("~/.vibe-trading/jarvis_scalper_trader.log")
    if not os.path.exists(log_path):
        return JSONResponse({"lines": [], "total": 0})
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        lines = [l.rstrip() for l in lines if l.strip()]
        total = len(lines)
        return JSONResponse({"lines": lines[-min(limit, total):], "total": total})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/scalper/start")
def api_scalper_start(symbol: str = "BTCUSDT"):
    return JSONResponse({"ok": False, "reason": "短线交易需通过命令行启动: python jarvis_scalper_trader.py run --symbol " + symbol})


@app.post("/api/scalper/stop")
def api_scalper_stop():
    return JSONResponse({"ok": False, "reason": "短线交易需通过命令行停止（Ctrl+C）"})


# ─────────────────────────── 进化引擎 API ───────────────────────────

@app.get("/api/evolve/status")
def api_evolve_status():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"error": "jarvis_scalper_evolve 模块未安装"}, status_code=503)
    try:
        gy = jse.load_graveyard()
        hof = jse.load_hall_of_fame()
        best = jse.get_best_strategy()
        return JSONResponse({
            "graveyard_count": len(gy),
            "hall_of_fame_count": len(hof),
            "best_strategy": best.get("name") if best else None,
            "best_win_rate": best.get("win_rate_pct") if best else None,
            "best_profit_factor": best.get("profit_factor") if best else None,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/evolve/start")
def api_evolve_start(rounds: int = 10):
    return JSONResponse({"ok": False, "reason": f"进化引擎需通过命令行启动: python jarvis_scalper_evolve.py evolve --rounds {rounds}"})


@app.post("/api/evolve/stop")
def api_evolve_stop():
    return JSONResponse({"ok": False, "reason": "进化引擎需通过命令行停止（Ctrl+C）"})


@app.get("/api/evolve/graveyard")
def api_evolve_graveyard():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"error": "jarvis_scalper_evolve 模块未安装"}, status_code=503)
    try:
        return JSONResponse(jse.load_graveyard())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/evolve/hall-of-fame")
def api_evolve_hall_of_fame():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"error": "jarvis_scalper_evolve 模块未安装"}, status_code=503)
    try:
        return JSONResponse(jse.load_hall_of_fame())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─────────────────────────── 成长进度 API ───────────────────────────

@app.get("/api/growth/timeline")
def api_growth_timeline():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse([])
    try:
        gy = jse.load_graveyard()
        hof = jse.load_hall_of_fame()
        events = []
        for entry in gy:
            events.append({
                "time": entry.get("buried_at", ""),
                "event": f"策略「{entry.get('name', '?')}」失败入墓",
                "result": "fail",
                "detail": entry.get("failure_reason", "未达标"),
                "metrics": {
                    "win_rate": entry.get("win_rate_pct"),
                    "profit_factor": entry.get("profit_factor"),
                },
            })
        for entry in hof:
            events.append({
                "time": entry.get("promoted_at", ""),
                "event": f"策略「{entry.get('name', '?')}」达标入榜",
                "result": "success",
                "detail": f"胜率 {entry.get('win_rate_pct', '?')}%，盈亏比 {entry.get('profit_factor', '?')}",
                "metrics": {
                    "win_rate": entry.get("win_rate_pct"),
                    "profit_factor": entry.get("profit_factor"),
                },
            })
        events.sort(key=lambda e: e["time"], reverse=True)
        return JSONResponse(events)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/growth/milestones")
def api_growth_milestones():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse([])
    try:
        gy = jse.load_graveyard()
        hof = jse.load_hall_of_fame()
        milestones = []
        total_strategies = len(gy) + len(hof)
        if total_strategies > 0:
            milestones.append({"title": "第一个策略", "achieved": True, "detail": f"已尝试 {total_strategies} 个策略"})
        if len(hof) > 0:
            milestones.append({"title": "第一个达标策略", "achieved": True, "detail": f"名人堂已有 {len(hof)} 个策略"})
        else:
            milestones.append({"title": "第一个达标策略", "achieved": False, "detail": "仍在进化中..."})
        if _HAS_SCALPER_TRADER:
            report = jst.get_report()
            trades = report.get("total_trades", 0)
            if trades >= 10:
                milestones.append({"title": f"累计交易 {trades} 笔", "achieved": True, "detail": f"胜率 {report.get('win_rate_pct', 0)}%"})
            if report.get("win_rate_pct", 0) >= 55:
                milestones.append({"title": "胜率突破 55%", "achieved": True, "detail": f"当前胜率 {report['win_rate_pct']}%"})
        if total_strategies >= 10:
            milestones.append({"title": "试错 10 个策略", "achieved": True, "detail": f"已尝试 {total_strategies} 个"})
        if total_strategies >= 50:
            milestones.append({"title": "试错 50 个策略", "achieved": True, "detail": f"已尝试 {total_strategies} 个"})
        elif total_strategies >= 10:
            milestones.append({"title": "试错 50 个策略", "achieved": False, "detail": f"进度 {total_strategies}/50"})
        return JSONResponse(milestones)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/growth/stats")
def api_growth_stats():
    if not _HAS_SCALPER_EVOLVE:
        return JSONResponse({"dimensions": [], "values": []})
    try:
        gy = jse.load_graveyard()
        hof = jse.load_hall_of_fame()
        all_strategies = gy + hof
        total = len(all_strategies)

        trend_score = 50
        risk_score = 50
        timing_score = 50
        position_score = 50
        learning_score = 50

        if total > 0:
            win_rates = [s.get("win_rate_pct", 0) for s in all_strategies if s.get("win_rate_pct")]
            if win_rates:
                trend_score = min(100, max(0, int(sum(win_rates) / len(win_rates))))
            pfs = [s.get("profit_factor", 0) for s in all_strategies if s.get("profit_factor")]
            if pfs:
                risk_score = min(100, max(0, int(sum(pfs) / len(pfs) * 40)))
            if len(hof) > 0:
                timing_score = min(100, int(len(hof) / max(1, total) * 200))
            position_score = min(100, int(total * 3))
            learning_score = min(100, int((1 - len(gy) / max(1, total * 2)) * 100 + len(hof) * 20))

        dimensions = ["趋势识别", "风控能力", "择时精准", "仓位管理", "学习速度"]
        values = [trend_score, risk_score, timing_score, position_score, learning_score]

        failure_reasons = {}
        for s in gy:
            reason = s.get("failure_reason", "未知")
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

        return JSONResponse({
            "dimensions": dimensions,
            "values": values,
            "total_strategies": total,
            "success_count": len(hof),
            "failure_count": len(gy),
            "failure_reasons": failure_reasons,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─────────────────────────── 配置 API ───────────────────────────

@app.get("/api/config")
def api_config_get():
    if _HAS_YAML and os.path.exists(_SCALPER_CONFIG_PATH):
        try:
            with open(_SCALPER_CONFIG_PATH, encoding="utf-8") as f:
                cfg = _yaml.safe_load(f)
            return JSONResponse(cfg or {})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({})


@app.put("/api/config")
def api_config_put(data: dict):
    if not _HAS_YAML:
        return JSONResponse({"ok": False, "reason": "PyYAML 未安装"}, status_code=503)
    try:
        with open(_SCALPER_CONFIG_PATH, "w", encoding="utf-8") as f:
            _yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)


# ─────────────────────────── 市场概览 API ───────────────────────────

@app.get("/api/market/overview")
def api_market_overview():
    try:
        snap = _cached("snap:BTCUSDT", 300, lambda: jb.build("BTCUSDT"))
        rd = snap.get("real_data", {}) or {}
        fac = snap.get("factor_state", {}) or {}
        return JSONResponse({
            "btc_price": fac.get("price"),
            "fear_greed": rd.get("fear_greed", {}),
            "funding": rd.get("funding", {}),
            "market_structure": rd.get("market_structure", {}),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


@app.get("/lite", response_class=HTMLResponse)
def lite():
    """小白模式单页：只看结论 + 操作 + K线信号 + 模拟战绩，自动刷新。"""
    return LITE_HTML


@app.get("/cockpit", response_class=HTMLResponse)
def cockpit():
    """小白驾驶舱：左 K 线(画线提示点位) + 右 AI 面板/事件流/问答，自动跟盘。"""
    return COCKPIT_HTML


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>贾维斯 JARVIS · 加密决策仪表盘</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script src="https://s3.tradingview.com/tv.js"></script>
<style>
  :root{--bg:#0b0e14;--card:#141a24;--bd:#222c3a;--fg:#e6edf3;--mut:#8b98a9;--up:#16c784;--down:#ea3943;--accent:#3b82f6;--warn:#f0b90b;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--fg);font-family:-apple-system,"PingFang SC","Microsoft YaHei",Inter,sans-serif;padding:20px;}
  h1{font-size:20px;font-weight:700;display:flex;align-items:center;gap:10px}
  .sub{color:var(--mut);font-size:12px;margin-top:4px}
  .bar{display:flex;gap:10px;align-items:center;margin:16px 0;flex-wrap:wrap}
  select,button{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:8px 12px;font-size:14px;cursor:pointer}
  .inp{background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:8px;font-size:14px;width:110px}
  button.primary{background:var(--accent);border-color:var(--accent)}
  button:hover{filter:brightness(1.15)}
  .grid{display:grid;gap:14px}
  .cards{grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px}
  .card .k{color:var(--mut);font-size:12px}
  .card .v{font-size:22px;font-weight:700;margin-top:6px}
  .card .v small{font-size:12px;color:var(--mut);font-weight:400}
  .decision{display:grid;grid-template-columns:200px 1fr;gap:18px;margin-bottom:14px}
  .gauge{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:6px}
  .plan{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px}
  .plan h3{font-size:15px;margin-bottom:10px}
  .pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:13px;font-weight:600}
  .reasons{margin-top:10px;color:var(--mut);font-size:13px;line-height:1.9}
  .charts{grid-template-columns:1fr 1fr}
  .chart{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;height:300px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--bd)}
  th:first-child,td:first-child{text-align:left}
  .pos{color:var(--up)} .neg{color:var(--down)} .warnc{color:var(--warn)}
  .sec{margin:18px 0 8px;font-size:15px;font-weight:600}
  .loading{color:var(--mut)}
  .foot{color:var(--mut);font-size:11px;margin-top:18px;line-height:1.7}
  @media(max-width:820px){.charts{grid-template-columns:1fr}.decision{grid-template-columns:1fr}}
</style>
</head>
<body>
  <h1>🤖 贾维斯 JARVIS · 加密决策仪表盘</h1>
  <div class="sub">真实数据（Binance/CoinGecko/alternative.me/链上） · 因子经 P3 回测 + P4 样本外验证 · 仅研究不构成交易建议</div>

  <div class="bar">
    <select id="symbol">
      <option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>BNBUSDT</option>
    </select>
    <button class="primary" onclick="loadAll()">刷新</button>
    <span id="status" class="loading"></span>
  </div>

  <div class="decision">
    <div class="gauge" id="gauge"></div>
    <div class="plan" id="plan"><div class="loading">加载中…</div></div>
  </div>

  <div class="sec">因子归因（本次信心分由哪些因子贡献 · 绿加分 / 红减分）</div>
  <div class="card" style="height:240px"><div id="cAttr" style="height:100%"></div></div>

  <div class="sec">实时行情（TradingView · 免登录 · 多周期 K 线）</div>
  <div class="card" style="padding:0;overflow:hidden">
    <div id="tvchart" style="height:480px;width:100%"></div>
  </div>

  <div class="sec" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    贾维斯 K 线（含决策信号叠加：入场区间 / 硬止损 / 参考止盈）
    <span id="klineIv" style="display:flex;gap:6px">
      <button data-iv="15m" onclick="setIv('15m')">15m</button>
      <button data-iv="1h" class="primary" onclick="setIv('1h')">1h</button>
      <button data-iv="4h" onclick="setIv('4h')">4h</button>
      <button data-iv="1d" onclick="setIv('1d')">1d</button>
    </span>
    <span id="klineStatus" class="loading" style="font-size:12px"></span>
  </div>
  <div class="card" style="height:420px"><div id="cKline" style="height:100%"></div></div>

  <div class="sec">真实数据快照</div>
  <div class="grid cards" id="cards"></div>

  <div class="sec">历史图表</div>
  <div class="grid charts">
    <div class="chart" id="chartPrice"></div>
    <div class="chart" id="chartFng"></div>
  </div>

  <div class="sec">已验证因子（事件研究：买入极度恐惧后前瞻收益 vs 基线）</div>
  <div class="card"><div id="factorTable"><div class="loading">加载中…</div></div></div>

  <div class="sec" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    置信度校准（贾维斯有多大把握 vs 实际兑现 · Brier 自我评分）
    <span id="calibH" style="display:flex;gap:6px">
      <button data-h="7" onclick="setCalibH(7)">7天</button>
      <button data-h="30" class="primary" onclick="setCalibH(30)">30天</button>
    </span>
    <span id="calibStatus" class="loading" style="font-size:12px"></span>
  </div>
  <div class="grid charts">
    <div class="chart" id="cCalib"></div>
    <div class="card" style="height:300px;overflow:auto"><div id="calibStat"><div class="loading">加载中…</div></div></div>
  </div>

  <div class="sec" style="display:flex;align-items:center;gap:12px">
    历史战绩（贾维斯真实前向准确率追踪）
    <button onclick="recordToday()" style="font-size:12px;padding:5px 10px">记录今日决策</button>
    <span id="trackStatus" class="loading" style="font-size:12px"></span>
  </div>
  <div class="grid charts">
    <div class="chart" id="chartTrack"></div>
    <div class="card" style="height:300px;overflow:auto"><div id="trackStat"><div class="loading">加载中…</div></div></div>
  </div>
  <div class="card" style="margin-top:14px"><div id="trackTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">💰 模拟交易台账（钱包余额 · 限价挂单 · 买卖成交记录）</div>
  <div class="grid cards" id="walletCards"><div class="loading">加载中…</div></div>

  <div class="card" style="margin-top:12px">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <strong style="font-size:13px">挂限价单（自己指定点位）：</strong>
      <select id="loSym"><option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>BNBUSDT</option></select>
      <select id="loSide"><option value="buy">买入</option><option value="sell">卖出</option></select>
      <input id="loPrice" placeholder="限价" class="inp"/>
      <input id="loQty" placeholder="数量" class="inp"/>
      <input id="loSL" placeholder="止损(可选)" class="inp"/>
      <input id="loTP" placeholder="止盈(可选)" class="inp"/>
      <button class="primary" onclick="placeOrder()">挂单</button>
      <button onclick="matchOrders()">立即撮合</button>
      <span style="border-left:1px solid var(--bd);height:24px"></span>
      <input id="depAmt" placeholder="入金额" class="inp"/>
      <button onclick="deposit()">入金</button>
      <span style="border-left:1px solid var(--bd);height:24px"></span>
      <input id="cycSyms" placeholder="跟盘币种 BTC,ETH" class="inp" style="width:150px" value="BTC,ETH,SOL"/>
      <button class="primary" onclick="runCycle()">跑一轮自动跟盘</button>
      <button onclick="loadTrader()">刷新台账</button>
      <span id="loStatus" class="loading" style="font-size:12px"></span>
    </div>
    <div class="k" style="margin-top:8px">买单到价（现价≤限价）才成交并冻结资金；点「立即撮合」用当前现价撮合。「跑一轮自动跟盘」按贾维斯决策自动撮合+盯平仓+找开仓（需出网取价，约 10-30 秒）。</div>
  </div>

  <div class="sec">挂单簿（pending）</div>
  <div class="card"><div id="ordersTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">当前持仓</div>
  <div class="card"><div id="positionsTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">已平仓历史（战绩复盘）</div>
  <div class="card"><div id="closedStat" class="k" style="margin-bottom:8px"></div><div id="closedTable"><div class="loading">加载中…</div></div></div>

  <div class="sec">买卖成交记录（钱包资金流水）</div>
  <div class="card"><div id="ledgerTable"><div class="loading">加载中…</div></div></div>

  <div class="foot" id="foot"></div>

<script>
const $ = id => document.getElementById(id);
let gauge, cPrice, cFng, cTrack, cKline, cCalib, cAttr;
let tvWidget = null, tvSymbol = null;
let klineIv = '1h';
let lastDecision = null;  // 缓存最近一次决策，供 K 线叠加信号
let lastTraderStat = null;  // 缓存最近一次跟盘账户总览，供持仓表复用现价/浮盈

function pct(x){return (x>0?'+':'')+x+'%';}
function cls(x){return x>0?'pos':(x<0?'neg':'');}

function renderTV(sym){
  // 免登录嵌入 TradingView 高级图表（实时 K 线、多周期），数据走 Binance 同源
  if(typeof TradingView==='undefined'){ return; }
  if(tvSymbol===sym && tvWidget) return;  // 同币种不重复创建
  tvSymbol = sym;
  $('tvchart').innerHTML = '';
  tvWidget = new TradingView.widget({
    container_id: 'tvchart',
    symbol: 'BINANCE:'+sym,
    interval: '60',
    timezone: 'Asia/Shanghai',
    theme: 'dark',
    style: '1',
    locale: 'zh_CN',
    autosize: true,
    enable_publishing: false,
    hide_side_toolbar: false,
    allow_symbol_change: true,
    studies: ['Volume@tv-basicstudies'],
    backgroundColor: '#141a24',
    gridColor: '#222c3a'
  });
}

function setIv(iv){
  klineIv = iv;
  document.querySelectorAll('#klineIv button').forEach(b=>{
    b.className = (b.getAttribute('data-iv')===iv) ? 'primary' : '';
  });
  loadKline($('symbol').value);
}

async function loadKline(sym){
  $('klineStatus').textContent = '拉取 K 线…';
  try{
    const d = await (await fetch('/api/kline?symbol='+sym+'&interval='+klineIv+'&limit=200')).json();
    if(d.error){ $('klineStatus').textContent='K线失败: '+d.error; return; }
    renderKline(d);
    $('klineStatus').textContent = d.interval+' · '+d.rows.length+' 根';
  }catch(e){ $('klineStatus').textContent='K线失败: '+e; }
}

function renderKline(d){
  if(!cKline) cKline=echarts.init($('cKline'));
  const rows=d.rows||[];
  const dates=rows.map(r=>r.t);
  const ohlc=rows.map(r=>[r.o,r.c,r.l,r.h]);  // echarts: [open,close,low,high]
  const vol=rows.map(r=>r.v);
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  // 叠加决策信号线
  const ml=[];
  const dec=lastDecision||{};
  if(dec.suggested_position_pct>0){
    if(dec.stop_loss) ml.push({yAxis:dec.stop_loss,name:'硬止损',lineStyle:{color:'#ea3943'},label:{formatter:'止损 '+dec.stop_loss,color:'#ea3943',position:'insideEndTop'}});
    if(dec.take_profit_ref) ml.push({yAxis:dec.take_profit_ref,name:'参考止盈',lineStyle:{color:'#16c784'},label:{formatter:'止盈 '+dec.take_profit_ref,color:'#16c784',position:'insideEndTop'}});
    if(dec.entry_zone){const parts=(''+dec.entry_zone).split('~').map(s=>parseFloat(s.trim())).filter(x=>!isNaN(x));
      parts.forEach((p,i)=>ml.push({yAxis:p,name:'入场',lineStyle:{color:'#3b82f6',type:'dashed'},label:{formatter:'入场 '+p,color:'#3b82f6',position:'insideEndTop'}}));}
  }
  cKline.setOption({
    tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
    legend:{data:['K线','成交量'],textStyle:{color:'#8b98a9'},top:0},
    grid:[{left:55,right:20,top:30,height:'62%'},{left:55,right:20,top:'74%',height:'16%'}],
    xAxis:[{type:'category',data:dates,...ax,axisLabel:{color:'#8b98a9'},boundaryGap:true},
           {type:'category',gridIndex:1,data:dates,axisLabel:{show:false},axisLine:{show:false}}],
    yAxis:[{scale:true,...ax,axisLabel:{color:'#8b98a9'}},
           {gridIndex:1,...ax,axisLabel:{show:false},splitLine:{show:false}}],
    dataZoom:[{type:'inside',xAxisIndex:[0,1],start:55,end:100},{type:'slider',xAxisIndex:[0,1],bottom:0,height:14,start:55,end:100,textStyle:{color:'#8b98a9'}}],
    series:[
      {name:'K线',type:'candlestick',data:ohlc,itemStyle:{color:'#16c784',color0:'#ea3943',borderColor:'#16c784',borderColor0:'#ea3943'},
       markLine:{symbol:'none',data:ml,silent:true}},
      {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:vol,itemStyle:{color:'#3b82f680'}}
    ]
  });
}

async function loadAll(){
  const sym = $('symbol').value;
  renderTV(sym);
  loadKline(sym);
  $('status').textContent = '拉取真实数据中（约 10-20 秒）…';
  try{
    const snap = await (await fetch('/api/snapshot?symbol='+sym)).json();
    renderDecision(snap); renderCards(snap);
    $('status').textContent='更新于 '+snap.generated_at_utc+' UTC';
  }catch(e){ $('status').textContent='快照失败: '+e; }

  fetch('/api/series?symbol='+sym+'&days=365').then(r=>r.json()).then(renderCharts).catch(()=>{});
  fetch('/api/factor?symbol='+sym).then(r=>r.json()).then(renderFactor).catch(()=>{});
  fetch('/api/track?symbol='+sym).then(r=>r.json()).then(renderTrack).catch(()=>{});
  loadCalib(sym);
  loadTrader();
}

// ───────── 模拟交易台账 ─────────
async function loadTrader(){
  try{ lastTraderStat = await (await fetch('/api/trader/status')).json(); renderWallet(lastTraderStat); }catch(e){ lastTraderStat=null; }
  fetch('/api/orders').then(r=>r.json()).then(renderOrders).catch(()=>{});
  fetch('/api/positions?status=open').then(r=>r.json()).then(renderPositions).catch(()=>{});
  fetch('/api/positions?status=closed').then(r=>r.json()).then(renderClosed).catch(()=>{});
  fetch('/api/ledger?limit=40').then(r=>r.json()).then(renderLedger).catch(()=>{});
}

function renderWallet(st){
  const eqc=st.equity_change_pct;
  const cards=[
    ['总权益', (st.equity_usdt??'—')+' U', '较起始 '+(eqc==null?'—':pct(eqc))],
    ['可用现金', (st.cash_usdt??'—')+' U', '起始入金 '+(st.start_equity_usdt??'—')+'U'],
    ['挂单冻结', (st.frozen_usdt??'—')+' U', ''],
    ['持仓市值', (st.holdings_value_usdt??'—')+' U', (st.open_positions||0)+' 个持仓'],
    ['已实现盈亏', (st.realized_pnl_usdt??'—')+' U', '浮动 '+(st.unrealized_pnl_usdt??'—')+'U'],
    ['盈亏比', st.profit_factor??'—', '胜率 '+(st.win_rate_pct==null?'—':st.win_rate_pct+'%')],
  ];
  $('walletCards').innerHTML=cards.map(c=>`<div class="card"><div class="k">${c[0]}</div><div class="v">${c[1]}</div><div class="k" style="margin-top:4px">${c[2]}</div></div>`).join('');
}

function renderOrders(rows){
  if(!rows||!rows.length){ $('ordersTable').innerHTML='<div class="loading">暂无挂单</div>'; return; }
  const tr=rows.map(o=>`<tr><td>#${o.id}</td><td>${o.symbol}</td><td class="${o.side==='buy'?'pos':'neg'}">${o.side==='buy'?'买':'卖'}</td><td>${o.limit_price}</td><td>${o.qty}</td><td>${o.notional_usdt??'—'}</td><td>${o.created_date||''}</td><td><button onclick="cancelOrder(${o.id})" style="padding:3px 8px;font-size:12px">撤单</button></td></tr>`).join('');
  $('ordersTable').innerHTML=`<table><thead><tr><th>单号</th><th>币种</th><th>方向</th><th>限价</th><th>数量</th><th>冻结U</th><th>挂单日</th><th></th></tr></thead><tbody>${tr}</tbody></table>`;
}

function renderPositions(rows){
  if(!rows||!rows.length){ $('positionsTable').innerHTML='<div class="loading">暂无持仓</div>'; return; }
  const det={}; ((lastTraderStat&&lastTraderStat.open_detail)||[]).forEach(d=>det[d.id]=d);
  const tr=rows.map(p=>{
    const d=det[p.id]||{};
    const cur=(d.cur_price!=null)?d.cur_price:'—';
    const up=d.unrealized_usdt;
    const upPct=(d.cur_price!=null&&p.entry_price)?+(((d.cur_price/p.entry_price)-1)*100).toFixed(2):null;
    const upTxt=(up==null)?'—':((up>0?'+':'')+up+'U'+(upPct==null?'':` (${upPct>0?'+':''}${upPct}%)`));
    let held=null; if(p.entry_date){const t=new Date(p.entry_date+'T00:00:00'); if(!isNaN(t)) held=Math.floor((Date.now()-t.getTime())/86400000);}
    const ts=p.time_stop_days;
    const heldTxt=(held==null)?'—':`${held}/${ts??'—'} 天`;
    const heldCls=(held!=null&&ts&&held>=ts*0.8)?'neg':'';
    return `<tr><td>#${p.id}</td><td>${p.symbol}</td><td>${p.entry_price}</td><td>${cur}</td><td>${p.qty}</td><td class="${cls(up||0)}">${upTxt}</td><td class="neg">${p.stop_loss??'—'}</td><td class="pos">${p.take_profit??'—'}</td><td>${p.entry_date||''}</td><td class="${heldCls}">${heldTxt}</td><td><button onclick="closePos('${p.symbol}')" style="padding:3px 8px;font-size:12px">平仓</button></td></tr>`;
  }).join('');
  $('positionsTable').innerHTML=`<table><thead><tr><th>持仓</th><th>币种</th><th>入场价</th><th>现价</th><th>数量</th><th>浮动盈亏</th><th>止损</th><th>止盈</th><th>开仓日</th><th>持仓(天)</th><th></th></tr></thead><tbody>${tr}</tbody></table>`;
}

function renderClosed(rows){
  const st=lastTraderStat||{};
  $('closedStat').innerHTML=`已平仓 <b>${st.closed_trades??0}</b> 笔 · 胜率 <b>${st.win_rate_pct==null?'—':st.win_rate_pct+'%'}</b>（${st.wins??0} 胜 / ${st.losses??0} 负）· 盈亏比 <b>${st.profit_factor??'—'}</b> · 平均盈 <span class="pos">${st.avg_win_usdt??'—'}U</span> / 平均亏 <span class="neg">${st.avg_loss_usdt??'—'}U</span> · 已实现 <b class="${cls(st.realized_pnl_usdt||0)}">${st.realized_pnl_usdt??'—'}U</b>`;
  if(!rows||!rows.length){ $('closedTable').innerHTML='<div class="loading">暂无已平仓记录</div>'; return; }
  const rmap={stop:'止损',take:'止盈',time:'到期',signal:'反转',manual:'手动'};
  const tr=rows.map(p=>`<tr><td>#${p.id}</td><td>${p.symbol}</td><td>${p.entry_price}</td><td>${p.exit_price??'—'}</td><td>${p.qty}</td><td>${rmap[p.exit_reason]||p.exit_reason||'—'}</td><td class="${cls(p.realized_pnl_usdt||0)}">${p.realized_pnl_usdt==null?'—':((p.realized_pnl_usdt>0?'+':'')+p.realized_pnl_usdt+'U')}</td><td class="${cls(p.realized_pnl_pct||0)}">${p.realized_pnl_pct==null?'—':pct(p.realized_pnl_pct)}</td><td>${p.exit_date||''}</td></tr>`).join('');
  $('closedTable').innerHTML=`<table><thead><tr><th>持仓</th><th>币种</th><th>入场价</th><th>出场价</th><th>数量</th><th>平仓原因</th><th>盈亏U</th><th>盈亏%</th><th>平仓日</th></tr></thead><tbody>${tr}</tbody></table>`;
}

function renderLedger(rows){
  if(!rows||!rows.length){ $('ledgerTable').innerHTML='<div class="loading">暂无成交记录</div>'; return; }
  const map={deposit:'入金',buy:'买入',sell:'卖出',freeze:'冻结',unfreeze:'解冻'};
  const tr=rows.map(r=>`<tr><td>${r.dt||''}</td><td>${map[r.type]||r.type}</td><td>${r.symbol||'-'}</td><td class="${cls(r.amount_usdt)}">${(r.amount_usdt>0?'+':'')+r.amount_usdt}</td><td>${r.cash_after??'—'}</td><td>${r.note||''}</td></tr>`).join('');
  $('ledgerTable').innerHTML=`<table><thead><tr><th>时间</th><th>类型</th><th>币种</th><th>金额U</th><th>现金余额U</th><th>备注</th></tr></thead><tbody>${tr}</tbody></table>`;
}

async function placeOrder(){
  const sym=$('loSym').value, side=$('loSide').value, price=$('loPrice').value, qty=$('loQty').value, sl=$('loSL').value, tp=$('loTP').value;
  if(!price||!qty){ $('loStatus').textContent='请填写限价和数量'; return; }
  let url=`/api/orders/place?symbol=${sym}&side=${side}&price=${price}&qty=${qty}`;
  if(sl) url+=`&stop_loss=${sl}`;
  if(tp) url+=`&take_profit=${tp}`;
  $('loStatus').textContent='提交中…';
  try{
    const r=await (await fetch(url,{method:'POST'})).json();
    $('loStatus').textContent=r.ok?`✅ 挂单 #${r.order_id} 成功`:`❌ ${r.reason}`;
    if(r.ok){ $('loPrice').value=''; $('loQty').value=''; $('loSL').value=''; $('loTP').value=''; }
  }catch(e){ $('loStatus').textContent='挂单失败: '+e; }
  loadTrader();
}

async function cancelOrder(id){ await fetch('/api/orders/cancel?order_id='+id,{method:'POST'}); loadTrader(); }

async function matchOrders(){
  $('loStatus').textContent='撮合中（取现价）…';
  try{ const r=await (await fetch('/api/orders/match',{method:'POST'})).json();
    $('loStatus').textContent=`撮合完成：成交 ${(r.matched||[]).length} 笔`;
  }catch(e){ $('loStatus').textContent='撮合失败: '+e; }
  loadTrader();
}

async function runCycle(){
  const syms=($('cycSyms').value||'BTC,ETH,SOL').trim();
  $('loStatus').textContent='自动跟盘中（拉决策+取价，约 10-30 秒）…';
  try{
    const r=await (await fetch('/api/trader/cycle?symbols='+encodeURIComponent(syms),{method:'POST'})).json();
    const m=(r.matched||[]).length, c=(r.closed||[]).length, o=(r.opened||[]).length;
    $('loStatus').textContent=`跟盘完成：撮合 ${m} / 平仓 ${c} / 开仓 ${o} / 当前持仓 ${r.open_after??'—'}`;
  }catch(e){ $('loStatus').textContent='跟盘失败: '+e; }
  loadTrader();
}

async function deposit(){
  const amt=$('depAmt').value;
  if(!amt){ $('loStatus').textContent='请填写入金额'; return; }
  await fetch('/api/wallet/deposit?amount='+amt,{method:'POST'});
  $('depAmt').value=''; loadTrader();
}

async function closePos(sym){
  if(!confirm(sym+' 确认按现价平仓？')) return;
  await fetch('/api/positions/close?symbol='+sym,{method:'POST'});
  loadTrader();
}

let calibH = 30;
function setCalibH(h){
  calibH = h;
  document.querySelectorAll('#calibH button').forEach(b=>{
    b.className = (parseInt(b.getAttribute('data-h'))===h) ? 'primary' : '';
  });
  loadCalib($('symbol').value);
}

async function loadCalib(sym){
  $('calibStatus').textContent = '计算校准…';
  try{
    const c = await (await fetch('/api/calibration?symbol='+sym+'&horizon='+calibH)).json();
    renderCalib(c);
    $('calibStatus').textContent = c.n? ('样本 '+c.n+' 条') : '';
  }catch(e){ $('calibStatus').textContent='校准失败: '+e; }
}

function renderCalib(c){
  if(!cCalib) cCalib=echarts.init($('cCalib'));
  const bk=c.buckets||[];
  // 可靠性曲线：x=预测兑现率, y=实际兑现率；对角线=完美校准
  const pts=bk.map(b=>[b.pred_pct,b.actual_pct,b.n]);
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  cCalib.setOption({
    title:{text:'可靠性曲线（贴对角线=越准）',textStyle:{color:'#e6edf3',fontSize:13}},
    tooltip:{formatter:p=>p.seriesName==='理想'?'完美校准线':`预测兑现 ${p.data[0]}%<br>实际兑现 ${p.data[1]}%<br>样本 ${p.data[2]} 条`},
    grid:{left:50,right:25,top:50,bottom:40},
    xAxis:{type:'value',name:'贾维斯预测兑现率%',min:40,max:100,...ax,axisLabel:{color:'#8b98a9'}},
    yAxis:{type:'value',name:'实际兑现率%',min:0,max:100,...ax,axisLabel:{color:'#8b98a9'}},
    series:[
      {name:'理想',type:'line',data:[[50,50],[100,100]],showSymbol:false,lineStyle:{color:'#8b98a9',type:'dashed'},silent:true},
      {name:'贾维斯',type:'scatter',data:pts,symbolSize:p=>Math.min(40,12+p[2]),
       itemStyle:{color:'#3b82f6'},label:{show:true,position:'top',color:'#8b98a9',formatter:p=>'n='+p.data[2]}}
    ]
  });
  // 统计卡
  if(!c.n){ $('calibStat').innerHTML='<div class="loading">暂无可评估的方向性决策。先 backfill/record + evaluate 积累样本。</div>'; return; }
  const bssTxt = c.bss==null?'—':(c.bss>0?`<span class="pos">+${c.bss}</span>（比瞎猜强）`:`<span class="neg">${c.bss}</span>（不如瞎猜）`);
  let st=`<div style="font-size:13px;line-height:2">`+
    `${c.horizon}天前瞻 · 方向性样本 <b>${c.n}</b> 条 · 总体兑现率 <b>${c.overall_hit_pct}%</b><br>`+
    `Brier 分 <b>${c.brier}</b> <small>(越低越好，基线 ${c.brier_baseline})</small><br>`+
    `Brier 技巧分 BSS <b>${bssTxt}</b><br><br>`+
    `<b>分桶（预测 vs 实际）：</b>`;
  st+=`<table style="margin-top:6px"><thead><tr><th>把握度</th><th>样本</th><th>预测</th><th>实际</th></tr></thead><tbody>`;
  for(const b of bk){
    const gap=b.actual_pct-b.pred_pct;
    st+=`<tr><td>${b.label}</td><td>${b.n}</td><td>${b.pred_pct}%</td><td class="${cls(gap)}">${b.actual_pct}%</td></tr>`;
  }
  st+=`</tbody></table>`;
  st+=`<div class="foot" style="margin-top:10px">注：把握度由信心分映射（|score| 越大越自信）。实际低于预测=过度自信，高于预测=偏保守。这是贾维斯的「自我意识」体检。</div></div>`;
  $('calibStat').innerHTML=st;
}

async function recordToday(){
  const sym = $('symbol').value;
  $('trackStatus').textContent = '记录中…';
  try{
    const r = await (await fetch('/api/track/record?symbol='+sym,{method:'POST'})).json();
    const rec = r.record||{};
    $('trackStatus').textContent = rec.ok ? ('已记录 '+rec.as_of_date+'，并回填 '+(r.evaluate?.outcomes_filled??0)+' 条结果') : ('失败: '+(rec.error||''));
    fetch('/api/track?symbol='+sym).then(x=>x.json()).then(renderTrack);
  }catch(e){ $('trackStatus').textContent='失败: '+e; }
}

function renderTrack(t){
  const rep = t.report||{}, recent = t.recent||[];
  const bh = rep.by_horizon||{};
  // 柱状图：各前瞻 偏多 vs 中性 平均收益
  if(!cTrack) cTrack=echarts.init($('chartTrack'));
  const horizons = Object.keys(bh);
  const dirs = ['偏多（战术）','中性观望'];
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  const series = dirs.map(d=>({name:d,type:'bar',
    data:horizons.map(h=>{const a=(bh[h].by_direction||{})[d]; return a&&a.n?a.avg_ret_pct:0;}),
    label:{show:true,position:'top',color:'#8b98a9',formatter:'{c}%'}}));
  cTrack.setOption({title:{text:'平均前向收益：偏多信号 vs 中性基线',textStyle:{color:'#e6edf3',fontSize:13}},
    tooltip:{trigger:'axis'},legend:{textStyle:{color:'#8b98a9'},top:22},grid:{left:45,right:20,top:55,bottom:30},
    xAxis:{type:'category',data:horizons,...ax,axisLabel:{color:'#8b98a9'}},
    yAxis:{type:'value',name:'收益%',...ax,axisLabel:{color:'#8b98a9'}},
    color:['#16c784','#8b98a9'], series});
  // 统计卡
  let st=`<div style="font-size:13px;line-height:2">累计快照 <b>${rep.total_snapshots??0}</b> 条 · 已评估 <b>${rep.evaluated_outcomes??0}</b> 条<br>`;
  for(const h in bh){const ov=bh[h].overall; if(!ov.n)continue;
    const bull=(bh[h].by_direction||{})['偏多（战术）'];
    st+=`<div style="margin-top:8px"><b>${h}前瞻</b> 全体均值 <span class="${cls(ov.avg_ret_pct)}">${pct(ov.avg_ret_pct)}</span>`;
    if(bull&&bull.n) st+=` · 偏多 <span class="${cls(bull.avg_ret_pct)}">${pct(bull.avg_ret_pct)}</span>（n=${bull.n}，命中 ${bull.hit_rate_pct??'-'}%）`;
    st+=`</div>`;}
  st+=`</div>`;
  $('trackStat').innerHTML = rep.total_snapshots? st : '<div class="loading">还没有快照。点上方「记录今日决策」开始积累，或命令行跑 backfill 立刻出历史战绩。</div>';
  // 最近表
  if(!recent.length){ $('trackTable').innerHTML='<div class="loading">暂无快照</div>'; return; }
  const fmt=(r,ok)=> r==null?'<span class="loading">待评估</span>':`<span class="${cls(r)}">${pct(r)}</span> ${ok===1?'✅':(ok===0?'❌':'·')}`;
  let rows='';
  for(const r of recent){
    rows+=`<tr><td>${r.as_of_date}</td><td>${r.price}</td><td>${r.conviction_score}</td><td>${r.direction}</td><td>${r.position_pct}%</td><td>${fmt(r.r7,r.c7)}</td><td>${fmt(r.r30,r.c30)}</td></tr>`;
  }
  $('trackTable').innerHTML=`<table><thead><tr><th>日期</th><th>价格</th><th>信心</th><th>方向</th><th>仓位</th><th>7天</th><th>30天</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderAttr(d){
  if(!cAttr) cAttr=echarts.init($('cAttr'));
  const attr=(d.attribution||[]).slice().sort((a,b)=>a.contribution-b.contribution);
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  if(!attr.length){
    cAttr.setOption({title:{text:'本次无显式因子贡献（中性）',textStyle:{color:'#8b98a9',fontSize:13},left:'center',top:'center'}},true);
    return;
  }
  cAttr.setOption({
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:p=>{const it=p[0];const a=attr[it.dataIndex];return `<b>${a.factor}</b>: ${a.contribution>0?'+':''}${a.contribution}<br><span style="color:#8b98a9">${a.note}</span>`;}},
    grid:{left:120,right:40,top:20,bottom:25},
    xAxis:{type:'value',...ax,axisLabel:{color:'#8b98a9'},name:'对信心分的贡献'},
    yAxis:{type:'category',data:attr.map(a=>a.factor),...ax,axisLabel:{color:'#e6edf3'}},
    series:[{type:'bar',data:attr.map(a=>({value:a.contribution,itemStyle:{color:a.contribution>=0?'#16c784':'#ea3943'}})),
      label:{show:true,position:'right',color:'#8b98a9',formatter:p=>(p.value>0?'+':'')+p.value}}]
  },true);
}

function renderDecision(s){
  const d = s.decision||{}; const fac=s.factor_state||{};
  lastDecision = d;
  renderAttr(d);
  if(cKline) loadKline($('symbol').value);  // 决策更新后重绘 K 线以叠加信号
  const score = d.conviction_score ?? 0;
  if(!gauge) gauge = echarts.init($('gauge'));
  gauge.setOption({
    series:[{type:'gauge',min:-2,max:2,splitNumber:4,radius:'92%',
      axisLine:{lineStyle:{width:14,color:[[0.4,'#ea3943'],[0.6,'#8b98a9'],[1,'#16c784']]}},
      pointer:{width:5}, progress:{show:false},
      axisLabel:{distance:-6,fontSize:9,color:'#8b98a9'}, axisTick:{show:false}, splitLine:{length:10},
      detail:{valueAnimation:true,fontSize:26,offsetCenter:[0,'58%'],formatter:'{value}',color:'#e6edf3'},
      title:{offsetCenter:[0,'82%'],fontSize:12,color:'#8b98a9'},
      data:[{value:score,name:'信心分'}]}]
  });
  const dir = d.direction||'-';
  const color = score>=0.8?'#16c784':(score<=-0.8?'#ea3943':'#8b98a9');
  let html = `<h3>决策：<span class="pill" style="background:${color}22;color:${color}">${dir}</span> &nbsp; 建议仓位 <b>${d.suggested_position_pct??0}%</b></h3>`;
  if(d.suggested_position_pct>0){
    html += `<table style="margin-top:6px">
      <tr><td>入场区间</td><td>${d.entry_zone||'-'}</td></tr>
      <tr><td>硬止损 (-10%)</td><td class="neg">${d.stop_loss||'-'}</td></tr>
      <tr><td>参考止盈 (+8%)</td><td class="pos">${d.take_profit_ref||'-'}</td></tr>
      <tr><td>时间止损</td><td>${d.time_stop_days||'-'} 天</td></tr>
      <tr><td>组合最大风险</td><td class="warnc">≈${d.max_risk_pct||'-'}%</td></tr></table>`;
  }
  html += `<div class="reasons"><b>依据：</b><br>` + (d.reasons||[]).map(r=>'· '+r).join('<br>') + `</div>`;
  $('plan').innerHTML = html;
}

function card(k,v,sub){return `<div class="card"><div class="k">${k}</div><div class="v">${v} ${sub?('<small>'+sub+'</small>'):''}</div></div>`;}

function renderCards(s){
  const f=s.real_data.funding||{}, oi=s.real_data.open_interest||{}, ls=s.real_data.long_short||{},
        fng=s.real_data.fear_greed||{}, ms=s.real_data.market_structure||{}, oc=s.real_data.onchain||{},
        fac=s.factor_state||{};
  let h='';
  h+=card('价格', fac.price??'-', 'USDT');
  h+=card('距高点回撤', (fac.drawdown_from_ath_pct??'-')+'%', fac.above_ma200?'200MA之上':'200MA之下');
  h+=card('资金费率(8h)', (f.last_funding_rate_8h_pct??'-')+'%', f.funding_regime||'');
  h+=card('恐慌贪婪', fng.fng_value??'-', fng.fng_class||'');
  h+=card('全网多空比', ls.global_long_short_ratio??'-', '大户 '+(ls.top_trader_long_short_ratio??'-'));
  h+=card('未平仓OI', oi.open_interest_contracts??'-', '7日 '+(oi.oi_7d_trend_pct??'-')+'%');
  h+=card('BTC市占率', (ms.btc_dominance_pct??'-')+'%', '总市值 '+(ms.total_market_cap_usd_b??'-')+'B');
  if(oc.hashrate_eh_s) h+=card('全网算力', oc.hashrate_eh_s+' EH/s', '内存池 '+(oc.mempool_tx_count??'-'));
  $('cards').innerHTML=h;
  $('foot').innerHTML='数据来源：Binance Futures/Spot、CoinGecko、alternative.me、blockchain.info/mempool.space（真实拉取，非估算）。'+
    '<br>因子 edge 经样本外验证偏弱，本仪表盘刻意以小仓位+硬止损+时间止损控制风险。不构成交易建议。';
}

function renderCharts(d){
  if(!cPrice) cPrice=echarts.init($('chartPrice'));
  if(!cFng) cFng=echarts.init($('chartFng'));
  const ax={axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  cPrice.setOption({title:{text:'价格 & 距高点回撤',textStyle:{color:'#e6edf3',fontSize:13}},
    tooltip:{trigger:'axis'},legend:{textStyle:{color:'#8b98a9'},top:22},grid:{left:55,right:55,top:55,bottom:30},
    xAxis:{type:'category',data:d.dates,...ax,axisLabel:{color:'#8b98a9'}},
    yAxis:[{type:'value',name:'价',...ax,axisLabel:{color:'#8b98a9'},scale:true},
           {type:'value',name:'回撤%',...ax,axisLabel:{color:'#8b98a9'},max:0}],
    series:[{name:'收盘价',type:'line',data:d.close,showSymbol:false,lineStyle:{color:'#3b82f6'}},
            {name:'回撤%',type:'line',yAxisIndex:1,data:d.drawdown_pct,showSymbol:false,areaStyle:{color:'#ea394322'},lineStyle:{color:'#ea3943'}}]});
  cFng.setOption({title:{text:'恐慌贪婪指数',textStyle:{color:'#e6edf3',fontSize:13}},
    tooltip:{trigger:'axis'},grid:{left:45,right:20,top:45,bottom:30},
    xAxis:{type:'category',data:d.dates,...ax,axisLabel:{color:'#8b98a9'}},
    yAxis:{type:'value',min:0,max:100,...ax,axisLabel:{color:'#8b98a9'}},
    visualMap:{show:false,pieces:[{lte:25,color:'#ea3943'},{gt:25,lte:45,color:'#f0b90b'},{gt:45,lte:55,color:'#8b98a9'},{gt:55,lte:75,color:'#7ac74f'},{gt:75,color:'#16c784'}]},
    series:[{name:'F&G',type:'line',data:d.fng,showSymbol:false}]});
}

function renderFactor(f){
  let rows='';
  for(const k in f){
    const lab=k.replace('fng_below_','F&G<');
    for(const h in f[k]){const m=f[k][h];
      rows+=`<tr><td>${lab}</td><td>${h.slice(1)}天</td><td>${m.n_events}</td><td>${m.cond_mean_ret_pct}%</td><td>${m.baseline_mean_ret_pct}%</td><td class="${cls(m.edge_pct)}">${pct(m.edge_pct)}</td><td>${m.cond_win_rate_pct}%</td></tr>`;
    }
  }
  $('factorTable').innerHTML=`<table><thead><tr><th>条件</th><th>持有</th><th>事件数</th><th>条件后收益</th><th>基线</th><th>超额edge</th><th>胜率</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="foot">注：F&G&lt;20 持有 30 天 edge 最佳（胜率显著高于基线）；持有 90 天 edge 转负——极度恐惧是<b>短期反弹</b>信号而非长期持仓。回撤≤-50% 因子经 P4 已判定过拟合，未纳入决策。</div>`;
}

window.addEventListener('resize',()=>{[gauge,cPrice,cFng,cTrack,cKline,cCalib,cAttr].forEach(c=>c&&c.resize());});
loadAll();
</script>
</body>
</html>
"""


LITE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>贾维斯 · 小白模式</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  :root{--bg:#0b0e14;--card:#141a24;--bd:#222c3a;--fg:#e6edf3;--mut:#8b98a9;--up:#16c784;--down:#ea3943;--accent:#3b82f6;--warn:#f0b90b;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--fg);font-family:-apple-system,"PingFang SC","Microsoft YaHei",Inter,sans-serif;padding:16px;max-width:760px;margin:0 auto;}
  .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
  h1{font-size:19px;font-weight:700}
  select,button{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:7px 12px;font-size:14px;cursor:pointer}
  button.primary{background:var(--accent);border-color:var(--accent)}
  button:hover{filter:brightness(1.15)}
  .refresh{margin-left:auto;color:var(--mut);font-size:12px;text-align:right;line-height:1.5}
  .verdict{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:20px;margin:12px 0;text-align:center}
  .verdict .big{font-size:34px;font-weight:800;letter-spacing:1px}
  .verdict .sub{color:var(--mut);font-size:14px;margin-top:8px}
  .conf{display:inline-block;padding:2px 12px;border-radius:999px;font-size:13px;font-weight:700;margin-left:6px}
  .ops{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:12px 0}
  .op{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px 14px}
  .op .k{color:var(--mut);font-size:12px}
  .op .v{font-size:19px;font-weight:700;margin-top:5px}
  .why{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px;margin:12px 0;font-size:14px;line-height:1.95}
  .why b{color:var(--fg)} .why .li{color:var(--mut)}
  .sec{margin:16px 0 8px;font-size:14px;font-weight:600;color:var(--mut)}
  .chart{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:10px;height:340px}
  .ivbar{display:flex;gap:6px;margin-bottom:8px}
  .ivbar button{padding:5px 12px;font-size:13px}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}
  .stat{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;text-align:center}
  .stat .k{color:var(--mut);font-size:12px}
  .stat .v{font-size:20px;font-weight:700;margin-top:5px}
  .pos{color:var(--up)} .neg{color:var(--down)}
  .foot{color:var(--mut);font-size:11px;margin-top:18px;line-height:1.7;text-align:center}
  .foot a{color:var(--accent)}
  .pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--up);margin-right:5px;animation:p 1.6s infinite}
  @keyframes p{0%{opacity:.3}50%{opacity:1}100%{opacity:.3}}
</style>
</head>
<body>
  <div class="top">
    <h1>🤖 贾维斯 · 小白模式</h1>
    <select id="symbol" onchange="loadAll()">
      <option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option><option>BNBUSDT</option>
    </select>
    <button class="primary" onclick="loadAll()">立即刷新</button>
    <span class="refresh"><span class="pulse"></span><span id="upAt">加载中…</span><br><span id="nextIn"></span></span>
  </div>

  <div class="verdict" id="verdict"><div class="big">加载中…</div></div>

  <div class="sec">该怎么操作（模拟，不构成投资建议）</div>
  <div class="ops" id="ops"></div>

  <div class="why" id="why"></div>

  <div class="sec" style="display:flex;align-items:center;gap:10px">
    K 线 + 贾维斯的建议线（蓝=入场 红=止损 绿=止盈）
    <span class="ivbar" id="ivbar">
      <button data-iv="1h" class="primary" onclick="setIv('1h')">1h</button>
      <button data-iv="4h" onclick="setIv('4h')">4h</button>
      <button data-iv="1d" onclick="setIv('1d')">1d</button>
    </span>
  </div>
  <div class="chart"><div id="kline" style="height:100%"></div></div>

  <div class="sec">模拟盘战绩（贾维斯自己跟单的真实表现）</div>
  <div class="stats" id="stats"></div>

  <div class="foot" id="foot">
    数据来源：Binance / CoinGecko / alternative.me / 链上（真实拉取）。本页每 60 秒自动刷新一次。<br>
    想看完整专业版（因子归因 / 置信度校准 / 挂单台账）？打开 <a href="/">完整仪表盘</a>。
  </div>

<script>
const $ = id => document.getElementById(id);
const REFRESH = 60;                 // 自动刷新间隔（秒）
let kline, lastDec = null, klineIv = '1h', countdown = REFRESH, tick = null;

function pct(x){return (x==null?'—':((x>0?'+':'')+x+'%'));}
function cls(x){return x>0?'pos':(x<0?'neg':'');}

function verdictOf(d){
  const s = d.conviction_score ?? 0, abs = Math.abs(s);
  const conf = abs>=1.2?'高':(abs>=0.6?'中':'低');
  if(s>=0.6)  return {txt:'可小仓试多 📈', color:'#16c784', conf, score:s};
  if(s<=-0.6) return {txt:'偏空 · 别追多 📉', color:'#ea3943', conf, score:s};
  return {txt:'先观望 ⏸', color:'#8b98a9', conf:'低', score:s};
}

function renderVerdict(s){
  const d = s.decision || {}; lastDec = d;
  const v = verdictOf(d);
  const price = (s.factor_state||{}).price;
  $('verdict').innerHTML =
    `<div class="big" style="color:${v.color}">${v.txt}</div>`+
    `<div class="sub">${($('symbol').value)} 现价约 <b style="color:var(--fg)">${price??'—'}</b> · `+
    `贾维斯把握度 <span class="conf" style="background:${v.color}22;color:${v.color}">${v.conf}</span> `+
    `<span style="font-size:12px">(信心分 ${v.score})</span></div>`;
  // 操作卡
  const pos = d.suggested_position_pct ?? 0;
  let ops = `<div class="op"><div class="k">建议仓位</div><div class="v">${pos}%</div></div>`;
  if(pos>0){
    ops += `<div class="op"><div class="k">入场区间</div><div class="v" style="font-size:15px">${d.entry_zone||'—'}</div></div>`;
    ops += `<div class="op"><div class="k">止损（守不住就卖）</div><div class="v neg">${d.stop_loss||'—'}</div></div>`;
    ops += `<div class="op"><div class="k">止盈（到了可落袋）</div><div class="v pos">${d.take_profit_ref||'—'}</div></div>`;
    ops += `<div class="op"><div class="k">最多持有</div><div class="v">${d.time_stop_days||'—'} 天</div></div>`;
  } else {
    ops += `<div class="op" style="grid-column:1/-1"><div class="k">为什么不给点位</div><div class="v" style="font-size:14px;color:var(--mut)">当前信号不够强，贾维斯建议空仓等更好的机会，不硬找入场点。</div></div>`;
  }
  $('ops').innerHTML = ops;
  // 通俗依据（取前 3 条）
  const rs = (d.reasons||[]).slice(0,3);
  $('why').innerHTML = `<b>贾维斯为什么这么说：</b><br>` +
    (rs.length ? rs.map(r=>'<span class="li">· '+r+'</span>').join('<br>') : '<span class="li">暂无明确依据，信号偏中性。</span>');
  if(kline) renderKlineOverlay();
}

function setIv(iv){
  klineIv = iv;
  document.querySelectorAll('#ivbar button').forEach(b=>{
    b.className = (b.getAttribute('data-iv')===iv) ? 'primary' : '';
  });
  loadKline();
}

async function loadKline(){
  try{
    const sym = $('symbol').value;
    const d = await (await fetch('/api/kline?symbol='+sym+'&interval='+klineIv+'&limit=160')).json();
    if(d.error) return;
    renderKline(d);
  }catch(e){}
}

let lastKline = null;
function renderKline(d){
  lastKline = d;
  if(!kline) kline = echarts.init($('kline'));
  renderKlineOverlay();
}

function renderKlineOverlay(){
  if(!kline || !lastKline) return;
  const rows = lastKline.rows || [];
  const dates = rows.map(r=>r.t);
  const ohlc = rows.map(r=>[r.o,r.c,r.l,r.h]);
  const ax = {axisLine:{lineStyle:{color:'#8b98a9'}},splitLine:{lineStyle:{color:'#1c2530'}}};
  const ml = []; const dec = lastDec || {};
  if(dec.suggested_position_pct>0){
    if(dec.stop_loss) ml.push({yAxis:dec.stop_loss,lineStyle:{color:'#ea3943'},label:{formatter:'止损 '+dec.stop_loss,color:'#ea3943',position:'insideEndTop'}});
    if(dec.take_profit_ref) ml.push({yAxis:dec.take_profit_ref,lineStyle:{color:'#16c784'},label:{formatter:'止盈 '+dec.take_profit_ref,color:'#16c784',position:'insideEndTop'}});
    if(dec.entry_zone){(''+dec.entry_zone).split('~').map(s=>parseFloat(s.trim())).filter(x=>!isNaN(x))
      .forEach(p=>ml.push({yAxis:p,lineStyle:{color:'#3b82f6',type:'dashed'},label:{formatter:'入场 '+p,color:'#3b82f6',position:'insideEndTop'}}));}
  }
  kline.setOption({
    tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
    grid:{left:55,right:18,top:14,bottom:48},
    xAxis:{type:'category',data:dates,...ax,axisLabel:{color:'#8b98a9'},boundaryGap:true},
    yAxis:{scale:true,...ax,axisLabel:{color:'#8b98a9'}},
    dataZoom:[{type:'inside',start:50,end:100},{type:'slider',bottom:8,height:14,start:50,end:100,textStyle:{color:'#8b98a9'}}],
    series:[{type:'candlestick',data:ohlc,itemStyle:{color:'#16c784',color0:'#ea3943',borderColor:'#16c784',borderColor0:'#ea3943'},
      markLine:{symbol:'none',data:ml,silent:true}}]
  });
}

async function loadStats(){
  try{
    const st = await (await fetch('/api/trader/status')).json();
    const cards = [
      ['总权益', (st.equity_usdt??'—')+' U', cls(st.equity_change_pct)],
      ['总收益', pct(st.equity_change_pct), cls(st.equity_change_pct)],
      ['胜率', (st.win_rate_pct==null?'—':st.win_rate_pct+'%'), ''],
      ['盈亏比', (st.profit_factor??'—'), ''],
      ['已平仓', (st.closed_trades??0)+' 笔', ''],
    ];
    $('stats').innerHTML = cards.map(c=>
      `<div class="stat"><div class="k">${c[0]}</div><div class="v ${c[2]}">${c[1]}</div></div>`).join('');
  }catch(e){ $('stats').innerHTML='<div class="stat"><div class="k">模拟盘</div><div class="v" style="font-size:13px">还没跑跟盘</div></div>'; }
}

async function loadAll(){
  countdown = REFRESH;
  $('upAt').textContent = '更新中…';
  try{
    const sym = $('symbol').value;
    const snap = await (await fetch('/api/snapshot?symbol='+sym)).json();
    renderVerdict(snap);
    const t = new Date();
    $('upAt').textContent = '更新于 ' + t.toLocaleTimeString('zh-CN',{hour12:false});
  }catch(e){ $('upAt').textContent = '刷新失败，下次重试'; }
  loadKline();
  loadStats();
}

function startTimer(){
  if(tick) clearInterval(tick);
  tick = setInterval(()=>{
    countdown--;
    $('nextIn').textContent = countdown>0 ? (countdown+' 秒后自动刷新') : '刷新中…';
    if(countdown<=0) loadAll();
  }, 1000);
}

window.addEventListener('resize',()=>{ if(kline) kline.resize(); });
loadAll();
startTimer();
</script>
</body>
</html>
"""


COCKPIT_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>贾维斯 · 驾驶舱</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@5.0.8/dist/lightweight-charts.standalone.production.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#080b11;--card:rgba(20,27,38,0.62);--card2:rgba(13,18,26,0.66);--glass:rgba(22,30,44,0.55);
    --bd:rgba(255,255,255,0.08);--bd2:rgba(255,255,255,0.14);
    --fg:#eef3f9;--fg2:#c4cfdd;--mut:#8794a6;
    --up:#16c784;--down:#ea3943;--accent:#3b82f6;--accent2:#60a5fa;--warn:#f0b90b;
    --r:14px;--r2:10px;--r3:999px;
    --sp1:6px;--sp2:10px;--sp3:14px;--sp4:18px;
    --sh:0 8px 30px rgba(0,0,0,0.38);--sh2:0 2px 10px rgba(0,0,0,0.25);
    --blur:saturate(160%) blur(14px);
    --font:"Inter",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
    --mono:"JetBrains Mono","Inter",ui-monospace,SFMono-Regular,Menlo,monospace;
    --ease:cubic-bezier(.22,.61,.36,1);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  .num{font-family:var(--mono);font-feature-settings:"tnum" 1;font-variant-numeric:tabular-nums;letter-spacing:-.2px}
  body{
    background:var(--bg);color:var(--fg);font-family:var(--font);height:100vh;
    display:flex;flex-direction:column;overflow:hidden;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
    background-image:radial-gradient(900px 520px at 18% -8%,rgba(59,130,246,0.16),transparent 60%),radial-gradient(760px 480px at 100% 0%,rgba(22,199,132,0.10),transparent 55%),radial-gradient(700px 600px at 60% 120%,rgba(124,92,214,0.10),transparent 60%);
    background-attachment:fixed;
  }
  @keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  .hdr{display:flex;align-items:center;gap:12px;padding:11px 18px;border-bottom:1px solid var(--bd);flex-wrap:wrap;background:rgba(10,14,20,0.5);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur)}
  .logo{font-size:16px;font-weight:800;letter-spacing:-.3px;display:flex;align-items:center;gap:7px}
  .chips{display:flex;gap:6px}
  .chip{background:var(--card);border:1px solid var(--bd);border-radius:var(--r3);padding:5px 14px;font-size:13px;font-weight:600;cursor:pointer;color:var(--mut);transition:all .22s var(--ease);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur)}
  .chip:hover{color:var(--fg);border-color:var(--bd2);transform:translateY(-1px)}
  .chip.on{background:linear-gradient(135deg,var(--accent),var(--accent2));border-color:transparent;color:#fff;font-weight:700;box-shadow:0 4px 14px rgba(59,130,246,0.4)}
  .symin{background:var(--card2);color:var(--fg);border:1px solid var(--bd);border-radius:var(--r2);padding:6px 11px;font-size:13px;width:124px;transition:border-color .22s var(--ease)}
  .symin:focus{outline:none;border-color:var(--accent)}
  .price{font-size:18px;font-weight:800;margin-left:4px}
  .price #px{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.4px}
  .price small{font-size:12px;color:var(--mut);font-weight:500;font-family:var(--font)}
  .spacer{flex:1}
  .live{color:var(--mut);font-size:12px;text-align:right;line-height:1.4}
  .pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--up);margin-right:5px;animation:p 1.6s infinite}
  @keyframes p{0%{opacity:.3}50%{opacity:1}100%{opacity:.3}}
  .cockpit{flex:1;display:grid;grid-template-columns:1fr 392px;gap:var(--sp3);padding:var(--sp3);min-height:0}
  .chartcol{display:flex;flex-direction:column;gap:var(--sp2);min-height:0}
  .ivbar{display:flex;gap:6px;align-items:center}
  .ivbar button{background:var(--card);color:var(--mut);border:1px solid var(--bd);border-radius:8px;padding:5px 13px;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s var(--ease)}
  .ivbar button:hover{color:var(--fg);border-color:var(--bd2)}
  .ivbar button.on{background:linear-gradient(135deg,var(--accent),var(--accent2));border-color:transparent;color:#fff;font-weight:700;box-shadow:0 3px 12px rgba(59,130,246,0.35)}
  .ivbar .hint{color:var(--mut);font-size:12px;margin-left:auto}
  #kline{flex:1;background:var(--card2);border:1px solid var(--bd);border-radius:var(--r);min-height:0;box-shadow:var(--sh);overflow:hidden}
  .sidecol{display:flex;flex-direction:column;gap:12px;min-height:0;overflow:hidden}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:15px;box-shadow:var(--sh);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur);animation:fadeUp .4s var(--ease) both}
  .copilot{position:relative;overflow:hidden}
  .copilot::before{content:"";position:absolute;inset:0;background:radial-gradient(420px 160px at 100% 0%,rgba(59,130,246,0.12),transparent 70%);pointer-events:none}
  .copilot .vd{font-size:24px;font-weight:800;letter-spacing:-.4px}
  .copilot .vsub{color:var(--mut);font-size:12px;margin-top:5px}
  .conf{display:inline-block;padding:1px 9px;border-radius:var(--r3);font-size:12px;font-weight:700;margin-left:4px}
  .opgrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:13px;position:relative}
  .op{background:var(--card2);border:1px solid var(--bd);border-radius:var(--r2);padding:9px 11px;transition:border-color .2s var(--ease),transform .2s var(--ease)}
  .op:hover{border-color:var(--bd2);transform:translateY(-1px)}
  .op .k{color:var(--mut);font-size:11px;font-weight:500}
  .op .v{font-size:15px;font-weight:700;margin-top:4px;font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.2px}
  .why{margin-top:11px;font-size:13px;line-height:1.7;color:var(--mut);border-top:1px solid var(--bd);padding-top:10px}
  .why b{color:var(--fg)}
  .panelhd{font-size:12px;font-weight:700;color:var(--mut);margin-bottom:11px;display:flex;align-items:center;gap:7px;text-transform:uppercase;letter-spacing:.6px}
  .events{flex:1;display:flex;flex-direction:column;min-height:120px}
  .evlist{flex:1;overflow:auto;display:flex;flex-direction:column;gap:8px}
  .ev{display:flex;gap:9px;font-size:12px;border-left:2px solid var(--bd);padding:3px 0 3px 9px}
  .ev.buy{border-color:var(--accent)} .ev.win{border-color:var(--up)} .ev.loss{border-color:var(--down)} .ev.flat{border-color:var(--mut)} .ev.info{border-color:var(--accent2)}
  .ev .t{color:var(--mut);white-space:nowrap;font-size:11px}
  .ev .tt{font-weight:600} .ev .dd{color:var(--mut);margin-top:2px}
  .empty{color:var(--mut);font-size:12px;text-align:center;padding:18px 0}
  .ask{display:flex;flex-direction:column;height:248px}
  .chat{flex:1;overflow:auto;display:flex;flex-direction:column;gap:9px;padding-right:2px}
  .msg{font-size:13px;line-height:1.6;max-width:92%;padding:8px 11px;border-radius:11px}
  .msg.u{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:3px}
  .msg.a{align-self:flex-start;background:var(--card2);border:1px solid var(--bd);border-bottom-left-radius:3px}
  .askbar{display:flex;gap:7px;margin-top:9px}
  .askbar input{flex:1;background:var(--card2);color:var(--fg);border:1px solid var(--bd);border-radius:var(--r2);padding:9px 11px;font-size:13px;transition:border-color .22s var(--ease)}
  .askbar input:focus{outline:none;border-color:var(--accent)}
  .askbar button{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:var(--r2);color:#fff;padding:0 16px;font-weight:700;cursor:pointer;transition:filter .2s var(--ease),transform .2s var(--ease)}
  .askbar button:hover{filter:brightness(1.1);transform:translateY(-1px)}
  .qsugg{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .qsugg span{background:var(--card2);border:1px solid var(--bd);border-radius:var(--r3);padding:4px 11px;font-size:11px;color:var(--mut);cursor:pointer;transition:all .2s var(--ease)}
  .qsugg span:hover{color:var(--fg);border-color:var(--bd2)}
  .strip{display:flex;align-items:stretch;gap:10px;padding:11px 14px;border-top:1px solid var(--bd);overflow-x:auto;background:rgba(10,14,20,0.45);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur)}
  .stat{background:var(--card);border:1px solid var(--bd);border-radius:var(--r2);padding:8px 14px;min-width:98px;text-align:center;flex-shrink:0;transition:border-color .2s var(--ease)}
  .stat:hover{border-color:var(--bd2)}
  .stat .k{color:var(--mut);font-size:11px} .stat .v{font-size:16px;font-weight:700;margin-top:4px;font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.2px}
  .pos{color:var(--up)} .neg{color:var(--down)} .mut{color:var(--mut)}
  .actbtn{margin-left:auto;display:flex;gap:8px;align-items:center;flex-shrink:0}
  .actbtn button{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:var(--r2);color:#fff;padding:9px 16px;font-weight:700;cursor:pointer;font-size:13px;transition:filter .2s var(--ease),transform .2s var(--ease)}
  .actbtn button.ghost{background:var(--card);border:1px solid var(--bd);color:var(--fg)}
  .actbtn button:hover{filter:brightness(1.12);transform:translateY(-1px)}
  .holds{font-size:11px;color:var(--mut);align-self:center;flex-shrink:0;max-width:300px}
  .foot{color:var(--mut);font-size:10px;padding:0 14px 8px;line-height:1.5}
  .foot a{color:var(--accent)}
  @media(max-width:980px){.cockpit{grid-template-columns:1fr;overflow:auto}.sidecol{overflow:visible}#kline{height:340px;flex:none}.ask{height:300px}}
</style>
</head>
<body>
  <div class="hdr">
    <div class="logo">🤖 贾维斯驾驶舱</div>
    <div class="chips" id="chips"></div>
    <input class="symin" id="symin" placeholder="看其它币 如 DOGE" onkeydown="if(event.key==='Enter')pickInput()"/>
    <div class="price"><span id="px">—</span> <small id="pxsub"></small></div>
    <div class="spacer"></div>
    <div class="live"><span class="pulse"></span><span id="upAt">加载中…</span><br><span id="nextIn"></span></div>
  </div>

  <div class="cockpit">
    <div class="chartcol">
      <div class="ivbar" id="ivbar">
        <button data-iv="15m" onclick="setIv('15m')">15分</button>
        <button data-iv="1h" class="on" onclick="setIv('1h')">1时</button>
        <button data-iv="4h" onclick="setIv('4h')">4时</button>
        <button data-iv="1d" onclick="setIv('1d')">日线</button>
        <span class="hint">蓝=入场 红=止损 绿=止盈 ▲▼=买卖点 · 黄=MA7 蓝=MA25 · 底部柱=成交量</span>
      </div>
      <div id="kline"></div>
    </div>

    <div class="sidecol">
      <div class="card copilot" id="copilot"><div class="vd">加载中…</div></div>

      <div class="card events">
        <div class="panelhd">📡 实时事件流 <span class="mut" style="font-weight:400">（真实行情 + 模拟盘）</span></div>
        <div class="evlist" id="evlist"><div class="empty">加载中…</div></div>
      </div>

      <div class="card ask">
        <div class="panelhd">💬 问贾维斯</div>
        <div class="chat" id="chat"></div>
        <div class="qsugg" id="qsugg">
          <span onclick="quick('现在该买吗')">现在该买吗</span>
          <span onclick="quick('止盈止损在哪')">止盈止损在哪</span>
          <span onclick="quick('为什么这么判断')">为什么</span>
          <span onclick="quick('最近战绩怎么样')">最近战绩</span>
        </div>
        <div class="askbar">
          <input id="qin" placeholder="问问该买什么、卖多少、为什么…" onkeydown="if(event.key==='Enter')sendAsk()"/>
          <button onclick="sendAsk()">问</button>
        </div>
      </div>
    </div>
  </div>

  <div class="strip" id="strip"></div>
  <div class="foot">全程模拟盘(paper)不碰真钱 · 数据来自 Binance/CoinGecko/alternative.me · 仅研究不构成投资建议 · <a href="/lite">小白单页</a> · <a href="/">专业版</a></div>

<script>
const $ = id => document.getElementById(id);
const REFRESH = 60;
const SYMS = ['BTCUSDT','ETHUSDT','SOLUSDT'];
let sym = 'BTCUSDT', iv = '1h';
let chart, candleSeries=null, volSeries=null, maSeries={}, markersPrim=null, priceLines=[], chartKey='';
let lastDec=null, lastFac=null, lastKline=null, lastPositions=[];
let countdown = REFRESH, tick=null;
const TZ = new Date().getTimezoneOffset()*60;   // 秒：让 X 轴显示本地时间
const barTime = r => Math.floor(r.ts/1000) - TZ;

function fmt(n){ if(n==null||isNaN(n)) return '—'; n=+n; const d=Math.abs(n)>=100?2:(Math.abs(n)>=1?3:6); return n.toLocaleString('en-US',{maximumFractionDigits:d}); }
function pct(x){ return x==null?'—':((x>0?'+':'')+x+'%'); }

function renderChips(){
  $('chips').innerHTML = SYMS.map(s=>`<div class="chip ${s===sym?'on':''}" onclick="pick('${s}')">${s.replace('USDT','')}</div>`).join('')
    + (SYMS.includes(sym)?'':`<div class="chip on">${sym.replace('USDT','')}</div>`);
}
function pick(s){ sym=s; renderChips(); loadAll(); }
function pickInput(){ let v=$('symin').value.trim().toUpperCase().replace(/[-/]/g,''); if(!v) return; if(!v.endsWith('USDT')) v+='USDT'; $('symin').value=''; sym=v; renderChips(); loadAll(); }
function setIv(x){ iv=x; document.querySelectorAll('#ivbar button').forEach(b=>b.className=(b.getAttribute('data-iv')===x?'on':'')); loadKline(); }

function verdictOf(d){
  const s=d.conviction_score??0, a=Math.abs(s);
  const conf=a>=1.2?'高':(a>=0.6?'中':'低');
  if(s>=0.6)  return {txt:'可小仓试多 📈',color:'#16c784',conf,s};
  if(s<=-0.6) return {txt:'偏空·别追多 📉',color:'#ea3943',conf,s};
  return {txt:'先观望 ⏸',color:'#8b98a9',conf:'低',s};
}
function rr(d){
  const sl=+d.stop_loss, tp=+d.take_profit_ref; let e=null;
  if(d.entry_zone){ const ps=(''+d.entry_zone).split('~').map(x=>parseFloat(x)).filter(x=>!isNaN(x)); if(ps.length) e=ps.reduce((a,b)=>a+b,0)/ps.length; }
  if(!e||isNaN(sl)||isNaN(tp)||e<=sl) return null;
  return ((tp-e)/(e-sl));
}
function entryMid(d){ if(!d.entry_zone) return null; const ps=(''+d.entry_zone).split('~').map(x=>parseFloat(x)).filter(x=>!isNaN(x)); return ps.length?ps.reduce((a,b)=>a+b,0)/ps.length:null; }

function renderCopilot(snap){
  const d=snap.decision||{}, fac=snap.factor_state||{}; lastDec=d; lastFac=fac;
  const price=fac.price; $('px').textContent=fmt(price); $('pxsub').textContent=sym.replace('USDT','/USDT');
  if(d._error||fac._error){ $('copilot').innerHTML=`<div class="vd" style="color:var(--mut)">取数暂不可用</div><div class="why">${d._error||fac._error||''}</div>`; return; }
  const v=verdictOf(d), pos=d.suggested_position_pct??0, r=rr(d);
  let ops='';
  ops+=`<div class="op"><div class="k">建议仓位</div><div class="v">${pos}%</div></div>`;
  if(pos>0){
    ops+=`<div class="op"><div class="k">入场区间</div><div class="v" style="font-size:13px">${d.entry_zone||'—'}</div></div>`;
    ops+=`<div class="op"><div class="k">盈亏比</div><div class="v pos">${r?r.toFixed(2)+':1':'—'}</div></div>`;
    ops+=`<div class="op"><div class="k">止损</div><div class="v neg">${fmt(d.stop_loss)}</div></div>`;
    ops+=`<div class="op"><div class="k">止盈</div><div class="v pos">${fmt(d.take_profit_ref)}</div></div>`;
    ops+=`<div class="op"><div class="k">最多持有</div><div class="v">${d.time_stop_days||'—'}天</div></div>`;
  }else{
    ops+=`<div class="op" style="grid-column:2/4"><div class="k">为什么不给点位</div><div class="v" style="font-size:12px;color:var(--mut)">信号不够强，空仓等更好机会</div></div>`;
  }
  const rs=(d.reasons||[]).slice(0,2);
  $('copilot').innerHTML=
    `<div class="vd" style="color:${v.color}">${v.txt}<span class="conf" style="background:${v.color}22;color:${v.color}">把握${v.conf}</span></div>`+
    `<div class="vsub">${sym.replace('USDT','')} 现价 ${fmt(price)} · 信心分 ${v.s}</div>`+
    `<div class="opgrid">${ops}</div>`+
    `<div class="why"><b>为什么：</b>${rs.length?rs.join('；'):'当前因子偏中性，无明确方向。'}</div>`;
  if(chart) drawOverlay();
}

async function loadKline(){
  const reqSym=sym, reqIv=iv;
  try{
    const d=await (await fetch('/api/kline?symbol='+reqSym+'&interval='+reqIv+'&limit=180')).json();
    if(reqSym!==sym||reqIv!==iv) return;            // 期间已切币，丢弃过期响应
    if(d.error||!(d.rows&&d.rows.length)){
      if(sym+'|'+iv!==chartKey&&candleSeries){ candleSeries.setData([]); chartKey=sym+'|'+iv; }
      return;
    }
    lastKline=d; drawChart();
  }catch(e){}
}

function drawChart(){
  if(!chart){
    chart = LightweightCharts.createChart($('kline'), {
      autoSize:true,
      layout:{ background:{type:'solid',color:'#0b0f16'}, textColor:'#9aa7b6', fontSize:11, fontFamily:'Inter,-apple-system,PingFang SC,Microsoft YaHei,sans-serif', attributionLogo:false },
      grid:{ vertLines:{color:'rgba(42,54,69,0.35)'}, horzLines:{color:'rgba(42,54,69,0.35)'} },
      rightPriceScale:{ borderColor:'#2a3645', scaleMargins:{top:0.12,bottom:0.12} },
      timeScale:{ borderColor:'#2a3645', timeVisible:true, secondsVisible:false, rightOffset:8, barSpacing:9, minBarSpacing:3 },
      crosshair:{ mode:1, vertLine:{color:'#3b82f6',width:1,style:LightweightCharts.LineStyle.Dotted,labelBackgroundColor:'#3b82f6'}, horzLine:{color:'#3b82f6',labelBackgroundColor:'#3b82f6'} },
      localization:{ locale:'zh-CN', priceFormatter:p=>fmt(p) }
    });
    candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor:'#0ecb81', downColor:'#f6465d', borderVisible:true,
      borderUpColor:'#0ecb81', borderDownColor:'#f6465d',
      wickUpColor:'#0ecb81', wickDownColor:'#f6465d'
    });
    // 成交量副图（底部 ~18%，独立价格轴，对标 trendiq）
    volSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
      priceScaleId:'vol', priceFormat:{type:'volume'},
      lastValueVisible:false, priceLineVisible:false
    });
    chart.priceScale('vol').applyOptions({ scaleMargins:{top:0.82,bottom:0}, borderVisible:false });
    // MA 均线（细线，半透，不抢蜡烛戏）
    maSeries.ma7  = chart.addSeries(LightweightCharts.LineSeries,{color:'rgba(240,185,11,0.85)',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
    maSeries.ma25 = chart.addSeries(LightweightCharts.LineSeries,{color:'rgba(96,165,250,0.85)',lineWidth:1,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
    markersPrim = LightweightCharts.createSeriesMarkers(candleSeries, []);
  }
  const rows=(lastKline&&lastKline.rows)||[];
  const data=rows.map(r=>({time:barTime(r),open:r.o,high:r.h,low:r.l,close:r.c}));
  const key=sym+'|'+iv;
  if(key!==chartKey){
    candleSeries.setData(data);
    drawVolMA(rows);
    chart.timeScale().fitContent();
    chartKey=key;
    openWS();                 // 历史就位后，切到 Binance WebSocket 逐 tick 实时推送
  }
  // key 未变时不在这里更新蜡烛——由 WebSocket 实时 update()，避免 REST 旧值覆盖更新
  drawTrendlines();
  drawPattern();
  drawOverlay();
}

// ───────── 成交量副图 + MA 均线（对标 trendiq）─────────
function ma(rows,n){
  const out=[]; let sum=0;
  for(let i=0;i<rows.length;i++){
    sum+=rows[i].c;
    if(i>=n) sum-=rows[i-n].c;
    if(i>=n-1) out.push({time:barTime(rows[i]),value:+(sum/n).toFixed(2)});
  }
  return out;
}
function drawVolMA(rows){
  if(!chart||!volSeries||!rows||!rows.length) return;
  volSeries.setData(rows.map(r=>({
    time:barTime(r), value:+r.v||0,
    color:(r.c>=r.o)?'rgba(14,203,129,0.45)':'rgba(246,70,93,0.45)'
  })));
  if(maSeries.ma7)  maSeries.ma7.setData(ma(rows,7));
  if(maSeries.ma25) maSeries.ma25.setData(ma(rows,25));
}

// ───────── 斜趋势线（连真实摆动高/低点，形成上下轨通道，对标 trendiq）─────────
let trendSeries=[];
function clearTrend(){ trendSeries.forEach(s=>{ try{chart.removeSeries(s);}catch(e){} }); trendSeries=[]; }
function pivots(rows,k,type){
  const out=[];
  for(let i=k;i<rows.length-k;i++){
    let ok=true;
    for(let j=i-k;j<=i+k&&ok;j++){
      if(j===i) continue;
      if(type==='high'){ if(rows[j].h>rows[i].h) ok=false; }
      else{ if(rows[j].l<rows[i].l) ok=false; }
    }
    if(ok) out.push({t:barTime(rows[i]), v:(type==='high'?rows[i].h:rows[i].l)});
  }
  return out;
}
function addTrend(p1,p2,color){
  if(!lastKline||!lastKline.rows.length||p2.t<=p1.t) return;
  const rows=lastKline.rows, lastT=barTime(rows[rows.length-1]);
  const slope=(p2.v-p1.v)/(p2.t-p1.t);
  const endV=p2.v+slope*(lastT-p2.t);                 // 沿趋势延伸到最新一根
  const s=chart.addSeries(LightweightCharts.LineSeries,{color,lineWidth:1,
    lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false,
    pointMarkersVisible:false});
  s.setData([{time:p1.t,value:+p1.v},{time:lastT,value:+endV.toFixed(2)}]);
  trendSeries.push(s);
}
function drawTrendlines(){
  if(!chart||!lastKline) return;
  clearTrend();
  const rows=lastKline.rows||[]; if(rows.length<20) return;
  const win=rows.slice(-Math.min(rows.length,120));
  const k=Math.max(2,Math.round(win.length/24));      // 摆动点检测半窗
  const hs=pivots(win,k,'high'), ls=pivots(win,k,'low');
  if(hs.length>=2) addTrend(hs[hs.length-2],hs[hs.length-1],'rgba(214,184,92,0.78)');    // 上轨/阻力趋势线（雅致金，1px）
  if(ls.length>=2) addTrend(ls[ls.length-2],ls[ls.length-1],'rgba(64,196,160,0.78)');    // 下轨/支撑趋势线（雅致青，1px）
}

// ───────── V 形反转顶 形态（左低点→顶→右低点 阴影三角 · 自绘 canvas 图元）─────────
function detectVtop(rows){
  const W=Math.min(rows.length,90); if(W<25) return null;
  const seg=rows.slice(-W);
  let pi=-1,ph=-Infinity;
  for(let i=8;i<seg.length-4;i++){ if(seg[i].h>ph){ph=seg[i].h;pi=i;} }   // 留边距找最高点=反转顶
  if(pi<0) return null;
  const lstart=Math.max(0,pi-30); let li=lstart,lv=Infinity;
  for(let i=lstart;i<=pi;i++){ if(seg[i].l<lv){lv=seg[i].l;li=i;} }        // 顶左侧最低=左基
  const rend=Math.min(seg.length-1,pi+30); let ri=pi,rv=Infinity;
  for(let i=pi;i<=rend;i++){ if(seg[i].l<rv){rv=seg[i].l;ri=i;} }          // 顶右侧最低=右基
  if(li>=pi||ri<=pi) return null;
  const peak=seg[pi].h;
  const dropL=(peak-seg[li].l)/peak, dropR=(peak-seg[ri].l)/peak;
  if(dropL<0.004||dropR<0.004) return null;                               // 两侧涨/跌幅太小不算反转
  return { left:{time:barTime(seg[li]),value:seg[li].l},
           peak:{time:barTime(seg[pi]),value:peak},
           right:{time:barTime(seg[ri]),value:seg[ri].l} };
}

function makeVtopPrimitive(){
  return {
    pts:null, chart:null, series:null, req:null,
    attached(p){ this.chart=p.chart; this.series=p.series; this.req=p.requestUpdate; },
    detached(){ this.chart=this.series=this.req=null; },
    setPts(pts){ this.pts=pts; if(this.req) this.req(); },
    paneViews(){
      const self=this;
      return [{ renderer(){ return { draw(target){
        const pts=self.pts; if(!pts) return;
        target.useBitmapCoordinateSpace(scope=>{
          const ctx=scope.context, hr=scope.horizontalPixelRatio, vr=scope.verticalPixelRatio, ts=self.chart.timeScale();
          const P=[pts.left,pts.peak,pts.right].map(p=>{
            const x=ts.timeToCoordinate(p.time), y=self.series.priceToCoordinate(p.value);
            return (x==null||y==null)?null:{x:x*hr,y:y*vr};
          });
          if(P.some(p=>p==null)) return;
          ctx.beginPath(); ctx.moveTo(P[0].x,P[0].y); ctx.lineTo(P[1].x,P[1].y); ctx.lineTo(P[2].x,P[2].y); ctx.closePath();
          ctx.fillStyle='rgba(124,92,214,0.16)'; ctx.fill();
          ctx.lineWidth=1*hr; ctx.strokeStyle='rgba(214,150,60,0.85)'; ctx.stroke();
          // 标签「V 形反转顶」+ 向下箭头
          const peak=P[1], fs=11*vr;
          ctx.font='600 '+fs+'px -apple-system,PingFang SC,sans-serif';
          ctx.textAlign='center'; ctx.fillStyle='rgba(232,200,120,0.95)';
          ctx.fillText('V 形反转顶', peak.x, peak.y-22*vr);
          ctx.beginPath(); ctx.moveTo(peak.x,peak.y-16*vr); ctx.lineTo(peak.x-3*hr,peak.y-21*vr); ctx.lineTo(peak.x+3*hr,peak.y-21*vr); ctx.closePath();
          ctx.fillStyle='rgba(232,200,120,0.95)'; ctx.fill();
        });
      } }; } }];
    }
  };
}

let vtopPrim=null;
function drawPattern(){
  if(!chart||!candleSeries||!lastKline) return;
  if(!vtopPrim){ vtopPrim=makeVtopPrimitive(); try{ candleSeries.attachPrimitive(vtopPrim); }catch(e){ vtopPrim=null; return; } }
  const v=detectVtop(lastKline.rows||[]);
  vtopPrim.setPts(v);   // 无形态时传 null，渲染器自动跳过
}

// ───────── Binance WebSocket 逐 tick 实时 K 线 ─────────
let ws=null, wsRetry=0, wsWanted='', wsStatus='连接实时行情…';
function wsLive(){ return ws && ws.readyState===1; }
function renderLive(){
  const el=$('nextIn'); if(!el) return;
  el.textContent = wsStatus + ' · 决策 ' + (countdown>0?countdown+'s 后刷新':'刷新中…');
}
function setWsStatus(t){ wsStatus=t; renderLive(); }
function closeWS(){ if(ws){ try{ ws.onclose=null; ws.onerror=null; ws.close(); }catch(e){} ws=null; } }
function openWS(){
  const want=sym+'|'+iv; wsWanted=want; closeWS();
  let sock;
  try{ sock=new WebSocket('wss://stream.binance.com:9443/ws/'+sym.toLowerCase()+'@kline_'+iv); }
  catch(e){ setWsStatus('🟡 实时不可用·轮询兜底'); return; }
  ws=sock;
  sock.onopen=()=>{ wsRetry=0; setWsStatus('🟢 实时 · Binance WS'); };
  sock.onmessage=(ev)=>{
    if(wsWanted!==want) return;                 // 期间已切币/切周期，忽略旧流
    let m; try{ m=JSON.parse(ev.data); }catch(e){ return; }
    const k=m.k; if(!k||!candleSeries) return;
    const t=Math.floor(k.t/1000)-TZ;
    const bar={time:t, open:+k.o, high:+k.h, low:+k.l, close:+k.c};
    try{ candleSeries.update(bar); }catch(e){}  // 逐 tick 更新当前蜡烛，丝滑不重画
    try{ if(volSeries) volSeries.update({time:t, value:+k.v||0, color:(+k.c>=+k.o)?'rgba(14,203,129,0.45)':'rgba(246,70,93,0.45)'}); }catch(e){}
    $('px').textContent=fmt(+k.c);
    $('upAt').textContent='实时 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  };
  sock.onerror=()=>{ try{ sock.close(); }catch(e){} };
  sock.onclose=()=>{ if(wsWanted===want){ wsRetry=Math.min(wsRetry+1,6); setWsStatus('🟡 实时重连中…'); setTimeout(()=>{ if(wsWanted===want) openWS(); }, 1000*wsRetry); } };
}

function nearestRow(tsMs){
  const rows=(lastKline&&lastKline.rows)||[]; if(!rows.length||!rows[0].ts) return null;
  let best=null,bd=Infinity;
  for(const r of rows){ const dd=Math.abs(r.ts-tsMs); if(dd<bd){bd=dd;best=r;} }
  return best;
}

function drawOverlay(){
  if(!chart||!candleSeries||!lastKline) return;
  const rows=lastKline.rows||[];
  const LS=LightweightCharts.LineStyle;
  priceLines.forEach(pl=>{ try{candleSeries.removePriceLine(pl);}catch(e){} }); priceLines=[];
  function addPL(price,color,title,style){
    if(price==null||isNaN(+price)) return;
    priceLines.push(candleSeries.createPriceLine({price:+price,color,lineWidth:(style===LS.Dotted?1:2),lineStyle:style,axisLabelVisible:true,title}));
  }
  const d=lastDec||{}, fac=lastFac||{}, e=entryMid(d), pos=(d.suggested_position_pct||0)>0;
  if(pos){
    if(e) addPL(+e.toFixed(2),'#3b82f6','入场',LS.Dashed);
    addPL(d.stop_loss,'#ea3943','止损',LS.Solid);
    addPL(d.take_profit_ref,'#16c784','止盈',LS.Solid);
  }
  if(fac.price) addPL(+fac.price,'#9aa7b6','现价',LS.Dotted);
  // 自动支撑/阻力（近窗摆动高低 · 细虚线，不抢戏）
  const win=rows.slice(-60);
  if(win.length>5){
    addPL(Math.max(...win.map(r=>r.h)),'rgba(246,70,93,0.35)','阻力',LS.Dashed);
    addPL(Math.min(...win.map(r=>r.l)),'rgba(14,203,129,0.35)','支撑',LS.Dashed);
  }
  // 模拟买卖点 ▲▼
  const mks=[];
  (lastPositions||[]).forEach(p=>{
    if(p.opened_ts&&p.entry_price){ const r=nearestRow(p.opened_ts*1000); if(r) mks.push({time:barTime(r),position:'belowBar',color:'#16c784',shape:'arrowUp',text:'买'}); }
    if(p.status==='closed'&&p.closed_ts&&p.exit_price){ const r=nearestRow(p.closed_ts*1000); if(r){ const isWin=(p.realized_pnl_usdt||0)>=0; mks.push({time:barTime(r),position:'aboveBar',color:isWin?'#16c784':'#ea3943',shape:'arrowDown',text:'卖'}); } }
  });
  mks.sort((a,b)=>a.time-b.time);
  if(markersPrim) markersPrim.setMarkers(mks);
}

async function loadEvents(){
  try{
    const d=await (await fetch('/api/events?symbol='+sym+'&limit=30')).json();
    const evs=d.events||[];
    $('evlist').innerHTML = evs.length ? evs.map(e=>
      `<div class="ev ${e.tone}"><div><div class="tt">${e.title}</div><div class="dd">${e.detail||''}</div></div><div class="t" style="margin-left:auto">${e.time}</div></div>`
    ).join('') : '<div class="empty">暂无模拟盘动作。点右下「自动跟盘」让贾维斯开始跟单。</div>';
  }catch(e){ $('evlist').innerHTML='<div class="empty">事件加载失败</div>'; }
}

async function loadStats(){
  try{
    const st=await (await fetch('/api/trader/status?symbol=')).json();
    lastPositions=await (await fetch('/api/positions?status=all')).json();
    const tp=st.total_pnl_usdt, eq=st.equity_change_pct;
    $('strip').innerHTML=
      `<div class="stat"><div class="k">账户权益</div><div class="v">${fmt(st.equity_usdt)}U</div></div>`+
      `<div class="stat"><div class="k">总盈亏</div><div class="v ${tp>0?'pos':(tp<0?'neg':'')}">${tp>0?'+':''}${fmt(tp)}U</div></div>`+
      `<div class="stat"><div class="k">较起始</div><div class="v ${eq>0?'pos':(eq<0?'neg':'')}">${pct(eq)}</div></div>`+
      `<div class="stat"><div class="k">胜率</div><div class="v">${st.win_rate_pct==null?'—':st.win_rate_pct+'%'}</div></div>`+
      `<div class="stat"><div class="k">盈亏比</div><div class="v">${st.profit_factor??'—'}</div></div>`+
      `<div class="stat"><div class="k">持仓/已平</div><div class="v">${st.open_positions}/${st.closed_trades}</div></div>`+
      `<div class="holds">${(st.open_detail||[]).length?('持仓：'+st.open_detail.map(o=>o.symbol.replace('USDT','')+' @'+fmt(o.entry)+(o.unrealized_usdt!=null?(' '+(o.unrealized_usdt>=0?'+':'')+fmt(o.unrealized_usdt)+'U'):'')).join('，')):'当前无持仓'}</div>`+
      `<div class="actbtn"><button class="ghost" onclick="loadAll()">刷新</button><button onclick="runCycle()" id="cycBtn">▶ 自动跟盘一轮</button></div>`;
    if(chart) drawOverlay();
  }catch(e){ $('strip').innerHTML='<div class="empty">战绩加载失败</div>'; }
}

async function runCycle(){
  const b=$('cycBtn'); if(b){ b.textContent='跟盘中…(约10-30秒)'; b.disabled=true; }
  try{
    const syms=[...new Set([...SYMS, sym])].map(s=>s.replace('USDT','')).join(',');
    await fetch('/api/trader/cycle?symbols='+encodeURIComponent(syms),{method:'POST'});
  }catch(e){}
  if(b){ b.textContent='▶ 自动跟盘一轮'; b.disabled=false; }
  loadEvents(); loadStats();
}

function addMsg(t,who){ const m=document.createElement('div'); m.className='msg '+who; m.textContent=t; $('chat').appendChild(m); $('chat').scrollTop=$('chat').scrollHeight; return m; }
function quick(q){ $('qin').value=q; sendAsk(); }
async function sendAsk(){
  const q=$('qin').value.trim(); if(!q) return; $('qin').value='';
  addMsg(q,'u'); const a=addMsg('贾维斯思考中…','a');
  try{
    const d=await (await fetch('/api/ask?symbol='+sym+'&q='+encodeURIComponent(q),{method:'POST'})).json();
    a.textContent=(d.answer||'我暂时答不上来，换个问法试试。')+(d.engine==='llm'?' 🧠':'');
  }catch(e){ a.textContent='网络抖动，稍后再问我一次。'; }
}

async function loadSnapshot(){
  try{
    const snap=await (await fetch('/api/snapshot?symbol='+sym)).json();
    renderCopilot(snap);
    if(!wsLive()) $('upAt').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  }catch(e){ if(!wsLive()) $('upAt').textContent='刷新失败，下次重试'; }
}

// 侧栏（决策/事件/战绩）走 60s 轮询；K 线蜡烛由 WebSocket 实时推送，二者解耦。
// loadKline 在此仅用于刷新趋势线/档位（key 未变不会重画蜡烛、不扰动 WS）。
function refreshSide(){ loadSnapshot(); loadEvents(); loadStats(); loadKline(); }
function loadAll(){ countdown=REFRESH; loadKline(); refreshSide(); }   // 切币/切周期/首次：重拉历史 K 线并重连 WS
function startTimer(){ if(tick)clearInterval(tick); tick=setInterval(()=>{ countdown--; renderLive(); if(countdown<=0){ countdown=REFRESH; refreshSide(); } },1000); }

renderChips();
addMsg('你好！我是贾维斯。问我「现在该买什么」「卖多少」「为什么这么判断」，或直接点下面的快捷问题。','a');
loadAll();
startTimer();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯可视化仪表盘")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7899)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
