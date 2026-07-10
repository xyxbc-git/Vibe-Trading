#!/usr/bin/env python3
"""贾维斯 JARVIS — 主动提醒中心（Alert Center）。

解决的用户痛点：看到系统看涨就买入，下跌后割肉，之后价格涨回去却不知情，
错过反弹再入场时机造成真实亏损。让贾维斯「主动喊话」而不是等用户来看。

三类监控规则：
  1) signal_flip  信号反转：关注币种的十二套技术共识方向变化时立即通知
                  （看涨↔看跌互翻 / 方向信号建立 / 方向信号转中性）
  2) price_level  价格关键位：涨破/跌破用户设定价位，或一键采用系统关键支撑/阻力位
                  （穿越语义：从另一侧穿越目标价才触发，避免建仓即误报）
  3) reentry      割肉后回升：标记已平仓（割肉）价位，价格重新站回该位置时提醒，
                  避免错过再入场时机

通知渠道（可扩展抽象，新渠道实现 AlertChannel 即可挂入 CHANNELS）：
  - inapp   页内通知中心（事件落库 alert_center_events，前端轮询/SSE 拉取）
  - browser 浏览器系统通知（SSE 推给前端 → Notification API 弹系统通知）
  - telegram / email 预留：分别复用 jarvis_notify 与 jarvis_price_alert.send_email，
    默认关闭，在设置里打开且配置就绪后自动生效。

存储：~/.vibe-trading/jarvis_journal.db（alert_center_* 表；经 jarvis_db 兼容层，
SQLite 默认 / PostgreSQL 可切）。提醒历史 = alert_center_events 表，可查可标已读。

作为库使用：
  from jarvis_alert_center import (
      list_rules, add_rule, update_rule, delete_rule,
      list_events, unread_count, mark_read,
      evaluate_all, start_monitor, stop_monitor, monitor_status,
      get_settings, update_settings, key_levels, subscribe, unsubscribe,
  )

命令行（联网前本地验证）：
  python jarvis_alert_center.py check --dry-run
  python jarvis_alert_center.py rules
  python jarvis_alert_center.py events
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid

import jarvis_journal as jj

try:
    import jarvis_price_alert as jpa
except Exception:  # noqa: BLE001 — 离线/单测场景允许缺失
    jpa = None  # type: ignore

KIND_SIGNAL_FLIP = "signal_flip"
KIND_PRICE_LEVEL = "price_level"
KIND_REENTRY = "reentry"
KINDS = (KIND_SIGNAL_FLIP, KIND_PRICE_LEVEL, KIND_REENTRY)

DIRECTION_ABOVE = "above"   # 涨破：价格向上穿越目标价
DIRECTION_BELOW = "below"   # 跌破：价格向下穿越目标价

_ALLOWED_TFS = ("15m", "1h", "4h", "1d")
_ALLOWED_SEVERITIES = ("info", "warning", "critical")

_DIR_CN = {"bullish": "看涨", "bearish": "看跌", "neutral": "中性"}

DEFAULT_SETTINGS: dict = {
    "poll_interval_s": 30,       # 价格类规则轮询间隔（秒）
    "signal_interval_s": 300,    # 信号类规则巡检间隔（12 套共识计算较重，独立节流）
    "channels": {"inapp": True, "browser": True, "telegram": False, "email": False},
}

_LOCK = threading.RLock()
_DB_INITIALIZED = False


# ─────────────────────────── 建表 ───────────────────────────

def init_db() -> None:
    """在 jarvis_journal.db 中建提醒中心相关表（幂等）。"""
    global _DB_INITIALIZED
    jj.init_db()
    with jj._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_center_rules (
                id                TEXT PRIMARY KEY,
                kind              TEXT    NOT NULL,
                symbol            TEXT    NOT NULL,
                enabled           INTEGER NOT NULL DEFAULT 1,
                is_repeat         INTEGER NOT NULL DEFAULT 0,
                params_json       TEXT    NOT NULL DEFAULT '{}',
                note              TEXT    NOT NULL DEFAULT '',
                created_at        REAL    NOT NULL,
                state_json        TEXT    NOT NULL DEFAULT '{}',
                last_triggered_at REAL,
                triggered_count   INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_center_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL    NOT NULL,
                rule_id       TEXT,
                kind          TEXT    NOT NULL,
                symbol        TEXT    NOT NULL,
                severity      TEXT    NOT NULL DEFAULT 'info',
                title         TEXT    NOT NULL,
                detail        TEXT    NOT NULL DEFAULT '',
                price         REAL,
                read_at       REAL,
                channels_json TEXT    NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_center_settings (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                poll_interval_s   INTEGER NOT NULL DEFAULT 30,
                signal_interval_s INTEGER NOT NULL DEFAULT 300,
                channels_json     TEXT    NOT NULL DEFAULT '{}',
                updated_at        REAL    NOT NULL
            )
            """
        )
    _DB_INITIALIZED = True


