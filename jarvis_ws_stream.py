#!/usr/bin/env python3
"""贾维斯 JARVIS — Binance 合约 WebSocket 实时数据地基（M2 s4-ws-base）。

接入币安 USDT 本位合约组合流（wss://fstream.binance.com/stream），四条流：
  kline      K 线增量（<sym>@kline_<iv>）
  aggTrade   逐笔归集成交（<sym>@aggTrade）——whale tape 依赖
  forceOrder 强平单（<sym>@forceOrder）——爆仓面板/清算校准依赖（可选落 SQLite）
  depth      盘口深度增量（<sym>@depth@<speed>）——幌骗检测依赖

架构（与 dashboard 既有后台线程模式一致，不占 uvicorn loop）：
  daemon 线程内跑独立 asyncio 事件循环 → aiohttp ws_connect（自动带本地代理）
  → 消息解析分发 → 每流每币种环形缓冲 + 已注册回调 + 健康度统计。

端点回退策略（2026-07-13 实测：大陆代理环境下合约域 fstream 常见
「握手成功但数据帧被代理分流规则丢弃」，现货域完全正常）：
  按顺序试 ①合约域经代理 ②合约域直连 ③现货域经代理 ④现货域直连；
  连接后 15s 内收到首帧才算策略生效，失效即切下一策略；成功策略被记住，
  重连时优先复用。现货回退模式下 kline/aggTrade/depth 平替可用（现货
  depth 固定 @100ms），forceOrder 无现货对应流 → health() 标注
  degraded_streams 供下游（爆仓面板等）判断降级。

下游消费接口（后续 whale tape / 爆仓面板 / 幌骗检测直接用）：
  latest(stream, symbol, n=None)   取环形缓冲最近 n 条（默认全部，list[dict]）
  register_callback(stream, fn)    注册回调 fn(symbol: str, data: dict)——在
                                   WS 线程内同步调用，必须轻量非阻塞
  force_orders_recent(...)         读强平单历史（SQLite）
  health()                         每流 last_msg_ts / 消息计数 / 速率 / 重连次数

可靠性：
  - 断线自动重连：指数退避 ws_reconnect_base_s × 2^n 封顶 ws_reconnect_max_s，
    连接成功后退避归零；重连即重建订阅 URL（组合流 URL 即订阅，天然重订阅）
  - 币安 24h 强制断开：连接寿命 > 23h 主动重连，避开被动断流窗口
  - aiohttp autoping 响应服务端 ping（币安 3 分钟 ping、10 分钟无 pong 断开）
  - 所有异常只记日志重连，永不拖垮宿主进程

配置（全部走 jarvis_config 配置中心，Settings UI 可改）：
  system.ws_enabled  data.ws_stream_* / ws_kline_interval / ws_depth_speed /
  ws_buffer_size / ws_reconnect_base_s / ws_reconnect_max_s / ws_force_order_persist

用法：
  python jarvis_ws_stream.py test --seconds 12   # 主网四流实测（打样本+计数）
  # 作为库：import jarvis_ws_stream as jws; jws.start(); jws.health()
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from collections import deque
from typing import Callable, Optional

import jarvis_net as _jnet

_jnet.ensure_proxy()  # 大陆网络：Binance 出网自动走本地代理

FUTURES_BASE = "wss://fstream.binance.com/stream"
SPOT_BASE = "wss://stream.binance.com:443/stream"
STREAM_TYPES = ("kline", "aggTrade", "forceOrder", "depth")
CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
FORCE_ORDER_DB = os.path.join(CONFIG_DIR, "jarvis_ws_force_orders.db")
LOG_PATH = os.path.join(CONFIG_DIR, "jarvis_ws_stream.log")

# 连接寿命上限：币安 24h 强断，23h 主动重连避开被动断流
MAX_CONN_AGE_S = 23 * 3600.0

# 首帧确认窗口：连接后该秒数内收不到任何数据帧 = 当前端点策略无效
# [任务H 方案4] 15→8s：坏端点更快出局（合约域经代理"握手成功但丢数据帧"场景，
# 8s 无帧已足够判死，整轮探测时间近乎减半）
FIRST_FRAME_TIMEOUT_S = 8.0

# 连接建立超时：[任务H 方案4] 12→6s——直连被墙/代理僵死场景更快轮换下一策略
SOCK_CONNECT_TIMEOUT_S = 6.0

# 端点策略表：(名称, base_url, 是否用代理, 是否合约域)
_ENDPOINT_PLANS: tuple[tuple[str, str, bool, bool], ...] = (
    ("futures+proxy", FUTURES_BASE, True, True),
    ("futures+direct", FUTURES_BASE, False, True),
    ("spot+proxy", SPOT_BASE, True, False),
    ("spot+direct", SPOT_BASE, False, False),
)


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [WS] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _cfg() -> dict:
    try:
        import jarvis_config as jc
        return jc.load()
    except Exception:  # noqa: BLE001 — 配置层异常用兜底默认，不拖垮数据流
        return {}


# ────────────────────────── 运行时状态（模块级单例）──────────────────────────

# 环形缓冲：{(stream_type, SYMBOL): deque[dict]}
_BUFFERS: dict[tuple[str, str], deque] = {}
_BUF_LOCK = threading.Lock()

# 健康度统计：{stream_type: {...}}
_STATS: dict[str, dict] = {
    t: {"last_msg_ts": None, "msg_count": 0, "rate_per_min": 0.0,
        "_rate_window": deque(maxlen=600)} for t in STREAM_TYPES
}
_META = {"running": False, "connected": False, "reconnects": 0,
         "connected_at": None, "started_at": None, "symbols": [],
         "url_streams": 0, "last_error": None,
         "endpoint": None, "market": None, "degraded_streams": []}

# 下游回调：{stream_type: [fn(symbol, data)]}
_CALLBACKS: dict[str, list[Callable[[str, dict], None]]] = {t: [] for t in STREAM_TYPES}

_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()
_FO_CONN: Optional[sqlite3.Connection] = None


# ────────────────────────── 下游消费接口 ──────────────────────────

def register_callback(stream_type: str, fn: Callable[[str, dict], None]) -> bool:
    """注册流回调 fn(symbol, data)。WS 线程内同步调用——必须轻量非阻塞。"""
    if stream_type not in STREAM_TYPES:
        return False
    _CALLBACKS[stream_type].append(fn)
    return True


def latest(stream_type: str, symbol: str, n: int | None = None) -> list[dict]:
    """取某流某币环形缓冲的最近 n 条（None=全部）。线程安全，返回拷贝。"""
    key = (stream_type, symbol.upper())
    with _BUF_LOCK:
        buf = _BUFFERS.get(key)
        if not buf:
            return []
        items = list(buf)
    return items[-n:] if n else items


def health() -> dict:
    """每流健康度 + 连接元信息（GET /api/ws/health 的数据源）。"""
    now = time.time()
    streams = {}
    for t in STREAM_TYPES:
        s = _STATS[t]
        win = s["_rate_window"]
        rate = sum(1 for ts in win if now - ts <= 60.0)
        age = round(now - s["last_msg_ts"], 1) if s["last_msg_ts"] else None
        streams[t] = {"last_msg_ts": s["last_msg_ts"], "last_msg_age_s": age,
                      "msg_count": s["msg_count"], "rate_per_min": rate}
    with _BUF_LOCK:
        buffered = {f"{t}:{sym}": len(buf) for (t, sym), buf in _BUFFERS.items()}
    return {"running": _META["running"], "connected": _META["connected"],
            "reconnects": _META["reconnects"], "connected_at": _META["connected_at"],
            "started_at": _META["started_at"], "symbols": _META["symbols"],
            "stream_count": _META["url_streams"], "last_error": _META["last_error"],
            "endpoint": _META["endpoint"], "market": _META["market"],
            "degraded_streams": _META["degraded_streams"],
            "streams": streams, "buffered": buffered}


# ────────────────────────── forceOrder 落库 ──────────────────────────

def _fo_conn(db_path: str | None = None) -> sqlite3.Connection:
    global _FO_CONN
    if _FO_CONN is None or db_path:
        p = db_path or FORCE_ORDER_DB
        os.makedirs(os.path.dirname(p), exist_ok=True)
        conn = sqlite3.connect(p, check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS force_orders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol     TEXT NOT NULL,
                side       TEXT,
                price      REAL,
                qty        REAL,
                avg_price  REAL,
                status     TEXT,
                trade_time INTEGER,
                notional   REAL,
                raw        TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fo_sym_ts "
                     "ON force_orders(symbol, trade_time)")
        conn.commit()
        if db_path:
            return conn  # 测试注入：不缓存
        _FO_CONN = conn
    return _FO_CONN


def persist_force_order(data: dict, db_path: str | None = None) -> bool:
    """把 forceOrder 消息落 SQLite。o 字段结构见币安文档。永不抛出。"""
    try:
        o = data.get("o") or {}
        price = float(o.get("ap") or o.get("p") or 0)
        qty = float(o.get("q") or 0)
        conn = _fo_conn(db_path)
        conn.execute(
            "INSERT INTO force_orders (symbol, side, price, qty, avg_price, "
            "status, trade_time, notional, raw) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(o.get("s") or "").upper(), o.get("S"), float(o.get("p") or 0),
             qty, price, o.get("X"), int(o.get("T") or 0),
             round(price * qty, 4), json.dumps(data, ensure_ascii=False)[:2000]))
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"forceOrder 落库失败（忽略继续）: {exc!r}")
        return False


def force_orders_recent(symbol: str | None = None, limit: int = 100,
                        db_path: str | None = None) -> list[dict]:
    """读最近强平单历史（爆仓面板数据源）。"""
    try:
        conn = _fo_conn(db_path)
        q = ("SELECT symbol, side, price, qty, avg_price, status, trade_time, notional "
             "FROM force_orders ")
        args: list = []
        if symbol:
            q += "WHERE symbol=? "
            args.append(symbol.upper())
        q += "ORDER BY trade_time DESC LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        cur = conn.execute(q, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


# ────────────────────────── 流名构建 / 消息分发 ──────────────────────────

def build_stream_names(symbols: list[str], cfg: dict | None = None,
                       market: str = "futures",
                       kline_intervals: list[str] | None = None,
                       stream_types: set[str] | frozenset | None = None) -> list[str]:
    """按配置开关生成组合流名列表（币安要求符号小写）。

    market="spot"（回退模式）：forceOrder 无现货对应流自动跳过；
    现货 depth 增量流只支持 @100ms/@1000ms，250ms 归一到 100ms。

    [T10] kline_intervals：显式 kline 周期列表（如 ["5m","1h"]），每周期一条流，
    去重保序；None = 沿用配置 ws_kline_interval 单周期（旧行为）。
    [T10] stream_types：显式启用的流类型集合（无视 ws_stream_* 配置开关，
    仍受现货回退约束）；None = 走配置开关（旧行为）。供信号采集器等独立进程
    只订阅自己需要的流，不影响 dashboard 进程的订阅组合。
    """
    c = cfg if cfg is not None else _cfg()
    depth_sp = str(c.get("ws_depth_speed") or "250ms")
    spot = market == "spot"
    if spot and depth_sp not in ("100ms", "1000ms"):
        depth_sp = "100ms"
    if kline_intervals:
        kline_ivs = [str(iv) for iv in dict.fromkeys(kline_intervals) if str(iv)]
    else:
        kline_ivs = [str(c.get("ws_kline_interval") or "1m")]

    def _enabled(stype: str, cfg_key: str) -> bool:
        if stream_types is not None:
            return stype in stream_types
        return bool(c.get(cfg_key, True))

    out: list[str] = []
    for sym in symbols:
        s = sym.lower()
        if _enabled("kline", "ws_stream_kline"):
            out.extend(f"{s}@kline_{iv}" for iv in kline_ivs)
        if _enabled("aggTrade", "ws_stream_aggtrade"):
            out.append(f"{s}@aggTrade")
        if _enabled("forceOrder", "ws_stream_forceorder") and not spot:
            out.append(f"{s}@forceOrder")
        if _enabled("depth", "ws_stream_depth"):
            out.append(f"{s}@depth@{depth_sp}")
    return out


def _classify(stream_name: str) -> tuple[str | None, str | None]:
    """'btcusdt@kline_1m' → ('kline', 'BTCUSDT')；未知流返回 (None, None)。"""
    parts = (stream_name or "").split("@")
    if len(parts) < 2:
        return None, None
    sym = parts[0].upper()
    kind = parts[1]
    if kind.startswith("kline"):
        return "kline", sym
    if kind == "aggTrade":
        return "aggTrade", sym
    if kind == "forceOrder":
        return "forceOrder", sym
    if kind.startswith("depth"):
        return "depth", sym
    return None, None


def dispatch(raw_msg: str | dict, cfg: dict | None = None,
             fo_db_path: str | None = None) -> tuple[str | None, str | None]:
    """解析组合流消息 → 缓冲 + 统计 + 回调 + forceOrder 落库。

    返回 (stream_type, symbol)；无法识别返回 (None, None)。永不抛出。
    独立成纯函数便于 smoketest 注入 mock 消息。
    """
    try:
        msg = json.loads(raw_msg) if isinstance(raw_msg, str) else raw_msg
        if not isinstance(msg, dict):
            return None, None
        stype, sym = _classify(str(msg.get("stream") or ""))
        data = msg.get("data")
        if not stype or not sym or not isinstance(data, dict):
            return None, None
        c = cfg if cfg is not None else _cfg()
        cap = int(c.get("ws_buffer_size") or 1000)
        key = (stype, sym)
        with _BUF_LOCK:
            buf = _BUFFERS.get(key)
            if buf is None or buf.maxlen != cap:
                buf = deque(buf or [], maxlen=cap)
                _BUFFERS[key] = buf
            buf.append(data)
        st = _STATS[stype]
        now = time.time()
        st["last_msg_ts"] = now
        st["msg_count"] += 1
        st["_rate_window"].append(now)
        if stype == "forceOrder" and c.get("ws_force_order_persist", True):
            persist_force_order(data, db_path=fo_db_path)
        for fn in _CALLBACKS[stype]:
            try:
                fn(sym, data)
            except Exception as exc:  # noqa: BLE001 — 回调异常不拖垮数据流
                _log(f"{stype} 回调异常（忽略）: {exc!r}")
        return stype, sym
    except Exception as exc:  # noqa: BLE001
        _log(f"消息解析异常（忽略）: {exc!r}")
        return None, None


# ────────────────────────── 重连退避 ──────────────────────────

def next_backoff(attempt: int, base_s: float, max_s: float) -> float:
    """第 attempt 次（从 0 起）重连的等待秒：base × 2^attempt 封顶 max。"""
    try:
        return min(float(max_s), float(base_s) * (2.0 ** max(0, int(attempt))))
    except Exception:  # noqa: BLE001
        return 5.0


# [任务N1v2·B] 连续未成通路的冷却档阈值：fail_streak 达标后退避不再按 60s
# 常规封顶，抬升到 ws_backoff_cold_s——45+ 次/小时的握手风暴本身就是共享 IP
# 连接压力（币安 WS 握手按 IP 限 300 次/5min，第三方也在消耗同一预算）。
_COLD_STREAK_N = 10


def _chunk_size(cfg: dict | None = None) -> int:
    """握手 URL 最大携带流数（0=不拆，旧行为）。配置层异常回退默认 15。"""
    c = cfg if cfg is not None else _cfg()
    try:
        return max(0, int(c.get("ws_max_streams_per_conn", 15)))
    except (TypeError, ValueError):
        return 15


def cold_backoff(wait: float, fail_streak: int, cfg: dict | None = None) -> float:
    """连续 fail_streak 次未成通路时把 wait 抬到冷却档；未达标原样返回。"""
    if fail_streak < _COLD_STREAK_N:
        return wait
    c = cfg if cfg is not None else _cfg()
    try:
        cold = float(c.get("ws_backoff_cold_s") or 300.0)
    except (TypeError, ValueError):
        cold = 300.0
    return max(wait, cold)


# ────────────────────────── WS 主循环 ──────────────────────────

# [T10] 本进程订阅覆写（start() 传入；None=走配置，见 build_stream_names）
_START_OPTS: dict = {"kline_intervals": None, "stream_types": None}

# 记住上次成功的端点策略索引（重连时优先复用，减少探测时间）
# [任务H 方案4] 持久化到磁盘：进程重启后直接从历史可用策略起步，省掉整轮探测
_LAST_GOOD_PLAN: dict = {"idx": None}
LAST_GOOD_PLAN_PATH = os.path.join(CONFIG_DIR, "ws_last_good_plan.json")


def _save_last_good_plan(idx: int) -> None:
    """成功策略落盘（按名称存，策略表增删重排也不错位）。失败静默不影响主链路。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LAST_GOOD_PLAN_PATH, "w", encoding="utf-8") as f:
            json.dump({"name": _ENDPOINT_PLANS[idx][0], "ts": time.time()}, f)
    except Exception:  # noqa: BLE001
        pass


