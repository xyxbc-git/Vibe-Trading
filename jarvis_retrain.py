#!/usr/bin/env python3
"""贾维斯 JARVIS - 误差沉淀 → 温和重训因子权重（T-17，自进化闭环的「训」）。

闭环角色：
  养（daemon record+evaluate）→ 攒出带真实前向收益的历史战绩（jarvis_journal）
  → 训（本模块）：按因子统计「触发 vs 未触发」的未来收益差，据此**温和**调整
     jarvis_weights 里的因子权重 → 越跑越贴合真实表现。

为什么这样设计（吸取 P4 过拟合教训）：
  - 只调「价格-only 因子」(jarvis_weights.PRICE_ONLY_FACTORS)：因为 journal 历史快照
    只稳定存了 dd30_active / fng / above_ma200，可如实重建这些因子是否触发；
    衍生品(funding)/突破因子历史难回溯，不在本模块调整范围（避免拿脏数据训）。
  - 三重护栏防过拟合 / 防失控：
      1) 样本门槛 min_n：触发样本太少不调（小样本最易过拟合，见 P4 -50% 假 edge）。
      2) OOS 一致性：把样本按时间切两半，训练段与验证段的「对齐 edge」必须同号
         才采纳调整（样本外不复现就不信，正是 P4 识破假 edge 的方法）。
      3) 步长 + 边界双夹：单次调整不超过 max_step；最终权重夹到 weights.WEIGHT_BOUNDS。
  - 默认 **dry-run**：只产出建议报告，不写盘；--apply 才落 jarvis_weights（version+1，可回退）。

用法：
  python jarvis_retrain.py                      # dry-run：看建议，不改权重
  python jarvis_retrain.py --symbol BTCUSDT
  python jarvis_retrain.py --apply              # 采纳建议，写入 jarvis_weights.json
  python jarvis_retrain.py --json
"""

from __future__ import annotations

import argparse
import json
import sys

import jarvis_weights as jw

HORIZON_DEFAULT = 30

# 价格-only 因子的「意图方向」：+1 期望触发后上涨（看多贡献）；-1 期望触发后偏弱（看空贡献）。
FACTOR_SIGN: dict[str, int] = {
    "dd30_dip": +1,
    "fear_in_downtrend": +1,
    "ma200_above": +1,
    "fear_in_uptrend": -1,
    "ma200_below": -1,
}


def factor_active(snap: dict, factor: str) -> bool | None:
    """根据历史快照存的字段，重建某价格因子当时是否触发。无法判定返回 None。"""
    fng = snap.get("fng")
    above = snap.get("above_ma200")
    dd30 = snap.get("dd30_active")
    if factor == "dd30_dip":
        return None if dd30 is None else bool(dd30)
    if above is None:
        return None
    above_b = bool(above)
    if factor == "ma200_above":
        return above_b
    if factor == "ma200_below":
        return not above_b
    if factor in ("fear_in_downtrend", "fear_in_uptrend"):
        if fng is None:
            return None
        fear = fng < 20
        if factor == "fear_in_downtrend":
            return fear and not above_b
        return fear and above_b
    return None


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def aligned_edge(rows: list[dict], factor: str) -> dict:
    """计算某因子的「对齐 edge」：触发子集平均前向收益 − 未触发子集平均，乘意图方向。

    rows 每项需含 fwd_ret_pct 及快照字段（fng/above_ma200/dd30_active）。
    返回 {n_trig, n_not, mean_trig, mean_not, raw_edge, aligned_edge, hit_rate_trig}。
    aligned_edge>0 = 因子在其意图方向上「有效」；<0 = 反效果应收缩。
    """
    trig, not_trig = [], []
    for r in rows:
        ret = r.get("fwd_ret_pct")
        if ret is None:
            continue
        a = factor_active(r, factor)
        if a is None:
            continue
        (trig if a else not_trig).append(float(ret))
    sign = FACTOR_SIGN.get(factor, +1)
    mt, mn = _mean(trig), _mean(not_trig)
    raw = round(mt - mn, 4) if (mt is not None and mn is not None) else None
    al = round(raw * sign, 4) if raw is not None else None
    # 命中率：正向因子看触发后涨的比例，负向因子看触发后跌的比例
    if trig:
        if sign > 0:
            hits = sum(1 for x in trig if x > 0)
        else:
            hits = sum(1 for x in trig if x < 0)
        hit_rate = round(100 * hits / len(trig), 1)
    else:
        hit_rate = None
    return {
        "factor": factor,
        "sign": sign,
        "n_trig": len(trig),
        "n_not": len(not_trig),
        "mean_trig": mt,
        "mean_not": mn,
        "raw_edge": raw,
        "aligned_edge": al,
        "hit_rate_trig": hit_rate,
    }


