#!/usr/bin/env python3
"""贾维斯 JARVIS — 本地订单簿引擎（order-flow phase-1，docs/order-flow-design.md §4）。

消费 jarvis_ws_stream 已有的 depth 增量流（此前零消费方），维护每币种本地
订单簿（L2），并留存深度历史时间序列——Bookmap 热力（phase-3）与 Jigsaw
实时 DOM（phase-2）的共同数据地基。

订单簿维护协议（币安官方口径）：
  合约（fstream depthUpdate 带 pu）：
    快照 GET /fapi/v1/depth?limit=1000 → lastUpdateId
    丢弃 u < lastUpdateId 的增量；首条须 U <= lastUpdateId <= u；
    此后每条须 pu == 上一条 u，断档即置 resync 重拉快照。
  现货（回退模式，无 pu）：
    首条须 U <= lastUpdateId+1 <= u；此后每条须 U == 上一条 u + 1。

REST 限流纪律（418 封禁事故后的硬约束）：
  - 快照**只在初始化/断档重同步时拉取**，日常全靠 WS 增量维护，绝无周期轮询；
  - 每币快照最小间隔 SNAP_MIN_INTERVAL_S、全局最小间隔 SNAP_GLOBAL_GAP_S；
  - 连续失败按指数退避（封顶 SNAP_BACKOFF_MAX_S）；
  - 收到 418/429 → 按 Retry-After（缺省 120s）设**全局冷却**，冷却期内一切
    快照请求直接短路（零网络请求），与 desktop binanceFeed 的封禁闸门同语义；
  - 不复用 jarvis_crypto_data._get：其失败回退磁盘缓存会返回陈旧
    lastUpdateId，导致增量永远对不齐（对本模块是错误语义）。

存储（与 tape_classify 的「内存实时 + 落库历史」双层同构）：
  - 内存：每币 deque 保留最近 SLICE_MEM_MAX 片（5s 粒度，约 30 分钟）；
  - 落库：orderbook_depth_slices（jarvis_db 兼容层，pg 可切），分钟级降采样
    （每分钟保留该分钟最后一片），upsert 幂等，保留期 book_retention_days；
  - WS ingest 线程内绝不落盘、绝不联网（快照/落库全在独立后台线程）。

下游接口：
  book(symbol)              当前 DOM 载荷（形状对齐 jarvis_depth_view.aggregate_book，
                            前端零改造切换；附 synced/age_ms/seq）
  heatmap(symbol, s, e)     历史深度切片区间查询（phase-3 热力图数据源）
  health()                  各币同步状态/快照计数/限流状态

用法：
  python jarvis_orderbook.py test --seconds 30   # 主网实测：对齐后与 REST 快照对账
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from typing import Optional

import jarvis_db as jdb

FAPI_DEPTH = "https://fapi.binance.com/fapi/v1/depth"
SPOT_DEPTH = "https://api.binance.com/api/v3/depth"
SNAPSHOT_LIMIT = 1000
TIMEOUT = 10
_HEADERS = {"User-Agent": "jarvis-orderbook/1.0"}

# ── REST 快照限流（418 事故后的硬纪律，见模块头）──
SNAP_MIN_INTERVAL_S = 30.0     # 同币两次快照最小间隔
SNAP_GLOBAL_GAP_S = 2.0        # 任意两次快照的全局最小间隔
SNAP_BACKOFF_BASE_S = 30.0     # 连续失败退避起点
SNAP_BACKOFF_MAX_S = 300.0     # 退避封顶
BAN_DEFAULT_S = 120.0          # 418/429 无 Retry-After 时的保守全局冷却

# ── 内存/落库口径 ──
PRE_SYNC_BUFFER_MAX = 4000     # 未同步期增量缓冲上限（250ms 流 ≈ 16 分钟）
BOOK_LEVELS_MAX = 2000         # 单侧价位数上限（防脏数据撑爆内存）
SLICE_MEM_MAX = 360            # 内存切片数（5s 粒度 ≈ 30 分钟）
SLICE_INTERVAL_S = 5.0         # 内存切片周期（配置 book_snapshot_interval_s 可覆盖）
PERSIST_LEVELS = 60            # 每片每侧落库价格档数（配置 book_levels 可覆盖）
RETENTION_DAYS = 14            # 切片保留天数（配置 book_retention_days 可覆盖）
_PRUNE_INTERVAL_S = 3600.0     # 保留期清理节流
HEATMAP_INTERVALS_S = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
HEATMAP_SLICES_MAX = 720       # 区间查询单次最多返回切片数（1m 一整天=1440，取半天）

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")
LOG_PATH = os.path.join(DB_DIR, "jarvis_orderbook.log")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [BOOK] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _cfg_get(key: str, fallback):
    try:
        import jarvis_config as jc
        v = jc.get(key)
        return fallback if v is None else v
    except Exception:  # noqa: BLE001
        return fallback


# ═══════════════════════════ 纯函数核心（smoketest 直测） ═══════════════════════════


def apply_diff(bids: dict[float, float], asks: dict[float, float],
               event: dict) -> None:
    """把一条 depthUpdate 增量应用到 book 两侧（qty=0 删档）。原地修改。"""
    for key, side in (("b", bids), ("a", asks)):
        for lv in event.get(key) or []:
            try:
                p, q = float(lv[0]), float(lv[1])
            except (TypeError, ValueError, IndexError):
                continue
            if p <= 0:
                continue
            if q <= 0:
                side.pop(p, None)
            elif len(side) < BOOK_LEVELS_MAX or p in side:
                side[p] = q


def event_ids(event: dict) -> tuple[int, int, Optional[int]]:
    """增量事件 → (U, u, pu)；现货无 pu 返回 None。坏字段抛 ValueError。"""
    U = int(event["U"])
    u = int(event["u"])
    pu = event.get("pu")
    return U, u, (int(pu) if pu is not None else None)


def covers_snapshot(U: int, u: int, pu: Optional[int], last_update_id: int) -> bool:
    """首条增量是否衔接快照。合约：U <= id <= u；现货：U <= id+1 <= u。"""
    if pu is not None:
        return U <= last_update_id <= u
    return U <= last_update_id + 1 <= u


def is_continuous(U: int, pu: Optional[int], prev_u: int) -> bool:
    """后续增量是否连续。合约：pu == prev_u；现货：U == prev_u + 1。"""
    if pu is not None:
        return pu == prev_u
    return U == prev_u + 1


def replay_buffer(bids: dict, asks: dict, buffered: list[dict],
                  last_update_id: int) -> tuple[bool, int]:
    """快照建簿后回放缓冲增量。

    Returns: (ok, last_u)。ok=False 表示缓冲与快照无法衔接（快照太新/太旧
    或中途断档），调用方应重拉快照。缓冲为空视为成功（等首条实时增量校验）。
    """
    prev_u = None
    for ev in buffered:
        try:
            U, u, pu = event_ids(ev)
        except (KeyError, TypeError, ValueError):
            continue
        if u <= last_update_id:      # 快照已覆盖的旧增量：丢弃
            continue
        if prev_u is None:
            if not covers_snapshot(U, u, pu, last_update_id):
                return False, last_update_id
        elif not is_continuous(U, pu, prev_u):
            return False, last_update_id
        apply_diff(bids, asks, ev)
        prev_u = u
    return True, (prev_u if prev_u is not None else last_update_id)


def book_levels(bids: dict[float, float], asks: dict[float, float]
                ) -> tuple[list[list[float]], list[list[float]]]:
    """book dict → 币安快照形状的档位列表（买降序 / 卖升序）。"""
    b = sorted(bids.items(), key=lambda kv: -kv[0])
    a = sorted(asks.items(), key=lambda kv: kv[0])
    return [[p, q] for p, q in b], [[p, q] for p, q in a]


def slice_book(bids: dict[float, float], asks: dict[float, float],
               levels: int = PERSIST_LEVELS,
               bucket: float | None = None) -> dict | None:
    """当前 book → 落库切片：{bucket, bids:[[price,usd]..], asks:[[price,usd]..]}。

    复用 jarvis_depth_view 的桶口径（nice_step(mid×0.02%)），每侧取靠近
    mid 的前 levels 桶。book 空返回 None。
    """
    if not bids or not asks:
        return None
    best_bid = max(bids)
    best_ask = min(asks)
    mid = (best_bid + best_ask) / 2.0
    if not (mid > 0 and math.isfinite(mid)):
        return None
    if not bucket or bucket <= 0:
        try:
            import jarvis_depth_view as jdv
            bucket = float(jdv.nice_step(mid * 0.0002))
        except Exception:  # noqa: BLE001 — 兜底镜像 nice_step
            raw = mid * 0.0002
            exp = math.floor(math.log10(raw)) if raw > 0 else 0
            frac = raw / (10 ** exp) if raw > 0 else 1.0
            nice = 1.0 if frac < 1.5 else 2.0 if frac < 3.5 else \
                5.0 if frac < 7.5 else 10.0
            bucket = nice * (10 ** exp)

    def _agg(side: dict, is_bid: bool) -> list[list[float]]:
        acc: dict[float, float] = {}
        for p, q in side.items():
            b = (math.floor(p / bucket) if is_bid else math.ceil(p / bucket)) * bucket
            acc[round(b, 10)] = acc.get(round(b, 10), 0.0) + p * q
        rows = sorted(acc.items(), key=lambda kv: -kv[0] if is_bid else kv[0])
        return [[p, round(usd, 2)] for p, usd in rows[:levels]]

    return {"bucket": bucket, "mid": round(mid, 8),
            "bids": _agg(bids, True), "asks": _agg(asks, False)}


def downsample_slices(rows: list[dict], interval_s: int,
                      max_slices: int = HEATMAP_SLICES_MAX) -> tuple[list[dict], bool]:
    """切片列表（ts 升序）→ 每 interval 桶保留最后一片；超上限截尾保留近端。"""
    by_bucket: dict[int, dict] = {}
    for r in rows:
        by_bucket[int(r["ts"]) // interval_s * interval_s] = r
    out = [{**by_bucket[ts], "ts": ts} for ts in sorted(by_bucket)]
    truncated = len(out) > max_slices
    return out[-max_slices:], truncated


# ═══════════════════════════ 模块级运行时状态 ═══════════════════════════

_LOCK = threading.Lock()
# {SYMBOL: {...}}；synced=False 时增量进 buffer，等后台线程拉快照对齐
_STATE: dict[str, dict] = {}
_REGISTERED = False
_SYNC_THREAD: Optional[threading.Thread] = None
_PERSIST_STARTED = False
_INITED = False
_LAST_PRUNE = 0.0

# REST 快照限流状态（全局）
_SNAP = {"global_last_ts": 0.0, "ban_until": 0.0, "fetches": 0, "bans": 0}


def _sym_state(symbol: str) -> dict:
    st = _STATE.get(symbol)
    if st is None:
        st = {"bids": {}, "asks": {}, "last_u": 0, "synced": False,
              "buffer": deque(maxlen=PRE_SYNC_BUFFER_MAX),
              "seq": 0, "updated_ts": 0.0, "market": None,
              "snap_last_ts": 0.0, "snap_fails": 0, "resyncs": 0,
              "slices": deque(maxlen=SLICE_MEM_MAX), "flushed_min": 0}
        _STATE[symbol] = st
    return st


def ingest(symbol: str, data: dict) -> None:
    """depth 流回调入口（WS 线程内同步调用——O(档位数) 轻量，永不抛出）。"""
    try:
        sym = (symbol or "").upper()
        with _LOCK:
            st = _sym_state(sym)
            if not st["synced"]:
                st["buffer"].append(data)
                return
            try:
                U, u, pu = event_ids(data)
            except (KeyError, TypeError, ValueError):
                return
            if u <= st["last_u"]:        # 迟到/重复帧：丢弃
                return
            if not is_continuous(U, pu, st["last_u"]):
                # 断档：置未同步，本条起重新缓冲，等后台线程重拉快照
                st["synced"] = False
                st["resyncs"] += 1
                st["buffer"].clear()
                st["buffer"].append(data)
                return
            apply_diff(st["bids"], st["asks"], data)
            st["last_u"] = u
            st["seq"] += 1
            st["updated_ts"] = time.time()
    except Exception:  # noqa: BLE001 — WS 回调铁律：绝不向数据流抛出
        pass


# ═══════════════════════════ REST 快照（低频 + 全局限流） ═══════════════════════════


def _fetch_snapshot(symbol: str, market: str) -> dict | None:
    """拉一次深度快照（418/429 → 设全局冷却并返回 None）。仅同步线程调用。

    [2026-08-09 封禁成因加固] 冷却从进程内升级为跨进程：出网前查 jarvis_net
    共享封禁登记（别的进程撞出的 418 也短路本模块）；本模块吃到 418/429 时
    反向登记共享冷却，全系统一起退避。快照 limit=1000 权重 20，纳入共享
    权重预算记账。
    """
    import requests

    import jarvis_net as _jnet
    _jnet.ensure_proxy()
    url = FAPI_DEPTH if market == "futures" else SPOT_DEPTH
    ban_ts = 0.0
    try:
        ban_ts = float(_jnet.banned_until(url) or 0)
    except Exception:  # noqa: BLE001 — 登记层异常不阻断快照
        pass
    if ban_ts:
        _SNAP["ban_until"] = max(_SNAP["ban_until"], ban_ts)
        return None
    try:
        _budget = getattr(_jnet, "budget_take", None)
        if callable(_budget):
            try:
                ok = _budget(url, 180, cost=20.0)
            except TypeError:  # 旧版无 cost 参数
                ok = _budget(url, 180)
            if not ok:
                _log(f"{symbol} 快照因分钟预算耗尽跳过（下轮重试）")
                return None
    except Exception:  # noqa: BLE001
        pass
    try:
        r = requests.get(url, params={"symbol": symbol, "limit": SNAPSHOT_LIMIT},
                         headers=_HEADERS, timeout=TIMEOUT)
        if r.status_code in (418, 429):
            retry_after = float(r.headers.get("Retry-After") or 0) or BAN_DEFAULT_S
            _SNAP["ban_until"] = time.time() + retry_after
            _SNAP["bans"] += 1
            # 反向登记共享冷却：418 尝试解析 "banned until" 精确截止，
            # 解析不到 / 429 按 Retry-After 冷却
            try:
                body_msg = ""
                try:
                    body_msg = str((r.json() or {}).get("msg", ""))
                except Exception:  # noqa: BLE001
                    pass
                import re as _re
                m = _re.search(r"banned until (\d{10,16})", body_msg, _re.I)
                if m:
                    ts = float(m.group(1))
                    _jnet.report_ban(url, ts / 1000.0 if ts > 1e12 else ts)
                else:
                    getattr(_jnet, "report_cooldown",
                            lambda *_: None)(url, retry_after)
            except Exception:  # noqa: BLE001
                pass
            _log(f"⚠️ REST {r.status_code}（限流/封禁），全局冷却 {retry_after:.0f}s"
                 "——冷却期内快照请求零发出（已同步登记跨进程冷却）")
            return None
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("lastUpdateId") and data.get("bids"):
            _SNAP["fetches"] += 1
            return data
    except Exception as exc:  # noqa: BLE001
        _log(f"{symbol} 快照拉取失败: {repr(exc)[:160]}")
    return None


def _snapshot_allowed(st: dict, now: float) -> bool:
    """限流门禁：全局冷却 / 全局间隔 / 每币间隔 / 失败退避。"""
    if now < _SNAP["ban_until"]:
        return False
    if now - _SNAP["global_last_ts"] < SNAP_GLOBAL_GAP_S:
        return False
    backoff = min(SNAP_BACKOFF_MAX_S,
                  SNAP_BACKOFF_BASE_S * (2 ** min(st["snap_fails"], 6)))
    gap = max(SNAP_MIN_INTERVAL_S, backoff if st["snap_fails"] else 0.0)
    return now - st["snap_last_ts"] >= gap


def _ws_market() -> str:
    """当前 WS 端点市场（futures/spot）——快照必须与增量流同域才能对齐。"""
    try:
        import jarvis_ws_stream as jws
        return jws.health().get("market") or "futures"
    except Exception:  # noqa: BLE001
        return "futures"


def _sync_once(sym: str) -> bool:
    """对单币执行一次「快照 + 缓冲回放」对齐。仅同步线程调用。"""
    now = time.time()
    with _LOCK:
        st = _sym_state(sym)
        if st["synced"] or not _snapshot_allowed(st, now):
            return False
        st["snap_last_ts"] = now
        _SNAP["global_last_ts"] = now
    market = _ws_market()
    snap = _fetch_snapshot(sym, market)
    if snap is None:
        with _LOCK:
            _sym_state(sym)["snap_fails"] += 1
        return False
    last_id = int(snap["lastUpdateId"])
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    apply_diff(bids, asks, {"b": snap.get("bids") or [],
                            "a": snap.get("asks") or []})
    with _LOCK:
        st = _sym_state(sym)
        ok, last_u = replay_buffer(bids, asks, list(st["buffer"]), last_id)
        if not ok:
            # 快照与缓冲无法衔接（快照偏旧/缓冲断档）：保留缓冲，退避后重试
            st["snap_fails"] += 1
            return False
        st.update({"bids": bids, "asks": asks, "last_u": last_u,
                   "synced": True, "market": market, "snap_fails": 0,
                   "updated_ts": time.time()})
        st["buffer"].clear()
        depth_n = (len(bids), len(asks))
    _log(f"{sym} 订单簿已对齐 [{market}] lastUpdateId={last_id} "
         f"档位 bid={depth_n[0]}/ask={depth_n[1]}")
    return True


def _sync_loop() -> None:
    while True:
        try:
            with _LOCK:
                pending = [s for s, st in _STATE.items() if not st["synced"]]
            for sym in pending:
                _sync_once(sym)
        except Exception as exc:  # noqa: BLE001
            _log(f"同步线程异常（继续）: {exc!r}")
        time.sleep(1.0)


def register() -> bool:
    """幂等：挂 WS depth 回调 + 启动快照对齐线程。WS 模块缺失返回 False。"""
    global _REGISTERED, _SYNC_THREAD
    if _REGISTERED:
        return True
    try:
        import jarvis_ws_stream as jws
        if not jws.register_callback("depth", ingest):
            return False
        _SYNC_THREAD = threading.Thread(target=_sync_loop, daemon=True,
                                        name="jarvis-book-sync")
        _SYNC_THREAD.start()
        _REGISTERED = True
        return True
    except Exception:  # noqa: BLE001
        return False


# ═══════════════════════════ 落库（内存切片 → 分钟降采样） ═══════════════════════════


def _conn(db_path: str | None = None):
    os.makedirs(DB_DIR, exist_ok=True)
    return jdb.connect(db_path or DB_PATH)


def init_db(db_path: str | None = None) -> None:
    global _INITED
    with _conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orderbook_depth_slices (
                symbol  TEXT NOT NULL,
                ts      INTEGER NOT NULL,
                bucket  REAL,
                bids    TEXT,
                asks    TEXT,
                PRIMARY KEY (symbol, ts)
            )
            """
        )
    if db_path is None:
        _INITED = True