def _ensure_db() -> None:
    if not _DB_INITIALIZED:
        with _LOCK:
            if not _DB_INITIALIZED:
                init_db()


# ─────────────────────────── 设置 ───────────────────────────

def get_settings() -> dict:
    _ensure_db()
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    with jj._conn() as conn:
        row = conn.execute(
            "SELECT poll_interval_s, signal_interval_s, channels_json "
            "FROM alert_center_settings WHERE id = 1"
        ).fetchone()
    if row:
        out["poll_interval_s"] = int(row["poll_interval_s"])
        out["signal_interval_s"] = int(row["signal_interval_s"])
        try:
            ch = json.loads(row["channels_json"] or "{}")
            if isinstance(ch, dict):
                out["channels"] = {**out["channels"], **ch}
        except Exception:  # noqa: BLE001 — 渠道配置损坏回退默认
            pass
    return out


def update_settings(data: dict) -> dict:
    _ensure_db()
    cur = get_settings()
    if data.get("poll_interval_s") is not None:
        try:
            cur["poll_interval_s"] = max(10, min(3600, int(data["poll_interval_s"])))
        except (TypeError, ValueError):
            pass
    if data.get("signal_interval_s") is not None:
        try:
            cur["signal_interval_s"] = max(60, min(86400, int(data["signal_interval_s"])))
        except (TypeError, ValueError):
            pass
    if isinstance(data.get("channels"), dict):
        for k in ("inapp", "browser", "telegram", "email"):
            if k in data["channels"]:
                cur["channels"][k] = bool(data["channels"][k])
    with _LOCK, jj._conn() as conn:
        conn.execute(
            """
            INSERT INTO alert_center_settings
              (id, poll_interval_s, signal_interval_s, channels_json, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              poll_interval_s = excluded.poll_interval_s,
              signal_interval_s = excluded.signal_interval_s,
              channels_json = excluded.channels_json,
              updated_at = excluded.updated_at
            """,
            (cur["poll_interval_s"], cur["signal_interval_s"],
             json.dumps(cur["channels"], ensure_ascii=False), time.time()),
        )
    return get_settings()


# ─────────────────────────── 规则 CRUD ───────────────────────────

def _normalize_symbol(symbol: str) -> str:
    sym = str(symbol or "").upper().replace("-", "").replace("/", "").strip()
    if sym and not sym.endswith(("USDT", "USDC", "BUSD")):
        sym = sym + "USDT"
    return sym


def _row_to_rule(row) -> dict:
    try:
        params = json.loads(row["params_json"] or "{}")
    except Exception:  # noqa: BLE001
        params = {}
    try:
        state = json.loads(row["state_json"] or "{}")
    except Exception:  # noqa: BLE001
        state = {}
    rule = {
        "id": row["id"],
        "kind": row["kind"],
        "symbol": row["symbol"],
        "enabled": bool(row["enabled"]),
        "repeat": bool(row["is_repeat"]),
        "params": params,
        "note": row["note"] or "",
        "created_at": row["created_at"],
        "state": state,
        "last_triggered_at": row["last_triggered_at"],
        "triggered_count": int(row["triggered_count"] or 0),
    }
    rule["desc"] = describe_rule(rule)
    return rule


def describe_rule(rule: dict) -> str:
    """给前端展示用的一句话规则描述。"""
    p = rule.get("params", {}) or {}
    sym = str(rule.get("symbol", "")).replace("USDT", "")
    kind = rule.get("kind")
    if kind == KIND_SIGNAL_FLIP:
        mc = float(p.get("min_confidence", 0) or 0)
        return (f"{sym} 十二套共识方向变化即提醒（{p.get('tf', '4h')} 周期"
                + (f"，置信度 ≥ {mc:.0%}" if mc > 0 else "") + "）")
    if kind == KIND_PRICE_LEVEL:
        act = "涨破" if p.get("direction") != DIRECTION_BELOW else "跌破"
        label = f"（{p['label']}）" if p.get("label") else ""
        return f"{sym} {act} {p.get('target_price')}{label} 时提醒"
    if kind == KIND_REENTRY:
        side = p.get("side", "long")
        confirm = float(p.get("confirm_pct", 0) or 0)
        verb = "重新站回" if side == "long" else "重新跌回"
        extra = f"（确认幅度 {confirm}%）" if confirm else ""
        return f"{sym} {verb}你的平仓价 {p.get('exit_price')}{extra} 时提醒再入场机会"
    return f"{sym} {kind}"


def list_rules(symbol: str | None = None) -> list:
    _ensure_db()
    with jj._conn() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM alert_center_rules WHERE symbol = ? ORDER BY created_at DESC",
                (_normalize_symbol(symbol),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alert_center_rules ORDER BY created_at DESC"
            ).fetchall()
    return [_row_to_rule(r) for r in rows]


