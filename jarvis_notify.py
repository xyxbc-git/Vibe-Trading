#!/usr/bin/env python3
"""贾维斯 JARVIS — [补完-13] 通知推送：信号 / 风险触发推 Telegram / 飞书。

把贾维斯的决策信号与风险告警推到即时通讯，让它「会喊话」。支持两个渠道：
  - Telegram：bot token + chat_id（Bot API sendMessage）
  - 飞书(Feishu/Lark)：自定义机器人 webhook（text 消息）

配置优先级：CLI > 环境变量 > ~/.vibe-trading/notify_config.json > 内置默认。
敏感凭据**不硬编码**：
  - TG：env `JARVIS_TG_BOT_TOKEN` + `JARVIS_TG_CHAT_ID`
  - 飞书：env `JARVIS_FEISHU_WEBHOOK`

设计：未配置的渠道自动跳过（不报错）；任一渠道发送失败不影响其它渠道；
全程可 `--dry-run` 只打印不真发，便于联网前本地验证格式。

用法：
  python jarvis_notify.py "贾维斯：BTC 偏多信号" --dry-run
  python jarvis_notify.py "实盘告警..." --channel telegram
  # 作为库：from jarvis_notify import notify, send_decision, send_radar
"""

from __future__ import annotations

import argparse
import json
import os
import time

import requests

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
CONFIG_PATH = os.path.join(CONFIG_DIR, "notify_config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "jarvis_notify.log")

DEFAULTS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "feishu_webhook": "",
    "request_timeout_s": 15,
}


def _log(msg: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def load_config(cli: dict | None = None) -> dict:
    cfg = dict(DEFAULTS)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items() if v is not None})
    except Exception as exc:  # noqa: BLE001
        _log(f"⚠️ 读取通知配置失败（用默认继续）: {exc}")
    env_map = {
        "telegram_bot_token": "JARVIS_TG_BOT_TOKEN",
        "telegram_chat_id": "JARVIS_TG_CHAT_ID",
        "feishu_webhook": "JARVIS_FEISHU_WEBHOOK",
    }
    for key, env in env_map.items():
        val = os.getenv(env)
        if val:
            cfg[key] = val
    if cli:
        cfg.update({k: v for k, v in cli.items() if v is not None})
    return cfg


# ─────────────────────────── 渠道实现 ───────────────────────────

def _send_telegram(cfg: dict, text: str) -> dict:
    token = cfg.get("telegram_bot_token")
    chat = cfg.get("telegram_chat_id")
    if not token or not chat:
        return {"channel": "telegram", "ok": False, "skipped": True, "reason": "未配置 token/chat_id"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url, json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=int(cfg.get("request_timeout_s", 15)),
        )
        ok = resp.status_code == 200 and resp.json().get("ok", False)
        return {"channel": "telegram", "ok": bool(ok), "http": resp.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"channel": "telegram", "ok": False, "error": repr(exc)[:200]}


def _send_feishu(cfg: dict, text: str) -> dict:
    hook = cfg.get("feishu_webhook")
    if not hook:
        return {"channel": "feishu", "ok": False, "skipped": True, "reason": "未配置 webhook"}
    try:
        resp = requests.post(
            hook, json={"msg_type": "text", "content": {"text": text}},
            timeout=int(cfg.get("request_timeout_s", 15)),
        )
        ok = resp.status_code == 200 and resp.json().get("StatusCode", resp.json().get("code", 0)) in (0, None)
        return {"channel": "feishu", "ok": bool(ok), "http": resp.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"channel": "feishu", "ok": False, "error": repr(exc)[:200]}


def notify(text: str, cfg: dict | None = None, channels: list[str] | None = None,
           dry_run: bool = False) -> dict:
    """推送文本到配置好的渠道。channels=None 表示所有已配置渠道。"""
    cfg = cfg or load_config()
    channels = channels or ["telegram", "feishu"]
    if dry_run:
        _log(f"🧪 dry-run 通知（渠道={channels}）:\n{text}")
        return {"dry_run": True, "channels": channels, "text_preview": text[:200]}

    results = []
    if "telegram" in channels:
        results.append(_send_telegram(cfg, text))
    if "feishu" in channels:
        results.append(_send_feishu(cfg, text))

    sent = [r for r in results if r.get("ok")]
    skipped = [r for r in results if r.get("skipped")]
    _log(f"📣 通知: 成功 {len(sent)} / 跳过(未配置) {len(skipped)} / 共 {len(results)} 渠道")
    if not sent and not skipped:
        _log(f"⚠️ 所有渠道发送失败: {results}")
    return {"results": results, "sent": len(sent), "skipped": len(skipped)}


# ─────────────────────────── 消息模板 ───────────────────────────

def format_decision(brief: dict) -> str:
    """把单币决策格式化为推送文本。"""
    d = brief.get("decision", {})
    sym = brief.get("symbol", "?")
    lessons = d.get("lessons") or []
    lines = [
        f"📊 贾维斯决策 · {sym}",
        f"方向：{d.get('direction','?')} | 信心 {d.get('conviction_score','?')} | 仓位 {d.get('suggested_position_pct','?')}%",
    ]
    if d.get("entry_zone"):
        lines.append(f"入场 {d.get('entry_zone')} | 止损 {d.get('stop_loss')} | 止盈 {d.get('take_profit_ref')}")
    if lessons:
        top = lessons[0]
        lines.append(f"⚠️ 教训：{top.get('title','')} — {top.get('advice','')}")
    return "\n".join(lines)


def format_radar(radar: dict, limit: int = 8) -> str:
    """把多币雷达结果格式化为推送文本。"""
    hits = radar.get("actionable", [])
    lines = [f"🛰️ 贾维斯机会雷达（{radar.get('scanned',0)} 币 / {len(hits)} 个信号）"]
    if not hits:
        lines.append("本轮无达标信号。")
        return "\n".join(lines)
    for it in hits[:limit]:
        lines.append(
            f"• {it['symbol']}：{it['direction']} 信心 {it['conviction_score']} 仓位 {it['position_pct']}%"
            + (f" ⚠️x{it['lesson_count']}" if it.get("lesson_count") else "")
        )
    return "\n".join(lines)


def send_decision(brief: dict, cfg: dict | None = None, dry_run: bool = False) -> dict:
    return notify(format_decision(brief), cfg=cfg, dry_run=dry_run)


def send_radar(radar: dict, cfg: dict | None = None, dry_run: bool = False) -> dict:
    return notify(format_radar(radar), cfg=cfg, dry_run=dry_run)


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯通知推送（Telegram / 飞书）")
    ap.add_argument("text", help="要推送的文本")
    ap.add_argument("--channel", action="append", choices=["telegram", "feishu"],
                    help="指定渠道（可多次）；默认全部已配置渠道")
    ap.add_argument("--dry-run", action="store_true", help="只打印不真发")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    out = notify(args.text, cfg=cfg, channels=args.channel, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