def _ensure_init() -> None:
    if not _INITED:
        init_db()


def take_slices(now_s: float | None = None) -> int:
    """把各已同步币种的当前 book 切一片进内存 deque。Returns 切片数。"""
    now = now_s if now_s is not None else time.time()
    n = 0
    levels = int(_cfg_get("book_levels", PERSIST_LEVELS))
    with _LOCK:
        for sym, st in _STATE.items():
            if not st["synced"]:
                continue
            sl = slice_book(st["bids"], st["asks"], levels=levels)
            if sl is None:
                continue
            st["slices"].append({"ts": int(now), **sl})
            n += 1
    return n


def flush_slices(now_s: float | None = None, db_path: str | None = None) -> int:
    """分钟降采样落库：每币每完结分钟保留该分钟**最后一片** upsert。

    持锁只做快照，DB 写在锁外；写成功才推进水位（失败下轮重试，upsert 幂等）。
    Returns 写入行数。
    """
    global _LAST_PRUNE
    written = 0
    try:
        if db_path is None:
            _ensure_init()
        now = now_s if now_s is not None else time.time()
        cur_min = int(now) // 60
        pending: dict[str, dict[int, dict]] = {}
        with _LOCK:
            for sym, st in _STATE.items():
                mark = int(st.get("flushed_min") or 0)
                rows: dict[int, dict] = {}
                for sl in st["slices"]:
                    mn = int(sl["ts"]) // 60
                    if mn >= cur_min or mn <= mark:
                        continue
                    rows[mn] = sl          # 同分钟后者覆盖 = 分钟最后一片
                if rows:
                    pending[sym] = rows
        for sym, rows in pending.items():
            try:
                with _conn(db_path) as conn:
                    for mn in sorted(rows):
                        sl = rows[mn]
                        conn.execute(
                            """
                            INSERT INTO orderbook_depth_slices
                              (symbol, ts, bucket, bids, asks)
                            VALUES (?,?,?,?,?)
                            ON CONFLICT(symbol, ts) DO UPDATE SET
                              bucket=excluded.bucket,
                              bids=excluded.bids,
                              asks=excluded.asks
                            """,
                            (sym, mn * 60, sl["bucket"],
                             json.dumps(sl["bids"], separators=(",", ":")),
                             json.dumps(sl["asks"], separators=(",", ":"))))
                written += len(rows)
                with _LOCK:
                    st = _STATE.get(sym)
                    if st is not None:
                        st["flushed_min"] = max(int(st.get("flushed_min") or 0),
                                                max(rows))
            except Exception as exc:  # noqa: BLE001 — 单币失败不影响其它
                _log(f"{sym} 切片落库失败: {exc!r}")
        if db_path is None and time.time() - _LAST_PRUNE > _PRUNE_INTERVAL_S:
            _LAST_PRUNE = time.time()
            prune_old()
        return written
    except Exception as exc:  # noqa: BLE001 — 持久化绝不向调用方抛出
        _log(f"切片落库异常: {exc!r}")
        return written


