#!/usr/bin/env python3
"""贾维斯 JARVIS — 交易订单邮件提醒（按笔配置）。

每一笔自创订单（手动挂单 / 计划生成单 / 持仓）可单独配置：
  - 接收通知的邮箱（单个）
  - 通知类型：止盈通知 / 止损通知（可单选、可全选）

订单触发止盈(take)/止损(stop)平仓时，按该笔配置发送邮件。发件账号复用
`jarvis_price_alert` 的全局 SMTP 配置（设置页「价位提醒」已有配置 UI）。

order_id 约定（TEXT，跨订单形态通用）：
  - "order-<limit_order_id>" : limit_orders 挂单（成交前配置）
  - "pos-<position_id>"      : paper_positions 持仓
  - 其它稳定字符串           : 计划生成自创订单等外部模型（agent 协作约定）

触发查找顺序（notify_position_closed）：
  1. pos-<position_id> 直查
  2. 挂单成交转持仓的单：limit_orders.position_id 反查 → order-<id>
  （因此挂单阶段配置的通知，成交后无需迁移即可命中。）

存储：~/.vibe-trading/jarvis_journal.db（order_notify_config 表）。

作为库使用：
  from jarvis_order_notify import (
      set_config, get_config, delete_config, list_configs,
      config_for_position, notify_position_closed, send_test_email,
  )

命令行（联网前本地验证）：
  python jarvis_order_notify.py list
  python jarvis_order_notify.py set pos-1 a@x.com --tp --sl
  python jarvis_order_notify.py test pos-1 --dry-run
"""

from __future__ import annotations

import json
import threading
import time

import jarvis_journal as jj
import jarvis_price_alert as jpa

NOTIFY_TAKE = "take"
NOTIFY_STOP = "stop"

_LOCK = threading.RLock()


# ─────────────────────────── 表结构 ───────────────────────────

