#!/usr/bin/env python3
"""贾维斯 JARVIS - 错误记忆回喂（[补完-6]）。

让贾维斯把"自己踩过的坑"用起来：决策时主动援引历史教训，而不是每次
都从零开始、重复犯同一个错。这是「自我意识」档（[补完-4]校准、[补完-5]
归因 之后）的最后一块——从"知道自己准不准"升级到"主动避开已知的错"。

两类教训：
  1) 静态教训（STATIC_LESSONS）：开发过程中已用真实数据证伪/证实的硬经验，
     如 -50% 回撤假 edge、牛市恐惧是利空、7 天预测本质抛硬币、弱因子别梭哈。
  2) 动态教训（mine_lessons）：从 jarvis_journal 历史快照 + 评估结果里实时
     挖出"系统性低命中"的方向/信号组合，自动沉淀为新教训，越跑越聪明。

设计原则：
  - 教训只"提示与告警"，默认不二次扣分（避免与因子归因重复惩罚同一信号）；
    是否据此收手由决策者/用户判断。可选 apply_score_adjustment 做温和修正。
  - 纯本地：静态教训零依赖；动态挖掘只读 SQLite，不联网。
  - 可追责：每条教训带 evidence（数据出处）与 source（static/journal）。

用法：
  python jarvis_lessons.py                       # 列出全部教训（静态 + 动态）
  python jarvis_lessons.py --symbol BTCUSDT
  python jarvis_lessons.py --symbol BTCUSDT --json
"""

from __future__ import annotations

import argparse
import json
import sys


# ── 1. 静态教训：开发过程中已被真实数据证伪/证实的硬经验 ─────────────────
# 每条 trigger 是一组「当前 context 命中条件」，全部满足才算适用。
# 支持的 trigger 键：
#   above_ma200: bool           价格是否在 200 日均线之上
#   below_ma200: bool           价格是否在 200 日均线之下
#   fng_lt: int                 恐慌贪婪指数 < 该值
#   fng_gt: int                 恐慌贪婪指数 > 该值
#   dd30_active: bool           回撤≤-30% 因子是否触发
#   dd50_active: bool           回撤≤-50% 因子是否触发
#   funding_regime_contains: s  资金费率 regime 包含子串
#   direction_in: [str,...]     决策方向命中其中之一
STATIC_LESSONS: list[dict] = [
    {
        "id": "dd50_overfit",
        "title": "回撤≤-50% 是过拟合假 edge，别因跌更深就加码抄底",
        "trigger": {"dd50_active": True},
        "advice": "更深的回撤不代表更强信号；-50% 阈值样本外 edge 仅 -0.03%，已判定过拟合，不要据此放大仓位。",
        "evidence": "P4 样本外：dd50 OOS edge -0.03% / 胜率 52.3%（≈抛硬币）",
        "severity": "high",
        "score_adjust": 0.0,
    },
    {
        "id": "bull_fear_bearish",
        "title": "牛市中恐惧是利空，不是抄底信号",
        "trigger": {"above_ma200": True, "fng_lt": 20},
        "advice": "F&G<20 但价在 200MA 之上时，历史是负 edge；这是牛市途中的恐慌，不要当成抄底机会加多。",
        "evidence": "P4 样本：牛市恐惧 30 天 edge ≈ -6.6%",
        "severity": "high",
        "score_adjust": 0.0,
    },
    {
        "id": "short_horizon_coinflip",
        "title": "7 天预测本质是抛硬币，短期别重仓",
        "trigger": {"direction_in": ["偏多（战术）", "偏空（战术）"]},
        "advice": "因子是 30 天均值回归信号；校准显示 7 天 BSS 为负（不如瞎猜）。有方向观点时也别赌短期兑现，按 30 天周期管理。",
        "evidence": "[补完-4] 校准：7 天 BSS -0.118（102 样本），30 天 BSS +0.092",
        "severity": "medium",
        "score_adjust": 0.0,
    },
    {
        "id": "weak_edge_humility",
        "title": "深跌反弹只是弱因子，小仓位别梭哈",
        "trigger": {"dd30_active": True},
        "advice": "深度回撤抄底样本外 edge 仅 +0.46%、胜率 55%；只够做小仓位倾斜，配硬止损，别因为'跌很多了'就重仓。",
        "evidence": "P3/P4：dd30 OOS edge +0.46% / 胜率 55.4%",
        "severity": "medium",
        "score_adjust": 0.0,
    },
    {
        "id": "crowded_long_funding",
        "title": "资金费率过热时追多易接盘",
        "trigger": {"funding_regime_contains": "overheated_long"},
        "advice": "多头拥挤、资金费率过热往往对应短期回调风险，此时追多容易在局部高点接盘，宁可等费率回落。",
        "evidence": "经验规则：funding overheated_long → 回调风险偏高",
        "severity": "low",
        "score_adjust": 0.0,
    },
]


