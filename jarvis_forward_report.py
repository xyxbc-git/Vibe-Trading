#!/usr/bin/env python3
"""贾维斯 JARVIS - 前向战绩自评报告（T-16，自进化闭环的「总结不足」）。

把四路信号汇成一份「贾维斯的自我体检」，回答三问：我准不准？哪里不足？该怎么改？
  1) 战绩       ← jarvis_journal.report（按方向/horizon 的命中率与前向收益）
  2) 已知教训   ← jarvis_lessons.all_lessons（静态硬经验 + 从历史挖的动态低命中）
  3) 参数漂移   ← jarvis_weights.diff_from_default（权重被重训改了多少）
  4) 改进建议   ← jarvis_retrain.run(dry-run)（下一步该如何温和调权）

纯只读：不下单、不写权重（重训建议仅 dry-run 呈现），可安全定时跑。

用法：
  python jarvis_forward_report.py
  python jarvis_forward_report.py --symbol BTCUSDT
  python jarvis_forward_report.py --json
"""

from __future__ import annotations

import argparse
import json
import sys


def build(symbol: str | None = None, *, horizon: int = 30) -> dict:
    """汇总自评数据。每一路都异常隔离，单路失败不拖垮整份报告。"""
    out: dict = {"symbol": (symbol.upper() if symbol else "ALL"), "horizon": horizon}

    try:
        import jarvis_journal as jj
        out["scoreboard"] = jj.report(symbol)
    except Exception as exc:  # noqa: BLE001
        out["scoreboard"] = {"error": repr(exc)[:200]}

    try:
        import jarvis_lessons as jl
        out["lessons"] = jl.all_lessons(symbol)
    except Exception as exc:  # noqa: BLE001
        out["lessons"] = {"error": repr(exc)[:200]}

    try:
        import jarvis_weights as jw
        out["weight_drift"] = jw.diff_from_default()
    except Exception as exc:  # noqa: BLE001
        out["weight_drift"] = {"error": repr(exc)[:200]}

    try:
        import jarvis_retrain as jr
        out["retrain_suggestion"] = jr.run(symbol, horizon=horizon, apply=False)
    except Exception as exc:  # noqa: BLE001
        out["retrain_suggestion"] = {"error": repr(exc)[:200]}

    out["weaknesses"] = _diagnose_weaknesses(out, horizon)
    return out


def _diagnose_weaknesses(data: dict, horizon: int) -> list[str]:
    """从战绩 + 教训里提炼「不足」清单（给人看的诊断结论）。"""
    weak: list[str] = []
    sb = data.get("scoreboard", {})
    if isinstance(sb, dict) and "error" not in sb:
        if sb.get("total_snapshots", 0) == 0:
            weak.append("尚无决策快照：闭环还没开始攒数据，先 backfill 造历史或让 daemon 跑起来。")
        byh = sb.get("by_horizon", {}).get(f"{horizon}d", {})
        ov = byh.get("overall", {})
        if ov.get("hit_rate_pct") is not None and ov["hit_rate_pct"] < 50 and (ov.get("graded_n") or 0) >= 8:
            weak.append(f"{horizon}d 整体命中率仅 {ov['hit_rate_pct']}%（n={ov.get('graded_n')}），低于抛硬币——方向判断整体偏弱。")
        for d, agg in (byh.get("by_direction", {}) or {}).items():
            hr = agg.get("hit_rate_pct")
            if hr is not None and (agg.get("graded_n") or 0) >= 8 and hr < 45:
                weak.append(f"方向「{d}」{horizon}d 命中率 {hr}%（n={agg.get('graded_n')}）系统性偏低，需降低该方向信心或加强确认。")
    les = data.get("lessons", {})
    if isinstance(les, dict):
        for dyn in les.get("dynamic", []) or []:
            weak.append(f"动态教训：{dyn.get('title')} — {dyn.get('advice')}")
    if not weak:
        weak.append("暂未发现系统性短板（样本可能仍不足，继续积累后复检）。")
    return weak


def to_markdown(data: dict) -> str:
    sym = data["symbol"]
    h = data["horizon"]
    lines = [f"# 贾维斯自评报告 — {sym}", ""]

    sb = data.get("scoreboard", {})
    lines.append("## 一、战绩（我准不准）")
    if isinstance(sb, dict) and "error" not in sb:
        lines.append(f"- 累计快照 {sb.get('total_snapshots', 0)} 条 | 已评估前向 {sb.get('evaluated_outcomes', 0)} 条")
        byh = sb.get("by_horizon", {}).get(f"{h}d", {})
        ov = byh.get("overall", {})
        if ov.get("n"):
            hr = f"{ov['hit_rate_pct']}%" if ov.get("hit_rate_pct") is not None else "—"
            lines.append(f"- {h}d 整体：n={ov['n']} 平均收益 {ov.get('avg_ret_pct')}% | 命中率 {hr}")
            for d, a in (byh.get("by_direction", {}) or {}).items():
                if a.get("n"):
                    dhr = f"{a['hit_rate_pct']}%" if a.get("hit_rate_pct") is not None else "—（中性不计）"
                    lines.append(f"  - {d}: n={a['n']} 平均 {a.get('avg_ret_pct')}% | 命中 {dhr}")
        else:
            lines.append(f"- {h}d 暂无已到期评估样本。")
    else:
        lines.append(f"- ⚠ 战绩不可用：{sb.get('error')}")

    lines += ["", "## 二、不足（哪里需要改）"]
    for w in data.get("weaknesses", []):
        lines.append(f"- {w}")

    drift = data.get("weight_drift", {})
    lines += ["", "## 三、参数漂移（权重被训成了什么样）"]
    if isinstance(drift, dict) and "error" not in drift:
        wchg = drift.get("weights", {})
        if wchg:
            for k, v in wchg.items():
                lines.append(f"- {k}: 默认 {v['default']} → 当前 {v['current']}（Δ{v['delta']:+}）")
        else:
            lines.append("- 权重仍为内置默认（尚未重训或重训未采纳任何调整）。")
        meta = drift.get("meta", {})
        lines.append(f"- 配置版本 v{meta.get('version')} | 来源 {meta.get('source')} | 更新 {meta.get('updated_at')}")
    else:
        lines.append(f"- ⚠ 漂移不可用：{drift.get('error')}")

    rt = data.get("retrain_suggestion", {})
    lines += ["", "## 四、改进建议（下一步怎么训）"]
    if isinstance(rt, dict) and "error" not in rt:
        lines.append(f"- 基于 {rt.get('samples')} 条历史样本，本轮建议采纳 {rt.get('adopted_count')} 项调整（dry-run，未写入）。")
        for p in rt.get("proposals", []):
            if p.get("adopted"):
                lines.append(f"  - ✅ {p['factor']}: {p['current_weight']}→{p['proposed_weight']}（{p['reason']}）")
        if rt.get("adopted_count", 0) == 0:
            lines.append("  - 本轮无采纳（样本不足 / OOS 不一致 / 已在边界）——保守是对的，不过拟合。")
        lines.append("- 如确认采纳：`python jarvis_retrain.py --apply`（写入后可 `jarvis_weights.py reset` 回退）。")
    else:
        lines.append(f"- ⚠ 建议不可用：{rt.get('error')}")

    lines += ["", "> 本报告纯只读：不下单、不改权重。重训需显式 `--apply`。"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯前向战绩自评报告（T-16）")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--horizon", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    data = build(args.symbol, horizon=args.horizon)
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else to_markdown(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
