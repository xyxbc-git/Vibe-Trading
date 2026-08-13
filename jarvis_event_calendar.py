#!/usr/bin/env python3
"""贾维斯 JARVIS — 金十财经事件日历（任务 U · 事件风险窗口最小闭环）。

回答一个问题：**现在（或某时刻附近）有没有高影响宏观数据要公布？**
CPI/非农/FOMC 公布瞬间常见插针，任何技术形态在数据面前都可能一秒作废——
这是导师证据链的第八路（event_risk）：风险窗口内提示「落地后再入场」。

数据源（插拔，当前主源 jin10）：
  金十开放平台 REST  https://open-data-api.jin10.com/data-api/calendar
  header 携带 secret-key（open.jin10.com 注册开发者账号后控制台申请）。
  免费档有 1-3 分钟延迟与频率限制——对「事件风险窗口」用途完全够用
  （日历提前数天公布，1h TTL 缓存即可，本模块不做高频轮询）。

密钥（不进 git）：~/.vibe-trading/jin10.json，格式 {"secret_key": "..."}，
  建议 chmod 600。未配置时**诚实返回 not_configured，绝不伪造日历数据**。

出网纪律：只打金十域名（URL 常量锁死，不碰币安）；出网前经
  jarvis_net.budget_take 登记预算；请求 10s 超时；失败回退磁盘缓存（标 stale）。

归一化事件结构（对下游唯一契约）：
  {ts, country, title, importance(1-3), previous, forecast, actual, source}

风险窗口（阈值 jarvis_config 三处登记，随配置热加载）：
  event_risk_min_star  ≥ 该星级才算高影响（默认 3）
  event_risk_pre_min   事件前 N 分钟进入风险窗口（默认 30）
  event_risk_post_min  事件后 N 分钟仍在风险窗口（默认 15）

用法：
  python3 jarvis_event_calendar.py status     # 配置/缓存状态
  python3 jarvis_event_calendar.py upcoming   # 未来 24h 事件
  python3 jarvis_event_calendar.py risk       # 当前风险窗口判定
"""

from __future__ import annotations

import json
import os
import time

KEY_PATH = os.path.expanduser("~/.vibe-trading/jin10.json")
CACHE_DIR = os.path.expanduser("~/.vibe-trading/cache")
CACHE_PATH = os.path.join(CACHE_DIR, "jin10_calendar.json")

# 出网只打这一个域名（红线：不碰币安）
API_HOST = "open-data-api.jin10.com"
CALENDAR_URL = f"https://{API_HOST}/data-api/calendar"

CALENDAR_TTL_S = 3600.0        # 日历 1h TTL（提前数天公布，不需频繁拉）
BUDGET_PER_MIN = 6             # 对金十域名的每分钟出网预算（TTL 挡在前面，兜底用）
REQUEST_TIMEOUT_S = 10.0

# 风险窗口阈值默认（jarvis_config 可覆盖，三处登记）
MIN_STAR_DEFAULT = 3
PRE_MIN_DEFAULT = 30
POST_MIN_DEFAULT = 15

_mem_cache: dict = {"ts": 0.0, "events": None}   # 进程内缓存（events=None 表示未加载）


# ─────────────────────────── 配置与密钥 ───────────────────────────

def _load_key() -> str | None:
    """读 secret-key；文件缺失/格式坏/空值一律 None（= not_configured）。"""
    try:
        with open(KEY_PATH, encoding="utf-8") as f:
            key = str((json.load(f) or {}).get("secret_key") or "").strip()
        return key or None
    except Exception:  # noqa: BLE001 — 未配置/坏文件都按未配置，不抛
        return None


def configured() -> bool:
    return _load_key() is not None


def _cfg_num(key: str, default: float) -> float:
    try:
        import jarvis_config as jc
        v = jc.get(key)
        return float(v) if v is not None else float(default)
    except Exception:  # noqa: BLE001 — 配置层异常回退默认
        return float(default)


# ─────────────────────────── 拉取与归一化 ───────────────────────────

def _normalize(raw: list) -> list[dict]:
    """金十日历原始行 → 归一化事件（字段名宽容多候选；解析失败的行丢弃计数）。

    已知字段（开放平台 data 数组）：pub_time/star/title/previous/consensus/
    actual/revised/affect_txt/country；时间兼容 epoch 秒/毫秒与
    'YYYY-MM-DD HH:MM:SS' 字符串（金十为北京时间）。
    """
    out: list[dict] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        ts = _parse_ts(r.get("pub_time") or r.get("public_time") or r.get("time"))
        title = str(r.get("title") or r.get("name") or "").strip()
        if ts is None or not title:
            continue
        try:
            star = int(float(r.get("star") or r.get("importance") or 0))
        except (TypeError, ValueError):
            star = 0
        out.append({
            "ts": ts,
            "country": str(r.get("country") or r.get("region") or "").strip(),
            "title": title,
            "importance": max(0, min(3, star)),
            "previous": r.get("previous"),
            "forecast": r.get("consensus") if r.get("consensus") is not None
                        else r.get("forecast"),
            "actual": r.get("actual"),
            "source": "jin10",
        })
    out.sort(key=lambda e: e["ts"])
    return out


