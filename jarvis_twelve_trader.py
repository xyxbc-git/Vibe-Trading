#!/usr/bin/env python3
"""贾维斯 JARVIS — 十二系统 × 六时间轴 槽位级模拟交易引擎（Vibe-Trading 台账）。

概念：配置币种（如 ETHUSDT）后，每个 tf×system 槽位（6×12=72）开一个独立虚拟
钱包（默认本金 100U），按 `twelve_signal_state` 的当前态信号自动开平仓记台账，
用于统计「哪个信号系统 / 哪个时间轴」胜率最高。

边界（硬约束）：
  - **只读** twelve_signal_state：绝不触发 12 信号重算、绝不改 jarvis_twelve_systems；
  - paper-only：不碰真钱、不打真实交易所，现价复用 jarvis_paper_trader.latest_price
    同款通道（Agent Gateway /price，失败回退 brief 因子价）；
  - 独立进程运行（CLI loop / daemon --twelve-sim），绝不挂进 dashboard 请求路径。

交易规则（每轮 run_cycle，平仓判定优先级 爆仓 > 止损 > 止盈 > 时间止损；
2026-08-06 R3 重构：点位触发才成交 + 信号变更失效留痕 + 已成交仓位独立）：
  1. 盯盘：持仓槽位先用「自开仓以来已收盘 bar 的 high/low」判断 爆仓(liq)/
     止损(sl)/止盈(tp) 盘中触碰（影线也算；同 bar 双触按保守取 SL；结算价=触发位；
     K 线取不到时优雅回退快照现价比对，缺口按更差价结算）；
  2. 已成交仓位独立（R3 规则3）：持仓后同槽位信号再变化（反向/转中性/点位更新）
     **绝不影响已成交仓位**——不 flip 平仓、不跟随更新 SL/TP，仅记 applied=0
     留痕日志；持仓只按自身 liq/sl/tp/timeout 生命周期退出。反向信号作为
     **新的独立计划**评估（同槽位可并存 1 多 + 1 空 + 1 pending）；
     同方向信号视为同一观点延续，不重复建仓；
  3. 时间止损：持仓超过 TF 分档上限（5m/15m:1天 30m:2天 1h:3天 4h:7天 1d:14天）
     → 以现价平仓(exit_reason=timeout)；
  4. 开仓（R3 规则1：点位触发才算成交）：信号 bullish 开多 / bearish 开空；
     neutral 不动；只要 plan 给出有效 entry 点位就必须比对实时价格：
     - entry_type=breakout/pullback：沿用方向性触达口径（见 4.5）；
     - entry_type=market（或缺 entry_type）且带有效 entry：按「点位或更优」限价
       口径（做多 现价≤点位 / 做空 现价≥点位）；已处于可成交侧 → 按现价立即
       成交（现有业务口径）；未触达 → 挂计划(pending) 等触达；
     - 计划缺失 / entry 非法 → 无点位可比，维持现状按现价立即开仓；
     - pending 不建持仓、不动钱包、不进胜率、不写 twelve_sim_trade；
  4.5 计划盯盘（每轮，先于持仓开仓处理）：
     - 触达判定用「自计划创建以来已收盘 bar 的 high/low ∪ 快照现价」（同 _exit_check
       口径）：breakout 多 high≥entry / 空 low≤entry；pullback 多 low≤entry / 空 high≥entry；
       market 按限价口径（多 low≤entry / 空 high≥entry，成交价取点位与现价更优侧）；
     - 触达即「成交」：以 **plan['entry'] 价**转持仓（entry_price=entry、entry_ts=成交
       时刻），SL/TP/杠杆/qty 经 _resolve_entry_params 基于 entry 价合成；
       成交落 twelve_sim_signal_log 留痕（change_kinds=fill）；
     - 未成交期间信号 entry/SL/TP 实质变化（≥0.2%）→ 更新计划点位并记
       twelve_sim_signal_log（position_id 关联 pending 行）；
     - R3 规则2：pending 期间同级别信号变更（反向/转 neutral/计划消失/槽位停用/
       挂单超 TF 超时档）→ **旧计划失效**：行保留 status='canceled' + cancel_reason
       + canceled_ts 留痕（看板可见「已失效」），并落 twelve_sim_signal_log
       （change_kinds=cancel）；失效后价格再触达旧点位也不成交，一切以最新信号
       为准（撤销判定先于触达成交判定）；canceled 行保留 7 天后清理
       （日志表留痕永久）；全程不产生 twelve_sim_trade；
  5. 参数：止损/止盈/杠杆/仓位% 优先用 twelve_sim_config 覆盖
     （优先级 信号级 > tf组级 > 币种级），否则用 plan_json 系统推荐；杠杆双兜底：
     配置/plan 均未给时按止损距离自动推荐（打到止损亏≈保证金50%，夹 [1,20]）；
  6. 逐仓口径：margin = balance × position_pct%，qty = margin × leverage / entry；
     爆仓价 = entry × (1 ∓ 1/leverage)，触发即以爆仓价强平 pnl=-margin；
     开/平双边手续费按名义单边 0.05%（jarvis_config: twelve_sim_fee_pct 可配）
     折进净 pnl；单笔最大亏损钳到 -margin（不倒欠）。

五张本地表（经 jarvis_db 兼容层懒建，pg 可切）：
  twelve_sim_wallet     槽位虚拟钱包 + 累计战绩（UNIQUE symbol,tf,system）
  twelve_sim_position   计划/在途持仓（status pending计划未成交 / open持仓 /
                        closed已平 / canceled已失效；pending 行 entry_price=计划
                        入场价、entry_ts=计划创建时刻、qty/margin=0 延迟到成交时
                        按 entry 价计算回填；canceled 行带 cancel_reason/canceled_ts）
  twelve_sim_trade      平仓台账流水（exit_reason tp/sl/flip(历史)/timeout/liq；
                        只有「成交→平仓」才写，计划的建/撤/改不落此表）
  twelve_sim_signal_log 信号变更留痕日志（计划建/撤/成交、pending 点位跟随、
                        持仓期间信号漂移 applied=0 留痕；关联 position_id）
  twelve_sim_config     参数配置（scope_tf/scope_system 可 NULL 分层覆盖；
                        内容由 RuoYi 同步链路回读落地，本模块只负责
                        建表 + 读取 + 提供 upsert_config 函数）

用法：
  python jarvis_twelve_trader.py config-set ETHUSDT                # 启用币种（币种级配置）
  python jarvis_twelve_trader.py config-set ETHUSDT --tf 1h --leverage 3
  python jarvis_twelve_trader.py config-list
  python jarvis_twelve_trader.py cycle                             # 跑一轮（读配置币种）
  python jarvis_twelve_trader.py cycle --symbols ETH,BTC           # 跑一轮（显式币种）
  python jarvis_twelve_trader.py run --interval-min 5              # 常驻循环（独立进程）
  python jarvis_twelve_trader.py status --symbol ETHUSDT           # 槽位战绩榜
"""

from __future__ import annotations

import argparse
import json
import os
import time

import jarvis_db as jdb

DB_DIR = os.path.expanduser("~/.vibe-trading")
DB_PATH = os.path.join(DB_DIR, "jarvis_journal.db")
LOG_PATH = os.path.join(DB_DIR, "jarvis_twelve_trader.log")

TFS = ("5m", "15m", "30m", "1h", "4h", "1d")
SYSTEMS = ("turtle", "dow", "elliott", "volatility", "gann", "chanlun",
           "rule123", "gap", "martingale", "oscillator", "triple_rsi", "arbitrage")
NAME_CN = {
    "turtle": "海龟交易", "dow": "道氏理论", "elliott": "艾略特波浪",
    "volatility": "波动率系统", "gann": "江恩时间窗", "chanlun": "缠论",
    "rule123": "123法则", "gap": "跳空缺口", "martingale": "马丁格尔",
    "oscillator": "摆动震荡", "triple_rsi": "三重平滑RSI", "arbitrage": "套利系统",
}

DEFAULT_PRINCIPAL = 100.0     # 槽位默认本金（U）
DEFAULT_POSITION_PCT = 10.0   # 无配置且计划未给时的默认仓位%（占槽位余额）
MIN_BALANCE = 1.0             # 余额低于此视为爆仓槽位，不再开新仓
DEFAULT_FEE_PCT = 0.05        # 单边手续费%（按名义；jarvis_config: twelve_sim_fee_pct 可覆盖）

