#!/usr/bin/env python3
"""贾维斯 → RuoYi 同步器 · 通道 B（HTTP API 快照，T4）。

模块名 jarvis_sync_tasks_b 与框架 load_task_modules() 的加载契约对齐
（jarvis_sync_tasks_a=通道 A / jarvis_sync_tasks_b=通道 B），由框架在
启动时自动导入，无需手工 import。

经 jarvis_sync 框架的 register_task 协议接入两张 API 拉取表：
  - jarvis_reco_plan       （api 组，默认 180s）：逐币拉 /api/twelve/consensus，
    trade_plan 序列化 sha256 截 16 字节为 plan_hash，内容变化才插新行；
    plan_status=neutral/watch 无计划时也插一行（side=NULL），界面可区分
    "无计划"与"没同步"（计划 §7 Q6）。该请求同时触发源侧 record_batch
    重算落库，保证通道 A fast 组持续有增量（计划 §3.2「一石二鸟」）。
  - jarvis_market_snapshot （market_snapshot 组，默认 300s）：/api/market-intel
    全局一次 + 逐币 /api/sentiment 组装时序快照，snap_time 分钟对齐。
    oi/多空仅 BTCUSDT、funding 仅 4 币有源，其余列按 NULL 容忍（计划 §3.3）。

通道 B 附加纪律（计划 §3.3/§3.4）：
  - HTTP 超时取 config.http_timeout_s（默认 10s）；dashboard 不可达记 warn
    下轮重试，连续 CONSEC_ERROR_THRESHOLD 轮才升级 error；
  - MySQL 不可达时本轮快照暂存内存（每表仅最近一轮），恢复后先补写再继续；
  - 判重幂等：reco_plan 靠 uk(symbol,plan_hash) INSERT IGNORE；market_snapshot
    靠 uk(symbol,snap_time) INSERT IGNORE——重放/补写天然无重复。
  - 源侧零写入：本模块只发 GET 请求，不连贾维斯 PG/SQLite。
  - 币种池收敛（2026-08-05）：market_snapshot 写库成功后追加 symbol 维度
    delete-absent 清扫（10 分钟节流）——镜像中不属于 watchlist（ctx.symbols）的
    历史快照行删除，监控总览 marketCards/trendSeries 的币种跟随收敛
    （与 tasks_a tape_bar/signal_change 同纪律；需 jarvis_sync 账号有 DELETE
    权限，见 sql/jarvis_mysql_watchlist_cleanup.sql）。

游标口径：reco_plan 无游标语义，本地 cursors.json 存「symbol→plan_hash」
JSON 映射（跨重启跳过未变化计划的 no-op INSERT）；心跳表 cursor_value 只放
≤64 字符的摘要（丢失后靠 uk 幂等自愈，不依赖恢复）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from jarvis_sync import SyncContext, TaskResult, register_task

log = logging.getLogger("jarvis_sync.api")

RECO_TABLE = "jarvis_reco_plan"
SNAP_TABLE = "jarvis_market_snapshot"

# dashboard 连续不可达多少轮才在心跳/日志升级为 error（计划 §3.3：10 轮）
CONSEC_ERROR_THRESHOLD = 10

TZ_GMT8 = timezone(timedelta(hours=8))

# 进程级状态：连续失败计数（按任务）与 MySQL 不可达时的暂存快照（每表最近一轮）
_consec_fail: dict[str, int] = {RECO_TABLE: 0, SNAP_TABLE: 0}
_pending: dict[str, list[tuple]] = {}
# reco_plan 内存判重：symbol → plan_hash（首轮从 cursors.json 播种）
_last_hash: dict[str, str] = {}
_last_hash_seeded = False


# ══════════════════════════════════════════════════════ 通用工具


def _now_gmt8() -> datetime:
    """东八区当前时间（DATETIME 列统一存 GMT+8 挂钟，计划 §2.1 修订④）。"""
    return datetime.now(TZ_GMT8)


def _dt3(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _http_json(base_url: str, path: str, timeout: float) -> Optional[dict]:
    """GET 并解析 JSON；任何网络/解析错误返回 None（调用方按轮计失败）。"""
    url = base_url.rstrip("/") + path
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                log.warning("HTTP %s %s", resp.status, url)
                return None
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as e:
        log.warning("HTTP 拉取失败 %s — %s", url, e)
        return None


def _round_fail(table: str, detail: str) -> TaskResult:
    """dashboard 整轮不可达：warn 重试，连续超阈值才升级 error（计划 §3.3）。"""
    _consec_fail[table] += 1
    n = _consec_fail[table]
    if n >= CONSEC_ERROR_THRESHOLD:
        return TaskResult(rows=0, error=f"dashboard 连续 {n} 轮不可达（{detail}）")
    log.warning("[%s] dashboard 不可达（连续第 %d/%d 轮，%s），下轮重试",
                table, n, CONSEC_ERROR_THRESHOLD, detail)
    return TaskResult(rows=0, cursor_value=f"http_retry_{n}")


def _num(v: Any) -> Optional[float]:
    """数值容忍转换：None/非数值/NaN → None（DECIMAL 列按 NULL 写入）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN 过滤


