#!/usr/bin/env python3
"""贾维斯 JARVIS — 13诊断 D7：反向信号影子验证（独立进程 CLI）。

问题：D1 归因识别出的「某环境稳定亏」组合（system×tf×ctx_regime）只是统计
候选；『稳定亏 = 可反向利用的 alpha』必须用影子单**前向验证**，不能拿历史
flip 理论值（未扣滑点、SL/TP 结构不对称）直接上实盘/模拟主台账。

机制：
  1. 读 D1 stable_losers（与 dashboard api_twelve_attribution 同 SQL 口径，
     直查 twelve_sim_trade——诊断用 twelve_sim 实际成交口径，非信号边沿回测）；
  2. 对命中组合订阅 read_signals(sym) 对应槽位的当前态信号，**方向取反**开
     影子纸面单（SL/TP 镜像互换：影子 SL=原向 TP、影子 TP=原向 SL）；
  3. 环境匹配门禁：仅当开仓时刻 _market_context 的 ctx_regime 与
     stable_loser 的 env 一致才开（环境错配的反向没有统计依据）；
     环境取不到（None）同样不开——影子实验宁缺毋滥，与主链路「放行哲学」
     相反是有意为之：影子单本身就是验证实验，样本纯度优先；
  4. 生命周期复用 jarvis_twelve_trader 纯函数（_exit_check/_timeout_due，
     import 复用不复制粘贴）：爆仓 > 止损 > 止盈 > 时间止损；
  5. report 子命令：原向 vs 反向 净利/胜率/费用对照，影子样本 ≥
     SHADOW_MIN_SAMPLES 才输出结论行。

边界（硬约束）：
  - **零接触**主台账五表（twelve_sim_wallet/position/trade/signal_log/config
    只读，绝不 INSERT/UPDATE/DELETE）；影子数据独立三表
    twelve_shadow_wallet / twelve_shadow_position / twelve_shadow_trade；
  - 只读 twelve_signal_state（经 jtt.read_signals），绝不触发 12 信号重算；
  - 独立进程运行（cycle / run），绝不挂 dashboard 请求路径；
  - 不新增出网端点：现价/K线/环境全走 jarvis_twelve_trader 既有通道
    （latest_price / _fetch_bars / _market_context，均带缓存限频）。

样本不足时优雅空转：stable_losers 为空 → cycle 返回 idle=True，不开任何单。

用法：
  python3 jarvis_twelve_shadow.py cycle                       # 跑一轮（默认读 sim 已配置币种）
  python3 jarvis_twelve_shadow.py cycle --symbols ETH,BTC --days 7
  python3 jarvis_twelve_shadow.py run --interval-min 5        # 常驻循环
  python3 jarvis_twelve_shadow.py report --days 30            # 原向 vs 反向对照
  python3 jarvis_twelve_shadow.py status                      # 影子钱包/持仓概览
"""

from __future__ import annotations

import argparse
import json
import os
import time

import jarvis_twelve_trader as jtt

DB_DIR = os.path.expanduser("~/.vibe-trading")
LOG_PATH = os.path.join(DB_DIR, "jarvis_twelve_shadow.log")

# ── 参数（模块常量）────────────────────────────────────────────────────────
# TODO(后续单独任务): 迁入 jarvis_config DEFAULTS+GROUPS（signal 组）+ BOUNDS，
# 键名建议 twelve_shadow_diag_days / twelve_shadow_min_samples /
# twelve_shadow_max_winrate / twelve_shadow_principal / twelve_shadow_max_open。
SHADOW_DIAG_DAYS = 7          # stable_losers 统计回看窗口（天）
SHADOW_MIN_SAMPLES = 20       # 对齐 D1 twelve_diag_min_samples 默认
SHADOW_MAX_WINRATE = 30.0     # 对齐 D1 twelve_diag_max_winrate 默认
SHADOW_PRINCIPAL = 100.0      # 影子槽位本金（U），口径对齐主台账
SHADOW_MIN_BALANCE = 1.0      # 影子槽位余额下限，低于视为爆槽不再开新仓

