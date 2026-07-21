#!/usr/bin/env python3
"""贾维斯 → RuoYi MySQL 单向同步器（T2 · 调度框架层）。

把贾维斯侧数据（12信号 / 推荐点位 / 盘口 / 币种市场快照）按分组周期
推送到 RuoYi 的 MySQL `jiaweisi` 库 jarvis_* 镜像表。本文件是 T2 框架：
config 加载校验、flock 单实例、游标持久化（原子写 + 三级恢复）、MySQL
长连接管理（每轮 ping + 指数退避）、分组调度循环、心跳写入、日志轮转。
具体表同步任务由 T3（通道 A：源库增量）/ T4（通道 B：HTTP 快照）通过
`register_task` 注册接入，框架本身不连贾维斯源库。

设计出处：Vibe-Trading/贾维斯-RuoYi同步-开发计划.md §3-§4；
上游方案 Vibe-Trading/贾维斯-RuoYi同步方案.md §2.2-§2.6。

硬纪律：
  - 源侧零写入：本框架及后续任务对贾维斯 PG/SQLite 只读（本文件不连源库）。
  - 单向推送：MySQL 侧唯一"回读"是自己写的 jarvis_sync_state 游标字段。
  - 密码不入仓：真实 config 在 ~/.vibe-trading/sync/（chmod 600）；仓库只有
    sync_config.example.json 模板。

用法：
  python3 jarvis_sync.py                       # 常驻循环（launchd 模式）
  python3 jarvis_sync.py --once                # 各启用组各跑一轮后退出
  python3 jarvis_sync.py --once --dry-run      # 打印一轮调度计划，不连库不写盘
  python3 jarvis_sync.py --config /path/x.json # 指定配置（默认 ~/.vibe-trading/sync/sync_config.json）

T3/T4 接入点（表同步函数注册协议）：
  from jarvis_sync import register_task, SyncContext, TaskResult

  @register_task(group="fast", table="jarvis_signal_change")
  def sync_signal_change(ctx: SyncContext) -> TaskResult:
      cur = ctx.cursors.get("jarvis_signal_change")     # None=冷启动
      conn = ctx.mysql.get()                            # 可能为 None（退避期）
      ...拉源增量 → executemany upsert → conn.commit()...
      ctx.cursors.set("jarvis_signal_change", new_cur)  # 成功后才推进（从不后退）
      return TaskResult(rows=n, cursor_value=str(new_cur), lag_seconds=lag)

  分组语义（计划 §3.3）：fast=5s（signal_state/change）；mid=60s（tape_bar/
  intraday_prediction/force_order_min/snapshot/outcome/position/limit_order）；
  api=180s（reco_plan，通道 B）；market_snapshot=300s（market_snapshot，通道 B）。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Callable, Optional
from urllib.parse import urlparse, unquote

SYNC_DIR = os.path.expanduser("~/.vibe-trading/sync")
CONFIG_PATH = os.path.join(SYNC_DIR, "sync_config.json")
CURSORS_PATH = os.path.join(SYNC_DIR, "cursors.json")
LOCK_PATH = os.path.join(SYNC_DIR, "sync.lock")
EXAMPLE_NAME = "sync_config.example.json"

HEARTBEAT_TABLE = "jarvis_sync_state"
FRAMEWORK_HB_NAME = "_sync_framework"

ENV_MYSQL_URL = "JARVIS_SYNC_MYSQL_URL"

# 分组默认周期（秒），与计划 §3.3 / §4 一致
GROUP_DEFAULTS = {
    "fast": 5,
    "mid": 60,
    "api": 180,
    "market_snapshot": 300,
}

log = logging.getLogger("jarvis_sync")


# ══════════════════════════════════════════════════════ 配置加载与校验


class ConfigError(Exception):
    """配置缺失/非法——main 捕获后友好输出并以退出码 2 结束。"""


def _parse_mysql_url(url: str) -> dict:
    """解析 JARVIS_SYNC_MYSQL_URL=mysql://user:pass@host:port/db 覆盖段。"""
    p = urlparse(url)
    if p.scheme not in ("mysql", "mysql+pymysql"):
        raise ConfigError(f"{ENV_MYSQL_URL} scheme 必须是 mysql://，当前: {p.scheme}")
    if not p.hostname or not p.path.lstrip("/"):
        raise ConfigError(f"{ENV_MYSQL_URL} 缺 host 或 database: {url!r}")
    out = {
        "host": p.hostname,
        "port": p.port or 3306,
        "database": p.path.lstrip("/"),
    }
    if p.username:
        out["user"] = unquote(p.username)
    if p.password:
        out["password"] = unquote(p.password)
    return out


