#!/usr/bin/env python3
"""T1/T2/T3 运行时验收巡检（正期望重建·任务 K，只读）。

四项验收（源自计划 §T1/§T2/§T3 契约，离线不可证，须重启上线后取数）：
  A1 T1·sl 结算穿透   sl 单 |exit−计划SL|/entry 的 P99 < 0.05%（旧口径基线 1.82%）
  A2 T1·时刻错配      穿透幅度与 holding_minutes 的 Pearson |r| < 0.05（基线 +0.230）
  A3 T2·成交速率      窗口内成交速率 ≥ 50 笔/天（对照 8/05 的 146 笔/天）
  A4 T3·过路费占比    fee/R（=toll_ratio）> 0.2 的成交占比 < 2%（基线约 70%）

只读：仅 SELECT（`_query` 前缀断言），不写库、不下单、不出网。
样本不足显示「不可判定」而非给结论（对齐 T8/T9 的三态纪律）。

用法：
    python3 _t1t2t3_runtime_check.py                     # 默认近 48h（T2 验收窗）
    python3 _t1t2t3_runtime_check.py --window-hours 240  # 自定窗口（如跑旧口径基线）
    python3 _t1t2t3_runtime_check.py --since "2026-08-14 09:00"  # 上线时刻起
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime

import numpy as np

import jarvis_db

# ── 验收阈值（计划契约原文，改动须同步文档）──
P99_PENETRATION_MAX = 0.05      # A1：sl 穿透 P99 上限（%）
CORR_ABS_MAX = 0.05             # A2：穿透×持仓时长 |r| 上限
FILL_RATE_MIN = 50.0            # A3：成交速率下限（笔/天）
TOLL_SHARE_MAX = 2.0            # A4：toll_ratio>0.2 占比上限（%）
TOLL_GATE = 0.20                # A4：过路费占比门槛（T3 判据）

# ── 样本下限（低于则不可判定；P99/相关系数小样本无意义）──
MIN_N_P99 = 20
MIN_N_CORR = 30
MIN_SPAN_HOURS = 6.0            # A3：窗口内实际数据跨度下限


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """只读 SELECT（非 SELECT/WITH 拒绝），jarvis_db 兼容层自动跟随 pg/SQLite。"""
    head = sql.lstrip()[:6].upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise ValueError(f"runtime_check 只允许 SELECT，收到：{sql.lstrip()[:40]!r}")
    conn = jarvis_db.connect(jarvis_db._DEFAULT_SQLITE)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _fee_side_pct() -> float:
    """单边费率%（jarvis_config 只读；异常回退 trader 内置默认 0.05）。"""
    try:
        import jarvis_config as jc
        v = jc.get("twelve_sim_fee_pct")
        return max(0.0, float(v)) if v is not None else 0.05
    except Exception:  # noqa: BLE001 — 配置层异常用默认，巡检不因此中断
        return 0.05


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def check_a1_penetration(trades: list[dict]) -> dict:
    """A1 T1：sl 单 |exit−计划SL|/entry 穿透 P99 < 0.05%。"""
    pen = []
    for t in trades:
        if str(t.get("exit_reason")) != "sl" or t.get("stop_loss") is None:
            continue
        entry = float(t["entry_price"])
        if entry <= 0:
            continue
        pen.append(abs(float(t["exit_price"]) - float(t["stop_loss"])) / entry * 100.0)
    n = len(pen)
    out = {"name": "A1 T1·sl结算穿透 P99", "n_sl": n,
           "threshold": f"< {P99_PENETRATION_MAX}%"}
    if n < MIN_N_P99:
        out.update(verdict="INSUFFICIENT",
                   note=f"sl 样本 {n} < {MIN_N_P99}，不可判定")
        return out
    p99 = float(np.percentile(pen, 99))
    out.update(p99_pct=round(p99, 4),
               exact_at_plan_pct=round(100.0 * sum(1 for x in pen if x < 1e-9) / n, 1),
               verdict=_verdict(p99 < P99_PENETRATION_MAX))
    if n < 100:
        out["note"] = f"样本 {n} < 100，P99 估计方差大，判定置信度低"
    return out


def check_a2_correlation(trades: list[dict]) -> dict:
    """A2 T1：穿透幅度 × holding_minutes 的 Pearson |r| < 0.05（滞后签名已消除）。"""
    xs, ys = [], []
    for t in trades:
        if str(t.get("exit_reason")) != "sl" or t.get("stop_loss") is None \
                or t.get("holding_minutes") is None:
            continue
        entry = float(t["entry_price"])
        if entry <= 0:
            continue
        xs.append(abs(float(t["exit_price"]) - float(t["stop_loss"])) / entry * 100.0)
        ys.append(float(t["holding_minutes"]))
    n = len(xs)
    out = {"name": "A2 T1·穿透×持仓时长相关", "n_sl": n,
           "threshold": f"|r| < {CORR_ABS_MAX}"}
    if n < MIN_N_CORR:
        out.update(verdict="INSUFFICIENT",
                   note=f"sl 样本 {n} < {MIN_N_CORR}，相关系数不可判定")
        return out
    xa, ya = np.array(xs), np.array(ys)
    if float(xa.std()) == 0.0 or float(ya.std()) == 0.0:
        # 穿透恒定（如全按计划位+常数滑点结算）= T1 修复的理想态 → 无相关，达标
        out.update(r=0.0, verdict="PASS", note="穿透方差为 0（结算恒常），视同无相关")
        return out
    r = float(np.corrcoef(xa, ya)[0, 1])
    out.update(r=round(r, 4), verdict=_verdict(abs(r) < CORR_ABS_MAX))
    # 文档基线口径对照（§T1 的 +0.230 只算被劣化的 sl 单）：仅供对账，不参与判定
    mask = xa > 1e-9
    if int(mask.sum()) >= MIN_N_CORR and float(xa[mask].std()) > 0:
        out["r_degraded_only"] = round(float(np.corrcoef(xa[mask], ya[mask])[0, 1]), 4)
    return out


def check_a3_fill_rate(trades: list[dict], window_hours: float) -> dict:
    """A3 T2：窗口内成交速率 ≥ 50 笔/天（按窗口内实际数据跨度折算）。"""
    n = len(trades)
    out = {"name": "A3 T2·成交速率", "n_trades": n,
           "threshold": f">= {FILL_RATE_MIN} 笔/天"}
    if n == 0:
        out.update(verdict="INSUFFICIENT", note="窗口内零成交——若在 T2 上线后仍持续，"
                   "本身就是 FAIL 信号（对照 8/05 的 146 笔/天），请扩窗复核")
        return out
    ts = [float(t["exit_ts"]) for t in trades if t.get("exit_ts") is not None]
    span_h = (max(ts) - min(ts)) / 3600.0 if len(ts) >= 2 else 0.0
    # 跨度取「窗口长度」与「实际数据跨度」中更能代表在场时段的那个：
    # 数据只占窗口一角时（如刚重启 2h），按窗口长度算会稀释速率
    eff_h = max(span_h, min(window_hours, span_h * 2)) if span_h > 0 else window_hours
    if eff_h < MIN_SPAN_HOURS:
        out.update(verdict="INSUFFICIENT",
                   note=f"有效跨度 {eff_h:.1f}h < {MIN_SPAN_HOURS}h，速率不可判定")
        return out
    rate = n / (eff_h / 24.0)
    out.update(rate_per_day=round(rate, 1), span_hours=round(span_h, 1),
               verdict=_verdict(rate >= FILL_RATE_MIN))
    return out


def check_a4_toll_share(trades: list[dict], fee_side_pct: float) -> dict:
    """A4 T3：fee/R（=toll_ratio）> 0.2 的成交占比 < 2%。

    toll_ratio 列直读；NULL（T3 上线前旧行）按 2×费率÷计划SL距离% 兜底重算，
    与 T3/归因分档同口径。stop_loss 也缺的行计入 no_sl 显式曝光。
    """
    n_over = n_ok = n_no_sl = 0
    for t in trades:
        ratio = t.get("toll_ratio")
        if ratio is None:
            try:
                entry = float(t["entry_price"])
                sl_dist = abs(entry - float(t["stop_loss"])) / entry * 100.0
                ratio = 2.0 * fee_side_pct / sl_dist if sl_dist > 0 else None
            except (TypeError, ValueError, KeyError, ZeroDivisionError):
                ratio = None
        if ratio is None or not math.isfinite(float(ratio)):
            n_no_sl += 1
            continue
        if float(ratio) > TOLL_GATE:
            n_over += 1
        else:
            n_ok += 1
    n = n_over + n_ok
    out = {"name": "A4 T3·toll_ratio>0.2 占比", "n_trades": n, "n_no_sl": n_no_sl,
           "threshold": f"< {TOLL_SHARE_MAX}%"}
    if n == 0:
        out.update(verdict="INSUFFICIENT", note="窗口内无可算 toll 的成交")
        return out
    share = n_over / n * 100.0
    out.update(over_share_pct=round(share, 2), n_over=n_over,
               verdict=_verdict(share < TOLL_SHARE_MAX))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="T1/T2/T3 运行时验收巡检（只读）")
    ap.add_argument("--window-hours", type=float, default=48.0,
                    help="回看窗口小时数（默认 48 = T2 验收窗）")
    ap.add_argument("--since", help='起点时刻（本地时区 "YYYY-MM-DD HH:MM"），优先于窗口')
    args = ap.parse_args()

    import time
    now = time.time()
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp()
        window_hours = (now - since) / 3600.0
    else:
        window_hours = max(1.0, float(args.window_hours))
        since = now - window_hours * 3600.0

    has_toll = True
    try:
        trades = _query(
            "SELECT exit_reason, entry_price, exit_price, stop_loss, qty, "
            "holding_minutes, exit_ts, toll_ratio "
            "FROM twelve_sim_trade WHERE exit_ts >= ?", (since,))
    except Exception:  # noqa: BLE001 — 旧库无 toll_ratio 列，降级不带列（值走兜底重算）
        has_toll = False
        trades = _query(
            "SELECT exit_reason, entry_price, exit_price, stop_loss, qty, "
            "holding_minutes, exit_ts, NULL AS toll_ratio "
            "FROM twelve_sim_trade WHERE exit_ts >= ?", (since,))

    fee = _fee_side_pct()
    since_str = datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M")
    print("=" * 74)
    print(f"T1/T2/T3 运行时验收巡检  窗口 {since_str} 起（{window_hours:.1f}h）  "
          f"已平仓 {len(trades)} 笔  单边费率 {fee}%"
          + ("" if has_toll else "  [源库无 toll_ratio 列，A4 走兜底重算]"))
    print("  判定三态：PASS / FAIL / INSUFFICIENT（样本不足=不可判定，不给结论）")
    print("=" * 74)

    checks = [
        check_a1_penetration(trades),
        check_a2_correlation(trades),
        check_a3_fill_rate(trades, window_hours),
        check_a4_toll_share(trades, fee),
    ]
    for c in checks:
        keys = [k for k in c if k not in ("name", "verdict", "note", "threshold")]
        detail = "  ".join(f"{k}={c[k]}" for k in keys)
        print(f"\n  {c['name']:<28} [{c['verdict']}]")
        print(f"    阈值 {c['threshold']}    {detail}")
        if c.get("note"):
            print(f"    注：{c['note']}")

    tally = {s: sum(1 for c in checks if c["verdict"] == s)
             for s in ("PASS", "FAIL", "INSUFFICIENT")}
    print("\n" + "=" * 74)
    print("  汇总：" + "  ".join(f"{k}={v}" for k, v in tally.items())
          + "   （T1/T2/T3 重启上线后重跑本脚本，四项全 PASS 即验收通过）")


if __name__ == "__main__":
    main()