# 各 TF 时间止损（天），口径对齐 jarvis_paper_trader._TWELVE_TIME_STOP
TF_TIME_STOP_DAYS = {"5m": 1, "15m": 1, "30m": 2, "1h": 3, "4h": 7, "1d": 14}

# 自动杠杆推荐：打到止损时目标亏损占保证金比例，与杠杆上限
AUTO_LEV_SL_LOSS_FRAC = 0.5
MAX_AUTO_LEVERAGE = 20.0

# 点位跟随：SL/TP 相对变化 ≥ 此阈值(%)才算实质变更（对齐 jarvis_signal_history
# 计划价 0.2% 变更判定，滤掉浮点噪音与微调抖动）
SLTP_MIN_CHANGE_PCT = 0.2

# 已失效(canceled)计划行保留天数：看板留痕窗口；到期物理清理防表膨胀
# （twelve_sim_signal_log 的 cancel 留痕永久保留，完整审计链在日志表）
CANCELED_RETENTION_DAYS = 7

# 计划失效原因 → 中文留痕说明（写进 twelve_sim_signal_log.note，看板直读）
CANCEL_REASON_CN = {
    "neutral": "信号转中性", "flip": "信号反向", "plan_gone": "信号计划消失",
    "disabled": "槽位停用", "timeout": "挂单超时", "broke": "槽位余额不足",
    "incoherent": "点位与配置不自洽",
}

_INITED = False