def _get_rule(rule_id: str) -> dict | None:
    _ensure_db()
    with jj._conn() as conn:
        row = conn.execute(
            "SELECT * FROM alert_center_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    return _row_to_rule(row) if row else None


def _validate_params(kind: str, data: dict) -> tuple[dict | None, str]:
    """按 kind 提取并校验参数。返回 (params, error)。"""
    if kind == KIND_SIGNAL_FLIP:
        tf = str(data.get("tf") or "4h")
        if tf not in _ALLOWED_TFS:
            tf = "4h"
        try:
            mc = max(0.0, min(1.0, float(data.get("min_confidence") or 0)))
        except (TypeError, ValueError):
            mc = 0.0
        return {"tf": tf, "min_confidence": mc}, ""
    if kind == KIND_PRICE_LEVEL:
        try:
            target = float(data.get("target_price") or 0)
        except (TypeError, ValueError):
            target = 0.0
        if target <= 0:
            return None, "目标价位必须 > 0"
        direction = (DIRECTION_BELOW if data.get("direction") == DIRECTION_BELOW
                     else DIRECTION_ABOVE)
        return {"target_price": target, "direction": direction,
                "label": str(data.get("label") or "").strip()[:60]}, ""
    if kind == KIND_REENTRY:
        try:
            exit_price = float(data.get("exit_price") or 0)
        except (TypeError, ValueError):
            exit_price = 0.0
        if exit_price <= 0:
            return None, "平仓价位必须 > 0"
        side = "short" if data.get("side") == "short" else "long"
        try:
            confirm = max(0.0, min(20.0, float(data.get("confirm_pct") or 0)))
        except (TypeError, ValueError):
            confirm = 0.0
        return {"exit_price": exit_price, "side": side, "confirm_pct": confirm}, ""
    return None, f"未知规则类型：{kind}"


def add_rule(data: dict) -> dict:
    _ensure_db()
    kind = str(data.get("kind") or "").strip()
    if kind not in KINDS:
        return {"ok": False, "reason": f"kind 须为 {'/'.join(KINDS)}"}
    sym = _normalize_symbol(data.get("symbol") or "")
    if not sym:
        return {"ok": False, "reason": "symbol 必填"}
    params, err = _validate_params(kind, data)
    if params is None:
        return {"ok": False, "reason": err}
    # 信号反转天然是持续监控（每次翻转都值得提醒）；价位/回升类默认一次性
    repeat_default = kind == KIND_SIGNAL_FLIP
    rule = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "symbol": sym,
        "enabled": bool(data.get("enabled", True)),
        "repeat": bool(data.get("repeat", repeat_default)),
        "params": params,
        "note": str(data.get("note") or "").strip()[:200],
        "created_at": time.time(),
        "state": {},
        "last_triggered_at": None,
        "triggered_count": 0,
    }
    with _LOCK, jj._conn() as conn:
        conn.execute(
            """
            INSERT INTO alert_center_rules
              (id, kind, symbol, enabled, is_repeat, params_json, note,
               created_at, state_json, last_triggered_at, triggered_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rule["id"], rule["kind"], rule["symbol"],
             1 if rule["enabled"] else 0, 1 if rule["repeat"] else 0,
             json.dumps(rule["params"], ensure_ascii=False), rule["note"],
             rule["created_at"], "{}", None, 0),
        )
    rule["desc"] = describe_rule(rule)
    return {"ok": True, "rule": rule}


def update_rule(rule_id: str, data: dict) -> dict:
    rule = _get_rule(rule_id)
    if not rule:
        return {"ok": False, "reason": "未找到该规则"}
    if any(k in data for k in ("target_price", "direction", "label", "exit_price",
                               "side", "confirm_pct", "tf", "min_confidence")):
        merged = {**rule["params"], **{k: v for k, v in data.items() if v is not None}}
        params, err = _validate_params(rule["kind"], merged)
        if params is None:
            return {"ok": False, "reason": err}
        rule["params"] = params
        rule["state"] = {}   # 参数变更重置监控基线，避免用旧基线误判穿越
    if "enabled" in data:
        rule["enabled"] = bool(data["enabled"])
    if "repeat" in data:
        rule["repeat"] = bool(data["repeat"])
    if "note" in data:
        rule["note"] = str(data["note"] or "").strip()[:200]
    if data.get("symbol"):
        new_sym = _normalize_symbol(data["symbol"])
        if new_sym and new_sym != rule["symbol"]:
            rule["symbol"] = new_sym
            rule["state"] = {}
    with _LOCK, jj._conn() as conn:
        conn.execute(
            """
            UPDATE alert_center_rules
            SET symbol = ?, enabled = ?, is_repeat = ?, params_json = ?,
                note = ?, state_json = ?
            WHERE id = ?
            """,
            (rule["symbol"], 1 if rule["enabled"] else 0, 1 if rule["repeat"] else 0,
             json.dumps(rule["params"], ensure_ascii=False), rule["note"],
             json.dumps(rule["state"], ensure_ascii=False), rule_id),
        )
    rule["desc"] = describe_rule(rule)
    return {"ok": True, "rule": rule}


def delete_rule(rule_id: str) -> dict:
    _ensure_db()
    with _LOCK, jj._conn() as conn:
        cur = conn.execute("DELETE FROM alert_center_rules WHERE id = ?", (rule_id,))
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if not deleted:
        return {"ok": False, "reason": "未找到该规则"}
    return {"ok": True}


def _save_rule_state(rule_id: str, state: dict, *, triggered: bool = False,
                     disable: bool = False) -> None:
    _ensure_db()
    with _LOCK, jj._conn() as conn:
        if triggered:
            conn.execute(
                """
                UPDATE alert_center_rules
                SET state_json = ?, last_triggered_at = ?,
                    triggered_count = triggered_count + 1,
                    enabled = CASE WHEN ? THEN 0 ELSE enabled END
                WHERE id = ?
                """,
                (json.dumps(state, ensure_ascii=False), time.time(),
                 1 if disable else 0, rule_id),
            )
        else:
            conn.execute(
                "UPDATE alert_center_rules SET state_json = ? WHERE id = ?",
                (json.dumps(state, ensure_ascii=False), rule_id),
            )


# ─────────────────────────── 事件（提醒历史 / 页内通知中心） ───────────────────────────

def _row_to_event(row) -> dict:
    try:
        channels = json.loads(row["channels_json"] or "{}")
    except Exception:  # noqa: BLE001
        channels = {}
    return {
        "id": row["id"],
        "ts": row["ts"],
        "time": time.strftime("%m-%d %H:%M:%S", time.localtime(row["ts"])),
        "rule_id": row["rule_id"],
        "kind": row["kind"],
        "symbol": row["symbol"],
        "severity": row["severity"],
        "title": row["title"],
        "detail": row["detail"] or "",
        "price": row["price"],
        "read": row["read_at"] is not None,
        "channels": channels,
    }


def add_event(kind: str, symbol: str, title: str, detail: str = "",
              severity: str = "info", rule_id: str | None = None,
              price: float | None = None) -> dict:
    """落一条提醒事件（页内通知中心数据源）。返回完整事件 dict。"""
    _ensure_db()
    sev = severity if severity in _ALLOWED_SEVERITIES else "info"
    ts = time.time()
    with _LOCK, jj._conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO alert_center_events
              (ts, rule_id, kind, symbol, severity, title, detail, price, read_at, channels_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}')
            """,
            (ts, rule_id, str(kind), _normalize_symbol(symbol) or str(symbol),
             sev, str(title)[:200], str(detail)[:2000], price),
        )
        event_id = cur.lastrowid
    return {
        "id": event_id, "ts": ts,
        "time": time.strftime("%m-%d %H:%M:%S", time.localtime(ts)),
        "rule_id": rule_id, "kind": kind, "symbol": symbol,
        "severity": sev, "title": title, "detail": detail,
        "price": price, "read": False, "channels": {},
    }