def prune_old(now_s: float | None = None, db_path: str | None = None) -> int:
    """删除保留期外切片（book_retention_days，默认 14 天）。失败返回 0。"""
    try:
        if db_path is None:
            _ensure_init()
        now = now_s if now_s is not None else time.time()
        days = int(_cfg_get("book_retention_days", RETENTION_DAYS))
        cutoff = int(now) - max(1, days) * 86400
        with _conn(db_path) as conn:
            cur = conn.execute(
                "DELETE FROM orderbook_depth_slices WHERE ts < ?", (cutoff,))
            return cur.rowcount if cur.rowcount is not None else 0
    except Exception as exc:  # noqa: BLE001
        _log(f"保留期清理失败: {exc!r}")
        return 0


def start_persist() -> bool:
    """启动切片+落库后台 daemon 线程（幂等）。失败返回 False，绝不抛出。"""
    global _PERSIST_STARTED
    if _PERSIST_STARTED:
        return True
    try:
        def _loop() -> None:
            last_flush = 0.0
            while True:
                itv = max(1.0, float(_cfg_get("book_snapshot_interval_s",
                                              SLICE_INTERVAL_S)))
                time.sleep(itv)
                try:
                    take_slices()
                    if time.time() - last_flush >= 60.0:
                        last_flush = time.time()
                        flush_slices()
                except Exception:  # noqa: BLE001 — 双保险
                    pass

        threading.Thread(target=_loop, daemon=True,
                         name="jarvis-book-persist").start()
        _PERSIST_STARTED = True
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"落库线程启动失败: {exc!r}")
        return False


