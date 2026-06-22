#!/usr/bin/env python3
"""贾维斯 JARVIS — 模拟跟盘引擎：按决策自动买入/卖出，跟踪盈亏比。

这是 M1 执行手的「自动平仓 + 持仓盈亏跟踪」那半套，把单次买入升级成完整的
**模拟盘跟盘**：根据 `jarvis_brief` 决策自动开仓(买)，并按 止损/止盈/到期/
信号反转 自动平仓(卖)，全程记账，让你观察一段时间的真实盈亏比。

与各模块的分工：
  - `jarvis_brief`     ：出决策（方向/信心/入场/止损/止盈/时间止损）
  - `jarvis_executor`  ：单次下单 + 护栏 + sizing（本引擎复用其下单与配置）
  - 本引擎             ：维护持仓生命周期（开→盯→平）+ 盈亏统计
  - `jarvis_reconcile` ：与 QuantDinger 成交按订单号对账（本引擎开/平仓都登记）

平仓规则（默认，均可在 config 调）：
  1. 现价 ≤ 硬止损          → 止损平仓(stop)
  2. 现价 ≥ 参考止盈        → 止盈平仓(take)
  3. 持仓天数 ≥ 时间止损     → 到期平仓(time)
  4. 最新决策翻成 偏空/中性  → 信号反转平仓(signal)

账户模型（虚拟）：起始权益 `account_equity_usdt`，每仓用决策建议仓位%（经护栏
缩仓）。realized PnL 累加，可算 胜率 / 盈亏比(profit factor) / 平均盈亏。

存储：复用 `~/.vibe-trading/jarvis_journal.db` 的 `paper_positions` 表。

用法：
  export QUANTDINGER_AGENT_TOKEN=qd_agent_xxx
  python jarvis_paper_trader.py cycle --symbols BTC,ETH   # 跑一轮：先盯平仓，再找开仓
  python jarvis_paper_trader.py status                    # 看持仓 + 累计盈亏比
  python jarvis_paper_trader.py report                    # 盈亏比报表
  python jarvis_paper_trader.py close BTCUSDT --reason manual   # 手动平某仓
  python jarvis_paper_trader.py run --symbols BTC,ETH --interval-hours 6  # 常驻跟盘
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

import jarvis_brief as jb
import jarvis_executor as jx
import jarvis_journal as jj
import jarvis_wallet as jw

try:
    import jarvis_reconcile as jr
except Exception:  # noqa: BLE001
    jr = None

LOG_DIR = os.path.expanduser("~/.vibe-trading")
LOG_PATH = os.path.join(LOG_DIR, "jarvis_paper_trader.log")
LONG_PREFIX = "偏多"


def _log(msg: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────── 表结构 ───────────────────────────

def init_positions_table() -> None:
    jj.init_db()
    with jj._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_positions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'open',
                side             TEXT    NOT NULL DEFAULT 'buy',
                qty              REAL    NOT NULL,
                entry_date       TEXT,
                entry_price      REAL,
                entry_order_uid  TEXT,
                stop_loss        REAL,
                take_profit      REAL,
                time_stop_days   INTEGER,
                conviction_score REAL,
                exit_date        TEXT,
                exit_price       REAL,
                exit_order_uid   TEXT,
                exit_reason      TEXT,
                realized_pnl_usdt REAL,
                realized_pnl_pct  REAL,
                opened_ts        REAL,
                closed_ts        REAL
            )
            """
        )


# ─────────────────────────── 取现价 ───────────────────────────

def latest_price(cfg: dict, symbol: str) -> float | None:
    """优先经 Agent Gateway /price 取现价；失败回退 brief 因子价。"""
    sym = symbol if symbol.endswith("USDT") else symbol + "USDT"
    try:
        url = f"{cfg['gateway_base'].rstrip('/')}/api/agent/v1/price"
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {cfg.get('agent_token','')}"},
            params={"market": cfg.get("market", "Crypto"), "symbol": sym},
            timeout=int(cfg.get("request_timeout_s", 30)),
        )
        if resp.status_code == 200:
            p = ((resp.json() or {}).get("data") or {}).get("price")
            if p is not None:
                return float(p)
    except Exception as exc:  # noqa: BLE001
        _log(f"⚠️ {sym} 取现价(gateway)失败: {exc!r}"[:160])
    try:
        b = jb.build(sym)
        return b.get("factor_state", {}).get("price")
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────── 持仓查询 ───────────────────────────

