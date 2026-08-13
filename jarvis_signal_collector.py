#!/usr/bin/env python3
"""贾维斯 JARVIS — T10 常驻信号采集器（采集覆盖率补齐，T9 结论可信的前置）。

═══ 为什么需要它（驱动链证据，2026-08-13 实地核验）═══
twelve_signal_changes 此前的全部生产写入点都挂在 dashboard 进程的 API 重算路径：
  ① /api/twelve/signals    jarvis_dashboard.py:4097  jsh.record_batch(...)
  ② /api/twelve/consensus  jarvis_dashboard.py:4136  jsh.record_batch(...)（5 TF 逐个）
两处都包在 _cached(60s/120s) 的 _calc 里——只有「dashboard 进程活着 + 有人调 API
（浏览器面板开着 / jarvis_sync_tasks_b RuoYi 同步在轮询）+ 缓存过期」三者同时成立
才会落库。tape_minute_bars（T9 P2 的唯一价格源）同样挂在 dashboard 进程：
WS aggTrade → jarvis_tape_classify.ingest（dashboard startup 注册，:3255）
→ start_persist 30s flush（:3261）。面板一关 / 机器一睡 → 信号与真 bar 双双断流，
缺口每天同一时段重复（T9 实测可用率按 UTC 小时 30%~86.5%，偏斜 2.9×）。

═══ 本模块 = 独立常驻进程，与面板无关 ═══
  jarvis_ws_stream 多周期 kline 流（sigcol_tfs，默认 5m/15m/30m/1h/4h）
    → 只认已收盘 bar（k.x=true；进行中 bar 一律丢弃）
    → WS 回调 O(1) 入队（绝不在 WS 线程算信号）
    → 工作线程：内存 K 线史（每 (币,TF) 一个 deque）→ jarvis_twelve_systems.run_all
      纯 CPU 计算 → jarvis_signal_history.record_batch(now=bar 收盘时刻) 落库
    → signal_collect_log 采集台账（每根已处理 bar 一行，覆盖率报表的数据源）
  可选（sigcol_tape，默认开）：同进程挂 aggTrade→tape 分钟 bar 落库链，
  使真 bar 覆盖也不再依赖 dashboard。

═══ REST 纪律（任务纪律①：WS 不占 REST 权重，不新增常态轮询）═══
  常态运行零 REST 轮询。仅三处按需出网，全部经 jarvis_crypto_data._get →
  jarvis_net 跨进程共享分钟预算 / 封禁登记 / 权重刹车：
    1. 种子：每 (币,TF) 首次事件拉 300 根历史（一次性，进程内加 0.25s 间隔）
    2. 缺口回补：WS 断线/进程重启后，单 (币,TF) 一次拉最新 ≤500 根补缺
       （上限 sigcol_backfill_bars，失败 60s 冷却，绝不循环重试）
    3. 基差（sigcol_basis，可关）：期现两腿 K 线，300s TTL 与 dashboard 同频
  注意：tape_minute_bars 无法用 kline 回补（aggTrade 逐笔即逝，且 T9 P2 只认
  真实分钟成交），真 bar 覆盖只能靠进程常驻——这正是「常开机器」的价值。

═══ 断层窗防线（2026-08-13 实跑教训：封禁期缓存会给出数天前的陈旧窗）═══
  REST 被封禁时 jarvis_crypto_data 回退磁盘缓存——种子/回补可能拿到陈旧数据。
  把「8 天前的 300 根 + 今天的 WS bar」拼成一窗算指标 = 污染 T9 样本。三道防线：
    1. 种子/回补一律做新鲜度检查（最新根须贴到事件前一根），陈旧即拒用；
    2. 小缺口回补失败 → 本根缓期（台账记 gap_unfilled 占位，可被升级），
       下次事件重试；大缺口且回补不可用 → 弃断层旧史改纯 WS 重建窗；
    3. 降级窗史攒够 30 根前如实记 insufficient_history 不算信号，
       每小时重试整窗重播种，REST 恢复后自动回到 300 根全窗。

═══ 双写入者防打架（state 新鲜度守卫）═══
  dashboard 面板开着时也在写 twelve_signal_state。record_batch 前先查该
  (币,TF) 的最大 updated_ts：bar 收盘时刻 ≤ 它 → 本 bar 只记台账不写信号库，
  防止两个写入者互相回退状态、制造 flip-flop 假变更污染 T9 样本。
  同理 sigcol_basis 关闭/取数失败时直接剔除 arbitrage 信号（不硬造降级中性），
  避免与 dashboard 的带基差信号来回翻方向。

用法：
  python jarvis_signal_collector.py run                  # 常驻采集（launchd/systemd 用）
  python jarvis_signal_collector.py coverage --days 7    # 覆盖率自检报表
  python jarvis_signal_collector.py coverage --days 7 --json
  # 作为库：import jarvis_signal_collector as jsc; jsc.start(); jsc.health()
常驻安装见 贾维斯-信号采集器-常驻运行说明.md（macOS launchd / Linux systemd）。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from typing import Optional

import jarvis_db as jdb
import jarvis_net

jarvis_net.ensure_proxy()  # 大陆网络：Binance 出网自动走本地代理

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")
LOG_PATH = os.path.join(DB_DIR, "jarvis_signal_collector.log")
STATUS_PATH = os.path.join(DB_DIR, "jarvis_signal_collector_status.json")

# TF → 毫秒。只支持币安 kline 流存在且 12 系统共识用到的周期。
TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
DEFAULT_TFS = ("5m", "15m", "30m", "1h", "4h")

HIST_MAXLEN = 360            # 每 (币,TF) 内存 K 线史容量（计算窗 300 + 余量）
SEED_LIMIT = 300             # 种子拉取根数（与 dashboard /api/twelve 同参）
MIN_BARS_FOR_SIGNALS = 30    # 与 dashboard 一致：不足 30 根不出信号
BASIS_TTL_S = 300.0          # 基差缓存 TTL（与 dashboard _twelve_basis 同频）
REST_MIN_INTERVAL_S = 0.25   # 进程内 REST 最小间隔（叠加共享预算之上的礼貌值）
BACKFILL_RETRY_COOLDOWN_S = 60.0  # 单 (币,TF) 回补失败后的冷却
RESEED_COOLDOWN_S = 3600.0   # 降级种子（纯 WS 起窗）的整窗重播种重试周期
LEDGER_PRUNE_INTERVAL_S = 3600.0  # 台账保留期清理节流：每小时一次
STATUS_WRITE_INTERVAL_S = 60.0    # run() 心跳落状态 json 周期


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [SIGCOL] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — 日志失败不中断采集
        pass


def _cfg() -> dict:
    try:
        import jarvis_config as jc
        return jc.load()
    except Exception:  # noqa: BLE001 — 配置层异常用内置默认，不拖垮采集
        return {}


# ────────────────────────── 运行时状态（模块级单例）──────────────────────────

_HIST: dict[tuple[str, str], deque] = {}     # (SYM, tf) → deque[bar dict]
_SEEDED: set[tuple[str, str]] = set()
# 降级种子登记：REST 不可用/缓存陈旧时纯 WS 起窗的 (币,TF) → 下次重播种时刻
_SEED_DEGRADED: dict[tuple[str, str], float] = {}
_ACTIVE_SYMBOLS: set[str] = set()
_ACTIVE_TFS: set[str] = set()
_Q: Optional[queue.Queue] = None
_WORKER: Optional[threading.Thread] = None
_STOP = threading.Event()
_INITED = False

# start() 时从配置快照（改配置需重启采集器进程生效）
_OPTS = {"backfill_bars": 288, "basis": True, "tape": True,
         "queue_max": 4096, "ledger_retention_days": 45}

_STATS: dict = {"started_at": None, "events_seen": 0, "events_dropped": 0,
                "bars_ws": 0, "bars_backfill": 0, "recorded": 0, "changed": 0,
                "skipped_state_fresher": 0, "seed_fails": 0, "seed_degraded": 0,
                "stale_events": 0, "gaps_detected": 0, "gap_bars_lost": 0,
                "bars_deferred": 0, "worker_errors": 0, "record_fails": 0}

_BASIS_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_BACKFILL_FAIL: dict[tuple[str, str], float] = {}
_LAST_REST_TS = 0.0
_LAST_LEDGER_PRUNE = 0.0


# ────────────────────────── 采集台账（覆盖率报表数据源）──────────────────────────

def _conn():
    os.makedirs(DB_DIR, exist_ok=True)
    return jdb.connect(DB_PATH)


def init_db() -> None:
    global _INITED
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_collect_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                symbol       TEXT NOT NULL,
                tf           TEXT NOT NULL,
                bar_open_ms  INTEGER NOT NULL,
                bar_close_ms INTEGER NOT NULL,
                source       TEXT NOT NULL,
                n_signals    INTEGER,
                n_changed    INTEGER,
                lag_ms       INTEGER,
                note         TEXT,
                UNIQUE (symbol, tf, bar_open_ms)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scl_close "
            "ON signal_collect_log(bar_close_ms)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scl_ts ON signal_collect_log(ts)"
        )
    _INITED = True


def _ensure_init() -> None:
    if not _INITED:
        init_db()


def ledger_record(symbol: str, tf: str, bar_open_ms: int, bar_close_ms: int,
                  source: str, n_signals: int | None = None,
                  n_changed: int | None = None, lag_ms: int | None = None,
                  note: str | None = None) -> bool:
    """记一根已处理 bar。UNIQUE 幂等 + 占位升级语义：

    同一根 bar 已有「真实处理行」（n_signals 非空）时任何重写都被拒绝；
    只有占位行（gap_unfilled 等 n_signals 为空）允许被后来的真实处理升级——
    缓期 bar 回补成功后台账自动转正，覆盖率口径不失真。失败静默 False。
    """
    try:
        _ensure_init()
        with _conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO signal_collect_log
                  (ts, symbol, tf, bar_open_ms, bar_close_ms, source,
                   n_signals, n_changed, lag_ms, note)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (symbol, tf, bar_open_ms) DO UPDATE SET
                  ts=excluded.ts, source=excluded.source,
                  n_signals=excluded.n_signals, n_changed=excluded.n_changed,
                  lag_ms=excluded.lag_ms, note=excluded.note
                WHERE signal_collect_log.n_signals IS NULL
                  AND excluded.n_signals IS NOT NULL
                """,
                (time.time(), (symbol or "").upper(), tf, int(bar_open_ms),
                 int(bar_close_ms), source, n_signals, n_changed, lag_ms, note))
            return bool(cur.rowcount and cur.rowcount > 0)
    except Exception as exc:  # noqa: BLE001 — 台账失败绝不拖垮采集主链路
        _log(f"台账写入失败（忽略继续）: {exc!r}")
        return False


