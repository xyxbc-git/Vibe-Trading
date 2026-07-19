#!/usr/bin/env python3
"""贾维斯 JARVIS — 模拟下单全生命周期追踪 + 邮件通知。

需求口径（仅限模拟盘，不碰实盘链路）：
  1. 下单周期打标：挂单若参考某时间框架（如 30m）推荐点位，给 limit_orders 补记
     signal_tf；下单时间/入场参数（created_ts、limit_price/SL/TP/qty/side）表里已有，
     不重复存。
  2. 周期到点回录：下单起过一个周期时长（30m 单 → 30 分钟后），把当时市价回录到
     order_tf_snapshots，供「推荐点位 vs 周期后实际点位」复盘。直接开仓且带
     signal_tf 的持仓（如 12 系统单）同样按 opened_ts 起算回录。
  3. 成交入场通知：挂单撮合成交转持仓时，把 signal_tf 传播到持仓行，并发一封
     「入场信号」邮件（标题含币种/方向/入场价/周期；正文含入损盈三价 +
     下单时间→成交时间）；发送失败只记日志，绝不阻断成交主链路。
  4. 盈亏阈值邮件：持仓方向性浮盈 ≥ +20% 发一封、浮亏 ≤ -50% 发一封；
     口径 = (现价/入场价 - 1) × 100，做空镜像取负，未计杠杆。
     每仓每阈值只发一次（lifecycle_events 落库去重，重启不重发）。
     阈值可配：jarvis_config 的 lifecycle_gain_alert_pct / lifecycle_loss_alert_pct
     （缺省 20 / 50，config.yaml 或 jarvis_config.json 里写同名键即可覆盖）。
  5. 查询接口：lifecycle_list() 列表 + lifecycle_of(ref) 单条完整时间线，
     由 jarvis_dashboard 尾部路由挂接为 /api/order-lifecycle*。

邮件基建复用 jarvis_price_alert：全局 SMTP 配置 + 通讯录收件人（设置页已配）。
存储与全仓同库（~/.vibe-trading/jarvis_journal.db，经 jarvis_db 兼容层，
DDL 写 SQLite 方言由兼容层翻译到 pg；ALTER ADD COLUMN 幂等）。

后台巡检：dashboard startup 拉起 daemon 线程，每 60s 一轮 run_sweep()
（到点回录 + 阈值扫描）；所有异常自吞，绝不拖垮交易主链路。

作为库使用：
  from jarvis_order_lifecycle import (
      ensure_schema, tag_order_tf, on_order_filled, run_sweep,
      lifecycle_of, lifecycle_list, start_monitor, stop_monitor, monitor_status,
  )
"""

from __future__ import annotations

import json
import os
import threading
import time

import jarvis_journal as jj
import jarvis_price_alert as jpa

LOG_DIR = os.path.expanduser("~/.vibe-trading")
LOG_PATH = os.path.join(LOG_DIR, "jarvis_order_lifecycle.log")

# 阈值缺省：浮盈 +20% / 浮亏 -50%（jarvis_config 同名键可覆盖）
DEFAULT_GAIN_ALERT_PCT = 20.0
DEFAULT_LOSS_ALERT_PCT = 50.0
DEFAULT_SWEEP_INTERVAL_S = 60

# 事件类型
EVENT_ENTRY_MAIL = "entry_mail"    # 挂单成交入场邮件
EVENT_GAIN_ALERT = "gain_alert"    # 浮盈阈值邮件
EVENT_LOSS_ALERT = "loss_alert"    # 浮亏阈值邮件

_LOCK = threading.RLock()


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [order-lifecycle] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — 日志写盘失败不影响主流程
        pass


# ─────────────────────────── 周期解析 ───────────────────────────

# multi = 12 系统多周期共识单，回录时长按主周期 4h 口径（与 _TWELVE_TIME_STOP 一致）
_TF_ALIASES = {"multi": "4h"}
_TF_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def tf_seconds(tf) -> int | None:
    """把时间框架字符串解析成秒（"30m"→1800、"4h"→14400、"1d"→86400、"multi"→4h）。

    解析失败返回 None（调用方据此拒绝打标 / 跳过回录）。
    """
    s = str(tf or "").strip().lower()
    if not s:
        return None
    s = _TF_ALIASES.get(s, s)
    unit = s[-1]
    if unit not in _TF_UNIT_S:
        return None
    try:
        n = float(s[:-1])
    except ValueError:
        return None
    if n <= 0:
        return None
    return int(n * _TF_UNIT_S[unit])


# ─────────────────────────── 表结构 ───────────────────────────