def _log(msg: str) -> None:
    os.makedirs(DB_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — 日志失败不拖垮交易循环
        pass


def _conn():
    os.makedirs(DB_DIR, exist_ok=True)
    return jdb.connect(DB_PATH)


def init_db() -> None:
    global _INITED
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_wallet (
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
                profit_factor    REAL,
                max_drawdown_pct REAL,
                updated_ts       REAL,
                UNIQUE (symbol, tf, system)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_position (
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
                status         TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tsp_sym_status "
            "ON twelve_sim_position(symbol, status)"
        )
        # 旧库升级：R3 失效留痕列（SQLite 无 IF NOT EXISTS，重复加列抛错=已升级过；
        # jarvis_db 兼容层对 pg 自动翻译为 ADD COLUMN IF NOT EXISTS 幂等）
        for _ddl in ("ALTER TABLE twelve_sim_position ADD COLUMN cancel_reason TEXT",
                     "ALTER TABLE twelve_sim_position ADD COLUMN canceled_ts REAL"):
            try:
                conn.execute(_ddl)
            except Exception:  # noqa: BLE001 — duplicate column = 已升级过
                pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_trade (
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
                holding_minutes REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tst_slot "
            "ON twelve_sim_trade(symbol, tf, system, exit_ts)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_config (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                scope_tf        TEXT,
                scope_system    TEXT,
                principal       REAL DEFAULT 100,
                leverage        REAL,
                position_pct    REAL,
                stop_loss_pct   REAL,
                take_profit_pct REAL,
                enabled         INTEGER NOT NULL DEFAULT 1,
                UNIQUE (symbol, scope_tf, scope_system)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twelve_sim_signal_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                symbol       TEXT NOT NULL,
                tf           TEXT NOT NULL,
                system       TEXT NOT NULL,
                name_cn      TEXT,
                position_id  INTEGER,
                prev_entry   REAL,
                prev_sl      REAL,
                prev_tp      REAL,
                new_entry    REAL,
                new_sl       REAL,
                new_tp       REAL,
                price        REAL,
                change_kinds TEXT,
                applied      INTEGER NOT NULL DEFAULT 1,
                note         TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tssl_slot "
            "ON twelve_sim_signal_log(symbol, tf, system, ts)"
        )
    _INITED = True


def _ensure_init() -> None:
    if not _INITED:
        init_db()


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return s if s.endswith("USDT") else s + "USDT"


# ─────────────────────────── 现价通道 ───────────────────────────

def latest_price(cfg: dict, symbol: str) -> float | None:
    """现价：复用 jarvis_paper_trader.latest_price 同款通道（惰性导入）。

    冒烟测试直接对本函数打桩（jtt.latest_price = lambda ...），不触发导入。
    """
    try:
        import jarvis_paper_trader as jpt
        return jpt.latest_price(cfg, symbol)
    except Exception as exc:  # noqa: BLE001 — 取价失败降级为无价，本轮跳过该币
        _log(f"⚠️ {symbol} 取现价失败: {exc!r}"[:160])
        return None


def mark_price_of(cfg: dict, symbol: str) -> float | None:
    """标记价（爆仓判定口径，币安合约强平按 markPrice 触发）：惰性导入 jcd。

    冒烟测试直接对本函数打桩（jtt.mark_price_of = lambda ...）；返回 None 时
    爆仓判定回退最新成交价（与旧行为一致）。
    """
    try:
        import jarvis_crypto_data as jcd
        return jcd.fetch_mark_price(symbol)
    except Exception as exc:  # noqa: BLE001 — 标记价缺失不阻塞盯盘，回退成交价
        _log(f"⚠️ {symbol} 取标记价失败: {exc!r}"[:160])
        return None


def _load_cfg() -> dict:
    """执行手配置（gateway_base/agent_token 等，供取价用）；失败回空 dict。"""
    try:
        import jarvis_executor as jx
        return jx.load_config()
    except Exception:  # noqa: BLE001
        return {}


def _fee_pct() -> float:
    """单边手续费%（按名义）：jarvis_config twelve_sim_fee_pct > 内置默认 0.05。"""
    try:
        import jarvis_config as jc
        v = jc.get("twelve_sim_fee_pct")
        return max(0.0, float(v)) if v is not None else DEFAULT_FEE_PCT
    except Exception:  # noqa: BLE001 — 配置层异常回退默认，不拖垮交易循环
        return DEFAULT_FEE_PCT


def _fetch_bars(symbol: str, tf: str) -> list[dict] | None:
    """取该 TF 最近的已收盘 K 线（[{time(ms), high, low}]），供影线盘中触发判定。

    只拉数据不算信号（不触发 12 信号重算）；失败返回 None → 调用方
    优雅回退快照现价比对。冒烟测试直接对本函数打桩。
    """
    try:
        import jarvis_twelve_systems as jts
        df = jts.fetch_klines_df(symbol, tf, 300, drop_unclosed=True)
        if df is None or len(df) == 0:
            return None
        return df[["time", "high", "low"]].to_dict("records")
    except Exception as exc:  # noqa: BLE001 — 取数失败降级为快照比对
        _log(f"⚠️ {symbol} [{tf}] 取K线失败（回退现价比对）: {exc!r}"[:160])
        return None


# ─────────────────────────── 配置：upsert / 读取 / 合并 ───────────────────────────

_CFG_FIELDS = ("principal", "leverage", "position_pct",
               "stop_loss_pct", "take_profit_pct", "enabled")


def upsert_config(symbol: str, scope_tf: str | None = None,
                  scope_system: str | None = None, *,
                  principal: float | None = None,
                  leverage: float | None = None,
                  position_pct: float | None = None,
                  stop_loss_pct: float | None = None,
                  take_profit_pct: float | None = None,
                  enabled: bool | int | None = None) -> dict:
    """写入/更新一条配置（供 RuoYi 回读链路与 CLI 调用）。

    scope_tf/scope_system 为 NULL 时 UNIQUE 约束不生效（SQL NULL 语义），
    故此处显式先查后改，保证同 scope 只有一行。只更新显式传入的字段。
    """
    _ensure_init()
    sym = _norm_symbol(symbol)
    stf = scope_tf or None
    ssys = scope_system or None
    if stf is not None and stf not in TFS:
        return {"ok": False, "error": f"scope_tf 非法: {stf}（可选 {'/'.join(TFS)}）"}
    if ssys is not None and ssys not in SYSTEMS:
        return {"ok": False, "error": f"scope_system 非法: {ssys}"}
    fields = {"principal": principal, "leverage": leverage,
              "position_pct": position_pct, "stop_loss_pct": stop_loss_pct,
              "take_profit_pct": take_profit_pct,
              "enabled": (None if enabled is None else int(bool(enabled)))}
    with _conn() as conn:
        cond = ["symbol=?"]
        args: list = [sym]
        for col, val in (("scope_tf", stf), ("scope_system", ssys)):
            if val is None:
                cond.append(f"{col} IS NULL")
            else:
                cond.append(f"{col}=?")
                args.append(val)
        row = conn.execute(
            "SELECT id FROM twelve_sim_config WHERE " + " AND ".join(cond),
            args).fetchone()
        if row:
            sets = {k: v for k, v in fields.items() if v is not None}
            if sets:
                conn.execute(
                    "UPDATE twelve_sim_config SET "
                    + ", ".join(f"{k}=?" for k in sets)
                    + " WHERE id=?", (*sets.values(), row["id"]))
            return {"ok": True, "id": row["id"], "updated": sorted(sets)}
        cur = conn.execute(
            """
            INSERT INTO twelve_sim_config
              (symbol, scope_tf, scope_system, principal, leverage,
               position_pct, stop_loss_pct, take_profit_pct, enabled)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (sym, stf, ssys,
             fields["principal"] if fields["principal"] is not None else DEFAULT_PRINCIPAL,
             fields["leverage"], fields["position_pct"],
             fields["stop_loss_pct"], fields["take_profit_pct"],
             fields["enabled"] if fields["enabled"] is not None else 1))
        return {"ok": True, "id": cur.lastrowid, "created": True}


def list_configs(symbol: str | None = None) -> list[dict]:
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_config WHERE symbol=? "
                "ORDER BY scope_tf, scope_system", (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_config ORDER BY symbol, scope_tf, scope_system")
        return [dict(r) for r in cur.fetchall()]


def configured_symbols() -> list[str]:
    """已启用的配置币种（币种级行 scope 全 NULL 且 enabled=1）。"""
    _ensure_init()
    with _conn() as conn:
        cur = conn.execute(
            "SELECT DISTINCT symbol FROM twelve_sim_config "
            "WHERE scope_tf IS NULL AND scope_system IS NULL AND enabled=1 "
            "ORDER BY symbol")
        return [str(r["symbol"]) for r in cur.fetchall()]


def effective_config(symbol: str, tf: str, system: str,
                     rows: list[dict] | None = None) -> dict:
    """槽位生效配置：逐字段按 信号级 > tf组级 > 币种级 取第一个非 NULL。

    返回 {principal, leverage, position_pct, stop_loss_pct, take_profit_pct, enabled}；
    leverage/position_pct/sl/tp 可能为 None（= 交给 plan_json / 默认值兜底）。
    """
    sym = _norm_symbol(symbol)
    if rows is None:
        rows = [r for r in list_configs(sym)]
    levels: list[dict | None] = [None, None, None]
    for r in rows:
        stf, ssys = r.get("scope_tf") or None, r.get("scope_system") or None
        if stf == tf and ssys == system:
            levels[0] = r
        elif stf == tf and ssys is None:
            levels[1] = r
        elif stf is None and ssys is None:
            levels[2] = r
    out: dict = {"principal": DEFAULT_PRINCIPAL, "leverage": None,
                 "position_pct": None, "stop_loss_pct": None,
                 "take_profit_pct": None, "enabled": True}
    for field in _CFG_FIELDS:
        for lv in levels:
            if lv is not None and lv.get(field) is not None:
                out[field] = (bool(lv[field]) if field == "enabled"
                              else float(lv[field]))
                break
    return out


# ─────────────────────────── 钱包 / 持仓 / 台账 读取 ───────────────────────────

def ensure_wallets(symbol: str, cfg_rows: list[dict] | None = None) -> int:
    """给该币种补齐 72 个槽位钱包（缺哪个建哪个，幂等）；返回新建数量。"""
    _ensure_init()
    sym = _norm_symbol(symbol)
    rows = cfg_rows if cfg_rows is not None else list_configs(sym)
    created = 0
    now = time.time()
    with _conn() as conn:
        cur = conn.execute(
            "SELECT tf, system FROM twelve_sim_wallet WHERE symbol=?", (sym,))
        have = {(r["tf"], r["system"]) for r in cur.fetchall()}
        for tf in TFS:
            for system in SYSTEMS:
                if (tf, system) in have:
                    continue
                principal = float(effective_config(sym, tf, system, rows)["principal"])
                conn.execute(
                    """
                    INSERT INTO twelve_sim_wallet
                      (symbol, tf, system, name_cn, principal, balance, equity,
                       total_trades, win_trades, total_pnl, updated_ts)
                    VALUES (?,?,?,?,?,?,?,0,0,0,?)
                    """,
                    (sym, tf, system, NAME_CN.get(system, system),
                     principal, principal, principal, now))
                created += 1
    return created


def get_wallets(symbol: str | None = None) -> list[dict]:
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_wallet WHERE symbol=? ORDER BY tf, system",
                (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_wallet ORDER BY symbol, tf, system")
        return [dict(r) for r in cur.fetchall()]


def open_positions(symbol: str | None = None) -> list[dict]:
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='open' AND symbol=? "
                "ORDER BY tf, system", (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='open' "
                "ORDER BY symbol, tf, system")
        return [dict(r) for r in cur.fetchall()]


def pending_positions(symbol: str | None = None) -> list[dict]:
    """计划(pending)行查询：已挂未成交的入场计划（CLI / 冒烟 / 调试用）。"""
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='pending' AND symbol=? "
                "ORDER BY tf, system", (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='pending' "
                "ORDER BY symbol, tf, system")
        return [dict(r) for r in cur.fetchall()]


def canceled_positions(symbol: str | None = None) -> list[dict]:
    """已失效(canceled)计划行查询：R3 规则2 留痕（保留 7 天，看板/冒烟/调试用）。"""
    _ensure_init()
    with _conn() as conn:
        if symbol:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='canceled' AND symbol=? "
                "ORDER BY canceled_ts DESC, id DESC", (_norm_symbol(symbol),))
        else:
            cur = conn.execute(
                "SELECT * FROM twelve_sim_position WHERE status='canceled' "
                "ORDER BY canceled_ts DESC, id DESC")
        return [dict(r) for r in cur.fetchall()]


def trades(symbol: str | None = None, tf: str | None = None,
           system: str | None = None, limit: int = 200) -> list[dict]:
    _ensure_init()
    cond, args = [], []
    if symbol:
        cond.append("symbol=?")
        args.append(_norm_symbol(symbol))
    if tf:
        cond.append("tf=?")
        args.append(tf)
    if system:
        cond.append("system=?")
        args.append(system)
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    with _conn() as conn:
        cur = conn.execute(
            f"SELECT * FROM twelve_sim_trade{where} "
            f"ORDER BY exit_ts DESC, id DESC LIMIT ?", (*args, max(1, int(limit))))
        return [dict(r) for r in cur.fetchall()]


# ─────────────────────────── 信号读取（只读 twelve_signal_state） ───────────────────────────

def read_signals(symbol: str) -> dict[tuple[str, str], dict]:
    """读该币全部槽位当前态信号 → {(tf, system): {direction, strength, plan}}。

    只 SELECT，绝不触发重算；plan_json 解析失败按无计划处理。
    """
    _ensure_init()
    sym = _norm_symbol(symbol)
    out: dict[tuple[str, str], dict] = {}
    with _conn() as conn:
        cur = conn.execute(
            "SELECT tf, system, name_cn, direction, strength, plan_json, updated_ts "
            "FROM twelve_signal_state WHERE symbol=?", (sym,))
        for r in cur.fetchall():
            try:
                plan = json.loads(r["plan_json"] or "null")
            except (TypeError, ValueError):
                plan = None
            out[(str(r["tf"]), str(r["system"]))] = {
                "direction": str(r["direction"] or "neutral"),
                "strength": float(r["strength"] or 0.0),
                "name_cn": r["name_cn"],
                "plan": plan if isinstance(plan, dict) else None,
                "updated_ts": r["updated_ts"],
            }
    return out


# ─────────────────────────── 开仓参数解析 ───────────────────────────

def _auto_leverage(entry: float, stop_loss: float) -> float:
    """按止损距离反推推荐杠杆：打到止损亏损 ≈ 保证金 50%，夹到 [1, 20]。

    （移植自原 jarvis_sim_trader.recommend_leverage，仅在配置/plan 均无杠杆时用。）
    """
    import math
    try:
        dist = abs(float(entry) - float(stop_loss)) / float(entry)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    if not math.isfinite(dist) or dist <= 0:
        return 1.0
    return float(max(1.0, min(MAX_AUTO_LEVERAGE, math.floor(AUTO_LEV_SL_LOSS_FRAC / dist))))


def _resolve_entry_params(direction: str, price: float, eff: dict,
                          plan: dict | None) -> dict | None:
    """合成一笔开仓参数：配置覆盖 > plan_json 推荐 > 默认/自动推荐。

    杠杆兜底顺序：配置 > plan_json > 按止损距离自动推荐（夹 [1,20]）。
    返回 {stop_loss, take_profit, leverage, position_pct} 或 None（点位不自洽，
    如现价已越过计划止损/止盈 → 宁缺毋滥不硬开）。
    """
    plan = plan or {}
    pos_pct = eff.get("position_pct")
    if pos_pct is None:
        pos_pct = plan.get("position_pct") or DEFAULT_POSITION_PCT
    pos_pct = max(0.1, min(100.0, float(pos_pct)))

    long_side = direction == "long"
    sl_pct, tp_pct = eff.get("stop_loss_pct"), eff.get("take_profit_pct")
    sl = (price * (1 - sl_pct / 100.0) if long_side else price * (1 + sl_pct / 100.0)) \
        if sl_pct else None
    tp = (price * (1 + tp_pct / 100.0) if long_side else price * (1 - tp_pct / 100.0)) \
        if tp_pct else None
    if sl is None:
        sl = plan.get("stop_loss")
    if tp is None:
        tp = plan.get("take_profit") or plan.get("take_profit_1")
    try:
        sl = float(sl) if sl is not None else None
        tp = float(tp) if tp is not None else None
    except (TypeError, ValueError):
        return None
    if sl is None or tp is None:
        return None   # 无止损/止盈点位不开仓（宁缺毋滥）
    # 方向自洽校验：多单 SL < 现价 < TP；空单 TP < 现价 < SL（行情跑掉不硬开）
    if long_side and not (sl < price < tp):
        return None
    if not long_side and not (tp < price < sl):
        return None

    lev = eff.get("leverage")
    if lev is None:
        lev = plan.get("leverage")
    lev = max(1.0, float(lev)) if lev else _auto_leverage(price, sl)
    return {"stop_loss": sl, "take_profit": tp,
            "leverage": lev, "position_pct": pos_pct}


def _max_drawdown_pct(principal: float, balance_series: list[float]) -> float | None:
    """由 本金 + 逐笔 balance_after 序列 算最大回撤%（峰谷口径）。"""
    peak = principal
    mdd = 0.0
    for b in balance_series:
        peak = max(peak, b)
        if peak > 0:
            mdd = max(mdd, (peak - b) / peak * 100.0)
    return round(mdd, 2)


# ─────────────────────────── 开仓 / 平仓（内部，同一连接内执行） ───────────────────────────

def _do_open(conn, sym: str, tf: str, system: str,
             direction: str, price: float, balance: float,
             params: dict, now: float) -> dict:
    margin = round(min(balance, balance * params["position_pct"] / 100.0), 8)
    qty = round(margin * params["leverage"] / price, 8)
    conn.execute(
        """
        INSERT INTO twelve_sim_position
          (symbol, tf, system, direction, entry_price, entry_ts, qty, margin,
           leverage, position_pct, stop_loss, take_profit, cur_price,
           unrealized_pnl, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,'open')
        """,
        (sym, tf, system, direction, price, now, qty, margin,
         params["leverage"], params["position_pct"],
         params["stop_loss"], params["take_profit"], price))
    return {"symbol": sym, "tf": tf, "system": system, "direction": direction,
            "entry_price": price, "qty": qty, "margin": margin,
            "leverage": params["leverage"], "stop_loss": params["stop_loss"],
            "take_profit": params["take_profit"]}


def _do_close(conn, pos: dict, exit_price: float, reason: str, now: float) -> dict:
    """平仓：净 PnL = 毛盈亏 − 开/平双边手续费（按名义，出场名义=qty×出场价，
    多空同式无方向 bug）；爆仓(liq) 直接亏光保证金、不再另计费；
    逐仓亏损钳到 -margin → 记台账 → 更新钱包战绩。"""
    sym, tf, system = pos["symbol"], pos["tf"], pos["system"]
    entry = float(pos["entry_price"])
    qty = float(pos["qty"])
    margin = float(pos["margin"])
    sign = 1.0 if pos["direction"] == "long" else -1.0
    if reason == "liq":
        pnl = -margin   # 爆仓：损失以保证金为上限，手续费不再另计
    else:
        gross = (exit_price - entry) * qty * sign
        fee = (entry * qty + exit_price * qty) * _fee_pct() / 100.0
        pnl = max(gross - fee, -margin)   # 逐仓：最大亏损=保证金，不倒欠
    pnl = round(pnl, 8)
    pnl_pct = round(pnl / margin * 100.0, 2) if margin > 0 else None
    sl, tp = pos.get("stop_loss"), pos.get("take_profit")
    rr = None
    if sl is not None and tp is not None:
        risk = abs(entry - float(sl))
        if risk > 0:
            rr = round(abs(float(tp) - entry) / risk, 2)
    holding_min = round((now - float(pos["entry_ts"])) / 60.0, 1)

    conn.execute("UPDATE twelve_sim_position SET status='closed', cur_price=?, "
                 "unrealized_pnl=0 WHERE id=?", (exit_price, pos["id"]))

    wal = conn.execute(
        "SELECT * FROM twelve_sim_wallet WHERE symbol=? AND tf=? AND system=?",
        (sym, tf, system)).fetchone()
    wal = dict(wal) if wal else {"principal": DEFAULT_PRINCIPAL,
                                 "balance": DEFAULT_PRINCIPAL}
    balance_after = round(float(wal["balance"]) + pnl, 8)

    conn.execute(
        """
        INSERT INTO twelve_sim_trade
          (symbol, tf, system, name_cn, direction, entry_price, entry_ts,
           exit_price, exit_ts, qty, margin, leverage, stop_loss, take_profit,
           exit_reason, pnl, pnl_pct, rr, balance_after, holding_minutes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (sym, tf, system, NAME_CN.get(system, system), pos["direction"],
         entry, pos["entry_ts"], exit_price, now, qty, margin,
         pos.get("leverage") or 1.0, sl, tp, reason, pnl, pnl_pct, rr,
         balance_after, holding_min))

    # 钱包战绩重算（该槽位全量台账，72 槽位×小样本，代价可忽略）
    rows = conn.execute(
        "SELECT pnl, balance_after FROM twelve_sim_trade "
        "WHERE symbol=? AND tf=? AND system=? ORDER BY exit_ts, id",
        (sym, tf, system)).fetchall()
    pnls = [float(r["pnl"]) for r in rows]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    principal = float(wal.get("principal") or DEFAULT_PRINCIPAL)
    conn.execute(
        """
        UPDATE twelve_sim_wallet
        SET balance=?, equity=?, total_trades=?, win_trades=?, total_pnl=?,
            win_rate=?, profit_factor=?, max_drawdown_pct=?, updated_ts=?
        WHERE symbol=? AND tf=? AND system=?
        """,
        (balance_after, balance_after, len(pnls), len(wins),
         round(sum(pnls), 8),
         round(100.0 * len(wins) / len(pnls), 1) if pnls else None,
         round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
         _max_drawdown_pct(principal, [float(r["balance_after"]) for r in rows]),
         now, sym, tf, system))
    return {"symbol": sym, "tf": tf, "system": system,
            "direction": pos["direction"], "entry_price": entry,
            "exit_price": exit_price, "exit_reason": reason, "pnl": pnl,
            "pnl_pct": pnl_pct, "balance_after": balance_after,
            "holding_minutes": holding_min}


def _liq_price(entry: float, direction: str, leverage: float) -> float | None:
    """逐仓爆仓价：逆向变动 = 1/杠杆（近似口径，忽略维持保证金率）；1 倍无爆仓价。"""
    lev = float(leverage or 1.0)
    if lev <= 1.0:
        return None
    return entry * (1.0 - 1.0 / lev) if direction == "long" else entry * (1.0 + 1.0 / lev)


def _exit_check(pos: dict, price: float,
                bars: list[dict] | None,
                mark_price: float | None = None) -> tuple[str, float] | None:
    """爆仓/止损/止盈 盘中触发判定 → (reason, 结算价) 或 None。

    bars=自开仓以来已收盘 bar（含影线 high/low）；None/为空 → 回退快照现价。
    优先级 liq > sl > tp（同 bar 双触按保守取 SL）；结算价=触发位本身，
    缺口行情（快照已越过触发位）按更差价结算。
    mark_price=合约标记价（2026-08-05 口径切换：爆仓按 markPrice 判定，与币安
    强平规则一致）；None 时回退 price（成交价），sl/tp 始终按成交价口径。
    """
    entry = float(pos["entry_price"])
    long_side = pos["direction"] == "long"
    hi = lo = None
    if bars:
        entry_ms = float(pos["entry_ts"]) * 1000.0
        rel = [b for b in bars if float(b["time"]) > entry_ms]
        if rel:
            hi = max(float(b["high"]) for b in rel)
            lo = min(float(b["low"]) for b in rel)
    # 有效触及范围 = 已收盘 bar 影线 ∪ 快照现价（快照可能领先未收盘 bar）
    eff_hi = max(hi, price) if hi is not None else price
    eff_lo = min(lo, price) if lo is not None else price

    liq = _liq_price(entry, pos["direction"], pos.get("leverage") or 1.0)
    if liq is not None:
        mark = float(mark_price) if mark_price else price
        m_hi = max(hi, mark) if hi is not None else mark
        m_lo = min(lo, mark) if lo is not None else mark
        if long_side and m_lo <= liq:
            return ("liq", liq)
        if not long_side and m_hi >= liq:
            return ("liq", liq)

    sl, tp = pos.get("stop_loss"), pos.get("take_profit")
    if sl is not None:
        sl = float(sl)
        if long_side and eff_lo <= sl:
            return ("sl", min(sl, price))    # 缺口向下：按更差的快照价结算
        if not long_side and eff_hi >= sl:
            return ("sl", max(sl, price))
    if tp is not None:
        tp = float(tp)
        if long_side and eff_hi >= tp:
            return ("tp", tp)
        if not long_side and eff_lo <= tp:
            return ("tp", tp)
    return None


def _timeout_due(pos: dict, now: float) -> bool:
    """TF 分档时间止损：持仓时长 ≥ 上限（5m/15m:1天 30m:2天 1h:3天 4h:7天 1d:14天）。

    对 pending 计划行同口径复用：entry_ts=计划创建时刻 → 挂单超时撤销分档。
    """
    days = float(TF_TIME_STOP_DAYS.get(str(pos["tf"]), 7))
    return (now - float(pos["entry_ts"])) >= days * 86400.0


# ─────────────────────────── 计划(pending)：挂单 / 触达成交 / 跟随 / 撤销 ───────────────────────────

def _plan_points(plan: dict | None) -> dict | None:
    """从信号 plan 提取计划点位与入场方式；entry 缺失/非法返回 None（无计划）。

    返回 {entry, entry_type, stop_loss, take_profit}；sl/tp 允许 None
    （成交时经 _resolve_entry_params 再校验，宁缺毋滥）。
    """
    if not isinstance(plan, dict) or not plan:
        return None
    try:
        entry = float(plan["entry"]) if plan.get("entry") is not None else None
        sl = float(plan["stop_loss"]) if plan.get("stop_loss") is not None else None
        tp_raw = plan.get("take_profit") or plan.get("take_profit_1")
        tp = float(tp_raw) if tp_raw is not None else None
    except (TypeError, ValueError):
        return None
    if entry is None or entry <= 0:
        return None
    et = str(plan.get("entry_type") or "market")
    if et not in ("breakout", "pullback", "market"):
        et = "market"
    return {"entry": entry, "entry_type": et, "stop_loss": sl, "take_profit": tp}


def _entry_touched(direction: str, entry_type: str, entry: float, price: float,
                   bars: list[dict] | None, since_ts: float) -> bool:
    """计划入场价是否已触达：自 since_ts 以来已收盘 bar 影线 ∪ 快照现价（同
    _exit_check 有效触及口径；bars 取不到时优雅回退快照现价比对）。

    breakout：多 high≥entry / 空 low≤entry；pullback：多 low≤entry / 空 high≥entry；
    market（带有效点位，R3 规则1）：按「点位或更优」限价口径 = 多 low≤entry /
    空 high≥entry（与任务边界约定一致：做多触发 现价≤点位 / 做空触发 现价≥点位）。
    """
    hi = lo = None
    if bars:
        since_ms = float(since_ts) * 1000.0
        rel = [b for b in bars if float(b["time"]) > since_ms]
        if rel:
            hi = max(float(b["high"]) for b in rel)
            lo = min(float(b["low"]) for b in rel)
    eff_hi = max(hi, price) if hi is not None else price
    eff_lo = min(lo, price) if lo is not None else price
    long_side = direction == "long"
    if entry_type in ("pullback", "market"):
        return (eff_lo <= entry) if long_side else (eff_hi >= entry)
    return (eff_hi >= entry) if long_side else (eff_lo <= entry)


def _do_plan(conn, sym: str, tf: str, system: str, direction: str,
             pts: dict, eff: dict, price: float, now: float) -> dict:
    """建一条计划(pending)：不建持仓、不动钱包、不进胜率、不写 twelve_sim_trade。

    entry_price=计划入场价、entry_ts=计划创建时刻；qty/margin=0 延迟到成交时
    按 entry 价计算回填（leverage 占位 1，成交时重算）。
    计划创建落 twelve_sim_signal_log 留痕（change_kinds=plan，新计划替换可追溯）。
    """
    cur = conn.execute(
        """
        INSERT INTO twelve_sim_position
          (symbol, tf, system, direction, entry_price, entry_ts, qty, margin,
           leverage, position_pct, stop_loss, take_profit, cur_price,
           unrealized_pnl, status)
        VALUES (?,?,?,?,?,?,0,0,1,?,?,?,?,0,'pending')
        """,
        (sym, tf, system, direction, pts["entry"], now,
         eff.get("position_pct"), pts["stop_loss"], pts["take_profit"], price))
    pid = cur.lastrowid
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,'plan',1,?)
        """,
        (now, sym, tf, system, NAME_CN.get(system, system), pid,
         pts["entry"], pts["stop_loss"], pts["take_profit"], price,
         f"新计划创建（挂单等触达，{'做多' if direction == 'long' else '做空'}"
         f"@{pts['entry']}）"))
    return {"symbol": sym, "tf": tf, "system": system, "direction": direction,
            "entry": pts["entry"], "entry_type": pts["entry_type"],
            "position_id": pid}


def _cancel_plan(conn, pen: dict, reason: str, price: float, now: float) -> dict:
    """计划失效（R3 规则2）：pending 行保留为 status='canceled' + 失效原因/时刻，
    并落 twelve_sim_signal_log 留痕（不产生 twelve_sim_trade，不影响钱包/胜率）。

    失效后该计划永不再参与触达成交判定（触达查询只认 status='pending'）。
    """
    conn.execute(
        "UPDATE twelve_sim_position SET status='canceled', cancel_reason=?, "
        "canceled_ts=?, cur_price=? WHERE id=? AND status='pending'",
        (reason, now, price, pen["id"]))
    old_sl = float(pen["stop_loss"]) if pen.get("stop_loss") is not None else None
    old_tp = float(pen["take_profit"]) if pen.get("take_profit") is not None else None
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,'cancel',1,?)
        """,
        (now, pen["symbol"], pen["tf"], pen["system"],
         NAME_CN.get(str(pen["system"]), str(pen["system"])), pen["id"],
         float(pen["entry_price"]), old_sl, old_tp, price,
         f"计划已失效（{CANCEL_REASON_CN.get(reason, reason)}），"
         f"之后价格触达旧点位也不成交"))
    return {"symbol": pen["symbol"], "tf": pen["tf"], "system": pen["system"],
            "direction": pen["direction"], "entry": float(pen["entry_price"]),
            "reason": reason}


def _maybe_update_plan(conn, pen: dict, pts: dict, price: float,
                       now: float) -> dict | None:
    """计划未成交期间信号点位跟随：entry/SL/TP 实质变化（≥0.2% 同阈值）
    → 更新 pending 行点位并记 twelve_sim_signal_log（applied=1）。"""
    old_entry = float(pen["entry_price"])
    old_sl = float(pen["stop_loss"]) if pen.get("stop_loss") is not None else None
    old_tp = float(pen["take_profit"]) if pen.get("take_profit") is not None else None

    upd_entry = _sltp_changed(old_entry, pts["entry"])
    upd_sl = _sltp_changed(old_sl, pts["stop_loss"])
    upd_tp = _sltp_changed(old_tp, pts["take_profit"])
    if not (upd_entry or upd_sl or upd_tp):
        return None

    new_entry = pts["entry"] if upd_entry else old_entry
    new_sl = pts["stop_loss"] if upd_sl else old_sl
    new_tp = pts["take_profit"] if upd_tp else old_tp
    conn.execute(
        "UPDATE twelve_sim_position SET entry_price=?, stop_loss=?, take_profit=?, "
        "cur_price=? WHERE id=?",
        (new_entry, new_sl, new_tp, price, pen["id"]))
    pen["entry_price"], pen["stop_loss"], pen["take_profit"] = new_entry, new_sl, new_tp

    kinds = ",".join(k for k, u in (("entry", upd_entry), ("sl", upd_sl),
                                    ("tp", upd_tp)) if u)
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
        """,
        (now, pen["symbol"], pen["tf"], pen["system"],
         NAME_CN.get(str(pen["system"]), str(pen["system"])), pen["id"],
         old_entry, old_sl, old_tp, new_entry, new_sl, new_tp,
         price, kinds, "计划(pending)点位跟随"))
    return {"symbol": pen["symbol"], "tf": pen["tf"], "system": pen["system"],
            "position_id": pen["id"], "change_kinds": kinds, "applied": True,
            "entry": new_entry, "stop_loss": new_sl, "take_profit": new_tp,
            "pending": True}


def _fill_plan(conn, pen: dict, plan: dict | None, eff: dict, balance: float,
               price: float, now: float) -> dict | None:
    """计划触达成交：以计划 entry 价转正式持仓（entry_ts=成交时刻），
    SL/TP/杠杆/qty 经 _resolve_entry_params 基于 entry 价合成回填；
    成交落 twelve_sim_signal_log 留痕（change_kinds=fill）。

    点位相对 entry 不自洽（配置/信号漂移）→ 返回 None，由调用方撤销计划。
    """
    entry = float(pen["entry_price"])
    params = _resolve_entry_params(str(pen["direction"]), entry, eff, plan)
    if params is None:
        return None
    margin = round(min(balance, balance * params["position_pct"] / 100.0), 8)
    qty = round(margin * params["leverage"] / entry, 8)
    conn.execute(
        """
        UPDATE twelve_sim_position
        SET entry_ts=?, qty=?, margin=?, leverage=?, position_pct=?,
            stop_loss=?, take_profit=?, cur_price=?, unrealized_pnl=0,
            status='open'
        WHERE id=? AND status='pending'
        """,
        (now, qty, margin, params["leverage"], params["position_pct"],
         params["stop_loss"], params["take_profit"], price, pen["id"]))
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,'fill',1,?)
        """,
        (now, pen["symbol"], pen["tf"], pen["system"],
         NAME_CN.get(str(pen["system"]), str(pen["system"])), pen["id"],
         entry, entry, params["stop_loss"], params["take_profit"], price,
         "计划触达成交（按计划价成交，成交后独立于后续信号变化）"))
    return {"symbol": pen["symbol"], "tf": pen["tf"], "system": pen["system"],
            "direction": pen["direction"], "entry_price": entry, "qty": qty,
            "margin": margin, "leverage": params["leverage"],
            "stop_loss": params["stop_loss"], "take_profit": params["take_profit"],
            "position_id": pen["id"]}