def open_positions(symbol: str | None = None) -> list:
    init_positions_table()
    with jj._conn() as conn:
        q = "SELECT * FROM paper_positions WHERE status='open'"
        params: list = []
        if symbol:
            q += " AND symbol=?"
            params.append((symbol if symbol.endswith("USDT") else symbol + "USDT").upper())
        q += " ORDER BY opened_ts DESC"
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def all_positions(symbol: str | None = None) -> list:
    init_positions_table()
    with jj._conn() as conn:
        q = "SELECT * FROM paper_positions"
        params: list = []
        if symbol:
            q += " WHERE symbol=?"
            params.append((symbol if symbol.endswith("USDT") else symbol + "USDT").upper())
        q += " ORDER BY opened_ts DESC"
        return [dict(r) for r in conn.execute(q, params).fetchall()]


# ─────────────────────────── 开仓 ───────────────────────────

def open_from_decision(symbol: str, cfg: dict, dry_run: bool = False) -> dict:
    """按决策开仓（仅偏多 + 护栏通过 + 该币无未平仓）。"""
    sym = (symbol if symbol.endswith("USDT") else symbol + "USDT").upper()
    if open_positions(sym):
        return {"action": "skip", "symbol": sym, "reason": "已有未平仓持仓"}

    # T-09 组合级熔断门禁
    if not dry_run:
        try:
            import jarvis_circuit_breaker as _cb
            _g = _cb.guard_new_order(cfg)
            if not _g.get("allow"):
                return {"action": "skip", "symbol": sym, "reason": "熔断生效：" + str(_g.get("reason"))}
        except Exception:  # noqa: BLE001
            pass

    try:
        brief = jb.build(sym)
    except Exception as exc:  # noqa: BLE001
        return {"action": "skip", "symbol": sym, "reason": f"决策构建失败: {exc!r}"[:160]}
    dec = brief.get("decision", {})
    if "_error" in dec or "_error" in brief.get("factor_state", {}):
        return {"action": "skip", "symbol": sym, "reason": "决策不可用"}

    guard = jx.evaluate_guardrails(dec, cfg)
    if guard["action"] != "place":
        return {"action": "skip", "symbol": sym, "reason": guard["reason"]}

    entry_price = guard["entry_price"]
    as_of = brief.get("factor_state", {}).get("as_of") or time.strftime("%Y-%m-%d")

    # 钱包预检：按参考入场价估算名义市值，余额不足直接拒单（和真实交易所一致）
    est_notional = round(guard["qty"] * entry_price, 2)
    wal = jw.ensure_account(cfg.get("account_equity_usdt", 1000.0))
    if dry_run:
        return {"action": "would_open", "symbol": sym, "qty": guard["qty"],
                "entry_price": entry_price, "stop_loss": guard["stop_loss"],
                "take_profit": guard["take_profit"], "est_notional_usdt": est_notional,
                "cash_available_usdt": round(wal["cash_usdt"], 2)}
    if est_notional > wal["cash_usdt"] + 1e-9:
        return {"action": "skip", "symbol": sym,
                "reason": f"钱包余额不足：需≈{est_notional}U，可用 {round(wal['cash_usdt'], 2)}U"}

    # 真实下买单
    order_uid = None
    fill = None
    if cfg.get("agent_token"):
        try:
            resp = jx.place_paper_order(cfg, sym, guard, f"jarvis-open-{sym}-{as_of}")
            data = (resp.get("body") or {}).get("data") or {}
            order_uid = data.get("order_uid")
            fill = data.get("fill_price")
        except Exception as exc:  # noqa: BLE001
            _log(f"⚠️ {sym} 开仓下单异常（仍按决策价登记持仓）: {exc!r}"[:160])

    # 成交价优先用真实 fill，回退决策参考价
    eff_entry = float(fill) if fill is not None else entry_price
    notional = round(guard["qty"] * eff_entry, 8)

    # 钱包扣款（按真实成交价口径）；余额不足则不建仓
    deb = jw.debit_buy(sym, notional, ref=f"open-{sym}-{as_of}")
    if not deb.get("ok"):
        return {"action": "skip", "symbol": sym, "reason": deb.get("reason")}

    pid = _insert_position(sym, guard["qty"], as_of, eff_entry, order_uid,
                           guard["stop_loss"], guard["take_profit"],
                           dec.get("time_stop_days", 30), dec.get("conviction_score"))

    if jr and order_uid:
        try:
            jr.link_order(order_uid=order_uid, symbol=sym, as_of_date=as_of,
                          side="buy", qty=guard["qty"], decision_price=entry_price,
                          status=(fill is not None and "filled" or "rejected"))
        except Exception:  # noqa: BLE001
            pass

    _log(f"🟢 {sym} 开仓 #{pid} qty={guard['qty']} 名义{notional}U 入场≈{eff_entry} "
         f"止损{guard['stop_loss']} 止盈{guard['take_profit']} 余额{deb.get('cash_after')}U")
    return {"action": "opened", "symbol": sym, "position_id": pid, "qty": guard["qty"],
            "entry_price": eff_entry, "order_uid": order_uid, "fill_price": fill,
            "notional_usdt": notional, "cash_after": deb.get("cash_after")}