def _ledger_last_open(symbol: str, tf: str) -> Optional[int]:
    """该 (币,TF) 台账里最新的 bar_open_ms；无记录返回 None。"""
    try:
        _ensure_init()
        with _conn() as conn:
            row = conn.execute(
                "SELECT MAX(bar_open_ms) AS m FROM signal_collect_log "
                "WHERE symbol=? AND tf=?", ((symbol or "").upper(), tf)).fetchone()
            return int(row["m"]) if row and row["m"] is not None else None
    except Exception:  # noqa: BLE001
        return None


def ledger_prune(retention_days: int | None = None) -> int:
    """删保留期外台账行；返回删除数，失败 0。"""
    try:
        _ensure_init()
        days = int(retention_days if retention_days is not None
                   else _OPTS["ledger_retention_days"])
        cutoff = time.time() - max(1, days) * 86400.0
        with _conn() as conn:
            cur = conn.execute("DELETE FROM signal_collect_log WHERE ts < ?",
                               (cutoff,))
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except Exception as exc:  # noqa: BLE001
        _log(f"台账清理失败（忽略）: {exc!r}")
        return 0


# ────────────────────────── 纯函数（离线可测）──────────────────────────

def parse_tfs(raw) -> list[str]:
    """配置 sigcol_tfs（逗号分隔/列表）→ 合法 TF 列表；无合法项回默认集。"""
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        items = []
    out = [x for x in dict.fromkeys(items) if x in TF_MS]
    return out or list(DEFAULT_TFS)


