#!/usr/bin/env python3
"""贾维斯 JARVIS — 15m 自动跟盘引擎（S-06）。

使用进化引擎产出的最优策略，实时 15 分钟自动交易。
只要有余额就持续交易，所有风控参数从 scalper_config.yaml 读取，
支持热更新（修改配置后无需重启）。

核心循环：
  永续循环:
    1. 拉最新 15m K 线
    2. 用最优策略计算信号
    3. 信号达标 + 余额充足 + 仓位未满 → 果断开仓
    4. 管理现有持仓（止盈/止损/移动止损）
    5. 记录战绩
    6. 等待下一根 K 线

依赖：
  - jarvis_scalper_evolve（获取最优策略）
  - jarvis_wallet（记账）
  - jarvis_crypto_data（拉 K 线）
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

import jarvis_scalper_evolve as evolve
import jarvis_wallet as jw
import jarvis_journal as jj

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

def _fetch_latest_klines(symbol: str, timeframe: str = "15m", limit: int = 100) -> list[dict] | None:
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


def _close_position(
    state: dict, position: dict, price: float, reason: str,
) -> float:
    """模拟平仓，返回盈亏。"""
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

    return pnl


def _manage_positions(state: dict, cfg: dict, current_price: float, signals: dict) -> None:
    """管理现有持仓：检查止盈止损。"""
    for pos in state.get("positions", []):
        if pos.get("status") != "open":
            continue

        pos["bars_held"] = pos.get("bars_held", 0) + 1
        entry = pos["entry_price"]
        direction = pos["direction"]

        # 简易止损止盈（基于价格百分比）
        sl_pct = 0.015
        tp_pct = 0.025

        if direction == "long":
            if current_price <= entry * (1 - sl_pct):
                _close_position(state, pos, current_price, "止损")
            elif current_price >= entry * (1 + tp_pct):
                _close_position(state, pos, current_price, "止盈")
            elif signals.get("close_long"):
                _close_position(state, pos, current_price, "信号反转")
        else:
            if current_price >= entry * (1 + sl_pct):
                _close_position(state, pos, current_price, "止损")
            elif current_price <= entry * (1 - tp_pct):
                _close_position(state, pos, current_price, "止盈")
            elif signals.get("close_short"):
                _close_position(state, pos, current_price, "信号反转")


# ═══════════════════════════ 主循环 ═══════════════════════════

def run_cycle(symbol: str, dry_run: bool = False) -> dict[str, Any]:
    """执行一个交易周期。

    Returns:
        {"action": str, "signals": dict, "price": float, ...}
    """
    cfg = load_config()
    state = _load_state()
    _reset_daily_if_needed(state)

    # 获取最优策略
    best = evolve.get_best_strategy()
    if not best:
        _log("名人堂为空，请先运行进化引擎")
        return {"action": "no_strategy", "reason": "名人堂为空"}

    strategy_code = best.get("code", "")
    if not strategy_code:
        _log("最优策略无代码")
        return {"action": "no_code", "reason": "策略无代码"}

    # 拉取 K 线
    klines = _fetch_latest_klines(symbol, cfg.get("timeframe", "15m"))
    if not klines:
        return {"action": "no_data", "reason": "K 线获取失败"}

    current_price = klines[-1]["close"]
    _log(f"当前价格: {symbol} = {current_price:.2f}")

    # 计算信号
    signals = _compute_signals(klines, strategy_code)
    _log(f"信号: {signals}")

    # 管理现有持仓
    _manage_positions(state, cfg, current_price, signals)

    # 风控检查
    jw.init_db()
    bal = jw.get_balance()
    balance = bal.get("cash_usdt", 0) if bal else 0

    can_trade, risk_reason = _check_risk(state, cfg, balance)

    action = "hold"

    if can_trade:
        if signals.get("open_long"):
            if not dry_run:
                _open_position(state, cfg, "long", current_price, balance, symbol)
            action = "open_long"
            _log(f"{'[DRY-RUN] ' if dry_run else ''}开多 {symbol}")
        elif signals.get("open_short"):
            if not dry_run:
                _open_position(state, cfg, "short", current_price, balance, symbol)
            action = "open_short"
            _log(f"{'[DRY-RUN] ' if dry_run else ''}开空 {symbol}")
    else:
        _log(f"风控拦截: {risk_reason}")
        action = f"blocked:{risk_reason}"

    _save_state(state)

    return {
        "action": action,
        "signals": signals,
        "price": current_price,
        "balance": balance,
        "strategy": best.get("name", "?"),
        "open_positions": len([p for p in state.get("positions", []) if p["status"] == "open"]),
        "daily_pnl": state.get("daily_pnl", 0),
    }


def run_loop(symbol: str, dry_run: bool = False) -> None:
    """永续交易循环。"""
    cfg = load_config()
    timeframe = cfg.get("timeframe", "15m")
    interval_sec = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}.get(timeframe, 900)

    _log(f"启动永续交易循环: {symbol} | 周期 {timeframe} | 间隔 {interval_sec}s")
    _log(f"配置: {json.dumps(cfg, ensure_ascii=False, default=str)}")

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
            _log(f"周期结果: {result.get('action', '?')} | "
                 f"持仓 {result.get('open_positions', 0)} | "
                 f"日盈亏 {result.get('daily_pnl', 0):+.2f}")

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