# ═══════════════════════════ 查询接口 ═══════════════════════════


def book(symbol: str, max_buckets: int = 30, bucket: float | None = None) -> dict:
    """当前本地订单簿 → DOM 载荷（形状对齐 depth_view.aggregate_book 输出）。

    synced=False / book 空时 ok=False（调用方可回退 REST 快照路径）。
    """
    sym = (symbol or "BTCUSDT").upper()
    with _LOCK:
        st = _STATE.get(sym)
        if st is None or not st["synced"] or not st["bids"] or not st["asks"]:
            return {"ok": False, "symbol": sym, "synced": False,
                    "error": "本地订单簿未就绪（未同步或无数据）"}
        bids, asks = book_levels(st["bids"], st["asks"])
        meta = {"market": st["market"], "seq": st["seq"],
                "age_ms": int((time.time() - st["updated_ts"]) * 1000),
                "last_u": st["last_u"]}
    import jarvis_depth_view as jdv
    return {"ok": True, "symbol": sym, "synced": True, "source": "ws_book",
            "ts": time.time(), **meta,
            **jdv.aggregate_book(bids, asks, bucket=bucket,
                                 max_buckets=max_buckets)}


def heatmap(symbol: str, start_s: int, end_s: int, interval: str = "1m",
            db_path: str | None = None) -> dict:
    """历史深度切片区间查询（热力图数据源）：库内切片 + 内存未落盘切片合并。

    Returns: {ok, symbol, interval, slices:[{ts, bucket, mid?, bids, asks}],
    range:{start,end,truncated}}；ts 升序。失败返回错误封套，不抛出。
    """
    sym = (symbol or "BTCUSDT").upper()
    try:
        itv = HEATMAP_INTERVALS_S.get(interval)
        if itv is None:
            return {"ok": False, "symbol": sym, "interval": interval,
                    "slices": [], "error": f"interval 无效：{interval}"
                    f"（可选 {'/'.join(HEATMAP_INTERVALS_S)}）"}
        s0, e0 = max(0, int(start_s)), max(0, int(end_s))
        if s0 > e0:
            s0, e0 = e0, s0
        rows: list[dict] = []
        if db_path is None:
            _ensure_init()
        with _conn(db_path) as conn:
            cur = conn.execute(
                "SELECT ts, bucket, bids, asks FROM orderbook_depth_slices "
                "WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts ASC",
                (sym, s0, e0))
            for r in cur.fetchall():
                try:
                    rows.append({"ts": int(r["ts"]), "bucket": r["bucket"],
                                 "bids": json.loads(r["bids"] or "[]"),
                                 "asks": json.loads(r["asks"] or "[]")})
                except Exception:  # noqa: BLE001 — 单行损坏不拖垮查询
                    continue
        # 内存切片并入（未落盘的近端；同 ts 桶以内存为准由 downsample 覆盖实现）
        with _LOCK:
            st = _STATE.get(sym)
            mem = ([{"ts": sl["ts"], "bucket": sl["bucket"],
                     "bids": sl["bids"], "asks": sl["asks"]}
                    for sl in st["slices"] if s0 <= sl["ts"] <= e0]
                   if st else [])
        rows.extend(mem)
        rows.sort(key=lambda r: r["ts"])
        slices, truncated = downsample_slices(rows, itv)
        return {"ok": True, "symbol": sym, "interval": interval,
                "slices": slices,
                "range": {"start": s0, "end": e0, "truncated": truncated}}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "symbol": sym, "interval": interval,
                "slices": [], "error": repr(exc)[:200]}