def closed_bar_from_kline(data: dict) -> Optional[tuple[str, dict]]:
    """kline 流消息 data → (tf, bar)；进行中 bar / 字段缺失 / 未知周期 → None。

    bar 形状与 jts.fetch_klines_df 行完全一致：
    {"time": open_ms, "open","high","low","close","volume"}。
    """
    try:
        k = data.get("k") or {}
        if not k.get("x"):
            return None
        tf = str(k.get("i") or "")
        if tf not in TF_MS:
            return None
        bar = {"time": int(k["t"]),
               "open": float(k["o"]), "high": float(k["h"]),
               "low": float(k["l"]), "close": float(k["c"]),
               "volume": float(k["v"])}
        return tf, bar
    except (KeyError, TypeError, ValueError):
        return None


def missing_opens(last_open_ms: int, new_open_ms: int, tf_ms: int,
                  cap: int) -> tuple[list[int], int]:
    """缺口内部 bar 的 open_ms 序列（旧→新，不含两端）。

    超 cap 只保留最新 cap 根。Returns (opens, 被截断丢弃的根数)。
    """
    n = (new_open_ms - last_open_ms) // tf_ms - 1
    if n <= 0:
        return [], 0
    opens = [last_open_ms + tf_ms * (i + 1) for i in range(n)]
    if cap <= 0:
        return [], len(opens)
    if len(opens) > cap:
        return opens[-cap:], len(opens) - cap
    return opens, 0


def _df_from_hist(hist: deque):
    """内存史 → 与 jts.fetch_klines_df 同形 DataFrame（列序/类型一致）。"""
    import pandas as pd
    return pd.DataFrame(list(hist),
                        columns=["time", "open", "high", "low", "close", "volume"])


# ────────────────────────── WS 回调（WS 线程内，必须 O(1)）──────────────────────────

def _on_kline(sym: str, data: dict) -> None:
    try:
        parsed = closed_bar_from_kline(data)
        if parsed is None:
            return
        tf, bar = parsed
        if tf not in _ACTIVE_TFS or sym not in _ACTIVE_SYMBOLS:
            return
        _STATS["events_seen"] += 1
        q = _Q
        if q is None:
            return
        try:
            q.put_nowait((sym, tf, bar))
        except queue.Full:
            _STATS["events_dropped"] += 1
    except Exception:  # noqa: BLE001 — 回调异常绝不拖垮 WS 数据流
        _STATS["worker_errors"] += 1


# ────────────────────────── REST（仅种子/回补/基差，全走共享预算）──────────────────────────

_LAST_BAN_LOG = 0.0


def _rest_ban_remaining() -> float:
    """fapi 封禁剩余秒数（未封禁 0）。封禁期种子/回补显式静默——只走 WS。

    jarvis_crypto_data._get 在封禁期本就短路不出网（回缓存/报错），此处提前
    拦截是为了：不做无谓的陈旧缓存空转、冷却直接对齐封禁到期、日志可审计。
    """
    try:
        import jarvis_crypto_data as jcd
        return max(0.0, jarvis_net.banned_until(jcd.FAPI) - time.time())
    except Exception:  # noqa: BLE001 — 查不到按未封禁处理（_get 层仍有短路兜底）
        return 0.0


def _paced_fetch_df(sym: str, tf: str, limit: int):
    """fetch_klines_df + 进程内最小间隔。只认已收盘 bar。失败/封禁期返回 None。"""
    global _LAST_REST_TS, _LAST_BAN_LOG
    ban = _rest_ban_remaining()
    if ban > 0:
        now = time.time()
        if now - _LAST_BAN_LOG > 600:
            _LAST_BAN_LOG = now
            _log(f"⛔ fapi 封禁剩余 {ban / 60:.0f} 分钟：REST 静默（只走 WS），"
                 "解禁后自动重播种/回补")
        return None
    wait = REST_MIN_INTERVAL_S - (time.time() - _LAST_REST_TS)
    if wait > 0:
        time.sleep(wait)
    _LAST_REST_TS = time.time()
    try:
        import jarvis_twelve_systems as jts
        return jts.fetch_klines_df(sym, tf, limit, drop_unclosed=True)
    except Exception as exc:  # noqa: BLE001 — 取数失败调用方降级
        _log(f"{sym} {tf} K线拉取异常: {exc!r}")
        return None


def _basis(sym: str) -> Optional[dict]:
    """期现基差（套利腿）。sigcol_basis 关 / 取数失败 → None（调用方剔除 arbitrage）。"""
    if not _OPTS["basis"]:
        return None
    now = time.time()
    hit = _BASIS_CACHE.get(sym)
    if hit and now - hit[0] < BASIS_TTL_S:
        return hit[1]
    data: Optional[dict] = None
    try:
        import jarvis_crypto_data as jcd
        data = jcd.fetch_basis_series(sym) or None
    except Exception:  # noqa: BLE001 — 基差失败降级，不硬造信号
        data = None
    _BASIS_CACHE[sym] = (now, data)
    return data


# ────────────────────────── 信号计算 + 落库 ──────────────────────────

def _state_max_ts(sym: str, tf: str) -> Optional[float]:
    """twelve_signal_state 里该 (币,TF) 的最大 updated_ts（新鲜度守卫用）。"""
    try:
        import jarvis_signal_history as jsh
        st = jsh.state(sym, tf)
        vals = [float(r["updated_ts"]) for r in (st.get("rows") or [])
                if r.get("updated_ts")]
        return max(vals) if vals else None
    except Exception:  # noqa: BLE001 — 查不到按无状态处理（放行写入）
        return None