def _insert_position(sym: str, qty: float, entry_date: str, entry_price: float,
                     order_uid: str | None, stop_loss, take_profit,
                     time_stop_days, conviction_score) -> int:
    """登记一条 open 持仓，返回 position_id。"""
    init_positions_table()
    with jj._conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_positions
              (symbol, status, side, qty, entry_date, entry_price, entry_order_uid,
               stop_loss, take_profit, time_stop_days, conviction_score, opened_ts)
            VALUES (?, 'open', 'buy', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sym, qty, entry_date, entry_price, order_uid,
             stop_loss, take_profit, time_stop_days, conviction_score, time.time()),
        )
        return cur.lastrowid


# ─────────────────────────── 平仓 ───────────────────────────

def _close_position(pos: dict, exit_price: float, reason: str, cfg: dict) -> dict:
    """平掉一个持仓：下卖单 + 算 realized PnL + 落库。"""
    sym = pos["symbol"]
    qty = pos["qty"]
    order_uid = None
    if cfg.get("agent_token"):
        try:
            resp = jx.place_paper_order(cfg, sym, {"side": "sell", "qty": qty},
                                        f"jarvis-close-{sym}-{int(time.time())}")
            data = (resp.get("body") or {}).get("data") or {}
            order_uid = data.get("order_uid")
            if data.get("fill_price") is not None:
                exit_price = float(data["fill_price"])
        except Exception as exc:  # noqa: BLE001
            _log(f"⚠️ {sym} 平仓下单异常（仍按现价登记）: {exc!r}"[:160])

    entry = float(pos["entry_price"]) if pos.get("entry_price") else None
    pnl_usdt = pnl_pct = None
    if entry and exit_price and entry > 0:
        pnl_usdt = round((exit_price - entry) * qty, 4)
        pnl_pct = round((exit_price / entry - 1.0) * 100, 2)

    with jj._conn() as conn:
        conn.execute(
            """
            UPDATE paper_positions
            SET status='closed', exit_date=?, exit_price=?, exit_order_uid=?,
                exit_reason=?, realized_pnl_usdt=?, realized_pnl_pct=?, closed_ts=?
            WHERE id=?
            """,
            (time.strftime("%Y-%m-%d"), exit_price, order_uid, reason,
             pnl_usdt, pnl_pct, time.time(), pos["id"]),
        )

    # 钱包回款：卖出所得回到可用现金
    proceeds = round((exit_price or 0) * qty, 8)
    cash_after = None
    if proceeds > 0:
        cr = jw.credit_sell(sym, proceeds, ref=f"close-{sym}-{pos['id']}")
        cash_after = cr.get("cash_after")

    _log(f"🔴 {sym} 平仓 #{pos['id']} 现价≈{exit_price} 原因={reason} PnL={pnl_pct}% ({pnl_usdt}U) "
         f"回款{proceeds}U 余额{cash_after}U")
    return {"symbol": sym, "position_id": pos["id"], "exit_price": exit_price,
            "reason": reason, "pnl_pct": pnl_pct, "pnl_usdt": pnl_usdt, "order_uid": order_uid,
            "proceeds_usdt": proceeds, "cash_after": cash_after}


