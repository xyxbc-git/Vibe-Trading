#!/usr/bin/env python3
"""贾维斯 JARVIS — 模拟钱包余额台账 + 限价挂单簿。

把原来「起始权益 + 逐仓盈亏统计」升级成一个**真实记账的虚拟钱包**：
  - 钱包有 可用现金(cash) 与 挂单冻结(frozen)，开仓买入即扣现金、平仓卖出即回款。
  - 每一笔出入账都写 `wallet_ledger` 流水（充值/买/卖/冻结/解冻），余额可逐笔追溯。
  - 限价挂单簿 `limit_orders`：按你指定的点位挂单，下单即冻结资金，到价才成交。
  - 余额不足直接拒单——和真实交易所一样的硬约束。

所有表与 `jarvis_journal.db` 同库（~/.vibe-trading/jarvis_journal.db），便于统一管理。

用法（CLI）：
  python jarvis_wallet.py init --deposit 1000      # 首次入金（幂等，只在无账户时建）
  python jarvis_wallet.py deposit 500              # 追加入金
  python jarvis_wallet.py balance                  # 看余额
  python jarvis_wallet.py ledger --limit 20        # 看流水
  python jarvis_wallet.py reset --deposit 1000     # 清空重置（危险，仅模拟盘）
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import jarvis_journal as jj

DEFAULT_DEPOSIT = 1000.0


# ─────────────────────────── 建表 ───────────────────────────

def init_db() -> None:
    jj.init_db()
    with jj._conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet (
                id                   INTEGER PRIMARY KEY CHECK (id = 1),
                cash_usdt            REAL NOT NULL,
                frozen_usdt          REAL NOT NULL DEFAULT 0,
                initial_deposit_usdt REAL NOT NULL,
                created_ts           REAL,
                updated_ts           REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_ledger (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                dt           TEXT,
                type         TEXT NOT NULL,
                symbol       TEXT,
                amount_usdt  REAL NOT NULL,
                cash_after   REAL,
                frozen_after REAL,
                ref          TEXT,
                note         TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS limit_orders (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol         TEXT NOT NULL,
                side           TEXT NOT NULL,
                limit_price    REAL NOT NULL,
                qty            REAL NOT NULL,
                notional_usdt  REAL,
                status         TEXT NOT NULL DEFAULT 'pending',
                stop_loss      REAL,
                take_profit    REAL,
                time_stop_days INTEGER,
                created_date   TEXT,
                created_ts     REAL,
                filled_price   REAL,
                filled_ts      REAL,
                cancel_ts      REAL,
                position_id    INTEGER,
                note           TEXT
            )
            """
        )


# ─────────────────────────── 账户 ───────────────────────────

def ensure_account(initial: float = DEFAULT_DEPOSIT) -> dict:
    """没有账户则按 initial 入金建账；已有则原样返回。"""
    init_db()
    w = get_wallet()
    if w:
        return w
    now = time.time()
    with jj._conn() as conn:
        conn.execute(
            "INSERT INTO wallet (id, cash_usdt, frozen_usdt, initial_deposit_usdt, created_ts, updated_ts) "
            "VALUES (1, ?, 0, ?, ?, ?)",
            (float(initial), float(initial), now, now),
        )
    _add_ledger("deposit", None, float(initial), ref="init", note="首次入金")
    return get_wallet()


def get_wallet() -> dict | None:
    init_db()
    with jj._conn() as conn:
        row = conn.execute("SELECT * FROM wallet WHERE id = 1").fetchone()
    return dict(row) if row else None


def _update_wallet(cash: float, frozen: float) -> None:
    with jj._conn() as conn:
        conn.execute(
            "UPDATE wallet SET cash_usdt = ?, frozen_usdt = ?, updated_ts = ? WHERE id = 1",
            (round(cash, 8), round(frozen, 8), time.time()),
        )


def _add_ledger(typ: str, symbol: str | None, amount: float, ref: str | None = None,
                note: str | None = None) -> None:
    w = get_wallet() or {"cash_usdt": None, "frozen_usdt": None}
    now = time.time()
    with jj._conn() as conn:
        conn.execute(
            "INSERT INTO wallet_ledger (ts, dt, type, symbol, amount_usdt, cash_after, frozen_after, ref, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, time.strftime("%Y-%m-%d %H:%M:%S"), typ, symbol, round(amount, 8),
             w.get("cash_usdt"), w.get("frozen_usdt"), ref, note),
        )


# ─────────────────────────── 资金操作 ───────────────────────────