def _compute_and_record(sym: str, tf: str, bar: dict, source: str) -> str:
    """一根已收盘 bar → run_all → (守卫通过时) record_batch → 台账。

    Returns 处理结果标签："ok" / "no_change" / note 值。永不抛出由调用方保证
    （worker 外层兜底），此处只保证台账与统计一致。
    """
    bar_close_ms = int(bar["time"]) + TF_MS[tf]
    lag_ms = max(0, int(time.time() * 1000) - bar_close_ms)
    note: Optional[str] = None
    n_signals: Optional[int] = None
    n_changed: Optional[int] = None

    hist = _HIST.get((sym, tf))
    # 回放旧 bar 时史里可能已有更新的 bar：只取 ≤ 当前 bar 的切片，
    # 确保信号计算绝不偷看未来（正常路径末根=当前 bar，切片零损耗）。
    rows = ([r for r in hist if int(r["time"]) <= int(bar["time"])]
            if hist is not None else [])
    if len(rows) < MIN_BARS_FOR_SIGNALS:
        note = "insufficient_history"
    else:
        import jarvis_twelve_systems as jts
        basis = _basis(sym)
        signals = jts.run_all(_df_from_hist(rows), basis_data=basis)
        if basis is None:
            # 无基差时套利系统只会输出「数据不足」降级中性；剔除而非落库，
            # 防与 dashboard 的带基差方向信号来回翻转制造假变更。
            signals = [s for s in signals if s.get("system") != "arbitrage"]
        n_signals = len(signals)
        bar_close_ts = bar_close_ms / 1000.0
        smax = _state_max_ts(sym, tf)
        if smax is not None and bar_close_ts <= smax:
            note = "state_fresher"
            _STATS["skipped_state_fresher"] += 1
        else:
            import jarvis_signal_history as jsh
            meta = jsh.record_batch(sym, tf, signals,
                                    price=float(bar["close"]), now=bar_close_ts)
            if not meta:
                note = "record_failed"
                _STATS["record_fails"] += 1
            else:
                n_changed = sum(1 for m in meta.values()
                                if m.get("changed_at") == bar_close_ts)
                _STATS["recorded"] += 1
                _STATS["changed"] += n_changed
    ledger_record(sym, tf, int(bar["time"]), bar_close_ms, source,
                  n_signals=n_signals, n_changed=n_changed,
                  lag_ms=lag_ms, note=note)
    _STATS["bars_backfill" if source == "backfill" else "bars_ws"] += 1
    return note or ("ok" if n_changed else "no_change")


# ────────────────────────── 种子 / 缺口回补 ──────────────────────────

def _df_rows(df) -> list[dict]:
    """fetch_klines_df 结果 → 归一 bar 行列表（空/None → []）。"""
    if df is None or getattr(df, "empty", True):
        return []
    return [{"time": int(r["time"]), "open": float(r["open"]),
             "high": float(r["high"]), "low": float(r["low"]),
             "close": float(r["close"]), "volume": float(r["volume"])}
            for r in df.to_dict("records")]


def _seed_pair(sym: str, tf: str, event_open_ms: int) -> bool:
    """首次事件（或降级重试）拉 300 根史建内存窗。Returns True=全窗种子。

    新鲜度检查：种子最新根必须贴到事件根或其前一根——REST 被封禁时
    jarvis_crypto_data 会回退磁盘缓存，可能给出数天前的陈旧窗（2026-08-13
    实测 BTCUSDT 5m 缓存止于 08-05），拿它拼今天的 WS bar = 断层窗算指标。
    不新鲜 → 纯 WS 降级起窗（史攒够 30 根前如实记 insufficient_history），
    登记 _SEED_DEGRADED 由 _maybe_reseed 周期重试整窗重播种。
    """
    key = (sym, tf)
    tfms = TF_MS[tf]
    rows = _df_rows(_paced_fetch_df(sym, tf, SEED_LIMIT))
    fresh = bool(rows) and rows[-1]["time"] >= event_open_ms - tfms
    if not fresh:
        if key not in _SEEDED:
            _HIST[key] = deque(maxlen=HIST_MAXLEN)
            _SEEDED.add(key)
        # 冷却对齐封禁到期：封禁期内不再空转重试，解禁后 60s 内自动重播种
        retry_after = max(RESEED_COOLDOWN_S, _rest_ban_remaining() + 60.0)
        _SEED_DEGRADED[key] = time.time() + retry_after
        _STATS["seed_fails" if not rows else "seed_degraded"] += 1
        _log(f"⚠ {sym} {tf} 种子不可用/陈旧"
             + (f"（缓存止于 {time.strftime('%m-%d %H:%M', time.localtime(rows[-1]['time'] / 1000))}）"
                if rows else "（取数失败/封禁静默）")
             + f"，纯 WS 降级起窗，{retry_after / 60:.0f} 分钟后重试整窗")
        return False
    hist = _HIST.get(key)
    if hist is None:
        hist = deque(maxlen=HIST_MAXLEN)
        _HIST[key] = hist
    hist.clear()
    hist.extend(rows)
    _SEEDED.add(key)
    _SEED_DEGRADED.pop(key, None)
    _log(f"{sym} {tf} 种子 {len(hist)} 根"
         f"（{time.strftime('%m-%d %H:%M', time.localtime(rows[0]['time'] / 1000))}"
         f" → {time.strftime('%m-%d %H:%M', time.localtime(rows[-1]['time'] / 1000))} 开盘）")

    # 断点续采：台账有历史（=进程重启）时，把停机窗口内、种子里已有的 bar
    # 按时间序回放（state 新鲜度守卫防回退；上限 sigcol_backfill_bars）。
    last = _ledger_last_open(sym, tf)
    if last is not None:
        cap = int(_OPTS["backfill_bars"])
        pending = [b for b in rows if b["time"] > last]
        if cap <= 0:
            pending = []
        elif len(pending) > cap:
            _STATS["gap_bars_lost"] += len(pending) - cap
            pending = pending[-cap:]
        for b in pending:
            _compute_and_record(sym, tf, b, "backfill")
        if pending:
            _log(f"{sym} {tf} 断点续采回放 {len(pending)} 根（台账断点之后）")
    return True


def _maybe_reseed(sym: str, tf: str, event_open_ms: int) -> None:
    """降级起窗的 (币,TF) 周期性重试整窗种子；REST 恢复后自动补回 300 根全窗。"""
    key = (sym, tf)
    due = _SEED_DEGRADED.get(key)
    if due is None or time.time() < due:
        return
    if not _seed_pair(sym, tf, event_open_ms):
        return
    _log(f"{sym} {tf} 重播种成功，恢复全窗计算")