def _exit_reason(pos: dict, price: float, fresh_direction: str | None) -> str | None:
    """判断是否触发平仓，返回原因或 None。"""
    sl = pos.get("stop_loss")
    tp = pos.get("take_profit")
    if sl and price <= float(sl):
        return "stop"
    if tp and price >= float(tp):
        return "take"
    if pos.get("entry_date") and pos.get("time_stop_days"):
        try:
            held = (time.time() - time.mktime(time.strptime(pos["entry_date"], "%Y-%m-%d"))) / 86400.0
            if held >= float(pos["time_stop_days"]):
                return "time"
        except Exception:  # noqa: BLE001
            pass
    if fresh_direction is not None and not fresh_direction.startswith(LONG_PREFIX):
        return "signal"
    return None


def check_exits(cfg: dict, symbols: list[str] | None = None) -> list:
    """盯所有未平仓持仓，触发条件则平仓。"""
    closed = []
    poss = open_positions()
    if symbols:
        want = {(s if s.endswith("USDT") else s + "USDT").upper() for s in symbols}
        poss = [p for p in poss if p["symbol"] in want]
    for pos in poss:
        price = latest_price(cfg, pos["symbol"])
        if price is None:
            _log(f"⏸ {pos['symbol']} #{pos['id']} 无现价，跳过本轮平仓判断")
            continue
        # 信号反转：取最新决策方向
        fresh_dir = None
        try:
            fresh_dir = jb.build(pos["symbol"]).get("decision", {}).get("direction")
        except Exception:  # noqa: BLE001
            pass
        reason = _exit_reason(pos, price, fresh_dir)
        if reason:
            closed.append(_close_position(pos, price, reason, cfg))
    return closed


# ─────────────────────────── 限价挂单撮合 ───────────────────────────

def match_limit_orders(cfg: dict) -> list:
    """撮合所有 pending 限价单：买单现价≤限价 → 成交建仓；卖单现价≥限价 → 成交平仓。"""
    filled = []
    for o in jw.pending_orders():
        sym = o["symbol"]
        price = latest_price(cfg, sym)
        if price is None:
            _log(f"⏸ 限价单 #{o['id']} {sym} 无现价，跳过撮合")
            continue
        side = o["side"]
        if side == "buy" and price <= float(o["limit_price"]):
            fill_price = float(o["limit_price"])  # 限价单不会比挂价更差
            notional = round(fill_price * o["qty"], 8)
            deb = jw.debit_buy(sym, notional, ref=f"limit-fill-{o['id']}",
                               from_frozen=float(o["notional_usdt"] or 0))
            if not deb.get("ok"):
                _log(f"⚠️ 限价买单 #{o['id']} 扣款失败：{deb.get('reason')}")
                continue
            sl = o["stop_loss"] if o["stop_loss"] else round(fill_price * 0.90, 2)
            tp = o["take_profit"] if o["take_profit"] else round(fill_price * 1.08, 2)
            pid = _insert_position(sym, o["qty"], time.strftime("%Y-%m-%d"), fill_price,
                                   None, sl, tp, o["time_stop_days"] or 30, None)
            jw.mark_filled(o["id"], fill_price, pid)
            _log(f"✅ 限价买单 #{o['id']} {sym} @ {fill_price} 成交 → 持仓 #{pid}（现价 {price}）")
            filled.append({"order_id": o["id"], "side": "buy", "symbol": sym,
                           "fill_price": fill_price, "position_id": pid})
        elif side == "sell" and price >= float(o["limit_price"]):
            fill_price = float(o["limit_price"])
            poss = open_positions(sym)
            if not poss:
                jw.cancel_limit_order(o["id"])
                _log(f"ℹ️ 限价卖单 #{o['id']} {sym} 无持仓可平 → 撤销")
                continue
            target = poss[-1]  # 最早开的先平
            res = _close_position(target, fill_price, "limit_sell", cfg)
            jw.mark_filled(o["id"], fill_price, target["id"])
            _log(f"✅ 限价卖单 #{o['id']} {sym} @ {fill_price} 成交 → 平仓 #{target['id']}（现价 {price}）")
            filled.append({"order_id": o["id"], "side": "sell", "symbol": sym,
                           "fill_price": fill_price, "position_id": target["id"], "close": res})
    return filled