def ensure_schema() -> None:
    """建生命周期相关表 + 给 limit_orders 补 signal_tf 列（全部幂等）。"""
    jj.init_db()
    with jj._conn() as conn:
        # 周期到点回录表：order_id（挂单路径）与 position_id（直接开仓路径）二选一为根
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_tf_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id        INTEGER,
                position_id     INTEGER,
                symbol          TEXT    NOT NULL,
                signal_tf       TEXT    NOT NULL,
                due_ts          REAL    NOT NULL,
                snap_ts         REAL    NOT NULL,
                snap_price      REAL,
                entry_ref_price REAL,
                note            TEXT
            )
            """
        )
        # 生命周期事件表：入场邮件 / 阈值邮件的去重标记 + 留痕（重启不重发靠它）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT    NOT NULL,
                dedup_key   TEXT    NOT NULL UNIQUE,
                order_id    INTEGER,
                position_id INTEGER,
                symbol      TEXT,
                payload     TEXT,
                created_ts  REAL    NOT NULL
            )
            """
        )
    # 旧库升级：limit_orders 补 signal_tf（SQLite 重复加列抛错即视为已存在；
    # pg 后端经 jarvis_db 翻译自动带 IF NOT EXISTS，天然幂等）
    try:
        with jj._conn() as conn:
            conn.execute("ALTER TABLE limit_orders ADD COLUMN signal_tf TEXT")
    except Exception:  # noqa: BLE001 — duplicate column = 已升级过
        pass
    # 巡检要读 paper_positions；新装环境该表可能还没建（首仓前），顺带确保存在。
    # 惰性导入避免模块级循环依赖；失败不阻断（下面各扫描路径自会隔离异常）。
    try:
        import jarvis_paper_trader as _jpt
        _jpt.init_positions_table()
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────── 下单周期打标 ───────────────────────────

def tag_order_tf(order_id: int, signal_tf: str) -> dict:
    """给一笔挂单补记信号周期（/api/orders/place 与 CLI limit 下单后调用）。

    tf 无法解析（拼写错误等）拒绝打标，避免脏数据进回录扫描。
    """
    ensure_schema()
    tf = str(signal_tf or "").strip()
    if tf_seconds(tf) is None:
        return {"ok": False, "order_id": order_id, "reason": f"无法解析的时间框架: {tf!r}"}
    with jj._conn() as conn:
        cur = conn.execute(
            "UPDATE limit_orders SET signal_tf=? WHERE id=?", (tf, int(order_id)))
        if (cur.rowcount or 0) == 0:
            return {"ok": False, "order_id": order_id, "reason": "挂单不存在"}
    _log(f"🏷 挂单 #{order_id} 打标周期 {tf}")
    return {"ok": True, "order_id": order_id, "signal_tf": tf}


# ─────────────────────────── 事件去重 ───────────────────────────

def _event_exists(dedup_key: str) -> bool:
    with jj._conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM lifecycle_events WHERE dedup_key=?", (dedup_key,)).fetchone()
    return row is not None