def _backfill_gap(sym: str, tf: str, last_open_ms: int, new_open_ms: int) -> bool:
    """WS 断线缺口回补：一次 REST 拉最新窗，缺口 bar 按时间序回放。

    Returns True=修复成功（内存史与事件根连续），False=修复失败（取数失败/
    数据陈旧/冷却中/回补关闭——调用方缓期本根，绝不在断层窗上算信号）。
    新鲜度检查同种子：拉到的最新根必须贴到事件前一根，识破封禁期陈旧缓存。
    拿不全缺口头部（比单次拉取窗还老 / 超回补上限）时用整段连续新窗原地重建，
    宁可回放少也不在史里留洞。
    """
    key = (sym, tf)
    tfms = TF_MS[tf]
    _STATS["gaps_detected"] += 1
    n_missing = (new_open_ms - last_open_ms) // tfms - 1
    cap = int(_OPTS["backfill_bars"])
    if cap <= 0:
        _STATS["gap_bars_lost"] += max(0, n_missing)
        return False
    if time.time() < _BACKFILL_FAIL.get(key, 0.0):
        return False
    lim = min(500, max(50, n_missing + 3))
    recs = _df_rows(_paced_fetch_df(sym, tf, lim))
    if not recs or recs[-1]["time"] < new_open_ms - tfms:
        # 冷却对齐封禁到期：封禁期内不空转，解禁后 30s 内恢复回补
        cool = max(BACKFILL_RETRY_COOLDOWN_S, _rest_ban_remaining() + 30.0)
        _BACKFILL_FAIL[key] = time.time() + cool
        _log(f"{sym} {tf} 缺口回补失败（缺 {n_missing} 根，"
             + ("取数失败/封禁静默" if not recs else
                f"数据陈旧止于 {time.strftime('%m-%d %H:%M', time.localtime(recs[-1]['time'] / 1000))}")
             + f"，{cool / 60:.0f} 分钟冷却）")
        return False
    rows = [r for r in recs if last_open_ms < r["time"] < new_open_ms]
    lost = n_missing - len(rows)
    if len(rows) > cap:
        lost += len(rows) - cap
        rows = rows[-cap:]
    hist = _HIST[key]
    if lost == 0 and rows and rows[0]["time"] == last_open_ms + tfms:
        for b in rows:
            hist.append(b)
            _compute_and_record(sym, tf, b, "backfill")
    else:
        # 缺口头部拿不到：整段连续新窗原地重建（clear+extend 保持外部引用有效）
        fresh = [r for r in recs if r["time"] < new_open_ms]
        hist.clear()
        hist.extend(fresh)
        for b in rows:
            _compute_and_record(sym, tf, b, "backfill")
    if lost > 0:
        _STATS["gap_bars_lost"] += lost
    _log(f"{sym} {tf} 缺口回补：缺 {n_missing} 根，回放 {len(rows)} 根"
         + (f"，丢 {lost} 根（超窗/超上限）" if lost > 0 else ""))
    return True


# ────────────────────────── 事件处理（工作线程内）──────────────────────────

def _process_event(sym: str, tf: str, bar: dict) -> None:
    key = (sym, tf)
    t = int(bar["time"])
    tfms = TF_MS[tf]
    if key not in _SEEDED:
        _seed_pair(sym, tf, t)   # 失败也已降级起窗（纯 WS），继续处理本根
    else:
        _maybe_reseed(sym, tf, t)
    hist = _HIST[key]
    last_open = int(hist[-1]["time"]) if hist else None
    if last_open is not None and t <= last_open:
        if t == last_open:
            hist[-1] = bar  # WS 收盘帧权威覆盖种子同根（守卫+台账幂等自然去重）
            _compute_and_record(sym, tf, bar, "ws")
        else:
            _STATS["stale_events"] += 1
        return
    if last_open is not None and t > last_open + tfms:
        if not _backfill_gap(sym, tf, last_open, t):
            n_missing = (t - last_open) // tfms - 1
            if n_missing > int(_OPTS["backfill_bars"]):
                # 缺口大过回补上限且回补不可用（整夜休眠 + REST 封禁等）：
                # 丢弃断层旧史改纯 WS 重建窗，登记降级待重播种；
                # 史攒够 30 根前台账如实记 insufficient_history。
                hist.clear()
                _SEED_DEGRADED.setdefault(key, time.time() + RESEED_COOLDOWN_S)
                _STATS["gap_bars_lost"] += n_missing
                _log(f"{sym} {tf} 缺口 {n_missing} 根超上限且回补不可用，"
                     "弃断层旧史改纯 WS 重建窗")
            else:
                # 小缺口回补失败：本根缓期（不入史不计算，台账记占位可升级），
                # 下次事件重试回补——绝不把断层窗喂给指标。
                _STATS["bars_deferred"] += 1
                ledger_record(sym, tf, t, t + tfms, "ws", note="gap_unfilled",
                              lag_ms=max(0, int(time.time() * 1000) - (t + tfms)))
                return
    hist.append(bar)
    _compute_and_record(sym, tf, bar, "ws")


def _maybe_prune_ledger() -> None:
    global _LAST_LEDGER_PRUNE
    now = time.time()
    if now - _LAST_LEDGER_PRUNE > LEDGER_PRUNE_INTERVAL_S:
        _LAST_LEDGER_PRUNE = now
        removed = ledger_prune()
        if removed:
            _log(f"台账保留期清理 {removed} 行")


def _worker_loop() -> None:
    while not _STOP.is_set():
        try:
            item = _Q.get(timeout=1.0)
        except queue.Empty:
            _maybe_prune_ledger()
            continue
        try:
            _process_event(*item)
        except Exception as exc:  # noqa: BLE001 — 单事件失败绝不拖垮采集循环
            _STATS["worker_errors"] += 1
            _log(f"事件处理异常（忽略继续）: {exc!r}")
        _maybe_prune_ledger()


# ────────────────────────── 启停 / 健康 ──────────────────────────