# ─────────────────────────── 持仓点位跟随（信号更新 → 同步 SL/TP + 变更日志） ───────────────────────────

def _sltp_changed(old: float | None, new: float | None) -> bool:
    """新点位是否构成实质变更：旧值缺失时有新值即算；否则相对变化 ≥ 阈值。"""
    if new is None or new <= 0:
        return False
    if old is None or old <= 0:
        return True
    return abs(new - old) / old * 100.0 >= SLTP_MIN_CHANGE_PCT


def _log_signal_drift(conn, pos: dict, sig: dict | None,
                      price: float, now: float) -> dict | None:
    """持仓期间同级别信号点位漂移留痕（R3 规则3：已成交仓位不受后续信号影响）。

    已开仓位的 SL/TP **绝不**跟随信号更新（成交即独立，只按自身
    liq/sl/tp/timeout 生命周期退出）；此处仅在同方向信号的 plan 点位相对持仓
    当前风控点位实质变化（≥ SLTP_MIN_CHANGE_PCT%）时记 applied=0 留痕，
    供看板追溯「信号变了但仓位保持独立」。

    防重：与该持仓最近一条日志的 new 值一致（同阈值口径）则不重复记录。
    返回日志摘要 dict（applied 恒为 False），无实质变化返回 None。
    """
    plan = (sig or {}).get("plan") or {}
    if not isinstance(plan, dict) or not plan:
        return None
    try:
        new_sl = float(plan["stop_loss"]) if plan.get("stop_loss") is not None else None
        tp_raw = plan.get("take_profit") or plan.get("take_profit_1")
        new_tp = float(tp_raw) if tp_raw is not None else None
        new_entry = float(plan["entry"]) if plan.get("entry") is not None else None
    except (TypeError, ValueError):
        return None
    old_sl = float(pos["stop_loss"]) if pos.get("stop_loss") is not None else None
    old_tp = float(pos["take_profit"]) if pos.get("take_profit") is not None else None

    drift_sl = _sltp_changed(old_sl, new_sl)
    drift_tp = _sltp_changed(old_tp, new_tp)
    if not (drift_sl or drift_tp):
        return None

    # 防重：信号维持同一组点位时每轮都会走到这里，与最近一条日志一致则跳过
    last = conn.execute(
        "SELECT new_sl, new_tp FROM twelve_sim_signal_log "
        "WHERE position_id=? ORDER BY id DESC LIMIT 1",
        (pos["id"],)).fetchone()
    if last is not None:
        last_sl = float(last["new_sl"]) if last["new_sl"] is not None else None
        last_tp = float(last["new_tp"]) if last["new_tp"] is not None else None
        if (not _sltp_changed(last_sl, new_sl)
                and not _sltp_changed(last_tp, new_tp)):
            return None

    kinds = ",".join(k for k, u in (("sl", drift_sl), ("tp", drift_tp)) if u)
    conn.execute(
        """
        INSERT INTO twelve_sim_signal_log
          (ts, symbol, tf, system, name_cn, position_id,
           prev_entry, prev_sl, prev_tp, new_entry, new_sl, new_tp,
           price, change_kinds, applied, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
        """,
        (now, pos["symbol"], pos["tf"], pos["system"],
         NAME_CN.get(str(pos["system"]), str(pos["system"])), pos["id"],
         float(pos["entry_price"]), old_sl, old_tp,
         new_entry, new_sl, new_tp, price, kinds,
         "信号点位已更新，但已成交仓位保持独立不跟随（新信号另行计算计划）"))
    return {"symbol": pos["symbol"], "tf": pos["tf"], "system": pos["system"],
            "position_id": pos["id"], "change_kinds": kinds,
            "applied": False, "stop_loss": old_sl, "take_profit": old_tp}