def _ctx_from(fac: dict, deriv: dict, direction: str | None) -> dict:
    """把决策上下文压平成 trigger 可比对的字段。"""
    fng = None
    fg = deriv.get("fear_greed", {}) if isinstance(deriv, dict) else {}
    if isinstance(fg, dict):
        fng = fg.get("fng_value")
    funding = deriv.get("funding", {}) if isinstance(deriv, dict) else {}
    regime = funding.get("funding_regime", "") if isinstance(funding, dict) else ""
    return {
        "above_ma200": fac.get("above_ma200") is True,
        "below_ma200": fac.get("above_ma200") is False,
        "fng": fng,
        "dd30_active": bool(fac.get("dd30_signal_active")),
        "dd50_active": bool(fac.get("dd50_signal_active")),
        "funding_regime": regime or "",
        "direction": direction,
    }


def _match(trigger: dict, ctx: dict) -> bool:
    """trigger 的所有条件都满足才算命中。"""
    for key, want in trigger.items():
        if key == "above_ma200" and ctx["above_ma200"] is not want:
            return False
        if key == "below_ma200" and ctx["below_ma200"] is not want:
            return False
        if key == "fng_lt":
            if ctx["fng"] is None or not (ctx["fng"] < want):
                return False
        if key == "fng_gt":
            if ctx["fng"] is None or not (ctx["fng"] > want):
                return False
        if key == "dd30_active" and ctx["dd30_active"] is not want:
            return False
        if key == "dd50_active" and ctx["dd50_active"] is not want:
            return False
        if key == "funding_regime_contains" and want not in ctx["funding_regime"]:
            return False
        if key == "direction_in":
            if ctx["direction"] not in want:
                return False
    return True


def applicable_lessons(
    fac: dict,
    deriv: dict,
    direction: str | None = None,
    *,
    symbol: str | None = None,
    include_dynamic: bool = True,
) -> list[dict]:
    """返回当前决策上下文下应主动援引的教训（静态 + 可选动态）。"""
    ctx = _ctx_from(fac or {}, deriv or {}, direction)
    hits: list[dict] = []
    for lesson in STATIC_LESSONS:
        if _match(lesson.get("trigger", {}), ctx):
            hits.append({k: v for k, v in lesson.items() if k != "trigger"})
    if include_dynamic:
        try:
            for dyn in mine_lessons(symbol=symbol):
                if direction is None or dyn.get("direction") in (None, direction):
                    hits.append(dyn)
        except Exception as exc:  # 动态挖掘失败不能拖垮决策
            hits.append({
                "id": "dynamic_unavailable",
                "title": "历史教训库暂不可用",
                "advice": f"无法读取 journal 动态教训：{exc}",
                "severity": "low",
                "source": "journal",
                "score_adjust": 0.0,
            })
    # 严重度排序：high > medium > low
    order = {"high": 0, "medium": 1, "low": 2}
    hits.sort(key=lambda x: order.get(x.get("severity", "low"), 3))
    return hits