def start(symbols: list[str] | None = None, tfs: list[str] | None = None) -> bool:
    """启动采集器（幂等）。独立进程模式：本进程只订 kline(+可选 aggTrade)。"""
    global _Q, _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return True
    cfg = _cfg()
    if not bool(cfg.get("sigcol_enabled", True)):
        _log("采集器未启用（sigcol_enabled=false），退出")
        return False
    # 标的优先级：显式传参 > sigcol_symbols（与 watchlist 解耦扩容）> watchlist
    cfg_syms = [s.strip() for s in str(cfg.get("sigcol_symbols") or "").split(",")
                if s.strip()]
    syms = [str(s).upper()
            for s in (symbols or cfg_syms or cfg.get("watchlist") or ["BTCUSDT"])
            if str(s).strip()]
    tf_list = parse_tfs(tfs if tfs is not None else cfg.get("sigcol_tfs"))
    _ACTIVE_SYMBOLS.clear()
    _ACTIVE_SYMBOLS.update(syms)
    _ACTIVE_TFS.clear()
    _ACTIVE_TFS.update(tf_list)
    _OPTS.update(
        backfill_bars=int(cfg.get("sigcol_backfill_bars", 288) or 0),
        basis=bool(cfg.get("sigcol_basis", True)),
        tape=bool(cfg.get("sigcol_tape", True)),
        queue_max=int(cfg.get("sigcol_queue_max", 4096) or 4096),
        ledger_retention_days=int(cfg.get("sigcol_ledger_retention_days", 45) or 45),
    )
    init_db()
    _Q = queue.Queue(maxsize=max(256, _OPTS["queue_max"]))
    _STOP.clear()
    _STATS["started_at"] = time.time()
    _WORKER = threading.Thread(target=_worker_loop, daemon=True,
                               name="jarvis-signal-collector")
    _WORKER.start()

    import jarvis_ws_stream as jws
    jws.register_callback("kline", _on_kline)
    stream_types = {"kline"}
    if _OPTS["tape"]:
        stream_types.add("aggTrade")
        try:
            import jarvis_tape_classify as jtc
            if jtc.register() and jtc.start_persist():
                _log("tape 分钟 bar 落库链已挂载（aggTrade→tape_minute_bars，30s flush）")
            else:
                _log("⚠ tape 链挂载失败（真 bar 覆盖仍依赖 dashboard 进程）")
        except Exception as exc:  # noqa: BLE001 — tape 链失败不影响信号采集主链路
            _log(f"⚠ tape 链异常（忽略）: {exc!r}")
    jws.start(syms, kline_intervals=tf_list, stream_types=stream_types)
    _log(f"采集器已启动：{len(syms)} 币 × {tf_list}（只认已收盘 bar；"
         f"回补上限 {_OPTS['backfill_bars']} 根；基差 {'开' if _OPTS['basis'] else '关'}；"
         f"tape {'开' if _OPTS['tape'] else '关'}）")
    return True


def stop(timeout: float = 5.0) -> None:
    """停止采集器与本进程 WS（独立进程模式下本进程独占 WS）。"""
    _STOP.set()
    w = _WORKER
    if w is not None and w.is_alive():
        w.join(timeout=timeout)
    try:
        import jarvis_ws_stream as jws
        jws.stop()
    except Exception:  # noqa: BLE001
        pass
    _log("采集器已停止")


def health() -> dict:
    """采集器健康度（run() 心跳落盘的同一份数据）。"""
    out = dict(_STATS)
    out["queue_depth"] = _Q.qsize() if _Q is not None else None
    out["pairs_seeded"] = len(_SEEDED)
    out["pairs_total"] = len(_ACTIVE_SYMBOLS) * len(_ACTIVE_TFS)
    out["symbols"] = sorted(_ACTIVE_SYMBOLS)
    out["tfs"] = sorted(_ACTIVE_TFS, key=lambda x: TF_MS.get(x, 0))
    try:
        import jarvis_ws_stream as jws
        h = jws.health()
        out["ws"] = {"connected": h.get("connected"), "endpoint": h.get("endpoint"),
                     "market": h.get("market"), "reconnects": h.get("reconnects"),
                     "kline_rate_per_min": (h.get("streams") or {}).get("kline", {}).get("rate_per_min")}
    except Exception:  # noqa: BLE001
        out["ws"] = None
    return out


# ────────────────────────── 覆盖率自检报表（只读）──────────────────────────

def _hour_skew(rates: dict[int, float]) -> float:
    """24 小时桶覆盖率的偏斜倍数 max/min；空桶或 min=0 → inf。"""
    vals = [rates[h] for h in rates if rates[h] is not None]
    if len(vals) < 2:
        return float("inf")
    lo, hi = min(vals), max(vals)
    return (hi / lo) if lo > 0 else float("inf")


