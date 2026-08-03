#!/usr/bin/env python3
"""模拟交易器同步链路冒烟（子任务2：jarvis_sync_tasks_sim 离线仿真）。

用法：
  .venv/bin/python _sim_sync_smoketest.py   # 或 python3（不依赖 pymysql/psycopg）

用例（对齐 _sync_smoketest.py 用例5/6 的进程内仿真手法；全程 mock sink +
临时 SQLite，不连真实 MySQL / 不碰真实源库，零写入生产数据。源表建表语句
逐字取自 jarvis_twelve_trader.init_db() 已落地版本）：
  1) 源表暂缺容忍：twelve_sim_*（trader 未运行）缺表 → rows=0 且不算失败
  2) 钱包全量 upsert：行数、system→system_code 映射、updated_ts→src_updated_ts、lag
  3) 持仓映射：entry_ts→DATETIME(3)、unrealized_pnl_pct 按 pnl/margin*100 推导
  4) 持仓镜像 status：open→closed 再跑，整行覆盖捕获到新状态
  5) 成交流水 id 游标：增量推进、重跑幂等（0 新行）、断网游标不动、
     holding_minutes REAL→INT 取整、src_ts=exit_ts
  5.5) 点位跟随变更日志 id 游标：全量/幂等/增量、ts→log_time DATETIME(3)、
     applied INT 直传、position_id 关联
  6) 配置回读：懒建 + 业务键 upsert（含 NULL scope / trader 自插行命中更新）、
     重跑幂等、enabled '0'→0、MySQL 不可达静默保留旧配置、远端表未建静默容忍
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import jarvis_sync_tasks_sim as ts  # noqa: E402

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ══════════════════════════════════════ mock 件（对齐 _sync_smoketest 手法）


class _CapCursor:
    def __init__(self, sink, broken: bool = False):
        self.sink = sink
        self.broken = broken

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def executemany(self, sql, rows):
        if self.broken:
            raise ConnectionError("simulated mysql down mid-batch")
        self.sink.append((sql, list(rows)))
        return len(rows)

    def execute(self, sql, params=None):
        raise AssertionError("镜像任务不应对 MySQL 发单条 execute")


class _CapConn:
    def __init__(self):
        self.batches: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0
        self.broken = False

    def cursor(self):
        return _CapCursor(self.batches, self.broken)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _CapMySQL:
    def __init__(self):
        self.conn = _CapConn()

    def get(self):
        return self.conn


class _DownMySQL:
    def get(self):
        return None


class _FakeCursors:
    def __init__(self):
        self.data = {}

    def get(self, k):
        return self.data.get(k)

    def set(self, k, v):
        self.data[k] = v


class _Ctx:
    def __init__(self, mysql=None):
        self.config = {"batch_size": 100, "exec_batch": 50}
        self.mysql = mysql or _CapMySQL()
        self.cursors = _FakeCursors()
        self.symbols = ["BTCUSDT"]
        self.dry_run = False


def _rows_of(conn: _CapConn) -> list[tuple]:
    out = []
    for _sql, rows in conn.batches:
        out.extend(rows)
    return out


def _tmp_db(name: str) -> str:
    path = os.path.join(tempfile.gettempdir(), name)
    if os.path.exists(path):
        os.remove(path)
    return path


def _patch_local(path: str):
    def _conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    ts._local_db = _conn


_ORIG_LOCAL_DB = ts._local_db

print("=" * 62)
print("模拟交易器同步冒烟（离线仿真：mock MySQL sink + 临时 SQLite）")
print("=" * 62)

NOW = time.time()

# ══════════ 1) 源表暂缺容忍 ══════════
print("\n── 用例1 源表暂缺容忍（trader 未运行仿真）──")
empty_db = _tmp_db("_sim_sync_empty.db")
sqlite3.connect(empty_db).close()  # 空库：任何表都 no such table
_patch_local(empty_db)
try:
    for fn, name in ((ts.sync_sim_wallet, "wallet"), (ts.sync_sim_position, "position"),
                     (ts.sync_sim_trade, "trade"),
                     (ts.sync_sim_signal_log, "signal_log")):
        ts._missing_warned.clear()
        res = fn(_Ctx())
        check(f"1.{name} 缺表 rows=0 且不算失败", res.rows == 0 and res.error is None,
              f"rows={res.rows} err={res.error}")
finally:
    os.remove(empty_db)

# ══════════ 源库样本（建表语句逐字取自 jarvis_twelve_trader.init_db）══════════
src_db = _tmp_db("_sim_sync_src.db")
sc = sqlite3.connect(src_db)
sc.executescript(f"""
CREATE TABLE twelve_sim_wallet (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT NOT NULL, tf TEXT NOT NULL, system TEXT NOT NULL,
    name_cn          TEXT,
    principal        REAL NOT NULL DEFAULT 100,
    balance          REAL NOT NULL DEFAULT 100,
    equity           REAL NOT NULL DEFAULT 100,
    total_trades     INTEGER NOT NULL DEFAULT 0,
    win_trades       INTEGER NOT NULL DEFAULT 0,
    total_pnl        REAL NOT NULL DEFAULT 0,
    win_rate         REAL, profit_factor REAL, max_drawdown_pct REAL,
    updated_ts       REAL,
    UNIQUE (symbol, tf, system));