def list_events(limit: int = 50, unread_only: bool = False,
                symbol: str | None = None) -> list:
    _ensure_db()
    n = max(1, min(int(limit), 500))
    where, args = [], []
    if unread_only:
        where.append("read_at IS NULL")
    if symbol:
        where.append("symbol = ?")
        args.append(_normalize_symbol(symbol))
    sql = "SELECT * FROM alert_center_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(n)
    with jj._conn() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
    return [_row_to_event(r) for r in rows]


def unread_count() -> int:
    _ensure_db()
    with jj._conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM alert_center_events WHERE read_at IS NULL"
        ).fetchone()
    return int(row["n"] if row else 0)


def mark_read(ids: list | None = None, mark_all: bool = False) -> dict:
    _ensure_db()
    now = time.time()
    with _LOCK, jj._conn() as conn:
        if mark_all:
            cur = conn.execute(
                "UPDATE alert_center_events SET read_at = ? WHERE read_at IS NULL", (now,)
            )
        elif ids:
            clean = [int(i) for i in ids if str(i).lstrip("-").isdigit()]
            if not clean:
                return {"ok": True, "updated": 0}
            placeholders = ",".join("?" * len(clean))
            cur = conn.execute(
                f"UPDATE alert_center_events SET read_at = ? "
                f"WHERE read_at IS NULL AND id IN ({placeholders})",
                (now, *clean),
            )
        else:
            return {"ok": True, "updated": 0}
        updated = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return {"ok": True, "updated": updated}