# ─────────────────────────── 跟盘循环 ───────────────────────────

def _notify_actions(out: dict) -> None:
    """有开/平仓动作时推送一条摘要通知。

    惰性导入 jarvis_notify；未配置渠道自动跳过、异常只记日志不外抛，绝不影响跟盘主流程。
    """
    try:
        import jarvis_notify as jn
        lines = [f"🤖 贾维斯自动跟盘 · {out.get('ts')}"]
        for c in out.get("closed") or []:
            lines.append(f"🔴 平仓 {c.get('symbol')} 原因={c.get('reason')} "
                         f"PnL={c.get('pnl_pct')}% ({c.get('pnl_usdt')}U)")
        for o in out.get("opened") or []:
            lines.append(f"🟢 开仓 {o.get('symbol')} 入场≈{o.get('entry_price')} "
                         f"名义{o.get('notional_usdt')}U")
        lines.append(f"当前持仓 {out.get('open_after')} 个")
        jn.notify("\n".join(lines))
    except Exception as exc:  # noqa: BLE001 — 通知失败不影响跟盘
        _log(f"通知推送失败（已忽略）: {exc!r}"[:160])


def run_cycle(symbols: list[str], cfg: dict, dry_run: bool = False,
              notify_on_action: bool = False) -> dict:
    """一轮跟盘：先撮合限价单，再盯平仓，最后按决策找新开仓。

    notify_on_action=True 且非 dry_run 且本轮有开/平仓时，推送一条摘要通知（用于无人值守的
    daemon 自动跟盘）；看板手动触发默认不推送，避免打扰在屏前的操作。
    """
    syms = [(s if s.endswith("USDT") else s + "USDT").upper() for s in symbols]
    matched = [] if dry_run else match_limit_orders(cfg)
    closed = check_exits(cfg, syms)
    opened = []
    for s in syms:
        r = open_from_decision(s, cfg, dry_run=dry_run)
        if r.get("action") in ("opened", "would_open"):
            opened.append(r)
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "matched": matched, "closed": closed,
           "opened": opened, "open_after": len(open_positions())}
    _log(f"🔁 跟盘一轮：限价成交 {len(matched)} / 平仓 {len(closed)} / 开仓 {len(opened)} / 当前持仓 {out['open_after']}")
    if notify_on_action and not dry_run and (opened or closed):
        _notify_actions(out)
    return out


# ─────────────────────────── 盈亏统计 ───────────────────────────

def stats(cfg: dict | None = None, symbol: str | None = None) -> dict:
    """累计盈亏比统计 + 未平仓浮盈。"""
    cfg = cfg or jx.load_config()
    wal = jw.ensure_account(cfg.get("account_equity_usdt", 1000.0))
    poss = all_positions(symbol)
    closed = [p for p in poss if p["status"] == "closed" and p.get("realized_pnl_usdt") is not None]
    opens = [p for p in poss if p["status"] == "open"]

    wins = [p for p in closed if (p["realized_pnl_usdt"] or 0) > 0]
    losses = [p for p in closed if (p["realized_pnl_usdt"] or 0) < 0]
    gross_profit = round(sum(p["realized_pnl_usdt"] for p in wins), 4) if wins else 0.0
    gross_loss = round(sum(p["realized_pnl_usdt"] for p in losses), 4) if losses else 0.0
    realized = round(sum(p["realized_pnl_usdt"] or 0 for p in closed), 4)
    avg_win = round(gross_profit / len(wins), 4) if wins else None
    avg_loss = round(gross_loss / len(losses), 4) if losses else None
    profit_factor = round(gross_profit / abs(gross_loss), 3) if gross_loss else (None if not wins else float("inf"))
    win_rate = round(100 * len(wins) / len(closed), 1) if closed else None

    # 未平仓浮盈 + 持仓市值（需现价）
    unrealized = 0.0
    holdings_value = 0.0
    open_detail = []
    for p in opens:
        price = latest_price(cfg, p["symbol"]) if cfg.get("agent_token") else None
        upnl = None
        if price and p.get("entry_price"):
            upnl = round((price - p["entry_price"]) * p["qty"], 4)
            unrealized += upnl
        if price:
            holdings_value += price * p["qty"]
        open_detail.append({"symbol": p["symbol"], "id": p["id"], "entry": p.get("entry_price"),
                            "qty": p["qty"], "cur_price": price, "unrealized_usdt": upnl})

    cash = round(wal["cash_usdt"], 2)
    frozen = round(wal["frozen_usdt"], 2)
    equity = round(cash + frozen + holdings_value, 2)
    initial = round(wal["initial_deposit_usdt"], 2)
    return {
        "closed_trades": len(closed),
        "open_positions": len(opens),
        "win_rate_pct": win_rate,
        "wins": len(wins), "losses": len(losses),
        "gross_profit_usdt": gross_profit, "gross_loss_usdt": gross_loss,
        "realized_pnl_usdt": realized,
        "avg_win_usdt": avg_win, "avg_loss_usdt": avg_loss,
        "profit_factor": profit_factor if profit_factor != float("inf") else "∞（暂无亏损）",
        "unrealized_pnl_usdt": round(unrealized, 4),
        "total_pnl_usdt": round(realized + unrealized, 4),
        "start_equity_usdt": initial,
        "cash_usdt": cash,
        "frozen_usdt": frozen,
        "holdings_value_usdt": round(holdings_value, 2),
        "equity_usdt": equity,
        "equity_change_pct": round((equity / initial - 1.0) * 100, 2) if initial else None,
        "open_detail": open_detail,
    }