def signal_logs(symbol: str | None = None, tf: str | None = None,
                system: str | None = None, limit: int = 200) -> list[dict]:
    """点位跟随变更日志查询（CLI / 同步器 / 调试用）。"""
    _ensure_init()
    cond, args = [], []
    if symbol:
        cond.append("symbol=?")
        args.append(_norm_symbol(symbol))
    if tf:
        cond.append("tf=?")
        args.append(tf)
    if system:
        cond.append("system=?")
        args.append(system)
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    with _conn() as conn:
        cur = conn.execute(
            f"SELECT * FROM twelve_sim_signal_log{where} "
            f"ORDER BY ts DESC, id DESC LIMIT ?", (*args, max(1, int(limit))))
        return [dict(r) for r in cur.fetchall()]


# ─────────────────────────── 主循环：一轮 ───────────────────────────

_DIR_OF_SIGNAL = {"bullish": "long", "bearish": "short"}


def run_cycle(symbols: list[str] | None = None, cfg: dict | None = None,
              now: float | None = None) -> dict:
    """跑一轮：每个配置币种 → 计划盯盘（失效留痕/点位跟随/触达成交）→ 持仓盯盘
    （liq/sl/tp/timeout，含影线；已成交仓位独立，不受信号变更影响）
    → 开仓/挂计划（有点位未触达只挂 pending；反向信号=新独立计划）。

    symbols=None 时用 twelve_sim_config 里已启用的币种；cfg 为取价配置
    （None 自动加载执行手配置）。单币失败只记日志跳过，永不抛出。
    """
    _ensure_init()
    syms = ([_norm_symbol(s) for s in symbols] if symbols
            else configured_symbols())
    out: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "symbols": {}}
    if not syms:
        out["note"] = "无已启用币种（先 config-set 配置币种级行）"
        return out
    if cfg is None:
        cfg = _load_cfg()
    for sym in syms:
        try:
            out["symbols"][sym] = _cycle_symbol(sym, cfg, now)
        except Exception as exc:  # noqa: BLE001 — 单币异常绝不拖垮整轮
            out["symbols"][sym] = {"error": repr(exc)[:200]}
            _log(f"❌ {sym} 本轮异常（已兜底）: {exc!r}"[:200])
    return out


