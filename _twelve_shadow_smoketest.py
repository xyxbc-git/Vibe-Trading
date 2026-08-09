"""离线冒烟：13诊断 D7 反向信号影子验证（jarvis_twelve_shadow）。

打桩手法同 _twelve_sim_smoketest.py：临时 DB（jarvis_db 对非默认路径强制走
SQLite）、latest_price/_fetch_bars/_market_context 打桩、直接向
twelve_signal_state / twelve_sim_trade 注入样本。全离线：不联网、不触发
12 信号重算、不碰生产库。

验收口径（开发计划 D7 节）：
  1. 注入稳定亏组合 + 同 env 信号 → 影子反向单开仓（SL/TP 镜像互换）
  2. env 不匹配 → 不开
  3. 样本不足（19 < 20）→ stable_losers 不命中；无候选 → cycle 优雅空转
  4. 平仓生命周期（sl 触发）→ twelve_shadow_trade 落账、钱包更新
  5. report 原向 vs 反向对照正确
  6. 主台账五表零变更（行数前后一致断言）
"""
from __future__ import annotations

import json
import os
import tempfile
import time

_d = tempfile.mkdtemp()

import jarvis_signal_history as jsh  # noqa: E402
import jarvis_twelve_trader as jtt  # noqa: E402
import jarvis_twelve_shadow as jsw  # noqa: E402

# 三模块共用同一个临时库（shadow._conn 转发 jtt._conn，重定向 jtt 即整体隔离）
jsh.DB_DIR = _d
jsh.DB_PATH = os.path.join(_d, "test.db")
jtt.DB_DIR = _d
jtt.DB_PATH = jsh.DB_PATH
jtt.LOG_PATH = os.path.join(_d, "test.log")
jsw.LOG_PATH = os.path.join(_d, "shadow.log")

fails: list[str] = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


SYM = "ETHUSDT"

# ── 全局打桩：全程无网络 ─────────────────────────────────────────────────
_PRICE = {"v": 100.0}
jtt.latest_price = lambda cfg, s: _PRICE["v"]
jtt.mark_price_of = lambda cfg, s: None
jtt._fetch_bars = lambda symbol, tf: None
jtt._fee_pct = lambda: 0.0            # 免手续费保持整数断言
_CTX = {"v": {"ctx_regime": "trending"}}
jsw._market_context = lambda sym, tf, now: dict(_CTX["v"])

# 建表
jsh.init_db()
jtt.init_db()
jsw.init_db()


def set_signal(tf: str, system: str, direction: str, plan: dict | None = None,
               strength: float = 0.6) -> None:
    """注入/覆盖一条槽位当前态信号（DELETE+INSERT，绕开方言差异）。"""
    with jsh._conn() as conn:
        conn.execute(
            "DELETE FROM twelve_signal_state WHERE symbol=? AND tf=? AND system=?",
            (SYM, tf, system))
        conn.execute(
            """
            INSERT INTO twelve_signal_state
              (symbol, tf, system, name_cn, direction, strength, reasoning,
               levels_json, plan_json, updated_ts, changed_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (SYM, tf, system, jtt.NAME_CN.get(system, system), direction,
             strength, "smoketest", "[]",
             json.dumps(plan, ensure_ascii=False), time.time(), time.time()))


def inject_sim_trades(system: str, tf: str, env: str, n: int,
                      win_rate: float, pnl_win: float = 5.0,
                      pnl_loss: float = -5.0) -> None:
    """向主台账 twelve_sim_trade 注入 n 笔带 ctx_regime 的历史成交样本。"""
    now = time.time()
    wins = int(round(n * win_rate))
    with jtt._conn() as conn:
        for i in range(n):
            pnl = pnl_win if i < wins else pnl_loss
            conn.execute(
                """
                INSERT INTO twelve_sim_trade
                  (symbol, tf, system, direction, entry_price, entry_ts,
                   exit_price, exit_ts, qty, margin, leverage, exit_reason,
                   pnl, ctx_regime)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (SYM, tf, system, "long", 100.0, now - 3600.0 - i,
                 100.0 + pnl, now - i, 1.0, 10.0, 1.0,
                 "sl" if pnl < 0 else "tp", pnl, env))


def table_counts() -> dict[str, int]:
    """主台账五表行数（零变更断言用）。"""
    tables = ("twelve_sim_wallet", "twelve_sim_position", "twelve_sim_trade",
              "twelve_sim_signal_log", "twelve_sim_config")
    out = {}
    with jtt._conn() as conn:
        for t in tables:
            out[t] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {t}")
                         .fetchone()["n"])
    return out


# ═══ 用例 0：无候选 → 优雅空转 ═══════════════════════════════════════════
r0 = jsw.run_cycle([SYM])
check("0a 无稳定亏候选 idle=True", r0.get("idle") is True, json.dumps(r0))
check("0b 空转不开单", r0.get("opened") == 0)

# ═══ 用例 1：stable_losers 识别（门槛边界） ═══════════════════════════════
# oscillator@5m trending：25 笔 胜率 20% 净亏 → 命中
inject_sim_trades("oscillator", "5m", "trending", 25, 0.20)
# gap@15m ranging：19 笔（< min_samples 20）→ 不命中
inject_sim_trades("gap", "15m", "ranging", 19, 0.10)
losers = jsw.stable_losers(days=7)
keys = {(c["system"], c["tf"], c["env"]) for c in losers}
check("1a 25笔胜率20%净亏 命中 stable_losers",
      ("oscillator", "5m", "trending") in keys, str(keys))