_INITED = False


def _log(msg: str) -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [shadow] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — 日志失败不拖垮循环
        pass


def _conn():
    """复用 jarvis_twelve_trader 的连接工厂：同一 journal 库、同一方言兼容层；
    冒烟测试重定向 jtt.DB_PATH 即可整体隔离。"""
    return jtt._conn()


# ─────────────────────────── 建表（独立三表，零接触主台账） ───────────────────────────

_SRC_COLUMNS_DDL = (
    ("src_system", "TEXT"),            # 稳定亏候选来源 system（=行内 system，留痕溯源）
    ("src_env", "TEXT"),               # 稳定亏候选环境（ctx_regime 口径）
    ("flip_of_position_id", "INTEGER"),  # 同槽位原向在途持仓 id（无则 NULL）
)


def init_db() -> None:
    global _INITED
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_shadow_wallet (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT NOT NULL,
                tf               TEXT NOT NULL,
                system           TEXT NOT NULL,
                name_cn          TEXT,
                principal        REAL NOT NULL DEFAULT 100,
                balance          REAL NOT NULL DEFAULT 100,
                equity           REAL NOT NULL DEFAULT 100,
                total_trades     INTEGER NOT NULL DEFAULT 0,
                win_trades       INTEGER NOT NULL DEFAULT 0,
                total_pnl        REAL NOT NULL DEFAULT 0,
                win_rate         REAL,
                updated_ts       REAL,
                UNIQUE (symbol, tf, system)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_shadow_position (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol         TEXT NOT NULL,
                tf             TEXT NOT NULL,
                system         TEXT NOT NULL,
                direction      TEXT NOT NULL,
                entry_price    REAL NOT NULL,
                entry_ts       REAL NOT NULL,
                qty            REAL NOT NULL,
                margin         REAL NOT NULL,
                leverage       REAL NOT NULL DEFAULT 1,
                position_pct   REAL,
                stop_loss      REAL,
                take_profit    REAL,
                cur_price      REAL,
                unrealized_pnl REAL,
                status         TEXT NOT NULL DEFAULT 'open',
                src_system     TEXT,
                src_env        TEXT,
                flip_of_position_id INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tshp_sym_status "
            "ON twelve_shadow_position(symbol, status)"
        )
        # 结构镜像 twelve_sim_trade（含 funding_fee 与 D0 ctx 列）+ 3 个 src 列
        ctx_cols = ",\n                ".join(
            f"{c} {t}" for c, t in jtt.CTX_COLUMNS_DDL)
        src_cols = ",\n                ".join(
            f"{c} {t}" for c, t in _SRC_COLUMNS_DDL)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS twelve_shadow_trade (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                tf              TEXT NOT NULL,
                system          TEXT NOT NULL,
                name_cn         TEXT,
                direction       TEXT NOT NULL,
                entry_price     REAL NOT NULL,
                entry_ts        REAL NOT NULL,
                exit_price      REAL NOT NULL,
                exit_ts         REAL NOT NULL,
                qty             REAL NOT NULL,
                margin          REAL NOT NULL,
                leverage        REAL NOT NULL DEFAULT 1,
                stop_loss       REAL,
                take_profit     REAL,
                exit_reason     TEXT NOT NULL,
                pnl             REAL NOT NULL,
                pnl_pct         REAL,
                rr              REAL,
                balance_after   REAL,
                holding_minutes REAL,
                funding_fee     REAL,
                {ctx_cols},
                {src_cols}
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tsht_combo "
            "ON twelve_shadow_trade(system, tf, src_env, exit_ts)"
        )
    _INITED = True


def _ensure_init() -> None:
    if not _INITED:
        init_db()


# ─────────────────────────── D1 稳定亏候选（同 SQL 口径） ───────────────────────────

def stable_losers(days: int = SHADOW_DIAG_DAYS) -> list[dict]:
    """(system, tf, ctx_regime) 稳定亏组合，SQL 与 dashboard D1 归因同口径。

    过滤：trades ≥ SHADOW_MIN_SAMPLES 且 win_rate < SHADOW_MAX_WINRATE 且
    net_pnl < 0；按 net_pnl 升序。只读 twelve_sim_trade（实际成交口径）。
    """
    _ensure_init()
    since = time.time() - float(days) * 86400.0
    fee_side_pct = jtt._fee_pct()
    out: list[dict] = []
    with _conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT system, tf, ctx_regime AS env, COUNT(*) AS trades,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(pnl) AS net_pnl,
                       SUM((entry_price + exit_price) * qty) AS notional
                FROM twelve_sim_trade
                WHERE exit_ts >= ? AND ctx_regime IS NOT NULL
                GROUP BY system, tf, ctx_regime
                """, (since,)).fetchall()
        except Exception:  # noqa: BLE001 — ctx_regime 列不存在（D0 未合入）
            return []
    for r in rows:
        r = dict(r)
        n = int(r["trades"] or 0)
        wr = (int(r["wins"] or 0) / n * 100.0) if n else 0.0
        net = float(r["net_pnl"] or 0.0)
        fee = float(r["notional"] or 0.0) * fee_side_pct / 100.0
        if n >= SHADOW_MIN_SAMPLES and wr < SHADOW_MAX_WINRATE and net < 0:
            out.append({
                "system": str(r["system"]), "tf": str(r["tf"]),
                "env": str(r["env"]), "trades": n,
                "win_rate_pct": round(wr, 2), "net_pnl": round(net, 4),
                "flip_hint": {"win_rate_pct": round(100.0 - wr, 2),
                              "net_pnl": round(-net - 2.0 * fee, 4)},
            })
    out.sort(key=lambda x: x["net_pnl"])
    return out


# ─────────────────────────── 环境匹配 ───────────────────────────

def _market_context(sym: str, tf: str, now: float) -> dict:
    """开仓时刻环境快照：转发 jtt._market_context（TTL 缓存 + 容错在其内部）。

    独立成模块函数便于冒烟打桩（shadow._market_context = lambda ...）。
    """
    try:
        return jtt._market_context(sym, tf, now) or {}
    except Exception:  # noqa: BLE001 — 环境取不到 → 空 dict → 本轮不开影子单
        return {}


# ─────────────────────────── 钱包 ───────────────────────────

def _wallet_for(conn, sym: str, tf: str, system: str) -> dict:
    row = conn.execute(
        "SELECT * FROM twelve_shadow_wallet WHERE symbol=? AND tf=? AND system=?",
        (sym, tf, system)).fetchone()
    if row:
        return dict(row)
    conn.execute(
        """
        INSERT INTO twelve_shadow_wallet
          (symbol, tf, system, name_cn, principal, balance, equity, updated_ts)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (sym, tf, system, jtt.NAME_CN.get(system, system),
         SHADOW_PRINCIPAL, SHADOW_PRINCIPAL, SHADOW_PRINCIPAL, time.time()))
    return {"symbol": sym, "tf": tf, "system": system,
            "principal": SHADOW_PRINCIPAL, "balance": SHADOW_PRINCIPAL}