def _cycle_symbol(sym: str, cfg: dict, now: float | None = None) -> dict:
    ts = float(now if now is not None else time.time())
    price = latest_price(cfg, sym)
    if price is None or price <= 0:
        return {"skipped": "无现价"}
    mark = mark_price_of(cfg, sym)   # 爆仓判定用标记价；None 时 _exit_check 回退成交价
    cfg_rows = list_configs(sym)
    ensure_wallets(sym, cfg_rows)
    signals = read_signals(sym)
    res = {"price": price, "closed": [], "opened": [], "holds": 0,
           "sltp_updates": [], "planned": [], "filled": [], "canceled": [],
           "plan_updates": []}

    with _conn() as conn:
        pos_rows = [dict(p) for p in conn.execute(
            "SELECT * FROM twelve_sim_position WHERE status='open' AND symbol=?",
            (sym,)).fetchall()]
        pend_rows = conn.execute(
            "SELECT * FROM twelve_sim_position WHERE status='pending' AND symbol=?",
            (sym,)).fetchall()
        pendings = {(str(p["tf"]), str(p["system"])): dict(p) for p in pend_rows}
        wal_rows = conn.execute(
            "SELECT tf, system, balance FROM twelve_sim_wallet WHERE symbol=?",
            (sym,)).fetchall()
        balances = {(str(w["tf"]), str(w["system"])): float(w["balance"])
                    for w in wal_rows}
        bars_cache: dict[str, list[dict] | None] = {}   # 每 TF 只拉一次 K 线
        # 本轮成交槽位 → 成交方向（成交后同槽位同方向本轮不再评估新计划）
        filled_dirs: dict[tuple[str, str], set[str]] = {}
        # 超时撤销的槽位本轮不再重挂（否则同轮重建=无限续期）；下一轮信号仍在则重新评估
        timeout_slots: set[tuple[str, str]] = set()

        # 0) 计划(pending)盯盘：失效判定 → 点位跟随 → 触达成交
        #    （R3 规则2：失效判定先于触达成交——信号已变更时，价格触达旧点位也不成交）
        for slot, pen in list(pendings.items()):
            tf_, system_ = slot
            sig = signals.get(slot)
            want = _DIR_OF_SIGNAL.get((sig or {}).get("direction") or "")
            pts = _plan_points((sig or {}).get("plan"))
            eff = effective_config(sym, tf_, system_, cfg_rows)

            reason = None
            if want is None:
                reason = "neutral"           # 信号转中性 → 旧计划失效
            elif want != str(pen["direction"]):
                reason = "flip"              # 信号反向 → 旧计划失效
            elif pts is None:
                reason = "plan_gone"         # 计划消失/entry 失效 → 失效
            elif not eff["enabled"]:
                reason = "disabled"          # 槽位停用 → 失效
            elif _timeout_due(pen, ts):
                reason = "timeout"           # 挂单超过 TF 超时档 → 失效
            if reason:
                res["canceled"].append(_cancel_plan(conn, pen, reason, price, ts))
                pendings.pop(slot)
                if reason == "timeout":
                    timeout_slots.add(slot)
                continue

            # 点位跟随（同方向信号点位更新 → pending 计划以最新信号为准；
            # 先更新再判触达，触达判定用最新 entry）
            upd = _maybe_update_plan(conn, pen, pts, price, ts)
            if upd:
                res["plan_updates"].append(upd)

            if tf_ not in bars_cache:
                bars_cache[tf_] = _fetch_bars(sym, tf_)
            if _entry_touched(str(pen["direction"]), pts["entry_type"],
                              float(pen["entry_price"]), price,
                              bars_cache[tf_], float(pen["entry_ts"])):
                balance = balances.get(slot, DEFAULT_PRINCIPAL)
                if balance < MIN_BALANCE:
                    res["canceled"].append(_cancel_plan(conn, pen, "broke", price, ts))
                    pendings.pop(slot)
                    continue
                filled = _fill_plan(conn, pen, (sig or {}).get("plan"),
                                    eff, balance, price, ts)
                if filled is None:
                    # 点位相对 entry 不自洽（配置/信号漂移）→ 宁缺毋滥失效
                    res["canceled"].append(
                        _cancel_plan(conn, pen, "incoherent", price, ts))
                    pendings.pop(slot)
                    continue
                res["filled"].append(filled)
                filled_dirs.setdefault(slot, set()).add(str(pen["direction"]))
                pendings.pop(slot)
                continue
            # 未触达继续挂：仅刷新展示用现价
            conn.execute("UPDATE twelve_sim_position SET cur_price=? WHERE id=?",
                         (price, pen["id"]))

        # 1) 持仓盯盘：优先级 liq > sl > tp > timeout。
        #    R3 规则3：已成交仓位独立——信号反向不再 flip 平仓、SL/TP 不跟随信号，
        #    只按自身生命周期退出；同方向信号点位漂移仅记 applied=0 留痕。
        #    同槽位可能并存多笔仓位（1多+1空），浮盈按槽位累计后更新钱包 equity。
        open_dirs: dict[tuple[str, str], set[str]] = {}
        upnl_by_slot: dict[tuple[str, str], float] = {}
        for pos in pos_rows:
            slot = (str(pos["tf"]), str(pos["system"]))
            tf_ = slot[0]
            if tf_ not in bars_cache:
                bars_cache[tf_] = _fetch_bars(sym, tf_)
            hit = _exit_check(pos, price, bars_cache[tf_], mark_price=mark)
            if hit:
                reason, exit_px = hit
                res["closed"].append(_do_close(conn, pos, exit_px, reason, ts))
                continue
            if _timeout_due(pos, ts):
                # TF 分档持仓超时：以现价平仓
                res["closed"].append(_do_close(conn, pos, price, "timeout", ts))
                continue
            # 持有（本轮不平仓）：同方向信号点位漂移留痕（不应用），刷新现价与浮盈
            sig = signals.get(slot)
            want = _DIR_OF_SIGNAL.get((sig or {}).get("direction") or "")
            if want == str(pos["direction"]):
                upd = _log_signal_drift(conn, pos, sig, price, ts)
                if upd:
                    res["sltp_updates"].append(upd)
            sign = 1.0 if pos["direction"] == "long" else -1.0
            upnl = round(max((price - float(pos["entry_price"]))
                             * float(pos["qty"]) * sign,
                             -float(pos["margin"])), 8)
            conn.execute(
                "UPDATE twelve_sim_position SET cur_price=?, unrealized_pnl=? "
                "WHERE id=?", (price, upnl, pos["id"]))
            open_dirs.setdefault(slot, set()).add(str(pos["direction"]))
            upnl_by_slot[slot] = upnl_by_slot.get(slot, 0.0) + upnl
            res["holds"] += 1
        for slot, upnl in upnl_by_slot.items():
            conn.execute(
                "UPDATE twelve_sim_wallet SET equity=balance+?, updated_ts=? "
                "WHERE symbol=? AND tf=? AND system=?",
                (round(upnl, 8), ts, sym, slot[0], slot[1]))

        # 2) 开仓/挂计划（R3 规则1+3）：
        #    - 同槽位已有**同方向**持仓 → 视为同一信号观点延续，不重复建仓；
        #    - 反向信号即使有持仓也作为**新的独立计划**评估（不影响已成交仓位）；
        #    - 有有效点位必须触达才成交，未触达一律挂 pending。
        wal_rows = conn.execute(
            "SELECT tf, system, balance FROM twelve_sim_wallet WHERE symbol=?",
            (sym,)).fetchall()
        balances = {(str(w["tf"]), str(w["system"])): float(w["balance"])
                    for w in wal_rows}
        for slot, sig in signals.items():
            tf, system = slot
            if (slot in pendings or slot in timeout_slots
                    or tf not in TFS or system not in SYSTEMS):
                continue
            direction = _DIR_OF_SIGNAL.get(sig["direction"])
            if not direction:
                continue   # neutral 不动
            occupied = open_dirs.get(slot, set()) | filled_dirs.get(slot, set())
            if direction in occupied:
                continue   # 同方向已有持仓 → 不重复建仓
            eff = effective_config(sym, tf, system, cfg_rows)
            if not eff["enabled"]:
                continue
            balance = balances.get(slot, DEFAULT_PRINCIPAL)
            if balance < MIN_BALANCE:
                continue   # 槽位已爆仓，停开
            plan = sig.get("plan")
            pts = _plan_points(plan)
            if (pts and not _entry_touched(direction, pts["entry_type"],
                                           pts["entry"], price, None, ts)):
                # 有点位且现价未触达 → 只挂计划(pending)，价到才成交（规则1）
                res["planned"].append(
                    _do_plan(conn, sym, tf, system, direction, pts, eff, price, ts))
                continue
            # 计划缺失（无点位可比）/ 现价已处于可成交侧 → 按现价立即成交（现有口径）
            params = _resolve_entry_params(direction, price, eff, plan)
            if params is None:
                continue   # 点位缺失或不自洽，宁缺毋滥
            opened = _do_open(conn, sym, tf, system,
                              direction, price, balance, params, ts)
            res["opened"].append(opened)

        # 3) 已失效(canceled)留痕行到期清理（日志表留痕永久，行级留痕仅保窗口期）
        conn.execute(
            "DELETE FROM twelve_sim_position WHERE symbol=? AND status='canceled' "
            "AND canceled_ts IS NOT NULL AND canceled_ts < ?",
            (sym, ts - CANCELED_RETENTION_DAYS * 86400.0))

    if (res["closed"] or res["opened"] or res["sltp_updates"] or res["planned"]
            or res["filled"] or res["canceled"] or res["plan_updates"]):
        _log(f"🧭 {sym} 槽位轮：价 {price} / 平 {len(res['closed'])} "
             f"/ 开 {len(res['opened'])} / 持 {res['holds']} "
             f"/ 计划 +{len(res['planned'])} 成交 {len(res['filled'])} "
             f"撤 {len(res['canceled'])} "
             f"/ 点位跟随 {len(res['sltp_updates']) + len(res['plan_updates'])}")
    return res


