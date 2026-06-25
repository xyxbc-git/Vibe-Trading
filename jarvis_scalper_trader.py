#!/usr/bin/env python3
"""贾维斯 JARVIS — 15m 自动跟盘引擎（S-06 v2）。

v2 升级：集成三层复合策略框架 + 行情分类器 + 动态止损 + 多时间框架。

核心循环：
  永续循环:
    1. 拉最新 15m/1h/4h 多级别 K 线
    2. 行情分类器判定当前市场状态（震荡/趋势/突破）
    3. 复合策略三层决策（进攻/解困/保命）
    4. ATR 动态止盈止损 + 移动止损
    5. 管理现有持仓
    6. 记录战绩 + 行情分类日志
    7. 等待下一根 K 线

依赖：
  - jarvis_regime_classifier（行情分类器）
  - jarvis_composite_strategy（三层复合策略）
  - jarvis_scalper_evolve（获取最优策略）
  - jarvis_wallet（记账）
  - scalper_config.yaml（配置文件）

用法：
  python jarvis_scalper_trader.py run --symbol BTCUSDT
  python jarvis_scalper_trader.py status
  python jarvis_scalper_trader.py report
  python jarvis_scalper_trader.py dry-run --symbol BTCUSDT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import pandas as pd

import jarvis_scalper_evolve as evolve
import jarvis_wallet as jw
import jarvis_journal as jj
import jarvis_regime_classifier as rc
import jarvis_composite_strategy as cs
import jarvis_performance_tracker as pt

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
LOG_PATH = os.path.join(CONFIG_DIR, "jarvis_scalper_trader.log")
STATE_PATH = os.path.join(CONFIG_DIR, "scalper_trader_state.json")
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "scalper_config.yaml"
)

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def _log(msg: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [TRADER] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ═══════════════════════════ 配置管理 ═══════════════════════════

DEFAULT_CONFIG = {
    "risk": {
        "daily_loss_limit": -0.02,
        "daily_loss_action": "warn",
        "max_concurrent_positions": 3,
        "single_trade_risk": 0.01,
        "min_balance_to_trade": 10,
    },
    "trading": {
        "always_on": True,
        "confidence_threshold": 0.6,
        "aggressive_mode": True,
        "cool_down_bars": 0,
    },
    "timeframe": "15m",
    "symbol": "BTCUSDT",
}


def load_config() -> dict[str, Any]:
    """加载配置文件，支持热更新。"""
    cfg = dict(DEFAULT_CONFIG)
    if HAS_YAML and os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                file_cfg = yaml.safe_load(f)
            if file_cfg:
                _deep_merge(cfg, file_cfg)
        except Exception as e:
            _log(f"配置文件读取异常，使用默认值: {e}")
    return cfg


def _deep_merge(base: dict, override: dict) -> None:
    """深度合并字典。"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ═══════════════════════════ 状态管理 ═══════════════════════════

def _load_state() -> dict[str, Any]:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "positions": [],
        "daily_pnl": 0.0,
        "daily_trades": 0,
        "daily_reset_date": "",
        "consecutive_losses": 0,
        "cool_down_until_bar": 0,
        "total_trades": 0,
        "total_pnl": 0.0,
        "wins": 0,
        "losses": 0,
    }


