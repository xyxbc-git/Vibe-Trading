#!/usr/bin/env python3
"""贾维斯 JARVIS - P3 因子回测：恐慌贪婪 + 资金费率 反向因子。

验证假设：
  - 当市场「极度恐惧」(F&G 低) 时做多 BTC，是否能跑赢买入持有？
  - 叠加「资金费率转负」(空头拥挤) 过滤，是否能进一步提升？

数据源（全部免费、免 Key、可真实回测）：
  - Binance Spot K线：BTCUSDT 日线收盘价
  - alternative.me：恐慌贪婪指数全历史（2018 至今）
  - Binance Futures：资金费率历史（8h，约最近 1 年）

防前瞻：第 t 日仓位只用第 t-1 日收盘时已知的信号决定。
不构成交易建议，仅用于研究因子是否具备统计优势。

用法：
  python jarvis_factor_backtest.py
  python jarvis_factor_backtest.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

import jarvis_net

SPOT_API = "https://api.binance.com"
FAPI = "https://fapi.binance.com"
FNG_API = "https://api.alternative.me/fng/"
TIMEOUT = 20
_HEADERS = {"User-Agent": "jarvis-factor-backtest/1.0"}


def _get(url: str, params: Optional[dict] = None, retries: int = 4) -> Any:
    delay = 1.5
    last_err = None
    jarvis_net.ensure_proxy()
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=TIMEOUT)
            if r.status_code in (418, 429):
                last_err = f"HTTP {r.status_code}"
                time.sleep(delay)
                delay *= 2
                continue
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)[:200]
            jarvis_net.ensure_proxy(force=True)
            time.sleep(delay)
            delay *= 2
    return {"_error": last_err}


def _day(ts_seconds: float) -> str:
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_price_daily(symbol: str = "BTCUSDT", start: str = "2018-02-01") -> dict:
    """日线收盘价全历史，返回 {date: close}。

    Binance 单次上限 1000 根，故从 start 起按 startTime 分页向前翻，直至当前。
    """
    out: dict[str, float] = {}
    start_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    one_day_ms = 86_400_000
    cursor = start_ms
    now_ms = int(time.time() * 1000)
    for _ in range(20):  # 最多 20 页 (2 万天，足够)
        kl = _get(f"{SPOT_API}/api/v3/klines",
                  {"symbol": symbol, "interval": "1d", "startTime": cursor, "limit": 1000})
        if not isinstance(kl, list) or not kl:
            break
        for row in kl:
            out[_day(row[0] / 1000)] = float(row[4])  # close
        last_open = kl[-1][0]
        if len(kl) < 1000 or last_open + one_day_ms >= now_ms:
            break
        cursor = last_open + one_day_ms
    return out


def fetch_fng_all() -> dict:
    """恐慌贪婪指数全历史，返回 {date: value}。"""
    out: dict[str, int] = {}
    d = _get(FNG_API, {"limit": 0})
    if isinstance(d, dict) and d.get("data"):
        for row in d["data"]:
            out[_day(int(row["timestamp"]))] = int(row["value"])
    return out


def fetch_funding_daily(symbol: str = "BTCUSDT") -> dict:
    """资金费率历史(8h)聚合成 {date: 当日均费率}。Binance 单次上限 1000 条≈333 天。"""
    out: dict[str, list] = {}
    hist = _get(f"{FAPI}/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1000})
    if isinstance(hist, list):
        for row in hist:
            day = _day(int(row["fundingTime"]) / 1000)
            out.setdefault(day, []).append(float(row["fundingRate"]))
    return {day: sum(v) / len(v) for day, v in out.items()}


# ----------------------------------------------------------------------------
# 回测引擎
# ----------------------------------------------------------------------------

def _max_drawdown(equity: list) -> float:
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return mdd


def _sharpe(daily_rets: list) -> float:
    if len(daily_rets) < 2:
        return 0.0
    mean = sum(daily_rets) / len(daily_rets)
    var = sum((r - mean) ** 2 for r in daily_rets) / (len(daily_rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return mean / sd * math.sqrt(365)


def _default_cost_bps() -> float:
    """[D1] 回测默认换仓成本：读 jarvis_config 的 backtest_cost_bps（默认单边 10bps，
    与 jarvis_slippage.FALLBACK_BPS 同口径）。配置层异常回退 10，永不抛出。"""
    try:
        import jarvis_config as jcfg
        return float(jcfg.get("backtest_cost_bps"))
    except Exception:  # noqa: BLE001 — 配置不可用不拖垮回测
        return 10.0


def backtest(dates: list, prices: dict, fng: dict, funding: dict,
             fear_thresh: int, greed_thresh: int, use_funding: bool,
             allow_short: bool, cost_bps: float | None = None) -> dict:
    """逐日回测。仓位由前一日信号决定（防前瞻）。

    多头条件: F&G < fear_thresh (且若 use_funding，要求当日资金费率 <= 0)
    空头/离场: F&G > greed_thresh (allow_short=True 则做空，否则空仓)
    其余: 维持上一仓位（持有惯性）。

    [T-07] cost_bps：每次换仓（开/平/翻转）扣减的单边滑点成本（bps）。
    [D1] 默认 None = 读 jarvis_config.backtest_cost_bps（内置 10bps，绩效不再虚高）；
    显式传 0 可复现历史 P3/P4 零成本口径（零回归）；传正值即得「滑点后 edge」。
    """
    if cost_bps is None:
        cost_bps = _default_cost_bps()
    pos = 0  # -1 / 0 / 1
    strat_equity = [1.0]
    bh_equity = [1.0]
    strat_rets: list[float] = []
    days_in_market = 0
    win_days = 0
    trades = 0
    prev_pos = 0

    for i in range(1, len(dates)):
        d_prev = dates[i - 1]
        d_cur = dates[i]
        # 用前一日信号决定今日仓位
        fg_prev = fng.get(d_prev)
        fund_prev = funding.get(d_prev)
        new_pos = pos
        if fg_prev is not None:
            if fg_prev < fear_thresh:
                if (not use_funding) or (fund_prev is not None and fund_prev <= 0) or (fund_prev is None):
                    new_pos = 1
            elif fg_prev > greed_thresh:
                new_pos = -1 if allow_short else 0
        turned = new_pos != prev_pos
        if turned:
            trades += 1
        prev_pos = new_pos
        pos = new_pos

        ret = prices[d_cur] / prices[d_prev] - 1.0
        strat_ret = pos * ret
        # [T-07] 换仓滑点成本：仓位变动当日按单边 cost_bps 扣减（开/平/翻转各计一次）。
        if turned and cost_bps:
            strat_ret -= float(cost_bps) / 10000.0
        strat_rets.append(strat_ret)
        strat_equity.append(strat_equity[-1] * (1 + strat_ret))
        bh_equity.append(bh_equity[-1] * (1 + ret))
        if pos != 0:
            days_in_market += 1
            if strat_ret > 0:
                win_days += 1

    n = len(dates) - 1
    years = n / 365.0 if n else 1
    strat_total = strat_equity[-1] - 1
    bh_total = bh_equity[-1] - 1
    strat_cagr = strat_equity[-1] ** (1 / years) - 1 if years > 0 else 0
    bh_cagr = bh_equity[-1] ** (1 / years) - 1 if years > 0 else 0
    return {
        "fear_thresh": fear_thresh,
        "greed_thresh": greed_thresh,
        "use_funding": use_funding,
        "allow_short": allow_short,
        "cost_bps": cost_bps,
        "days": n,
        "days_in_market": days_in_market,
        "exposure_pct": round(days_in_market / n * 100, 1) if n else 0,
        "trades": trades,
        "strat_total_return_pct": round(strat_total * 100, 2),
        "bh_total_return_pct": round(bh_total * 100, 2),
        "excess_vs_bh_pct": round((strat_total - bh_total) * 100, 2),
        "strat_cagr_pct": round(strat_cagr * 100, 2),
        "bh_cagr_pct": round(bh_cagr * 100, 2),
        "strat_max_drawdown_pct": round(_max_drawdown(strat_equity) * 100, 2),
        "bh_max_drawdown_pct": round(_max_drawdown(bh_equity) * 100, 2),
        "strat_sharpe": round(_sharpe(strat_rets), 2),
        "win_rate_in_market_pct": round(win_days / days_in_market * 100, 1) if days_in_market else 0,
    }


def event_study(dates: list, prices: dict, fng: dict, horizons=(7, 30, 90)) -> dict:
    """事件研究：买入「极度恐惧」后未来 N 天收益 vs 无条件平均(基线)。

    这是判断逆向因子是否有 edge 的正确方法 —— 不受择时进出场逻辑干扰，
    直接看「在恐惧时点买入」的前瞻收益是否系统性高于随机时点。
    """
    idx = {d: i for i, d in enumerate(dates)}
    closes = [prices[d] for d in dates]

    def fwd(i: int, h: int):
        j = i + h
        if j >= len(closes):
            return None
        return closes[j] / closes[i] - 1.0

    result: dict[str, Any] = {}
    for thresh in (20, 25):
        block: dict[str, Any] = {}
        for h in horizons:
            cond, base = [], []
            for i, d in enumerate(dates):
                r = fwd(i, h)
                if r is None:
                    continue
                base.append(r)
                v = fng.get(d)
                if v is not None and v < thresh:
                    cond.append(r)
            if cond and base:
                c_mean = sum(cond) / len(cond)
                b_mean = sum(base) / len(base)
                block[f"h{h}"] = {
                    "n_events": len(cond),
                    "cond_mean_ret_pct": round(c_mean * 100, 2),
                    "baseline_mean_ret_pct": round(b_mean * 100, 2),
                    "edge_pct": round((c_mean - b_mean) * 100, 2),
                    "cond_win_rate_pct": round(sum(1 for r in cond if r > 0) / len(cond) * 100, 1),
                    "baseline_win_rate_pct": round(sum(1 for r in base if r > 0) / len(base) * 100, 1),
                }
        result[f"fng_below_{thresh}"] = block
    return result


def _build_series(dates: list, prices: dict):
    """返回 closes / 200日均线 / 距滚动历史高点回撤 三条对齐序列。

    dates 为空时返回三条空列表（避免 dashboard /api/series 在外部行情拉取失败时 500）。
    """
    closes = [prices[d] for d in dates]
    if not closes:
        return [], [], []
    ma200: list[Optional[float]] = []
    dd: list[float] = []
    peak = closes[0]
    for i, c in enumerate(closes):
        ma200.append(sum(closes[i - 199:i + 1]) / 200 if i >= 199 else None)
        peak = max(peak, c)
        dd.append(c / peak - 1.0)
    return closes, ma200, dd


def event_study_advanced(dates: list, prices: dict, fng: dict, horizon: int = 30) -> dict:
    """第二个因子：趋势过滤 + 回撤因子。统一用 horizon 天前瞻收益。

    验证两件事：
      1) 把「极度恐惧」按 200 日均线拆成「牛市恐惧 vs 熊市恐惧」，看 edge 集中在哪。
      2) 「距历史高点回撤」本身是不是独立 edge；与恐惧叠加是否更强。
    """
    closes, ma200, dd = _build_series(dates, prices)

    def fwd(i: int):
        j = i + horizon
        return closes[j] / closes[i] - 1.0 if j < len(closes) else None

    base = [r for i in range(len(dates)) if (r := fwd(i)) is not None]
    b_mean = sum(base) / len(base) if base else 0.0
    b_win = sum(1 for r in base if r > 0) / len(base) * 100 if base else 0.0

    def stat(name: str, cond_fn) -> dict:
        cond = []
        for i, d in enumerate(dates):
            r = fwd(i)
            if r is None or ma200[i] is None:
                continue
            if cond_fn(i, d):
                cond.append(r)
        if not cond:
            return {"name": name, "n_events": 0}
        c_mean = sum(cond) / len(cond)
        return {
            "name": name,
            "n_events": len(cond),
            "cond_mean_ret_pct": round(c_mean * 100, 2),
            "edge_pct": round((c_mean - b_mean) * 100, 2),
            "cond_win_rate_pct": round(sum(1 for r in cond if r > 0) / len(cond) * 100, 1),
        }

    fg = lambda i, d, t=20: (fng.get(d) is not None and fng.get(d) < t)  # noqa: E731
    rows = [
        stat("F&G<20 & 价>200MA(牛市恐惧)", lambda i, d: fg(i, d) and closes[i] > ma200[i]),
        stat("F&G<20 & 价<200MA(熊市恐惧)", lambda i, d: fg(i, d) and closes[i] < ma200[i]),
        stat("回撤≤-30%", lambda i, d: dd[i] <= -0.30),
        stat("回撤≤-50%", lambda i, d: dd[i] <= -0.50),
        stat("F&G<20 & 价>200MA & 回撤≤-20%", lambda i, d: fg(i, d) and closes[i] > ma200[i] and dd[i] <= -0.20),
    ]
    return {
        "horizon": horizon,
        "baseline_mean_ret_pct": round(b_mean * 100, 2),
        "baseline_win_rate_pct": round(b_win, 1),
        "rows": rows,
    }


def run(cost_bps: float | None = None) -> dict:
    """[D1] cost_bps=None 时按 jarvis_config.backtest_cost_bps（默认 10bps）扣成本。"""
    if cost_bps is None:
        cost_bps = _default_cost_bps()
    prices = fetch_price_daily()
    fng = fetch_fng_all()
    funding = fetch_funding_daily()
    # 只回测三者(价格+情绪)都覆盖的日期；资金费率历史短，作为可选过滤
    dates = sorted(set(prices) & set(fng))
    if len(dates) < 30:
        return {"_error": "数据不足", "price_days": len(prices), "fng_days": len(fng)}

    scenarios = []
    # 基线：纯恐慌贪婪反向(多/空仓)
    for fear in (20, 25, 30):
        scenarios.append(backtest(dates, prices, fng, funding, fear, 80, False, False, cost_bps))
    # 叠加资金费率过滤
    scenarios.append(backtest(dates, prices, fng, funding, 25, 80, True, False, cost_bps))
    # 允许做空(极度贪婪时做空)
    scenarios.append(backtest(dates, prices, fng, funding, 25, 75, False, True, cost_bps))

    return {
        "symbol": "BTCUSDT",
        "fetched_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "sample_start": dates[0],
        "sample_end": dates[-1],
        "sample_days": len(dates),
        "funding_history_days": len(funding),
        "cost_bps": cost_bps,
        "scenarios": scenarios,
        "event_study": event_study(dates, prices, fng),
        "factor2": event_study_advanced(dates, prices, fng, horizon=30),
    }


def to_markdown(r: dict) -> str:
    if "_error" in r:
        return f"回测失败: {r}"
    lines = [
        f"## P3 因子回测 — 恐慌贪婪 + 资金费率反向因子 ({r['symbol']})",
        "",
        f"样本: {r['sample_start']} → {r['sample_end']}  ({r['sample_days']} 天) | 资金费率历史 {r['funding_history_days']} 天",
        "",
        "| 策略 | 暴露% | 交易 | 策略收益 | 买持收益 | 超额 | 策略回撤 | 买持回撤 | 夏普 | 在场胜率 |",
        "|------|------|------|---------|---------|------|---------|---------|------|---------|",
    ]
    for s in r["scenarios"]:
        name = f"F&G<{s['fear_thresh']}"
        if s["use_funding"]:
            name += "+负费率"
        if s["allow_short"]:
            name += f"/>{s['greed_thresh']}做空"
        lines.append(
            f"| {name} | {s['exposure_pct']} | {s['trades']} | {s['strat_total_return_pct']}% | "
            f"{s['bh_total_return_pct']}% | {s['excess_vs_bh_pct']}% | {s['strat_max_drawdown_pct']}% | "
            f"{s['bh_max_drawdown_pct']}% | {s['strat_sharpe']} | {s['win_rate_in_market_pct']}% |"
        )
    es = r.get("event_study", {})
    if es:
        lines += [
            "",
            "### 事件研究：买入极度恐惧后的前瞻收益 vs 无条件平均",
            "",
            "| 条件 | 持有 | 事件数 | 条件后平均收益 | 基线平均收益 | **超额edge** | 条件胜率 | 基线胜率 |",
            "|------|------|------|------|------|------|------|------|",
        ]
        for cond_name, block in es.items():
            label = cond_name.replace("fng_below_", "F&G<")
            for h, m in block.items():
                lines.append(
                    f"| {label} | {h[1:]}天 | {m['n_events']} | {m['cond_mean_ret_pct']}% | "
                    f"{m['baseline_mean_ret_pct']}% | **{m['edge_pct']}%** | "
                    f"{m['cond_win_rate_pct']}% | {m['baseline_win_rate_pct']}% |"
                )
    f2 = r.get("factor2", {})
    if f2 and f2.get("rows"):
        lines += [
            "",
            f"### 因子2：趋势过滤 + 回撤因子（前瞻 {f2['horizon']} 天，基线 {f2['baseline_mean_ret_pct']}% / 胜率 {f2['baseline_win_rate_pct']}%）",
            "",
            "| 条件 | 事件数 | 条件后收益 | **超额edge** | 条件胜率 |",
            "|------|------|------|------|------|",
        ]
        for m in f2["rows"]:
            if m.get("n_events", 0) == 0:
                lines.append(f"| {m['name']} | 0 | - | - | - |")
            else:
                lines.append(
                    f"| {m['name']} | {m['n_events']} | {m['cond_mean_ret_pct']}% | "
                    f"**{m['edge_pct']}%** | {m['cond_win_rate_pct']}% |"
                )
    lines += [
        "",
        "> 防前瞻：每日仓位仅用前一日已知信号。数据真实拉取（Binance + alternative.me），非估算。",
        "> 仅研究因子统计特性，不构成交易建议。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 P3 因子回测")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--slippage-bps", type=float, default=None,
                    help="[T-07/D1] 每次换仓单边滑点成本(bps)；缺省读配置 backtest_cost_bps"
                         "（内置默认 10）；显式传 0 复现历史零成本口径")
    args = ap.parse_args()
    r = run(cost_bps=args.slippage_bps)
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else to_markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