def _s(v: Any, limit: int) -> Optional[str]:
    """字符串容忍截断（对齐 varchar 列宽，防 strict mode 写入失败）。"""
    if v is None:
        return None
    text = str(v).strip()
    return text[:limit] if text else None


def _flush_rows(conn, table: str, sql: str, rows: list[tuple]) -> int:
    """执行 INSERT IGNORE 批量写；返回实际影响行数。失败上抛由框架记账。"""
    if not rows:
        return 0
    with conn.cursor() as cur:
        affected = cur.executemany(sql, rows)
    conn.commit()
    return int(affected or 0)


def _stash_pending(table: str, rows: list[tuple]) -> None:
    """MySQL 不可达：暂存本轮快照（只保最近一轮，计划 §3.4）。"""
    if rows:
        _pending[table] = rows
        log.warning("[%s] MySQL 不可达，本轮 %d 行暂存内存待补写", table, len(rows))


def _take_pending(table: str) -> list[tuple]:
    return _pending.pop(table, [])


# symbol 维度 delete-absent 清扫节流（与 tasks_a 同款纪律；模块自含独立计时）
_SWEEP_INTERVAL_S = 600.0
_last_sweep: dict[str, float] = {}


def _sweep_absent_symbols(mysql_conn, table: str, symbols: list[str]) -> int:
    """symbol 维度 delete-absent：清理镜像中不属于当前币种池的行。

    与 tasks_a._sweep_absent_symbols 同语义（jarvis-monitor-redesign §2.2-4 sim
    纪律推广）：watchlist 收敛后镜像 distinct symbol 跟随收敛。本表行本就按
    ctx.symbols 生产，残留只来自历史币种池变更。
    护栏：symbols 为空不动镜像；失败 rollback 后上抛（框架记 error 下轮重试）。
    Returns: 本轮删除行数（被节流跳过返回 0）。
    """
    if not symbols:
        return 0
    now = time.monotonic()
    last = _last_sweep.get(table)
    if last is not None and now - last < _SWEEP_INTERVAL_S:
        return 0
    ph = ",".join(["%s"] * len(symbols))
    try:
        with mysql_conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} WHERE symbol NOT IN ({ph})", list(symbols))
            deleted = int(cur.rowcount or 0)
        mysql_conn.commit()
    except Exception:
        try:
            mysql_conn.rollback()
        except Exception:  # noqa: BLE001 — 连接已断时 rollback 本身可失败，不掩盖原异常
            pass
        raise
    _last_sweep[table] = now
    if deleted:
        log.warning("[%s] delete-absent 清理 %d 行非币种池残留（收敛到 %s）",
                    table, deleted, ",".join(symbols))
    return deleted


# ══════════════════════════════════════════════════════ reco_plan（api 组 180s）