def _save_state(state: dict[str, Any]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _reset_daily_if_needed(state: dict[str, Any]) -> None:
    """日重置：每天零点重置日内计数。"""
    today = time.strftime("%Y-%m-%d")
    if state.get("daily_reset_date") != today:
        state["daily_pnl"] = 0.0
        state["daily_trades"] = 0
        state["daily_reset_date"] = today


# ═══════════════════════════ K 线获取 ═══════════════════════════

def _fetch_latest_klines(symbol: str, timeframe: str = "15m", limit: int = 200) -> list[dict] | None:
    """从 Binance 获取最新 K 线数据。"""
    try:
        import requests
        interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
        interval = interval_map.get(timeframe, "15m")
        url = f"https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        klines = []
        for k in raw:
            klines.append({
                "time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return klines
    except Exception as e:
        _log(f"K 线获取失败: {e}")
        return None


# ═══════════════════════════ 信号计算 ═══════════════════════════

def _compute_signals(klines: list[dict], strategy_code: str) -> dict[str, bool]:
    """用策略代码计算最新 K 线的信号。

    Returns:
        {"open_long": bool, "close_long": bool,
         "open_short": bool, "close_short": bool}
    """
    try:
        import pandas as pd
        import numpy as np

        df = pd.DataFrame(klines)

        sandbox = {
            "df": df,
            "pd": pd,
            "np": np,
            "params": {},
            "open": df["open"],
            "high": df["high"],
            "low": df["low"],
            "close": df["close"],
            "volume": df["volume"],
        }

        exec(strategy_code, sandbox)  # noqa: S102
        df = sandbox["df"]

        last_idx = len(df) - 1
        return {
            "open_long": bool(df.get("open_long", pd.Series(False)).iloc[last_idx]) if "open_long" in df.columns else False,
            "close_long": bool(df.get("close_long", pd.Series(False)).iloc[last_idx]) if "close_long" in df.columns else False,
            "open_short": bool(df.get("open_short", pd.Series(False)).iloc[last_idx]) if "open_short" in df.columns else False,
            "close_short": bool(df.get("close_short", pd.Series(False)).iloc[last_idx]) if "close_short" in df.columns else False,
        }
    except Exception as e:
        _log(f"信号计算异常: {e}")
        return {"open_long": False, "close_long": False, "open_short": False, "close_short": False}


# ═══════════════════════════ 风控检查 ═══════════════════════════

def _check_risk(state: dict, cfg: dict, balance: float) -> tuple[bool, str]:
    """风控检查：是否允许开仓。

    Returns:
        (can_trade, reason)
    """
    risk = cfg.get("risk", {})

    # 余额检查
    min_bal = risk.get("min_balance_to_trade", 10)
    if balance < min_bal:
        return False, f"余额不足: {balance:.2f} < {min_bal}"

    # 持仓数量检查
    max_pos = risk.get("max_concurrent_positions", 3)
    open_positions = [p for p in state.get("positions", []) if p.get("status") == "open"]
    if len(open_positions) >= max_pos:
        return False, f"持仓已满: {len(open_positions)}/{max_pos}"

    # 日亏损检查
    daily_limit = risk.get("daily_loss_limit", -0.02)
    action = risk.get("daily_loss_action", "warn")
    daily_pnl_ratio = state.get("daily_pnl", 0) / max(balance, 1)
    if daily_pnl_ratio <= daily_limit:
        if action == "stop":
            return False, f"日亏损达限({daily_pnl_ratio:.2%})，已停手"
        elif action == "pause":
            return False, f"日亏损达限({daily_pnl_ratio:.2%})，暂停1小时"
        else:
            _log(f"警告: 日亏损达限({daily_pnl_ratio:.2%})，继续交易")

    # 冷却检查
    cool_down = cfg.get("trading", {}).get("cool_down_bars", 0)
    if cool_down > 0 and state.get("cool_down_until_bar", 0) > time.time():
        return False, "冷却期中"

    return True, "OK"


# ═══════════════════════════ 交易执行 ═══════════════════════════

def _open_position(
    state: dict, cfg: dict, direction: str,
    price: float, balance: float, symbol: str,
) -> dict | None:
    """模拟开仓。"""
    risk = cfg.get("risk", {})
    trade_risk = risk.get("single_trade_risk", 0.01)
    position_size = balance * trade_risk

    if position_size < 1:
        _log(f"仓位太小: {position_size:.2f} USDT")
        return None

    qty = position_size / price

    position = {
        "id": f"scalper_{int(time.time()*1000)}",
        "symbol": symbol,
        "direction": direction,
        "entry_price": price,
        "qty": qty,
        "size_usdt": position_size,
        "open_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "open_ts": time.time(),
        "status": "open",
        "bars_held": 0,
    }

    state["positions"].append(position)
    state["daily_trades"] = state.get("daily_trades", 0) + 1
    state["total_trades"] = state.get("total_trades", 0) + 1

    _log(f"开仓: {direction} {symbol} @ {price:.2f} | 仓位 {position_size:.2f} USDT")
    return position


_tracker: pt.PerformanceTracker | None = None


def _get_tracker() -> pt.PerformanceTracker:
    """获取全局表现追踪器（懒初始化）。"""
    global _tracker
    if _tracker is None:
        _tracker = pt.PerformanceTracker()
    return _tracker


def _close_position(
    state: dict, position: dict, price: float, reason: str,
    regime_name: str = "unknown",
) -> float:
    """模拟平仓，返回盈亏。自动记录到表现追踪器。"""
    entry = position["entry_price"]
    qty = position["qty"]
    direction = position["direction"]

    if direction == "long":
        pnl = (price - entry) * qty
    else:
        pnl = (entry - price) * qty

    position["status"] = "closed"
    position["exit_price"] = price
    position["exit_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    position["pnl"] = pnl
    position["exit_reason"] = reason

    state["daily_pnl"] = state.get("daily_pnl", 0) + pnl
    state["total_pnl"] = state.get("total_pnl", 0) + pnl

    if pnl > 0:
        state["wins"] = state.get("wins", 0) + 1
        state["consecutive_losses"] = 0
    else:
        state["losses"] = state.get("losses", 0) + 1
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1

    _log(f"平仓: {direction} {position['symbol']} @ {price:.2f} | "
         f"盈亏 {pnl:+.2f} USDT | 原因: {reason}")

    try:
        tracker = _get_tracker()
        tracker.record_trade({
            "symbol": position.get("symbol", ""),
            "direction": direction,
            "entry_price": entry,
            "exit_price": price,
            "qty": qty,
            "pnl": round(pnl, 4),
            "regime": regime_name,
            "reason": reason,
            "bars_held": position.get("bars_held", 0),
            "is_hedge": position.get("is_hedge", False),
        })
    except Exception as e:
        _log(f"表现追踪记录失败（不影响交易）: {e}")

    return pnl


def _manage_positions(
    state: dict, cfg: dict, current_price: float, signals: dict,
    regime: rc.RegimeResult | None = None, atr: float = 0.0,
    composite: cs.CompositeStrategy | None = None,
) -> None:
    """管理现有持仓：使用复合策略的动态止盈止损。"""
    regime_name = regime.regime if regime else "unknown"

    for pos in state.get("positions", []):
        if pos.get("status") != "open":
            continue

        pos["bars_held"] = pos.get("bars_held", 0) + 1

        if composite and regime and atr > 0:
            cs_pos = cs.Position(
                id=pos.get("id", ""),
                symbol=pos.get("symbol", ""),
                direction=pos["direction"],
                entry_price=pos["entry_price"],
                qty=pos["qty"],
                size_usdt=pos.get("size_usdt", 0),
                open_time=pos.get("open_time", ""),
                bars_held=pos.get("bars_held", 0),
                avg_price=pos.get("avg_price", pos["entry_price"]),
                total_qty=pos.get("total_qty", pos["qty"]),
                total_size=pos.get("total_size", pos.get("size_usdt", 0)),
                stop_loss=pos.get("stop_loss", 0),
                take_profit=pos.get("take_profit", 0),
                trailing_high=pos.get("trailing_high", 0),
                trailing_low=pos.get("trailing_low", 999999),
            )
            decision = composite.manage_position(cs_pos, regime, current_price, atr, signals)
            pos["stop_loss"] = cs_pos.stop_loss
            pos["trailing_high"] = cs_pos.trailing_high
            pos["trailing_low"] = cs_pos.trailing_low
            if decision:
                _close_position(state, pos, current_price, decision.reasoning, regime_name)
        else:
            direction = pos["direction"]
            sl = pos.get("stop_loss", 0)
            tp = pos.get("take_profit", 0)

            if sl > 0:
                if direction == "long" and current_price <= sl:
                    _close_position(state, pos, current_price, "止损", regime_name)
                    continue
                if direction == "short" and current_price >= sl:
                    _close_position(state, pos, current_price, "止损", regime_name)
                    continue
            if tp > 0:
                if direction == "long" and current_price >= tp:
                    _close_position(state, pos, current_price, "止盈", regime_name)
                    continue
                if direction == "short" and current_price <= tp:
                    _close_position(state, pos, current_price, "止盈", regime_name)
                    continue

            if direction == "long" and signals.get("close_long"):
                _close_position(state, pos, current_price, "信号反转", regime_name)
            elif direction == "short" and signals.get("close_short"):
                _close_position(state, pos, current_price, "信号反转", regime_name)


# ═══════════════════════════ 主循环 ═══════════════════════════

def run_cycle(symbol: str, dry_run: bool = False) -> dict[str, Any]:
    """执行一个交易周期（v2：三层复合策略 + 行情分类）。

    Returns:
        {"action": str, "signals": dict, "price": float, "regime": dict, ...}
    """
    cfg = load_config()
    state = _load_state()
    _reset_daily_if_needed(state)

    # 获取最优策略（用于信号计算）
    best = evolve.get_best_strategy()
    strategy_code = best.get("code", "") if best else ""

    # 拉取多时间框架 K 线
    klines = _fetch_latest_klines(symbol, cfg.get("timeframe", "15m"), 200)
    if not klines:
        return {"action": "no_data", "reason": "K 线获取失败"}

    df_15m = pd.DataFrame(klines)
    current_price = df_15m["close"].iloc[-1]
    _log(f"当前价格: {symbol} = {current_price:.2f}")

    # 拉取 1h 和 4h 数据用于多时间框架分析
    klines_1h = _fetch_latest_klines(symbol, "1h", 200)
    klines_4h = _fetch_latest_klines(symbol, "4h", 200)
    df_1h = pd.DataFrame(klines_1h) if klines_1h else None
    df_4h = pd.DataFrame(klines_4h) if klines_4h else None

    # 行情分类
    regime = rc.classify_multi_tf(df_15m, df_1h, df_4h)
    _log(f"行情分类: {regime.regime} | {regime.direction} | "
         f"置信度 {regime.confidence:.0%} | {regime.reasoning}")

    # 计算 ATR
    atr = rc._atr(df_15m["high"], df_15m["low"], df_15m["close"], 14).iloc[-1]

    # 计算交易信号
    if strategy_code:
        signals = _compute_signals(klines, strategy_code)
    else:
        signals = {
            "open_long": regime.direction == "bullish" and regime.confidence > 0.6,
            "open_short": regime.direction == "bearish" and regime.confidence > 0.6,
            "close_long": regime.direction == "bearish" and regime.confidence > 0.7,
            "close_short": regime.direction == "bullish" and regime.confidence > 0.7,
        }
    _log(f"信号: {signals}")

    # 初始化复合策略
    composite = cs.CompositeStrategy(cfg.get("composite_strategy"))

    # 构建持仓列表
    cs_positions = []
    for p in state.get("positions", []):
        if p.get("status") != "open":
            continue
        cs_positions.append(cs.Position(
            id=p.get("id", ""),
            symbol=p.get("symbol", symbol),
            direction=p["direction"],
            entry_price=p["entry_price"],
            qty=p["qty"],
            size_usdt=p.get("size_usdt", 0),
            open_time=p.get("open_time", ""),
            bars_held=p.get("bars_held", 0),
            add_count=p.get("add_count", 0),
            avg_price=p.get("avg_price", p["entry_price"]),
            total_qty=p.get("total_qty", p["qty"]),
            total_size=p.get("total_size", p.get("size_usdt", 0)),
            stop_loss=p.get("stop_loss", 0),
            take_profit=p.get("take_profit", 0),
            trailing_high=p.get("trailing_high", 0),
            trailing_low=p.get("trailing_low", 999999),
            is_hedge=p.get("is_hedge", False),
        ))

    # 管理现有持仓（动态止损）
    _manage_positions(state, cfg, current_price, signals, regime, atr, composite)

    # 风控检查
    jw.init_db()
    bal = jw.get_balance()
    balance = bal.get("cash_usdt", 0) if bal else 0

    can_trade, risk_reason = _check_risk(state, cfg, balance)

    action = "hold"
    decision_info = {}

    if can_trade:
        decision = composite.decide(
            regime=regime,
            signals=signals,
            positions=cs_positions,
            current_price=current_price,
            atr=atr,
            balance=balance,
        )

        action = decision.action
        decision_info = decision.to_dict()
        _log(f"复合策略决策: {decision.action} | 层: {decision.layer} | {decision.reasoning}")

        if not dry_run and decision.action not in ("hold",):
            if "open_long" in decision.action or "add_long" in decision.action:
                pos = _open_position(state, cfg, "long", current_price, balance, symbol)
                if pos:
                    pos["stop_loss"] = decision.stop_loss
                    pos["take_profit"] = decision.take_profit
            elif "open_short" in decision.action or "add_short" in decision.action:
                pos = _open_position(state, cfg, "short", current_price, balance, symbol)
                if pos:
                    pos["stop_loss"] = decision.stop_loss
                    pos["take_profit"] = decision.take_profit
            elif "hedge_long" in decision.action:
                pos = _open_position(state, cfg, "long", current_price, balance, symbol)
                if pos:
                    pos["is_hedge"] = True
                    pos["stop_loss"] = decision.stop_loss
                    pos["take_profit"] = decision.take_profit
            elif "hedge_short" in decision.action:
                pos = _open_position(state, cfg, "short", current_price, balance, symbol)
                if pos:
                    pos["is_hedge"] = True
                    pos["stop_loss"] = decision.stop_loss
                    pos["take_profit"] = decision.take_profit
    else:
        _log(f"风控拦截: {risk_reason}")
        action = f"blocked:{risk_reason}"

    _save_state(state)

    return {
        "action": action,
        "signals": signals,
        "price": current_price,
        "balance": balance,
        "strategy": best.get("name", "行情驱动") if best else "行情驱动",
        "regime": regime.to_dict(),
        "atr": round(atr, 2),
        "decision": decision_info,
        "open_positions": len([p for p in state.get("positions", []) if p.get("status") == "open"]),
        "daily_pnl": state.get("daily_pnl", 0),
    }


def run_loop(symbol: str, dry_run: bool = False) -> None:
    """永续交易循环（v2：含衰退检测 + 自动再进化 + 周报）。"""
    cfg = load_config()
    timeframe = cfg.get("timeframe", "15m")
    interval_sec = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}.get(timeframe, 900)

    _log(f"启动永续交易循环: {symbol} | 周期 {timeframe} | 间隔 {interval_sec}s")
    _log(f"配置: {json.dumps(cfg, ensure_ascii=False, default=str)}")

    cycle_count = 0
    last_decay_check = 0
    last_weekly_report = ""

    DECAY_CHECK_INTERVAL = 50
    WEEKLY_REPORT_DAY = 6

    while True:
        try:
            cfg = load_config()

            jw.init_db()
            bal = jw.get_balance()
            balance = bal.get("cash_usdt", 0) if bal else 0

            min_bal = cfg.get("risk", {}).get("min_balance_to_trade", 10)
            if balance < min_bal:
                _log(f"余额 {balance:.2f} < {min_bal}，等待充值...")
                time.sleep(interval_sec)
                continue

            result = run_cycle(symbol, dry_run=dry_run)
            cycle_count += 1
            _log(f"周期结果: {result.get('action', '?')} | "
                 f"持仓 {result.get('open_positions', 0)} | "
                 f"日盈亏 {result.get('daily_pnl', 0):+.2f}")

            # 定期衰退检测（每 DECAY_CHECK_INTERVAL 个周期）
            if cycle_count - last_decay_check >= DECAY_CHECK_INTERVAL:
                last_decay_check = cycle_count
                try:
                    tracker = _get_tracker()
                    if len(tracker._history) >= 20:
                        report = tracker.evaluate(recent_n=30)
                        if report.is_decaying:
                            _log(f"⚠️ 衰退检测: {report.decay_signals}")
                            tuner = pt.AdaptiveTuner()
                            tune_result = tuner.tune(report, cfg)
                            if tune_result.adjusted:
                                _log(f"📊 自适应调整: {tune_result.reasoning}")
                            if len(report.decay_signals) >= 2 and not dry_run:
                                _log("🔄 触发自动再进化...")
                                evolve.check_and_trigger_re_evolve(symbol=symbol)
                        else:
                            _log(f"✓ 表现正常: 胜率{report.win_rate:.0f}% "
                                 f"PF{report.profit_factor:.2f}")
                except Exception as e:
                    _log(f"衰退检测异常（不影响交易）: {e}")

            # 每周日自动生成周报
            import datetime
            today = datetime.date.today()
            week_key = today.strftime("%Y-W%W")
            if today.weekday() == WEEKLY_REPORT_DAY and week_key != last_weekly_report:
                last_weekly_report = week_key
                try:
                    md = pt.generate_weekly_report()
                    _log(f"📋 周报已生成: {week_key}")
                except Exception as e:
                    _log(f"周报生成异常: {e}")

        except KeyboardInterrupt:
            _log("收到中断信号，停止交易")
            break
        except Exception as e:
            _log(f"交易循环异常: {e}")

        _log(f"等待 {interval_sec}s 下一根 K 线...")
        time.sleep(interval_sec)


def get_report() -> dict[str, Any]:
    """生成战绩报表。"""
    state = _load_state()
    total = state.get("total_trades", 0)
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    total_pnl = state.get("total_pnl", 0)

    win_rate = wins / max(total, 1) * 100
    closed = [p for p in state.get("positions", []) if p.get("status") == "closed"]
    open_pos = [p for p in state.get("positions", []) if p.get("status") == "open"]

    profit_trades = [p for p in closed if p.get("pnl", 0) > 0]
    loss_trades = [p for p in closed if p.get("pnl", 0) < 0]

    avg_profit = sum(p["pnl"] for p in profit_trades) / len(profit_trades) if profit_trades else 0
    avg_loss = abs(sum(p["pnl"] for p in loss_trades) / len(loss_trades)) if loss_trades else 1
    profit_factor = avg_profit / avg_loss if avg_loss > 0 else 0

    avg_bars = sum(p.get("bars_held", 0) for p in closed) / len(closed) if closed else 0

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "daily_pnl": round(state.get("daily_pnl", 0), 2),
        "avg_bars_held": round(avg_bars, 1),
        "open_positions": len(open_pos),
        "closed_positions": len(closed),
        "consecutive_losses": state.get("consecutive_losses", 0),
    }


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="15m 自动跟盘引擎")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="启动永续交易循环")
    p_run.add_argument("--symbol", default="BTCUSDT")

    p_dry = sub.add_parser("dry-run", help="试运行一个周期（不实际交易）")
    p_dry.add_argument("--symbol", default="BTCUSDT")

    sub.add_parser("status", help="查看当前状态")
    sub.add_parser("report", help="查看战绩报表")
    sub.add_parser("reset", help="重置交易状态（危险）")

    args = parser.parse_args()

    if args.cmd == "run":
        run_loop(args.symbol, dry_run=False)

    elif args.cmd == "dry-run":
        result = run_cycle(args.symbol, dry_run=True)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.cmd == "status":
        state = _load_state()
        report = get_report()
        best = evolve.get_best_strategy()

        print("=== 15m 自动跟盘状态 ===")
        print(f"  当前策略: {best['name'] if best else '无（需先运行进化引擎）'}")
        print(f"  开仓中:   {report['open_positions']} 个")
        print(f"  今日盈亏: {report['daily_pnl']:+.2f} USDT")
        print(f"  总交易:   {report['total_trades']} 笔")
        print(f"  胜率:     {report['win_rate_pct']:.1f}%")
        print(f"  盈亏比:   {report['profit_factor']:.2f}")
        print(f"  累计盈亏: {report['total_pnl']:+.2f} USDT")

    elif args.cmd == "report":
        report = get_report()
        print("=== 战绩报表 ===")
        for k, v in report.items():
            print(f"  {k:25s} {v}")

    elif args.cmd == "reset":
        confirm = input("确认重置所有交易状态？(yes/no): ")
        if confirm.lower() == "yes":
            _save_state({
                "positions": [],
                "daily_pnl": 0.0,
                "daily_trades": 0,
                "daily_reset_date": "",
                "consecutive_losses": 0,
                "cool_down_until_bar": 0,
                "total_trades": 0,
                "total_pnl": 0.0,
                "wins": 0,
                "losses": 0,
            })
            print("已重置")
        else:
            print("取消")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