def _load_last_good_plan() -> Optional[int]:
    """读回持久化的成功策略索引；文件缺失/损坏/名称已不存在返回 None。"""
    try:
        with open(LAST_GOOD_PLAN_PATH, encoding="utf-8") as f:
            name = str(json.load(f).get("name") or "")
        for i, plan in enumerate(_ENDPOINT_PLANS):
            if plan[0] == name:
                return i
    except Exception:  # noqa: BLE001
        pass
    return None


def _apply_fail_jump(fail_streak: int, plan_idx: int) -> int:
    """[任务H 方案4] 连续失败 2 次时直接跳回历史可用策略，不再按表慢慢轮。

    无历史记录 / 已在该策略上时原样返回；仅在 streak==2 触发一次，
    历史策略也失败则继续正常轮换（避免死磕）。
    """
    good = _LAST_GOOD_PLAN["idx"]
    if fail_streak == 2 and good is not None and good != plan_idx:
        return good
    return plan_idx


async def _connect_once(symbols: list[str], cfg: dict, plan_idx: int) -> str:
    """按端点策略连一次并持续收消息。

    返回退出原因：'no-first-frame'（15s 无首帧=策略无效，换下一策略）/
    'closed'（正常断开，同策略重连）/ 'stopped'。异常向上抛由外层记录。
    """
    import aiohttp

    name, base, use_proxy, is_futures = _ENDPOINT_PLANS[plan_idx]
    market = "futures" if is_futures else "spot"
    streams = build_stream_names(symbols, cfg, market=market,
                                 kline_intervals=_START_OPTS["kline_intervals"],
                                 stream_types=_START_OPTS["stream_types"])
    if not streams:
        _META["last_error"] = "所有流开关均关闭"
        await asyncio.sleep(30)
        return "closed"
    # [任务N1v2·B] 大订阅拆块：握手 URL 只携带首块 ≤ws_max_streams_per_conn 条，
    # 其余连接成功后用 SUBSCRIBE 消息补订（币安组合流支持热订阅，回执帧
    # {"result":null,"id":N} 无 stream 键、dispatch 天然忽略）。
    # 背景：采集器 30 流组合 URL 四策略连续 45+ 次握手超时，而 dashboard 同机
    # 同代理 15 流握手秒连——把握手尺寸对齐实证可用的小订阅，降低被代理/
    # 交易所握手侧限制拒绝的面积。0 = 不拆（旧行为）。
    url_streams, pending = streams, []
    max_per = _chunk_size(cfg)
    if 0 < max_per < len(streams):
        url_streams = streams[:max_per]
        pending = [streams[i:i + max_per]
                   for i in range(max_per, len(streams), max_per)]
    url = base + "?streams=" + "/".join(url_streams)
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
             if use_proxy else None)
    _META["url_streams"] = len(streams)

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=SOCK_CONNECT_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(url, proxy=proxy, heartbeat=None,
                                      autoping=True, max_msg_size=4 * 2 ** 20) as ws:
            _META.update({"connected": True, "connected_at": time.time(),
                          "endpoint": name, "market": market,
                          "degraded_streams": (["forceOrder"] if market == "spot"
                                               and cfg.get("ws_stream_forceorder", True)
                                               else [])})
            _log(f"已连接 [{name}]（{len(streams)} 流 / {len(symbols)} 币）"
                 + (f" via {proxy}" if proxy else " 直连")
                 + ("；⚠️ 现货回退模式 forceOrder 降级" if market == "spot" else ""))
            if pending:
                for i, chunk in enumerate(pending, 1):
                    try:
                        await ws.send_json({"method": "SUBSCRIBE",
                                            "params": chunk, "id": i})
                    except Exception as exc:  # noqa: BLE001
                        _log(f"[{name}] 补订阅第 {i} 批失败"
                             f"（{len(chunk)} 流）: {exc!r}，重连重来")
                        return "closed"
                _log(f"[{name}] 握手携带 {len(url_streams)} 流，连接后补订 "
                     f"{len(pending)} 批共 {len(streams) - len(url_streams)} 流")
            got_first = False
            while not _STOP.is_set():
                if time.time() - _META["connected_at"] > MAX_CONN_AGE_S:
                    _log("连接寿命达 23h，主动重连（避开币安 24h 强断）")
                    return "closed"
                try:
                    msg = await ws.receive(
                        timeout=60.0 if got_first else FIRST_FRAME_TIMEOUT_S)
                except asyncio.TimeoutError:
                    if not got_first:
                        _log(f"[{name}] {FIRST_FRAME_TIMEOUT_S:g}s 未收到首帧"
                             "（代理可能丢弃数据帧），标记该端点无效")
                        return "no-first-frame"
                    _log("60s 无消息，重连")
                    return "closed"
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if not got_first:
                        got_first = True
                        _META["last_error"] = None
                        _LAST_GOOD_PLAN["idx"] = plan_idx
                        _save_last_good_plan(plan_idx)
                        _log(f"[{name}] 首帧确认，数据流正常")
                    dispatch(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.ERROR):
                    _log(f"连接关闭/错误: {msg.type}")
                    return "closed" if got_first else "no-first-frame"
    return "stopped"