def _set_event_channels(event_id, results: dict) -> None:
    try:
        with _LOCK, jj._conn() as conn:
            conn.execute(
                "UPDATE alert_center_events SET channels_json = ? WHERE id = ?",
                (json.dumps(results, ensure_ascii=False), event_id),
            )
    except Exception:  # noqa: BLE001 — 渠道结果记录失败不影响主流程
        pass


# ─────────────────────────── SSE 订阅（浏览器实时推送） ───────────────────────────

_SUBSCRIBERS: "list[queue.Queue]" = []
_SUB_LOCK = threading.Lock()


def subscribe(maxsize: int = 200) -> "queue.Queue":
    q: "queue.Queue" = queue.Queue(maxsize=maxsize)
    with _SUB_LOCK:
        _SUBSCRIBERS.append(q)
    return q


def unsubscribe(q) -> None:
    with _SUB_LOCK:
        try:
            _SUBSCRIBERS.remove(q)
        except ValueError:
            pass


def _broadcast(event: dict) -> int:
    """把事件推给所有 SSE 订阅者，返回送达数。满队列的订阅者视为死连接移除。"""
    dead = []
    sent = 0
    with _SUB_LOCK:
        for q in _SUBSCRIBERS:
            try:
                q.put_nowait(event)
                sent += 1
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                _SUBSCRIBERS.remove(q)
            except ValueError:
                pass
    return sent


# ─────────────────────────── 通知渠道抽象 ───────────────────────────

class AlertChannel:
    """通知渠道基类：新渠道实现 available/send 并注册进 CHANNELS 即可。"""

    name = "base"

    def available(self, settings: dict) -> bool:  # noqa: ARG002
        return True

    def send(self, event: dict) -> dict:
        raise NotImplementedError


class InAppChannel(AlertChannel):
    """页内通知中心：事件本身已落库（add_event），此渠道恒成功。"""

    name = "inapp"

    def available(self, settings: dict) -> bool:
        return bool(settings.get("channels", {}).get("inapp", True))

    def send(self, event: dict) -> dict:  # noqa: ARG002
        return {"ok": True}


class BrowserChannel(AlertChannel):
    """浏览器渠道：经 SSE 推给前端，由前端弹系统通知（Notification API）。"""

    name = "browser"

    def available(self, settings: dict) -> bool:
        return bool(settings.get("channels", {}).get("browser", True))

    def send(self, event: dict) -> dict:
        return {"ok": True, "subscribers": _broadcast(event)}


class TelegramChannel(AlertChannel):
    """Telegram 渠道（预留）：复用 jarvis_notify；设置开启 + 配置就绪才生效。"""

    name = "telegram"

    def available(self, settings: dict) -> bool:
        if not bool(settings.get("channels", {}).get("telegram", False)):
            return False
        try:
            import jarvis_notify as jn
            cfg = jn.load_config()
            return bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"))
        except Exception:  # noqa: BLE001
            return False

    def send(self, event: dict) -> dict:
        import jarvis_notify as jn
        text = f"{event['title']}\n{event.get('detail', '')}".strip()
        out = jn.notify(text, channels=["telegram"])
        return {"ok": bool(out.get("sent")), "raw": out.get("results")}


class EmailChannel(AlertChannel):
    """邮件渠道（预留）：复用价位提醒的 SMTP 配置与默认收件人；设置开启才生效。"""

    name = "email"

    def available(self, settings: dict) -> bool:
        if not bool(settings.get("channels", {}).get("email", False)):
            return False
        if jpa is None:
            return False
        try:
            cfg = jpa.load_config()
            smtp = cfg.get("smtp", {})
            return bool(smtp.get("host") and smtp.get("username")
                        and smtp.get("password") and cfg.get("recipients"))
        except Exception:  # noqa: BLE001
            return False

    def send(self, event: dict) -> dict:
        cfg = jpa.load_config()
        body = (f"{event.get('detail', '')}\n\n"
                f"触发时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(event['ts']))}\n"
                "— 贾维斯主动提醒中心 自动发送")
        return jpa.send_email(f"【贾维斯提醒】{event['title']}", body,
                              cfg.get("recipients", []), cfg=cfg)


CHANNELS: list[AlertChannel] = [InAppChannel(), BrowserChannel(),
                                TelegramChannel(), EmailChannel()]


def dispatch(event: dict, settings: dict | None = None, dry_run: bool = False) -> dict:
    """把事件分发到所有可用渠道；单渠道失败不影响其它渠道。返回各渠道结果。"""
    settings = settings or get_settings()
    results: dict = {}
    for ch in CHANNELS:
        try:
            if not ch.available(settings):
                results[ch.name] = {"skipped": True}
                continue
            if dry_run and ch.name in ("telegram", "email"):
                results[ch.name] = {"ok": True, "dry_run": True}
                continue
            results[ch.name] = ch.send(event)
        except Exception as e:  # noqa: BLE001 — 渠道故障隔离
            results[ch.name] = {"ok": False, "error": repr(e)[:200]}
    if event.get("id") is not None:
        _set_event_channels(event["id"], results)
    event["channels"] = results
    return results


