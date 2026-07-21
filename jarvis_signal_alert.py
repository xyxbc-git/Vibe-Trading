#!/usr/bin/env python3
"""贾维斯 JARVIS — 12 系统信号变更邮件提醒（逐信号独立开关）。

用户需求：每张信号卡片一个开关；开了的 (symbol, timeframe, system) 三元组在
发生「实质变更」（方向翻转 / 强度Δ≥0.15 / 计划价>0.2%，判定见
jarvis_signal_history.diff_signal）时发邮件；关了就不发。

链路：jarvis_signal_history.record_batch 在变更流水落库后调用
maybe_notify(events)（try/except 包裹，提醒失败绝不拖垮信号主链路）。
变更检测只在 dashboard 重算路径触发；RuoYi 同步器 api 组每 180s 拉
/api/twelve/consensus 已让该路径全天有心跳，无人看界面提醒也能发。

发信：复用 jarvis_price_alert 的全局 SMTP 配置（设置页「价位提醒」已有
配置 UI），收件箱为本模块独立的全局单邮箱（未配置时回退价位提醒通讯录）。

防轰炸：
  - 每三元组冷却 COOLDOWN_S（默认 600s，可配）：冷却期内的再次变更直接
    丢弃（计一次 suppressed，不发信）
  - 每日总上限 daily_limit（默认 50 封，可配）：当日（本地时区）达到上限
    后跳过并 warn 一次

存储：~/.vibe-trading/jarvis_journal.db（signal_alert_* 表，与
order_notify_config / price_alert_* 同库同习惯）。

作为库使用：
  from jarvis_signal_alert import (
      get_state, update_state, set_sub, maybe_notify, today_sent_count,
  )

命令行（本地验证）：
  python jarvis_signal_alert.py state
  python jarvis_signal_alert.py sub BTCUSDT 4h turtle --on
  python jarvis_signal_alert.py test --dry-run
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import jarvis_journal as jj
import jarvis_price_alert as jpa

log = logging.getLogger("jarvis_signal_alert")

# 环境变量开 dry-run（冒烟验证不真发信，send_email 返回预览）
DRY_RUN_ENV = "JARVIS_SIGNAL_ALERT_DRY"

DEFAULT_COOLDOWN_S = 600
DEFAULT_DAILY_LIMIT = 50

_LOCK = threading.RLock()
_INITED = False
_daily_limit_warned_day = ""

_DIR_CN = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}


# ─────────────────────────── 表结构 ───────────────────────────


def init_db() -> None:
    """建 signal_alert_* 三张表（幂等）：订阅 / 全局设置 / 发送日志。"""
    global _INITED
    jj.init_db()
    with jj._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_alert_subs (
                symbol       TEXT NOT NULL,
                tf           TEXT NOT NULL,
                system       TEXT NOT NULL,
                enabled      INTEGER NOT NULL DEFAULT 1,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL,
                last_sent_at REAL,
                sent_count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, tf, system)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_alert_settings (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                enabled     INTEGER NOT NULL DEFAULT 1,
                email       TEXT    NOT NULL DEFAULT '',
                cooldown_s  INTEGER NOT NULL DEFAULT 600,
                daily_limit INTEGER NOT NULL DEFAULT 50,
                updated_at  REAL    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_alert_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL NOT NULL,
                symbol  TEXT NOT NULL,
                tf      TEXT NOT NULL,
                system  TEXT NOT NULL,
                subject TEXT,
                result  TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sal_ts ON signal_alert_log(ts)"
        )
    _INITED = True


def _ensure_init() -> None:
    if not _INITED:
        init_db()


# ─────────────────────────── 设置读写 ───────────────────────────


def _load_settings(conn) -> dict:
    row = conn.execute(
        "SELECT enabled, email, cooldown_s, daily_limit FROM signal_alert_settings WHERE id = 1"
    ).fetchone()
    if row:
        return {
            "enabled": bool(row["enabled"]),
            "email": row["email"] or "",
            "cooldown_s": int(row["cooldown_s"]),
            "daily_limit": int(row["daily_limit"]),
        }
    return {
        "enabled": True,
        "email": "",
        "cooldown_s": DEFAULT_COOLDOWN_S,
        "daily_limit": DEFAULT_DAILY_LIMIT,
    }


def _today_start_ts(now: float | None = None) -> float:
    lt = time.localtime(now if now is not None else time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                        lt.tm_wday, lt.tm_yday, lt.tm_isdst))


def today_sent_count(conn=None) -> int:
    """今日（本地时区）已成功发送封数。"""
    _ensure_init()
    def q(c):
        row = c.execute(
            "SELECT COUNT(*) AS n FROM signal_alert_log WHERE ts >= ? AND result = 'ok'",
            (_today_start_ts(),),
        ).fetchone()
        return int(row["n"] if row else 0)
    if conn is not None:
        return q(conn)
    with jj._conn() as c:
        return q(c)


def get_state() -> dict:
    """全量状态：全局设置 + 订阅列表 + 今日已发封数（供 GET API）。"""
    try:
        _ensure_init()
        with jj._conn() as conn:
            settings = _load_settings(conn)
            rows = conn.execute(
                "SELECT symbol, tf, system, enabled, last_sent_at, sent_count, updated_at "
                "FROM signal_alert_subs ORDER BY updated_at DESC"
            ).fetchall()
            subs = [{
                "symbol": r["symbol"], "tf": r["tf"], "system": r["system"],
                "enabled": bool(r["enabled"]),
                "last_sent_at": r["last_sent_at"],
                "sent_count": int(r["sent_count"] or 0),
            } for r in rows]
            today = today_sent_count(conn)
        return {"ok": True, **settings, "today_sent": today, "subs": subs}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200], "subs": []}


def update_state(data: dict) -> dict:
    """合并更新全局设置：email / enabled / cooldown_s / daily_limit（按需提供）。"""
    _ensure_init()
    with _LOCK, jj._conn() as conn:
        cur = _load_settings(conn)
        if isinstance(data.get("email"), str):
            email = data["email"].strip()
            if email and ("@" not in email or "." not in email.split("@")[-1]):
                return {"ok": False, "error": "邮箱格式不正确"}
            cur["email"] = email
        if data.get("enabled") is not None:
            cur["enabled"] = bool(data["enabled"])
        if data.get("cooldown_s") is not None:
            try:
                cur["cooldown_s"] = max(0, int(data["cooldown_s"]))
            except (TypeError, ValueError):
                pass
        if data.get("daily_limit") is not None:
            try:
                cur["daily_limit"] = max(1, int(data["daily_limit"]))
            except (TypeError, ValueError):
                pass
        conn.execute(
            """
            INSERT INTO signal_alert_settings (id, enabled, email, cooldown_s, daily_limit, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              enabled = excluded.enabled, email = excluded.email,
              cooldown_s = excluded.cooldown_s, daily_limit = excluded.daily_limit,
              updated_at = excluded.updated_at
            """,
            (1 if cur["enabled"] else 0, cur["email"], cur["cooldown_s"],
             cur["daily_limit"], time.time()),
        )
    return get_state()


def set_sub(symbol: str, tf: str, system: str, enabled: bool) -> dict:
    """开/关某个三元组订阅（前端信号卡铃铛开关的落点）。"""
    sym = str(symbol or "").upper().strip()
    tf = str(tf or "").strip()
    system = str(system or "").strip()
    if not (sym and tf and system):
        return {"ok": False, "error": "symbol/tf/system 均不能为空"}
    now = time.time()
    _ensure_init()
    with _LOCK, jj._conn() as conn:
        conn.execute(
            """
            INSERT INTO signal_alert_subs (symbol, tf, system, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, tf, system) DO UPDATE SET
              enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (sym, tf, system, 1 if enabled else 0, now, now),
        )
    return {"ok": True, "sub": {"symbol": sym, "tf": tf, "system": system,
                                "enabled": bool(enabled)}}