def to_markdown(st: dict) -> str:
    lines = [
        "# 贾维斯模拟跟盘 · 盈亏比报表",
        "",
        f"- 💰 钱包总权益 {st.get('equity_usdt')}U（现金 {st.get('cash_usdt')} + 冻结 {st.get('frozen_usdt')} "
        f"+ 持仓市值 {st.get('holdings_value_usdt')}） | 较起始 {st.get('equity_change_pct')}%",
        f"- 已平仓 {st['closed_trades']} 笔 | 持仓中 {st['open_positions']} 笔 | 起始入金 {st['start_equity_usdt']}U",
        f"- 胜率 {st['win_rate_pct'] if st['win_rate_pct'] is not None else '—'}%"
        f"（{st['wins']} 胜 / {st['losses']} 负）",
        f"- **盈亏比(profit factor) {st['profit_factor']}** | 平均盈 {st['avg_win_usdt']}U / 平均亏 {st['avg_loss_usdt']}U",
        f"- 已实现盈亏 {st['realized_pnl_usdt']}U | 浮动盈亏 {st['unrealized_pnl_usdt']}U | **合计 {st['total_pnl_usdt']}U**",
    ]
    if st["open_detail"]:
        lines += ["", "## 当前持仓", "",
                  "| 币种 | #ID | 入场价 | 数量 | 现价 | 浮盈U |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for d in st["open_detail"]:
            lines.append(
                f"| {d['symbol']} | {d['id']} | {d.get('entry')} | {d['qty']} "
                f"| {d.get('cur_price') if d.get('cur_price') is not None else '取价失败'} "
                f"| {d.get('unrealized_usdt') if d.get('unrealized_usdt') is not None else '—'} |"
            )
    if st["closed_trades"] == 0 and st["open_positions"] == 0:
        lines += ["", "> 还没有跟盘记录。先 `python jarvis_paper_trader.py cycle --symbols BTC,ETH`。"]
    return "\n".join(lines)


# ─────────────────────────── CLI ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯模拟跟盘引擎：自动买卖 + 盈亏比跟踪")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cyc = sub.add_parser("cycle", help="跑一轮：先盯平仓再找开仓")
    p_cyc.add_argument("--symbols", default="BTCUSDT", help="逗号分隔，如 BTC,ETH")
    p_cyc.add_argument("--equity", type=float, default=None)
    p_cyc.add_argument("--dry-run", action="store_true")
    p_cyc.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="常驻跟盘循环")
    p_run.add_argument("--symbols", default="BTCUSDT")
    p_run.add_argument("--interval-hours", type=float, default=6.0)
    p_run.add_argument("--equity", type=float, default=None)

    p_st = sub.add_parser("status", help="看持仓 + 累计盈亏比")
    p_st.add_argument("--symbol", default=None)
    p_st.add_argument("--json", action="store_true")

    p_rep = sub.add_parser("report", help="盈亏比报表（同 status 的 markdown）")
    p_rep.add_argument("--symbol", default=None)

    p_cl = sub.add_parser("close", help="手动平某币所有未平仓")
    p_cl.add_argument("symbol")
    p_cl.add_argument("--reason", default="manual")

    p_lim = sub.add_parser("limit", help="挂限价单（自己指定点位买/卖）")
    p_lim.add_argument("symbol")
    p_lim.add_argument("side", choices=["buy", "sell"])
    p_lim.add_argument("price", type=float, help="限价")
    p_lim.add_argument("qty", type=float, help="数量")
    p_lim.add_argument("--stop-loss", type=float, default=None)
    p_lim.add_argument("--take-profit", type=float, default=None)
    p_lim.add_argument("--time-stop-days", type=int, default=30)

    p_ord = sub.add_parser("orders", help="查看限价挂单簿")
    p_ord.add_argument("--symbol", default=None)
    p_ord.add_argument("--all", action="store_true", help="含已成交/已撤")
    p_ord.add_argument("--json", action="store_true")

    p_can = sub.add_parser("cancel", help="撤销某个限价挂单")
    p_can.add_argument("order_id", type=int)

    p_match = sub.add_parser("match", help="手动触发一次限价单撮合")
    p_match.add_argument("--json", action="store_true")

    args = ap.parse_args()
    cli = {}
    if getattr(args, "equity", None) is not None:
        cli["account_equity_usdt"] = args.equity
    cfg = jx.load_config(cli)

    if args.cmd == "cycle":
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        out = run_cycle(syms, cfg, dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json
              else f"跟盘完成：限价成交 {len(out['matched'])} / 平仓 {len(out['closed'])} / 开仓 {len(out['opened'])} / 持仓 {out['open_after']}")
    elif args.cmd == "run":
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        _log(f"▶️ 常驻跟盘启动：{syms} 每 {args.interval_hours}h")
        while True:
            try:
                run_cycle(syms, cfg)
            except Exception as exc:  # noqa: BLE001 — 跟盘循环永不退出
                _log(f"❌ 跟盘轮异常（继续）: {exc!r}"[:200])
            time.sleep(max(60.0, args.interval_hours * 3600.0))
    elif args.cmd == "status":
        st = stats(cfg, args.symbol)
        print(json.dumps(st, ensure_ascii=False, indent=2) if args.json else to_markdown(st))
    elif args.cmd == "report":
        print(to_markdown(stats(cfg, args.symbol)))
    elif args.cmd == "close":
        poss = open_positions(args.symbol)
        if not poss:
            print(f"{args.symbol} 无未平仓持仓")
        else:
            for p in poss:
                price = latest_price(cfg, p["symbol"]) or p.get("entry_price")
                print(_close_position(p, price, args.reason, cfg))
    elif args.cmd == "limit":
        jw.ensure_account(cfg.get("account_equity_usdt", 1000.0))
        res = jw.place_limit_order(args.symbol, args.side, args.price, args.qty,
                                   stop_loss=args.stop_loss, take_profit=args.take_profit,
                                   time_stop_days=args.time_stop_days)
        if res.get("ok"):
            print(f"✅ 已挂{('买' if args.side == 'buy' else '卖')}单 #{res['order_id']}: "
                  f"{res['symbol']} {res['qty']} @ {res['limit_price']}"
                  + (f"（冻结 {res['frozen_usdt']}U）" if args.side == "buy" else ""))
        else:
            print(f"❌ 挂单失败：{res.get('reason')}")
    elif args.cmd == "orders":
        rows = jw.all_limit_orders(args.symbol) if args.all else jw.pending_orders(args.symbol)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif not rows:
            print("（无挂单）")
        else:
            for o in rows:
                print(f"#{o['id']} {o['symbol']} {o['side']} {o['qty']} @ {o['limit_price']} "
                      f"[{o['status']}]" + (f" 成交价 {o['filled_price']}" if o.get('filled_price') else ""))
    elif args.cmd == "cancel":
        res = jw.cancel_limit_order(args.order_id)
        print(f"✅ 已撤单 #{args.order_id}" if res.get("ok") else f"❌ {res.get('reason')}")
    elif args.cmd == "match":
        matched = match_limit_orders(cfg)
        print(json.dumps(matched, ensure_ascii=False, indent=2) if args.json
              else f"撮合完成：成交 {len(matched)} 笔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
