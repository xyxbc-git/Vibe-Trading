#!/usr/bin/env python3
"""贾维斯 JARVIS - 闭环决策简报（数据 → 因子 → 风控交易计划）。

把前面跑通的能力串成一条闭环：
  1) 拉真实数据（jarvis_crypto_data：资金费率/OI/多空比/情绪/链上/市场结构）
  2) 算当前「已验证因子」状态（距高点回撤、F&G、200日均线位置）
  3) 给每个因子标上 P3/P4 验证过的真实 edge（含过拟合警示）
  4) 汇成信心分 → 输出带仓位/止损/时间止损的风控交易计划

设计原则（来自 P4 教训）：
  - 不夸大 edge：-30% 回撤是「弱因子」(样本外 +0.46%)，只给小仓位倾斜。
  - -50% 回撤被标为过拟合，不参与决策。
  - 弱信号 + 强风控，而非强信号梭哈。

用法：
  python jarvis_brief.py BTCUSDT
  python jarvis_brief.py BTCUSDT --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import jarvis_crypto_data as jcd
import jarvis_lessons as jl
import jarvis_weights as jw
from jarvis_factor_backtest import _build_series, fetch_fng_all, fetch_price_daily


# 已验证因子的真实 edge（来自 P3/P4，30 天前瞻）
VERIFIED_EDGES = {
    "dd30": {"label": "距高点回撤≤-30%", "oos_edge_pct": 0.46, "oos_win_pct": 55.4, "trust": "弱但样本外稳健"},
    "dd50": {"label": "距高点回撤≤-50%", "oos_edge_pct": -0.03, "oos_win_pct": 52.3, "trust": "过拟合，不采用"},
    "fear_down": {"label": "F&G<20 且 下跌趋势", "win_pct": 62.9, "trust": "样本内强，未单独做OOS，谨慎"},
}


def current_factor_state(symbol_base: str) -> dict:
    """计算当前价格相关因子：距滚动高点回撤、相对200日均线、近30日动量。"""
    spot_symbol = symbol_base + "USDT"
    prices = fetch_price_daily(spot_symbol)
    dates = sorted(prices)
    if len(dates) < 210:
        return {"_error": "价格历史不足"}
    closes, ma200, dd = _build_series(dates, prices)
    last = len(dates) - 1
    cur_price = closes[last]
    cur_dd = dd[last]
    cur_ma200 = ma200[last]
    mom30 = closes[last] / closes[last - 30] - 1.0 if last >= 30 else None
    return {
        "as_of": dates[last],
        "price": round(cur_price, 2),
        "drawdown_from_ath_pct": round(cur_dd * 100, 2),
        "ma200": round(cur_ma200, 2) if cur_ma200 else None,
        "above_ma200": cur_price > cur_ma200 if cur_ma200 else None,
        "momentum_30d_pct": round(mom30 * 100, 2) if mom30 is not None else None,
        "dd30_signal_active": cur_dd <= -0.30,
        "dd50_signal_active": cur_dd <= -0.50,
        "breakout_20d_active": (cur_price >= max(closes[last - 19:last + 1])) if last >= 19 else False,
    }


def score_and_plan(deriv: dict, fac: dict, fng_now: int) -> dict:
    """把因子状态汇成信心分，再翻译成带风控的交易计划。

    因子权重与方向阈值从 jarvis_weights 读取（可被重训覆盖）；配置不存在时
    回退内置默认（= 历史硬编码原值），保证零回归。
    """
    reasons: list[str] = []
    attribution: list[dict] = []  # 因子归因：每项对信心分的带符号贡献
    score = 0.0  # 正=偏多, 负=偏空，范围约 [-2, 2]
    W = jw.get_weights()

    def _add(name: str, value: float, note: str) -> None:
        nonlocal score
        score += value
        attribution.append({"factor": name, "contribution": round(value, 2), "note": note})
        reasons.append(note)

    # 因子1：回撤抄底（弱因子，小权重）
    if fac.get("dd30_signal_active"):
        _add("深度回撤抄底", W["dd30_dip"], f"距高点回撤 {fac['drawdown_from_ath_pct']}% ≤-30%（弱抄底因子，样本外 +0.46%/胜率55%）")
    # 因子2：极度恐惧 + 下跌趋势（反弹）
    below_ma = fac.get("above_ma200") is False
    if fng_now < 20 and below_ma:
        _add("下跌中恐惧", W["fear_in_downtrend"], f"F&G {fng_now}<20 且 价<200MA（下跌中恐惧，历史30天胜率62.9%）")
    elif fng_now < 20 and fac.get("above_ma200"):
        _add("牛市中恐惧", W["fear_in_uptrend"], f"F&G {fng_now}<20 但 价>200MA（牛市中恐惧，历史为利空 edge-6.6%）")

    # 衍生品确认/反驳
    f = deriv.get("funding", {})
    regime = f.get("funding_regime", "")
    if "overheated_short" in regime:
        _add("资金费率转负", W["funding_overheated_short"], "资金费率深度转负（空头拥挤，易轧空）")
    elif "overheated_long" in regime:
        _add("资金费率过热", W["funding_overheated_long"], "资金费率过热（多头拥挤，回调风险）")

    ls = deriv.get("long_short", {})
    g7 = ls.get("global_ls_7d")
    if isinstance(g7, list) and len(g7) >= 2 and g7[-1] < g7[0]:
        reasons.append(f"多空比 7 日下降 {g7[0]}→{g7[-1]}（多头去杠杆）")

    # 200MA 大方向
    if fac.get("above_ma200"):
        _add("200MA 多头结构", W["ma200_above"], "价格在 200 日均线之上（中期多头结构）")
    else:
        _add("200MA 偏弱结构", W["ma200_below"], "价格在 200 日均线之下（中期偏弱）")

    # 因子（T-04 新增）：20日新高突破（正交动量因子，已验证 OOS+蒙卡）
    if fac.get("breakout_20d_active"):
        _add("20日新高突破", W["breakout_20d"], "创20日新高·正交动量因子（edge+2.95%/OOS+1.79%/蒙卡p=0.0016）")
    score = round(max(-2.0, min(2.0, score)), 2)

    # 信心分 → 方向 + 仓位（弱因子，仓位上限保守）
    TH = jw.get_thresholds()
    if score >= TH["long"]:
        direction, base_pos = "偏多（战术）", min(0.4, 0.2 + score * 0.1)
    elif score <= TH["short"]:
        direction, base_pos = "偏空/观望", 0.0
    else:
        direction, base_pos = "中性观望", 0.1 if score > 0 else 0.0

    price = fac.get("price")
    plan = {
        "conviction_score": score,
        "direction": direction,
        "suggested_position_pct": round(base_pos * 100, 0),
        "reasons": reasons,
        "attribution": attribution,
    }
    if price and base_pos > 0:
        plan["entry_zone"] = f"{round(price*0.985,2)} ~ {round(price*1.005,2)}"
        plan["stop_loss"] = round(price * 0.90, 2)  # -10% 硬止损
        plan["take_profit_ref"] = round(price * 1.08, 2)  # 参考 +8%
        plan["time_stop_days"] = 30  # 因子是 30 天均值回归，过期离场
        plan["max_risk_pct"] = round(base_pos * 10, 1)  # 仓位×止损幅度=组合最大风险
        # [T-08] 期望值计入永续 funding 成本：弱 edge 可能被持仓期 funding 费吃光。
        plan["expected_value"] = _expected_value(
            deriv, score, tp_pct=8.0, sl_pct=-10.0, hold_days=plan["time_stop_days"]
        )
        ev = plan["expected_value"]
        if ev.get("net_ev_pct") is not None and ev["net_ev_pct"] <= 0:
            reasons.append(
                f"⚠ funding 成本 {ev['funding_cost_pct']}% 吃光 edge：净期望 {ev['net_ev_pct']}% ≤0，不建议持有 30 天"
            )
        elif ev.get("funding_drag_share_pct") is not None and ev["funding_drag_share_pct"] >= 30:
            reasons.append(
                f"funding 成本侵蚀 {ev['funding_drag_share_pct']}% 毛期望（净 {ev['net_ev_pct']}%），弱 edge 注意"
            )
    return plan


def _expected_value(deriv: dict, score: float, tp_pct: float, sl_pct: float, hold_days: int) -> dict:
    """[T-08] 多头交易的期望值，扣减持仓期永续 funding 成本。

    - 胜率 p 由信心分启发式映射（0.5 基线，越强偏多 p 越高，封顶 0.66，贴近历史强因子胜率）。
    - 毛期望 gross = p·tp + (1-p)·sl（tp 正、sl 负，单位 %）。
    - funding 成本：对多头，正费率=付费（拖累）。fwd 8h 费率优先取 7 日均，× 持仓周期 intervals(3/天)。
    - 净期望 net = gross − funding_cost；funding 缺数据时 net=gross 并标注。
    """
    p = max(0.5, min(0.66, 0.5 + max(0.0, score) * 0.06))
    gross = round(p * tp_pct + (1 - p) * sl_pct, 3)

    f = deriv.get("funding", {}) if isinstance(deriv, dict) else {}
    fwd_8h = f.get("funding_7d_avg_8h_pct")
    if fwd_8h is None:
        fwd_8h = f.get("last_funding_rate_8h_pct")
    out = {
        "win_prob": round(p, 3),
        "take_profit_pct": tp_pct,
        "stop_loss_pct": sl_pct,
        "gross_ev_pct": gross,
        "hold_days": hold_days,
    }
    if fwd_8h is None:
        out["funding_8h_pct"] = None
        out["funding_cost_pct"] = None
        out["net_ev_pct"] = gross
        out["funding_drag_share_pct"] = None
        out["note"] = "无 funding 数据，净期望未扣 funding 成本"
        return out
    intervals = hold_days * 3  # 永续每 8h 一次，3 次/天
    funding_cost = round(float(fwd_8h) * intervals, 3)  # 多头：正费率=正成本（拖累）
    net = round(gross - funding_cost, 3)
    out["funding_8h_pct"] = round(float(fwd_8h), 5)
    out["funding_intervals"] = intervals
    out["funding_cost_pct"] = funding_cost
    out["net_ev_pct"] = net
    out["funding_drag_share_pct"] = round(funding_cost / gross * 100, 1) if gross > 0 else None
    return out


def build(symbol: str) -> dict:
    base = symbol.upper().replace("USDT", "").replace("-", "").replace("/", "")
    deriv = jcd.collect(base + "USDT")
    fac = current_factor_state(base)
    fng_now = deriv.get("fear_greed", {}).get("fng_value", 50)
    plan = score_and_plan(deriv, fac, fng_now) if "_error" not in fac else {"_error": fac["_error"]}
    # [补完-6] 错误记忆回喂：把适用于当前情形的历史教训挂到决策上，主动援引。
    if "_error" not in fac and "_error" not in plan:
        try:
            plan["lessons"] = jl.applicable_lessons(
                fac, deriv, plan.get("direction"), symbol=base + "USDT"
            )
        except Exception as exc:
            plan["lessons"] = [{
                "id": "lessons_unavailable",
                "title": "教训库加载失败",
                "advice": str(exc),
                "severity": "low",
                "source": "static",
            }]
    return {
        "symbol": base + "USDT",
        "generated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "real_data": deriv,
        "factor_state": fac,
        "decision": plan,
    }


def to_markdown(b: dict) -> str:
    fac = b["factor_state"]
    d = b["decision"]
    deriv = b["real_data"]
    f = deriv.get("funding", {})
    fng = deriv.get("fear_greed", {})
    lines = [
        f"# 贾维斯决策简报 — {b['symbol']}  ({b['generated_at_utc']} UTC)",
        "",
        "## 一、当前真实数据快照",
        f"- 价格 {fac.get('price','N/A')} | 距历史高点回撤 {fac.get('drawdown_from_ath_pct','N/A')}% | "
        f"200MA {fac.get('ma200','N/A')}（{'之上' if fac.get('above_ma200') else '之下'}）| 30日动量 {fac.get('momentum_30d_pct','N/A')}%",
        f"- 资金费率8h {f.get('last_funding_rate_8h_pct','N/A')}%（{f.get('funding_regime','N/A')}）| "
        f"恐慌贪婪 {fng.get('fng_value','N/A')}（{fng.get('fng_class','N/A')}）",
        "",
        "## 二、已验证因子当前状态",
        f"- 回撤≤-30% 因子：{'✅ 触发' if fac.get('dd30_signal_active') else '— 未触发'}  "
        f"（{VERIFIED_EDGES['dd30']['trust']}，样本外 edge +{VERIFIED_EDGES['dd30']['oos_edge_pct']}%）",
        f"- 回撤≤-50% 因子：{'触发但' if fac.get('dd50_signal_active') else '未触发，'}**已判定过拟合，不参与决策**",
        "",
        "## 三、决策（弱信号 + 强风控）",
    ]
    if "_error" in d:
        lines.append(f"- 无法决策：{d['_error']}")
        return "\n".join(lines)
    lines += [
        f"- **信心分: {d['conviction_score']}（范围 -2~+2）→ {d['direction']}**",
        f"- 建议仓位: **{d['suggested_position_pct']}%**（弱因子，刻意保守）",
    ]
    if d.get("suggested_position_pct", 0) > 0:
        lines += [
            f"- 入场区间: {d.get('entry_zone')}",
            f"- 硬止损: {d.get('stop_loss')}（-10%）| 参考止盈: {d.get('take_profit_ref')}（+8%）",
            f"- 时间止损: {d.get('time_stop_days')} 天（因子是 30 天均值回归，过期离场）",
            f"- 组合最大风险敞口: 约 {d.get('max_risk_pct')}%",
        ]
        ev = d.get("expected_value")
        if isinstance(ev, dict):
            cost = ev.get("funding_cost_pct")
            cost_str = f"{cost}%（{ev.get('funding_intervals')}期×{ev.get('funding_8h_pct')}%）" if cost is not None else "无数据"
            lines.append(
                f"- 期望值(T-08): 毛期望 {ev.get('gross_ev_pct')}%（胜率{ev.get('win_prob')}）− funding 成本 {cost_str} "
                f"= **净期望 {ev.get('net_ev_pct')}%**"
                + (f"，funding 侵蚀 {ev.get('funding_drag_share_pct')}%" if ev.get('funding_drag_share_pct') is not None else "")
            )
    lines += ["", "### 依据"]
    lines += [f"- {r}" for r in d.get("reasons", [])]
    lessons = d.get("lessons", [])
    if lessons:
        sev_icon = {"high": "🔴", "medium": "🟠", "low": "🔵"}
        lines += ["", "### 历史教训（错误记忆回喂）"]
        for l in lessons:
            lines.append(f"- {sev_icon.get(l.get('severity'), '·')} **{l.get('title','')}** — {l.get('advice','')}")
    lines += [
        "",
        "> 全部数据真实拉取（Binance/CoinGecko/alternative.me/链上），因子 edge 均经 P3 回测 + P4 样本外验证。",
        "> edge 偏弱，本简报刻意以小仓位 + 硬止损 + 时间止损控制风险，不构成交易建议。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯闭环决策简报")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    b = build(args.symbol)
    print(json.dumps(b, ensure_ascii=False, indent=2) if args.json else to_markdown(b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