# ─────────────────────────── 开仓 / 平仓 ───────────────────────────

def _open_shadow(conn, sym: str, tf: str, system: str, env: str,
                 orig_direction: str, price: float, plan: dict | None,
                 ctx: dict, now: float) -> dict | None:
    """按稳定亏候选开一笔反向影子单；参数不自洽/余额不足返回 None。"""
    # 原向参数合成（配置覆盖走空 eff：影子实验用 plan 推荐 + 默认，
    # 不吃 twelve_sim_config 的槽位覆盖，保持实验口径独立稳定）
    params = jtt._resolve_entry_params(orig_direction, price, {}, plan, tf)
    if not params:
        return None
    orig_sl, orig_tp = params.get("stop_loss"), params.get("take_profit")
    if orig_sl is None or orig_tp is None:
        return None   # 镜像互换需要两个点位都在，宁缺毋滥
    direction = "short" if orig_direction == "long" else "long"
    # SL/TP 镜像互换：影子 SL=原向 TP、影子 TP=原向 SL（方向取反后天然在正确侧）
    sl, tp = float(orig_tp), float(orig_sl)
    long_side = direction == "long"
    if not ((long_side and sl < price < tp) or (not long_side and tp < price < sl)):
        return None   # 现价已越过镜像点位（缺口/滞后信号），不硬开
    wal = _wallet_for(conn, sym, tf, system)
    balance = float(wal.get("balance") or 0.0)
    if balance < SHADOW_MIN_BALANCE:
        return None
    leverage = float(params["leverage"])
    pos_pct = float(params["position_pct"])
    margin = round(min(balance, balance * pos_pct / 100.0), 8)
    qty = round(margin * leverage / price, 8)
    flip_of = conn.execute(
        "SELECT id FROM twelve_sim_position WHERE symbol=? AND tf=? AND system=? "
        "AND status='open' AND direction=? ORDER BY id DESC",
        (sym, tf, system, orig_direction)).fetchone()
    cur = conn.execute(
        """
        INSERT INTO twelve_shadow_position
          (symbol, tf, system, direction, entry_price, entry_ts, qty, margin,
           leverage, position_pct, stop_loss, take_profit, cur_price,
           unrealized_pnl, status, src_system, src_env, flip_of_position_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,'open',?,?,?)
        """,
        (sym, tf, system, direction, price, now, qty, margin, leverage,
         pos_pct, sl, tp, price, system, env,
         int(flip_of["id"]) if flip_of else None))
    _log(f"影子开仓 {sym} {tf}×{system} {direction} @ {price} "
         f"(env={env} sl={sl} tp={tp} lev={leverage} margin={margin})")
    return {"position_id": cur.lastrowid, "symbol": sym, "tf": tf,
            "system": system, "direction": direction, "entry_price": price,
            "src_env": env}


