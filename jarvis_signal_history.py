#!/usr/bin/env python3
"""贾维斯 JARVIS — 十二系统信号快照与变更历史（驾驶舱需求 1）。

两张表（挂 ~/.vibe-trading/jarvis_journal.db，经 jarvis_db 兼容层，pg 可切）：
  twelve_signal_state    每 (symbol, tf, system) 一行「当前态」：
                         最近一次计算时间 updated_ts + 最近一次变更时间 changed_ts
  twelve_signal_changes  变更流水：方向/强度/交易计划/关键位任一发生实质变化时
                         记一条，含变更前后完整参数快照（prev_json/new_json）

写入点：dashboard /api/twelve/signals 与 /api/twelve/consensus 的重算路径
（缓存命中不写，天然与「信号刷新」同频）。

变更判定（防止每根 K 线微抖动刷屏）：
  direction  方向翻转                        → 必记
  strength   强度绝对变化 ≥ 0.15             → 记
  plan       计划新增/消失/side 变/entry_type 变/入损盈任一价相对变化 > 0.2% → 记
  levels     关键位数量变化或任一价相对变化 > 0.5%（仅辅助，不单独触发时也入快照）

纯函数核心：diff_signal / _plan_changed / _levels_changed 离线可测
（_signal_history_smoketest.py 注入 mock 信号序列）。
"""

from __future__ import annotations

import json
import os
import time

import jarvis_db as jdb

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")

# 变更判定阈值
STRENGTH_EPS = 0.15          # 强度绝对变化
PLAN_PRICE_EPS_PCT = 0.2     # 计划价相对变化 %
LEVEL_PRICE_EPS_PCT = 0.5    # 关键位价相对变化 %

# 流水保留上限（自动裁剪，防无界增长）
MAX_CHANGE_ROWS = 50_000
# 自动裁剪节流：每小时最多跑一次
_PRUNE_INTERVAL_S = 3600.0
_LAST_PRUNE = 0.0

_INITED = False


def _conn():
    os.makedirs(DB_DIR, exist_ok=True)
    return jdb.connect(DB_PATH)


def init_db() -> None:
    global _INITED
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_signal_state (
                symbol      TEXT NOT NULL,
                tf          TEXT NOT NULL,
                system      TEXT NOT NULL,
                name_cn     TEXT,
                direction   TEXT,
                strength    REAL,
                reasoning   TEXT,
                levels_json TEXT,
                plan_json   TEXT,
                updated_ts  REAL,
                changed_ts  REAL,
                PRIMARY KEY (symbol, tf, system)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_signal_changes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             REAL NOT NULL,
                symbol         TEXT NOT NULL,
                tf             TEXT NOT NULL,
                system         TEXT NOT NULL,
                name_cn        TEXT,
                prev_direction TEXT,
                new_direction  TEXT,
                prev_strength  REAL,
                new_strength   REAL,
                change_kinds   TEXT,
                summary        TEXT,
                prev_json      TEXT,
                new_json       TEXT,
                price          REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tsc_sym_tf_sys_ts "
            "ON twelve_signal_changes(symbol, tf, system, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tsc_ts ON twelve_signal_changes(ts)"
        )
    _INITED = True


def _ensure_init() -> None:
    if not _INITED:
        init_db()


# ─────────────────────────── 纯函数：变更判定 ───────────────────────────


def _rel_moved(a: float | None, b: float | None, eps_pct: float) -> bool:
    """两价相对变化是否超过 eps_pct%（任一缺失且另一存在 = 变化）。"""
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return True
    if fa == fb:
        return False
    base = abs(fa) if abs(fa) > 1e-12 else 1e-12
    return abs(fb - fa) / base * 100.0 > eps_pct


def _plan_changed(prev: dict | None, new: dict | None) -> bool:
    """交易计划实质变化：出现/消失/side/entry_type/三价任一超阈值移动。"""
    if prev is None and new is None:
        return False
    if (prev is None) != (new is None):
        return True
    assert prev is not None and new is not None
    if (prev.get("side") or "") != (new.get("side") or ""):
        return True
    if (prev.get("entry_type") or "") != (new.get("entry_type") or ""):
        return True
    for k in ("entry", "stop_loss", "take_profit"):
        if _rel_moved(prev.get(k), new.get(k), PLAN_PRICE_EPS_PCT):
            return True
    return False