def propose(
    rows: list[dict],
    *,
    weights: dict[str, float] | None = None,
    min_n: int = 8,
    learning_rate: float = 0.05,
    max_step: float = 0.1,
) -> list[dict]:
    """对每个价格-only 因子产出权重调整建议（含 OOS 一致性判定）。纯函数，不写盘。

    采纳条件（全满足才 adopt）：
      - 全样本触发数 ≥ min_n；
      - 时间切半后，训练段与验证段触发数各 ≥ max(3, min_n//2)；
      - 全样本 / 训练段 / 验证段三者 aligned_edge 同号（样本外可复现）。
    delta = sign · clip(learning_rate · aligned_edge, −max_step, max_step)；
    new = clamp_weight(name, current + delta)。
    """
    W = dict(weights or jw.get_weights())
    # 按时间排序后中点切分（as_of_date 字典序即时间序）
    ordered = sorted([r for r in rows if r.get("fwd_ret_pct") is not None],
                     key=lambda r: r.get("as_of_date") or "")
    mid = len(ordered) // 2
    train, test = ordered[:mid], ordered[mid:]

    out: list[dict] = []
    for factor in jw.PRICE_ONLY_FACTORS:
        full = aligned_edge(ordered, factor)
        tr = aligned_edge(train, factor)
        te = aligned_edge(test, factor)
        cur = round(float(W.get(factor, jw.DEFAULT_WEIGHTS.get(factor, 0.0))), 4)
        sign = FACTOR_SIGN.get(factor, +1)
        rec = {
            "factor": factor,
            "current_weight": cur,
            "full": full,
            "train_aligned_edge": tr["aligned_edge"],
            "test_aligned_edge": te["aligned_edge"],
            "adopted": False,
            "proposed_weight": cur,
            "delta": 0.0,
            "reason": "",
        }
        ae = full["aligned_edge"]
        if full["n_trig"] < min_n:
            rec["reason"] = f"触发样本 {full['n_trig']} < 门槛 {min_n}，不调（防小样本过拟合）"
            out.append(rec)
            continue
        half_min = max(3, min_n // 2)
        if tr["n_trig"] < half_min or te["n_trig"] < half_min:
            rec["reason"] = f"OOS 切分后样本不足（train={tr['n_trig']}/test={te['n_trig']}<{half_min}），不调"
            out.append(rec)
            continue
        if ae is None or tr["aligned_edge"] is None or te["aligned_edge"] is None:
            rec["reason"] = "edge 不可计算（数据缺失），不调"
            out.append(rec)
            continue
        same_sign = (ae > 0 and tr["aligned_edge"] > 0 and te["aligned_edge"] > 0) or \
                    (ae < 0 and tr["aligned_edge"] < 0 and te["aligned_edge"] < 0)
        if not same_sign:
            rec["reason"] = (f"OOS 不一致（full={ae}/train={tr['aligned_edge']}/test={te['aligned_edge']} "
                             f"非同号），样本外不复现，不调（P4 教训）")
            out.append(rec)
            continue
        raw_delta = learning_rate * ae
        clipped = max(-max_step, min(max_step, raw_delta))
        delta = round(sign * clipped, 4)
        new_w = jw.clamp_weight(factor, cur + delta)
        rec["adopted"] = abs(new_w - cur) > 1e-9
        rec["proposed_weight"] = new_w
        rec["delta"] = round(new_w - cur, 4)
        if not rec["adopted"]:
            rec["reason"] = "调整量被边界夹没（已在 WEIGHT_BOUNDS 边缘），维持原值"
        else:
            arrow = "↑增强" if (new_w - cur) * sign > 0 else "↓收缩"
            rec["reason"] = (f"OOS 同号有效（aligned_edge={ae}，触发 {full['n_trig']} 次，"
                             f"触发命中 {full['hit_rate_trig']}%）→ {arrow} {cur}→{new_w}")
        out.append(rec)
    return out


def _load_rows(symbol: str | None, horizon: int) -> list[dict]:
    """从 jarvis_journal DB 读取「快照字段 + 指定 horizon 的前向收益」联表行。"""
    import jarvis_journal as jj
    jj.init_db()
    with jj._conn() as conn:
        q = (
            "SELECT s.symbol, s.as_of_date, s.fng, s.above_ma200, s.dd30_active, "
            "       o.fwd_ret_pct AS fwd_ret_pct "
            "FROM snapshots s JOIN outcomes o "
            "  ON o.snapshot_id = s.id AND o.horizon = ? "
            "WHERE o.fwd_ret_pct IS NOT NULL"
        )
        params: list = [horizon]
        if symbol:
            q += " AND s.symbol = ?"
            params.append(symbol.upper())
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def run(
    symbol: str | None = None,
    *,
    horizon: int = HORIZON_DEFAULT,
    apply: bool = False,
    min_n: int = 8,
    learning_rate: float = 0.05,
    max_step: float = 0.1,
) -> dict:
    """读历史战绩 → 产建议 →（可选）写权重。返回结构化结果。"""
    rows = _load_rows(symbol, horizon)
    proposals = propose(rows, min_n=min_n, learning_rate=learning_rate, max_step=max_step)
    adopted = [p for p in proposals if p["adopted"]]
    result = {
        "symbol": symbol.upper() if symbol else "ALL",
        "horizon": horizon,
        "samples": len(rows),
        "proposals": proposals,
        "adopted_count": len(adopted),
        "applied": False,
    }
    if apply and adopted:
        new_weights = {p["factor"]: p["proposed_weight"] for p in adopted}
        note = f"retrain h={horizon} n={len(rows)} adopt={len(adopted)} sym={result['symbol']}"
        cfg = jw.save(new_weights, source="retrain", note=note)
        result["applied"] = True
        result["new_version"] = cfg["meta"]["version"]
    return result


def to_markdown(res: dict) -> str:
    lines = [
        f"# 贾维斯重训建议 — {res['symbol']}（{res['horizon']}d 前瞻）",
        f"- 历史样本: {res['samples']} 条 | 采纳调整: {res['adopted_count']} 项 | "
        f"已写入权重: {'是 v' + str(res.get('new_version')) if res.get('applied') else '否（dry-run）'}",
        "",
        "| 因子 | 当前 | 建议 | Δ | 采纳 | 依据 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for p in res["proposals"]:
        lines.append(
            f"| {p['factor']} | {p['current_weight']} | {p['proposed_weight']} | "
            f"{p['delta']:+} | {'✅' if p['adopted'] else '—'} | {p['reason']} |"
        )
    if res["samples"] == 0:
        lines += ["", "> 暂无已评估样本。先用 `jarvis_journal backfill` 造历史，或让 daemon 跑一阵 record+evaluate。"]
    elif res["adopted_count"] == 0:
        lines += ["", "> 本轮无采纳：样本不足 / OOS 不一致 / 已在边界。这是**好的保守**——宁可不调也不过拟合。"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯误差沉淀→温和重训因子权重（T-17）")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--horizon", type=int, default=HORIZON_DEFAULT)
    ap.add_argument("--apply", action="store_true", help="采纳建议并写入 jarvis_weights（默认 dry-run）")
    ap.add_argument("--min-n", type=int, default=8)
    ap.add_argument("--learning-rate", type=float, default=0.05)
    ap.add_argument("--max-step", type=float, default=0.1)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = run(args.symbol, horizon=args.horizon, apply=args.apply,
              min_n=args.min_n, learning_rate=args.learning_rate, max_step=args.max_step)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else to_markdown(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