INSERT INTO twelve_sim_wallet VALUES
  (1,'BTCUSDT','1h','turtle','海龟',100,95.5,97.2,10,6,-4.5,60.0,1.35,8.2,{NOW - 30}),
  (2,'BTCUSDT','4h','chanlun','缠论',100,120,120,5,4,20,80.0,3.1,2.0,{NOW - 90});
CREATE TABLE twelve_sim_position (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL, tf TEXT NOT NULL, system TEXT NOT NULL,
    direction      TEXT NOT NULL,
    entry_price    REAL NOT NULL,
    entry_ts       REAL NOT NULL,
    qty            REAL NOT NULL,
    margin         REAL NOT NULL,
    leverage       REAL NOT NULL DEFAULT 1,
    position_pct   REAL, stop_loss REAL, take_profit REAL,
    cur_price      REAL, unrealized_pnl REAL,
    status         TEXT NOT NULL DEFAULT 'open');
INSERT INTO twelve_sim_position VALUES
  (1,'BTCUSDT','1h','turtle','long',60000,{NOW - 600},0.01,60,10,10,59000,62000,60500,5,'open');
CREATE TABLE twelve_sim_trade (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL, tf TEXT NOT NULL, system TEXT NOT NULL,
    name_cn         TEXT,
    direction       TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    entry_ts        REAL NOT NULL,
    exit_price      REAL NOT NULL,
    exit_ts         REAL NOT NULL,
    qty             REAL NOT NULL,
    margin          REAL NOT NULL,
    leverage        REAL NOT NULL DEFAULT 1,
    stop_loss       REAL, take_profit REAL,
    exit_reason     TEXT NOT NULL,
    pnl             REAL NOT NULL,
    pnl_pct         REAL, rr REAL, balance_after REAL,
    holding_minutes REAL);
INSERT INTO twelve_sim_trade VALUES
  (1,'BTCUSDT','1h','turtle','海龟','long',60000,{NOW - 7200},61000,{NOW - 3600},
   0.01,60,10,59000,62000,'tp',10,16.67,2.0,110,60.4),
  (2,'BTCUSDT','4h','gann','江恩','short',61000,{NOW - 7000},60500,{NOW - 3500},
   0.02,120,10,62000,60000,'sl',-10,-8.33,-1.0,90,58.6);
CREATE TABLE twelve_sim_signal_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    symbol       TEXT NOT NULL, tf TEXT NOT NULL, system TEXT NOT NULL,
    name_cn      TEXT,
    position_id  INTEGER,
    prev_entry   REAL, prev_sl REAL, prev_tp REAL,
    new_entry    REAL, new_sl REAL, new_tp REAL,
    price        REAL,
    change_kinds TEXT,
    applied      INTEGER NOT NULL DEFAULT 1,
    note         TEXT);
INSERT INTO twelve_sim_signal_log VALUES
  (1,{NOW - 300},'BTCUSDT','1h','turtle','海龟',1,60000,59000,62000,
   60200,59500,62500,60100,'sl,tp',1,NULL),
  (2,{NOW - 120},'BTCUSDT','1h','turtle','海龟',1,60000,59500,62500,
   60900,60800,63000,60100,'sl,tp',0,'新点位与现价不自洽，未应用（维持原风控点位）');