def _levels_changed(prev: list | None, new: list | None) -> bool:
    """关键位实质变化：数量变化或同 label 价格移动超阈值。"""
    p = prev or []
    n = new or []
    if len(p) != len(n):
        return True
    pm = {str(x.get("label")): x.get("price") for x in p if isinstance(x, dict)}
    nm = {str(x.get("label")): x.get("price") for x in n if isinstance(x, dict)}
    if set(pm.keys()) != set(nm.keys()):
        return True
    return any(_rel_moved(pm[k], nm[k], LEVEL_PRICE_EPS_PCT) for k in pm)


_DIR_CN = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}


def diff_signal(prev_row: dict | None, sig: dict) -> tuple[list[str], str]:
    """对比上次状态与本次信号 → (变更类型列表, 一句话摘要)。

    prev_row: state 表行（direction/strength/plan_json/levels_json），None=首见。
    返回 ([], "") 表示无实质变化。首见不算「变更」（只建 state 不记流水）。
    """
    if prev_row is None:
        return [], ""
    kinds: list[str] = []
    bits: list[str] = []

    prev_dir = str(prev_row.get("direction") or "neutral")
    new_dir = str(sig.get("direction") or "neutral")
    if prev_dir != new_dir:
        kinds.append("direction")
        bits.append(f"方向 {_DIR_CN.get(prev_dir, prev_dir)}→{_DIR_CN.get(new_dir, new_dir)}")

    try:
        prev_str = float(prev_row.get("strength") or 0.0)
    except (TypeError, ValueError):
        prev_str = 0.0
    new_str = float(sig.get("strength") or 0.0)
    if abs(new_str - prev_str) >= STRENGTH_EPS:
        kinds.append("strength")
        bits.append(f"强度 {prev_str:.0%}→{new_str:.0%}")

    try:
        prev_plan = json.loads(prev_row.get("plan_json") or "null")
    except (TypeError, ValueError):
        prev_plan = None
    new_plan = sig.get("trade_plan")
    if _plan_changed(prev_plan, new_plan):
        kinds.append("plan")
        if prev_plan is None:
            bits.append("新增交易计划")
        elif new_plan is None:
            bits.append("交易计划撤销")
        else:
            seg = []
            for k, cn in (("entry", "入场"), ("stop_loss", "止损"), ("take_profit", "止盈")):
                if _rel_moved(prev_plan.get(k), new_plan.get(k), PLAN_PRICE_EPS_PCT):
                    seg.append(f"{cn} {prev_plan.get(k)}→{new_plan.get(k)}")
            bits.append("计划调整" + ("（" + "；".join(seg) + "）" if seg else ""))

    try:
        prev_levels = json.loads(prev_row.get("levels_json") or "[]")
    except (TypeError, ValueError):
        prev_levels = []
    if kinds and _levels_changed(prev_levels, sig.get("key_levels")):
        # levels 变化不单独触发流水，只在已触发时附带记录
        kinds.append("levels")
        bits.append("关键位更新")

    return kinds, "；".join(bits)


# ─────────────────────────── 写入：批量记录 ───────────────────────────