async def _run_ws(symbols: list[str]) -> None:
    attempt = 0
    fail_streak = 0  # 连续未确认首帧的连接次数（正常收过数即归零）
    plan_idx = _LAST_GOOD_PLAN["idx"] if _LAST_GOOD_PLAN["idx"] is not None else 0
    while not _STOP.is_set():
        cfg = _cfg()
        try:
            outcome = await _connect_once(symbols, cfg, plan_idx)
            if outcome == "no-first-frame":
                fail_streak += 1
                plan_idx = (plan_idx + 1) % len(_ENDPOINT_PLANS)
                _log(f"切换端点策略 → {_ENDPOINT_PLANS[plan_idx][0]}")
            elif outcome == "closed":
                attempt = 0  # 曾正常收数：退避归零，同策略快速重连
                fail_streak = 0
        except Exception as exc:  # noqa: BLE001
            _META["last_error"] = repr(exc)[:200]
            _log(f"[{_ENDPOINT_PLANS[plan_idx][0]}] 连接异常: {exc!r}")
            # 连接层异常（超时/拒绝/代理挂了）也轮换策略，避免死磕坏端点
            fail_streak += 1
            plan_idx = (plan_idx + 1) % len(_ENDPOINT_PLANS)
        finally:
            _META["connected"] = False
        # [任务H 方案4] 连续 2 次失败：直接跳回历史可用策略
        jump = _apply_fail_jump(fail_streak, plan_idx)
        if jump != plan_idx:
            plan_idx = jump
            _log(f"连续 {fail_streak} 次失败，跳转历史可用策略 → "
                 f"{_ENDPOINT_PLANS[plan_idx][0]}")
        if _STOP.is_set():
            break
        wait = next_backoff(attempt, float(cfg.get("ws_reconnect_base_s") or 1.0),
                            float(cfg.get("ws_reconnect_max_s") or 60.0))
        cold = cold_backoff(wait, fail_streak, cfg)
        if cold > wait:
            wait = cold
            _log(f"连续 {fail_streak} 次未成通路，退避升冷却档 {wait:g}s"
                 "（降握手风暴，蹭共享 IP 连接预算恢复窗）")
        attempt += 1
        _META["reconnects"] += 1
        _log(f"{wait:.1f}s 后重连（第 {attempt} 次退避，策略 "
             f"{_ENDPOINT_PLANS[plan_idx][0]}）…")
        # 分片睡眠：退避期间也能及时响应 stop()
        end = time.time() + wait
        while time.time() < end and not _STOP.is_set():
            await asyncio.sleep(min(0.5, max(0.05, end - time.time())))