# ─────────────────────────── 行情 / 信号取数（可注入，便于离线测试） ───────────────────────────

_CONS_CACHE: dict = {}          # (symbol, tf) -> (ts, data)
_CONS_CACHE_TTL = 120.0


def _default_price(symbol: str) -> float | None:
    if jpa is None:
        return None
    return jpa.current_price(symbol)


def _default_consensus(symbol: str, tf: str = "4h") -> dict | None:
    """十二套技术共识（只认已收盘 bar，避免进行中 bar 反复翻转误报）。"""
    key = (symbol, tf)
    hit = _CONS_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _CONS_CACHE_TTL:
        return hit[1]
    try:
        import jarvis_twelve_systems as jts
        df = jts.fetch_klines_df(symbol, tf, 300, drop_unclosed=True)
        if df is None or len(df) < 30:
            return None
        out = jts.analyze(df)
        cons = out["consensus"]
        data = {
            "direction": cons.get("direction", "neutral"),
            "confidence": float(cons.get("confidence", 0.0) or 0.0),
            "score": cons.get("score"),
            "reasoning": cons.get("reasoning", ""),
            "key_levels": cons.get("key_levels", []),
            "price": round(float(df["close"].iloc[-1]), 6),
        }
        _CONS_CACHE[key] = (now, data)
        return data
    except Exception:  # noqa: BLE001 — 取数失败交由调用方跳过本轮
        return None


def key_levels(symbol: str, tf: str = "4h") -> dict:
    """系统关键支撑/阻力位（十二套共识聚合），供前端「一键采用系统关键位」。"""
    sym = _normalize_symbol(symbol)
    cons = _default_consensus(sym, tf if tf in _ALLOWED_TFS else "4h")
    if not cons:
        return {"ok": False, "symbol": sym, "levels": [], "error": "K线数据不足或拉取失败"}
    price = cons.get("price")
    levels = []
    for lv in cons.get("key_levels", []):
        try:
            lv_price = float(lv.get("price"))
        except (TypeError, ValueError):
            continue
        levels.append({
            "label": str(lv.get("label", "")),
            "price": lv_price,
            "source": str(lv.get("source", "")),
            # 高于现价 = 阻力（建议涨破提醒）；低于现价 = 支撑（建议跌破提醒）
            "suggest_direction": DIRECTION_ABOVE if (price and lv_price >= price) else DIRECTION_BELOW,
        })
    return {"ok": True, "symbol": sym, "price": price,
            "direction": cons.get("direction"), "confidence": cons.get("confidence"),
            "levels": levels}


# ─────────────────────────── 触发判定 ───────────────────────────

def _crossed(direction: str, prev: float, price: float, target: float) -> bool:
    """穿越语义（与 jarvis_price_alert 同款）：从目标价另一侧穿越到目标价才触发。"""
    if direction == DIRECTION_ABOVE:
        return prev < target <= price
    return prev > target >= price


def _fmt_price(p) -> str:
    if p is None:
        return "—"
    try:
        p = float(p)
    except (TypeError, ValueError):
        return str(p)
    digits = 2 if abs(p) >= 100 else (4 if abs(p) >= 1 else 6)
    return f"{p:,.{digits}f}".rstrip("0").rstrip(".")


def _check_price_level(rule: dict, price: float) -> tuple[dict | None, dict]:
    """价格关键位：穿越目标价触发。返回 (event_payload|None, new_state)。"""
    p = rule["params"]
    state = dict(rule.get("state") or {})
    prev = state.get("last_price")
    state["last_price"] = price
    target = float(p["target_price"])
    direction = p.get("direction", DIRECTION_ABOVE)
    if prev is None or not _crossed(direction, float(prev), price, target):
        return None, state
    act = "涨破" if direction == DIRECTION_ABOVE else "跌破"
    label = f"（{p['label']}）" if p.get("label") else ""
    sym_cn = rule["symbol"].replace("USDT", "")
    title = f"{'🚀' if direction == DIRECTION_ABOVE else '📉'} {sym_cn} {act} {_fmt_price(target)}{label}"
    detail = (f"现价 {_fmt_price(price)}，已{act}你关注的关键价位 {_fmt_price(target)}"
              f"（前值 {_fmt_price(prev)}）。")
    if rule.get("note"):
        detail += f"\n备注：{rule['note']}"
    return {"title": title, "detail": detail, "severity": "warning", "price": price}, state


