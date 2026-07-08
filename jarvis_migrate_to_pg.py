#!/usr/bin/env python3
"""贾维斯 JARVIS — SQLite → PostgreSQL 一次性数据迁移（非破坏）。

把本地 ~/.vibe-trading/jarvis_journal.db 的全部业务表整库搬进 PostgreSQL，
并顺带把邮箱价位提醒配置（price_alert_config.json）导入 pg。

安全特性：
  - **只读源 SQLite、只写目标 pg**，从不删除或修改本地 SQLite（可随时切回）。
  - 幂等：所有 INSERT 走 ON CONFLICT DO NOTHING，可重复执行不产生重复行。
  - 迁移末尾校准 BIGSERIAL 序列，避免后续插入主键冲突。
  - 迁移后逐表打印 源行数 vs 目标行数，供人工核对。

用法：
  export JARVIS_DB_URL='postgresql://jarvis:***@127.0.0.1:5432/jarvis'
  ./.venv/bin/python jarvis_migrate_to_pg.py               # 执行迁移
  ./.venv/bin/python jarvis_migrate_to_pg.py --dry-run     # 只建表+核对，不拷贝业务数据
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

SQLITE_PATH = os.path.expanduser("~/.vibe-trading/jarvis_journal.db")

# 需要拷贝的业务表（price_alert_* 不在此列——它们由 init 时从 JSON 直接导入 pg）。
# 顺序无外键依赖，随意；带 BIGSERIAL id 的表迁移后需 setval。
TABLES = [
    "snapshots", "outcomes", "executions",
    "wallet", "wallet_ledger", "limit_orders",
    "paper_positions", "intraday_positions", "intraday_predictions",
]
SERIAL_ID_TABLES = [
    "snapshots", "wallet_ledger", "limit_orders",
    "paper_positions", "intraday_positions", "intraday_predictions",
]


def _ensure_pg_env() -> str:
    import jarvis_db as jdb
    url = jdb.db_url()
    if not jdb.use_pg():
        print("❌ 未检测到 pg 连接串。请先 export JARVIS_DB_URL=postgresql://... "
              "或写入 ~/.vibe-trading/db.json", file=sys.stderr)
        sys.exit(2)
    return url


def create_schema() -> None:
    """在 pg 里建齐所有表；price_alert_* 同时从 JSON 导入邮箱提醒配置。"""
    import jarvis_journal as jj
    import jarvis_wallet as jwallet
    import jarvis_paper_trader as jpt
    import jarvis_reconcile as jr
    import jarvis_intraday_trader as jit
    import jarvis_price_alert as jpa

    jj.init_db()
    jwallet.init_db()
    jpt.init_positions_table()
    jr.init_exec_table()
    jit.ensure_db()
    jpa.init_price_alert_db()  # 建 price_alert_* + 把 price_alert_config.json 导入 pg
    print("✅ pg 表结构已就绪（含 price_alert_* 邮箱提醒表）")


def _sqlite_columns(scur, table: str) -> list[str]:
    return [r[1] for r in scur.execute(f"PRAGMA table_info({table})").fetchall()]


def copy_table(scur, pconn, table: str) -> tuple[int, int]:
    cols = _sqlite_columns(scur, table)
    rows = scur.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    if rows:
        collist = ", ".join(cols)
        ph = ", ".join(["%s"] * len(cols))
        sql = f"INSERT INTO {table} ({collist}) VALUES ({ph}) ON CONFLICT DO NOTHING"
        with pconn.cursor() as pc:
            pc.executemany(sql, [tuple(r) for r in rows])
        pconn.commit()
    return len(rows), _pg_count(pconn, table)


def _pg_count(pconn, table: str) -> int:
    with pconn.cursor() as pc:
        pc.execute(f"SELECT COUNT(*) FROM {table}")
        return int(pc.fetchone()[0])


def fix_sequences(pconn) -> None:
    with pconn.cursor() as pc:
        for t in SERIAL_ID_TABLES:
            pc.execute(
                f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {t}), 1), "
                f"(SELECT COUNT(*) FROM {t}) > 0)"
            )
    pconn.commit()
    print("✅ 已校准 BIGSERIAL 序列（后续插入不会主键冲突）")


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯 SQLite → PostgreSQL 迁移")
    ap.add_argument("--dry-run", action="store_true",
                    help="只建表 + 核对行数，不拷贝业务数据")
    args = ap.parse_args()

    url = _ensure_pg_env()
    print(f"→ 目标 pg：{url.rsplit('@', 1)[-1]}")
    if not os.path.exists(SQLITE_PATH):
        print(f"❌ 找不到源库 {SQLITE_PATH}", file=sys.stderr)
        return 2
    print(f"→ 源 SQLite：{SQLITE_PATH}")

    create_schema()

    import psycopg
    scon = sqlite3.connect(SQLITE_PATH)
    scur = scon.cursor()
    pconn = psycopg.connect(url)

    print("\n=== 逐表迁移 ===")
    print(f"{'table':<22}{'sqlite':>8}{'pg_after':>10}")
    total_src = 0
    for t in TABLES:
        try:
            src_n = int(scur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        except sqlite3.OperationalError:
            print(f"{t:<22}{'(源无此表, 跳过)':>18}")
            continue
        total_src += src_n
        if args.dry_run:
            pg_n = _pg_count(pconn, t)
            print(f"{t:<22}{src_n:>8}{pg_n:>10}  (dry-run 未拷贝)")
            continue
        s_n, p_n = copy_table(scur, pconn, t)
        flag = "" if p_n >= s_n else "  ⚠️ 目标行数偏少"
        print(f"{t:<22}{s_n:>8}{p_n:>10}{flag}")

    if not args.dry_run:
        fix_sequences(pconn)

    # price_alert_* 汇报（数据来自 JSON 导入，不来自 sqlite）
    print("\n=== 邮箱提醒表（来自 price_alert_config.json 导入） ===")
    for t in ("price_alert_smtp", "price_alert_contacts", "price_alert_plans",
              "price_alert_settings", "price_alert_meta"):
        try:
            print(f"{t:<22}{_pg_count(pconn, t):>8} rows")
        except Exception as e:  # noqa: BLE001
            print(f"{t:<22} 读取失败: {e}")

    scon.close()
    pconn.close()
    print(f"\n✅ 迁移完成。源业务行合计 {total_src}。本地 SQLite 未改动，可随时切回。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
