#!/usr/bin/env python3
"""贾维斯 JARVIS - 每日晨报自动生成（T-14）。

把分散的「机会雷达 + 组合相关性 + 熔断健康 + 前向战绩」聚合成一份**每天一封**
的结构化晨报，经 `jarvis_notify` 推送到 Telegram / 飞书，让贾维斯主动「早安播报」，
无需人工每天逐个敲命令。

聚合来源（全部惰性导入 + 异常隔离，单源失败不拖垮整封晨报）：
  - jarvis_radar.scan      ：扫币种池，挑达标信号 + 组合相关性折算
  - jarvis_circuit_breaker ：组合熔断健康度（回撤 / 是否已熔断）
  - jarvis_journal         ：前向战绩摘要（命中率 / 样本数，若有）

设计原则：
  - 永不抛出：任何子模块异常都降级为「该段不可用」，晨报照常生成。
  - 幂等播报：同一天重复跑只是重算，不会重复落库（推送由调用方控制频率）。
  - 可演练：--dry-run 只打印不真发，联网前可本地核对格式。

用法：
  python jarvis_morning_report.py                       # 生成并打印
  python jarvis_morning_report.py --notify --dry-run    # 生成 + 演练推送
  python jarvis_morning_report.py --symbols BTC,ETH,SOL --notify
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

LOG_DIR = os.path.expanduser("~/.vibe-trading")
STATUS_PATH = os.path.join(LOG_DIR, "jarvis_morning_report_status.json")


def _radar_section(symbols: list[str] | None, min_conviction: float) -> dict:
    try:
        import jarvis_radar as jr
        return {"ok": True, "data": jr.scan(symbols, min_conviction=min_conviction)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:200]}


def _circuit_section() -> dict:
    try:
        import jarvis_circuit_breaker as cb
        ev = cb.evaluate()
        return {"ok": True, "drawdown_pct": ev.get("drawdown_pct"),
                "already_tripped": bool(ev.get("already_tripped")),
                "should_halt": bool(ev.get("should_halt")),
                "equity_usdt": ev.get("equity_usdt")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:200]}


def _journal_section(symbol: str) -> dict:
    try:
        import jarvis_journal as jj
        rep = jj.report(symbol) if hasattr(jj, "report") else None
        if isinstance(rep, dict):
            return {"ok": True, "data": rep}
        return {"ok": False, "error": "journal.report 不可用"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:200]}


def build(symbols: list[str] | None = None, min_conviction: float = 0.8) -> dict:
    """生成晨报数据结构。永不抛出。"""
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "date": time.strftime("%Y-%m-%d"),
        "radar": _radar_section(symbols, min_conviction),
        "circuit": _circuit_section(),
        "journal": _journal_section((symbols[0] if symbols else "BTCUSDT")),
    }
    _write_status(report)
    return report


def _write_status(report: dict) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def to_text(report: dict, limit: int = 8) -> str:
    """把晨报压成适合推送的简洁文本（中文）。"""
    lines = [f"☀️ 贾维斯每日晨报 · {report.get('date')}"]

    # 熔断健康度
    c = report.get("circuit", {})
    if c.get("ok"):
        flag = "🛑 已熔断" if c.get("already_tripped") else ("⚠️ 触发条件" if c.get("should_halt") else "✅ 正常")
        lines.append(f"风控：{flag}（组合回撤 {c.get('drawdown_pct')}% / 权益 {c.get('equity_usdt')}U）")
    else:
        lines.append("风控：状态不可用")

    # 雷达达标信号
    r = report.get("radar", {})
    if r.get("ok"):
        rd = r["data"]
        hits = rd.get("actionable", [])
        lines.append(f"机会雷达：扫 {rd.get('scanned', 0)} 币 / 达标 {len(hits)} 个")
        for it in hits[:limit]:
            adj = it.get("position_pct_adjusted")
            pos = f"{it.get('position_pct')}%" + (f"→折算{adj}%" if adj is not None else "")
            lines.append(f"• {it['symbol']}：{it.get('direction')} 信心 {it.get('conviction_score')} 仓位 {pos}"
                         + (f" ⚠️x{it['lesson_count']}" if it.get("lesson_count") else ""))
        if not hits:
            lines.append("• 本轮无达标信号，观望为宜。")
        p = rd.get("portfolio")
        if isinstance(p, dict) and "_error" not in p and p.get("naive_total_pct") is not None:
            lines.append(f"组合：名义 {p.get('naive_total_pct')}% → 有效敞口 {p.get('effective_before_pct')}%"
                         + ("（已缩减）" if p.get("scaled") else ""))
    else:
        lines.append(f"机会雷达：不可用（{r.get('error')}）")

    # 战绩摘要（可选）
    j = report.get("journal", {})
    if j.get("ok") and isinstance(j.get("data"), dict):
        d = j["data"]
        hit = d.get("hit_rate_pct") or d.get("win_rate_pct")
        n = d.get("evaluated") or d.get("n") or d.get("samples")
        if hit is not None:
            lines.append(f"前向战绩：命中率 {hit}%（样本 {n}）")

    lines.append("— 仅研究信号，非交易建议。")
    return "\n".join(lines)


def send(report: dict, dry_run: bool = False) -> dict:
    """把晨报经 notify 推送。永不抛出。"""
    try:
        import jarvis_notify as jn
        return jn.notify(to_text(report), dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)[:200]}


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯每日晨报")
    ap.add_argument("--symbols", default=None, help="逗号分隔，如 BTC,ETH,SOL；默认配置中心币种池")
    ap.add_argument("--min-conviction", type=float, default=0.8)
    ap.add_argument("--notify", action="store_true", help="生成后推送")
    ap.add_argument("--dry-run", action="store_true", help="配合 --notify：只打印不真发")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    syms = [s for s in args.symbols.split(",")] if args.symbols else None
    report = build(syms, min_conviction=args.min_conviction)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(to_text(report))
    if args.notify:
        res = send(report, dry_run=args.dry_run)
        if not args.json:
            print(f"\n[推送] {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