def health() -> dict:
    """引擎健康度：各币同步态 + 快照限流状态（/api/orderbook/live 附带用）。"""
    now = time.time()
    with _LOCK:
        syms = {sym: {"synced": st["synced"], "seq": st["seq"],
                      "levels": (len(st["bids"]), len(st["asks"])),
                      "age_ms": (int((now - st["updated_ts"]) * 1000)
                                 if st["updated_ts"] else None),
                      "resyncs": st["resyncs"], "snap_fails": st["snap_fails"],
                      "buffered": len(st["buffer"]), "market": st["market"],
                      "mem_slices": len(st["slices"])}
                for sym, st in _STATE.items()}
    return {"registered": _REGISTERED, "persist_started": _PERSIST_STARTED,
            "snapshot": {"fetches": _SNAP["fetches"], "bans": _SNAP["bans"],
                         "ban_active": now < _SNAP["ban_until"],
                         "ban_remaining_s": max(0, round(_SNAP["ban_until"] - now))},
            "symbols": syms}


def reset_state() -> None:
    """清空运行时状态（smoketest 隔离用）。"""
    with _LOCK:
        _STATE.clear()
    _SNAP.update({"global_last_ts": 0.0, "ban_until": 0.0,
                  "fetches": 0, "bans": 0})


# ═══════════════════════════ CLI 自测 ═══════════════════════════


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="贾维斯本地订单簿引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test", help="主网实测：收流对齐后与 REST 快照对账")
    t.add_argument("--seconds", type=int, default=30)
    t.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args()

    import jarvis_ws_stream as jws
    sym = args.symbol.upper()
    register()
    jws.start([sym])
    print(f"收流 {args.seconds}s 等待订单簿对齐…")
    deadline = time.time() + max(10, args.seconds)
    while time.time() < deadline:
        time.sleep(1)
        h = health()["symbols"].get(sym) or {}
        if h.get("synced"):
            break
    out = book(sym, max_buckets=10)
    print(json.dumps({k: v for k, v in out.items() if k not in ("bids", "asks")},
                     ensure_ascii=False, indent=2))
    if not out.get("ok"):
        print("❌ 订单簿未对齐")
        jws.stop()
        return 1
    # 对账：本地 book mid 与新鲜 REST 快照 mid 偏差应 < 0.1%
    time.sleep(max(0.0, SNAP_GLOBAL_GAP_S))
    snap = _fetch_snapshot(sym, _ws_market())
    ok = False
    if snap:
        rb = float(snap["bids"][0][0])
        ra = float(snap["asks"][0][0])
        rmid = (rb + ra) / 2
        drift = abs(out["mid"] / rmid - 1) * 100
        ok = drift < 0.1
        print(f"对账：本地 mid={out['mid']} REST mid={rmid:.2f} "
              f"偏差 {drift:.4f}% → {'✅' if ok else '❌'}")
    print(json.dumps(health(), ensure_ascii=False, indent=2, default=str))
    jws.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
