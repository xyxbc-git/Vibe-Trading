#!/usr/bin/env python3
"""贾维斯 → RuoYi 同步器 · 通道 A：源库增量同步任务（T3）。

八张镜像表：
  fast 组（5s）：
    jarvis_signal_state        ← twelve_signal_state    （updated_ts 游标 + 5s 重叠，upsert 覆盖）
    jarvis_signal_change       ← twelve_signal_changes  （id 游标，追加型 no-op upsert）
  mid 组（60s）：
    jarvis_tape_bar            ← tape_minute_bars       （minute 游标 + 2 分钟重叠，upsert 覆盖）
    jarvis_intraday_prediction ← intraday_predictions   （bar_ts 游标 + 重推未回填行，PK=源 id 覆盖）
    jarvis_snapshot            ← snapshots              （created_ts 游标 + 7 天窗口重刷，PK=源 id 覆盖）
    jarvis_outcome             ← outcomes               （evaluated_ts 游标 + 60s 重叠，uk 覆盖）
    jarvis_position            ← paper_positions        （双游标 + 全量重推 open 态，PK=源 id 覆盖）
    jarvis_limit_order         ← limit_orders           （created_ts 游标 + 全量重推 pending，PK=源 id 覆盖）
    jarvis_force_order_min     ← force_orders(独立SQLite)（id 游标 → 整分钟桶聚合重算覆盖）

纪律（方案 §2.2-§2.6 / 开发计划 §3.3）：
  - 源侧零写入：jarvis_db 兼容层（自动跟随 pg/SQLite）只读；force_orders 以 ro URI 打开。
  - 显式列名 SELECT，禁止 SELECT *（源侧未来加列不破坏链路）。
  - 游标从不后退：每批 executemany + commit 成功后才推进并落盘。
  - 源表暂缺（twelve_* 懒建）容忍：记 warn 返回 0 行，不算失败。
  - 时区口径：MySQL 侧所有 DATETIME 列统一存东八区（GMT+8）挂钟时间。

时间单位备忘（源码核对，文件:行号见开发计划 §1.1）：
  twelve_signal_state.updated_ts / changes.ts / snapshots.created_ts /
  paper_positions.opened_ts,closed_ts / limit_orders.created_ts,filled_ts,cancel_ts
      → epoch 秒（float）
  intraday_predictions.bar_ts → epoch 毫秒（int，jarvis_intraday_trader.py BAR_MS=4h*1000）
  tape_minute_bars.minute     → epoch 分钟数（int，minute*60=epoch 秒）
  force_orders.trade_time     → epoch 毫秒（int）
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from jarvis_sync import SyncContext, TaskResult, register_task

log = logging.getLogger("jarvis_sync.tasks_a")

TZ8 = ZoneInfo("Asia/Shanghai")

SRC_DB = os.path.expanduser("~/.vibe-trading/jarvis_journal.db")
FORCE_DB = os.path.expanduser("~/.vibe-trading/jarvis_ws_force_orders.db")

# 源表暂缺 warn 节流（每表只提示一次，恢复后自动复位）
_missing_warned: set[str] = set()

# ══════════════════════════════════════════════════════ 公共 helper


def _dt8(epoch_s: Optional[float]) -> Optional[str]:
    """epoch 秒 → 东八区 'YYYY-MM-DD HH:MM:SS.mmm' 字符串（DATETIME(3) 直插）。"""
    if epoch_s is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(epoch_s), TZ8)
        return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    except (ValueError, OverflowError, OSError):
        return None


def _src_conn():
    """贾维斯源库连接（经 jarvis_db 兼容层，自动跟随 pg/SQLite；只读使用）。"""
    import jarvis_db

    return jarvis_db.connect(SRC_DB)


def _force_conn() -> Optional[sqlite3.Connection]:
    """force_orders 独立 SQLite，ro URI 打开；文件不存在返回 None（WS 未开过）。"""
    if not os.path.exists(FORCE_DB):
        return None
    conn = sqlite3.connect(f"file:{FORCE_DB}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _is_missing_table(err: Exception) -> bool:
    """源表尚未建出（twelve_* 懒建 / 全新库）的跨后端判定。"""
    msg = str(err).lower()
    return "no such table" in msg or "does not exist" in msg or "doesn't exist" in msg


def _tolerate_missing(table: str, err: Exception) -> Optional[TaskResult]:
    if _is_missing_table(err):
        if table not in _missing_warned:
            log.warning("[%s] 源表尚未创建（%s），等待源侧首次写入", table, err)
            _missing_warned.add(table)
        return TaskResult(rows=0)
    return None


def _mysql_or_skip(ctx: SyncContext, table: str) -> Optional[object]:
    conn = ctx.mysql.get()
    if conn is None:
        return None
    return conn


def _upsert_many(mysql_conn, sql: str, rows: list[tuple], exec_batch: int) -> None:
    """分批 executemany + 单次 commit；异常 rollback 后向上抛（框架记 error，游标不动）。

    rollback 保证半截批次不残留在连接事务里（与 tasks_b 同纪律）；uk/PK 幂等
    覆盖使下轮整批重试无重复风险。
    """
    try:
        with mysql_conn.cursor() as cur:
            for i in range(0, len(rows), exec_batch):
                cur.executemany(sql, rows[i : i + exec_batch])
        mysql_conn.commit()
    except Exception:
        try:
            mysql_conn.rollback()
        except Exception:  # noqa: BLE001 — 连接已断时 rollback 本身可失败，不掩盖原异常
            pass
        raise


def _catchup(pull_once) -> tuple[int, Optional[float]]:
    """追平模式：连续拉批直到批不满（方案 §2.3）。

    pull_once() -> (batch_rows:int, max_biz_ts:float|None, full:bool)
    Returns: (总行数, 最大业务时间)
    """
    total = 0
    max_ts: Optional[float] = None
    while True:
        n, ts, full = pull_once()
        total += n
        if ts is not None:
            max_ts = ts if max_ts is None else max(max_ts, ts)
        if not full:
            return total, max_ts
        time.sleep(0.05)


# ══════════════════════════════════════════════════════ fast 组


_SQL_STATE_SRC = (
    "SELECT symbol, tf, system, name_cn, direction, strength, reasoning, "
    "levels_json, plan_json, updated_ts, changed_ts "
    "FROM twelve_signal_state WHERE updated_ts >= ? ORDER BY updated_ts LIMIT ?"
)

_SQL_STATE_DST = (
    "INSERT INTO jarvis_signal_state "
    "(symbol, tf, system_code, name_cn, direction, strength, reasoning, "
    " levels_json, plan_json, src_updated_ts, src_changed_ts, updated_at, changed_at) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "name_cn=VALUES(name_cn), direction=VALUES(direction), strength=VALUES(strength), "
    "reasoning=VALUES(reasoning), levels_json=VALUES(levels_json), plan_json=VALUES(plan_json), "
    "src_updated_ts=VALUES(src_updated_ts), src_changed_ts=VALUES(src_changed_ts), "
    "updated_at=VALUES(updated_at), changed_at=VALUES(changed_at)"
)


@register_task(group="fast", table="jarvis_signal_state")
def sync_signal_state(ctx: SyncContext) -> TaskResult:
    """信号当前态：updated_ts 游标 + 5s 重叠窗，全值列覆盖 upsert。"""
    table = "jarvis_signal_state"
    mysql_conn = _mysql_or_skip(ctx, table)
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    cur_raw = ctx.cursors.get(table)
    cursor = float(cur_raw) if cur_raw else 0.0
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])

    try:
        with _src_conn() as src:
            rows = src.execute(_SQL_STATE_SRC, (max(0.0, cursor - 5.0), batch)).fetchall()
    except Exception as e:  # noqa: BLE001 — 源表懒建容忍
        tol = _tolerate_missing(table, e)
        if tol is not None:
            return tol
        raise
    _missing_warned.discard(table)
    if not rows:
        return TaskResult(rows=0, cursor_value=cur_raw)

    payload = []
    max_ts = cursor
    for r in rows:
        upd = float(r["updated_ts"] or 0.0)
        max_ts = max(max_ts, upd)
        payload.append((
            r["symbol"], r["tf"], r["system"], r["name_cn"], r["direction"],
            r["strength"], r["reasoning"], r["levels_json"] or None, r["plan_json"] or None,
            r["updated_ts"], r["changed_ts"], _dt8(r["updated_ts"]), _dt8(r["changed_ts"]),
        ))
    _upsert_many(mysql_conn, _SQL_STATE_DST, payload, exec_batch)
    ctx.cursors.set(table, str(max_ts))
    return TaskResult(rows=len(payload), cursor_value=str(max_ts),
                      lag_seconds=max(0.0, time.time() - max_ts))


_SQL_CHANGE_SRC = (
    "SELECT id, ts, symbol, tf, system, name_cn, prev_direction, new_direction, "
    "prev_strength, new_strength, change_kinds, summary, prev_json, new_json, price "
    "FROM twelve_signal_changes WHERE id > ? ORDER BY id LIMIT ?"
)

_SQL_CHANGE_DST = (
    "INSERT INTO jarvis_signal_change "
    "(id, signal_time, src_ts, symbol, tf, system_code, name_cn, prev_direction, "
    " new_direction, prev_strength, new_strength, change_kinds, summary, prev_json, "
    " new_json, price) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE id=id"
)


@register_task(group="fast", table="jarvis_signal_change")
def sync_signal_change(ctx: SyncContext) -> TaskResult:
    """信号变更流水：id 单调游标，追加型 no-op upsert（天然幂等）。"""
    table = "jarvis_signal_change"
    mysql_conn = _mysql_or_skip(ctx, table)
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])
    state = {"cursor": int(ctx.cursors.get(table) or 0)}

    def pull_once() -> tuple[int, Optional[float], bool]:
        try:
            with _src_conn() as src:
                rows = src.execute(_SQL_CHANGE_SRC, (state["cursor"], batch)).fetchall()
        except Exception as e:  # noqa: BLE001
            if _is_missing_table(e):
                if table not in _missing_warned:
                    log.warning("[%s] 源表尚未创建（%s），等待源侧首次写入", table, e)
                    _missing_warned.add(table)
                return 0, None, False
            raise
        _missing_warned.discard(table)
        if not rows:
            return 0, None, False
        payload = [(
            r["id"], _dt8(r["ts"]), r["ts"], r["symbol"], r["tf"], r["system"],
            r["name_cn"], r["prev_direction"], r["new_direction"], r["prev_strength"],
            r["new_strength"], r["change_kinds"], r["summary"],
            r["prev_json"] or None, r["new_json"] or None, r["price"],
        ) for r in rows]
        _upsert_many(mysql_conn, _SQL_CHANGE_DST, payload, exec_batch)
        state["cursor"] = int(rows[-1]["id"])
        ctx.cursors.set(table, str(state["cursor"]))
        return len(rows), float(rows[-1]["ts"] or 0.0), len(rows) >= batch

    total, max_ts = _catchup(pull_once)
    return TaskResult(rows=total, cursor_value=str(state["cursor"]),
                      lag_seconds=max(0.0, time.time() - max_ts) if max_ts else None)


# ══════════════════════════════════════════════════════ mid 组


_SQL_TAPE_SRC = (
    "SELECT symbol, minute, buy_usd, sell_usd, nr_buy_usd, nr_sell_usd, "
    "open_price, close_price, high_price, low_price, trades_n "
    "FROM tape_minute_bars WHERE minute > ? ORDER BY minute LIMIT ?"
)

_SQL_TAPE_DST = (
    "INSERT INTO jarvis_tape_bar "
    "(symbol, minute_time, src_minute, buy_usd, sell_usd, nr_buy_usd, nr_sell_usd, "
    " open_price, close_price, high_price, low_price, trades_n) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "minute_time=VALUES(minute_time), buy_usd=VALUES(buy_usd), sell_usd=VALUES(sell_usd), "
    "nr_buy_usd=VALUES(nr_buy_usd), nr_sell_usd=VALUES(nr_sell_usd), "
    "open_price=VALUES(open_price), close_price=VALUES(close_price), "
    "high_price=VALUES(high_price), low_price=VALUES(low_price), trades_n=VALUES(trades_n)"
)


@register_task(group="mid", table="jarvis_tape_bar")
def sync_tape_bar(ctx: SyncContext) -> TaskResult:
    """盘口分钟聚合：minute 游标 + 2 分钟重叠（源 30s flush 可能补写迟到分钟）。"""
    table = "jarvis_tape_bar"
    mysql_conn = _mysql_or_skip(ctx, table)
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])
    # [T8-③] 追平护栏：LIMIT 必须能装下整个重叠窗（2 分钟 × 币数），否则每轮
    # 返回的全是重叠窗旧行、游标不前进且批恒满 → _catchup 死循环
    overlap_rows = 2 * max(1, len(ctx.symbols)) + 8
    if batch <= overlap_rows:
        log.warning("[%s] batch_size=%d ≤ 重叠窗预估 %d 行，自动抬升防追平死循环",
                    table, batch, overlap_rows)
        batch = overlap_rows + 1
    state = {"cursor": int(ctx.cursors.get(table) or 0)}

    def pull_once() -> tuple[int, Optional[float], bool]:
        try:
            with _src_conn() as src:
                rows = src.execute(
                    _SQL_TAPE_SRC, (max(0, state["cursor"] - 2), batch)
                ).fetchall()
        except Exception as e:  # noqa: BLE001
            if _is_missing_table(e):
                if table not in _missing_warned:
                    log.warning("[%s] 源表尚未创建（%s），等待源侧首次写入", table, e)
                    _missing_warned.add(table)
                return 0, None, False
            raise
        _missing_warned.discard(table)
        if not rows:
            return 0, None, False
        payload = [(
            r["symbol"], _dt8(int(r["minute"]) * 60), int(r["minute"]),
            r["buy_usd"], r["sell_usd"], r["nr_buy_usd"], r["nr_sell_usd"],
            r["open_price"], r["close_price"], r["high_price"], r["low_price"],
            r["trades_n"],
        ) for r in rows]
        _upsert_many(mysql_conn, _SQL_TAPE_DST, payload, exec_batch)
        new_cur = max(int(r["minute"]) for r in rows)
        if new_cur > state["cursor"]:
            state["cursor"] = new_cur
            ctx.cursors.set(table, str(new_cur))
        return len(rows), float(new_cur * 60), len(rows) >= batch

    total, max_ts = _catchup(pull_once)
    return TaskResult(rows=total, cursor_value=str(state["cursor"]),
                      lag_seconds=max(0.0, time.time() - max_ts) if max_ts else None)


_SQL_PRED_SRC = (
    "SELECT id, symbol, bar_ts, direction, prob, tradeable, entry, stop, take, "
    "atr_pct, oos_hit_rate, p_value, reason, why_text, outcome_ret, hit "
    "FROM intraday_predictions WHERE bar_ts > ? OR outcome_ret IS NULL "
    "ORDER BY bar_ts LIMIT ?"
)

_SQL_PRED_DST = (
    "INSERT INTO jarvis_intraday_prediction "
    "(id, symbol, bar_time, src_bar_ts, direction, prob, tradeable, entry, stop, take, "
    " atr_pct, oos_hit_rate, p_value, reason, why_text, outcome_ret, hit) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "direction=VALUES(direction), prob=VALUES(prob), tradeable=VALUES(tradeable), "
    "entry=VALUES(entry), stop=VALUES(stop), take=VALUES(take), atr_pct=VALUES(atr_pct), "
    "oos_hit_rate=VALUES(oos_hit_rate), p_value=VALUES(p_value), reason=VALUES(reason), "
    "why_text=VALUES(why_text), outcome_ret=VALUES(outcome_ret), hit=VALUES(hit)"
)


@register_task(group="mid", table="jarvis_intraday_prediction")
def sync_intraday_prediction(ctx: SyncContext) -> TaskResult:
    """4h 预测：bar_ts（epoch 毫秒）游标 + 重推未回填行（outcome_ret IS NULL）。

    任务描述为"重推 hit IS NULL 行"；实现取 outcome_ret IS NULL——源侧回填以
    outcome_ret 为完成标志，震荡方向的行回填后 hit 合法地保持 NULL，若按 hit
    判断会导致这些行被永久重推。
    """
    table = "jarvis_intraday_prediction"
    mysql_conn = _mysql_or_skip(ctx, table)
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    cur_raw = ctx.cursors.get(table)
    cursor = int(float(cur_raw)) if cur_raw else 0
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])

    try:
        with _src_conn() as src:
            rows = src.execute(_SQL_PRED_SRC, (cursor, batch)).fetchall()
    except Exception as e:  # noqa: BLE001
        tol = _tolerate_missing(table, e)
        if tol is not None:
            return tol
        raise
    _missing_warned.discard(table)
    if not rows:
        return TaskResult(rows=0, cursor_value=cur_raw)

    payload = []
    max_bar_ms = cursor
    for r in rows:
        bar_ms = int(r["bar_ts"])
        max_bar_ms = max(max_bar_ms, bar_ms)
        payload.append((
            r["id"], r["symbol"], _dt8(bar_ms / 1000.0), bar_ms, r["direction"],
            r["prob"], r["tradeable"], r["entry"], r["stop"], r["take"],
            r["atr_pct"], r["oos_hit_rate"], r["p_value"], r["reason"],
            r["why_text"] if "why_text" in r.keys() else None,
            r["outcome_ret"], r["hit"],
        ))
    _upsert_many(mysql_conn, _SQL_PRED_DST, payload, exec_batch)
    ctx.cursors.set(table, str(max_bar_ms))
    return TaskResult(rows=len(payload), cursor_value=str(max_bar_ms),
                      lag_seconds=max(0.0, time.time() - max_bar_ms / 1000.0))


_SQL_SNAP_SRC = (
    "SELECT id, symbol, generated_at_utc, as_of_date, price, conviction_score, "
    "direction, position_pct, dd_pct, fng, above_ma200, dd30_active, stop_loss, "
    "take_profit, decision_json, created_ts "
    "FROM snapshots WHERE created_ts > ? ORDER BY created_ts LIMIT ?"
)

_SQL_SNAP_DST = (
    "INSERT INTO jarvis_snapshot "
    "(id, symbol, as_of_date, generated_at_utc, price, conviction_score, direction, "
    " position_pct, dd_pct, fng, above_ma200, dd30_active, stop_loss, take_profit, "
    " decision_json, src_created_ts) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "as_of_date=VALUES(as_of_date), generated_at_utc=VALUES(generated_at_utc), "
    "price=VALUES(price), conviction_score=VALUES(conviction_score), "
    "direction=VALUES(direction), position_pct=VALUES(position_pct), dd_pct=VALUES(dd_pct), "
    "fng=VALUES(fng), above_ma200=VALUES(above_ma200), dd30_active=VALUES(dd30_active), "
    "stop_loss=VALUES(stop_loss), take_profit=VALUES(take_profit), "
    "decision_json=VALUES(decision_json), src_created_ts=VALUES(src_created_ts)"
)

_SNAP_REFRESH_WINDOW_S = 7 * 86400  # 同日 DO UPDATE 无独立更新时间列，短窗重刷兜底


@register_task(group="mid", table="jarvis_snapshot")
def sync_snapshot(ctx: SyncContext) -> TaskResult:
    """每日决策快照：created_ts 游标 + 近 7 天窗口重刷。"""
    table = "jarvis_snapshot"
    mysql_conn = _mysql_or_skip(ctx, table)
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    cur_raw = ctx.cursors.get(table)
    cursor = float(cur_raw) if cur_raw else 0.0
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])

    since = max(0.0, cursor - _SNAP_REFRESH_WINDOW_S)
    try:
        with _src_conn() as src:
            rows = src.execute(_SQL_SNAP_SRC, (since, batch)).fetchall()
    except Exception as e:  # noqa: BLE001
        tol = _tolerate_missing(table, e)
        if tol is not None:
            return tol
        raise
    _missing_warned.discard(table)
    if not rows:
        return TaskResult(rows=0, cursor_value=cur_raw)

    payload = []
    max_ts = cursor
    for r in rows:
        max_ts = max(max_ts, float(r["created_ts"] or 0.0))
        payload.append((
            r["id"], r["symbol"], r["as_of_date"] or None, r["generated_at_utc"],
            r["price"], r["conviction_score"], r["direction"], r["position_pct"],
            r["dd_pct"], r["fng"], r["above_ma200"], r["dd30_active"],
            r["stop_loss"], r["take_profit"], r["decision_json"] or None,
            r["created_ts"],
        ))
    _upsert_many(mysql_conn, _SQL_SNAP_DST, payload, exec_batch)
    ctx.cursors.set(table, str(max_ts))
    return TaskResult(rows=len(payload), cursor_value=str(max_ts),
                      lag_seconds=max(0.0, time.time() - max_ts))


_SQL_OUT_SRC = (
    "SELECT snapshot_id, horizon, fwd_date, fwd_price, fwd_ret_pct, correct, evaluated_ts "
    "FROM outcomes WHERE evaluated_ts > ? OR evaluated_ts IS NULL "
    "ORDER BY evaluated_ts LIMIT ?"
)

_SQL_OUT_DST = (
    "INSERT INTO jarvis_outcome "
    "(snapshot_id, horizon, fwd_date, fwd_price, fwd_ret_pct, correct, src_evaluated_ts) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "fwd_date=VALUES(fwd_date), fwd_price=VALUES(fwd_price), "
    "fwd_ret_pct=VALUES(fwd_ret_pct), correct=VALUES(correct), "
    "src_evaluated_ts=VALUES(src_evaluated_ts)"
)


@register_task(group="mid", table="jarvis_outcome")
def sync_outcome(ctx: SyncContext) -> TaskResult:
    """前向收益：evaluated_ts 游标 + 60s 重叠（重评刷新 evaluated_ts 天然覆盖更新）。"""
    table = "jarvis_outcome"
    mysql_conn = _mysql_or_skip(ctx, table)
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    cur_raw = ctx.cursors.get(table)
    cursor = float(cur_raw) if cur_raw else 0.0
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])

    try:
        with _src_conn() as src:
            rows = src.execute(_SQL_OUT_SRC, (max(0.0, cursor - 60.0), batch)).fetchall()
    except Exception as e:  # noqa: BLE001
        tol = _tolerate_missing(table, e)
        if tol is not None:
            return tol
        raise
    _missing_warned.discard(table)
    if not rows:
        return TaskResult(rows=0, cursor_value=cur_raw)

    payload = []
    max_ts = cursor
    for r in rows:
        ev = r["evaluated_ts"]
        if ev is not None:
            max_ts = max(max_ts, float(ev))
        payload.append((
            r["snapshot_id"], r["horizon"], r["fwd_date"] or None, r["fwd_price"],
            r["fwd_ret_pct"], r["correct"], ev,
        ))
    _upsert_many(mysql_conn, _SQL_OUT_DST, payload, exec_batch)
    ctx.cursors.set(table, str(max_ts))
    return TaskResult(rows=len(payload), cursor_value=str(max_ts),
                      lag_seconds=max(0.0, time.time() - max_ts) if max_ts > 0 else None)


_SQL_POS_SRC = (
    "SELECT id, symbol, status, side, qty, signal_tf, entry_date, entry_price, "
    "stop_loss, take_profit, time_stop_days, conviction_score, exit_date, exit_price, "
    "exit_reason, realized_pnl_usdt, realized_pnl_pct, opened_ts, closed_ts "
    "FROM paper_positions "
    "WHERE opened_ts > ? OR closed_ts > ? OR status = 'open' ORDER BY id LIMIT ?"
)

_SQL_POS_DST = (
    "INSERT INTO jarvis_position "
    "(id, symbol, status, side, qty, signal_tf, entry_date, entry_price, stop_loss, "
    " take_profit, time_stop_days, conviction_score, exit_date, exit_price, exit_reason, "
    " realized_pnl_usdt, realized_pnl_pct, opened_at, closed_at, src_opened_ts, src_closed_ts) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "status=VALUES(status), side=VALUES(side), qty=VALUES(qty), signal_tf=VALUES(signal_tf), "
    "entry_date=VALUES(entry_date), entry_price=VALUES(entry_price), "
    "stop_loss=VALUES(stop_loss), take_profit=VALUES(take_profit), "
    "time_stop_days=VALUES(time_stop_days), conviction_score=VALUES(conviction_score), "
    "exit_date=VALUES(exit_date), exit_price=VALUES(exit_price), exit_reason=VALUES(exit_reason), "
    "realized_pnl_usdt=VALUES(realized_pnl_usdt), realized_pnl_pct=VALUES(realized_pnl_pct), "
    "opened_at=VALUES(opened_at), closed_at=VALUES(closed_at), "
    "src_opened_ts=VALUES(src_opened_ts), src_closed_ts=VALUES(src_closed_ts)"
)


@register_task(group="mid", table="jarvis_position")
def sync_position(ctx: SyncContext) -> TaskResult:
    """模拟仓：双游标（opened_ts/closed_ts，各 60s 重叠）+ 全量重推 open 态。"""
    table = "jarvis_position"
    mysql_conn = _mysql_or_skip(ctx, table)
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    raw = ctx.cursors.get(table)
    try:
        cur = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        cur = {}
    c_open = float(cur.get("o") or 0.0)
    c_close = float(cur.get("c") or 0.0)
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])

    try:
        with _src_conn() as src:
            rows = src.execute(
                _SQL_POS_SRC,
                (max(0.0, c_open - 60.0), max(0.0, c_close - 60.0), batch),
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        tol = _tolerate_missing(table, e)
        if tol is not None:
            return tol
        raise
    _missing_warned.discard(table)
    if not rows:
        return TaskResult(rows=0, cursor_value=raw)

    payload = []
    for r in rows:
        if r["opened_ts"] is not None:
            c_open = max(c_open, float(r["opened_ts"]))
        if r["closed_ts"] is not None:
            c_close = max(c_close, float(r["closed_ts"]))
        payload.append((
            r["id"], r["symbol"], r["status"], r["side"], r["qty"], r["signal_tf"],
            r["entry_date"] or None, r["entry_price"], r["stop_loss"], r["take_profit"],
            r["time_stop_days"], r["conviction_score"], r["exit_date"] or None,
            r["exit_price"], r["exit_reason"], r["realized_pnl_usdt"],
            r["realized_pnl_pct"], _dt8(r["opened_ts"]), _dt8(r["closed_ts"]),
            r["opened_ts"], r["closed_ts"],
        ))
    _upsert_many(mysql_conn, _SQL_POS_DST, payload, exec_batch)
    new_raw = json.dumps({"o": c_open, "c": c_close})
    ctx.cursors.set(table, new_raw)
    biz = max(c_open, c_close)
    return TaskResult(rows=len(payload), cursor_value=new_raw,
                      lag_seconds=max(0.0, time.time() - biz) if biz > 0 else None)


_SQL_LMT_SRC = (
    "SELECT id, symbol, side, limit_price, qty, notional_usdt, status, stop_loss, "
    "take_profit, time_stop_days, created_date, created_ts, filled_price, filled_ts, "
    "cancel_ts, position_id, note "
    "FROM limit_orders WHERE created_ts > ? OR status = 'pending' ORDER BY id LIMIT ?"
)

_SQL_LMT_DST = (
    "INSERT INTO jarvis_limit_order "
    "(id, symbol, side, limit_price, qty, notional_usdt, status, stop_loss, take_profit, "
    " time_stop_days, created_date, filled_price, filled_at, cancelled_at, position_id, "
    " note, src_created_ts) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "status=VALUES(status), stop_loss=VALUES(stop_loss), take_profit=VALUES(take_profit), "
    "time_stop_days=VALUES(time_stop_days), filled_price=VALUES(filled_price), "
    "filled_at=VALUES(filled_at), cancelled_at=VALUES(cancelled_at), "
    "position_id=VALUES(position_id), note=VALUES(note), src_created_ts=VALUES(src_created_ts)"
)


@register_task(group="mid", table="jarvis_limit_order")
def sync_limit_order(ctx: SyncContext) -> TaskResult:
    """限价挂单：created_ts 游标（60s 重叠）+ 全量重推 pending 态。

    源列 filled_ts/cancel_ts（epoch 秒）→ 镜像 filled_at/cancelled_at（东八区）。
    """
    table = "jarvis_limit_order"
    mysql_conn = _mysql_or_skip(ctx, table)
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    cur_raw = ctx.cursors.get(table)
    cursor = float(cur_raw) if cur_raw else 0.0
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])

    try:
        with _src_conn() as src:
            rows = src.execute(_SQL_LMT_SRC, (max(0.0, cursor - 60.0), batch)).fetchall()
    except Exception as e:  # noqa: BLE001
        tol = _tolerate_missing(table, e)
        if tol is not None:
            return tol
        raise
    _missing_warned.discard(table)
    if not rows:
        return TaskResult(rows=0, cursor_value=cur_raw)

    payload = []
    max_ts = cursor
    for r in rows:
        if r["created_ts"] is not None:
            max_ts = max(max_ts, float(r["created_ts"]))
        payload.append((
            r["id"], r["symbol"], r["side"], r["limit_price"], r["qty"],
            r["notional_usdt"], r["status"], r["stop_loss"], r["take_profit"],
            r["time_stop_days"], r["created_date"] or None, r["filled_price"],
            _dt8(r["filled_ts"]), _dt8(r["cancel_ts"]), r["position_id"], r["note"],
            r["created_ts"],
        ))
    _upsert_many(mysql_conn, _SQL_LMT_DST, payload, exec_batch)
    ctx.cursors.set(table, str(max_ts))
    return TaskResult(rows=len(payload), cursor_value=str(max_ts),
                      lag_seconds=max(0.0, time.time() - max_ts) if max_ts > 0 else None)


_SQL_FORCE_IDS = (
    "SELECT id, symbol, trade_time "
    "FROM force_orders WHERE id > ? AND trade_time < ? ORDER BY id LIMIT ?"
)

_SQL_FORCE_BUCKET = (
    "SELECT COUNT(*) AS cnt, "
    "SUM(CASE WHEN UPPER(side)='BUY' THEN 1 ELSE 0 END) AS buy_cnt, "
    "SUM(CASE WHEN UPPER(side)='SELL' THEN 1 ELSE 0 END) AS sell_cnt, "
    "SUM(qty) AS qty_sum, SUM(notional) AS notional_sum, MAX(notional) AS notional_max "
    "FROM force_orders WHERE symbol = ? AND trade_time >= ? AND trade_time < ?"
)

_SQL_FORCE_DST = (
    "INSERT INTO jarvis_force_order_min "
    "(symbol, minute_ts, order_cnt, buy_cnt, sell_cnt, qty_sum, notional_sum, notional_max) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "order_cnt=VALUES(order_cnt), buy_cnt=VALUES(buy_cnt), sell_cnt=VALUES(sell_cnt), "
    "qty_sum=VALUES(qty_sum), notional_sum=VALUES(notional_sum), "
    "notional_max=VALUES(notional_max)"
)


@register_task(group="mid", table="jarvis_force_order_min")
def sync_force_order_min(ctx: SyncContext) -> TaskResult:
    """强平分钟聚合：id 游标找出受影响分钟桶 → 逐桶**全量重算**覆盖（方案 §2.2 G7）。

    幂等关键：不用增量行做部分聚合（同分钟行跨批到达会互相覆盖丢数），而是对
    增量涉及的每个 (symbol, minute) 桶回源重算全桶（(symbol, trade_time) 索引命中），
    重复计算结果一致，覆盖天然幂等；只处理已完整过去的分钟（trade_time < 当前整分）。
    """
    table = "jarvis_force_order_min"
    mysql_conn = _mysql_or_skip(ctx, table)
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    src = _force_conn()
    if src is None:
        if table not in _missing_warned:
            log.warning("[%s] force_orders.db 不存在（WS 流未开启过），跳过", table)
            _missing_warned.add(table)
        return TaskResult(rows=0)
    _missing_warned.discard(table)

    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])
    state = {"cursor": int(ctx.cursors.get(table) or 0)}
    minute_floor_ms = int(time.time() * 1000) // 60000 * 60000

    buckets_written = 0
    max_minute_s: Optional[float] = None
    try:
        while True:
            rows = src.execute(
                _SQL_FORCE_IDS, (state["cursor"], minute_floor_ms, batch)
            ).fetchall()
            if not rows:
                break
            affected = sorted({
                (str(r["symbol"]), int(r["trade_time"]) // 60000) for r in rows
            }, key=lambda kv: (kv[1], kv[0]))

            payload = []
            for sym, minute in affected:
                lo_ms, hi_ms = minute * 60000, (minute + 1) * 60000
                b = src.execute(_SQL_FORCE_BUCKET, (sym, lo_ms, hi_ms)).fetchone()
                if not b or not b["cnt"]:
                    continue
                payload.append((
                    sym, _dt8(minute * 60.0), int(b["cnt"]), int(b["buy_cnt"] or 0),
                    int(b["sell_cnt"] or 0), round(float(b["qty_sum"] or 0.0), 10),
                    round(float(b["notional_sum"] or 0.0), 4),
                    round(float(b["notional_max"] or 0.0), 4),
                ))
            if payload:
                _upsert_many(mysql_conn, _SQL_FORCE_DST, payload, exec_batch)
                buckets_written += len(payload)
                max_minute_s = max(m * 60.0 for (_s, m) in affected)
            state["cursor"] = int(rows[-1]["id"])
            ctx.cursors.set(table, str(state["cursor"]))
            if len(rows) < batch:
                break
            time.sleep(0.05)
    finally:
        src.close()

    return TaskResult(
        rows=buckets_written, cursor_value=str(state["cursor"]),
        lag_seconds=max(0.0, time.time() - max_minute_s) if max_minute_s else None,
    )