def batch_off(subs: list) -> dict:
    """批量关闭（提醒页「已开订阅」块的批量关）。subs=[{symbol,tf,system}]。"""
    n = 0
    for s in subs or []:
        if isinstance(s, dict):
            out = set_sub(s.get("symbol", ""), s.get("tf", ""), s.get("system", ""), False)
            if out.get("ok"):
                n += 1
    return {"ok": True, "updated": n}


# ─────────────────────────── 邮件内容 ───────────────────────────


def _fmt_price(v) -> str:
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


def _format_mail(ev: dict) -> tuple[str, str]:
    """变更事件 → (subject, body)。中文模板，覆盖任务要求的全部字段。"""
    name = ev.get("name_cn") or ev.get("system") or "未知系统"
    sym = ev.get("symbol") or "—"
    tf = ev.get("tf") or "—"
    prev_dir = _DIR_CN.get(str(ev.get("prev_direction")), str(ev.get("prev_direction") or "—"))
    new_dir = _DIR_CN.get(str(ev.get("new_direction")), str(ev.get("new_direction") or "—"))
    subject = f"【贾维斯信号提醒】{sym} {tf} {name} 信号变化"
    lines = [
        f"{sym} {tf} 周期「{name}」发生实质变化：",
        "",
        f"变化内容：{ev.get('summary') or '（详见驾驶舱）'}",
        f"方向：{prev_dir} → {new_dir}",
    ]
    ps, ns = ev.get("prev_strength"), ev.get("new_strength")
    if ps is not None or ns is not None:
        try:
            lines.append(f"强度：{float(ps or 0):.0%} → {float(ns or 0):.0%}")
        except (TypeError, ValueError):
            pass
    lines += [
        f"当前价：{_fmt_price(ev.get('price'))}",
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "提示：单一系统信号变化不构成买卖建议，请结合共识面板与自身风控决策。",
        "可在驾驶舱信号卡片右上角铃铛随时关闭该提醒。",
        "",
        "— 贾维斯桌面交易终端 自动发送",
    ]
    return subject, "\n".join(lines)