def _close_shadow(conn, pos: dict, exit_price: float, reason: str,
                  now: float) -> dict:
    """影子平仓：净 PnL 口径对齐主台账 _do_close（双边费按名义、逐仓钳 -margin、
    爆仓亏光不另计费）。资金费暂不计提（TODO: 对齐 S7 口径后补，影子对照
    双侧同免时不影响相对结论）。"""
    entry = float(pos["entry_price"])
    qty = float(pos["qty"])
    margin = float(pos["margin"])
    sign = 1.0 if pos["direction"] == "long" else -1.0
    if reason == "liq":
        pnl = -margin
    else:
        gross = (exit_price - entry) * qty * sign
        fee = (entry * qty + exit_price * qty) * jtt._fee_pct() / 100.0
        pnl = max(gross - fee, -margin)
    pnl = round(pnl, 8)
    pnl_pct = round(pnl / margin * 100.0, 2) if margin > 0 else None
    sl, tp = pos.get("stop_loss"), pos.get("take_profit")
    rr = None
    if sl is not None and tp is not None:
        risk = abs(entry - float(sl))
        if risk > 0:
            rr = round(abs(float(tp) - entry) / risk, 2)
    holding_min = round((now - float(pos["entry_ts"])) / 60.0, 1)

    conn.execute("UPDATE twelve_shadow_position SET status='closed', "
                 "cur_price=?, unrealized_pnl=0 WHERE id=?",
                 (exit_price, pos["id"]))
    wal = _wallet_for(conn, pos["symbol"], pos["tf"], pos["system"])
    balance_after = round(float(wal["balance"]) + pnl, 8)
    ctx_vals = tuple(pos.get(f) for f in jtt.CTX_FIELDS)
    conn.execute(
        f"""
        INSERT INTO twelve_shadow_trade
          (symbol, tf, system, name_cn, direction, entry_price, entry_ts,
           exit_price, exit_ts, qty, margin, leverage, stop_loss, take_profit,
           exit_reason, pnl, pnl_pct, rr, balance_after, holding_minutes,
           funding_fee,
           {", ".join(jtt.CTX_FIELDS)}, context_tags, size_factor,
           src_system, src_env, flip_of_position_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                {",".join("?" * len(jtt.CTX_FIELDS))},?,?,?,?,?)
        """,
        (pos["symbol"], pos["tf"], pos["system"],
         jtt.NAME_CN.get(str(pos["system"]), pos["system"]), pos["direction"],
         entry, pos["entry_ts"], exit_price, now, qty, margin,
         pos.get("leverage") or 1.0, sl, tp, reason, pnl, pnl_pct, rr,
         balance_after, holding_min, 0.0,
         *ctx_vals, None, 1.0,
         pos.get("src_system"), pos.get("src_env"),
         pos.get("flip_of_position_id")))
    rows = conn.execute(
        "SELECT pnl FROM twelve_shadow_trade "
        "WHERE symbol=? AND tf=? AND system=? ORDER BY exit_ts, id",
        (pos["symbol"], pos["tf"], pos["system"])).fetchall()
    pnls = [float(r["pnl"]) for r in rows]
    wins = [x for x in pnls if x > 0]
    conn.execute(
        """
        UPDATE twelve_shadow_wallet
        SET balance=?, equity=?, total_trades=?, win_trades=?, total_pnl=?,
            win_rate=?, updated_ts=?
        WHERE symbol=? AND tf=? AND system=?
        """,
        (balance_after, balance_after, len(pnls), len(wins),
         round(sum(pnls), 8),
         round(100.0 * len(wins) / len(pnls), 1) if pnls else None,
         now, pos["symbol"], pos["tf"], pos["system"]))
    _log(f"影子平仓 {pos['symbol']} {pos['tf']}×{pos['system']} "
         f"{pos['direction']} {reason} @ {exit_price} pnl={pnl}")
    return {"position_id": pos["id"], "exit_reason": reason, "pnl": pnl,
            "balance_after": balance_after}