_RECO_INSERT = (
    f"INSERT IGNORE INTO {RECO_TABLE} "
    "(symbol, source_tf, side, entry_lo, entry_hi, stop_loss, take_profit_1, "
    "take_profit_2, rr, position_pct, plan_status, plan_reason, basis_json, "
    "price, direction, confidence, plan_hash, as_of) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


def _plan_hash(plan: Optional[dict], plan_state: str) -> str:
    """计划内容哈希（计划 §3.2：side/entry_zone/sl/tp1/tp2/position_pct 序列化
    sha256 截 16 字节 = 32 hex）。

    口径补充：payload 额外并入 plan_status.state——否则 watch 与 neutral 的
    无计划态（字段全 NULL）哈希相同，状态切换会被 uk 静默吞掉。
    """
    p = plan or {}
    zone = p.get("entry_zone") or [None, None]
    payload = {
        "side": p.get("side"),
        "entry_lo": zone[0] if len(zone) > 0 else None,
        "entry_hi": zone[1] if len(zone) > 1 else None,
        "stop_loss": p.get("stop_loss"),
        "tp1": p.get("take_profit_1"),
        "tp2": p.get("take_profit_2"),
        "position_pct": p.get("position_pct"),
        "state": plan_state,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _build_reco_row(symbol: str, data: dict, as_of: str) -> tuple[tuple, str]:
    """consensus 响应 → jarvis_reco_plan 一行。返回 (row, plan_hash)。"""
    cons = data.get("consensus") or {}
    plan = cons.get("trade_plan")  # None = neutral/watch 无计划（Q6：也落一行）
    if not isinstance(plan, dict):
        plan = None
    status = cons.get("plan_status") or {}
    state = _s(status.get("state"), 16) or ("ok" if plan else "neutral")
    h = _plan_hash(plan, state)

    p = plan or {}
    zone = p.get("entry_zone") or [None, None]
    basis = p.get("basis")
    row = (
        symbol,
        # 计划来源框架：MTF 融合选中的周期（plan.source_tf）优先，缺失回退 primary_tf
        _s(p.get("source_tf") or cons.get("primary_tf"), 8),
        _s(p.get("side"), 8),
        _num(zone[0] if len(zone) > 0 else None),
        _num(zone[1] if len(zone) > 1 else None),
        _num(p.get("stop_loss")),
        _num(p.get("take_profit_1")),
        _num(p.get("take_profit_2")),
        _num(p.get("rr")),
        _num(p.get("position_pct")),
        state,
        _s(status.get("reason"), 512),
        json.dumps(basis, ensure_ascii=False) if basis else None,
        _num(data.get("price")),
        _s(cons.get("direction"), 16),
        _num(cons.get("confidence")),
        h,
        as_of,
    )
    return row, h


def _seed_last_hash(ctx: SyncContext) -> None:
    """首轮从本地 cursors.json 播种 symbol→hash 映射（丢失无害，uk 兜底）。"""
    global _last_hash_seeded
    if _last_hash_seeded:
        return
    _last_hash_seeded = True
    raw = ctx.cursors.get(RECO_TABLE)
    if not raw:
        return
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            _last_hash.update({str(k): str(v) for k, v in data.items()})
            log.info("[%s] 判重映射已从 cursors.json 恢复 %d 币", RECO_TABLE, len(_last_hash))
    except json.JSONDecodeError:
        log.warning("[%s] cursors.json 中判重映射损坏，忽略（uk 幂等兜底）", RECO_TABLE)


# [2026-08-05 下线] 若依「推荐点位」页与 App 端已删除，表无消费者，停止注册（恢复：取消下行注释）
# @register_task(group="api", table=RECO_TABLE)
def sync_reco_plan(ctx: SyncContext) -> TaskResult:
    """逐币拉 consensus → hash 判重 → INSERT IGNORE jarvis_reco_plan。

    此拉取同时触发源侧 twelve record_batch 落库（一石二鸟，计划 §3.2）；
    consensus 源侧 180s 缓存，本组周期 ≥180s 时不产生额外重算压力。
    """
    _seed_last_hash(ctx)
    cfg = ctx.config
    base = cfg["dashboard_base_url"]
    timeout = float(cfg["http_timeout_s"])
    now = _now_gmt8()
    as_of = _dt3(now)

    rows: list[tuple] = []
    new_hash: dict[str, str] = {}
    ok_symbols = 0
    max_lag: Optional[float] = None

    for sym in ctx.symbols:
        data = _http_json(base, f"/api/twelve/consensus?symbol={sym}", timeout)
        if not data or not data.get("ok"):
            continue
        ok_symbols += 1
        row, h = _build_reco_row(sym, data, as_of)
        new_hash[sym] = h
        if _last_hash.get(sym) == h:
            continue  # 计划未变：跳过 no-op INSERT
        rows.append(row)

    if ok_symbols == 0:
        return _round_fail(RECO_TABLE, "consensus 全币失败")
    _consec_fail[RECO_TABLE] = 0

    conn = ctx.mysql.get()
    if conn is None:
        _stash_pending(RECO_TABLE, rows)
        return TaskResult(rows=0, error="MySQL 不可达，本轮已暂存内存")

    pending = _take_pending(RECO_TABLE)
    try:
        inserted = _flush_rows(conn, RECO_TABLE, _RECO_INSERT, pending + rows)
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        _pending[RECO_TABLE] = pending + rows  # 整批重试（游标未推进）
        raise

    # 写库成功才推进判重映射（对齐"游标从不后退"纪律）
    _last_hash.update(new_hash)
    ctx.cursors.set(RECO_TABLE, json.dumps(_last_hash, sort_keys=True, ensure_ascii=False))
    lag = (datetime.now(TZ_GMT8) - now).total_seconds()
    max_lag = round(lag, 3)
    return TaskResult(
        rows=inserted,
        cursor_value=f"{ok_symbols}sym@{now.strftime('%H:%M:%S')}",
        lag_seconds=max_lag,
    )


# ══════════════════════════════════════════ market_snapshot（market_snapshot 组 300s）

_SNAP_INSERT = (
    f"INSERT IGNORE INTO {SNAP_TABLE} "
    "(symbol, snap_time, price, price_chg_24h, funding_rate, oi_value, "
    "oi_change_pct, long_pct, short_pct, ls_ratio, fng_value, fng_class, "
    "sentiment_score, sentiment_bias, sentiment_headline, factors_json) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


def _build_snap_row(symbol: str, snap_time: str, intel: dict,
                    senti: Optional[dict]) -> tuple:
    """market-intel + sentiment → jarvis_market_snapshot 一行。

    源数据覆盖范围：funding 仅 4 币、oi/long_short 仅 BTCUSDT、fng 全市场
    共享——不匹配的列写 NULL 属正常。price_24h 自 2026-08-05 起带
    "all" 全量 USDT 映射（逐币价格不再 NULL）；旧 intel（dashboard 未重启）
    无 "all" 时回退单币匹配口径。
    """
    funding = (intel.get("funding_rate") or {}).get(symbol)
    oi = intel.get("oi") or {}
    ls = intel.get("long_short") or {}
    p24 = intel.get("price_24h") or {}
    fng = intel.get("fng") or {}
    oi_match = oi.get("symbol") == symbol
    ls_match = ls.get("symbol") == symbol
    p24_row = (p24.get("all") or {}).get(symbol)
    if p24_row is None and p24.get("symbol") == symbol:
        p24_row = p24  # 兼容旧单币口径（升级窗口期 dashboard/sync 版本不一致）
    p24_row = p24_row or {}

    senti = senti if isinstance(senti, dict) and senti.get("ok") else {}
    factors = senti.get("factors")
    return (
        symbol,
        snap_time,
        _num(p24_row.get("last_price")),
        _num(p24_row.get("change_pct")),
        _num(funding),
        _num(oi.get("value")) if oi_match else None,
        _num(oi.get("change_pct")) if oi_match else None,
        _num(ls.get("long_pct")) if ls_match else None,
        _num(ls.get("short_pct")) if ls_match else None,
        _num(ls.get("ratio")) if ls_match else None,
        int(fng["value"]) if isinstance(fng.get("value"), (int, float)) else None,
        _s(fng.get("classification"), 32),
        _num(senti.get("score")),
        _s(senti.get("bias"), 16),
        _s(senti.get("headline"), 255),
        json.dumps(factors, ensure_ascii=False) if factors else None,
    )


@register_task(group="market_snapshot", table=SNAP_TABLE)
def sync_market_snapshot(ctx: SyncContext) -> TaskResult:
    """market-intel 全局一次 + 逐币 sentiment → INSERT IGNORE 时序快照。

    写库成功后追加 symbol 维度 delete-absent（节流）：非 watchlist 币种的历史
    快照行从镜像清理，总览币种卡片跟随币种池收敛。
    """
    cfg = ctx.config
    base = cfg["dashboard_base_url"]
    timeout = float(cfg["http_timeout_s"])

    intel = _http_json(base, "/api/market-intel", timeout)
    if not intel or not intel.get("ok"):
        return _round_fail(SNAP_TABLE, "market-intel 失败")
    _consec_fail[SNAP_TABLE] = 0

    now = _now_gmt8()
    snap_time = now.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

    rows: list[tuple] = []
    senti_ok = 0
    for sym in ctx.symbols:
        senti = _http_json(base, f"/api/sentiment?symbol={sym}", timeout)
        if senti and senti.get("ok"):
            senti_ok += 1
        else:
            senti = None  # 情绪列 NULL 容忍，快照行仍写（intel 部分有效）
        rows.append(_build_snap_row(sym, snap_time, intel, senti))

    conn = ctx.mysql.get()
    if conn is None:
        _stash_pending(SNAP_TABLE, rows)
        return TaskResult(rows=0, error="MySQL 不可达，本轮已暂存内存")

    pending = _take_pending(SNAP_TABLE)
    try:
        inserted = _flush_rows(conn, SNAP_TABLE, _SNAP_INSERT, pending + rows)
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        _pending[SNAP_TABLE] = pending + rows
        raise

    # 写库成功后清扫非币种池残留（节流；失败上抛重试，本轮行已落库不受影响）
    _sweep_absent_symbols(conn, SNAP_TABLE, ctx.symbols)

    lag = (datetime.now(TZ_GMT8) - now).total_seconds()
    return TaskResult(
        rows=inserted,
        cursor_value=f"{snap_time}|senti {senti_ok}/{len(ctx.symbols)}",
        lag_seconds=round(lag, 3),
    )