def _thread_main(symbols: list[str]) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_ws(symbols))
    except Exception as exc:  # noqa: BLE001
        _META["last_error"] = repr(exc)[:200]
        _log(f"WS 线程异常退出: {exc!r}")
    finally:
        _META["running"] = False
        _META["connected"] = False
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


def start(symbols: list[str] | None = None, *,
          kline_intervals: list[str] | None = None,
          stream_types: set[str] | None = None) -> bool:
    """启动 WS 客户端 daemon 线程（幂等：已运行则直接返回 True）。

    [T10] kline_intervals / stream_types：本进程订阅覆写（语义见
    build_stream_names）；只在本次冷启动生效，已运行时忽略新覆写（幂等不变）。
    """
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return True
    cfg = _cfg()
    _START_OPTS["kline_intervals"] = (
        [str(iv) for iv in kline_intervals] if kline_intervals else None)
    _START_OPTS["stream_types"] = set(stream_types) if stream_types else None
    if symbols is None:
        symbols = [str(s).upper() for s in (cfg.get("watchlist") or ["BTCUSDT"])]
    # [任务H 方案4] 冷启动读回历史可用策略（进程内已有记忆时不覆盖）
    if _LAST_GOOD_PLAN["idx"] is None:
        disk_idx = _load_last_good_plan()
        if disk_idx is not None:
            _LAST_GOOD_PLAN["idx"] = disk_idx
            _log(f"读回历史可用端点策略：{_ENDPOINT_PLANS[disk_idx][0]}")
    _STOP.clear()
    _META.update({"running": True, "started_at": time.time(), "symbols": symbols})
    _THREAD = threading.Thread(target=_thread_main, args=(symbols,),
                               daemon=True, name="jarvis-ws-stream")
    _THREAD.start()
    _log(f"WS 客户端线程已启动：{symbols}")
    return True