# ─────────────────────────── 状态汇总 ───────────────────────────

def status(symbol: str | None = None) -> dict:
    """槽位战绩汇总（钱包 + 在途持仓数 + 计划中数），按累计盈亏降序。"""
    wallets = get_wallets(symbol)
    opens = open_positions(symbol)
    pends = pending_positions(symbol)
    open_slots = {(p["symbol"], p["tf"], p["system"]) for p in opens}
    rows = []
    for w in wallets:
        rows.append({**w, "has_open": (w["symbol"], w["tf"], w["system"]) in open_slots})
    rows.sort(key=lambda x: -(x.get("total_pnl") or 0.0))
    return {"wallets": rows, "open_positions": len(opens),
            "pending_plans": len(pends)}


def to_markdown(st: dict, top: int = 20) -> str:
    lines = ["# 十二系统 × 六时间轴 模拟台账", "",
             f"- 在途持仓 {st['open_positions']} 笔 | 计划中 "
             f"{st.get('pending_plans', 0)} 笔 | 槽位钱包 {len(st['wallets'])} 个",
             "",
             "| 币种 | 时间轴 | 系统 | 余额U | 笔数 | 胜率% | 累计盈亏U | PF | 最大回撤% | 在途 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for w in st["wallets"][:top]:
        lines.append(
            f"| {w['symbol']} | {w['tf']} | {w.get('name_cn') or w['system']} "
            f"| {round(w['balance'], 2)} | {w['total_trades']} "
            f"| {w['win_rate'] if w['win_rate'] is not None else '—'} "
            f"| {round(w['total_pnl'], 2)} "
            f"| {w['profit_factor'] if w['profit_factor'] is not None else '—'} "
            f"| {w['max_drawdown_pct'] if w['max_drawdown_pct'] is not None else '—'} "
            f"| {'●' if w['has_open'] else ''} |")
    return "\n".join(lines)


# ─────────────────────────── CLI（独立进程，绝不挂 dashboard） ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="十二系统×六时间轴槽位级模拟交易引擎（paper-only 独立进程）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cyc = sub.add_parser("cycle", help="跑一轮（默认读配置币种）")
    p_cyc.add_argument("--symbols", default=None, help="逗号分隔，如 ETH,BTC；缺省读配置")
    p_cyc.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", help="常驻循环（独立进程模式）")
    p_run.add_argument("--symbols", default=None)
    p_run.add_argument("--interval-min", type=float, default=5.0,
                       help="轮询间隔（分钟），默认 5")

    p_st = sub.add_parser("status", help="槽位战绩榜")
    p_st.add_argument("--symbol", default=None)
    p_st.add_argument("--json", action="store_true")

    p_cs = sub.add_parser("config-set", help="写一条配置（币种级/tf组级/信号级）")
    p_cs.add_argument("symbol")
    p_cs.add_argument("--tf", default=None, help=f"tf 组级/信号级 scope（{'/'.join(TFS)}）")
    p_cs.add_argument("--system", default=None, help="信号级 scope（system 编码）")
    p_cs.add_argument("--principal", type=float, default=None)
    p_cs.add_argument("--leverage", type=float, default=None)
    p_cs.add_argument("--position-pct", type=float, default=None)
    p_cs.add_argument("--stop-loss-pct", type=float, default=None)
    p_cs.add_argument("--take-profit-pct", type=float, default=None)
    p_cs.add_argument("--disable", action="store_true", help="停用该 scope")
    p_cs.add_argument("--enable", action="store_true", help="启用该 scope")

    p_cl = sub.add_parser("config-list", help="列配置")
    p_cl.add_argument("--symbol", default=None)

    args = ap.parse_args()

    if args.cmd == "cycle":
        syms = ([s.strip() for s in args.symbols.split(",") if s.strip()]
                if args.symbols else None)
        out = run_cycle(syms)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            for sym, r in out.get("symbols", {}).items():
                if r.get("skipped") or r.get("error"):
                    print(f"{sym}: {r.get('skipped') or r.get('error')}")
                else:
                    print(f"{sym}: 价 {r['price']} / 平 {len(r['closed'])} "
                          f"/ 开 {len(r['opened'])} / 持 {r['holds']} "
                          f"/ 计划 +{len(r.get('planned') or [])} "
                          f"成交 {len(r.get('filled') or [])} "
                          f"撤 {len(r.get('canceled') or [])} "
                          f"/ 点位跟随 {len(r.get('sltp_updates') or []) + len(r.get('plan_updates') or [])}")
            if out.get("note"):
                print(out["note"])
    elif args.cmd == "run":
        syms = ([s.strip() for s in args.symbols.split(",") if s.strip()]
                if args.symbols else None)
        interval = max(30.0, args.interval_min * 60.0)
        _log(f"▶️ 槽位模拟引擎启动：symbols={syms or '（配置币种）'} "
             f"间隔 {args.interval_min}min")
        while True:
            try:
                run_cycle(syms)
            except Exception as exc:  # noqa: BLE001 — 循环永不退出
                _log(f"❌ 槽位轮异常（继续）: {exc!r}"[:200])
            time.sleep(interval)
    elif args.cmd == "status":
        st = status(args.symbol)
        print(json.dumps(st, ensure_ascii=False, indent=2) if args.json
              else to_markdown(st))
    elif args.cmd == "config-set":
        enabled = True if args.enable else (False if args.disable else None)
        r = upsert_config(args.symbol, args.tf, args.system,
                          principal=args.principal, leverage=args.leverage,
                          position_pct=args.position_pct,
                          stop_loss_pct=args.stop_loss_pct,
                          take_profit_pct=args.take_profit_pct,
                          enabled=enabled)
        print(json.dumps(r, ensure_ascii=False))
    elif args.cmd == "config-list":
        for row in list_configs(args.symbol):
            print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