def load_config(path: str) -> dict:
    """读取并校验 sync_config.json；缺失/非法抛 ConfigError（含修复指引）。"""
    if not os.path.exists(path):
        raise ConfigError(
            f"配置文件不存在: {path}\n"
            f"  修复：mkdir -p {SYNC_DIR} && "
            f"cp <仓库>/Vibe-Trading/{EXAMPLE_NAME} {path}\n"
            f"  然后填入 MySQL 真实账号密码并 chmod 600 {path}"
        )
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ConfigError(f"配置文件解析失败: {path} — {e}") from e
    if not isinstance(cfg, dict):
        raise ConfigError(f"配置根节点必须是 JSON object: {path}")

    # 环境变量覆盖 mysql 段（调试用，计划 §4）
    env_url = os.environ.get(ENV_MYSQL_URL, "").strip()
    if env_url:
        cfg.setdefault("mysql", {})
        cfg["mysql"].update(_parse_mysql_url(env_url))
        log.info("mysql 段已被环境变量 %s 覆盖（host=%s）", ENV_MYSQL_URL, cfg["mysql"]["host"])

    # ── mysql 段必填校验 ──
    my = cfg.get("mysql")
    if not isinstance(my, dict):
        raise ConfigError("缺少 mysql 配置段（host/port/user/password/database）")
    missing = [k for k in ("host", "user", "password", "database") if not my.get(k)]
    if missing:
        raise ConfigError(f"mysql 段缺少必填项: {', '.join(missing)}")
    my.setdefault("port", 3306)
    my.setdefault("ssl_mode", "PREFERRED")
    my.setdefault("connect_timeout", 5)
    if not isinstance(my["port"], int) or not (0 < my["port"] < 65536):
        raise ConfigError(f"mysql.port 非法: {my['port']!r}")
    if str(my.get("password", "")).startswith("<"):
        # 不阻塞启动：允许拷贝 example 后先空转验证框架，连库自然失败进退避
        log.warning("mysql.password 仍是模板占位符，MySQL 通道将连不上（框架空转）")

    # ── groups 段补默认 ──
    groups = cfg.setdefault("groups", {})
    if not isinstance(groups, dict):
        raise ConfigError("groups 段必须是 object")
    # 兼容计划 §4 的扁平键 market_snapshot_period_s
    flat_ms = groups.pop("market_snapshot_period_s", None)
    for name, default_period in GROUP_DEFAULTS.items():
        g = groups.setdefault(name, {})
        if not isinstance(g, dict):
            raise ConfigError(f"groups.{name} 必须是 object")
        g.setdefault("enabled", True)
        g.setdefault("period_s", default_period)
        if not isinstance(g["period_s"], (int, float)) or g["period_s"] <= 0:
            raise ConfigError(f"groups.{name}.period_s 非法: {g['period_s']!r}")
    if flat_ms is not None:
        groups["market_snapshot"]["period_s"] = flat_ms

    # ── 其余键补默认（计划 §4）──
    cfg.setdefault("dashboard_base_url", "http://127.0.0.1:7899")
    cfg.setdefault("symbols", None)
    cfg.setdefault("table_prefix", "jarvis_")
    cfg.setdefault("batch_size", 5000)
    cfg.setdefault("exec_batch", 500)
    cfg.setdefault("retention_days", {})
    cfg.setdefault("http_timeout_s", 10)
    logc = cfg.setdefault("log", {})
    logc.setdefault("path", os.path.join(SYNC_DIR, "sync.log"))
    logc.setdefault("max_mb", 20)
    logc.setdefault("backups", 3)

    # 真实 config 权限提醒（不阻塞：example 拷贝初期常忘）
    try:
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            log.warning("配置文件权限过宽 %o，建议 chmod 600 %s（内含数据库密码）", mode, path)
    except OSError:
        pass
    return cfg