# ─────────────────────────── 触发入口 ───────────────────────────


def maybe_notify(events: list[dict]) -> dict:
    """信号变更落库后的提醒钩子（jarvis_signal_history.record_batch 调用）。

    events 每项：{symbol, tf, system, name_cn, summary, prev_direction,
                  new_direction, prev_strength, new_strength, price}
    命中订阅 → 冷却/日上限过滤 → 发邮件。任何异常不外抛（返回摘要 dict）。
    """
    global _daily_limit_warned_day
    out = {"matched": 0, "sent": 0, "suppressed": 0, "failed": 0}
    if not events:
        return out
    try:
        _ensure_init()
        dry_run = os.environ.get(DRY_RUN_ENV) == "1"
        with _LOCK, jj._conn() as conn:
            settings = _load_settings(conn)
            if not settings["enabled"]:
                return out
            today_key = time.strftime("%Y-%m-%d")
            sent_today = today_sent_count(conn)

            for ev in events:
                sym = str(ev.get("symbol") or "").upper()
                tf = str(ev.get("tf") or "")
                system = str(ev.get("system") or "")
                row = conn.execute(
                    "SELECT enabled, last_sent_at FROM signal_alert_subs "
                    "WHERE symbol=? AND tf=? AND system=?",
                    (sym, tf, system),
                ).fetchone()
                if not row or not row["enabled"]:
                    continue
                out["matched"] += 1
                now = time.time()

                # 冷却：同三元组 cooldown_s 内的再次变化直接丢弃
                last = row["last_sent_at"]
                if last is not None and now - float(last) < settings["cooldown_s"]:
                    out["suppressed"] += 1
                    continue

                # 每日总上限
                if sent_today >= settings["daily_limit"]:
                    out["suppressed"] += 1
                    if _daily_limit_warned_day != today_key:
                        _daily_limit_warned_day = today_key
                        log.warning("信号提醒已达当日上限 %d 封，今日剩余变更不再发信",
                                    settings["daily_limit"])
                    continue

                # 收件人：本模块全局邮箱，未配置回退价位提醒通讯录
                to_list = [settings["email"]] if settings["email"] else []
                if not to_list:
                    try:
                        to_list = list(jpa.load_config().get("recipients") or [])
                    except Exception:  # noqa: BLE001
                        to_list = []
                if not to_list:
                    out["failed"] += 1
                    log.warning("信号提醒命中 %s %s %s 但未配置收件邮箱，跳过", sym, tf, system)
                    continue

                subject, body = _format_mail(ev)
                send = jpa.send_email(subject, body, to_list, dry_run=dry_run)
                result = "ok" if send.get("ok") else str(send.get("reason") or "发送失败")[:200]
                conn.execute(
                    "INSERT INTO signal_alert_log (ts, symbol, tf, system, subject, result) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (now, sym, tf, system, subject, result),
                )
                if send.get("ok"):
                    out["sent"] += 1
                    sent_today += 1
                    conn.execute(
                        "UPDATE signal_alert_subs SET last_sent_at=?, sent_count=sent_count+1 "
                        "WHERE symbol=? AND tf=? AND system=?",
                        (now, sym, tf, system),
                    )
                else:
                    out["failed"] += 1
                    log.warning("信号提醒发送失败 %s %s %s：%s", sym, tf, system, result)
        return out
    except Exception as e:  # noqa: BLE001 — 提醒链路绝不拖垮信号主链路
        log.warning("信号提醒处理异常：%r", e)
        out["failed"] += 1
        return out


# ─────────────────────────── CLI（本地验证用） ───────────────────────────


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="贾维斯 12 系统信号邮件提醒")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("state", help="打印全量状态")

    p_sub = sub.add_parser("sub", help="开/关某三元组订阅")
    p_sub.add_argument("symbol")
    p_sub.add_argument("tf")
    p_sub.add_argument("system")
    grp = p_sub.add_mutually_exclusive_group(required=True)
    grp.add_argument("--on", action="store_true")
    grp.add_argument("--off", action="store_true")

    p_test = sub.add_parser("test", help="用假变更事件走一遍完整链路")
    p_test.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()
    if args.cmd == "state":
        print(json.dumps(get_state(), ensure_ascii=False, indent=2))
    elif args.cmd == "sub":
        print(json.dumps(set_sub(args.symbol, args.tf, args.system, bool(args.on)),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "test":
        if args.dry_run:
            os.environ[DRY_RUN_ENV] = "1"
        ev = {"symbol": "BTCUSDT", "tf": "4h", "system": "turtle", "name_cn": "海龟交易",
              "summary": "方向 中性→看涨；强度 20%→60%", "prev_direction": "neutral",
              "new_direction": "bullish", "prev_strength": 0.2, "new_strength": 0.6,
              "price": 64500.0}
        print(json.dumps(maybe_notify([ev]), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