def coverage_report(days: float = 7.0, symbols: list[str] | None = None,
                    tfs: list[str] | None = None,
                    now: float | None = None) -> dict:
    """T10 覆盖率自检报表（只读，不出网）。

    四个视角：
      ledger        采集台账：已处理 bar / 应有 bar（按 (币,TF) + 按 UTC 小时(5m)）
      tape          真 bar：tape_minute_bars 分钟覆盖率按 UTC 小时（T9 P2 价格源）
      changes_hist  twelve_signal_changes 记录条数按 UTC 小时分布（历史基线口径）
      acceptance    验收判定：连续 7 天台账覆盖 ≥95% 且 5m 小时偏斜 <1.2×
    覆盖口径：台账行 n_signals 非空才算「已采集」（种子失败/史不足不算）。
    """
    _ensure_init()
    t1 = float(now if now is not None else time.time())
    t0 = t1 - float(days) * 86400.0
    cfg = _cfg()
    cfg_syms = [s.strip() for s in str(cfg.get("sigcol_symbols") or "").split(",")
                if s.strip()]
    syms = [str(s).upper()
            for s in (symbols or cfg_syms or cfg.get("watchlist") or [])] or None
    tf_list = parse_tfs(tfs if tfs is not None else cfg.get("sigcol_tfs"))
    rep: dict = {"generated_at": t1,
                 "window": {"days_requested": float(days), "start_ts": t0, "end_ts": t1}}

    with _conn() as conn:
        # ── 台账窗口有效期（首行之前不算「应有」，避免冷启动被算成缺口）──
        row = conn.execute(
            "SELECT MIN(bar_close_ms) AS m FROM signal_collect_log "
            "WHERE bar_close_ms > ?", (int(t0 * 1000),)).fetchone()
        first_ms = int(row["m"]) if row and row["m"] is not None else None
        eff_t0 = max(t0, (first_ms / 1000.0)) if first_ms else t1
        eff_days = max(0.0, (t1 - eff_t0) / 86400.0)
        rep["window"]["days_effective"] = round(eff_days, 3)

        # ── ledger：按 (币,TF) 覆盖率 ──
        sym_filter = ""
        args_base: list = []
        if syms:
            sym_filter = " AND symbol IN (%s)" % ",".join("?" for _ in syms)
            args_base = list(syms)
        by_pair: dict[str, dict] = {}
        tot_expected = tot_covered = 0
        if eff_days > 0:
            eff_t0_ms, t1_ms = int(eff_t0 * 1000), int(t1 * 1000)
            cur = conn.execute(
                "SELECT symbol, tf, COUNT(*) AS n FROM signal_collect_log "
                "WHERE bar_close_ms > ? AND bar_close_ms <= ? "
                "AND n_signals IS NOT NULL" + sym_filter +
                " GROUP BY symbol, tf",
                (eff_t0_ms, t1_ms, *args_base))
            counts = {(r["symbol"], r["tf"]): int(r["n"]) for r in cur.fetchall()}
            pair_syms = syms or sorted({s for s, _ in counts})
            for s in pair_syms:
                for tf in tf_list:
                    tfms = TF_MS[tf]
                    expected = t1_ms // tfms - eff_t0_ms // tfms
                    covered = min(counts.get((s, tf), 0), expected)
                    if expected <= 0:
                        continue
                    tot_expected += expected
                    tot_covered += covered
                    by_pair[f"{s}:{tf}"] = {"expected": expected, "covered": covered,
                                            "rate": round(covered / expected, 4)}
        rep["ledger"] = {
            "overall": {"expected": tot_expected, "covered": tot_covered,
                        "rate": round(tot_covered / tot_expected, 4) if tot_expected else None},
            "by_pair": by_pair,
        }

        # ── ledger：5m 按 UTC 小时覆盖率（偏斜口径；5m=12 根/小时/币 最敏感）──
        by_hour_5m: dict[int, Optional[float]] = {}
        if eff_days > 0 and "5m" in tf_list:
            n_sym = len(syms) if syms else max(
                1, len({p.split(":")[0] for p in by_pair}))
            cur = conn.execute(
                "SELECT (bar_close_ms/3600000)%24 AS h, COUNT(*) AS n "
                "FROM signal_collect_log WHERE tf='5m' "
                "AND bar_close_ms > ? AND bar_close_ms <= ? "
                "AND n_signals IS NOT NULL" + sym_filter + " GROUP BY h",
                (int(eff_t0 * 1000), int(t1 * 1000), *args_base))
            got = {int(r["h"]): int(r["n"]) for r in cur.fetchall()}
            exp_per_hour = eff_days * 12.0 * n_sym
            for h in range(24):
                by_hour_5m[h] = (round(min(1.0, got.get(h, 0) / exp_per_hour), 4)
                                 if exp_per_hour > 0 else None)
        skew_5m = _hour_skew({h: v for h, v in by_hour_5m.items() if v is not None})
        rep["ledger"]["by_hour_5m"] = by_hour_5m
        rep["ledger"]["skew_5m"] = (round(skew_5m, 2)
                                    if skew_5m != float("inf") else None)

        # ── tape：真 bar 分钟覆盖率按 UTC 小时（T9 P2 价格源健康度）──
        tape: dict = {"overall_rate": None, "by_hour": {}, "skew": None,
                      "per_symbol": {}}
        try:
            start_min, end_min = int(t0 // 60), int(t1 // 60)
            total_min = max(0, end_min - start_min)
            cur = conn.execute(
                "SELECT symbol, COUNT(*) AS n FROM tape_minute_bars "
                "WHERE minute >= ? AND minute < ?" + sym_filter +
                " GROUP BY symbol",
                (start_min, end_min, *args_base))
            per_sym_n = {r["symbol"]: int(r["n"]) for r in cur.fetchall()}
            tape_syms = syms or sorted(per_sym_n)
            if tape_syms and total_min > 0:
                for s in tape_syms:
                    tape["per_symbol"][s] = round(
                        min(1.0, per_sym_n.get(s, 0) / total_min), 4)
                cur = conn.execute(
                    "SELECT (minute/60)%24 AS h, COUNT(*) AS n "
                    "FROM tape_minute_bars WHERE minute >= ? AND minute < ?"
                    + sym_filter + " GROUP BY h",
                    (start_min, end_min, *args_base))
                got_h = {int(r["h"]): int(r["n"]) for r in cur.fetchall()}
                exp_h = (total_min / 24.0) * len(tape_syms)
                by_hour = {h: round(min(1.0, got_h.get(h, 0) / exp_h), 4)
                           for h in range(24)} if exp_h > 0 else {}
                tape["by_hour"] = by_hour
                sk = _hour_skew(by_hour)
                tape["skew"] = round(sk, 2) if sk != float("inf") else None
                tape["overall_rate"] = round(
                    min(1.0, sum(per_sym_n.get(s, 0) for s in tape_syms)
                        / (total_min * len(tape_syms))), 4)
        except Exception as exc:  # noqa: BLE001 — tape 表缺失/后端差异不挡报表主体
            tape["error"] = repr(exc)[:200]
        rep["tape"] = tape

        # ── 历史基线：twelve_signal_changes 记录条数按 UTC 小时 ──
        hist: dict = {"per_hour_counts": {}, "skew": None, "total": 0}
        try:
            cur = conn.execute(
                "SELECT CAST(ts/3600 AS INTEGER)%24 AS h, COUNT(*) AS n "
                "FROM twelve_signal_changes WHERE ts >= ? AND ts < ?"
                + sym_filter + " GROUP BY h",
                (t0, t1, *args_base))
            got_c = {int(r["h"]): int(r["n"]) for r in cur.fetchall()}
            hist["per_hour_counts"] = {h: got_c.get(h, 0) for h in range(24)}
            hist["total"] = sum(got_c.values())
            nz = {h: float(v) for h, v in got_c.items() if v > 0}
            if len(nz) >= 2:
                sk = _hour_skew(nz)
                hist["skew"] = round(sk, 2) if sk != float("inf") else None
            if len(got_c) < 24:
                hist["hours_with_zero_records"] = 24 - len(got_c)
        except Exception as exc:  # noqa: BLE001
            hist["error"] = repr(exc)[:200]
        rep["changes_hist"] = hist

    # ── 验收判定（开发计划 §T10：连续 7 天 ≥95%、小时偏斜 <1.2×）──
    rate = rep["ledger"]["overall"]["rate"]
    cov_ok = rate is not None and rate >= 0.95
    skew_ok = skew_5m < 1.2
    window_ok = rep["window"]["days_effective"] >= 7.0
    notes = []
    if not window_ok:
        notes.append(f"台账有效窗口仅 {rep['window']['days_effective']:.2f} 天，"
                     "不足 7 天验收窗口（采集器需继续常驻运行）")
    if rate is None:
        notes.append("台账窗口内无数据（采集器尚未运行？）")
    if tape.get("overall_rate") is not None and tape["overall_rate"] < 0.95:
        notes.append(f"真 bar 分钟覆盖 {tape['overall_rate']:.1%} <95%："
                     "tape 无法回补历史，只能靠进程常驻改善（VPS 是治本项）")
    rep["acceptance"] = {"target": "连续 7 天覆盖率 ≥95% 且 5m 小时偏斜 <1.2×",
                         "coverage_ok": cov_ok, "skew_ok": skew_ok,
                         "window_ok": window_ok,
                         "pass": bool(cov_ok and skew_ok and window_ok),
                         "notes": notes}
    return rep


def _print_coverage(rep: dict) -> None:
    w = rep["window"]
    print("=" * 72)
    print(f"T10 采集覆盖率自检  窗口 {w['days_requested']:g} 天"
          f"（台账有效 {w.get('days_effective', 0):.2f} 天）")
    print("=" * 72)
    led = rep["ledger"]
    ov = led["overall"]
    if ov["rate"] is not None:
        print(f"\n【信号采集台账】应有 {ov['expected']:,} 根 / 已采 {ov['covered']:,} 根 "
              f"/ 覆盖率 {ov['rate']:.1%}")
        worst = sorted(led["by_pair"].items(), key=lambda kv: kv[1]["rate"])[:8]
        for k, v in worst:
            print(f"    {k:<16} {v['covered']:>6,}/{v['expected']:<6,} {v['rate']:.1%}")
        if led["skew_5m"] is not None:
            print(f"  5m 按 UTC 小时偏斜 {led['skew_5m']}×")
            for h in range(24):
                r = led["by_hour_5m"].get(h)
                if r is not None:
                    print(f"    {h:02d}h UTC {100 * r:>5.1f}% {'#' * int(40 * r)}")
    else:
        print("\n【信号采集台账】窗口内无数据（采集器尚未运行）")
    tape = rep["tape"]
    if tape.get("overall_rate") is not None:
        print(f"\n【真 bar 覆盖（tape_minute_bars，T9 P2 价格源）】"
              f"总体 {tape['overall_rate']:.1%}  偏斜 {tape.get('skew')}×")
        for h in range(24):
            r = tape["by_hour"].get(h)
            if r is not None:
                print(f"    {h:02d}h UTC {100 * r:>5.1f}% {'#' * int(40 * r)}")
        lows = sorted(tape["per_symbol"].items(), key=lambda kv: kv[1])[:5]
        print("  最差标的：" + "  ".join(f"{s} {r:.1%}" for s, r in lows))
    else:
        print("\n【真 bar 覆盖】tape_minute_bars 窗口内无数据"
              + (f"（{tape['error']}）" if tape.get("error") else ""))
    hist = rep["changes_hist"]
    if hist.get("total"):
        print(f"\n【历史信号记录分布（基线口径）】窗口内 {hist['total']:,} 条"
              f"  非零小时偏斜 {hist.get('skew')}×"
              + (f"  全零小时 {hist.get('hours_with_zero_records')} 个"
                 if hist.get("hours_with_zero_records") else ""))
    acc = rep["acceptance"]
    print(f"\n【验收】{acc['target']}")
    print(f"  覆盖率达标 {acc['coverage_ok']}  偏斜达标 {acc['skew_ok']}  "
          f"窗口足 7 天 {acc['window_ok']}  → {'✅ PASS' if acc['pass'] else '❌ 未达'}")
    for n in acc["notes"]:
        print(f"  ⚠ {n}")


# ────────────────────────── 常驻入口 ──────────────────────────

def run(symbols: list[str] | None = None, tfs: list[str] | None = None) -> int:
    """常驻采集主循环（launchd/systemd 入口）：心跳落状态 json，永不自杀。"""
    if not start(symbols=symbols, tfs=tfs):
        return 1
    try:
        import signal as _signal

        def _term(_sig, _frm):  # launchd unload / systemctl stop 发 SIGTERM
            raise KeyboardInterrupt
        _signal.signal(_signal.SIGTERM, _term)
    except Exception:  # noqa: BLE001 — 非主线程等场景装不上 handler 也不影响采集
        pass
    _log("进入常驻循环（Ctrl-C / SIGTERM 退出）")
    try:
        last_status = 0.0
        while True:
            time.sleep(5.0)
            now = time.time()
            if now - last_status >= STATUS_WRITE_INTERVAL_S:
                last_status = now
                h = health()
                h["ts"] = now
                try:
                    tmp = STATUS_PATH + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(h, f, ensure_ascii=False, indent=2)
                    os.replace(tmp, STATUS_PATH)
                except Exception:  # noqa: BLE001 — 状态落盘失败不影响采集
                    pass
                _log(f"心跳：事件 {h['events_seen']} / 已采 ws {h['bars_ws']} + 回补 "
                     f"{h['bars_backfill']} / 记录 {h['recorded']}（变更 {h['changed']}）"
                     f" / 守卫跳过 {h['skipped_state_fresher']} / 队列 {h['queue_depth']}"
                     f" / 种子 {h['pairs_seeded']}/{h['pairs_total']}"
                     f" / WS {'✓' if (h.get('ws') or {}).get('connected') else '✗'}")
    except KeyboardInterrupt:
        _log("收到退出信号")
    finally:
        stop()
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="贾维斯 T10 常驻信号采集器")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="常驻采集（WS 收盘 bar 事件驱动落库）")
    r.add_argument("--symbols", default=None, help="逗号分隔，缺省读配置 watchlist")
    r.add_argument("--tfs", default=None, help="逗号分隔，缺省读配置 sigcol_tfs")
    c = sub.add_parser("coverage", help="覆盖率自检报表（只读）")
    c.add_argument("--days", type=float, default=7.0)
    c.add_argument("--symbols", default=None)
    c.add_argument("--json", action="store_true", help="输出 JSON 而非人读报表")
    sub.add_parser("health", help="打印采集器健康度（常驻进程另有状态 json）")
    args = ap.parse_args()

    if args.cmd == "coverage":
        syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                if args.symbols else None)
        rep = coverage_report(days=args.days, symbols=syms)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            _print_coverage(rep)
        return 0
    if args.cmd == "health":
        print(json.dumps(health(), ensure_ascii=False, indent=2))
        return 0
    # run
    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if args.symbols else None)
    tfs = parse_tfs(args.tfs) if args.tfs else None
    return run(symbols=syms, tfs=tfs)


if __name__ == "__main__":
    raise SystemExit(main())