def resolve_symbols(cfg: dict) -> list[str]:
    """symbols=null 时跟随贾维斯 watchlist（jarvis_config），失败回退默认 7 币。"""
    explicit = cfg.get("symbols")
    if isinstance(explicit, list) and explicit:
        return [str(s).upper() for s in explicit]
    try:
        import jarvis_config  # 仓内配置中心，load() 永不抛出

        wl = jarvis_config.load().get("watchlist") or []
        if wl:
            return [str(s).upper() for s in wl]
    except Exception as e:  # noqa: BLE001 — 跟随失败走内置默认，不阻断同步
        log.warning("读取 jarvis_config watchlist 失败（%s），使用内置默认", e)
    return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]


# ══════════════════════════════════════════════════════ 日志


def setup_logging(cfg: dict, *, echo_console: bool, file_enabled: bool = True) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if file_enabled:
        logc = cfg["log"]
        path = os.path.expanduser(logc["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = RotatingFileHandler(
            path,
            maxBytes=int(logc["max_mb"] * 1024 * 1024),
            backupCount=int(logc["backups"]),
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if echo_console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ══════════════════════════════════════════════════════ flock 单实例


class SingleInstanceLock:
    """flock 排他锁：防 launchd 重复拉起叠加实例（方案 §2.5）。"""

    def __init__(self, path: str = LOCK_PATH):
        self.path = path
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = ""
            try:
                holder = os.read(self._fd, 64).decode("utf-8", "ignore").strip()
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None
            raise SystemExit(
                f"另一个 jarvis_sync 实例正在运行（pid={holder or '未知'}，锁 {self.path}）；"
                f"若确认无实例残留，删除锁文件后重试"
            )
        os.ftruncate(self._fd, 0)
        os.write(self._fd, str(os.getpid()).encode())
        os.fsync(self._fd)

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


# ══════════════════════════════════════════════════════ 游标持久化（三级恢复）


class CursorStore:
    """游标持久化：cursors.json 原子写；恢复顺序 本地 json → MySQL 心跳表 → 冷启动。

    纪律（方案 §2.2/§2.4）：游标从不后退——set() 仅在任务批量写 MySQL 成功
    commit 后调用；每次推进立即落盘。
    """

    def __init__(self, path: str = CURSORS_PATH):
        self.path = path
        self._data: dict[str, str] = {}
        self._loaded_from = "cold_start"

    # ── 一级：本地 json ──
    def load_local(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._data = {str(k): str(v) for k, v in raw.items() if v is not None}
                self._loaded_from = "local_json"
                return True
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as e:
            log.warning("cursors.json 损坏（%s），进入二级恢复", e)
        return False

    # ── 二级：MySQL 心跳表（唯一豁免的"回读"，只读自己写的游标字段）──
    def recover_from_mysql(self, conn) -> bool:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT table_name, cursor_value FROM {HEARTBEAT_TABLE} "
                    "WHERE cursor_value IS NOT NULL"
                )
                rows = cur.fetchall()
            if rows:
                self._data = {str(r[0]): str(r[1]) for r in rows}
                self._loaded_from = "mysql_heartbeat"
                self.flush()
                log.info("游标已从 MySQL 心跳表恢复 %d 项", len(self._data))
                return True
        except Exception as e:  # noqa: BLE001 — 表不存在/权限不足等均容忍，走冷启动
            log.warning("MySQL 心跳表游标恢复失败（%s），冷启动全量", e)
        return False

    # ── 三级：冷启动（get 返回 None，任务按游标零值全量拉，幂等 upsert 无害）──

    @property
    def loaded_from(self) -> str:
        return self._loaded_from

    def get(self, table: str) -> Optional[str]:
        return self._data.get(table)

    def set(self, table: str, value: str) -> None:
        self._data[table] = str(value)
        self.flush()

    def flush(self) -> None:
        """原子写：tmp + os.replace（方案 §2.4）。"""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


# ══════════════════════════════════════════════════════ MySQL 连接管理


class MySQLManager:
    """长连接 + 每轮 ping + 指数退避 1s→60s（方案 §2.5）。

    退避为"重试门"而非阻塞 sleep：连接失败后 next_retry 之前 get() 直接
    返回 None，调度循环不被单点拖停；恢复成功即退避归零。
    PyMySQL 未安装时降级为永远 None（框架空转可用，便于 T2 阶段无依赖验证）。
    """

    BACKOFF_MAX = 60.0

    def __init__(self, cfg: dict):
        self.cfg = cfg["mysql"]
        self._conn = None
        self._delay = 1.0
        self._next_retry = 0.0
        self._pymysql = None
        self._driver_missing_warned = False

    def _driver(self):
        if self._pymysql is None:
            try:
                import pymysql  # 唯一第三方依赖，requirements 待 T3 联调期定版

                self._pymysql = pymysql
            except ImportError:
                if not self._driver_missing_warned:
                    log.warning(
                        "PyMySQL 未安装（pip install PyMySQL），MySQL 通道停用；"
                        "框架继续空转，任务将跳过"
                    )
                    self._driver_missing_warned = True
                return None
        return self._pymysql

    def get(self):
        """返回可用连接或 None（退避期/驱动缺失/连不上）。每轮开头调用即含 ping。"""
        drv = self._driver()
        if drv is None:
            return None
        now = time.monotonic()
        if self._conn is not None:
            try:
                # 轻量 ping（不用 conn.ping(reconnect=)——PyMySQL 2.2+ 已弃用该参数）
                with self._conn.cursor() as c:
                    c.execute("SELECT 1")
                    c.fetchone()
                return self._conn
            except Exception as e:  # noqa: BLE001 — ping 失败进入重连退避
                log.warning("MySQL ping 失败（%s），进入重连退避", e)
                self._close()
        if now < self._next_retry:
            return None
        try:
            ssl_mode = str(self.cfg.get("ssl_mode", "PREFERRED")).upper()
            kwargs = dict(
                host=self.cfg["host"],
                port=int(self.cfg["port"]),
                user=self.cfg["user"],
                password=self.cfg["password"],
                database=self.cfg["database"],
                connect_timeout=int(self.cfg.get("connect_timeout", 5)),
                charset="utf8mb4",
                autocommit=False,
            )
            if ssl_mode in ("REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"):
                kwargs["ssl"] = {}  # 触发 TLS；证书校验策略迁云时按通道定
            self._conn = drv.connect(**kwargs)
            self._delay = 1.0
            log.info("MySQL 已连接 %s:%s/%s", self.cfg["host"], self.cfg["port"], self.cfg["database"])
            return self._conn
        except Exception as e:  # noqa: BLE001 — 连接失败统一退避
            self._next_retry = now + self._delay
            log.warning("MySQL 连接失败（%s），%.0fs 后重试", e, self._delay)
            self._delay = min(self._delay * 2, self.BACKOFF_MAX)
            return None

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def close(self) -> None:
        self._close()


# ══════════════════════════════════════════════════════ 任务注册协议（T3/T4 接入点）


@dataclass
class TaskResult:
    """单任务单轮执行结果——框架据此写心跳、推游标统计。"""

    rows: int = 0
    cursor_value: Optional[str] = None
    lag_seconds: Optional[float] = None
    error: Optional[str] = None


@dataclass
class SyncContext:
    """传给表同步函数的运行时上下文。"""

    config: dict
    mysql: MySQLManager
    cursors: CursorStore
    symbols: list[str]
    dry_run: bool = False


TaskFn = Callable[[SyncContext], TaskResult]


@dataclass
class SyncTask:
    table: str  # 逻辑表名（写心跳的 table_name），如 jarvis_signal_change
    group: str  # fast / mid / api / market_snapshot
    fn: TaskFn


TASK_REGISTRY: dict[str, list[SyncTask]] = {name: [] for name in GROUP_DEFAULTS}


def register_task(group: str, table: str) -> Callable[[TaskFn], TaskFn]:
    """装饰器：T3/T4 用它把表同步函数挂进分组调度。

    用法见模块 docstring。重复注册同名 table 视为编码错误直接抛出。
    """
    if group not in TASK_REGISTRY:
        raise ValueError(f"未知分组 {group!r}，可选: {sorted(TASK_REGISTRY)}")

    def deco(fn: TaskFn) -> TaskFn:
        if any(t.table == table for t in TASK_REGISTRY[group]):
            raise ValueError(f"任务重复注册: {group}/{table}")
        TASK_REGISTRY[group].append(SyncTask(table=table, group=group, fn=fn))
        return fn

    return deco


# ══════════════════════════════════════════════════════ 心跳


_HB_UPSERT = (
    f"INSERT INTO {HEARTBEAT_TABLE} "
    "(table_name, cursor_value, last_run_at, last_ok_at, last_error, rows_total, lag_seconds) "
    "VALUES (%s, %s, NOW(3), %s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE "
    "cursor_value=VALUES(cursor_value), last_run_at=VALUES(last_run_at), "
    "last_ok_at=COALESCE(VALUES(last_ok_at), last_ok_at), "
    "last_error=VALUES(last_error), "
    "rows_total=rows_total+VALUES(rows_total), "
    "lag_seconds=VALUES(lag_seconds)"
)


class HeartbeatWriter:
    """每轮 upsert jarvis_sync_state；表未建时容忍 warn 不崩（任务要求 6）。"""

    def __init__(self):
        self._table_missing_warned = False

    def write(self, conn, table: str, result: TaskResult) -> None:
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    _HB_UPSERT,
                    (
                        table,
                        result.cursor_value,
                        None if result.error else _now_dt3(),
                        (result.error or "")[:512] or None,
                        int(result.rows),
                        result.lag_seconds,
                    ),
                )
            conn.commit()
            self._table_missing_warned = False
        except Exception as e:  # noqa: BLE001 — 心跳失败绝不拖垮同步主链路
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            msg = str(e)
            if "1146" in msg or "doesn't exist" in msg:
                if not self._table_missing_warned:
                    log.warning("心跳表 %s 不存在（T1 建表未执行？），心跳暂不落库", HEARTBEAT_TABLE)
                    self._table_missing_warned = True
            else:
                log.warning("心跳写入失败: %s", msg)


def _now_dt3() -> str:
    t = time.time()
    ms = int((t % 1) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{ms:03d}"


# ══════════════════════════════════════════════════════ 调度循环


class Scheduler:
    """分组周期调度：各组独立 next_due，到点跑组内注册任务，组间互不拖累。"""

    def __init__(self, cfg: dict, ctx: SyncContext):
        self.cfg = cfg
        self.ctx = ctx
        self.hb = HeartbeatWriter()
        self._stop = False
        now = time.monotonic()
        self.schedule: dict[str, float] = {}
        for name, g in cfg["groups"].items():
            if g.get("enabled", True):
                self.schedule[name] = now  # 启动即各跑一轮

    def request_stop(self, signum, _frame) -> None:
        log.info("收到信号 %s，本轮结束后退出", signum)
        self._stop = True

    def run_group(self, group: str) -> None:
        tasks = TASK_REGISTRY.get(group, [])
        conn = self.ctx.mysql.get()
        if not tasks:
            # T2 阶段无任务：写框架心跳证明调度器存活
            self.hb.write(conn, f"{FRAMEWORK_HB_NAME}_{group}", TaskResult(rows=0))
            log.info("[%s] 空转（未注册任务）mysql=%s", group, "up" if conn else "down")
            return
        for task in tasks:
            t0 = time.monotonic()
            try:
                result = task.fn(self.ctx)
            except Exception as e:  # noqa: BLE001 — 单任务失败不影响其他任务（方案 §2.5）
                log.exception("[%s] %s 执行异常", group, task.table)
                result = TaskResult(error=f"{type(e).__name__}: {e}")
            cost = time.monotonic() - t0
            level = logging.WARNING if result.error else logging.INFO
            log.log(
                level,
                "[%s] %s rows=%d cursor=%s lag=%s cost=%.2fs%s",
                group,
                task.table,
                result.rows,
                result.cursor_value,
                f"{result.lag_seconds:.1f}s" if result.lag_seconds is not None else "-",
                cost,
                f" error={result.error}" if result.error else "",
            )
            self.hb.write(conn, task.table, result)

    def run_once(self) -> None:
        for group in list(self.schedule):
            self.run_group(group)

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        groups = self.cfg["groups"]
        log.info(
            "调度启动: %s",
            ", ".join(f"{n}={groups[n]['period_s']}s" for n in self.schedule),
        )
        while not self._stop:
            now = time.monotonic()
            due = [n for n, at in self.schedule.items() if now >= at]
            for name in due:
                self.run_group(name)
                # 按周期滚动 next_due；跑一轮超周期时对齐到下一个未来时点，不补跑积压
                period = float(groups[name]["period_s"])
                nxt = self.schedule[name] + period
                now2 = time.monotonic()
                if nxt <= now2:
                    nxt = now2 + period
                self.schedule[name] = nxt
                if self._stop:
                    break
            if self._stop:
                break
            wake = min(self.schedule.values())
            time.sleep(max(0.05, min(wake - time.monotonic(), 1.0)))  # ≤1s 粒度醒来响应信号
        log.info("调度循环退出")


# ══════════════════════════════════════════════════════ dry-run 计划输出


def print_plan(cfg: dict, cursors: CursorStore, symbols: list[str]) -> None:
    groups = cfg["groups"]
    my = cfg["mysql"]
    lines = [
        "── jarvis_sync 调度计划（dry-run，不连库不写盘）──",
        f"config      : mysql={my['user']}@{my['host']}:{my['port']}/{my['database']} "
        f"(ssl={my.get('ssl_mode')}, password=***)",
        f"dashboard   : {cfg['dashboard_base_url']} (timeout {cfg['http_timeout_s']}s)",
        f"symbols     : {','.join(symbols)}"
        + ("（跟随 watchlist）" if cfg.get("symbols") in (None, []) else "（显式配置）"),
        f"batch       : batch_size={cfg['batch_size']} exec_batch={cfg['exec_batch']}",
        f"cursors     : {cursors.path} [{cursors.loaded_from}]",
        f"log         : {os.path.expanduser(cfg['log']['path'])} "
        f"({cfg['log']['max_mb']}MB × {cfg['log']['backups']})",
        "分组调度：",
    ]
    for name in GROUP_DEFAULTS:
        g = groups[name]
        tasks = TASK_REGISTRY.get(name, [])
        state = "启用" if g.get("enabled", True) else "停用"
        task_desc = ", ".join(t.table for t in tasks) if tasks else "（空——待 T3/T4 注册）"
        lines.append(f"  {name:<16} {state} period={g['period_s']}s 任务: {task_desc}")
    print("\n".join(lines))


# ══════════════════════════════════════════════════════ main


def load_task_modules() -> None:
    """加载 T3/T4 任务模块（可选存在：缺失说明该通道尚未交付，对应分组空转）。

    直跑 `python jarvis_sync.py` 时本模块名是 __main__；先把 sys.modules 里的
    "jarvis_sync" 指向自己，任务模块 `from jarvis_sync import register_task`
    才会注册进同一个 TASK_REGISTRY（防经典 __main__ 双加载陷阱）。
    """
    import importlib

    sys.modules.setdefault("jarvis_sync", sys.modules[__name__])
    for mod in ("jarvis_sync_tasks_a", "jarvis_sync_tasks_b"):
        try:
            importlib.import_module(mod)
            log.info("任务模块 %s 已加载", mod)
        except ModuleNotFoundError as e:
            if e.name == mod:
                log.info("任务模块 %s 未交付，对应分组空转", mod)
            else:
                raise


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 → RuoYi MySQL 单向同步器（框架）")
    ap.add_argument("--config", default=CONFIG_PATH, help=f"配置文件路径（默认 {CONFIG_PATH}）")
    ap.add_argument("--once", action="store_true", help="各启用组各跑一轮后退出（调试/cron 友好）")
    ap.add_argument("--dry-run", action="store_true", help="只打印调度计划，不连库不写任何数据")
    args = ap.parse_args()

    try:
        cfg = load_config(os.path.expanduser(args.config))
    except ConfigError as e:
        print(f"[jarvis_sync] 配置错误:\n{e}", file=sys.stderr)
        return 2

    # dry-run 承诺不写盘：日志只进控制台，不建目录不落文件
    setup_logging(
        cfg,
        echo_console=args.once or args.dry_run or sys.stderr.isatty(),
        file_enabled=not args.dry_run,
    )
    load_task_modules()
    symbols = resolve_symbols(cfg)
    cursors = CursorStore()
    cursors.load_local()

    if args.dry_run:
        print_plan(cfg, cursors, symbols)
        return 0

    lock = SingleInstanceLock()
    try:
        lock.acquire()
    except SystemExit as e:
        print(f"[jarvis_sync] {e}", file=sys.stderr)
        return 1

    mysql = MySQLManager(cfg)
    try:
        # 二级游标恢复：本地 json 缺失且 MySQL 可达时读心跳表（唯一豁免回读）
        if cursors.loaded_from == "cold_start":
            conn = mysql.get()
            if conn is not None:
                cursors.recover_from_mysql(conn)
        log.info(
            "jarvis_sync 启动 pid=%d config=%s cursors=%s symbols=%s",
            os.getpid(), args.config, cursors.loaded_from, ",".join(symbols),
        )
        ctx = SyncContext(config=cfg, mysql=mysql, cursors=cursors, symbols=symbols)
        sched = Scheduler(cfg, ctx)
        if args.once:
            sched.run_once()
        else:
            sched.run_forever()
        return 0
    finally:
        mysql.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
