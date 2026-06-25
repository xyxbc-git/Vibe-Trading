#!/usr/bin/env python3
"""贾维斯 JARVIS — 实盘表现追踪器 + 参数自适应引擎（P3-01 & P3-02）。

持续监控实盘交易表现，自动检测策略衰退，触发参数微调或重新进化。

核心能力：
  1. 表现追踪：滚动窗口统计胜率、盈亏比、夏普
  2. 衰退检测：连续亏损、胜率骤降、表现持续下滑
  3. 参数自适应：凯利公式仓位 + 止损倍数微调 + 信号阈值调整
  4. 自动触发：衰退确认后自动触发重新进化或参数调整
  5. 成长报告：每周生成结构化 Markdown 报告

用法：
  python jarvis_performance_tracker.py evaluate
  python jarvis_performance_tracker.py report --weekly
  python jarvis_performance_tracker.py tune
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, asdict, field
from typing import Any

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
TRACKER_PATH = os.path.join(CONFIG_DIR, "performance_tracker.json")
TUNE_LOG_PATH = os.path.join(CONFIG_DIR, "adaptive_tune_log.json")
REPORTS_DIR = os.path.join(CONFIG_DIR, "reports")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [TRACKER] {msg}"
    print(line, flush=True)


# ═══════════════════════════ 表现追踪 ═══════════════════════════

@dataclass
class PerformanceReport:
    """表现评估报告。"""
    window_size: int
    total_trades: int
    win_rate: float
    profit_factor: float
    avg_pnl: float
    max_consecutive_loss: int
    sharpe_estimate: float
    regime_breakdown: dict
    is_decaying: bool
    decay_signals: list[str]
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class PerformanceTracker:
    """实盘表现追踪与衰退检测。"""

    def __init__(
        self,
        window_size: int = 50,
        decay_win_rate_threshold: float = 40.0,
        decay_consecutive_loss: int = 5,
        decay_pf_decline_periods: int = 3,
    ):
        self.window_size = window_size
        self.decay_win_rate = decay_win_rate_threshold
        self.decay_consec_loss = decay_consecutive_loss
        self.decay_pf_periods = decay_pf_decline_periods
        self._history: list[dict] = []
        self._reports: list[dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(TRACKER_PATH):
            try:
                with open(TRACKER_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                self._history = data.get("trades", [])
                self._reports = data.get("reports", [])
            except Exception:
                pass

    def _save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(TRACKER_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "trades": self._history[-500:],
                "reports": self._reports[-50:],
            }, f, ensure_ascii=False, indent=2)

    def record_trade(self, trade: dict) -> None:
        """记录一笔交易。"""
        trade["recorded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._history.append(trade)
        self._save()

    def evaluate(self, recent_n: int | None = None) -> PerformanceReport:
        """评估最近 N 笔交易的表现。"""
        n = recent_n or self.window_size
        trades = self._history[-n:] if self._history else []

        if not trades:
            return PerformanceReport(
                window_size=n, total_trades=0, win_rate=0, profit_factor=0,
                avg_pnl=0, max_consecutive_loss=0, sharpe_estimate=0,
                regime_breakdown={}, is_decaying=False, decay_signals=[],
                timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            )

        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0

        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 1
        pf = avg_win / avg_loss if avg_loss > 0 else 0

        pnls = [t.get("pnl", 0) for t in trades]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0

        import numpy as np
        pnl_arr = np.array(pnls) if pnls else np.array([0])
        sharpe = (pnl_arr.mean() / pnl_arr.std() * np.sqrt(96)) if pnl_arr.std() > 0 else 0

        max_consec = 0
        curr_consec = 0
        for t in trades:
            if t.get("pnl", 0) <= 0:
                curr_consec += 1
                max_consec = max(max_consec, curr_consec)
            else:
                curr_consec = 0

        regime_stats: dict[str, dict] = {}
        for t in trades:
            regime = t.get("regime", "unknown")
            if regime not in regime_stats:
                regime_stats[regime] = {"count": 0, "wins": 0, "total_pnl": 0}
            regime_stats[regime]["count"] += 1
            if t.get("pnl", 0) > 0:
                regime_stats[regime]["wins"] += 1
            regime_stats[regime]["total_pnl"] += t.get("pnl", 0)

        for regime, stats in regime_stats.items():
            stats["win_rate"] = round(stats["wins"] / max(stats["count"], 1) * 100, 1)
            stats["total_pnl"] = round(stats["total_pnl"], 2)

        # 衰退检测
        decay_signals = []
        recent_20 = trades[-20:] if len(trades) >= 20 else trades
        recent_wr = len([t for t in recent_20 if t.get("pnl", 0) > 0]) / max(len(recent_20), 1) * 100
        if recent_wr < self.decay_win_rate:
            decay_signals.append(f"近20笔胜率{recent_wr:.0f}%（阈值{self.decay_win_rate}%）")

        if max_consec >= self.decay_consec_loss:
            decay_signals.append(f"连续亏损{max_consec}笔（阈值{self.decay_consec_loss}）")

        if len(self._reports) >= self.decay_pf_periods:
            recent_pfs = [r.get("profit_factor", 0) for r in self._reports[-self.decay_pf_periods:]]
            if all(recent_pfs[i] > recent_pfs[i+1] for i in range(len(recent_pfs)-1)):
                decay_signals.append(f"盈亏比连续{self.decay_pf_periods}期下降")

        is_decaying = len(decay_signals) >= 2

        report = PerformanceReport(
            window_size=n,
            total_trades=len(trades),
            win_rate=round(win_rate, 1),
            profit_factor=round(pf, 2),
            avg_pnl=round(avg_pnl, 2),
            max_consecutive_loss=max_consec,
            sharpe_estimate=round(float(sharpe), 2),
            regime_breakdown=regime_stats,
            is_decaying=is_decaying,
            decay_signals=decay_signals,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        self._reports.append(report.to_dict())
        self._save()
        return report


# ═══════════════════════════ 参数自适应 ═══════════════════════════

@dataclass
class TuneResult:
    """参数调整结果。"""
    adjusted: bool
    changes: dict
    reasoning: str
    kelly_size: float
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class AdaptiveTuner:
    """基于实盘数据的参数自适应引擎。"""

    def __init__(self, max_adjust_pct: float = 0.2, min_interval_hours: float = 24):
        self.max_adjust = max_adjust_pct
        self.min_interval_h = min_interval_hours
        self._tune_log: list[dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(TUNE_LOG_PATH):
            try:
                with open(TUNE_LOG_PATH, encoding="utf-8") as f:
                    self._tune_log = json.load(f)
            except Exception:
                pass

    def _save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(TUNE_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(self._tune_log[-100:], f, ensure_ascii=False, indent=2)

    def kelly_sizing(
        self, win_rate: float, avg_win: float, avg_loss: float,
    ) -> float:
        """凯利公式计算最优仓位（使用半凯利降低风险）。
        
        f* = (p*b - q) / b
        p = 胜率, q = 1-p, b = 盈亏比
        """
        p = win_rate / 100.0
        q = 1 - p
        b = avg_win / max(avg_loss, 0.01)

        if b <= 0 or p <= 0:
            return 0.005

        kelly = (p * b - q) / b
        half_kelly = kelly / 2

        return max(0.005, min(0.05, half_kelly))

    def tune(
        self,
        report: PerformanceReport,
        current_config: dict,
    ) -> TuneResult:
        """基于表现报告微调策略参数。"""
        if self._tune_log:
            last_tune = self._tune_log[-1]
            last_time = last_tune.get("timestamp", "")
            if last_time:
                try:
                    last_ts = time.mktime(time.strptime(last_time, "%Y-%m-%d %H:%M:%S"))
                    hours_since = (time.time() - last_ts) / 3600
                    if hours_since < self.min_interval_h:
                        return TuneResult(
                            adjusted=False, changes={},
                            reasoning=f"距上次调整仅{hours_since:.1f}h，未到最小间隔{self.min_interval_h}h",
                            kelly_size=0, timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                        )
                except Exception:
                    pass

        changes = {}
        reasons = []

        trades = []
        tracker = PerformanceTracker()
        recent = tracker._history[-50:] if tracker._history else []
        wins = [t for t in recent if t.get("pnl", 0) > 0]
        losses = [t for t in recent if t.get("pnl", 0) <= 0]

        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 1

        kelly = self.kelly_sizing(report.win_rate, avg_win, avg_loss)
        changes["suggested_position_size"] = round(kelly, 4)
        reasons.append(f"凯利公式建议仓位: {kelly:.2%}")

        if report.win_rate < 45 and report.total_trades >= 20:
            reasons.append("胜率偏低，建议收紧入场信号阈值")
            changes["confidence_threshold_delta"] = +0.05

        if report.profit_factor < 1.0 and report.total_trades >= 20:
            reasons.append("盈亏比<1，建议扩大止盈/收紧止损")
            changes["tp_mult_delta"] = +0.2
            changes["sl_mult_delta"] = -0.1

        if report.max_consecutive_loss >= 4:
            reasons.append(f"连亏{report.max_consecutive_loss}笔，建议降低仓位至半凯利以下")
            changes["suggested_position_size"] = round(kelly * 0.5, 4)

        regime_weak = None
        for regime, stats in report.regime_breakdown.items():
            if stats.get("count", 0) >= 5 and stats.get("win_rate", 100) < 35:
                regime_weak = regime
                reasons.append(f"{regime}行情胜率仅{stats['win_rate']:.0f}%，建议该行情下降低仓位或跳过")
                changes[f"regime_{regime}_reduce"] = True

        adjusted = len(changes) > 1

        result = TuneResult(
            adjusted=adjusted,
            changes=changes,
            reasoning="；".join(reasons),
            kelly_size=round(kelly, 4),
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

        self._tune_log.append(result.to_dict())
        self._save()
        return result


# ═══════════════════════════ 成长报告 ═══════════════════════════

def generate_weekly_report() -> str:
    """生成每周成长报告（Markdown）。"""
    tracker = PerformanceTracker()
    report = tracker.evaluate()

    week_num = time.strftime("%Y-W%W")

    lines = [
        f"## 贾维斯周报 | {week_num}",
        "",
        "### 本周战绩",
        f"- 总交易 {report.total_trades} 笔，胜率 {report.win_rate:.0f}%，"
        f"盈亏比 {report.profit_factor:.2f}",
        f"- 平均盈亏 {report.avg_pnl:+.2f} USDT/笔",
        f"- 最大连亏 {report.max_consecutive_loss} 笔",
        f"- 夏普估算 {report.sharpe_estimate:.2f}",
        "",
    ]

    if report.regime_breakdown:
        lines.append("### 行情分类表现")
        lines.append("")
        lines.append("| 行情 | 笔数 | 胜率 | 盈亏 |")
        lines.append("| --- | --- | --- | --- |")
        for regime, stats in report.regime_breakdown.items():
            regime_cn = {"trending": "趋势", "ranging": "震荡", "breakout": "突破"}.get(regime, regime)
            lines.append(
                f"| {regime_cn} | {stats['count']} | {stats['win_rate']:.0f}% | "
                f"{stats['total_pnl']:+.2f} |"
            )
        lines.append("")

    if report.decay_signals:
        lines.append("### ⚠️ 衰退信号")
        for sig in report.decay_signals:
            lines.append(f"- {sig}")
        lines.append("")

    if report.is_decaying:
        lines.append("### 🔄 自动调整建议")
        tuner = AdaptiveTuner()
        tune_result = tuner.tune(report, {})
        lines.append(f"- {tune_result.reasoning}")
        lines.append(f"- 凯利公式建议仓位: {tune_result.kelly_size:.2%}")
        lines.append("")

    lines.append(f"---\n*报告生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}*")

    md = "\n".join(lines)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, f"weekly_{week_num}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(md)
    _log(f"周报已保存: {report_path}")

    return md


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="实盘表现追踪器")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("evaluate", help="评估当前表现")
    sub.add_parser("report", help="生成周报")
    sub.add_parser("tune", help="参数自适应建议")
    sub.add_parser("status", help="追踪器状态")

    args = parser.parse_args()

    if args.cmd == "evaluate":
        tracker = PerformanceTracker()
        report = tracker.evaluate()
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

    elif args.cmd == "report":
        md = generate_weekly_report()
        print(md)

    elif args.cmd == "tune":
        tracker = PerformanceTracker()
        report = tracker.evaluate()
        tuner = AdaptiveTuner()
        result = tuner.tune(report, {})
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

    elif args.cmd == "status":
        tracker = PerformanceTracker()
        print(f"=== 表现追踪器状态 ===")
        print(f"  记录交易数: {len(tracker._history)}")
        print(f"  评估报告数: {len(tracker._reports)}")
        if tracker._history:
            last = tracker._history[-1]
            print(f"  最近交易: {last.get('recorded_at', '?')}")
            print(f"  最近盈亏: {last.get('pnl', 0):+.2f}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