def stop(timeout: float = 5.0) -> None:
    """停止 WS 客户端（等待线程退出，超时放弃——daemon 线程随进程回收）。"""
    _STOP.set()
    t = _THREAD
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    _META["running"] = False
    _META["connected"] = False


# ────────────────────────── CLI 自测 ──────────────────────────

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="贾维斯 Binance WS 实时数据地基")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test", help="主网实测：连四流跑 N 秒，打印样本与计数")
    t.add_argument("--seconds", type=int, default=12)
    t.add_argument("--symbols", default=None, help="逗号分隔，缺省读配置 watchlist")
    sub.add_parser("health", help="打印当前健康度（需另一进程在跑时意义有限）")
    args = ap.parse_args()

    if args.cmd == "health":
        print(json.dumps(health(), ensure_ascii=False, indent=2))
        return 0

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else None)
    samples: dict[str, dict] = {}

    def _sampler(stype):
        def fn(sym, data):
            if stype not in samples:
                samples[stype] = {"symbol": sym,
                                  "keys": sorted(data.keys())[:12]}
        return fn

    for st_ in STREAM_TYPES:
        register_callback(st_, _sampler(st_))
    start(symbols)
    deadline = time.time() + max(3, args.seconds)
    while time.time() < deadline:
        time.sleep(1)
    h = health()
    print("\n=== 实测结果 ===")
    print(json.dumps({k: v for k, v in h.items() if k != "buffered"},
                     ensure_ascii=False, indent=2, default=str))
    print("样本（每流首条）:")
    print(json.dumps(samples, ensure_ascii=False, indent=2))
    got = [t2 for t2 in STREAM_TYPES if h["streams"][t2]["msg_count"] > 0]
    print(f"\n收到消息的流: {got}  端点: {h['endpoint']}  降级: {h['degraded_streams']}")
    stop()
    # forceOrder 是低频事件流，短测收不到属正常；三流有数据即判通过
    need = {"kline", "aggTrade", "depth"}
    return 0 if need.issubset(set(got)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