def init_db() -> None:
    """在 jarvis_journal.db 中建订单通知配置表（幂等）。"""
    jj.init_db()
    with jj._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_notify_config (
                order_id           TEXT PRIMARY KEY,
                email              TEXT    NOT NULL,
                notify_take_profit INTEGER NOT NULL DEFAULT 1,
                notify_stop_loss   INTEGER NOT NULL DEFAULT 1,
                created_at         REAL    NOT NULL,
                updated_at         REAL    NOT NULL,
                last_notified_at   REAL,
                last_notify_type   TEXT,
                last_send_result   TEXT
            )
            """
        )


def _row_to_config(row) -> dict:
    return {
        "order_id": row["order_id"],
        "email": row["email"],
        "notify_take_profit": bool(row["notify_take_profit"]),
        "notify_stop_loss": bool(row["notify_stop_loss"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_notified_at": row["last_notified_at"],
        "last_notify_type": row["last_notify_type"],
        "last_send_result": row["last_send_result"],
    }


# ─────────────────────────── 配置 CRUD ───────────────────────────

def _valid_email(email: str) -> bool:
    e = str(email or "").strip()
    return bool(e) and "@" in e and "." in e.split("@")[-1]


def set_config(order_id: str, email: str,
               notify_take_profit: bool = True,
               notify_stop_loss: bool = True) -> dict:
    """新建/覆盖一笔订单的通知配置。email 非法直接拒绝。"""
    oid = str(order_id or "").strip()
    email = str(email or "").strip()
    if not oid:
        return {"ok": False, "reason": "order_id 不能为空"}
    if not _valid_email(email):
        return {"ok": False, "reason": "邮箱格式不正确"}
    now = time.time()
    with _LOCK:
        init_db()
        with jj._conn() as conn:
            conn.execute(
                """
                INSERT INTO order_notify_config
                  (order_id, email, notify_take_profit, notify_stop_loss,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                  email = excluded.email,
                  notify_take_profit = excluded.notify_take_profit,
                  notify_stop_loss = excluded.notify_stop_loss,
                  updated_at = excluded.updated_at
                """,
                (oid, email,
                 1 if notify_take_profit else 0,
                 1 if notify_stop_loss else 0,
                 now, now),
            )
    return {"ok": True, "config": get_config(oid)}


def get_config(order_id: str) -> dict | None:
    init_db()
    with jj._conn() as conn:
        row = conn.execute(
            "SELECT * FROM order_notify_config WHERE order_id = ?",
            (str(order_id),),
        ).fetchone()
    return _row_to_config(row) if row else None


def delete_config(order_id: str) -> dict:
    with _LOCK:
        init_db()
        with jj._conn() as conn:
            cur = conn.execute(
                "DELETE FROM order_notify_config WHERE order_id = ?",
                (str(order_id),),
            )
            deleted = cur.rowcount
    return {"ok": True, "deleted": int(deleted or 0)}


def list_configs() -> list:
    init_db()
    with jj._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM order_notify_config ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_config(r) for r in rows]


def config_for_position(position_id: int) -> dict | None:
    """按持仓 id 找通知配置：先 pos-<id> 直查，再经 limit_orders 反查挂单配置。"""
    cfg = get_config(f"pos-{position_id}")
    if cfg:
        return cfg
    try:
        import jarvis_wallet as jw
        jw.init_db()  # 幂等；新装环境 limit_orders 表可能还未建
        with jj._conn() as conn:
            row = conn.execute(
                "SELECT id FROM limit_orders WHERE position_id = ?",
                (int(position_id),),
            ).fetchone()
    except Exception:  # noqa: BLE001 — 反查失败视为无挂单配置
        return None
    if row:
        return get_config(f"order-{row['id']}")
    return None


def _mark_sent(order_id: str, notify_type: str, result: str) -> None:
    with jj._conn() as conn:
        conn.execute(
            """
            UPDATE order_notify_config
            SET last_notified_at = ?, last_notify_type = ?, last_send_result = ?
            WHERE order_id = ?
            """,
            (time.time(), notify_type, result, str(order_id)),
        )


# ─────────────────────────── 邮件内容 ───────────────────────────

def _fmt_num(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f >= 1:
        return f"{f:,.2f}"
    if f >= 0.01:
        return f"{f:.4f}"
    return f"{f:.6g}"


def _format_close_mail(pos: dict, exit_price: float, reason: str,
                       pnl_usdt=None, pnl_pct=None) -> tuple[str, str]:
    sym = pos.get("symbol") or "—"
    side = (pos.get("side") or "buy").lower()
    side_cn = "空单" if side == "sell" else "多单"
    kind_cn = "止盈" if reason == NOTIFY_TAKE else "止损"
    pid = pos.get("id")

    subject = f"【贾维斯交易提醒】{sym} {side_cn}{kind_cn}触发"
    pnl_line = "盈亏：—"
    if pnl_pct is not None or pnl_usdt is not None:
        sign = "+" if (pnl_pct or 0) >= 0 else ""
        pnl_line = (f"盈亏：{sign}{_fmt_num(pnl_pct)}%"
                    f"（{sign}{_fmt_num(pnl_usdt)} USDT）")
    lines = [
        f"订单 #{pid} 已{kind_cn}平仓",
        "",
        f"币种：{sym}（{side_cn}）",
        f"入场价：{_fmt_num(pos.get('entry_price'))}",
        f"触发价：{_fmt_num(exit_price)}",
        f"止损价：{_fmt_num(pos.get('stop_loss'))}",
        f"止盈价：{_fmt_num(pos.get('take_profit'))}",
        f"数量：{_fmt_num(pos.get('qty'))}",
        pnl_line,
        f"触发时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "— 贾维斯桌面交易终端 自动发送",
    ]
    return subject, "\n".join(lines)


# ─────────────────────────── 触发发送 ───────────────────────────

def notify_position_closed(pos: dict, exit_price: float, reason: str,
                           pnl_usdt=None, pnl_pct=None,
                           dry_run: bool = False) -> dict:
    """持仓平仓后的邮件通知钩子（jarvis_paper_trader._close_position 调用）。

    只处理 take/stop 两种平仓原因；未配置、类型未勾选、发送失败都不外抛，
    返回摘要 dict 供日志记录。
    """
    if reason not in (NOTIFY_TAKE, NOTIFY_STOP):
        return {"sent": False, "skipped": f"平仓原因 {reason} 不触发通知"}
    pid = pos.get("id")
    if pid is None:
        return {"sent": False, "skipped": "持仓缺 id"}
    cfg = config_for_position(pid)
    if not cfg:
        return {"sent": False, "skipped": "该单未配置邮件提醒"}
    want = (cfg.get("notify_take_profit") if reason == NOTIFY_TAKE
            else cfg.get("notify_stop_loss"))
    if not want:
        kind_cn = "止盈" if reason == NOTIFY_TAKE else "止损"
        return {"sent": False, "skipped": f"该单未勾选{kind_cn}通知"}

    subject, body = _format_close_mail(pos, exit_price, reason,
                                       pnl_usdt=pnl_usdt, pnl_pct=pnl_pct)
    send = jpa.send_email(subject, body, [cfg["email"]], dry_run=dry_run)
    result = "ok" if send.get("ok") else str(send.get("reason") or "发送失败")[:200]
    if not dry_run:
        _mark_sent(cfg["order_id"], reason, result)
    return {"sent": bool(send.get("ok")), "to": cfg["email"],
            "notify_type": reason, "reason": send.get("reason")}


def send_test_email(order_id: str, dry_run: bool = False) -> dict:
    """对某笔配置发一封样例邮件，验证该单通知链路（SMTP + 收件邮箱）可用。"""
    cfg = get_config(order_id)
    if not cfg:
        return {"ok": False, "reason": "该订单未配置邮件提醒"}
    types = []
    if cfg.get("notify_take_profit"):
        types.append("止盈")
    if cfg.get("notify_stop_loss"):
        types.append("止损")
    subject = "【贾维斯交易提醒】订单通知测试邮件"
    body = "\n".join([
        f"这是订单 {cfg['order_id']} 的通知测试邮件。",
        "",
        f"收件邮箱：{cfg['email']}",
        f"已开启通知：{('、'.join(types)) or '（未勾选任何类型，触发时不会发信）'}",
        "收到本邮件说明 SMTP 配置与收件邮箱可用，触发止盈/止损时将按上述配置提醒。",
        "",
        "— 贾维斯桌面交易终端 自动发送",
    ])
    out = jpa.send_email(subject, body, [cfg["email"]], dry_run=dry_run)
    if not dry_run:
        _mark_sent(cfg["order_id"], "test",
                   "ok" if out.get("ok") else str(out.get("reason") or "发送失败")[:200])
    return out


# ─────────────────────────── CLI（本地验证用） ───────────────────────────

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="贾维斯订单邮件提醒（按笔配置）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出全部配置")

    p_set = sub.add_parser("set", help="设置某笔订单的通知配置")
    p_set.add_argument("order_id")
    p_set.add_argument("email")
    p_set.add_argument("--tp", action="store_true", help="开止盈通知")
    p_set.add_argument("--sl", action="store_true", help="开止损通知")

    p_del = sub.add_parser("delete", help="删除某笔订单的通知配置")
    p_del.add_argument("order_id")

    p_test = sub.add_parser("test", help="发送测试邮件")
    p_test.add_argument("order_id")
    p_test.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()
    if args.cmd == "list":
        print(json.dumps(list_configs(), ensure_ascii=False, indent=2))
    elif args.cmd == "set":
        print(json.dumps(set_config(args.order_id, args.email,
                                    notify_take_profit=args.tp,
                                    notify_stop_loss=args.sl),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "delete":
        print(json.dumps(delete_config(args.order_id), ensure_ascii=False, indent=2))
    elif args.cmd == "test":
        print(json.dumps(send_test_email(args.order_id, dry_run=args.dry_run),
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