""")
sc.commit()
sc.close()
_patch_local(src_db)

# ══════════ 2) 钱包全量 upsert ══════════
print("\n── 用例2 钱包全量 upsert + 字段映射 ──")
ctx2 = _Ctx()
res2 = ts.sync_sim_wallet(ctx2)
w_rows = _rows_of(ctx2.mysql.conn)
check("2.钱包推送 2 行且无 error", res2.rows == 2 and res2.error is None,
      f"rows={res2.rows} err={res2.error}")
w1 = next((r for r in w_rows if r[0] == 1), None)
check("2.system→system_code 位次正确（turtle）", bool(w1) and w1[3] == "turtle", str(w1))
check("2.updated_ts→src_updated_ts 直传 epoch", bool(w1)
      and abs(float(w1[14]) - (NOW - 30)) < 1.0)
check("2.lag 按最新 updated_ts 估算（≈30s）",
      res2.lag_seconds is not None and 25 <= res2.lag_seconds <= 40,
      f"lag={res2.lag_seconds}")

# ══════════ 3) 持仓映射 ══════════
print("\n── 用例3 持仓映射（entry_ts→DATETIME / pnl_pct 推导）──")
ctx3 = _Ctx()
res3 = ts.sync_sim_position(ctx3)
p_rows = _rows_of(ctx3.mysql.conn)
p1 = p_rows[0] if p_rows else None
check("3.持仓推送 1 行", res3.rows == 1 and bool(p1))
check("3.entry_ts epoch→DATETIME(3) 串", bool(p1) and isinstance(p1[6], str)
      and p1[6].count(":") == 2 and "." in p1[6], str(p1[6]) if p1 else "")
check("3.unrealized_pnl_pct=pnl/margin*100（5/60→8.33）", bool(p1)
      and p1[15] is not None and abs(float(p1[15]) - 8.33) < 0.01, str(p1[15]) if p1 else "")
check("3.status=open 镜像", bool(p1) and p1[16] == "open")

# ══════════ 4) 持仓 open→closed 镜像 ══════════
print("\n── 用例4 持仓 open→closed 镜像覆盖 ──")
uc = sqlite3.connect(src_db)
uc.execute("UPDATE twelve_sim_position SET status='closed', cur_price=61000, "
           "unrealized_pnl=0 WHERE id=1")
uc.commit()
uc.close()
ctx4 = _Ctx()
res4 = ts.sync_sim_position(ctx4)
p2 = _rows_of(ctx4.mysql.conn)
check("4.再跑整行覆盖捕获 closed", res4.rows == 1 and p2 and p2[0][16] == "closed"
      and float(p2[0][13]) == 61000.0, str(p2[0]) if p2 else "")

# ══════════ 5) 成交流水 id 游标增量 ══════════
print("\n── 用例5 trade id 游标：增量/幂等/断网不后退 ──")
ctx5 = _Ctx()
res5 = ts.sync_sim_trade(ctx5)
t_rows = _rows_of(ctx5.mysql.conn)
check("5.首轮全量 2 行，游标=2", res5.rows == 2
      and ctx5.cursors.get("jarvis_sim_trade") == "2",
      f"rows={res5.rows} cur={ctx5.cursors.get('jarvis_sim_trade')}")
t1 = next((r for r in t_rows if r[0] == 1), None)
check("5.holding_minutes REAL→INT 取整（60.4→60）", bool(t1) and t1[20] == 60, str(t1))
check("5.src_ts=exit_ts 直传 epoch", bool(t1)
      and abs(float(t1[21]) - (NOW - 3600)) < 1.0)
check("5.entry/exit epoch→DATETIME(3)", bool(t1) and isinstance(t1[7], str)
      and isinstance(t1[9], str) and "." in t1[9])
res5b = ts.sync_sim_trade(ctx5)
check("5.重跑幂等 0 新行，游标不动", res5b.rows == 0
      and ctx5.cursors.get("jarvis_sim_trade") == "2")
ac = sqlite3.connect(src_db)
ac.execute(f"INSERT INTO twelve_sim_trade VALUES "
           f"(3,'BTCUSDT','1d','martingale','马丁','long',60000,{NOW - 100},60100,"
           f"{NOW - 50},0.01,60,10,59000,61000,'flip',1,1.67,0.5,111,0.8)")
ac.commit()
ac.close()
res5c = ts.sync_sim_trade(ctx5)
check("5.新增后只推增量 1 行，游标=3", res5c.rows == 1
      and ctx5.cursors.get("jarvis_sim_trade") == "3")
# 断网仿真：sink 抛错 → 任务上抛（框架记 error），游标必须不动
ctx5.mysql.conn.broken = True
ac = sqlite3.connect(src_db)
ac.execute(f"INSERT INTO twelve_sim_trade VALUES "
           f"(4,'BTCUSDT','1h','gap','缺口','short',60000,{NOW - 40},59900,{NOW - 20},"
           f"0.01,60,10,60500,59500,'timeout',1,1.67,1.0,112,0.3)")
ac.commit()
ac.close()
raised = False
try:
    ts.sync_sim_trade(ctx5)
except ConnectionError:
    raised = True
check("5.断网上抛（框架记 error 口径）且 rollback 已调",
      raised and ctx5.mysql.conn.rollbacks >= 1)
check("5.断网游标未后退未推进", ctx5.cursors.get("jarvis_sim_trade") == "3")
ctx5.mysql.conn.broken = False
res5d = ts.sync_sim_trade(ctx5)
check("5.恢复后补推 1 行，游标=4", res5d.rows == 1
      and ctx5.cursors.get("jarvis_sim_trade") == "4")

# ══════════ 5.5) 点位跟随变更日志 id 游标增量 ══════════
print("\n── 用例5.5 signal_log id 游标：全量/幂等/增量 + 字段映射 ──")
ctx55 = _Ctx()
res55 = ts.sync_sim_signal_log(ctx55)
sl_rows = _rows_of(ctx55.mysql.conn)
check("5.5.首轮全量 2 行，游标=2", res55.rows == 2
      and ctx55.cursors.get("jarvis_sim_signal_log") == "2",
      f"rows={res55.rows} cur={ctx55.cursors.get('jarvis_sim_signal_log')}")
sl1 = next((r for r in sl_rows if r[0] == 1), None)
sl2 = next((r for r in sl_rows if r[0] == 2), None)
check("5.5.ts→log_time DATETIME(3) 串 + src_ts epoch 直传", bool(sl1)
      and isinstance(sl1[1], str) and "." in sl1[1]
      and abs(float(sl1[2]) - (NOW - 300)) < 1.0, str(sl1[1:3]) if sl1 else "")
check("5.5.system→system_code + position_id 关联", bool(sl1)
      and sl1[5] == "turtle" and sl1[7] == 1, str(sl1) if sl1 else "")
check("5.5.前后点位 + change_kinds 映射", bool(sl1)
      and float(sl1[9]) == 59000.0 and float(sl1[12]) == 59500.0
      and sl1[15] == "sl,tp", str(sl1) if sl1 else "")
check("5.5.applied INT 直传（1/0）+ note", bool(sl1) and sl1[16] == 1
      and bool(sl2) and sl2[16] == 0 and sl2[17] is not None,
      f"a1={sl1 and sl1[16]} a2={sl2 and sl2[16]}")
check("5.5.lag 按最新 ts 估算（≈120s）",
      res55.lag_seconds is not None and 110 <= res55.lag_seconds <= 140,
      f"lag={res55.lag_seconds}")
res55b = ts.sync_sim_signal_log(ctx55)
check("5.5.重跑幂等 0 新行，游标不动", res55b.rows == 0
      and ctx55.cursors.get("jarvis_sim_signal_log") == "2")
lc = sqlite3.connect(src_db)
lc.execute(f"INSERT INTO twelve_sim_signal_log VALUES "
           f"(3,{NOW - 10},'BTCUSDT','4h','dow','道氏',2,61000,62000,60000,"
           f"60900,61800,59800,60950,'sl',1,NULL)")
lc.commit()
lc.close()
res55c = ts.sync_sim_signal_log(ctx55)
check("5.5.新增后只推增量 1 行，游标=3", res55c.rows == 1
      and ctx55.cursors.get("jarvis_sim_signal_log") == "3")

# ══════════ 6) 配置回读 ══════════
print("\n── 用例6 配置回读：懒建/业务键 upsert/幂等/断供静默 ──")


class _PullCursor:
    def __init__(self, rows, missing=False):
        self.rows = rows
        self.missing = missing

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, sql, params=None):
        if self.missing:
            raise RuntimeError(f"(1146, \"Table 'jiaweisi.{ts.CONFIG_TABLE}' doesn't exist\")")

    def fetchall(self):
        return self.rows


class _PullConn:
    def __init__(self, rows, missing=False):
        self.rows = rows
        self.missing = missing

    def cursor(self):
        return _PullCursor(self.rows, self.missing)


class _PullMySQL:
    def __init__(self, rows, missing=False):
        self.conn = _PullConn(rows, missing)

    def get(self):
        return self.conn


cfg_db = _tmp_db("_sim_sync_cfg.db")
# 预置 trader 已建表 + 自插一行（本地 id=1），验证业务键命中更新而非重复插入
pc = sqlite3.connect(cfg_db)
pc.executescript("""
CREATE TABLE twelve_sim_config (
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
    UNIQUE (symbol, scope_tf, scope_system));