def _check_reentry(rule: dict, price: float) -> tuple[dict | None, dict]:
    """割肉后回升：价格重新站回（多头）/跌回（空头）平仓价位时提醒再入场。"""
    p = rule["params"]
    state = dict(rule.get("state") or {})
    prev = state.get("last_price")
    state["last_price"] = price
    exit_price = float(p["exit_price"])
    confirm = float(p.get("confirm_pct", 0) or 0)
    side = p.get("side", "long")
    if side == "long":
        target = exit_price * (1 + confirm / 100.0)
        direction = DIRECTION_ABOVE
    else:
        target = exit_price * (1 - confirm / 100.0)
        direction = DIRECTION_BELOW
    if prev is None or not _crossed(direction, float(prev), price, target):
        return None, state
    sym_cn = rule["symbol"].replace("USDT", "")
    verb = "重新站回" if side == "long" else "重新跌回"
    title = f"🔁 {sym_cn} 已{verb}你的平仓价 {_fmt_price(exit_price)}"
    detail = (f"现价 {_fmt_price(price)} 已{verb}你标记的平仓价位 {_fmt_price(exit_price)}"
              + (f"（含 {confirm}% 确认幅度，触发线 {_fmt_price(target)}）" if confirm else "")
              + "。行情已收复割肉点位，若逻辑仍看好可评估再入场，避免踏空后续行情。")
    if rule.get("note"):
        detail += f"\n备注：{rule['note']}"
    return {"title": title, "detail": detail, "severity": "critical", "price": price}, state


def _check_signal_flip(rule: dict, cons: dict) -> tuple[dict | None, dict]:
    """信号反转：共识方向变化触发（首轮只建基线）。"""
    p = rule["params"]
    state = dict(rule.get("state") or {})
    prev_dir = state.get("last_direction")
    cur_dir = cons.get("direction", "neutral")
    cur_conf = float(cons.get("confidence", 0.0) or 0.0)
    min_conf = float(p.get("min_confidence", 0) or 0)
    state["last_direction"] = cur_dir
    state["last_confidence"] = cur_conf
    state["last_signal_ts"] = time.time()

    if prev_dir is None or cur_dir == prev_dir:
        return None, state
    directional = ("bullish", "bearish")
    # 新方向为看涨/看跌时按置信度门槛降噪；转中性（信号消失）不受门槛限制。
    # 被门槛压下时基线保持原方向：置信度后续走强仍能触发本次方向变化的提醒。
    if cur_dir in directional and cur_conf < min_conf:
        state["last_direction"] = prev_dir
        return None, state

    sym_cn = rule["symbol"].replace("USDT", "")
    tf = p.get("tf", "4h")
    price = cons.get("price")
    if prev_dir in directional and cur_dir in directional:
        title = f"🔄 {sym_cn} 信号反转：{_DIR_CN[prev_dir]} → {_DIR_CN[cur_dir]}（{tf}）"
        severity = "critical"
    elif cur_dir in directional:
        title = f"📣 {sym_cn} 信号建立：中性 → {_DIR_CN[cur_dir]}（{tf}，置信度 {cur_conf:.0%}）"
        severity = "warning"
    else:
        title = f"⚠️ {sym_cn} 信号转中性：{_DIR_CN.get(prev_dir, prev_dir)} → 中性（{tf}）"
        severity = "warning"
    detail = (f"十二套技术共识方向由「{_DIR_CN.get(prev_dir, prev_dir)}」变为"
              f"「{_DIR_CN.get(cur_dir, cur_dir)}」，置信度 {cur_conf:.0%}"
              + (f"，现价 {_fmt_price(price)}" if price else "") + "。\n"
              + str(cons.get("reasoning", ""))[:400])
    if rule.get("note"):
        detail += f"\n备注：{rule['note']}"
    return {"title": title, "detail": detail, "severity": severity, "price": price}, state


# ─────────────────────────── 一轮巡检 ───────────────────────────