def _parse_ts(v) -> float | None:
    """epoch 秒/毫秒 或 'YYYY-MM-DD HH:MM[:SS]'（北京时间）→ epoch 秒。"""
    if v is None:
        return None
    try:
        x = float(v)
        if x > 1e12:      # 毫秒
            x /= 1000.0
        if x > 1e9:       # 合理的 epoch 秒（2001 年之后）
            return x
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timedelta, timezone
        s = str(v).strip().replace("T", " ")[:19]
        fmt = "%Y-%m-%d %H:%M:%S" if len(s) > 16 else "%Y-%m-%d %H:%M"
        dt = datetime.strptime(s, fmt)
        return dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
    except Exception:  # noqa: BLE001 — 无法识别的时间格式，该行丢弃
        return None


def _fetch_remote(key: str) -> list[dict]:
    """真实出网拉日历（仅金十域名；预算登记；超时 10s）。失败抛出由调用方兜底。"""
    import jarvis_net
    if not jarvis_net.budget_take(API_HOST, BUDGET_PER_MIN):
        raise RuntimeError("jin10 出网预算已满（60s 滑窗），本轮跳过")
    import requests
    resp = requests.get(CALENDAR_URL, headers={"secret-key": key},
                        params={"calendar_type": "cj"}, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data") if isinstance(body, dict) else body
    if not isinstance(data, list):
        raise ValueError(f"jin10 响应结构异常：data 非数组（{str(body)[:120]}）")
    return _normalize(data)


def _disk_load() -> tuple[float, list[dict]]:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f) or {}
        return float(d.get("fetched_at") or 0.0), list(d.get("events") or [])
    except Exception:  # noqa: BLE001 — 无缓存/坏缓存按空处理
        return 0.0, []


def _disk_save(events: list[dict]) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "events": events}, f,
                      ensure_ascii=False)
    except Exception:  # noqa: BLE001 — 缓存写失败不影响主流程
        pass


def events(force: bool = False) -> dict:
    """取归一化事件列表（1h TTL：进程内 → 磁盘 → 出网）。

    返回 {ok, configured, events, fetched_at, stale, note}——
    未配置 key 时 ok=False + configured=False，events 恒空**不伪造**。
    """
    now = time.time()
    key = _load_key()
    if key is None:
        return {"ok": False, "configured": False, "events": [], "fetched_at": None,
                "stale": False,
                "note": f"未配置金十 secret-key：去 open.jin10.com 注册后将 "
                        f"{{\"secret_key\": \"...\"}} 写入 {KEY_PATH}（chmod 600）"}
    if (not force and _mem_cache["events"] is not None
            and now - _mem_cache["ts"] < CALENDAR_TTL_S):
        return {"ok": True, "configured": True, "events": _mem_cache["events"],
                "fetched_at": _mem_cache["ts"], "stale": False, "note": None}
    disk_ts, disk_events = _disk_load()
    if not force and disk_events and now - disk_ts < CALENDAR_TTL_S:
        _mem_cache.update(ts=disk_ts, events=disk_events)
        return {"ok": True, "configured": True, "events": disk_events,
                "fetched_at": disk_ts, "stale": False, "note": None}
    try:
        fresh = _fetch_remote(key)
        _mem_cache.update(ts=now, events=fresh)
        _disk_save(fresh)
        return {"ok": True, "configured": True, "events": fresh,
                "fetched_at": now, "stale": False, "note": None}
    except Exception as exc:  # noqa: BLE001 — 出网失败回退旧缓存，如实标 stale
        note = f"jin10 拉取失败：{exc!r}"[:200]
        if disk_events:
            _mem_cache.update(ts=now - CALENDAR_TTL_S + 300.0, events=disk_events)
            return {"ok": True, "configured": True, "events": disk_events,
                    "fetched_at": disk_ts, "stale": True, "note": note + "（用旧缓存）"}
        return {"ok": False, "configured": True, "events": [], "fetched_at": None,
                "stale": False, "note": note}


# ─────────────────────────── 查询接口 ───────────────────────────

