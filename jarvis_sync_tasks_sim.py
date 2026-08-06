#!/usr/bin/env python3
"""贾维斯 → RuoYi 同步器 · 模拟交易器通道（子任务2：镜像同步 + 配置回读）。

模块名 jarvis_sync_tasks_sim 已登记进 jarvis_sync.load_task_modules()，
框架启动时自动导入；文件缺失时对应任务空缺、其余分组不受影响。

五个任务（全部挂 mid 组，默认 60s）：
  jarvis_sim_wallet     ← twelve_sim_wallet     （小表分页全量 upsert，PK=源 id 覆盖；
                          upsert 后 delete-absent：源端不存在的 id 从镜像删除，
                          台账重置后不留残留）
  jarvis_sim_position   ← twelve_sim_position   （分页全量 upsert + 全状态
                          delete-absent 镜像照源；源本身保留 closed 历史行与
                          canceled 失效留痕行（R3：pending 失效改为 status='canceled'
                          + cancel_reason/canceled_ts，7 天后源端清理，镜像随
                          delete-absent 同步消失）；台账重置残留按「源端不存在即删」清理）
  jarvis_sim_trade      ← twelve_sim_trade      （id 单调游标增量，覆盖式 upsert；
                          残留检测：源 id 回退 / 镜像行数或 max(id) 超过源
                          ⇒ 台账重置 → 旧轮次行先归档 jarvis_sim_trade_hist
                          （reset_epoch 递增）→ 镜像整表清理 + 游标归零重灌）
  jarvis_sim_signal_log ← twelve_sim_signal_log （id 单调游标增量，覆盖式 upsert；
                          持仓点位跟随变更日志，关联 position_id；残留检测同 trade）
  jarvis_sim_config     ← MySQL jarvis_sim_config **反向回读** → 本地 twelve_sim_config

源表列名依据（实读 jarvis_twelve_trader.py init_db()，勿凭契约草案猜列）：
  twelve_sim_wallet   : id symbol tf system name_cn principal balance equity
                        total_trades win_trades total_pnl win_rate profit_factor
                        max_drawdown_pct updated_ts(epoch 秒)
  twelve_sim_signal_log: id ts(epoch 秒) symbol tf system name_cn position_id
                        prev_entry prev_sl prev_tp new_entry new_sl new_tp
                        price change_kinds applied(INTEGER) note
  twelve_sim_position : id symbol tf system direction entry_price entry_ts(epoch)
                        qty margin leverage position_pct stop_loss take_profit
                        cur_price unrealized_pnl status cancel_reason
                        canceled_ts(epoch) —— 无 unrealized_pnl_pct 列，
                        镜像侧按源同口径推导（close 口径 pnl/margin*100，见 trader L542）
  twelve_sim_trade    : id symbol tf system name_cn direction entry_price entry_ts
                        exit_price exit_ts qty margin leverage stop_loss take_profit
                        exit_reason pnl pnl_pct rr balance_after holding_minutes(REAL)
                        —— 无独立 ts 列，镜像 src_ts 取 exit_ts（行的业务时间）
  twelve_sim_config   : id symbol scope_tf scope_system principal leverage
                        position_pct stop_loss_pct take_profit_pct enabled(INTEGER)
                        UNIQUE(symbol, scope_tf, scope_system) —— 无 remark/时间列，
                        MySQL 侧 remark/create_time/update_time 留在 RuoYi 不回读

纪律（对齐 tasks_a/tasks_b + 本子任务约定）：
  - twelve_sim_* 三张源表只读、显式列名 SELECT；**全链路唯一豁免的反向写**是
    sync_sim_config_pull 对本地 twelve_sim_config 的懒建 + upsert（仅限该表）。
  - 源表暂缺容忍：twelve_sim_*（trader 懒建）未建时记 warn 返回 0 行不算失败。
  - 游标从不后退：sim_trade 的 id 游标在批量写 MySQL commit 成功后才推进落盘。
  - MySQL 不可达：镜像任务报 backoff error（框架口径）；config 回读**静默保留旧配置**
    不报错（配置链路宁可陈旧不可断供）。
  - 时区口径：MySQL 侧 DATETIME(3) 统一存东八区（GMT+8）挂钟时间。
  - 配置回读 upsert 键用业务键 (symbol, scope_tf, scope_system)（NULL 归一比较），
    不用 id——本地 id 是 AUTOINCREMENT，trader 侧 set_config 也会自增插行，
    直插远端 id 会与本地序列/唯一键互踩。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from jarvis_sync import SyncContext, TaskResult, register_task

log = logging.getLogger("jarvis_sync.tasks_sim")

TZ8 = ZoneInfo("Asia/Shanghai")
SRC_DB = os.path.expanduser("~/.vibe-trading/jarvis_journal.db")

WALLET_TABLE = "jarvis_sim_wallet"
POSITION_TABLE = "jarvis_sim_position"
TRADE_TABLE = "jarvis_sim_trade"
TRADE_HIST_TABLE = "jarvis_sim_trade_hist"
SIGNAL_LOG_TABLE = "jarvis_sim_signal_log"
CONFIG_TABLE = "jarvis_sim_config"

LOCAL_CONFIG_TABLE = "twelve_sim_config"

# 源表/远端表暂缺 warn 节流（每表只提示一次，恢复后自动复位）
_missing_warned: set[str] = set()


# ══════════════════════════════════════════════════════ 公共 helper


def _local_db():
    """贾维斯本地库连接（jarvis_db 兼容层，自动跟随 pg/SQLite）。

    twelve_sim_wallet/position/trade 只读；twelve_sim_config 是全链路唯一
    豁免的反向写目标（懒建 + upsert，见模块 docstring）。
    """
    import jarvis_db

    return jarvis_db.connect(SRC_DB)


def _dt8(epoch_s) -> Optional[str]:
    """epoch 秒 → 东八区 'YYYY-MM-DD HH:MM:SS.mmm'（DATETIME(3) 直插，同 tasks_a）。"""
    if epoch_s is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(epoch_s), TZ8)
        return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _is_missing_table(err: Exception) -> bool:
    """源表尚未建出（twelve_sim_* 懒建 / 全新库）的跨后端判定。"""
    msg = str(err).lower()
    return "no such table" in msg or "does not exist" in msg or "doesn't exist" in msg


def _tolerate_missing(table: str, err: Exception) -> Optional[TaskResult]:
    if _is_missing_table(err):
        if table not in _missing_warned:
            log.warning("[%s] 源表尚未创建（%s），等待模拟交易器首次运行", table, err)
            _missing_warned.add(table)
        return TaskResult(rows=0)
    return None


def _upsert_many(mysql_conn, sql: str, rows: list[tuple], exec_batch: int) -> None:
    """分批 executemany + 单次 commit；异常 rollback 后向上抛（与 tasks_a 同纪律）。"""
    try:
        with mysql_conn.cursor() as cur:
            for i in range(0, len(rows), exec_batch):
                cur.executemany(sql, rows[i : i + exec_batch])
        mysql_conn.commit()
    except Exception:
        try:
            mysql_conn.rollback()
        except Exception:  # noqa: BLE001 — 连接已断时 rollback 可失败，不掩盖原异常
            pass
        raise


def _delete_absent(mysql_conn, table: str, seen_ids: list[int]) -> None:
    """delete-absent 清理：镜像侧存在但源端已消失的 id 删除（对齐 pending 清理纪律）。

    seen_ids 为空表示源表为空 → 镜像同样清空（台账重置场景）。
    """
    try:
        with mysql_conn.cursor() as cur:
            if seen_ids:
                ph = ",".join(["%s"] * len(seen_ids))
                cur.execute(
                    f"DELETE FROM {table} WHERE id NOT IN ({ph})", seen_ids)
            else:
                cur.execute(f"DELETE FROM {table}")
        mysql_conn.commit()
    except Exception:
        try:
            mysql_conn.rollback()
        except Exception:  # noqa: BLE001 — 连接已断时 rollback 可失败，不掩盖原异常
            pass
        raise


# hist 归档列（去掉 create_time；trade_id 对应镜像列 id，reset_epoch/archived_at 归档侧生成）
_TRADE_ARCHIVE_COLS = (
    "symbol, tf, system_code, name_cn, direction, entry_price, entry_time, "
    "exit_price, exit_time, qty, margin, leverage, stop_loss, take_profit, "
    "exit_reason, pnl, pnl_pct, rr, balance_after, holding_minutes, src_ts"
)


def _archive_stale_trades(cur, src_table: str) -> int:
    """台账重置清理前，把"源端已不存在"的镜像成交行归档进 jarvis_sim_trade_hist。

    判定口径：镜像行的 (id, src_ts) 在源端找不到匹配 ⇒ 属旧轮次残留，归档；
    仍在源端的行属新轮次（如序列续走时的边界行），重灌后继续留在当前表，
    不入档，避免"含历史"查询时同一笔被计两次。

    仅生成 SQL 不 commit——与调用方 _detect_source_reset 的 DELETE 同一事务，
    归档失败一起回滚（宁可暂留幽灵数据等下轮重试，不可丢历史）。
    返回归档行数。
    """
    with _local_db() as src:
        src_rows = src.execute(
            f"SELECT id, exit_ts FROM {src_table}").fetchall()
    src_map = {int(r["id"]): r["exit_ts"] for r in src_rows}

    cur.execute(f"SELECT id, src_ts FROM {TRADE_TABLE}")
    stale_ids = []
    for mid, sts in cur.fetchall():
        sv = src_map.get(int(mid))
        same = (
            sv is not None and sts is not None
            and abs(float(sts) - float(sv)) < 0.0005
        ) or (sv is None and sts is None and int(mid) in src_map)
        if not same:
            stale_ids.append(int(mid))
    if not stale_ids:
        return 0

    cur.execute(
        f"SELECT COALESCE(MAX(reset_epoch), 0) + 1 FROM {TRADE_HIST_TABLE}")
    epoch = int(cur.fetchone()[0])
    ph = ",".join(["%s"] * len(stale_ids))
    cur.execute(
        f"INSERT INTO {TRADE_HIST_TABLE} "
        f"(reset_epoch, trade_id, {_TRADE_ARCHIVE_COLS}) "
        f"SELECT %s, id, {_TRADE_ARCHIVE_COLS} "
        f"FROM {TRADE_TABLE} WHERE id IN ({ph})",
        [epoch] + stale_ids,
    )
    log.warning(
        "[%s] 已归档 %s 行旧轮次成交到 %s（reset_epoch=%s）",
        TRADE_TABLE, len(stale_ids), TRADE_HIST_TABLE, epoch)
    return len(stale_ids)


def _detect_source_reset(mysql_conn, ctx: SyncContext, table: str,
                         src_table: str, cursor: int,
                         archive_trades: bool = False) -> int:
    """台账重置/残留检测（追加型游标任务专用）。以下任一成立即判定镜像含
    重置前残留，需整表清理 + 游标归零重灌：

      1. 源 max(id) < 已落盘游标 —— 源被清空且 id 序列回退；
      2. 镜像行数 > 源行数       —— 源被清空但序列未回退（新 id 续走旧号段），
                                    旧镜像行永不会被增量游标触达；
      3. 镜像 max(id) > 源 max(id) —— 同上的另一种表现。

    正常运行时镜像恒为源的子集（追加型、只从源插入），三条件都不会误触发。
    archive_trades=True（仅成交表）时，清理前先把旧轮次行归档进
    jarvis_sim_trade_hist（归档 + 清理同一事务，失败一起回滚）。
    返回校正后的游标值。源表暂缺的异常原样上抛，由调用方统一容忍。
    """
    with _local_db() as src:
        row = src.execute(
            f"SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS mx "
            f"FROM {src_table}").fetchone()
    src_n, src_max = int(row["n"] or 0), int(row["mx"] or 0)
    with mysql_conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*), COALESCE(MAX(id), 0) FROM {table}")
        dst_n, dst_max = (int(v or 0) for v in cur.fetchone())
    if src_max >= cursor and dst_n <= src_n and dst_max <= src_max:
        return cursor
    log.warning(
        "[%s] 检测到台账重置残留（源 n=%s max=%s / 镜像 n=%s max=%s / 游标 %s），"
        "清理镜像并重灌", table, src_n, src_max, dst_n, dst_max, cursor)
    try:
        with mysql_conn.cursor() as cur:
            if archive_trades:
                _archive_stale_trades(cur, src_table)
            cur.execute(f"DELETE FROM {table}")
        mysql_conn.commit()
    except Exception:
        try:
            mysql_conn.rollback()
        except Exception:  # noqa: BLE001 — 连接已断时 rollback 可失败，不掩盖原异常
            pass
        raise
    ctx.cursors.set(table, "0")
    return 0


def _paged_full_scan(select_sql: str, batch: int):
    """小表分页全量扫（id 升序翻页）；yield 每页行列表。

    select_sql 须含 "WHERE id > ? ... ORDER BY id LIMIT ?" 两个占位。
    源表暂缺时抛出原异常，由调用方 _tolerate_missing 统一容忍。
    """
    last_id = 0
    while True:
        with _local_db() as src:
            rows = src.execute(select_sql, (last_id, batch)).fetchall()
        if not rows:
            return
        yield rows
        last_id = int(rows[-1]["id"] or 0)
        if len(rows) < batch:
            return
        time.sleep(0.05)


# ══════════════════════════════════════════════════════ 钱包（小表全量 upsert）


_SQL_WALLET_SRC = (
    "SELECT id, symbol, tf, system, name_cn, principal, balance, equity, "
    "total_trades, win_trades, total_pnl, win_rate, profit_factor, "
    "max_drawdown_pct, updated_ts "
    "FROM twelve_sim_wallet WHERE id > ? ORDER BY id LIMIT ?"
)

_SQL_WALLET_DST = (
    f"INSERT INTO {WALLET_TABLE} "
    "(id, symbol, tf, system_code, name_cn, principal, balance, equity, "
    " total_trades, win_trades, total_pnl, win_rate, profit_factor, "
    " max_drawdown_pct, src_updated_ts) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "symbol=VALUES(symbol), tf=VALUES(tf), system_code=VALUES(system_code), "
    "name_cn=VALUES(name_cn), principal=VALUES(principal), balance=VALUES(balance), "
    "equity=VALUES(equity), total_trades=VALUES(total_trades), "
    "win_trades=VALUES(win_trades), total_pnl=VALUES(total_pnl), "
    "win_rate=VALUES(win_rate), profit_factor=VALUES(profit_factor), "
    "max_drawdown_pct=VALUES(max_drawdown_pct), src_updated_ts=VALUES(src_updated_ts)"
)


@register_task(group="mid", table=WALLET_TABLE)
def sync_sim_wallet(ctx: SyncContext) -> TaskResult:
    """模拟钱包：每 symbol×tf×system 恒定一行的小表，分页全量 upsert（PK=源 id）。

    upsert 后做 delete-absent：源端不存在的 id 从镜像删除——台账重置后源表
    清空重建、id 序列回退，旧行若不清理会与新数据混存污染统计。
    """
    mysql_conn = ctx.mysql.get()
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])

    total = 0
    max_upd = 0.0
    seen_ids: list[int] = []
    try:
        for rows in _paged_full_scan(_SQL_WALLET_SRC, batch):
            payload = []
            for r in rows:
                upd = float(r["updated_ts"] or 0.0)
                max_upd = max(max_upd, upd)
                payload.append((
                    r["id"], r["symbol"], r["tf"], r["system"], r["name_cn"],
                    r["principal"], r["balance"], r["equity"], r["total_trades"],
                    r["win_trades"], r["total_pnl"], r["win_rate"],
                    r["profit_factor"], r["max_drawdown_pct"], r["updated_ts"],
                ))
            seen_ids.extend(int(r["id"]) for r in rows)
            _upsert_many(mysql_conn, _SQL_WALLET_DST, payload, exec_batch)
            total += len(payload)
    except Exception as e:  # noqa: BLE001 — 源表懒建容忍
        tol = _tolerate_missing(WALLET_TABLE, e)
        if tol is not None:
            return tol
        raise
    _missing_warned.discard(WALLET_TABLE)
    _delete_absent(mysql_conn, WALLET_TABLE, seen_ids)
    return TaskResult(
        rows=total,
        cursor_value=f"full@{total}rows",
        lag_seconds=max(0.0, time.time() - max_upd) if max_upd > 0 else None,
    )


# ══════════════════════════════════════════════════════ 持仓（全量镜像 status）


_SQL_POSITION_SRC = (
    "SELECT id, symbol, tf, system, direction, entry_price, entry_ts, qty, "
    "margin, leverage, position_pct, stop_loss, take_profit, cur_price, "
    "unrealized_pnl, status, cancel_reason, canceled_ts "
    "FROM twelve_sim_position WHERE id > ? ORDER BY id LIMIT ?"
)

_SQL_POSITION_DST = (
    f"INSERT INTO {POSITION_TABLE} "
    "(id, symbol, tf, system_code, direction, entry_price, entry_time, qty, "
    " margin, leverage, position_pct, stop_loss, take_profit, cur_price, "
    " unrealized_pnl, unrealized_pnl_pct, status, cancel_reason, cancel_time) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "symbol=VALUES(symbol), tf=VALUES(tf), system_code=VALUES(system_code), "
    "direction=VALUES(direction), entry_price=VALUES(entry_price), "
    "entry_time=VALUES(entry_time), qty=VALUES(qty), margin=VALUES(margin), "
    "leverage=VALUES(leverage), position_pct=VALUES(position_pct), "
    "stop_loss=VALUES(stop_loss), take_profit=VALUES(take_profit), "
    "cur_price=VALUES(cur_price), unrealized_pnl=VALUES(unrealized_pnl), "
    "unrealized_pnl_pct=VALUES(unrealized_pnl_pct), status=VALUES(status), "
    "cancel_reason=VALUES(cancel_reason), cancel_time=VALUES(cancel_time)"
)


def _upnl_pct(pnl, margin) -> Optional[float]:
    """未实现盈亏%（源无此列，按 trader 平仓同口径推导：pnl/margin*100）。"""
    try:
        m = float(margin)
        if m > 0 and pnl is not None:
            return round(float(pnl) / m * 100.0, 2)
    except (TypeError, ValueError):
        pass
    return None


@register_task(group="mid", table=POSITION_TABLE)
def sync_sim_position(ctx: SyncContext) -> TaskResult:
    """模拟持仓/计划：分页全量 upsert + 全状态 delete-absent（镜像照源）。

    源端语义（实读 trader 代码，R3 后）：closed 行只 UPDATE 不删除；pending
    失效改为 status='canceled' 留痕行（带 cancel_reason/canceled_ts，源端保留
    7 天后清理）——源表保留 open/closed/canceled 完整现场。镜像直接对齐源：
    源端不存在的 id 一律删除，天然覆盖 a) canceled 留痕到期清理、
    b) 台账重置残留（旧行冒充在场持仓）两类脏数据。
    """
    mysql_conn = ctx.mysql.get()
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])

    total = 0
    seen_ids: list[int] = []
    try:
        for rows in _paged_full_scan(_SQL_POSITION_SRC, batch):
            payload = [(
                r["id"], r["symbol"], r["tf"], r["system"], r["direction"],
                r["entry_price"], _dt8(r["entry_ts"]), r["qty"], r["margin"],
                r["leverage"], r["position_pct"], r["stop_loss"], r["take_profit"],
                r["cur_price"], r["unrealized_pnl"],
                _upnl_pct(r["unrealized_pnl"], r["margin"]), r["status"],
                r["cancel_reason"], _dt8(r["canceled_ts"]),
            ) for r in rows]
            seen_ids.extend(int(r["id"]) for r in rows)
            _upsert_many(mysql_conn, _SQL_POSITION_DST, payload, exec_batch)
            total += len(payload)
    except Exception as e:  # noqa: BLE001 — 源表懒建容忍
        tol = _tolerate_missing(POSITION_TABLE, e)
        if tol is not None:
            return tol
        raise
    _missing_warned.discard(POSITION_TABLE)
    _delete_absent(mysql_conn, POSITION_TABLE, seen_ids)
    return TaskResult(rows=total, cursor_value=f"full@{total}rows")


# ══════════════════════════════════════════════════════ 成交流水（id 游标增量）


_SQL_TRADE_SRC = (
    "SELECT id, symbol, tf, system, name_cn, direction, entry_price, entry_ts, "
    "exit_price, exit_ts, qty, margin, leverage, stop_loss, take_profit, "
    "exit_reason, pnl, pnl_pct, rr, balance_after, holding_minutes "
    "FROM twelve_sim_trade WHERE id > ? ORDER BY id LIMIT ?"
)

_SQL_TRADE_DST = (
    f"INSERT INTO {TRADE_TABLE} "
    "(id, symbol, tf, system_code, name_cn, direction, entry_price, entry_time, "
    " exit_price, exit_time, qty, margin, leverage, stop_loss, take_profit, "
    " exit_reason, pnl, pnl_pct, rr, balance_after, holding_minutes, src_ts) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "symbol=VALUES(symbol), tf=VALUES(tf), system_code=VALUES(system_code), "
    "name_cn=VALUES(name_cn), direction=VALUES(direction), "
    "entry_price=VALUES(entry_price), entry_time=VALUES(entry_time), "
    "exit_price=VALUES(exit_price), exit_time=VALUES(exit_time), "
    "qty=VALUES(qty), margin=VALUES(margin), leverage=VALUES(leverage), "
    "stop_loss=VALUES(stop_loss), take_profit=VALUES(take_profit), "
    "exit_reason=VALUES(exit_reason), pnl=VALUES(pnl), pnl_pct=VALUES(pnl_pct), "
    "rr=VALUES(rr), balance_after=VALUES(balance_after), "
    "holding_minutes=VALUES(holding_minutes), src_ts=VALUES(src_ts)"
)


@register_task(group="mid", table=TRADE_TABLE)
def sync_sim_trade(ctx: SyncContext) -> TaskResult:
    """模拟成交流水：id 单调游标增量，追加型 no-op upsert（天然幂等）。

    源无独立 ts 列，src_ts 取 exit_ts（平仓台账行的业务时间）；
    holding_minutes 源为 REAL，四舍五入落 MySQL INT 列。
    先做台账重置检测：命中 ⇒ 旧轮次行归档 hist → 镜像整表清理 + 游标归零重灌。
    """
    mysql_conn = ctx.mysql.get()
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])
    state = {"cursor": int(ctx.cursors.get(TRADE_TABLE) or 0)}
    try:
        state["cursor"] = _detect_source_reset(
            mysql_conn, ctx, TRADE_TABLE, "twelve_sim_trade", state["cursor"],
            archive_trades=True)
    except Exception as e:  # noqa: BLE001 — 源表懒建容忍
        tol = _tolerate_missing(TRADE_TABLE, e)
        if tol is not None:
            return tol
        raise
    total = 0
    max_ts = 0.0

    while True:
        try:
            with _local_db() as src:
                rows = src.execute(_SQL_TRADE_SRC, (state["cursor"], batch)).fetchall()
        except Exception as e:  # noqa: BLE001 — 源表懒建容忍
            tol = _tolerate_missing(TRADE_TABLE, e)
            if tol is not None:
                return (
                    tol if total == 0
                    else TaskResult(rows=total, cursor_value=str(state["cursor"]))
                )
            raise
        _missing_warned.discard(TRADE_TABLE)
        if not rows:
            break
        payload = []
        for r in rows:
            exit_ts = r["exit_ts"]
            if exit_ts is not None:
                max_ts = max(max_ts, float(exit_ts))
            hold = r["holding_minutes"]
            payload.append((
                r["id"], r["symbol"], r["tf"], r["system"], r["name_cn"],
                r["direction"], r["entry_price"], _dt8(r["entry_ts"]),
                r["exit_price"], _dt8(exit_ts), r["qty"], r["margin"],
                r["leverage"], r["stop_loss"], r["take_profit"], r["exit_reason"],
                r["pnl"], r["pnl_pct"], r["rr"], r["balance_after"],
                int(round(float(hold))) if hold is not None else None, exit_ts,
            ))
        _upsert_many(mysql_conn, _SQL_TRADE_DST, payload, exec_batch)
        # 游标从不后退：写 MySQL commit 成功后才推进并落盘
        state["cursor"] = int(rows[-1]["id"])
        ctx.cursors.set(TRADE_TABLE, str(state["cursor"]))
        total += len(payload)
        if len(rows) < batch:
            break
        time.sleep(0.05)

    return TaskResult(
        rows=total, cursor_value=str(state["cursor"]),
        lag_seconds=max(0.0, time.time() - max_ts) if max_ts > 0 else None,
    )


# ══════════════════════════════════ 点位跟随变更日志（id 游标增量）


_SQL_SIGNAL_LOG_SRC = (
    "SELECT id, ts, symbol, tf, system, name_cn, position_id, "
    "prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp, "
    "price, change_kinds, applied, note "
    "FROM twelve_sim_signal_log WHERE id > ? ORDER BY id LIMIT ?"
)

_SQL_SIGNAL_LOG_DST = (
    f"INSERT INTO {SIGNAL_LOG_TABLE} "
    "(id, log_time, src_ts, symbol, tf, system_code, name_cn, position_id, "
    " prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp, "
    " price, change_kinds, applied, note) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "log_time=VALUES(log_time), src_ts=VALUES(src_ts), symbol=VALUES(symbol), "
    "tf=VALUES(tf), system_code=VALUES(system_code), name_cn=VALUES(name_cn), "
    "position_id=VALUES(position_id), prev_entry=VALUES(prev_entry), "
    "prev_sl=VALUES(prev_sl), prev_tp=VALUES(prev_tp), "
    "new_entry=VALUES(new_entry), new_sl=VALUES(new_sl), new_tp=VALUES(new_tp), "
    "price=VALUES(price), change_kinds=VALUES(change_kinds), "
    "applied=VALUES(applied), note=VALUES(note)"
)


@register_task(group="mid", table=SIGNAL_LOG_TABLE)
def sync_sim_signal_log(ctx: SyncContext) -> TaskResult:
    """持仓点位跟随变更日志：id 单调游标增量，追加型 no-op upsert（同 sim_trade 纪律，
    含台账重置检测——日志关联 position_id，重置后残留同样需要清理）。"""
    mysql_conn = ctx.mysql.get()
    if mysql_conn is None:
        return TaskResult(error="mysql unavailable (backoff)")
    batch = int(ctx.config["batch_size"])
    exec_batch = int(ctx.config["exec_batch"])
    state = {"cursor": int(ctx.cursors.get(SIGNAL_LOG_TABLE) or 0)}
    try:
        state["cursor"] = _detect_source_reset(
            mysql_conn, ctx, SIGNAL_LOG_TABLE, "twelve_sim_signal_log",
            state["cursor"])
    except Exception as e:  # noqa: BLE001 — 源表懒建容忍
        tol = _tolerate_missing(SIGNAL_LOG_TABLE, e)
        if tol is not None:
            return tol
        raise
    total = 0
    max_ts = 0.0

    while True:
        try:
            with _local_db() as src:
                rows = src.execute(
                    _SQL_SIGNAL_LOG_SRC, (state["cursor"], batch)).fetchall()
        except Exception as e:  # noqa: BLE001 — 源表懒建容忍
            tol = _tolerate_missing(SIGNAL_LOG_TABLE, e)
            if tol is not None:
                return (
                    tol if total == 0
                    else TaskResult(rows=total, cursor_value=str(state["cursor"]))
                )
            raise
        _missing_warned.discard(SIGNAL_LOG_TABLE)
        if not rows:
            break
        payload = []
        for r in rows:
            ts = r["ts"]
            if ts is not None:
                max_ts = max(max_ts, float(ts))
            payload.append((
                r["id"], _dt8(ts), ts, r["symbol"], r["tf"], r["system"],
                r["name_cn"], r["position_id"], r["prev_entry"], r["prev_sl"],
                r["prev_tp"], r["new_entry"], r["new_sl"], r["new_tp"],
                r["price"], r["change_kinds"],
                int(r["applied"]) if r["applied"] is not None else 1, r["note"],
            ))
        _upsert_many(mysql_conn, _SQL_SIGNAL_LOG_DST, payload, exec_batch)
        # 游标从不后退：写 MySQL commit 成功后才推进并落盘
        state["cursor"] = int(rows[-1]["id"])
        ctx.cursors.set(SIGNAL_LOG_TABLE, str(state["cursor"]))
        total += len(payload)
        if len(rows) < batch:
            break
        time.sleep(0.05)

    return TaskResult(
        rows=total, cursor_value=str(state["cursor"]),
        lag_seconds=max(0.0, time.time() - max_ts) if max_ts > 0 else None,
    )


# ══════════════════════════════════ 配置回读（MySQL → 本地，全链路唯一豁免）


_SQL_CONFIG_PULL = (
    "SELECT id, symbol, scope_tf, scope_system, principal, leverage, "
    "position_pct, stop_loss_pct, take_profit_pct, enabled "
    f"FROM {CONFIG_TABLE}"
)

# 本地懒建 DDL：逐字对齐 jarvis_twelve_trader.init_db()（trader 未跑过时先建出同款表；
# 经 jarvis_db 翻译层自动转 PG：AUTOINCREMENT→BIGSERIAL、REAL→double precision）
_SQL_CONFIG_LOCAL_DDL = f"""
CREATE TABLE IF NOT EXISTS {LOCAL_CONFIG_TABLE} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    scope_tf        TEXT,
    scope_system    TEXT,
    principal       REAL DEFAULT 100,
    leverage        REAL,
    position_pct    REAL,
    stop_loss_pct   REAL,
    take_profit_pct REAL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (symbol, scope_tf, scope_system)
)
"""

# 业务键匹配用 COALESCE 归一 NULL（SQLite/PG 双兼容，避免 IS NOT DISTINCT FROM 方言差异）
_SQL_CONFIG_LOCAL_UPDATE = (
    f"UPDATE {LOCAL_CONFIG_TABLE} SET principal=?, leverage=?, position_pct=?, "
    "stop_loss_pct=?, take_profit_pct=?, enabled=? "
    "WHERE symbol=? AND COALESCE(scope_tf,'')=COALESCE(?,'') "
    "AND COALESCE(scope_system,'')=COALESCE(?,'')"
)

_SQL_CONFIG_LOCAL_INSERT = (
    f"INSERT INTO {LOCAL_CONFIG_TABLE} "
    "(symbol, scope_tf, scope_system, principal, leverage, position_pct, "
    " stop_loss_pct, take_profit_pct, enabled) "
    "VALUES (?,?,?,?,?,?,?,?,?)"
)


def _num_or_none(v) -> Optional[float]:
    """MySQL DECIMAL（PyMySQL 返回 decimal.Decimal）→ 本地 REAL 容忍转换。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _enabled_int(v) -> int:
    """MySQL enabled CHAR(1) '0'/'1' → 本地 INTEGER 0/1（非法值按启用兜底）。"""
    try:
        return 1 if int(str(v).strip() or 1) else 0
    except (TypeError, ValueError):
        return 1


