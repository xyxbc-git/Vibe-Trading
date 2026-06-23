#!/usr/bin/env python3
"""贾维斯 JARVIS — [补完-12] 多币种机会雷达：扫 Top N 找信号。

把单币的 `jarvis_brief` 决策扩展成「一次扫一篮子币」，按信心分排序，挑出
**达标的可执行信号**（偏多达阈值 / 强偏空 / 命中风险教训），让贾维斯主动
发现机会，而不是逐个币手敲。可选把雷达结果经 `jarvis_notify` 推送出去。

设计：
  - 逐币调 `jarvis_brief.build`；**单币失败（网络/数据）只记错继续**，不拖垮整轮扫描。
  - 排序：偏多按信心分降序优先，其次命中教训数；输出全量 + 达标子集。
  - 达标规则：偏多且信心≥min_conviction 且建议仓位>0；或方向偏空（风险预警）。

用法：
  python jarvis_radar.py                                  # 扫默认篮子
  python jarvis_radar.py --symbols BTC,ETH,SOL --min-conviction 0.8
  python jarvis_radar.py --notify --dry-run               # 扫完推送（演练）
  python jarvis_radar.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import jarvis_brief as jb
import jarvis_config as jcfg
import jarvis_correlation as jcorr

# 默认篮子（与仪表盘币种下拉一致，可扩展）。
# [T-15] 优先取配置中心 watchlist；配置缺失时回退此内置原值（零回归）。
DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]


def _watchlist() -> list[str]:
    try:
        wl = jcfg.get("watchlist")
        if isinstance(wl, list) and wl:
            return [str(s) for s in wl]
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_WATCHLIST

LOG_DIR = os.path.expanduser("~/.vibe-trading")
STATUS_PATH = os.path.join(LOG_DIR, "jarvis_radar_status.json")


def _norm(sym: str) -> str:
    s = sym.strip().upper().replace("-", "").replace("/", "")
    return s if s.endswith("USDT") else s + "USDT"


def _correlation_overlay(actionable: list[dict], max_effective_pct: float, corr_days: int) -> dict | None:
    """T-10 多币相关性折算：对同向（偏多）多币仓位折算有效敞口，超 cap 等比缩减。

    只对 ≥2 个偏多且 position_pct>0 的信号生效；就地给这些信号写入
    `position_pct_adjusted`，并返回组合级 portfolio 摘要（无可折算则返回 None）。
    永不抛出——相关性计算失败时退化为不缩减。
    """
    longs = [r for r in actionable
             if (r.get("direction") or "").startswith("偏多") and (r.get("position_pct") or 0) > 0]
    if len(longs) < 2:
        return None
    syms = [r["symbol"] for r in longs]
    weights = [float(r["position_pct"]) for r in longs]
    try:
        adj = jcorr.adjust_positions(syms, weights, days=corr_days, cap=max_effective_pct)
    except Exception as exc:  # noqa: BLE001 — 折算失败不拖垮雷达
        return {"_error": repr(exc)[:200]}
    by_sym = {p["symbol"]: p["position_pct_adjusted"] for p in adj.get("per_symbol", [])}
    for r in longs:
        r["position_pct_adjusted"] = by_sym.get(r["symbol"], r.get("position_pct"))
    return adj


def scan(symbols: list[str] | None = None, min_conviction: float = 0.8,
         max_effective_pct: float = jcorr.DEFAULT_MAX_EFFECTIVE_PCT, corr_days: int = 30) -> dict:
    """扫一篮子币，返回全量 + 达标信号。永不抛出（单币异常被吞）。"""
    symbols = [_norm(s) for s in (symbols or _watchlist())]
    results = []
    errors = []
    for sym in symbols:
        try:
            b = jb.build(sym)
            dec = b.get("decision", {})
            if "_error" in dec or "_error" in b.get("factor_state", {}):
                errors.append({"symbol": sym, "error": dec.get("_error") or b.get("factor_state", {}).get("_error")})
                continue
            lessons = dec.get("lessons") or []
            results.append({
                "symbol": sym,
                "direction": dec.get("direction"),
                "conviction_score": dec.get("conviction_score"),
                "position_pct": dec.get("suggested_position_pct"),
                "entry_zone": dec.get("entry_zone"),
                "stop_loss": dec.get("stop_loss"),
                "take_profit_ref": dec.get("take_profit_ref"),
                "lesson_count": len(lessons),
                "top_lesson": (lessons[0].get("title") if lessons else None),
            })
        except Exception as exc:  # noqa: BLE001 — 单币失败不影响整轮
            errors.append({"symbol": sym, "error": repr(exc)[:200]})

    def _is_actionable(r: dict) -> bool:
        sc = r.get("conviction_score")
        dirn = (r.get("direction") or "")
        if dirn.startswith("偏多") and sc is not None and sc >= min_conviction and (r.get("position_pct") or 0) > 0:
            return True
        if dirn.startswith("偏空"):
            return True
        return False

    actionable = [r for r in results if _is_actionable(r)]
    # 排序：偏多按信心降序优先，其次教训数；偏空殿后。
    actionable.sort(key=lambda r: (
        0 if (r.get("direction") or "").startswith("偏多") else 1,
        -(r.get("conviction_score") or -99),
        -(r.get("lesson_count") or 0),
    ))
    results.sort(key=lambda r: -(r.get("conviction_score") or -99))

    portfolio = _correlation_overlay(actionable, max_effective_pct, corr_days)

    out = {
        "scanned": len(results),
        "requested": len(symbols),
        "min_conviction": min_conviction,
        "actionable": actionable,
        "all": results,
        "errors": errors,
        "portfolio": portfolio,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_status(out)
    return out


def _write_status(out: dict) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def to_markdown(radar: dict) -> str:
    lines = [
        f"# 贾维斯机会雷达（{radar['generated_at']}）",
        "",
        f"- 请求 {radar['requested']} 币 | 成功扫描 {radar['scanned']} | 达标信号 {len(radar['actionable'])} | 失败 {len(radar['errors'])}",
        f"- 信心阈值: {radar['min_conviction']}",
    ]
    if radar["actionable"]:
        lines += [
            "",
            "## 达标信号",
            "",
            "| 币种 | 方向 | 信心 | 仓位% | 折算后% | 入场 | 止损 | 教训 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in radar["actionable"]:
            adj = r.get("position_pct_adjusted")
            adj_cell = adj if adj is not None else "—"
            lines.append(
                f"| {r['symbol']} | {r.get('direction') or '—'} | {r.get('conviction_score')} "
                f"| {r.get('position_pct')} | {adj_cell} | {r.get('entry_zone') or '—'} | {r.get('stop_loss') or '—'} "
                f"| {('⚠️x'+str(r['lesson_count'])) if r.get('lesson_count') else '—'} |"
            )
    else:
        lines += ["", "> 本轮无达标信号。"]

    p = radar.get("portfolio")
    if isinstance(p, dict) and "_error" not in p:
        lines += [
            "",
            "## 组合相关性折算（T-10 破分散假象）",
            "",
            f"- 名义总仓位: **{p.get('naive_total_pct')}%** → 相关性有效敞口: **{p.get('effective_before_pct')}%**"
            f"（分散比 {p.get('diversification_ratio')}，平均相关 {p.get('avg_offdiag_corr')}，源 {p.get('corr_source')}）",
        ]
        if p.get("scaled"):
            lines.append(
                f"- ⚠ 有效敞口超上限 {p.get('cap_pct')}% → 各偏多仓位等比缩减 ×{p.get('scale_factor')}，"
                f"缩减后有效敞口 {p.get('effective_after_pct')}%。"
            )
        else:
            lines.append(f"- ✅ 有效敞口未超上限 {p.get('cap_pct')}%，仓位无需缩减。")
    elif isinstance(p, dict):
        lines += ["", f"> 相关性折算失败：{p.get('_error')}"]
    if radar["errors"]:
        lines += ["", f"## 扫描失败 {len(radar['errors'])} 币", ""]
        for e in radar["errors"]:
            lines.append(f"- {e['symbol']}: {e['error']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯多币种机会雷达")
    ap.add_argument("--symbols", default=None, help="逗号分隔，如 BTC,ETH,SOL；默认内置篮子")
    ap.add_argument("--min-conviction", type=float, default=0.8, help="偏多达标的信心阈值")
    ap.add_argument("--max-effective", type=float, default=jcorr.DEFAULT_MAX_EFFECTIVE_PCT,
                    help="组合相关性有效敞口上限%%（T-10），超限等比缩减偏多仓位")
    ap.add_argument("--corr-days", type=int, default=30, help="相关性估计用的日线天数")
    ap.add_argument("--notify", action="store_true", help="扫完把达标信号推送出去")
    ap.add_argument("--dry-run", action="store_true", help="配合 --notify：只打印不真发")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    syms = [s for s in args.symbols.split(",")] if args.symbols else None
    radar = scan(syms, min_conviction=args.min_conviction,
                 max_effective_pct=args.max_effective, corr_days=args.corr_days)

    if args.json:
        print(json.dumps(radar, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(radar))

    if args.notify:
        try:
            import jarvis_notify as jn
            res = jn.send_radar(radar, dry_run=args.dry_run)
            if not args.json:
                print(f"\n[通知] {res}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[通知失败] {exc!r}"[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