def deposit(amount: float, note: str | None = None) -> dict:
    """充值入金。"""
    if amount <= 0:
        raise ValueError("入金金额必须 > 0")
    w = ensure_account()
    _update_wallet(w["cash_usdt"] + amount, w["frozen_usdt"])
    _add_ledger("deposit", None, amount, ref="deposit", note=note or "追加入金")
    return get_wallet()


def available(w: dict | None = None) -> float:
    w = w or ensure_account()
    return round(w["cash_usdt"], 8)


def debit_buy(symbol: str, notional: float, ref: str | None = None,
              from_frozen: float = 0.0) -> dict:
    """买入扣款。from_frozen 表示这笔有多少出自已冻结资金（限价单成交）。

    返回 {"ok": bool, "reason"|"cash_after"}。余额不足返回 ok=False，不扣款。
    """
    w = ensure_account()
    pay_from_cash = max(0.0, notional - from_frozen)
    if pay_from_cash > w["cash_usdt"] + 1e-9:
        return {"ok": False, "reason": f"余额不足：需现金 {round(pay_from_cash, 2)}U，可用 {round(w['cash_usdt'], 2)}U"}
    new_frozen = w["frozen_usdt"]
    if from_frozen > 0:
        new_frozen = max(0.0, w["frozen_usdt"] - from_frozen)
    _update_wallet(w["cash_usdt"] - pay_from_cash, new_frozen)
    _add_ledger("buy", symbol, -notional, ref=ref, note="买入扣款")
    return {"ok": True, "cash_after": get_wallet()["cash_usdt"]}


def credit_sell(symbol: str, proceeds: float, ref: str | None = None) -> dict:
    """卖出回款。"""
    w = ensure_account()
    _update_wallet(w["cash_usdt"] + proceeds, w["frozen_usdt"])
    _add_ledger("sell", symbol, proceeds, ref=ref, note="卖出回款")
    return {"ok": True, "cash_after": get_wallet()["cash_usdt"]}


def freeze(symbol: str, amount: float, ref: str | None = None) -> dict:
    """限价买单挂单：把资金从可用现金移到冻结。余额不足返回 ok=False。"""
    w = ensure_account()
    if amount > w["cash_usdt"] + 1e-9:
        return {"ok": False, "reason": f"余额不足以冻结：需 {round(amount, 2)}U，可用 {round(w['cash_usdt'], 2)}U"}
    _update_wallet(w["cash_usdt"] - amount, w["frozen_usdt"] + amount)
    _add_ledger("freeze", symbol, -amount, ref=ref, note="限价挂单冻结")
    return {"ok": True}


def unfreeze(symbol: str, amount: float, ref: str | None = None) -> dict:
    """撤单/成交差额：把冻结资金退回可用现金。"""
    w = ensure_account()
    amt = min(amount, w["frozen_usdt"])
    _update_wallet(w["cash_usdt"] + amt, w["frozen_usdt"] - amt)
    _add_ledger("unfreeze", symbol, amt, ref=ref, note="解冻退回")
    return {"ok": True}


# ─────────────────────────── 限价挂单簿 ───────────────────────────