def evaluate_all(dry_run: bool = False, price_getter=None, consensus_getter=None,
                 force_signal: bool = False) -> dict:
    """检查全部启用规则，命中即产出事件并分发渠道。返回本轮摘要。

    price_getter / consensus_getter 可注入（离线测试）；force_signal=True 跳过
    信号巡检节流（手动「立即检查」用）。
    """
    _ensure_db()
    settings = get_settings()
    price_getter = price_getter or _default_price
    consensus_getter = consensus_getter or _default_consensus
    signal_interval = int(settings.get("signal_interval_s", 300))

    rules = [r for r in list_rules() if r["enabled"]]
    price_cache: dict = {}
    results: list[dict] = []
    checked = 0
    triggered = 0
    now = time.time()

    for rule in rules:
        try:
            payload = None
            state = None
            if rule["kind"] in (KIND_PRICE_LEVEL, KIND_REENTRY):
                checked += 1
                sym = rule["symbol"]
                if sym not in price_cache:
                    price_cache[sym] = price_getter(sym)
                price = price_cache[sym]
                if price is None:
                    results.append({"id": rule["id"], "skipped": "价格获取失败"})
                    continue
                if rule["kind"] == KIND_PRICE_LEVEL:
                    payload, state = _check_price_level(rule, float(price))
                else:
                    payload, state = _check_reentry(rule, float(price))
            elif rule["kind"] == KIND_SIGNAL_FLIP:
                last_ts = float((rule.get("state") or {}).get("last_signal_ts", 0) or 0)
                if not force_signal and now - last_ts < signal_interval:
                    continue   # 信号巡检节流：未到间隔跳过（不计入 checked）
                checked += 1
                cons = consensus_getter(rule["symbol"], rule["params"].get("tf", "4h"))
                if not cons:
                    results.append({"id": rule["id"], "skipped": "信号计算失败"})
                    continue
                payload, state = _check_signal_flip(rule, cons)
            else:
                continue

            if payload is None:
                if state is not None:
                    _save_rule_state(rule["id"], state)
                continue

            triggered += 1
            disable = not rule["repeat"]
            _save_rule_state(rule["id"], state, triggered=True, disable=disable)
            event = add_event(rule["kind"], rule["symbol"], payload["title"],
                              detail=payload["detail"], severity=payload["severity"],
                              rule_id=rule["id"], price=payload.get("price"))
            dispatch(event, settings=settings, dry_run=dry_run)
            results.append({"id": rule["id"], "event_id": event["id"],
                            "title": payload["title"], "disabled_after": disable})
        except Exception as e:  # noqa: BLE001 — 单规则异常不拖垮整轮
            results.append({"id": rule.get("id"), "error": repr(e)[:200]})

    return {"checked": checked, "triggered": triggered, "dry_run": dry_run,
            "results": results, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}


# ─────────────────────────── 后台监控线程 ───────────────────────────

_MONITOR_THREAD: threading.Thread | None = None
_MONITOR_STOP = threading.Event()
_MONITOR_STATE: dict = {"running": False, "last_run": None, "last_summary": None,
                        "last_error": None}


def _monitor_loop() -> None:
    _MONITOR_STATE["running"] = True
    while not _MONITOR_STOP.is_set():
        try:
            summary = evaluate_all(dry_run=False)
            _MONITOR_STATE["last_run"] = summary.get("ts")
            _MONITOR_STATE["last_summary"] = {
                "checked": summary["checked"], "triggered": summary["triggered"],
            }
            _MONITOR_STATE["last_error"] = None
            if summary.get("triggered"):
                print(f"🔔 提醒中心：本轮触发 {summary['triggered']} 条提醒")
        except Exception as e:  # noqa: BLE001 — 单轮失败不退出循环
            _MONITOR_STATE["last_error"] = repr(e)[:300]
        try:
            interval = max(10, int(get_settings().get("poll_interval_s", 30)))
        except Exception:  # noqa: BLE001
            interval = 30
        _MONITOR_STOP.wait(interval)
    _MONITOR_STATE["running"] = False


def start_monitor() -> dict:
    global _MONITOR_THREAD
    if _MONITOR_THREAD and _MONITOR_THREAD.is_alive():
        return monitor_status()
    _MONITOR_STOP.clear()
    _MONITOR_THREAD = threading.Thread(target=_monitor_loop, name="alert-center-monitor",
                                       daemon=True)
    _MONITOR_THREAD.start()
    return monitor_status()


def stop_monitor() -> dict:
    _MONITOR_STOP.set()
    return monitor_status()


def monitor_status() -> dict:
    return {
        "running": bool(_MONITOR_THREAD and _MONITOR_THREAD.is_alive()),
        "last_run": _MONITOR_STATE.get("last_run"),
        "last_summary": _MONITOR_STATE.get("last_summary"),
        "last_error": _MONITOR_STATE.get("last_error"),
    }


# ─────────────────────────── CLI（本地验证用） ───────────────────────────

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="贾维斯主动提醒中心")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="执行一轮巡检")
    p_check.add_argument("--dry-run", action="store_true")
    p_check.add_argument("--force-signal", action="store_true")

    sub.add_parser("rules", help="列出规则")
    sub.add_parser("events", help="列出最近提醒事件")
    sub.add_parser("settings", help="打印设置")

    p_levels = sub.add_parser("levels", help="系统关键位建议")
    p_levels.add_argument("symbol")

    args = ap.parse_args()
    if args.cmd == "check":
        print(json.dumps(evaluate_all(dry_run=args.dry_run,
                                      force_signal=args.force_signal),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "rules":
        print(json.dumps(list_rules(), ensure_ascii=False, indent=2))
    elif args.cmd == "events":
        print(json.dumps(list_events(limit=30), ensure_ascii=False, indent=2))
    elif args.cmd == "settings":
        print(json.dumps(get_settings(), ensure_ascii=False, indent=2))
    elif args.cmd == "levels":
        print(json.dumps(key_levels(args.symbol), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