def score_penalty(lessons: list[dict]) -> float:
    """可选：把命中教训的 score_adjust 汇总成一个温和的信心分修正量。

    默认所有静态教训 score_adjust=0（只提示不扣分）；保留此接口供决策者
    显式选择"让教训直接压低信心分"。
    """
    return round(sum(float(l.get("score_adjust", 0.0) or 0.0) for l in lessons), 2)


# ── 2. 动态教训：从 jarvis_journal 历史评估里挖系统性低命中 ────────────────
def mine_lessons(
    symbol: str | None = None,
    *,
    min_n: int = 8,
    hit_threshold: float = 45.0,
    horizon: int = 30,
) -> list[dict]:
    """读 journal 历史评估，把"某方向在 N 次以上仍长期低命中"沉淀为教训。

    只读 SQLite、不联网。journal 为空 / 不存在时返回 []。
    """
    import jarvis_journal as jj  # 延迟导入，避免 brief↔journal↔lessons 循环

    rep = jj.report(symbol)
    by_h = rep.get("by_horizon", {}).get(f"{horizon}d", {})
    by_dir = by_h.get("by_direction", {})
    out: list[dict] = []
    for direction, agg in by_dir.items():
        if "中性" in (direction or ""):
            continue  # 中性不计命中率
        graded = agg.get("graded_n", 0) or 0
        hr = agg.get("hit_rate_pct")
        if graded >= min_n and hr is not None and hr < hit_threshold:
            out.append({
                "id": f"journal_lowhit_{direction}",
                "title": f"历史上『{direction}』在 {horizon} 天命中率仅 {hr}%",
                "advice": f"过去 {graded} 次『{direction}』判断里只对了 {hr}%，低于抛硬币；"
                          f"再出此方向时降低信心、缩小仓位或要求更强确认。",
                "evidence": f"jarvis_journal {horizon}d：{direction} graded_n={graded}, hit_rate={hr}%",
                "severity": "high" if hr < 40 else "medium",
                "source": "journal",
                "direction": direction,
                "score_adjust": 0.0,
            })
    return out


def all_lessons(symbol: str | None = None) -> dict:
    """汇总静态 + 动态全部教训，供 CLI / 仪表盘展示。"""
    statics = [{k: v for k, v in l.items() if k != "trigger"} | {"source": "static"} for l in STATIC_LESSONS]
    try:
        dynamics = mine_lessons(symbol=symbol)
        dyn_err = None
    except Exception as exc:
        dynamics = []
        dyn_err = str(exc)
    return {"symbol": (symbol or "ALL"), "static": statics, "dynamic": dynamics, "dynamic_error": dyn_err}


def to_markdown(data: dict) -> str:
    lines = [f"# 贾维斯错误记忆（教训库）— {data['symbol']}", ""]
    sev_icon = {"high": "🔴", "medium": "🟠", "low": "🔵"}
    lines.append("## 静态教训（已被数据证伪/证实的硬经验）")
    for l in data["static"]:
        lines.append(f"- {sev_icon.get(l.get('severity'),'·')} **{l['title']}**")
        lines.append(f"  - 建议：{l['advice']}")
        lines.append(f"  - 证据：{l['evidence']}")
    lines.append("")
    lines.append("## 动态教训（从历史战绩实时挖掘）")
    if data.get("dynamic_error"):
        lines.append(f"> 动态挖掘不可用：{data['dynamic_error']}")
    elif not data["dynamic"]:
        lines.append("> 暂无：要么 journal 样本不足，要么各方向命中率均健康。")
    else:
        for l in data["dynamic"]:
            lines.append(f"- {sev_icon.get(l.get('severity'),'·')} **{l['title']}**")
            lines.append(f"  - 建议：{l['advice']}")
            lines.append(f"  - 证据：{l['evidence']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯错误记忆回喂（教训库）")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    data = all_lessons(args.symbol)
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else to_markdown(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