def place_limit_order(symbol: str, side: str, limit_price: float, qty: float,
                      stop_loss: float | None = None, take_profit: float | None = None,
                      time_stop_days: int | None = 30, note: str | None = None) -> dict:
    """登记一笔限价挂单。买单立即冻结 limit_price*qty 资金；余额不足拒单。"""
    init_db()
    sym = (symbol if symbol.endswith("USDT") else symbol + "USDT").upper()
    side = side.lower()
    if side not in ("buy", "sell"):
        return {"ok": False, "reason": "side 必须是 buy 或 sell"}
    if limit_price <= 0 or qty <= 0:
        return {"ok": False, "reason": "价格与数量必须 > 0"}

    notional = round(limit_price * qty, 8)
    if side == "buy":
        fr = freeze(sym, notional, ref=f"limit-buy-{sym}")
        if not fr.get("ok"):
            return {"ok": False, "reason": fr.get("reason")}

    with jj._conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO limit_orders
              (symbol, side, limit_price, qty, notional_usdt, status, stop_loss, take_profit,
               time_stop_days, created_date, created_ts, note)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
            """,
            (sym, side, float(limit_price), float(qty), notional, stop_loss, take_profit,
             time_stop_days, time.strftime("%Y-%m-%d"), time.time(), note),
        )
        oid = cur.lastrowid
    return {"ok": True, "order_id": oid, "symbol": sym, "side": side,
            "limit_price": float(limit_price), "qty": float(qty), "frozen_usdt": notional}


def pending_orders(symbol: str | None = None) -> list:
    init_db()
    with jj._conn() as conn:
        q = "SELECT * FROM limit_orders WHERE status = 'pending'"
        params: list = []
        if symbol:
            q += " AND symbol = ?"
            params.append((symbol if symbol.endswith("USDT") else symbol + "USDT").upper())
        q += " ORDER BY created_ts DESC"
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def all_limit_orders(symbol: str | None = None, limit: int = 100) -> list:
    init_db()
    with jj._conn() as conn:
        q = "SELECT * FROM limit_orders"
        params: list = []
        if symbol:
            q += " WHERE symbol = ?"
            params.append((symbol if symbol.endswith("USDT") else symbol + "USDT").upper())
        q += " ORDER BY created_ts DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_order(order_id: int) -> dict | None:
    init_db()
    with jj._conn() as conn:
        row = conn.execute("SELECT * FROM limit_orders WHERE id = ?", (order_id,)).fetchone()
    return dict(row) if row else None


def cancel_limit_order(order_id: int) -> dict:
    """撤掉挂单：买单解冻退回资金。"""
    o = get_order(order_id)
    if not o:
        return {"ok": False, "reason": "挂单不存在"}
    if o["status"] != "pending":
        return {"ok": False, "reason": f"挂单状态为 {o['status']}，无法撤销"}
    if o["side"] == "buy":
        unfreeze(o["symbol"], o["notional_usdt"], ref=f"cancel-{order_id}")
    with jj._conn() as conn:
        conn.execute("UPDATE limit_orders SET status = 'cancelled', cancel_ts = ? WHERE id = ?",
                     (time.time(), order_id))
    return {"ok": True, "order_id": order_id}


def mark_filled(order_id: int, fill_price: float, position_id: int | None = None) -> None:
    with jj._conn() as conn:
        conn.execute(
            "UPDATE limit_orders SET status = 'filled', filled_price = ?, filled_ts = ?, position_id = ? WHERE id = ?",
            (float(fill_price), time.time(), position_id, order_id),
        )


def ledger(limit: int = 50, symbol: str | None = None) -> list:
    init_db()
    with jj._conn() as conn:
        q = "SELECT * FROM wallet_ledger"
        params: list = []
        if symbol:
            q += " WHERE symbol = ?"
            params.append((symbol if symbol.endswith("USDT") else symbol + "USDT").upper())
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def reset(initial: float = DEFAULT_DEPOSIT) -> dict:
    """清空钱包、流水、挂单并重新入金（仅模拟盘调试用）。"""
    init_db()
    with jj._conn() as conn:
        conn.execute("DELETE FROM wallet")
        conn.execute("DELETE FROM wallet_ledger")
        conn.execute("DELETE FROM limit_orders")
    return ensure_account(initial)


# ─────────────────────────── CLI ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯模拟钱包台账")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="首次入金建账（幂等）")
    p_init.add_argument("--deposit", type=float, default=DEFAULT_DEPOSIT)

    p_dep = sub.add_parser("deposit", help="追加入金")
    p_dep.add_argument("amount", type=float)

    sub.add_parser("balance", help="查看余额")

    p_led = sub.add_parser("ledger", help="查看流水")
    p_led.add_argument("--limit", type=int, default=20)
    p_led.add_argument("--symbol", default=None)

    p_rst = sub.add_parser("reset", help="清空重置（危险）")
    p_rst.add_argument("--deposit", type=float, default=DEFAULT_DEPOSIT)
    p_rst.add_argument("--yes", action="store_true", help="确认清空")

    args = ap.parse_args()

    if args.cmd == "init":
        w = ensure_account(args.deposit)
        print(f"✅ 账户就绪：可用 {w['cash_usdt']}U 冻结 {w['frozen_usdt']}U 起始入金 {w['initial_deposit_usdt']}U")
    elif args.cmd == "deposit":
        w = deposit(args.amount)
        print(f"✅ 已入金 {args.amount}U → 可用 {w['cash_usdt']}U")
    elif args.cmd == "balance":
        w = ensure_account()
        print(json.dumps(w, ensure_ascii=False, indent=2))
    elif args.cmd == "ledger":
        rows = ledger(args.limit, args.symbol)
        if not rows:
            print("（暂无流水）")
        for r in rows:
            print(f"#{r['id']} [{r['dt']}] {r['type']:8} {r.get('symbol') or '-':10} "
                  f"{r['amount_usdt']:+.2f}U → 现金 {r['cash_after']}U {r.get('note') or ''}")
    elif args.cmd == "reset":
        if not args.yes:
            print("⚠️ 这会清空钱包/流水/挂单。确认请加 --yes")
            return 1
        w = reset(args.deposit)
        print(f"✅ 已重置 → 可用 {w['cash_usdt']}U")
    return 0


if __name__ == "__main__":
    sys.exit(main())