def _record_event(kind: str, dedup_key: str, *, order_id=None, position_id=None,
                  symbol=None, payload: dict | None = None) -> None:
    with jj._conn() as conn:
        conn.execute(
            """
            INSERT INTO lifecycle_events
              (kind, dedup_key, order_id, position_id, symbol, payload, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, dedup_key, order_id, position_id, symbol,
             json.dumps(payload or {}, ensure_ascii=False), time.time()),
        )


# ─────────────────────────── 发信 ───────────────────────────

def _fmt(v) -> str:
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


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "—"


def _deliver(subject: str, body: str) -> dict:
    """经现有邮件基建发信：全局 SMTP + 通讯录全部收件人（测试可 monkey-patch）。"""
    try:
        cfg = jpa.load_config()
        recipients = list(cfg.get("recipients") or [])
        if not recipients:
            return {"ok": False, "reason": "邮件通讯录未配置收件人"}
        return jpa.send_email(subject, body, recipients, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 — 发信绝不外抛
        return {"ok": False, "reason": repr(exc)[:200]}


# ─────────────────────────── 成交入场钩子 ───────────────────────────

def on_order_filled(order: dict, position_id: int, fill_price: float) -> dict:
    """挂单撮合成交转持仓后的钩子（jarvis_paper_trader.match_limit_orders 调用）。

    做三件事，全程自吞异常：
      1. 把挂单的 signal_tf 传播到 paper_positions 行（后续阈值/回录都认持仓行）；
      2. 落 entry_mail 去重事件（同一挂单只发一次，重启不重发）；
      3. 发「入场信号」邮件（未配收件人/SMTP 失败只记日志，不影响成交）。
    """
    try:
        ensure_schema()
        oid = int(order.get("id"))
        sym = str(order.get("symbol") or "—")
        tf = (order.get("signal_tf") or "").strip() or None

        # 1) 周期标记传播到持仓行（持仓行已有 tf 时不覆盖——如未来直接开仓路径自带）
        if tf and position_id is not None:
            with jj._conn() as conn:
                conn.execute(
                    "UPDATE paper_positions SET signal_tf=? "
                    "WHERE id=? AND (signal_tf IS NULL OR signal_tf='')",
                    (tf, int(position_id)),
                )

        # 2) 去重：同一挂单只发一次入场邮件
        dedup = f"{EVENT_ENTRY_MAIL}:order-{oid}"
        if _event_exists(dedup):
            return {"ok": True, "skipped": "该挂单已发过入场邮件"}

        # 3) 组装并发送入场信号邮件
        side = str(order.get("side") or "buy").lower()
        side_cn = "空单" if side == "sell" else "多单"
        subject = (f"【贾维斯交易提醒】{sym} {side_cn}入场成交"
                   + (f"（{tf} 信号）" if tf else ""))
        lines = [
            f"挂单 #{oid} 已成交入场 → 持仓 #{position_id}",
            "",
            f"币种：{sym}（{side_cn}）",
            f"信号周期：{tf or '—'}",
            f"入场价（成交价）：{_fmt(fill_price)}",
            f"挂单价：{_fmt(order.get('limit_price'))}",
            f"止损价：{_fmt(order.get('stop_loss'))}",
            f"止盈价：{_fmt(order.get('take_profit'))}",
            f"数量：{_fmt(order.get('qty'))}",
            f"下单时间：{_fmt_ts(order.get('created_ts'))}",
            f"成交时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "— 贾维斯桌面交易终端 自动发送",
        ]
        send = _deliver(subject, "\n".join(lines))
        # 无论发送成败都落标记：防止 SMTP 故障时每轮撮合重试轰炸
        _record_event(EVENT_ENTRY_MAIL, dedup, order_id=oid, position_id=position_id,
                      symbol=sym, payload={
                          "signal_tf": tf, "fill_price": fill_price, "side": side,
                          "sent": bool(send.get("ok")),
                          "send_result": "ok" if send.get("ok") else str(send.get("reason"))[:200],
                      })
        if send.get("ok"):
            _log(f"📧 挂单 #{oid} {sym} 入场信号邮件已发送 → {send.get('to')}")
        else:
            _log(f"⚠️ 挂单 #{oid} {sym} 入场邮件发送失败（不阻断成交）: {send.get('reason')}"[:200])
        return {"ok": True, "sent": bool(send.get("ok")), "reason": send.get("reason")}
    except Exception as exc:  # noqa: BLE001 — 钩子绝不拖垮成交主链路
        _log(f"⚠️ 入场钩子异常（已忽略）: {exc!r}"[:200])
        return {"ok": False, "reason": repr(exc)[:200]}


# ─────────────────────────── 阈值读取 ───────────────────────────

def _thresholds() -> tuple[float, float]:
    """读浮盈/浮亏阈值（%正数）：jarvis_config 可配，异常回退缺省 20/50。"""
    gain, loss = DEFAULT_GAIN_ALERT_PCT, DEFAULT_LOSS_ALERT_PCT
    try:
        import jarvis_config as jc_mod
        gain = abs(float(jc_mod.get("lifecycle_gain_alert_pct", DEFAULT_GAIN_ALERT_PCT)
                         or DEFAULT_GAIN_ALERT_PCT))
        loss = abs(float(jc_mod.get("lifecycle_loss_alert_pct", DEFAULT_LOSS_ALERT_PCT)
                         or DEFAULT_LOSS_ALERT_PCT))
    except Exception:  # noqa: BLE001 — 配置层故障不阻断巡检
        pass
    return gain, loss


def _direction_pnl_pct(side: str, entry: float, price: float) -> float | None:
    """方向性浮盈亏%：(现价/入场 - 1)×100，做空镜像取负；未计杠杆。"""
    try:
        entry = float(entry)
        price = float(price)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or price <= 0:
        return None
    sign = -1.0 if str(side or "buy").lower() == "sell" else 1.0
    return (price / entry - 1.0) * 100.0 * sign


# ─────────────────────────── 取价 ───────────────────────────

def _default_price(symbol: str) -> float | None:
    """现价：复用价位提醒模块的取数链（Binance 主源 / OKX 兜底），失败 None。"""
    try:
        return jpa.current_price(symbol)
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────── 周期到点回录 ───────────────────────────

def _snap_order_ids() -> set:
    with jj._conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT order_id FROM order_tf_snapshots WHERE order_id IS NOT NULL"
        ).fetchall()
    return {int(r["order_id"]) for r in rows}


def _snap_position_ids() -> set:
    with jj._conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT position_id FROM order_tf_snapshots "
            "WHERE position_id IS NOT NULL AND order_id IS NULL"
        ).fetchall()
    return {int(r["position_id"]) for r in rows}


def _sweep_tf_snapshots(now: float, price_of, price_cache: dict) -> list:
    """扫「下单/开仓时间 + 周期时长已到且未回录」的挂单与持仓，回录当时点位。

    两条路径（去重互斥）：
      - 挂单路径：limit_orders.signal_tf 非空、状态 pending/filled，due 按 created_ts；
        成交转持仓的单仍按挂单起算（用户口径：从下单起 30 分钟）。
      - 持仓路径：paper_positions.signal_tf 非空、open、且不是挂单成交转来的
        （避免与挂单路径双记），due 按 opened_ts。
    """
    ensure_schema()
    recorded = []

    # ── 挂单路径 ──
    with jj._conn() as conn:
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM limit_orders WHERE signal_tf IS NOT NULL AND signal_tf != '' "
            "AND status IN ('pending','filled')"
        ).fetchall()]
    done_orders = _snap_order_ids()
    filled_position_ids = set()
    for o in orders:
        if o.get("position_id") is not None:
            filled_position_ids.add(int(o["position_id"]))
        oid = int(o["id"])
        if oid in done_orders:
            continue
        secs = tf_seconds(o.get("signal_tf"))
        created = o.get("created_ts")
        if secs is None or created is None:
            continue
        due = float(created) + secs
        if now < due:
            continue
        sym = str(o["symbol"])
        if sym not in price_cache:
            price_cache[sym] = price_of(sym)
        price = price_cache[sym]
        if price is None:
            _log(f"⏸ 挂单 #{oid} {sym} 周期到点但无现价，下轮重试")
            continue
        ref = o.get("filled_price") if o.get("status") == "filled" else o.get("limit_price")
        note = "到点时已成交持仓中" if o.get("status") == "filled" else "到点时挂单未成交"
        delta = _direction_pnl_pct(o.get("side"), ref, price)
        if delta is not None:
            note += f"；较参考价方向盈亏 {delta:+.2f}%"
        with jj._conn() as conn:
            conn.execute(
                """
                INSERT INTO order_tf_snapshots
                  (order_id, position_id, symbol, signal_tf, due_ts, snap_ts,
                   snap_price, entry_ref_price, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (oid, o.get("position_id"), sym, o["signal_tf"], due, now,
                 price, ref, note),
            )
        _log(f"📍 挂单 #{oid} {sym} {o['signal_tf']} 周期到点回录：现价 {price}（{note}）")
        recorded.append({"order_id": oid, "symbol": sym, "signal_tf": o["signal_tf"],
                         "snap_price": price})

    # ── 持仓路径（直接开仓且带 tf 的，如 12 系统单）──
    with jj._conn() as conn:
        poss = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_positions WHERE signal_tf IS NOT NULL AND signal_tf != '' "
            "AND status='open'"
        ).fetchall()]
    done_positions = _snap_position_ids()
    for p in poss:
        pid = int(p["id"])
        if pid in done_positions or pid in filled_position_ids:
            continue   # 挂单成交转来的持仓走挂单路径，不双记
        secs = tf_seconds(p.get("signal_tf"))
        opened = p.get("opened_ts")
        if secs is None or opened is None:
            continue
        due = float(opened) + secs
        if now < due:
            continue
        sym = str(p["symbol"])
        if sym not in price_cache:
            price_cache[sym] = price_of(sym)
        price = price_cache[sym]
        if price is None:
            _log(f"⏸ 持仓 #{pid} {sym} 周期到点但无现价，下轮重试")
            continue
        note = "持仓中"
        delta = _direction_pnl_pct(p.get("side"), p.get("entry_price"), price)
        if delta is not None:
            note += f"；较入场价方向盈亏 {delta:+.2f}%"
        with jj._conn() as conn:
            conn.execute(
                """
                INSERT INTO order_tf_snapshots
                  (order_id, position_id, symbol, signal_tf, due_ts, snap_ts,
                   snap_price, entry_ref_price, note)
                VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (pid, sym, p["signal_tf"], due, now, price, p.get("entry_price"), note),
            )
        _log(f"📍 持仓 #{pid} {sym} {p['signal_tf']} 周期到点回录：现价 {price}（{note}）")
        recorded.append({"position_id": pid, "symbol": sym, "signal_tf": p["signal_tf"],
                         "snap_price": price})
    return recorded


# ─────────────────────────── 盈亏阈值扫描 ───────────────────────────

def _threshold_mail(pos: dict, kind: str, pnl_pct: float, price: float,
                    threshold: float) -> None:
    """组装并发送阈值邮件 + 落去重事件（发送成败都落，防重试轰炸）。"""
    pid = int(pos["id"])
    sym = str(pos.get("symbol") or "—")
    side_cn = "空单" if str(pos.get("side") or "buy").lower() == "sell" else "多单"
    if kind == EVENT_GAIN_ALERT:
        subject = (f"【贾维斯交易提醒】{sym} {side_cn}浮盈 {pnl_pct:+.2f}% "
                   f"达标（≥+{threshold:g}%）")
        head = f"持仓 #{pid} 方向浮盈已达 +{threshold:g}% 阈值"
    else:
        subject = (f"【贾维斯交易提醒】{sym} {side_cn}浮亏 {pnl_pct:+.2f}% "
                   f"预警（≤-{threshold:g}%）")
        head = f"持仓 #{pid} 方向浮亏已破 -{threshold:g}% 阈值"
    lines = [
        head,
        "",
        f"币种：{sym}（{side_cn}）",
        f"信号周期：{pos.get('signal_tf') or '—'}",
        f"入场价：{_fmt(pos.get('entry_price'))}",
        f"当前价：{_fmt(price)}",
        f"方向浮盈亏：{pnl_pct:+.2f}%（价格相对入场价，未计杠杆）",
        f"止损价：{_fmt(pos.get('stop_loss'))}",
        f"止盈价：{_fmt(pos.get('take_profit'))}",
        f"数量：{_fmt(pos.get('qty'))}",
        f"开仓时间：{_fmt_ts(pos.get('opened_ts'))}",
        f"触发时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "（每仓每阈值只提醒一次）",
        "— 贾维斯桌面交易终端 自动发送",
    ]
    send = _deliver(subject, "\n".join(lines))
    _record_event(kind, f"{kind}:pos-{pid}", position_id=pid, symbol=sym, payload={
        "pnl_pct": round(pnl_pct, 4), "price": price, "threshold": threshold,
        "sent": bool(send.get("ok")),
        "send_result": "ok" if send.get("ok") else str(send.get("reason"))[:200],
    })
    label = "浮盈" if kind == EVENT_GAIN_ALERT else "浮亏"
    if send.get("ok"):
        _log(f"📧 持仓 #{pid} {sym} {label} {pnl_pct:+.2f}% 阈值邮件已发送 → {send.get('to')}")
    else:
        _log(f"⚠️ 持仓 #{pid} {sym} {label}阈值邮件发送失败: {send.get('reason')}"[:200])


def _sweep_pnl_thresholds(price_of, price_cache: dict) -> list:
    """扫全部 open 模拟持仓：方向浮盈 ≥ +gain% / 浮亏 ≤ -loss% 各发一封（每仓一次）。

    覆盖所有 open 持仓（不限带 signal_tf 的）；replay 历史回放样本除外。
    """
    ensure_schema()
    gain_th, loss_th = _thresholds()
    fired = []
    with jj._conn() as conn:
        poss = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_positions WHERE status='open' "
            "AND COALESCE(signal_source,'') != 'replay'"
        ).fetchall()]
    for p in poss:
        pid = int(p["id"])
        entry = p.get("entry_price")
        if not entry:
            continue
        need_gain = not _event_exists(f"{EVENT_GAIN_ALERT}:pos-{pid}")
        need_loss = not _event_exists(f"{EVENT_LOSS_ALERT}:pos-{pid}")
        if not need_gain and not need_loss:
            continue   # 两个阈值都发过，不再取价
        sym = str(p["symbol"])
        if sym not in price_cache:
            price_cache[sym] = price_of(sym)
        price = price_cache[sym]
        if price is None:
            continue
        pnl = _direction_pnl_pct(p.get("side"), entry, price)
        if pnl is None:
            continue
        if need_gain and pnl >= gain_th:
            _threshold_mail(p, EVENT_GAIN_ALERT, pnl, price, gain_th)
            fired.append({"position_id": pid, "kind": EVENT_GAIN_ALERT, "pnl_pct": pnl})
        if need_loss and pnl <= -loss_th:
            _threshold_mail(p, EVENT_LOSS_ALERT, pnl, price, loss_th)
            fired.append({"position_id": pid, "kind": EVENT_LOSS_ALERT, "pnl_pct": pnl})
    return fired


# ─────────────────────────── 巡检一轮 ───────────────────────────

def run_sweep(now: float | None = None, price_of=None) -> dict:
    """一轮巡检：周期到点回录 + 盈亏阈值扫描。永不抛出。

    now / price_of 供测试注入（伪造时间与价格）；生产传 None 走真实时钟与行情。
    同一轮内同 symbol 只取一次价。
    """
    t0 = now if now is not None else time.time()
    fn = price_of or _default_price
    price_cache: dict = {}
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "snapshots": [], "alerts": []}
    try:
        out["snapshots"] = _sweep_tf_snapshots(t0, fn, price_cache)
    except Exception as exc:  # noqa: BLE001 — 单环节失败不影响另一环节
        _log(f"⚠️ 周期回录环节异常（已忽略）: {exc!r}"[:200])
        out["snapshot_error"] = repr(exc)[:200]
    try:
        out["alerts"] = _sweep_pnl_thresholds(fn, price_cache)
    except Exception as exc:  # noqa: BLE001
        _log(f"⚠️ 阈值扫描环节异常（已忽略）: {exc!r}"[:200])
        out["alert_error"] = repr(exc)[:200]
    return out


# ─────────────────────────── 后台巡检线程 ───────────────────────────

_MONITOR_THREAD: threading.Thread | None = None
_MONITOR_STOP = threading.Event()
_MONITOR_STATE: dict = {"running": False, "last_run": None, "last_summary": None,
                        "last_error": None}


def _sweep_interval_s() -> int:
    try:
        import jarvis_config as jc_mod
        return max(10, int(jc_mod.get("lifecycle_sweep_interval_s",
                                      DEFAULT_SWEEP_INTERVAL_S)
                           or DEFAULT_SWEEP_INTERVAL_S))
    except Exception:  # noqa: BLE001
        return DEFAULT_SWEEP_INTERVAL_S


def _monitor_loop() -> None:
    _MONITOR_STATE["running"] = True
    while not _MONITOR_STOP.is_set():
        try:
            summary = run_sweep()
            _MONITOR_STATE["last_run"] = summary.get("ts")
            _MONITOR_STATE["last_summary"] = {
                "snapshots": len(summary.get("snapshots") or []),
                "alerts": len(summary.get("alerts") or []),
            }
            _MONITOR_STATE["last_error"] = None
        except Exception as e:  # noqa: BLE001 — 循环永不退出
            _MONITOR_STATE["last_error"] = repr(e)[:300]
        _MONITOR_STOP.wait(_sweep_interval_s())
    _MONITOR_STATE["running"] = False


def start_monitor() -> dict:
    """拉起后台巡检 daemon 线程（幂等：已在跑则直接返回状态）。"""
    global _MONITOR_THREAD
    if _MONITOR_THREAD and _MONITOR_THREAD.is_alive():
        return monitor_status()
    _MONITOR_STOP.clear()
    _MONITOR_THREAD = threading.Thread(target=_monitor_loop,
                                       name="order-lifecycle-monitor", daemon=True)
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


# ─────────────────────────── 查询接口 ───────────────────────────

def _parse_ref(ref: str) -> tuple[str, int] | None:
    """解析引用："order-12" / "pos-7" / 纯数字（按持仓 id）。非法返回 None。"""
    s = str(ref or "").strip().lower()
    if s.startswith("order-"):
        s2 = s[6:]
        return ("order", int(s2)) if s2.isdigit() else None
    if s.startswith("pos-"):
        s2 = s[4:]
        return ("pos", int(s2)) if s2.isdigit() else None
    return ("pos", int(s)) if s.isdigit() else None


def _events_for(order_id: int | None = None, position_id: int | None = None) -> list:
    conds, params = [], []
    if order_id is not None:
        conds.append("order_id=?")
        params.append(order_id)
    if position_id is not None:
        conds.append("position_id=?")
        params.append(position_id)
    if not conds:
        return []
    with jj._conn() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM lifecycle_events WHERE {' OR '.join(conds)} "
            "ORDER BY created_ts", params).fetchall()]
    for r in rows:
        try:
            r["payload"] = json.loads(r.get("payload") or "{}")
        except (TypeError, ValueError):
            pass
    return rows


def _snapshots_for(order_id: int | None = None, position_id: int | None = None) -> list:
    conds, params = [], []
    if order_id is not None:
        conds.append("order_id=?")
        params.append(order_id)
    if position_id is not None:
        conds.append("position_id=?")
        params.append(position_id)
    if not conds:
        return []
    with jj._conn() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM order_tf_snapshots WHERE {' OR '.join(conds)} "
            "ORDER BY snap_ts", params).fetchall()]


def lifecycle_of(ref: str) -> dict:
    """单条完整生命周期：下单 → 周期快照 → 成交 → 阈值事件 → 平仓（时间线）。

    ref 支持 "order-<挂单id>" / "pos-<持仓id>" / 纯数字（按持仓 id）。
    """
    ensure_schema()
    parsed = _parse_ref(ref)
    if not parsed:
        return {"ok": False, "reason": f"无法解析的引用: {ref!r}"}
    kind, rid = parsed

    order = position = None
    with jj._conn() as conn:
        if kind == "order":
            row = conn.execute("SELECT * FROM limit_orders WHERE id=?", (rid,)).fetchone()
            order = dict(row) if row else None
            if order and order.get("position_id") is not None:
                prow = conn.execute("SELECT * FROM paper_positions WHERE id=?",
                                    (int(order["position_id"]),)).fetchone()
                position = dict(prow) if prow else None
        else:
            prow = conn.execute("SELECT * FROM paper_positions WHERE id=?", (rid,)).fetchone()
            position = dict(prow) if prow else None
            orow = conn.execute(
                "SELECT * FROM limit_orders WHERE position_id=? ORDER BY id LIMIT 1",
                (rid,)).fetchone()
            order = dict(orow) if orow else None
    if order is None and position is None:
        return {"ok": False, "reason": "挂单/持仓不存在", "ref": ref}

    oid = int(order["id"]) if order else None
    pid = int(position["id"]) if position else None
    snapshots = _snapshots_for(order_id=oid, position_id=pid)
    events = _events_for(order_id=oid, position_id=pid)

    # 组装时间线（前端可直接按序渲染）
    timeline = []
    if order:
        timeline.append({"ts": order.get("created_ts"), "stage": "placed",
                         "desc": f"挂单创建 @ {_fmt(order.get('limit_price'))}"
                                 + (f"（{order.get('signal_tf')} 信号）"
                                    if order.get("signal_tf") else "")})
        if order.get("filled_ts"):
            timeline.append({"ts": order.get("filled_ts"), "stage": "filled",
                             "desc": f"成交入场 @ {_fmt(order.get('filled_price'))}"})
        if order.get("cancel_ts"):
            timeline.append({"ts": order.get("cancel_ts"), "stage": "cancelled",
                             "desc": "挂单撤销"})
    elif position:
        timeline.append({"ts": position.get("opened_ts"), "stage": "opened",
                         "desc": f"直接开仓 @ {_fmt(position.get('entry_price'))}"
                                 + (f"（{position.get('signal_tf')} 信号）"
                                    if position.get("signal_tf") else "")})
    for s in snapshots:
        timeline.append({"ts": s.get("snap_ts"), "stage": "tf_snapshot",
                         "desc": f"{s.get('signal_tf')} 周期到点回录：现价 "
                                 f"{_fmt(s.get('snap_price'))}（{s.get('note') or ''}）"})
    for e in events:
        stage_cn = {EVENT_ENTRY_MAIL: "入场信号邮件", EVENT_GAIN_ALERT: "浮盈阈值邮件",
                    EVENT_LOSS_ALERT: "浮亏阈值邮件"}.get(e.get("kind"), e.get("kind"))
        payload = e.get("payload") or {}
        sent_cn = "已发送" if payload.get("sent") else f"发送失败({payload.get('send_result')})"
        timeline.append({"ts": e.get("created_ts"), "stage": e.get("kind"),
                         "desc": f"{stage_cn}：{sent_cn}"})
    if position and position.get("closed_ts"):
        timeline.append({"ts": position.get("closed_ts"), "stage": "closed",
                         "desc": f"平仓 @ {_fmt(position.get('exit_price'))}"
                                 f"（{position.get('exit_reason')}，"
                                 f"PnL {position.get('realized_pnl_pct')}%）"})
    timeline.sort(key=lambda x: (x.get("ts") is None, x.get("ts") or 0))

    return {"ok": True, "ref": ref, "order": order, "position": position,
            "snapshots": snapshots, "events": events, "timeline": timeline}


def lifecycle_list(limit: int = 50) -> dict:
    """列表：全部打过周期标的挂单 + 直接开仓带 tf 的持仓（新→旧），带统计计数。"""
    ensure_schema()
    limit = max(1, min(500, int(limit)))
    items = []
    with jj._conn() as conn:
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM limit_orders WHERE signal_tf IS NOT NULL AND signal_tf != '' "
            "ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()]
        linked_pids = {int(o["position_id"]) for o in orders
                       if o.get("position_id") is not None}
        poss = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_positions WHERE signal_tf IS NOT NULL AND signal_tf != '' "
            "ORDER BY opened_ts DESC LIMIT ?", (limit,)).fetchall()]
        snap_o = {int(r["order_id"]): int(r["n"]) for r in conn.execute(
            "SELECT order_id, COUNT(*) AS n FROM order_tf_snapshots "
            "WHERE order_id IS NOT NULL GROUP BY order_id").fetchall()}
        snap_p = {int(r["position_id"]): int(r["n"]) for r in conn.execute(
            "SELECT position_id, COUNT(*) AS n FROM order_tf_snapshots "
            "WHERE position_id IS NOT NULL AND order_id IS NULL "
            "GROUP BY position_id").fetchall()}
        ev_o = {int(r["order_id"]): int(r["n"]) for r in conn.execute(
            "SELECT order_id, COUNT(*) AS n FROM lifecycle_events "
            "WHERE order_id IS NOT NULL GROUP BY order_id").fetchall()}
        ev_p = {int(r["position_id"]): int(r["n"]) for r in conn.execute(
            "SELECT position_id, COUNT(*) AS n FROM lifecycle_events "
            "WHERE position_id IS NOT NULL AND order_id IS NULL "
            "GROUP BY position_id").fetchall()}
    for o in orders:
        oid = int(o["id"])
        # 成交转持仓的单：阈值事件按持仓落库（order_id 为空），计数需合并
        pid_ev = (ev_p.get(int(o["position_id"]), 0)
                  if o.get("position_id") is not None else 0)
        items.append({
            "ref": f"order-{oid}", "kind": "order", "symbol": o.get("symbol"),
            "side": o.get("side"), "signal_tf": o.get("signal_tf"),
            "status": o.get("status"), "created_ts": o.get("created_ts"),
            "position_id": o.get("position_id"),
            "snapshots": snap_o.get(oid, 0), "events": ev_o.get(oid, 0) + pid_ev,
        })
    for p in poss:
        pid = int(p["id"])
        if pid in linked_pids:
            continue   # 挂单成交转来的持仓已在挂单条目中体现
        items.append({
            "ref": f"pos-{pid}", "kind": "position", "symbol": p.get("symbol"),
            "side": p.get("side"), "signal_tf": p.get("signal_tf"),
            "status": p.get("status"), "created_ts": p.get("opened_ts"),
            "position_id": pid,
            "snapshots": snap_p.get(pid, 0), "events": ev_p.get(pid, 0),
        })
    items.sort(key=lambda x: -(x.get("created_ts") or 0))
    return {"ok": True, "items": items[:limit], "total": len(items)}


# ─────────────────────────── CLI（本地验证用） ───────────────────────────

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="贾维斯模拟下单全生命周期追踪")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_tag = sub.add_parser("tag", help="给挂单打周期标")
    p_tag.add_argument("order_id", type=int)
    p_tag.add_argument("signal_tf")

    p_sweep = sub.add_parser("sweep", help="手动跑一轮巡检（到点回录+阈值扫描）")
    p_sweep.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="查看单条生命周期")
    p_show.add_argument("ref", help="order-<id> / pos-<id> / 持仓数字 id")

    p_ls = sub.add_parser("list", help="列出全部打标订单/持仓")
    p_ls.add_argument("--limit", type=int, default=50)

    sub.add_parser("status", help="后台巡检线程状态")

    args = ap.parse_args()
    if args.cmd == "tag":
        print(json.dumps(tag_order_tf(args.order_id, args.signal_tf),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "sweep":
        out = run_sweep()
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else f"巡检完成：回录 {len(out['snapshots'])} 条 / 阈值触发 {len(out['alerts'])} 条")
    elif args.cmd == "show":
        print(json.dumps(lifecycle_of(args.ref), ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        out = lifecycle_list(args.limit)
        for it in out["items"]:
            print(f"{it['ref']:<12} {it['symbol']:<10} {it['side']:<4} "
                  f"tf={it['signal_tf']:<5} [{it['status']}] "
                  f"快照 {it['snapshots']} / 事件 {it['events']}")
        if not out["items"]:
            print("（暂无打标订单/持仓）")
    elif args.cmd == "status":
        print(json.dumps(monitor_status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
