"""价位提醒配置 JSON→SQLite 迁移冒烟测试。使用临时目录，不碰真实 ~/.vibe-trading。"""
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

fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


# 准备旧 JSON
legacy = {
    "smtp": {
        "host": "smtp.example.com",
        "port": 587,
        "use_ssl": False,
        "username": "alert@example.com",
        "password": "secret-pass",
        "from_name": "测试提醒",
    },
    "recipients": ["a@x.com", "b@y.com"],
    "contact_labels": {"a@x.com": "Alice"},
    "poll_interval_s": 45,
    "plans": [
        {
            "id": "plan001",
            "name": "BTC 跌破",
            "symbol": "BTCUSDT",
            "target_price": 50000.0,
            "direction": "below",
            "recipients": ["a@x.com"],
            "enabled": True,
            "repeat": False,
            "note": "测试计划",
            "created_at": 1700000000.0,
            "last_price": 51000.0,
            "last_triggered_at": None,
            "triggered_count": 0,
            "last_send_result": None,
        }
    ],
}
with open(jpa.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(legacy, f, ensure_ascii=False, indent=2)

# 1. 首次 load → 自动导入
cfg1 = jpa.load_config()
check("JSON 导入 SMTP host", cfg1["smtp"]["host"] == "smtp.example.com")
check("JSON 导入收件人", cfg1["recipients"] == ["a@x.com", "b@y.com"])
check("JSON 导入备注", cfg1["contact_labels"]["a@x.com"] == "Alice")
check("JSON 导入轮询间隔", cfg1["poll_interval_s"] == 45)
check("JSON 导入计划", len(cfg1["plans"]) == 1 and cfg1["plans"][0]["id"] == "plan001")
check("JSON 已备份", os.path.exists(jpa.CONFIG_PATH + ".bak"))
check("原 JSON 已移除", not os.path.exists(jpa.CONFIG_PATH))

# 2. 更新 SMTP（密码留空保持原值）
pub = jpa.update_smtp({"host": "smtp.new.com", "port": 465, "password": ""})
check("update_smtp host", pub["smtp"]["host"] == "smtp.new.com")
cfg2 = jpa.load_config()
check("密码保持", cfg2["smtp"]["password"] == "secret-pass")

# 3. 更新通讯录
pub2 = jpa.set_contacts([{"email": "c@z.com", "label": "Carol"}])
check("set_contacts", pub2["recipients"] == ["c@z.com"])
check("public contacts", pub2["contacts"] == [{"email": "c@z.com", "label": "Carol"}])

# 4. 轮询间隔
jpa.set_poll_interval(120)
check("poll_interval", jpa.load_config()["poll_interval_s"] == 120)

# 5. 计划 CRUD
out = jpa.add_plan({"name": "ETH 涨破", "symbol": "ETH", "target_price": 3000, "direction": "above"})
check("add_plan", out.get("ok") is True)
new_id = out["plan"]["id"]
plans = jpa.list_plans()
check("list_plans 数量", len(plans) == 2)
upd = jpa.update_plan(new_id, {"enabled": False, "note": "暂停"})
check("update_plan", upd.get("ok") and upd["plan"]["enabled"] is False)
del_out = jpa.delete_plan("plan001")
check("delete_plan", del_out.get("ok") is True)
check("删除后剩 1 个", len(jpa.list_plans()) == 1)

# 6. 模拟重启：重置模块初始化标记后重读
jpa._DB_INITIALIZED = False
cfg3 = jpa.load_config()
check("重启后 SMTP 持久", cfg3["smtp"]["host"] == "smtp.new.com")
check("重启后通讯录", cfg3["recipients"] == ["c@z.com"])
check("重启后计划数", len(cfg3["plans"]) == 1)

# 7. public_config 脱敏
pub3 = jpa.public_config()
check("密码脱敏", pub3["smtp"]["has_password"] and "•" in pub3["smtp"]["password_masked"])
check("无明文密码字段", "password" not in pub3["smtp"])

print("\n=== " + ("全部通过" if not fails else f"失败 {len(fails)}: {fails}") + " ===")
raise SystemExit(1 if fails else 0)