def record_batch(symbol: str, tf: str, signals: list[dict],
                 price: float | None = None,
                 now: float | None = None) -> dict[str, dict]:
    """一轮信号计算结果入库：更新 state + 有实质变化的写 changes 流水。

    Returns:
        {system: {"updated_at": ts, "changed_at": ts|None}}，供 API 附到响应。
    失败静默返回 {}（信号主链路绝不能被历史记录拖垮）。
    """
    try:
        _ensure_init()
        ts = float(now if now is not None else time.time())
        sym = (symbol or "").upper()
        out: dict[str, dict] = {}
        changed_events: list[dict] = []  # 供邮件提醒钩子（jarvis_signal_alert）
        with _conn() as conn:
            cur = conn.execute(
                "SELECT system, direction, strength, plan_json, levels_json, changed_ts "
                "FROM twelve_signal_state WHERE symbol=? AND tf=?",
                (sym, tf))
            prev_map = {str(r["system"]): dict(r) for r in cur.fetchall()}

            for sig in signals or []:
                system = str(sig.get("system") or "")
                if not system:
                    continue
                prev = prev_map.get(system)
                kinds, summary = diff_signal(prev, sig)
                changed = bool(kinds)
                changed_ts = ts if changed else (
                    float(prev["changed_ts"]) if prev and prev.get("changed_ts") else None)

                levels_json = json.dumps(sig.get("key_levels") or [], ensure_ascii=False)
                plan_json = json.dumps(sig.get("trade_plan"), ensure_ascii=False)
                conn.execute(
                    """
                    INSERT INTO twelve_signal_state
                      (symbol, tf, system, name_cn, direction, strength, reasoning,
                       levels_json, plan_json, updated_ts, changed_ts)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol, tf, system) DO UPDATE SET
                       name_cn=excluded.name_cn,
                       direction=excluded.direction,
                       strength=excluded.strength,
                       reasoning=excluded.reasoning,
                       levels_json=excluded.levels_json,
                       plan_json=excluded.plan_json,
                       updated_ts=excluded.updated_ts,
                       changed_ts=COALESCE(excluded.changed_ts, twelve_signal_state.changed_ts)
                    """,
                    (sym, tf, system, sig.get("name_cn"), sig.get("direction"),
                     float(sig.get("strength") or 0.0), sig.get("reasoning"),
                     levels_json, plan_json, ts, changed_ts))

                if changed:
                    prev_snapshot = {
                        "direction": prev.get("direction") if prev else None,
                        "strength": prev.get("strength") if prev else None,
                        "trade_plan": (json.loads(prev.get("plan_json") or "null")
                                       if prev else None),
                        "key_levels": (json.loads(prev.get("levels_json") or "[]")
                                       if prev else []),
                    }
                    new_snapshot = {
                        "direction": sig.get("direction"),
                        "strength": sig.get("strength"),
                        "trade_plan": sig.get("trade_plan"),
                        "key_levels": sig.get("key_levels") or [],
                        "reasoning": sig.get("reasoning"),
                    }
                    conn.execute(
                        """
                        INSERT INTO twelve_signal_changes
                          (ts, symbol, tf, system, name_cn,
                           prev_direction, new_direction, prev_strength, new_strength,
                           change_kinds, summary, prev_json, new_json, price)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (ts, sym, tf, system, sig.get("name_cn"),
                         prev.get("direction") if prev else None,
                         sig.get("direction"),
                         prev.get("strength") if prev else None,
                         float(sig.get("strength") or 0.0),
                         json.dumps(kinds, ensure_ascii=False), summary,
                         json.dumps(prev_snapshot, ensure_ascii=False),
                         json.dumps(new_snapshot, ensure_ascii=False),
                         price))
                    changed_events.append({
                        "symbol": sym, "tf": tf, "system": system,
                        "name_cn": sig.get("name_cn"), "summary": summary,
                        "prev_direction": prev.get("direction") if prev else None,
                        "new_direction": sig.get("direction"),
                        "prev_strength": prev.get("strength") if prev else None,
                        "new_strength": sig.get("strength"), "price": price,
                    })

                out[system] = {"updated_at": ts, "changed_at": changed_ts}
        # 邮件提醒钩子：变更落库后检查订阅命中（独立 try，失败绝不拖垮主链路）
        if changed_events:
            try:
                import jarvis_signal_alert as jsa
                jsa.maybe_notify(changed_events)
            except Exception:  # noqa: BLE001
                pass
        # 小时级节流自动裁剪，防流水无界增长
        global _LAST_PRUNE
        if ts - _LAST_PRUNE > _PRUNE_INTERVAL_S:
            _LAST_PRUNE = ts
            prune()
        return out
    except Exception:  # noqa: BLE001 — 历史记录失败绝不拖垮信号主链路
        return {}


def prune(max_rows: int = MAX_CHANGE_ROWS) -> int:
    """裁剪最老流水到上限内；返回删除行数。失败返回 0。"""
    try:
        _ensure_init()
        with _conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM twelve_signal_changes").fetchone()
            n = int(row["n"] if row else 0)
            if n <= max_rows:
                return 0
            overflow = n - max_rows
            conn.execute(
                "DELETE FROM twelve_signal_changes WHERE id IN ("
                "SELECT id FROM twelve_signal_changes ORDER BY ts ASC LIMIT ?)",
                (overflow,))
            return overflow
    except Exception:  # noqa: BLE001
        return 0


# ─────────────────────────── 读取 / 删除（管理界面） ───────────────────────────


def _row_to_change(r: dict) -> dict:
    d = dict(r)
    for k in ("change_kinds", "prev_json", "new_json"):
        try:
            d[k] = json.loads(d.get(k) or "null")
        except (TypeError, ValueError):
            d[k] = None
    return d


def history(symbol: str | None = None, tf: str | None = None,
            system: str | None = None, since: float | None = None,
            until: float | None = None, limit: int = 100,
            offset: int = 0) -> dict:
    """按条件查变更流水（倒序）→ {total, rows}。"""
    try:
        _ensure_init()
        cond, args = [], []
        if symbol:
            cond.append("symbol=?")
            args.append(symbol.upper())
        if tf:
            cond.append("tf=?")
            args.append(tf)
        if system:
            cond.append("system=?")
            args.append(system)
        if since is not None:
            cond.append("ts>=?")
            args.append(float(since))
        if until is not None:
            cond.append("ts<=?")
            args.append(float(until))
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        lim = max(1, min(int(limit), 500))
        off = max(0, int(offset))
        with _conn() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM twelve_signal_changes{where}",
                args).fetchone()
            total = int(total_row["n"] if total_row else 0)
            cur = conn.execute(
                f"SELECT * FROM twelve_signal_changes{where} "
                f"ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (*args, lim, off))
            rows = [_row_to_change(r) for r in cur.fetchall()]
        return {"ok": True, "total": total, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "total": 0, "rows": [], "error": repr(exc)[:200]}


def state(symbol: str, tf: str) -> dict:
    """当前 per-system 状态（updated_ts/changed_ts），管理页/信号矩阵均可用。"""
    try:
        _ensure_init()
        with _conn() as conn:
            cur = conn.execute(
                "SELECT system, name_cn, direction, strength, updated_ts, changed_ts "
                "FROM twelve_signal_state WHERE symbol=? AND tf=? ORDER BY system",
                ((symbol or "").upper(), tf))
            rows = [dict(r) for r in cur.fetchall()]
        return {"ok": True, "rows": rows}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rows": [], "error": repr(exc)[:200]}


def delete_changes(ids: list[int] | None = None, symbol: str | None = None,
                   tf: str | None = None, system: str | None = None,
                   before: float | None = None) -> dict:
    """删除流水：按 id 列表，或按 (symbol/tf/system/before) 条件批量。

    两种模式二选一；均未给出时拒绝（防误删全表）。
    """
    try:
        _ensure_init()
        with _conn() as conn:
            if ids:
                clean = [int(i) for i in ids][:1000]
                ph = ",".join("?" for _ in clean)
                cur = conn.execute(
                    f"DELETE FROM twelve_signal_changes WHERE id IN ({ph})", clean)
                return {"ok": True, "deleted": cur.rowcount}
            cond, args = [], []
            if symbol:
                cond.append("symbol=?")
                args.append(symbol.upper())
            if tf:
                cond.append("tf=?")
                args.append(tf)
            if system:
                cond.append("system=?")
                args.append(system)
            if before is not None:
                cond.append("ts<?")
                args.append(float(before))
            if not cond:
                return {"ok": False, "deleted": 0,
                        "error": "缺少删除条件（ids 或 symbol/tf/system/before 至少一项）"}
            cur = conn.execute(
                "DELETE FROM twelve_signal_changes WHERE " + " AND ".join(cond), args)
            return {"ok": True, "deleted": cur.rowcount}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "deleted": 0, "error": repr(exc)[:200]}


# ─────────────────────────── CLI ───────────────────────────


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="十二系统信号变更历史")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("history", help="查流水")
    h.add_argument("--symbol", default=None)
    h.add_argument("--tf", default=None)
    h.add_argument("--system", default=None)
    h.add_argument("--limit", type=int, default=20)
    sub.add_parser("prune", help="裁剪流水到上限")
    args = ap.parse_args()
    if args.cmd == "history":
        print(json.dumps(history(args.symbol, args.tf, args.system,
                                 limit=args.limit), ensure_ascii=False, indent=2))
    elif args.cmd == "prune":
        print(f"pruned {prune()} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
