"""订单邮件提醒（按笔配置）冒烟测试。使用临时目录，不碰真实 ~/.vibe-trading。"""
from __future__ import annotations

import json
import os
import tempfile

import jarvis_journal as jj

_d = tempfile.mkdtemp()
jj.DB_DIR = _d
jj.DB_PATH = os.path.join(_d, "test.db")

import jarvis_price_alert as jpa

jpa.CONFIG_DIR = _d
jpa.CONFIG_PATH = os.path.join(_d, "price_alert_config.json")
jpa._DB_INITIALIZED = False

import jarvis_order_notify as jon

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# ── 1. 配置 CRUD ──
r = jon.set_config("pos-99999", "test@example.com",
                   notify_take_profit=True, notify_stop_loss=False)
check("set_config ok", r.get("ok"), json.dumps(r, ensure_ascii=False)[:120])

cfg = jon.get_config("pos-99999")
check("get_config 字段正确",
      bool(cfg) and cfg["email"] == "test@example.com"
      and cfg["notify_take_profit"] and not cfg["notify_stop_loss"])

r = jon.set_config("pos-99999", "new@example.com",
                   notify_take_profit=True, notify_stop_loss=True)
cfg = jon.get_config("pos-99999")
check("set_config 覆盖更新", bool(cfg) and cfg["email"] == "new@example.com"
      and cfg["notify_stop_loss"])

check("非法邮箱拒绝", not jon.set_config("pos-1", "bad-email").get("ok"))
check("空 order_id 拒绝", not jon.set_config("", "a@b.com").get("ok"))

rows = jon.list_configs()
check("list_configs 含配置", any(c["order_id"] == "pos-99999" for c in rows))

# ── 2. 触发判定（dry_run 不实际发信） ──
pos = {"id": 99999, "symbol": "BTCUSDT", "side": "buy", "qty": 0.01,
       "entry_price": 60000, "stop_loss": 58000, "take_profit": 65000}

r = jon.notify_position_closed(pos, 65100.0, "take",
                               pnl_usdt=11.0, pnl_pct=8.5, dry_run=True)
check("止盈触发发送", r.get("sent") and r.get("to") == "new@example.com",
      json.dumps(r, ensure_ascii=False)[:120])

jon.set_config("pos-99999", "new@example.com",
               notify_take_profit=True, notify_stop_loss=False)
r = jon.notify_position_closed(pos, 57900.0, "stop", dry_run=True)
check("未勾选止损则跳过", not r.get("sent") and "未勾选" in str(r.get("skipped")))

r = jon.notify_position_closed(pos, 61000.0, "manual", dry_run=True)
check("manual 平仓不触发", not r.get("sent"))

r = jon.notify_position_closed({"id": 88888, "symbol": "ETHUSDT"}, 3000.0,
                               "take", dry_run=True)
check("未配置的单跳过", not r.get("sent") and "未配置" in str(r.get("skipped")))

# ── 3. 挂单成交转持仓：limit_orders.position_id 反查 ──
import jarvis_wallet as jw

jw.ensure_account(10000.0)
lo = jw.place_limit_order("BTCUSDT", "buy", 50000.0, 0.01)
check("挂单登记", lo.get("ok"), json.dumps(lo, ensure_ascii=False)[:120])
oid = lo["order_id"]

jon.set_config(f"order-{oid}", "order@example.com")
jw.mark_filled(oid, 50000.0, position_id=77777)

cfg = jon.config_for_position(77777)
check("挂单配置经 position_id 反查命中",
      bool(cfg) and cfg["email"] == "order@example.com")

r = jon.notify_position_closed({"id": 77777, "symbol": "BTCUSDT", "side": "buy",
                                "qty": 0.01, "entry_price": 50000,
                                "stop_loss": 48000, "take_profit": 55000},
                               55100.0, "take", dry_run=True)
check("反查配置触发发送", r.get("sent") and r.get("to") == "order@example.com")

# ── 4. 测试邮件 + 邮件内容 ──
r = jon.send_test_email("pos-99999", dry_run=True)
check("测试邮件 dry_run", r.get("ok") and r.get("dry_run"))

subject, body = jon._format_close_mail(pos, 65100.0, "take",
                                       pnl_usdt=11.0, pnl_pct=8.5)
check("邮件主题含止盈", "止盈" in subject and "BTCUSDT" in subject, subject)
check("邮件正文含盈亏", "+8.5" in body and "65,100" in body)

subject, _ = jon._format_close_mail({**pos, "side": "sell"}, 57900.0, "stop")
check("空单止损主题", "空单止损" in subject, subject)

# ── 5. 删除 ──
r = jon.delete_config("pos-99999")
check("delete_config", r.get("ok") and r.get("deleted") == 1)
check("删除后查不到", jon.get_config("pos-99999") is None)
check("重复删除 deleted=0", jon.delete_config("pos-99999").get("deleted") == 0)

print()
if fails:
    print(f"FAILED: {len(fails)} 项 → {fails}")
    raise SystemExit(1)
print("订单邮件提醒冒烟测试全部通过 ✅")
