#!/usr/bin/env python3
"""贾维斯 JARVIS — 价位邮件提醒。

支持：
  - 多收件邮箱（全局默认 + 每个计划可单独指定收件人）
  - 多提醒计划（每个计划 = 币种 + 目标价位 + 方向(涨破/跌破)）
  - 价位由用户手动设定（不做 AI 推荐）
  - 后台轮询实时价格，命中目标价位即向配置的邮箱发送提醒邮件

配置存储：~/.vibe-trading/price_alert_config.json
敏感信息（SMTP 授权码）只落本地配置文件，绝不回传明文给前端。

价位判定采用「穿越」语义：先用一轮观测建立基线 last_price，之后当价格
从目标价另一侧穿越到目标价时才触发，避免「目标价设在现价另一侧时立即误触发」。

作为库使用：
  from jarvis_price_alert import (
      load_config, public_config, update_smtp, set_recipients,
      list_plans, add_plan, update_plan, delete_plan,
      send_email, evaluate_all, current_price,
      start_monitor, monitor_status,
  )

命令行（联网前本地验证）：
  python jarvis_price_alert.py price BTCUSDT
  python jarvis_price_alert.py check --dry-run
  python jarvis_price_alert.py test-email a@x.com --dry-run
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import threading
import time
import uuid
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

try:
    import jarvis_crypto_data as jcd
except Exception:  # noqa: BLE001 — 单元/离线场景下允许缺失
    jcd = None  # type: ignore

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
CONFIG_PATH = os.path.join(CONFIG_DIR, "price_alert_config.json")

DIRECTION_ABOVE = "above"  # 涨破：价格向上穿越目标价
DIRECTION_BELOW = "below"  # 跌破：价格向下穿越目标价

DEFAULTS: dict = {
    "smtp": {
        "host": "",
        "port": 465,
        "use_ssl": True,
        "username": "",
        "password": "",
        "from_name": "贾维斯价位提醒",
    },
    # recipients：默认收件邮箱（也是通讯录里的全部邮箱）；contact_labels：邮箱→备注名
    "recipients": [],
    "contact_labels": {},
    "poll_interval_s": 60,
    "plans": [],
}

_LOCK = threading.RLock()


# ─────────────────────────── 配置读写 ───────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """读取完整配置（含 SMTP 明文密码，仅供后端内部发信使用）。"""
    with _LOCK:
        cfg = json.loads(json.dumps(DEFAULTS))  # 深拷贝默认
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    cfg = _deep_merge(cfg, json.load(f) or {})
            except Exception:  # noqa: BLE001
                pass
        return cfg


def _save(cfg: dict) -> None:
    with _LOCK:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 4:
        return "•" * len(secret)
    return f"{secret[:2]}{'•' * 6}{secret[-2:]}"


def public_config() -> dict:
    """返回脱敏后的配置，供前端展示（不含 SMTP 密码明文）。"""
    cfg = load_config()
    smtp = cfg.get("smtp", {})
    return {
        "smtp": {
            "host": smtp.get("host", ""),
            "port": smtp.get("port", 465),
            "use_ssl": bool(smtp.get("use_ssl", True)),
            "username": smtp.get("username", ""),
            "from_name": smtp.get("from_name", "贾维斯价位提醒"),
            "password_masked": _mask(str(smtp.get("password", "") or "")),
            "has_password": bool(smtp.get("password")),
        },
        "recipients": list(cfg.get("recipients", [])),
        "contacts": _build_contacts(cfg),
        "poll_interval_s": int(cfg.get("poll_interval_s", 60)),
        "monitor": monitor_status(),
    }


def _build_contacts(cfg: dict) -> list:
    """把 recipients(邮箱列表) + contact_labels(备注映射) 合成通讯录给前端展示。"""
    labels = cfg.get("contact_labels", {}) or {}
    return [{"email": e, "label": str(labels.get(e, "") or "")}
            for e in cfg.get("recipients", [])]


def set_contacts(contacts: list) -> dict:
    """整体覆盖通讯录。contacts=[{email,label}]；email 去重，保留备注。"""
    emails: list[str] = []
    labels: dict[str, str] = {}
    for c in contacts or []:
        if isinstance(c, dict):
            email = str(c.get("email", "")).strip()
            label = str(c.get("label", "") or "").strip()
        else:
            email = str(c).strip()
            label = ""
        if email and "@" in email and email not in emails:
            emails.append(email)
            if label:
                labels[email] = label
    with _LOCK:
        cfg = load_config()
        cfg["recipients"] = emails
        cfg["contact_labels"] = labels
        # 清理已不在通讯录中的计划收件人，避免悬挂引用
        for plan in cfg.get("plans", []):
            plan["recipients"] = [r for r in plan.get("recipients", []) if r in emails]
        _save(cfg)
    return public_config()


def update_smtp(data: dict) -> dict:
    """合并更新 SMTP 配置；password 留空表示保持原值（不清空）。"""
    with _LOCK:
        cfg = load_config()
        smtp = cfg.get("smtp", {})
        for key in ("host", "username", "from_name"):
            if isinstance(data.get(key), str):
                smtp[key] = data[key].strip()
        if data.get("port") is not None:
            try:
                smtp["port"] = int(data["port"])
            except (TypeError, ValueError):
                pass
        if data.get("use_ssl") is not None:
            smtp["use_ssl"] = bool(data["use_ssl"])
        pwd = data.get("password")
        if isinstance(pwd, str) and pwd.strip():
            smtp["password"] = pwd.strip()
        cfg["smtp"] = smtp
        _save(cfg)
    return public_config()


def set_recipients(recipients: list) -> dict:
    clean = _clean_emails(recipients)
    with _LOCK:
        cfg = load_config()
        cfg["recipients"] = clean
        _save(cfg)
    return public_config()


def set_poll_interval(seconds: int) -> dict:
    with _LOCK:
        cfg = load_config()
        cfg["poll_interval_s"] = max(10, int(seconds))
        _save(cfg)
    return public_config()


def _clean_emails(emails) -> list:
    out = []
    for e in emails or []:
        e = str(e).strip()
        if e and "@" in e and e not in out:
            out.append(e)
    return out


# ─────────────────────────── 计划 CRUD ───────────────────────────

def list_plans() -> list:
    return load_config().get("plans", [])


def _normalize_symbol(symbol: str) -> str:
    sym = str(symbol or "").upper().replace("-", "").replace("/", "").strip()
    if sym and not sym.endswith(("USDT", "USDC", "BUSD")):
        sym = sym + "USDT"
    return sym


def add_plan(data: dict) -> dict:
    plan = {
        "id": uuid.uuid4().hex[:12],
        "name": str(data.get("name") or "").strip() or "未命名提醒",
        "symbol": _normalize_symbol(data.get("symbol") or "BTCUSDT"),
        "target_price": float(data.get("target_price") or 0),
        "direction": DIRECTION_BELOW if data.get("direction") == DIRECTION_BELOW else DIRECTION_ABOVE,
        "recipients": _clean_emails(data.get("recipients")),
        "enabled": bool(data.get("enabled", True)),
        "repeat": bool(data.get("repeat", False)),
        "note": str(data.get("note") or "").strip(),
        "created_at": time.time(),
        "last_price": None,
        "last_triggered_at": None,
        "triggered_count": 0,
        "last_send_result": None,
    }
    if plan["target_price"] <= 0:
        return {"ok": False, "reason": "目标价位必须 > 0"}
    with _LOCK:
        cfg = load_config()
        cfg.setdefault("plans", []).append(plan)
        _save(cfg)
    return {"ok": True, "plan": plan}


_EDITABLE_FIELDS = ("name", "symbol", "target_price", "direction",
                    "recipients", "enabled", "repeat", "note")


def update_plan(plan_id: str, data: dict) -> dict:
    with _LOCK:
        cfg = load_config()
        for plan in cfg.get("plans", []):
            if plan.get("id") != plan_id:
                continue
            for key in _EDITABLE_FIELDS:
                if key not in data:
                    continue
                if key == "symbol":
                    plan["symbol"] = _normalize_symbol(data[key])
                    plan["last_price"] = None  # 改币种重置基线
                elif key == "target_price":
                    try:
                        plan["target_price"] = float(data[key])
                    except (TypeError, ValueError):
                        pass
                elif key == "direction":
                    plan["direction"] = (
                        DIRECTION_BELOW if data[key] == DIRECTION_BELOW else DIRECTION_ABOVE
                    )
                elif key == "recipients":
                    plan["recipients"] = _clean_emails(data[key])
                elif key in ("enabled", "repeat"):
                    plan[key] = bool(data[key])
                else:
                    plan[key] = str(data[key]).strip()
            _save(cfg)
            return {"ok": True, "plan": plan}
    return {"ok": False, "reason": "未找到该计划"}


def delete_plan(plan_id: str) -> dict:
    with _LOCK:
        cfg = load_config()
        before = len(cfg.get("plans", []))
        cfg["plans"] = [p for p in cfg.get("plans", []) if p.get("id") != plan_id]
        if len(cfg["plans"]) == before:
            return {"ok": False, "reason": "未找到该计划"}
        _save(cfg)
    return {"ok": True}


def _apply_plan_updates(updates: dict) -> None:
    """把后台轮询计算出的计划状态变更（按 id）安全合并回磁盘配置。

    重新读取磁盘配置后再合并，避免覆盖轮询期间用户新增/编辑的计划。
    """
    if not updates:
        return
    with _LOCK:
        cfg = load_config()
        for plan in cfg.get("plans", []):
            upd = updates.get(plan.get("id"))
            if upd:
                plan.update(upd)
        _save(cfg)


# ─────────────────────────── 价格获取 ───────────────────────────

def current_price(symbol: str) -> float | None:
    """取现货最新成交价（Binance 主源，OKX 兜底）。失败返回 None。"""
    if jcd is None:
        return None
    sym = _normalize_symbol(symbol)
    try:
        r = jcd._get(jcd.SPOT_API + "/api/v3/ticker/price", {"symbol": sym})
        if isinstance(r, dict) and r.get("price"):
            return float(r["price"])
    except Exception:  # noqa: BLE001
        pass
    try:
        p = jcd._okx_spot_price(sym)
        if p:
            return float(p)
    except Exception:  # noqa: BLE001
        pass
    return None


# ─────────────────────────── 发信 ───────────────────────────

def send_email(subject: str, body: str, to_list: list,
               cfg: dict | None = None, dry_run: bool = False) -> dict:
    cfg = cfg or load_config()
    smtp = cfg.get("smtp", {})
    to_list = _clean_emails(to_list)
    if dry_run:
        return {"ok": True, "dry_run": True, "to": to_list,
                "subject": subject, "body_preview": body[:200]}
    host = smtp.get("host")
    user = smtp.get("username")
    pwd = smtp.get("password")
    if not host or not user or not pwd:
        return {"ok": False, "reason": "SMTP 未配置完整（需 host/username/password）"}
    if not to_list:
        return {"ok": False, "reason": "无有效收件人"}
    port = int(smtp.get("port") or 465)
    use_ssl = bool(smtp.get("use_ssl", True))
    from_name = smtp.get("from_name") or "贾维斯价位提醒"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(from_name, "utf-8")), user))
    msg["To"] = ", ".join(to_list)
    try:
        if use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                s.login(user, pwd)
                s.sendmail(user, to_list, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                try:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                except Exception:  # noqa: BLE001 — 服务器不支持 STARTTLS 时直接明文登录
                    pass
                s.login(user, pwd)
                s.sendmail(user, to_list, msg.as_string())
        return {"ok": True, "to": to_list}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": repr(e)[:300]}


def _direction_cn(direction: str) -> str:
    return "跌破" if direction == DIRECTION_BELOW else "涨破"


def _format_alert(plan: dict, price: float) -> tuple[str, str]:
    subject = f"【贾维斯价位提醒】{plan['name']} 已触发"
    lines = [
        f"提醒：{plan['name']}",
        f"币种：{plan['symbol']}",
        f"条件：{_direction_cn(plan['direction'])} {plan['target_price']}",
        f"当前价：{price}",
        f"触发时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if plan.get("note"):
        lines.append(f"备注：{plan['note']}")
    lines.append("")
    lines.append("— 贾维斯桌面交易终端 自动发送")
    return subject, "\n".join(lines)


# ─────────────────────────── 轮询判定 ───────────────────────────

def _crossed(direction: str, prev: float, price: float, target: float) -> bool:
    if direction == DIRECTION_ABOVE:
        return prev < target <= price
    return prev > target >= price


def evaluate_all(dry_run: bool = False) -> dict:
    """检查全部启用的计划，命中即发邮件。返回本轮结果摘要。"""
    cfg = load_config()
    plans = list(cfg.get("plans", []))
    global_recips = cfg.get("recipients", [])

    price_cache: dict[str, float | None] = {}
    updates: dict[str, dict] = {}
    results: list[dict] = []
    triggered = 0
    checked = 0

    for plan in plans:
        if not plan.get("enabled"):
            continue
        checked += 1
        sym = plan["symbol"]
        if sym not in price_cache:
            price_cache[sym] = current_price(sym)
        price = price_cache[sym]
        if price is None:
            results.append({"id": plan["id"], "name": plan["name"],
                            "skipped": "价格获取失败"})
            continue

        prev = plan.get("last_price")
        target = float(plan["target_price"])
        direction = plan.get("direction", DIRECTION_ABOVE)
        upd: dict = {"last_price": price}

        hit = prev is not None and _crossed(direction, prev, price, target)
        if hit:
            recips = plan.get("recipients") or global_recips
            subject, body = _format_alert(plan, price)
            send = send_email(subject, body, recips, cfg=cfg, dry_run=dry_run)
            upd["last_triggered_at"] = time.time()
            upd["triggered_count"] = int(plan.get("triggered_count", 0)) + 1
            upd["last_send_result"] = "ok" if send.get("ok") else send.get("reason")
            if not plan.get("repeat"):
                upd["enabled"] = False
            triggered += 1
            results.append({"id": plan["id"], "name": plan["name"], "price": price,
                            "target": target, "direction": direction,
                            "sent": send.get("ok"), "to": recips,
                            "reason": send.get("reason")})
        updates[plan["id"]] = upd

    _apply_plan_updates(updates)
    return {"checked": checked, "triggered": triggered,
            "dry_run": dry_run, "results": results,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}


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
                print(f"📧 价位提醒：本轮触发 {summary['triggered']} 个计划")
        except Exception as e:  # noqa: BLE001
            _MONITOR_STATE["last_error"] = repr(e)[:300]
        interval = max(10, int(load_config().get("poll_interval_s", 60)))
        _MONITOR_STOP.wait(interval)
    _MONITOR_STATE["running"] = False


def start_monitor() -> dict:
    global _MONITOR_THREAD
    if _MONITOR_THREAD and _MONITOR_THREAD.is_alive():
        return monitor_status()
    _MONITOR_STOP.clear()
    _MONITOR_THREAD = threading.Thread(target=_monitor_loop, name="price-alert-monitor",
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

    ap = argparse.ArgumentParser(description="贾维斯价位邮件提醒")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_price = sub.add_parser("price", help="查询现价")
    p_price.add_argument("symbol")

    p_check = sub.add_parser("check", help="执行一轮检查")
    p_check.add_argument("--dry-run", action="store_true")

    p_test = sub.add_parser("test-email", help="发送测试邮件")
    p_test.add_argument("to", nargs="+")
    p_test.add_argument("--dry-run", action="store_true")

    sub.add_parser("config", help="打印脱敏配置")
    sub.add_parser("plans", help="列出计划")

    args = ap.parse_args()

    if args.cmd == "price":
        print(current_price(args.symbol))
    elif args.cmd == "check":
        print(json.dumps(evaluate_all(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    elif args.cmd == "test-email":
        out = send_email("【贾维斯价位提醒】测试邮件",
                         "这是一封测试邮件，收到说明 SMTP 配置正确。",
                         args.to, dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.cmd == "config":
        print(json.dumps(public_config(), ensure_ascii=False, indent=2))
    elif args.cmd == "plans":
        print(json.dumps(list_plans(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