check("1b 19笔样本不足 不命中", ("gap", "15m", "ranging") not in keys)
hit = next(c for c in losers if c["system"] == "oscillator")
check("1c flip_hint 理论胜率=100-20",
      abs(hit["flip_hint"]["win_rate_pct"] - 80.0) < 1e-6, str(hit))

# 用例 3 要用的第二个稳定亏候选也先注入（基线快照必须在全部注入之后，
# 否则测试自身的注入会污染「shadow 零接触主台账」断言）
inject_sim_trades("elliott", "1h", "trending", 25, 0.10)

# 主台账基线快照（此后 shadow 的一切操作不得改变这些行数）
_BASE = table_counts()

# ═══ 用例 2：同 env 信号 → 反向影子单开仓（SL/TP 镜像互换） ═══════════════
# 原向信号 bullish（long）：entry=100 sl=95 tp=110 → 影子 short：sl=110 tp=95
set_signal("5m", "oscillator", "bullish",
           {"entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
            "leverage": 2, "position_pct": 10})
_CTX["v"] = {"ctx_regime": "trending"}
_PRICE["v"] = 100.0
r2 = jsw.run_cycle([SYM])
check("2a 环境匹配开影子单", r2.get("opened") == 1, json.dumps(r2))
with jtt._conn() as conn:
    pos = conn.execute(
        "SELECT * FROM twelve_shadow_position WHERE status='open'").fetchone()
pos = dict(pos) if pos else {}
check("2b 方向取反 short", pos.get("direction") == "short", str(pos))
check("2c SL=原TP 110", float(pos.get("stop_loss") or 0) == 110.0)
check("2d TP=原SL 95", float(pos.get("take_profit") or 0) == 95.0)
check("2e src_env=trending", pos.get("src_env") == "trending")
check("2f src_system=oscillator", pos.get("src_system") == "oscillator")

# 同槽位不重复建仓
r2x = jsw.run_cycle([SYM])
check("2g 在途影子单不重复开", r2x.get("opened") == 0, json.dumps(r2x))

# ═══ 用例 3：env 不匹配 → 不开 ═══════════════════════════════════════════
# 稳定亏候选 elliott@1h trending（样本已在基线前注入），现场环境切到 ranging
set_signal("1h", "elliott", "bullish",
           {"entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0})
_CTX["v"] = {"ctx_regime": "ranging"}
r3 = jsw.run_cycle([SYM])
check("3a 环境错配不开影子单", r3.get("opened") == 0, json.dumps(r3))
check("3b 错配计数=1", r3.get("skipped_env_mismatch") == 1)
# 环境取不到（空 dict）同样不开
_CTX["v"] = {}
r3x = jsw.run_cycle([SYM])
check("3c 环境缺失不开影子单", r3x.get("opened") == 0)

# ═══ 用例 4：平仓生命周期（影子 short 的 SL 在上方 110） ═══════════════════
_CTX["v"] = {"ctx_regime": "trending"}
_PRICE["v"] = 112.0   # 快照越过 SL=110 → 缺口按更差价（112）结算？口径：结算=触发位，缺口按更差价
r4 = jsw.run_cycle([SYM])
check("4a SL 触发平仓", r4.get("closed") == 1, json.dumps(r4))
with jtt._conn() as conn:
    tr = conn.execute("SELECT * FROM twelve_shadow_trade").fetchone()
    wal = conn.execute(
        "SELECT * FROM twelve_shadow_wallet WHERE symbol=? AND tf=? AND system=?",
        (SYM, "5m", "oscillator")).fetchone()
tr = dict(tr) if tr else {}
wal = dict(wal) if wal else {}
check("4b exit_reason=sl", tr.get("exit_reason") == "sl", str(tr.get("exit_reason")))
check("4c 影子亏损 pnl<0", float(tr.get("pnl") or 0) < 0, str(tr.get("pnl")))
check("4d src 三列落账",
      tr.get("src_system") == "oscillator" and tr.get("src_env") == "trending")
check("4e 钱包扣减", float(wal.get("balance") or 0)
      == 100.0 + float(tr.get("pnl") or 0), str(wal.get("balance")))
check("4f 钱包战绩计数", wal.get("total_trades") == 1)

# ═══ 用例 5：report 原向 vs 反向对照 ══════════════════════════════════════
rep = jsw.report(days=7)
combo = next((c for c in rep["combos"]
              if c["system"] == "oscillator" and c["env"] == "trending"), None)
check("5a report 命中组合", combo is not None, json.dumps(rep)[:300])
check("5b 原向 25 笔净亏",
      combo and combo["original"] and combo["original"]["trades"] == 25
      and combo["original"]["net_pnl"] < 0, str(combo and combo["original"]))
check("5c 影子 1 笔已对照", combo and combo["shadow"]["trades"] == 1)
check("5d 样本不足只陈列不下结论",
      combo and combo["forward_validated"] is None
      and "insufficient" in str(combo["note"]))

# ═══ 用例 6：主台账五表零变更 ═════════════════════════════════════════════
_AFTER = table_counts()
check("6a 主台账五表零变更", _AFTER == _BASE, f"{_BASE} -> {_AFTER}")

print()
if fails:
    print(f"FAILED: {len(fails)} 项 -> {fails}")
    raise SystemExit(1)
print("ALL GREEN ✅  (D7 影子验证冒烟全绿)")
