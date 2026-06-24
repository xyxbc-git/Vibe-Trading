#!/usr/bin/env python3
"""贾维斯 JARVIS — 回测结果分析器（S-04 "验尸官"）。

拿到 QD 回测结果后，自动归类亏损原因，生成结构化报告供 LLM 读取。
分析器不做任何决策，只做客观诊断。

亏损原因分类：
  - chasing_trapped：追涨被套（入场后 3 根内即触止损）
  - stop_too_loose：止损太松（最大浮盈超止盈50%但最终亏损）
  - fee_erosion：手续费侵蚀（毛收益正但净收益负）
  - direction_whipsaw：方向反复（连续 3+ 次同方向亏损）
  - time_stop_loss：时间止损浮亏（持仓到期仍浮亏）
  - short_squeeze：反向轧空（做空在上涨趋势中被轧）

输出：结构化 JSON 报告，含：
  - verdict（PASS / FAIL / MARGINAL）
  - 每种亏损原因的次数和占比
  - 改进建议（基于规则生成）

用法：
  python jarvis_scalper_analyzer.py analyze --result result.json
  python jarvis_scalper_analyzer.py analyze --result result.json --output report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


# ═══════════════════════════ 达标条件 ═══════════════════════════

DEFAULT_PASS_CRITERIA = {
    "min_win_rate": 52.0,
    "min_profit_factor": 1.2,
    "max_drawdown_pct": 15.0,
    "min_total_return_pct": 0.0,
    "min_trades": 20,
}


# ═══════════════════════════ 亏损归类 ═══════════════════════════

def _classify_trade_losses(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """逐笔分析亏损交易，归类原因。"""
    categories = {
        "chasing_trapped": {"count": 0, "trades": [], "label": "追涨被套"},
        "stop_too_loose": {"count": 0, "trades": [], "label": "止损太松"},
        "fee_erosion": {"count": 0, "trades": [], "label": "手续费侵蚀"},
        "direction_whipsaw": {"count": 0, "trades": [], "label": "方向反复"},
        "time_stop_loss": {"count": 0, "trades": [], "label": "时间止损浮亏"},
        "short_squeeze": {"count": 0, "trades": [], "label": "反向轧空"},
        "other": {"count": 0, "trades": [], "label": "其他亏损"},
    }

    losing_trades = [t for t in trades if _get_pnl(t) < 0]
    if not losing_trades:
        return categories

    # 追涨被套：入场后很快就亏（持仓 bar 数 <= 3）
    for t in losing_trades:
        bars = t.get("bars_held", t.get("barsHeld", 0))
        if bars <= 3:
            categories["chasing_trapped"]["count"] += 1
            categories["chasing_trapped"]["trades"].append(_trade_summary(t))

    # 止损太松：最大浮盈超过止盈目标的 50% 但最终亏损
    for t in losing_trades:
        max_profit = t.get("max_favorable", t.get("maxFavorable", 0))
        tp_target = abs(t.get("take_profit", t.get("takeProfit", 0)))
        if tp_target > 0 and max_profit > tp_target * 0.5:
            categories["stop_too_loose"]["count"] += 1
            categories["stop_too_loose"]["trades"].append(_trade_summary(t))

    # 手续费侵蚀：毛收益正但净收益负
    for t in trades:
        gross = t.get("gross_pnl", t.get("grossPnl", _get_pnl(t)))
        net = _get_pnl(t)
        fee = t.get("commission", t.get("fee", 0))
        if gross > 0 and net < 0:
            categories["fee_erosion"]["count"] += 1
            categories["fee_erosion"]["trades"].append(_trade_summary(t))

    # 方向反复：连续 3+ 次同方向亏损
    consecutive = 0
    last_dir = None
    for t in trades:
        direction = t.get("direction", t.get("side", "long"))
        pnl = _get_pnl(t)
        if pnl < 0 and direction == last_dir:
            consecutive += 1
        else:
            consecutive = 1 if pnl < 0 else 0
        last_dir = direction if pnl < 0 else None
        if consecutive >= 3:
            categories["direction_whipsaw"]["count"] += 1
            categories["direction_whipsaw"]["trades"].append(_trade_summary(t))

    # 时间止损浮亏：退出原因是超时且亏损
    for t in losing_trades:
        exit_reason = t.get("exit_reason", t.get("exitReason", ""))
        if "time" in str(exit_reason).lower() or "expire" in str(exit_reason).lower():
            categories["time_stop_loss"]["count"] += 1
            categories["time_stop_loss"]["trades"].append(_trade_summary(t))

    # 反向轧空：做空方向且亏损
    for t in losing_trades:
        direction = t.get("direction", t.get("side", "long"))
        if direction in ("short", "sell"):
            categories["short_squeeze"]["count"] += 1
            categories["short_squeeze"]["trades"].append(_trade_summary(t))

    # 未归类的亏损
    classified_ids = set()
    for cat in categories.values():
        for ts in cat.get("trades", []):
            classified_ids.add(ts.get("index", -1))
    for i, t in enumerate(losing_trades):
        if i not in classified_ids:
            categories["other"]["count"] += 1

    return categories


def _get_pnl(trade: dict) -> float:
    return float(trade.get("pnl", trade.get("profit", trade.get("net_pnl", 0))))


def _trade_summary(trade: dict) -> dict[str, Any]:
    return {
        "index": trade.get("index", trade.get("id", 0)),
        "pnl": _get_pnl(trade),
        "bars_held": trade.get("bars_held", trade.get("barsHeld", 0)),
        "direction": trade.get("direction", trade.get("side", "unknown")),
    }


# ═══════════════════════════ 改进建议 ═══════════════════════════

def _generate_hints(
    loss_breakdown: dict[str, dict[str, Any]],
    result: dict[str, Any],
) -> list[str]:
    """基于亏损分类生成改进建议。"""
    hints = []
    total_losses = sum(c["count"] for c in loss_breakdown.values())
    if total_losses == 0:
        return ["回测表现良好，无明显亏损模式"]

    for cat_id, cat_data in loss_breakdown.items():
        if cat_data["count"] == 0:
            continue
        pct = cat_data["count"] / max(total_losses, 1) * 100

        if cat_id == "chasing_trapped" and pct > 20:
            hints.append("入场信号需增加趋势确认过滤，避免假突破追涨")
        elif cat_id == "stop_too_loose" and pct > 15:
            hints.append("止损应收紧至 1.0~1.5x ATR，避免浮盈回吐")
        elif cat_id == "fee_erosion" and pct > 10:
            hints.append("交易频率过高，考虑提高信号门槛或增加冷却期")
        elif cat_id == "direction_whipsaw" and pct > 20:
            hints.append("连续同向亏损说明趋势判断有系统性偏差，考虑加入趋势过滤器")
        elif cat_id == "time_stop_loss" and pct > 15:
            hints.append("时间止损触发频繁，考虑缩短持仓周期或优化退出信号")
        elif cat_id == "short_squeeze" and pct > 25:
            hints.append("做空亏损占比高，考虑降低做空频率或增加做空入场门槛")

    win_rate = result.get("win_rate", 0)
    if win_rate < 45:
        hints.append(f"胜率仅 {win_rate:.1f}%，建议增加入场过滤条件提高信号质量")

    pf = result.get("profit_factor", 0)
    if 0 < pf < 1.0:
        hints.append(f"盈亏比仅 {pf:.2f}，需调整止盈止损比例")

    dd = abs(result.get("max_drawdown_pct", 0))
    if dd > 20:
        hints.append(f"最大回撤 {dd:.1f}% 过大，需加入仓位管理或回撤保护机制")

    return hints if hints else ["未发现明显的单一问题模式，建议综合调整因子组合"]


# ═══════════════════════════ 核心分析 ═══════════════════════════

def analyze(
    result: dict[str, Any],
    criteria: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """分析回测结果，输出结构化报告。

    Args:
        result: 标准化回测结果（来自 jarvis_scalper_backtest.parse_backtest_result）
        criteria: 达标条件（不传用默认值）

    Returns:
        结构化分析报告 JSON
    """
    crit = dict(DEFAULT_PASS_CRITERIA)
    if criteria:
        crit.update(criteria)

    trades = result.get("trades", [])
    total_return = result.get("total_return_pct", 0)
    win_rate = result.get("win_rate", 0)
    pf = result.get("profit_factor", 0)
    max_dd = abs(result.get("max_drawdown_pct", 0))
    sharpe = result.get("sharpe_ratio", 0)
    total_trades = result.get("total_trades", len(trades))

    # 达标判定
    checks = {
        "win_rate": win_rate >= crit["min_win_rate"],
        "profit_factor": pf >= crit["min_profit_factor"],
        "max_drawdown": max_dd <= crit["max_drawdown_pct"],
        "total_return": total_return >= crit["min_total_return_pct"],
        "min_trades": total_trades >= crit["min_trades"],
    }

    passed_count = sum(checks.values())
    if passed_count == len(checks):
        verdict = "PASS"
    elif passed_count >= len(checks) - 1:
        verdict = "MARGINAL"
    else:
        verdict = "FAIL"

    # 亏损归类
    loss_breakdown = _classify_trade_losses(trades)

    # 计算亏损占比
    total_losses = sum(c["count"] for c in loss_breakdown.values())
    loss_pcts = {}
    for cat_id, cat_data in loss_breakdown.items():
        if cat_data["count"] > 0:
            loss_pcts[cat_id] = {
                "count": cat_data["count"],
                "pct": round(cat_data["count"] / max(total_losses, 1) * 100, 1),
                "label": cat_data["label"],
            }

    # 改进建议
    hints = _generate_hints(loss_breakdown, result)

    return {
        "verdict": verdict,
        "criteria_checks": checks,
        "total_return_pct": total_return,
        "win_rate": win_rate,
        "profit_factor": pf,
        "max_drawdown_pct": max_dd,
        "sharpe_ratio": sharpe,
        "total_trades": total_trades,
        "avg_trade_pnl": result.get("avg_trade_pnl", 0),
        "avg_bars_held": result.get("avg_bars_held", 0),
        "loss_breakdown": loss_pcts,
        "improvement_hints": hints,
    }


def format_report(report: dict[str, Any]) -> str:
    """把分析报告格式化为可读文本。"""
    lines = []
    v = report["verdict"]
    icon = {"PASS": "PASS", "MARGINAL": "MARGINAL", "FAIL": "FAIL"}[v]
    lines.append(f"=== 回测分析报告 [{icon}] ===\n")

    lines.append("--- 核心指标 ---")
    lines.append(f"  总收益率:   {report['total_return_pct']:.2f}%")
    lines.append(f"  胜率:       {report['win_rate']:.1f}%")
    lines.append(f"  盈亏比:     {report['profit_factor']:.2f}")
    lines.append(f"  最大回撤:   {report['max_drawdown_pct']:.1f}%")
    lines.append(f"  夏普比率:   {report['sharpe_ratio']:.2f}")
    lines.append(f"  总交易次数: {report['total_trades']}")

    checks = report.get("criteria_checks", {})
    lines.append("\n--- 达标检查 ---")
    for k, passed in checks.items():
        status = "PASS" if passed else "FAIL"
        lines.append(f"  {k:20s} [{status}]")

    loss = report.get("loss_breakdown", {})
    if loss:
        lines.append("\n--- 亏损归类 ---")
        for cat_id, info in sorted(loss.items(), key=lambda x: -x[1]["count"]):
            lines.append(f"  {info['label']:12s} {info['count']:3d} 次 ({info['pct']:.1f}%)")

    hints = report.get("improvement_hints", [])
    if hints:
        lines.append("\n--- 改进建议 ---")
        for i, h in enumerate(hints, 1):
            lines.append(f"  {i}. {h}")

    return "\n".join(lines)


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="回测结果分析器")
    sub = parser.add_subparsers(dest="cmd")

    p_analyze = sub.add_parser("analyze", help="分析回测结果")
    p_analyze.add_argument("--result", required=True, help="回测结果 JSON 文件")
    p_analyze.add_argument("--output", help="输出报告文件路径")
    p_analyze.add_argument("--json", action="store_true", help="输出 JSON 格式")

    p_demo = sub.add_parser("demo", help="用模拟数据演示分析")

    args = parser.parse_args()

    if args.cmd == "analyze":
        with open(args.result, encoding="utf-8") as f:
            result = json.load(f)
        report = analyze(result)
        if args.json:
            output = json.dumps(report, ensure_ascii=False, indent=2)
        else:
            output = format_report(report)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"报告已保存: {args.output}")
        else:
            print(output)

    elif args.cmd == "demo":
        mock_result = {
            "status": "succeeded",
            "total_return_pct": -5.2,
            "win_rate": 42.0,
            "profit_factor": 0.78,
            "max_drawdown_pct": -12.3,
            "sharpe_ratio": -0.5,
            "total_trades": 120,
            "avg_trade_pnl": -4.3,
            "avg_bars_held": 8.5,
            "trades": [
                {"pnl": -20, "bars_held": 2, "direction": "long", "grossPnl": -18, "fee": 2},
                {"pnl": -15, "bars_held": 1, "direction": "long", "grossPnl": -13, "fee": 2},
                {"pnl": 30, "bars_held": 10, "direction": "long"},
                {"pnl": -8, "bars_held": 5, "direction": "short"},
                {"pnl": -12, "bars_held": 3, "direction": "short"},
                {"pnl": -5, "bars_held": 2, "direction": "short"},
                {"pnl": -3, "bars_held": 8, "direction": "long", "grossPnl": 1, "fee": 4},
                {"pnl": 15, "bars_held": 6, "direction": "long"},
                {"pnl": -25, "bars_held": 1, "direction": "long"},
                {"pnl": -10, "bars_held": 15, "direction": "long", "exitReason": "time_stop"},
            ],
        }
        report = analyze(mock_result)
        print(format_report(report))
        print("\n--- JSON ---")
        print(json.dumps(report, ensure_ascii=False, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