INSERT INTO twelve_sim_config
  (symbol, scope_tf, scope_system, principal, enabled) VALUES
  ('BTCUSDT', '1h', 'turtle', 100, 1);
""")
pc.commit()
pc.close()
_patch_local(cfg_db)

# 远端行（PyMySQL 元组序 = _SQL_CONFIG_PULL 列序；远端 id 与本地无关）
remote = [
    (11, "BTCUSDT", None, None, 100.0, 10.0, 10.0, 2.0, 4.0, "1"),
    (12, "BTCUSDT", "1h", "turtle", 200.0, 5.0, 15.0, 1.5, 3.0, "1"),
]
ctx6 = _Ctx(mysql=_PullMySQL(remote))
res6 = ts.sync_sim_config_pull(ctx6)
rc = sqlite3.connect(cfg_db)
rc.row_factory = sqlite3.Row
got = rc.execute(f"SELECT * FROM {ts.LOCAL_CONFIG_TABLE} ORDER BY id").fetchall()
check("6.upsert 2 行：NULL scope 新插 + 业务键命中更新", res6.rows == 2 and len(got) == 2,
      f"rows={res6.rows} local={len(got)}")
hit = next((r for r in got if r["scope_system"] == "turtle"), None)
check("6.trader 自插行被业务键更新（principal 100→200，id 不变）",
      bool(hit) and hit["id"] == 1 and float(hit["principal"]) == 200.0, str(dict(hit)) if hit else "")
glob_row = next((r for r in got if r["scope_tf"] is None and r["scope_system"] is None), None)
check("6.全局默认行（scope 全 NULL）已落地", bool(glob_row)
      and float(glob_row["leverage"]) == 10.0)
rc.close()

remote2 = [remote[0], (12, "BTCUSDT", "1h", "turtle", 500.0, 5.0, 15.0, 1.5, 3.0, "0")]
res6b = ts.sync_sim_config_pull(_Ctx(mysql=_PullMySQL(remote2)))
rc = sqlite3.connect(cfg_db)
rc.row_factory = sqlite3.Row
got2 = rc.execute(f"SELECT * FROM {ts.LOCAL_CONFIG_TABLE} ORDER BY id").fetchall()
hit2 = next((r for r in got2 if r["scope_system"] == "turtle"), None)
check("6.重跑幂等：仍 2 行且值已更新、enabled '0'→0", len(got2) == 2 and bool(hit2)
      and float(hit2["principal"]) == 500.0 and hit2["enabled"] == 0)
rc.close()

res6c = ts.sync_sim_config_pull(_Ctx(mysql=_DownMySQL()))
check("6.MySQL 不可达：静默 rows=0 无 error", res6c.rows == 0 and res6c.error is None,
      f"cursor={res6c.cursor_value}")
rc = sqlite3.connect(cfg_db)
n_keep = rc.execute(f"SELECT COUNT(*) FROM {ts.LOCAL_CONFIG_TABLE}").fetchone()[0]
rc.close()
check("6.断供期间本地旧配置保留", n_keep == 2)

ts._missing_warned.clear()
res6d = ts.sync_sim_config_pull(_Ctx(mysql=_PullMySQL([], missing=True)))
check("6.远端表未建：静默 rows=0 无 error", res6d.rows == 0 and res6d.error is None,
      f"cursor={res6d.cursor_value}")

# 懒建路径：全新空库跑一遍 config pull，应自建表再 upsert
lazy_db = _tmp_db("_sim_sync_lazy.db")
sqlite3.connect(lazy_db).close()
_patch_local(lazy_db)
res6e = ts.sync_sim_config_pull(_Ctx(mysql=_PullMySQL(remote)))
rc = sqlite3.connect(lazy_db)
n_lazy = rc.execute(f"SELECT COUNT(*) FROM {ts.LOCAL_CONFIG_TABLE}").fetchone()[0]
rc.close()
check("6.本地表懒建 + 首轮全插", res6e.rows == 2 and n_lazy == 2)
os.remove(lazy_db)

ts._local_db = _ORIG_LOCAL_DB
os.remove(src_db)
os.remove(cfg_db)

# ══════════ 汇总 ══════════
print("\n" + "=" * 62)
if fails:
    print(f"FAILED {len(fails)} 项: " + "; ".join(fails))
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
