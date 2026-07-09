#!/usr/bin/env python3
"""贾维斯 JARVIS — 数据库后端兼容层（SQLite ⇄ PostgreSQL）。

目标：让原先散落在各 jarvis_*.py 里的 SQLite 读写代码「一行不改逻辑」即可切到
PostgreSQL（复用 QuantDinger 那套 pg），同时**默认仍是 SQLite**，实现零回归、可随时切回。

启用 pg（任一即可，优先级：环境变量 > 配置文件）：
  1) 环境变量  export JARVIS_DB_URL=postgresql://jarvis:***@127.0.0.1:5432/jarvis
  2) 配置文件  ~/.vibe-trading/db.json  内容 {"url": "postgresql://..."}
未设置时 → 回退本地 SQLite（~/.vibe-trading/jarvis_journal.db），行为与改造前完全一致。

实现要点：
  - 只在 `_conn()` 这一层分流，调用方无需感知后端。
  - 一个轻量连接包装 `PgConnection`，把本仓库用到的 SQLite 方言即时翻译成 pg：
      · 占位符  ?          → %s          （序列参数）
      · 占位符  :name      → %(name)s    （字典参数）
      · 建表    INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY
      · 类型    REAL       → double precision
      · ALTER   ADD COLUMN → ADD COLUMN IF NOT EXISTS（幂等）
      · INSERT OR REPLACE  → INSERT ... ON CONFLICT (<唯一键>) DO UPDATE SET ...
  - `with conn:` 语义对齐 sqlite3：成功 commit / 异常 rollback（并关闭短连接，防泄漏）。
  - 提供 `lastrowid`（经 lastval()）与 `total_changes`（累计 rowcount）兼容属性。
  - 行以 dict 返回，兼容 `row["col"]` 与 `dict(row)`（本仓库核心模块无按位取行）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
_DB_JSON = os.path.join(CONFIG_DIR, "db.json")
# 生产默认库路径；只有指向它时才允许被 pg 接管，其余自定义路径（测试临时库）强制走 SQLite。
_DEFAULT_SQLITE = os.path.join(CONFIG_DIR, "jarvis_journal.db")

# INSERT OR REPLACE 的冲突目标（表 → 唯一键列）。翻译成 pg 的 ON CONFLICT 需要它。
# 仅登记本仓库真实用到 INSERT OR REPLACE 的表；未登记的表遇到该语法会抛错提醒补登记。
_REPLACE_CONFLICT: dict[str, tuple[str, ...]] = {
    "intraday_predictions": ("symbol", "bar_ts"),
}


# ─────────────────────────── 后端选择 ───────────────────────────

def db_url() -> str:
    """返回生效的 pg 连接串；未配置返回空串（= 用 SQLite）。"""
    env = (os.getenv("JARVIS_DB_URL") or "").strip()
    if env:
        return env
    try:
        if os.path.exists(_DB_JSON):
            with open(_DB_JSON, encoding="utf-8") as f:
                url = str((json.load(f) or {}).get("url", "") or "").strip()
                return url
    except Exception:  # noqa: BLE001 — 配置异常绝不能拖垮读写，回退 SQLite
        return ""
    return ""


def use_pg() -> bool:
    return db_url().lower().startswith(("postgres://", "postgresql://"))


def ping(timeout_s: float = 5.0) -> dict:
    """探测当前生效后端的连通性（供 daemon 健康检查）。永不抛出。

    返回 {"ok": bool, "backend": "pg"|"sqlite", "error"?: str}。
    SQLite 模式恒 ok（本地文件无远端依赖）。
    """
    if not use_pg():
        return {"ok": True, "backend": "sqlite"}
    try:
        import psycopg
        with psycopg.connect(db_url(), connect_timeout=int(timeout_s)) as c:
            c.execute("SELECT 1")
        return {"ok": True, "backend": "pg"}
    except Exception as exc:  # noqa: BLE001 — 健康探测只报状态，不许抛出
        return {"ok": False, "backend": "pg", "error": repr(exc)[:200]}


def connect(sqlite_path: str):
    """统一入口：pg 已配置且用默认库路径 → PgConnection；否则 SQLite（含测试自定义路径）。

    仅当 sqlite_path 指向生产默认库时才允许被 pg 接管；测试通过改写 DB_PATH 为临时库时，
    路径不同 → 始终走 SQLite，保证测试隔离不受 pg 配置影响。
    """
    if use_pg() and os.path.abspath(sqlite_path) == os.path.abspath(_DEFAULT_SQLITE):
        return PgConnection(db_url())
    os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────── SQL 方言翻译 ───────────────────────────

def _translate_ddl(sql: str) -> str:
    out = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
                 "BIGSERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
    # SQLite 的 INTEGER 是 64 位；pg 的 INTEGER 只有 32 位，epoch 毫秒(bar_ts/opened_ts)
    # 会溢出。统一映射到 BIGINT，保证时间戳等大整数安全。
    out = re.sub(r"\bINTEGER\b", "BIGINT", out)
    out = re.sub(r"\bREAL\b", "double precision", out)  # 大小写敏感：只命中类型 token
    out = re.sub(r"(ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN)\s+(?!IF\s+NOT\s+EXISTS)",
                 r"\1 IF NOT EXISTS ", out, flags=re.IGNORECASE)
    return out


def _rewrite_insert_or_replace(sql: str) -> str:
    """INSERT OR REPLACE INTO t (cols) ... → INSERT INTO t (cols) ... ON CONFLICT DO UPDATE。"""
    m = re.match(r"\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)",
                 sql, flags=re.IGNORECASE)
    if not m:
        # 无列清单的形式不支持，直接去掉 OR REPLACE（保持 INSERT 语义，冲突会抛错以便暴露）
        return re.sub(r"INSERT\s+OR\s+REPLACE", "INSERT", sql, count=1, flags=re.IGNORECASE)
    table = m.group(1)
    cols = [c.strip() for c in m.group(2).split(",") if c.strip()]
    conflict = _REPLACE_CONFLICT.get(table.lower())
    if not conflict:
        raise ValueError(
            f"jarvis_db: 表 {table} 使用了 INSERT OR REPLACE，但未在 _REPLACE_CONFLICT 登记唯一键"
        )
    body = re.sub(r"INSERT\s+OR\s+REPLACE", "INSERT", sql, count=1, flags=re.IGNORECASE)
    set_cols = [c for c in cols if c.lower() not in {x.lower() for x in conflict}]
    set_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in set_cols)
    conflict_target = ", ".join(conflict)
    return f"{body} ON CONFLICT ({conflict_target}) DO UPDATE SET {set_clause}"


def _convert_placeholders(sql: str, named: bool) -> str:
    """把 SQLite 占位符转成 psycopg 的 pyformat；同时把字面 % 转义为 %%。

    named=True  处理 :name → %(name)s
    named=False 处理 ?     → %s
    """
    out: list[str] = []
    in_str = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            in_str = not in_str
            out.append(ch)
            i += 1
            continue
        if ch == "%":
            out.append("%%")  # 转义字面百分号，避免被 psycopg 当占位符
            i += 1
            continue
        if not in_str:
            if not named and ch == "?":
                out.append("%s")
                i += 1
                continue
            if named and ch == ":":
                j = i + 1
                name = []
                while j < n and (sql[j].isalnum() or sql[j] == "_"):
                    name.append(sql[j])
                    j += 1
                if name:
                    out.append("%(" + "".join(name) + ")s")
                    i = j
                    continue
        out.append(ch)
        i += 1
    return "".join(out)


def translate(sql: str, params) -> str:
    head = sql.lstrip()[:12].upper()
    if head.startswith(("CREATE", "ALTER", "DROP")):
        return _translate_ddl(sql)
    if re.match(r"\s*INSERT\s+OR\s+REPLACE", sql, flags=re.IGNORECASE):
        sql = _rewrite_insert_or_replace(sql)
    if params is None:
        return sql  # psycopg 在 params=None 时不解析 %，原样透传
    return _convert_placeholders(sql, named=isinstance(params, dict))


# ─────────────────────────── pg 连接包装 ───────────────────────────

class _Cursor:
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount


class PgConnection:
    """薄封装：对外表现得像 sqlite3.Connection（本仓库用到的子集）。"""

    def __init__(self, dsn: str):
        import psycopg
        from psycopg.rows import dict_row
        self._pg = psycopg.connect(dsn, row_factory=dict_row)
        self.total_changes = 0

    def execute(self, sql: str, params=None):
        tsql = translate(sql, params)
        is_insert = sql.lstrip()[:6].upper() == "INSERT"
        cur = self._pg.cursor()
        if params is None:
            cur.execute(tsql)
        else:
            cur.execute(tsql, params)
        try:
            rc = cur.rowcount
            if rc and rc > 0:
                self.total_changes += rc
        except Exception:  # noqa: BLE001
            pass
        wrap = _Cursor(cur)
        if is_insert:
            # 用 savepoint 包裹 lastval() 探测：无序列的表 lastval 会报错，
            # 若不隔离会污染整个事务导致后续语句 InFailedSqlTransaction。
            try:
                with self._pg.transaction():
                    c2 = self._pg.cursor()
                    c2.execute("SELECT lastval() AS v")
                    row = c2.fetchone()
                    wrap.lastrowid = row["v"] if row else None
                    c2.close()
            except Exception:  # noqa: BLE001 — 该表无序列/未产生序列值，置空即可
                wrap.lastrowid = None
        return wrap

    def executemany(self, sql: str, seq_params):
        seq = list(seq_params)
        if not seq:
            return _Cursor(self._pg.cursor())
        tsql = translate(sql, seq[0])
        cur = self._pg.cursor()
        cur.executemany(tsql, seq)
        return _Cursor(cur)

    def cursor(self):
        return self._pg.cursor()

    def commit(self):
        self._pg.commit()

    def rollback(self):
        self._pg.rollback()

    def close(self):
        try:
            self._pg.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._pg.commit()
            else:
                self._pg.rollback()
        finally:
            self.close()  # 对齐「每次 _conn() 开短连接」的用法，用完即关，防连接泄漏
        return False