def upcoming_events(within_minutes: float = 1440.0, min_star: int = 1) -> dict:
    """未来 within_minutes 分钟内的事件（含刚过去 post 窗口内的，倒计时负值）。"""
    box = events()
    now = time.time()
    post = _cfg_num("event_risk_post_min", POST_MIN_DEFAULT) * 60.0
    picked = [
        {**e, "minutes_to": round((e["ts"] - now) / 60.0, 1)}
        for e in box["events"]
        if e["importance"] >= int(min_star)
        and now - post <= e["ts"] <= now + within_minutes * 60.0
    ]
    return {"ok": box["ok"], "configured": box["configured"],
            "events": picked, "stale": box.get("stale", False),
            "note": box.get("note")}


def risk_window(symbol: str | None = None) -> dict:
    """当前是否在高影响事件风险窗口内 → {in_window, event, minutes_to, note}。

    宏观数据（CPI/非农/利率决议）对加密市场是全市场共振冲击，当前不按
    symbol 过滤（参数保留作未来映射：如美股财报只影响个股）。
    未配置/拉取失败 → in_window=False + available=False，由调用方按
    unavailable 处理（不计分母），不把「没数据」误读成「没风险」。
    """
    box = events()
    now = time.time()
    if not box["ok"]:
        return {"available": False, "in_window": False, "event": None,
                "minutes_to": None, "note": box.get("note") or "事件日历不可用"}
    min_star = int(_cfg_num("event_risk_min_star", MIN_STAR_DEFAULT))
    pre = _cfg_num("event_risk_pre_min", PRE_MIN_DEFAULT) * 60.0
    post = _cfg_num("event_risk_post_min", POST_MIN_DEFAULT) * 60.0
    hits = [e for e in box["events"]
            if e["importance"] >= min_star and e["ts"] - pre <= now <= e["ts"] + post]
    if not hits:
        nxt = next((e for e in box["events"]
                    if e["importance"] >= min_star and e["ts"] > now), None)
        note = "未来无临近高影响事件"
        if nxt:
            note = (f"最近的高影响事件「{nxt['country']}{nxt['title']}」"
                    f"还有 {int((nxt['ts'] - now) / 60)} 分钟，当前不在风险窗口")
        return {"available": True, "in_window": False, "event": nxt,
                "minutes_to": (round((nxt["ts"] - now) / 60.0, 1) if nxt else None),
                "note": note, "stale": box.get("stale", False)}
    ev = min(hits, key=lambda e: abs(e["ts"] - now))
    mins = (ev["ts"] - now) / 60.0
    star_txt = "★" * ev["importance"]
    if mins >= 0:
        note = (f"{ev['country']}{ev['title']}（{star_txt}）还有 {int(round(mins))} 分钟公布，"
                "数据瞬间插针风险极高，建议落地后再入场")
    else:
        note = (f"{ev['country']}{ev['title']}（{star_txt}）已于 {int(round(-mins))} 分钟前公布，"
                "波动尚未消化，谨慎追单")
    return {"available": True, "in_window": True, "event": ev,
            "minutes_to": round(mins, 1), "note": note,
            "stale": box.get("stale", False)}


def mentor_item(symbol: str | None = None) -> dict:
    """导师第八路证据（jarvis_trade_mentor 动态 import 消费）。

    契约：{available, in_window, note, event, minutes_to}——available=False
    表示未配置/不可用（mentor 按 unavailable 处理，不计分母）。
    """
    return risk_window(symbol)


def status() -> dict:
    """配置/缓存健康态（dashboard /api/events/status 直出）。"""
    key = _load_key()
    disk_ts, disk_events = _disk_load()
    now = time.time()
    return {
        "configured": key is not None,
        "key_path": KEY_PATH,
        "cache_events": len(disk_events),
        "cache_age_s": round(now - disk_ts, 1) if disk_ts > 0 else None,
        "ttl_s": CALENDAR_TTL_S,
        "thresholds": {
            "min_star": int(_cfg_num("event_risk_min_star", MIN_STAR_DEFAULT)),
            "pre_min": int(_cfg_num("event_risk_pre_min", PRE_MIN_DEFAULT)),
            "post_min": int(_cfg_num("event_risk_post_min", POST_MIN_DEFAULT)),
        },
        "note": (None if key is not None else
                 f"未配置：去 open.jin10.com 注册开发者账号，控制台申请 secret-key 后"
                 f"写入 {KEY_PATH}（格式 {{\"secret_key\": \"...\"}}，chmod 600）"),
    }


# ─────────────────────────── CLI ───────────────────────────

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="金十事件日历（任务 U）")
    ap.add_argument("cmd", choices=("status", "upcoming", "risk"))
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()
    if args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.cmd == "upcoming":
        print(json.dumps(upcoming_events(args.hours * 60.0),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(risk_window(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