@register_task(group="mid", table=CONFIG_TABLE)
def sync_sim_config_pull(ctx: SyncContext) -> TaskResult:
    """反向配置回读：MySQL jarvis_sim_config 全量 → upsert 本地 twelve_sim_config。

    全链路唯一豁免的回读写（仅限该表）。MySQL 不可达 / 远端表未建时**静默**
    保留本地旧配置不报错（配置宁可陈旧不可断供）；本地表不存在则按 trader
    同款 DDL 懒建。upsert 键为业务键 (symbol, scope_tf, scope_system)，
    不回写 id（本地自增序列与 trader 自插行互不干扰）。
    """
    mysql_conn = ctx.mysql.get()
    if mysql_conn is None:
        # 静默保留旧配置：不算失败（区别于镜像任务的 backoff error 口径）
        return TaskResult(rows=0, cursor_value="mysql_down_keep_local")

    try:
        with mysql_conn.cursor() as cur:
            cur.execute(_SQL_CONFIG_PULL)
            remote_rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001 — 远端表未建（DDL 未执行）同样静默容忍
        if _is_missing_table(e):
            if CONFIG_TABLE not in _missing_warned:
                log.warning("[%s] MySQL 侧配置表尚未创建（%s），保留本地旧配置", CONFIG_TABLE, e)
                _missing_warned.add(CONFIG_TABLE)
            return TaskResult(rows=0, cursor_value="remote_table_missing")
        log.warning("[%s] 配置回读失败（%s），保留本地旧配置", CONFIG_TABLE, e)
        return TaskResult(rows=0, cursor_value="pull_failed_keep_local")
    _missing_warned.discard(CONFIG_TABLE)

    with _local_db() as db:
        db.execute(_SQL_CONFIG_LOCAL_DDL)
        upserted = 0
        for row in remote_rows:
            # 列序与 _SQL_CONFIG_PULL 一致：id 仅占位不回写
            (_rid, symbol, scope_tf, scope_system, principal, leverage,
             position_pct, stop_loss_pct, take_profit_pct, enabled) = row
            vals = (
                _num_or_none(principal), _num_or_none(leverage),
                _num_or_none(position_pct), _num_or_none(stop_loss_pct),
                _num_or_none(take_profit_pct), _enabled_int(enabled),
            )
            cur = db.execute(
                _SQL_CONFIG_LOCAL_UPDATE, vals + (symbol, scope_tf, scope_system)
            )
            if not getattr(cur, "rowcount", 0):
                db.execute(
                    _SQL_CONFIG_LOCAL_INSERT,
                    (symbol, scope_tf, scope_system) + vals,
                )
            upserted += 1

    return TaskResult(rows=upserted, cursor_value=f"pull@{upserted}rows")
