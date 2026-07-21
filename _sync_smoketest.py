#!/usr/bin/env python3
"""T5 冒烟测试：贾维斯 → RuoYi MySQL 同步器（jarvis_sync + tasks_a/tasks_b）。

用法（必须用仓内 venv，系统 python3 无 pymysql/psycopg）：
  .venv/bin/python _sync_smoketest.py

用例（开发计划 §5 T5 / §6 验证方案）：
  1) 游标推进 / 重叠窗幂等：连跑两轮 --once，镜像行数不减、游标不后退，
     signal_change 与源逐 id 对账无缺口无重复
  2) kill -9 断点续传：常驻进程杀 -9 后重启 --once，无缺口无重复
  3) 删 cursors.json → 从 MySQL 心跳表三级恢复
  4) MySQL 拒连退避：错密码配置不崩、指数退避；真配置随后追平
  5) 源表缺失容忍（twelve_* 懒建场景，进程内仿真）
  6) plan_hash 判重口径 + force_order 分钟桶聚合对账（源 0 行则造样本，进程内仿真）
  7) flock 双实例拒绝

纪律：源侧零写入（只读源库对账）；仿真用例全部走临时 SQLite / 内存 fake，
不碰真实 force_orders.db；MySQL 只写本来就归同步器管的 jarvis_* 镜像表。
前置：MySQL 可达、T1 建表已执行、dashboard 7899 在线（channel B 才有增量）；
launchd 常驻实例须先卸载（否则 flock 拒绝 --once，用例 7 之外全挂）。
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv", "bin", "python")
SYNC = os.path.join(HERE, "jarvis_sync.py")
SYNC_DIR = os.path.expanduser("~/.vibe-trading/sync")
CURSORS = os.path.join(SYNC_DIR, "cursors.json")
CONFIG = os.path.join(SYNC_DIR, "sync_config.json")

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def run_once(config: str = CONFIG, timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, SYNC, "--once", "--config", config],
        capture_output=True, text=True, timeout=timeout, cwd=HERE,
    )


def mysql_conn():
    import pymysql

    with open(CONFIG, encoding="utf-8") as f:
        my = json.load(f)["mysql"]
    return pymysql.connect(
        host=my["host"], port=int(my.get("port", 3306)), user=my["user"],
        password=my["password"], database=my["database"], charset="utf8mb4",
        autocommit=True, connect_timeout=5,
    )


MIRROR_TABLES = [
    "jarvis_signal_state", "jarvis_signal_change", "jarvis_tape_bar",
    "jarvis_intraday_prediction", "jarvis_snapshot", "jarvis_outcome",
    "jarvis_position", "jarvis_limit_order", "jarvis_force_order_min",
    "jarvis_reco_plan", "jarvis_market_snapshot",
]


def mirror_counts() -> dict[str, int]:
    out = {}
    with mysql_conn() as conn, conn.cursor() as cur:
        for t in MIRROR_TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            out[t] = int(cur.fetchone()[0])
    return out


def src_change_stats(max_id: int | None = None):
    """源 twelve_signal_changes 的 (count, max_id)；max_id 传入时按 id<= 截断对账。"""
    import psycopg

    import jarvis_db

    with psycopg.connect(jarvis_db.db_url(), connect_timeout=5) as conn:
        if max_id is None:
            cur = conn.execute("SELECT COUNT(*), COALESCE(MAX(id),0) FROM twelve_signal_changes")
        else:
            cur = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id),0) FROM twelve_signal_changes WHERE id <= %s",
                (max_id,),
            )
        n, m = cur.fetchone()
        return int(n), int(m)


def mirror_change_stats(max_id: int):
    with mysql_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), COUNT(DISTINCT id) FROM jarvis_signal_change WHERE id <= %s",
            (max_id,),
        )
        n, nd = cur.fetchone()
        return int(n), int(nd)


def read_cursors() -> dict:
    try:
        with open(CURSORS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


print("=" * 62)
print("T5 冒烟 · jarvis_sync（前置：MySQL/dashboard 在线，launchd 未载）")
print("=" * 62)

# 前置自检
r = subprocess.run([PY, "-c", "import pymysql, psycopg"], capture_output=True)
check("0.venv 依赖就绪(pymysql+psycopg)", r.returncode == 0)
try:
    mirror_counts()
    check("0.MySQL 可达且镜像表已建", True)
except Exception as e:  # noqa: BLE001
    check("0.MySQL 可达且镜像表已建", False, str(e)[:80])
    print("前置失败，终止")
    sys.exit(1)

# ══════════ 1) 游标推进 / 重叠窗幂等 ══════════
print("\n── 用例1 游标推进/重叠窗幂等（两轮 --once）──")
cur_before = read_cursors()
r1 = run_once()
check("1.第一轮 --once 退出码 0", r1.returncode == 0, f"rc={r1.returncode} {r1.stderr[-120:]}")
c1 = mirror_counts()
cur_mid = read_cursors()
r2 = run_once()
check("1.第二轮 --once 退出码 0", r2.returncode == 0)
c2 = mirror_counts()
cur_after = read_cursors()

non_decreasing = all(c2[t] >= c1[t] for t in MIRROR_TABLES)
check("1.两轮后各表行数不减（允许合法新增）", non_decreasing,
      "; ".join(f"{t}:{c1[t]}→{c2[t]}" for t in MIRROR_TABLES if c2[t] != c1[t]) or "全部持平")

# 游标从不后退（数值型游标逐项比较；json 型跳过）
receded = []
for k, v_new in cur_after.items():
    v_old = cur_mid.get(k)
    if v_old is None:
        continue
    try:
        if float(v_new) < float(v_old):
            receded.append(f"{k}:{v_old}→{v_new}")
    except (TypeError, ValueError):
        pass  # json 映射型（position/reco_plan）不做数值比较
check("1.游标从不后退", not receded, "; ".join(receded))

# signal_change 逐 id 对账（截断到源当前 max_id，规避对账窗内新增）
src_n, src_max = src_change_stats()
mir_n, mir_nd = mirror_change_stats(src_max)
src_n2, _ = src_change_stats(src_max)
check("1.signal_change 无缺口（镜像=源 截至同 id）", mir_n == src_n2, f"src={src_n2} mirror={mir_n}")
check("1.signal_change 无重复（PK 唯一）", mir_n == mir_nd)

# ══════════ 2) kill -9 断点续传 ══════════
print("\n── 用例2 kill -9 断点续传 ──")
daemon = subprocess.Popen(
    [PY, SYNC, "--config", CONFIG],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=HERE,
)
time.sleep(15)  # 让 fast 组跑 ≥2 轮
alive_before = daemon.poll() is None
os.kill(daemon.pid, signal.SIGKILL)
time.sleep(1.0)
daemon.wait(timeout=5)
check("2.常驻实例启动并被 kill -9", alive_before and daemon.returncode == -signal.SIGKILL,
      f"rc={daemon.returncode}")
cur_postkill = read_cursors()
r3 = run_once()
check("2.杀后重启 --once 正常（flock 已随进程释放）", r3.returncode == 0,
      f"rc={r3.returncode} {r3.stderr[-120:]}")
src_n, src_max = src_change_stats()
mir_n, mir_nd = mirror_change_stats(src_max)
src_n2, _ = src_change_stats(src_max)
check("2.断点续传无缺口", mir_n == src_n2, f"src={src_n2} mirror={mir_n}")
check("2.断点续传无重复", mir_n == mir_nd)
cur_final = read_cursors()
receded = []
for k in ("jarvis_signal_change", "jarvis_signal_state", "jarvis_tape_bar"):
    try:
        if float(cur_final.get(k, 0)) < float(cur_postkill.get(k, 0)):
            receded.append(k)
    except (TypeError, ValueError):
        pass
check("2.杀后游标未后退", not receded, ",".join(receded))

# ══════════ 3) 删 cursors.json → 心跳表三级恢复 ══════════
print("\n── 用例3 删 cursors.json → MySQL 心跳恢复 ──")
backup = read_cursors()
os.remove(CURSORS)
r4 = run_once()
check("3.删游标后 --once 退出码 0", r4.returncode == 0)
recovered_log = "游标已从 MySQL 心跳表恢复" in (r4.stderr or "")
check("3.日志确认从心跳表恢复", recovered_log, "" if recovered_log else "（未见恢复日志）")
cur_rec = read_cursors()
check("3.cursors.json 已重建且非空", bool(cur_rec))
try:
    ok_monotonic = float(cur_rec.get("jarvis_signal_change", 0)) >= float(
        backup.get("jarvis_signal_change", 0)
    )
except (TypeError, ValueError):
    ok_monotonic = False
check("3.恢复游标 ≥ 删除前（心跳含最新值）", ok_monotonic,
      f"{backup.get('jarvis_signal_change')} → {cur_rec.get('jarvis_signal_change')}")
src_n, src_max = src_change_stats()
mir_n, mir_nd = mirror_change_stats(src_max)
check("3.恢复后对账无缺口无重复", mir_n == src_change_stats(src_max)[0] and mir_n == mir_nd)

# ══════════ 4) MySQL 拒连退避 ══════════
print("\n── 用例4 MySQL 拒连指数退避 ──")
with open(CONFIG, encoding="utf-8") as f:
    bad_cfg = json.load(f)
bad_cfg["mysql"]["password"] = "definitely-wrong-password"
bad_cfg["log"] = {"path": os.path.join(tempfile.gettempdir(), "_sync_badpw.log"),
                  "max_mb": 5, "backups": 1}
bad_path = os.path.join(tempfile.gettempdir(), "_sync_badpw_config.json")
with open(bad_path, "w", encoding="utf-8") as f:
    json.dump(bad_cfg, f)
cur_snapshot = read_cursors()
r5 = run_once(config=bad_path)
err_out = (r5.stderr or "") + (r5.stdout or "")
check("4.错密码 --once 不崩（退出码 0）", r5.returncode == 0, f"rc={r5.returncode}")
check("4.日志含连接失败+退避", "MySQL 连接失败" in err_out and "重试" in err_out)
check("4.拒连期间游标未动", read_cursors() == cur_snapshot)
r6 = run_once()
check("4.真配置随后追平（退出码 0）", r6.returncode == 0)
os.remove(bad_path)

# ══════════ 5) 源表缺失容忍（进程内仿真） ══════════
print("\n── 用例5 源表缺失容忍（twelve_* 懒建仿真）──")
sys.path.insert(0, HERE)
import jarvis_sync_tasks_a as ta  # noqa: E402


class _FakeMySQL:
    def get(self):
        return object()  # 缺表路径在连源库时就返回，不会用到


class _FakeCursors:
    def __init__(self):
        self.data = {}

    def get(self, k):
        return self.data.get(k)

    def set(self, k, v):
        self.data[k] = v


class _Ctx:
    config = {"batch_size": 100, "exec_batch": 50}
    mysql = _FakeMySQL()
    cursors = _FakeCursors()
    symbols = ["BTCUSDT"]


_empty_db = os.path.join(tempfile.gettempdir(), "_sync_empty_src.db")
if os.path.exists(_empty_db):
    os.remove(_empty_db)
sqlite3.connect(_empty_db).close()  # 空库：任何表都 no such table
_orig_src_conn = ta._src_conn
ta._src_conn = lambda: sqlite3.connect(_empty_db)
try:
    res = ta.sync_signal_state(_Ctx())
    check("5.缺表返回 rows=0 且不算失败", res.rows == 0 and res.error is None,
          f"rows={res.rows} err={res.error}")
    res2 = ta.sync_signal_change(_Ctx())
    check("5.追平型任务缺表同样容忍", res2.rows == 0 and res2.error is None)
finally:
    ta._src_conn = _orig_src_conn
    os.remove(_empty_db)

# ══════════ 6) plan_hash 判重 + 分钟桶聚合对账（仿真） ══════════
print("\n── 用例6 plan_hash 判重 / force_order 分钟桶对账 ──")
import jarvis_sync_tasks_b as tb  # noqa: E402

plan = {"side": "long", "entry_zone": [100.0, 101.0], "stop_loss": 98.0,
        "take_profit_1": 105.0, "take_profit_2": None, "position_pct": 12.5}
h1 = tb._plan_hash(plan, "ok")
h2 = tb._plan_hash(dict(plan), "ok")
h3 = tb._plan_hash({**plan, "stop_loss": 97.5}, "ok")
h_none_neutral = tb._plan_hash(None, "neutral")
h_none_watch = tb._plan_hash(None, "watch")
check("6.同计划同 hash（稳定）", h1 == h2 and len(h1) == 32)
check("6.改点位 hash 变化", h3 != h1)
check("6.无计划 neutral≠watch（state 并入防吞）", h_none_neutral != h_none_watch)

# 分钟桶聚合：临时 SQLite 造 5 笔强平（跨 2 桶 2 币），fake mysql 截获聚合行
_force_db = os.path.join(tempfile.gettempdir(), "_sync_force_sample.db")
if os.path.exists(_force_db):
    os.remove(_force_db)
fc = sqlite3.connect(_force_db)
fc.execute("CREATE TABLE force_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, "
           "side TEXT, price REAL, qty REAL, avg_price REAL, status TEXT, "
           "trade_time INTEGER, notional REAL, raw TEXT)")
BASE_MIN = (int(time.time() * 1000) // 60000) - 10  # 10 分钟前（已完整过去）
rowsx = [
    ("BTCUSDT", "BUY",  100.0, 1.0, 100.0, "F", BASE_MIN * 60000 + 1000,  100.0),
    ("BTCUSDT", "SELL", 100.0, 2.0, 100.0, "F", BASE_MIN * 60000 + 2000,  200.0),
    ("BTCUSDT", "SELL", 100.0, 0.5, 100.0, "F", BASE_MIN * 60000 + 59000, 50.0),
    ("BTCUSDT", "BUY",  100.0, 3.0, 100.0, "F", (BASE_MIN + 1) * 60000 + 500, 300.0),
    ("ETHUSDT", "SELL", 10.0,  4.0, 10.0,  "F", BASE_MIN * 60000 + 3000,  40.0),
]
fc.executemany("INSERT INTO force_orders (symbol,side,price,qty,avg_price,status,"
               "trade_time,notional) VALUES (?,?,?,?,?,?,?,?)", rowsx)
fc.commit()
fc.close()


class _CapCursor:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def executemany(self, sql, rows):
        self.sink.extend(rows)
        return len(rows)


class _CapConn:
    def __init__(self):
        self.rows = []

    def cursor(self):
        return _CapCursor(self.rows)

    def commit(self):
        pass


class _CapMySQL:
    def __init__(self):
        self.conn = _CapConn()

    def get(self):
        return self.conn


def _fake_force_conn():
    conn = sqlite3.connect(_force_db)
    conn.row_factory = sqlite3.Row  # 对齐真实 _force_conn 的行访问方式
    return conn


_orig_force_conn = ta._force_conn
ta._force_conn = _fake_force_conn
try:
    ctx6 = _Ctx()
    ctx6.mysql = _CapMySQL()
    ctx6.cursors = _FakeCursors()
    res6 = ta.sync_force_order_min(ctx6)
    got = {(r[0], r[1]): r for r in ctx6.mysql.conn.rows}
    b0 = got.get(("BTCUSDT", ta._dt8(BASE_MIN * 60.0)))
    b1 = got.get(("BTCUSDT", ta._dt8((BASE_MIN + 1) * 60.0)))
    e0 = got.get(("ETHUSDT", ta._dt8(BASE_MIN * 60.0)))
    check("6.聚合桶数=3", len(got) == 3, f"got={len(got)} rows={res6.rows}")
    check("6.BTC桶0 cnt=3 buy=1 sell=2 qty=3.5 notional=350",
          bool(b0) and b0[2] == 3 and b0[3] == 1 and b0[4] == 2
          and abs(b0[5] - 3.5) < 1e-9 and abs(b0[6] - 350.0) < 1e-9,
          str(b0))
    check("6.BTC桶1 cnt=1 buy=1 notional_max=300",
          bool(b1) and b1[2] == 1 and b1[3] == 1 and abs(b1[7] - 300.0) < 1e-9)
    check("6.ETH桶 cnt=1 sell=1 qty=4", bool(e0) and e0[2] == 1 and e0[4] == 1
          and abs(e0[5] - 4.0) < 1e-9)
    check("6.游标推进到最大 id=5", ctx6.cursors.get("jarvis_force_order_min") == "5")
finally:
    ta._force_conn = _orig_force_conn
    os.remove(_force_db)

# ══════════ 7) flock 双实例拒绝 ══════════
print("\n── 用例7 flock 双实例拒绝 ──")
daemon2 = subprocess.Popen(
    [PY, SYNC, "--config", CONFIG],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=HERE,
)
time.sleep(3)
r7 = run_once(timeout=60)
msg = (r7.stderr or "") + (r7.stdout or "")
check("7.第二实例被拒（退出码 1）", r7.returncode == 1, f"rc={r7.returncode}")
check("7.提示另一实例运行中", "另一个 jarvis_sync 实例" in msg)
daemon2.terminate()
try:
    daemon2.wait(timeout=10)
except subprocess.TimeoutExpired:
    daemon2.kill()
check("7.常驻实例 SIGTERM 优雅退出", daemon2.returncode in (0, -signal.SIGTERM),
      f"rc={daemon2.returncode}")

# ══════════ 汇总 ══════════
print("\n" + "=" * 62)
if fails:
    print(f"FAILED {len(fails)} 项: " + "; ".join(fails))
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