# ─────────────────────────── 主循环 ───────────────────────────

def _configured_symbols() -> list[str]:
    """默认币种池：主台账已配置槽位钱包的币（只读 twelve_sim_wallet）。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM twelve_sim_wallet").fetchall()
    return [str(r["symbol"]) for r in rows]


def run_cycle(symbols: list[str] | None = None,
              days: int = SHADOW_DIAG_DAYS) -> dict:
    """跑一轮影子验证：盯盘已开影子单 → 按稳定亏候选开新反向单。

    候选为空（样本不足 / 无稳定亏组合）→ 优雅空转（idle=True，不开任何单，
    已开持仓仍正常盯盘退出）。
    """
    _ensure_init()
    now = time.time()
    losers = stable_losers(days)
    combos = {(c["system"], c["tf"]): c for c in losers}
    syms = ([jtt._norm_symbol(s) for s in symbols] if symbols
            else _configured_symbols())
    opened, closed, skipped_env = [], [], 0

    for sym in syms:
        price = jtt.latest_price({}, sym)
        if not price or price <= 0:
            continue
        mark = jtt.mark_price_of({}, sym)
        with _conn() as conn:
            # 1) 盯盘：已开影子单 爆仓>止损>止盈>时间止损（复用主台账纯函数）
            open_rows = [dict(r) for r in conn.execute(
                "SELECT * FROM twelve_shadow_position "
                "WHERE symbol=? AND status='open'", (sym,)).fetchall()]
            for pos in open_rows:
                bars = jtt._fetch_bars(sym, str(pos["tf"]))
                hit = jtt._exit_check(pos, float(price), bars, mark)
                if hit:
                    closed.append(_close_shadow(conn, pos, hit[1], hit[0], now))
                    continue
                if jtt._timeout_due(pos, now):
                    closed.append(_close_shadow(conn, pos, float(price),
                                                "timeout", now))
                    continue
                upnl = (float(price) - float(pos["entry_price"])) \
                    * float(pos["qty"]) \
                    * (1.0 if pos["direction"] == "long" else -1.0)
                conn.execute(
                    "UPDATE twelve_shadow_position SET cur_price=?, "
                    "unrealized_pnl=? WHERE id=?",
                    (price, round(upnl, 8), pos["id"]))

            if not combos:
                continue   # 空转：无候选不开新单

            # 2) 开仓：命中稳定亏组合 + 信号方向明确 + 环境匹配 → 反向影子单
            signals = jtt.read_signals(sym)
            for (system, tf), combo in combos.items():
                sig = signals.get((tf, system))
                if not sig:
                    continue
                d = str(sig.get("direction") or "neutral")
                if d not in ("bullish", "bearish"):
                    continue
                orig_direction = "long" if d == "bullish" else "short"
                shadow_dir = "short" if orig_direction == "long" else "long"
                dup = conn.execute(
                    "SELECT id FROM twelve_shadow_position WHERE symbol=? "
                    "AND tf=? AND system=? AND status='open' AND direction=?",
                    (sym, tf, system, shadow_dir)).fetchone()
                if dup:
                    continue   # 同槽位同向影子单已在途，不重复建仓
                ctx = _market_context(sym, tf, now)
                if ctx.get("ctx_regime") != combo["env"]:
                    skipped_env += 1
                    continue   # 环境错配/取不到 → 不开（样本纯度优先）
                res = _open_shadow(conn, sym, tf, system, str(combo["env"]),
                                   orig_direction, float(price),
                                   sig.get("plan"), ctx, now)
                if res:
                    opened.append(res)

    out = {"ok": True, "ts": now, "candidates": len(losers),
           "symbols": syms, "opened": len(opened), "closed": len(closed),
           "skipped_env_mismatch": skipped_env, "idle": not losers}
    if not losers:
        _log(f"空转：{days} 天窗口内无样本充足的稳定亏候选"
             f"（min_samples={SHADOW_MIN_SAMPLES}, "
             f"max_winrate={SHADOW_MAX_WINRATE}）")
    else:
        _log(f"cycle 完成：候选 {len(losers)} 组，开 {len(opened)} 平 {len(closed)} "
             f"环境错配跳过 {skipped_env}")
    return out


# ─────────────────────────── 对照报表 ───────────────────────────

def report(days: int = 30) -> dict:
    """原向 vs 反向 对照：按 (system, tf, env) 聚合净利/胜率/费用。

    结论行仅在影子样本 ≥ SHADOW_MIN_SAMPLES 时给出（forward_validated 字段）；
    样本不足标 insufficient_samples，只陈列不下结论。
    """
    _ensure_init()
    since = time.time() - float(days) * 86400.0
    fee_pct = jtt._fee_pct()

    def _agg(conn, table: str, env_col: str) -> dict:
        rows = conn.execute(
            f"""
            SELECT system, tf, {env_col} AS env, COUNT(*) AS trades,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(pnl) AS net_pnl,
                   SUM((entry_price + exit_price) * qty) AS notional
            FROM {table}
            WHERE exit_ts >= ? AND {env_col} IS NOT NULL
            GROUP BY system, tf, {env_col}
            """, (since,)).fetchall()
        out = {}
        for r in rows:
            r = dict(r)
            n = int(r["trades"] or 0)
            out[(str(r["system"]), str(r["tf"]), str(r["env"]))] = {
                "trades": n,
                "win_rate_pct": round(int(r["wins"] or 0) / n * 100.0, 2) if n else None,
                "net_pnl": round(float(r["net_pnl"] or 0.0), 4),
                "fee": round(float(r["notional"] or 0.0) * fee_pct / 100.0, 4),
            }
        return out

    with _conn() as conn:
        shadow = _agg(conn, "twelve_shadow_trade", "src_env")
        try:
            orig = _agg(conn, "twelve_sim_trade", "ctx_regime")
        except Exception:  # noqa: BLE001 — ctx 列未上线
            orig = {}

    rows = []
    for key, sh in sorted(shadow.items()):
        system, tf, env = key
        o = orig.get(key)
        enough = sh["trades"] >= SHADOW_MIN_SAMPLES
        row = {"system": system, "tf": tf, "env": env,
               "original": o, "shadow": sh,
               "forward_validated": (bool(o) and enough
                                     and sh["net_pnl"] > 0
                                     and o["net_pnl"] < 0) if enough else None,
               "note": None if enough else
               f"insufficient_samples（影子 {sh['trades']} < {SHADOW_MIN_SAMPLES}）"}
        rows.append(row)
    return {"ok": True, "days": days, "generated_at": time.time(),
            "min_samples": SHADOW_MIN_SAMPLES, "combos": rows,
            "note": ("forward_validated=True 仅代表窗口内影子净利为正且原向为负，"
                     "未含滑点；上实盘前仍需独立风控评审")}


def status() -> dict:
    """影子钱包与在途持仓概览。"""
    _ensure_init()
    with _conn() as conn:
        wallets = [dict(r) for r in conn.execute(
            "SELECT * FROM twelve_shadow_wallet ORDER BY symbol, tf, system"
        ).fetchall()]
        open_pos = [dict(r) for r in conn.execute(
            "SELECT * FROM twelve_shadow_position WHERE status='open' "
            "ORDER BY symbol, tf, system").fetchall()]
    return {"ok": True, "wallets": wallets, "open_positions": open_pos,
            "candidates": stable_losers()}


# ─────────────────────────── CLI ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="13诊断 D7：反向信号影子验证")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cycle = sub.add_parser("cycle", help="跑一轮影子验证")
    p_cycle.add_argument("--symbols", help="逗号分隔币种（默认读 sim 已配置币种）")
    p_cycle.add_argument("--days", type=int, default=SHADOW_DIAG_DAYS,
                         help="stable_losers 回看窗口（天）")

    p_run = sub.add_parser("run", help="常驻循环")
    p_run.add_argument("--interval-min", type=float, default=5.0)
    p_run.add_argument("--symbols", help="逗号分隔币种")
    p_run.add_argument("--days", type=int, default=SHADOW_DIAG_DAYS)

    p_rep = sub.add_parser("report", help="原向 vs 反向对照报表")
    p_rep.add_argument("--days", type=int, default=30)

    sub.add_parser("status", help="影子钱包/持仓概览")
    sub.add_parser("init", help="仅建表")

    args = ap.parse_args()
    if args.cmd == "init":
        init_db()
        print("twelve_shadow_* 三表就绪")
        return 0
    if args.cmd == "cycle":
        syms = [s for s in (args.symbols or "").split(",") if s.strip()] or None
        print(json.dumps(run_cycle(syms, args.days), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "run":
        syms = [s for s in (args.symbols or "").split(",") if s.strip()] or None
        _log(f"常驻启动 interval={args.interval_min}min symbols={syms or '自动'}")
        while True:
            try:
                run_cycle(syms, args.days)
            except Exception as exc:  # noqa: BLE001 — 单轮异常不退出常驻
                _log(f"⚠️ cycle 异常: {exc!r}"[:200])
            time.sleep(max(0.5, args.interval_min * 60.0))
    if args.cmd == "report":
        print(json.dumps(report(args.days), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
