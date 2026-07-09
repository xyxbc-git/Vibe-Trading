#!/usr/bin/env python3
"""贾维斯 JARVIS — QD API 回测桥接（S-03）。

通过 QuantDinger Agent Gateway API 提交策略代码并执行回测，
返回结构化回测结果。全程 API 调用，无需打开 QD 页面。

调用链：
  1. POST /api/agent/v1/backtests  → 提交回测任务（含策略代码）
  2. GET  /api/agent/v1/jobs/{id}  → 轮询任务状态
  3. 任务完成后解析 result 字段

依赖：
  - 环境变量 QUANTDINGER_AGENT_TOKEN（QD Agent Token，scope 需含 B）
  - QD 后端服务运行中（默认 http://localhost:8888）

用法：
  python jarvis_scalper_backtest.py run --code strategy.py --symbol BTCUSDT
  python jarvis_scalper_backtest.py run --rule rule.json --symbol BTCUSDT
  python jarvis_scalper_backtest.py status --job-id <job_id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import requests

import jarvis_scalper_codegen as codegen

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
LOG_PATH = os.path.join(CONFIG_DIR, "jarvis_scalper_backtest.log")

DEFAULTS = {
    "gateway_base": "http://localhost:8888",
    "agent_token": "",
    "default_market": "Crypto",
    "default_symbol": "BTC/USDT",
    "default_timeframe": "15m",
    "default_start_date": "2025-01-01",
    "default_end_date": "2026-06-01",
    "initial_capital": 10000,
    "poll_interval_sec": 3,
    "poll_timeout_sec": 300,
    "max_retries": 3,
    "retry_delay_sec": 5,
}


def _log(msg: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _get_config() -> dict[str, Any]:
    """读取配置：环境变量 > 配置文件 > 默认值。"""
    cfg = dict(DEFAULTS)
    config_path = os.path.join(CONFIG_DIR, "scalper_backtest_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                file_cfg = json.load(f)
                cfg.update(file_cfg)
        except Exception:
            pass

    env_token = os.getenv("QUANTDINGER_AGENT_TOKEN", "")
    if env_token:
        cfg["agent_token"] = env_token.strip()

    env_base = os.getenv("QUANTDINGER_GATEWAY_BASE", "")
    if env_base:
        cfg["gateway_base"] = env_base.strip()

    return cfg


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _api_request(
    method: str,
    url: str,
    token: str,
    data: dict | None = None,
    max_retries: int = 3,
    retry_delay: float = 5.0,
) -> dict[str, Any]:
    """带重试的 API 请求。"""
    last_err = None
    for attempt in range(max_retries):
        try:
            if method == "GET":
                resp = requests.get(url, headers=_headers(token), timeout=30)
            else:
                resp = requests.post(url, headers=_headers(token), json=data, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                _log(f"API 请求失败（第 {attempt+1} 次），{retry_delay}s 后重试: {e}")
                time.sleep(retry_delay)
    raise ConnectionError(f"API 请求失败（已重试 {max_retries} 次）: {last_err}")


# ═══════════════════════════ 核心功能 ═══════════════════════════

# QD 引擎按 K 线根数限制单次回测：15m 周期约 365 天（≈35040 根）为上限预算，
# 据此按周期换算各自允许的最大跨度（高周期更长、低周期更短）。
_MAX_BARS_BUDGET = 365 * 96  # 35040


def _timeframe_minutes(timeframe: str) -> int | None:
    """把 '15m' / '1h' / '1d' 解析为每根 K 线分钟数；无法解析返回 None。"""
    tf = (timeframe or "").strip().lower()
    if len(tf) < 2:
        return None
    unit = tf[-1]
    try:
        n = int(tf[:-1])
    except ValueError:
        return None
    if n <= 0:
        return None
    if unit == "m":
        return n
    if unit == "h":
        return n * 60
    if unit == "d":
        return n * 1440
    return None


def _clamp_date_range(start_date: str, end_date: str, timeframe: str) -> tuple[str, str]:
    """按 QD 引擎 K 线根数预算收窄过长的回测区间（保留 end_date，前移 start_date）。

    无法解析周期或日期时原样返回，避免影响正常请求。
    """
    minutes = _timeframe_minutes(timeframe)
    if not minutes:
        return start_date, end_date
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return start_date, end_date
    if end <= start:
        return start_date, end_date
    max_days = int(_MAX_BARS_BUDGET * minutes / 1440)
    # 留 1 天余量：QD validate_backtest_range 按 (end-start).days 比对上限，
    # 其自身推荐窗口也用 max_days-1，避免边界与少量指标 warmup 把请求顶过上限。
    safe_days = max(1, max_days - 1)
    span_days = (end - start).days
    if span_days <= safe_days:
        return start_date, end_date
    new_start_str = (end - timedelta(days=safe_days)).strftime("%Y-%m-%d")
    _log(
        f"⚠ 回测区间 {span_days} 天超过 {timeframe} 周期上限 {safe_days} 天，"
        f"自动收窄起始日期 {start_date} → {new_start_str}（保留结束日期 {end_date}）"
    )
    return new_start_str, end_date


def submit_backtest(
    code: str,
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    start_date: str = "2025-01-01",
    end_date: str = "2026-06-01",
    initial_capital: float = 10000,
    strategy_name: str | None = None,
) -> dict[str, Any]:
    """提交回测任务到 QD。

    Args:
        code: 策略 Python 代码字符串
        symbol: 交易对
        timeframe: K线周期
        start_date: 回测起始日期
        end_date: 回测结束日期
        initial_capital: 初始资金
        strategy_name: 策略名称（用于幂等键）

    Returns:
        {"job_id": str, "status": str, ...}
    """
    cfg = _get_config()
    token = cfg["agent_token"]
    if not token:
        raise ValueError("缺少 QUANTDINGER_AGENT_TOKEN 环境变量")

    start_date, end_date = _clamp_date_range(start_date, end_date, timeframe)

    idempotency_key = f"scalper-{strategy_name or 'unnamed'}-{uuid.uuid4().hex[:8]}"

    url = f"{cfg['gateway_base']}/api/agent/v1/backtests"
    payload = {
        "code": code,
        "market": cfg["default_market"],
        "symbol": symbol,
        "timeframe": timeframe,
        "start_date": start_date,
        "end_date": end_date,
        "strictMode": True,
    }

    headers = _headers(token)
    headers["Idempotency-Key"] = idempotency_key

    _log(f"提交回测: {strategy_name or 'unnamed'} | {symbol} {timeframe} | {start_date}~{end_date}")

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        job_id = result.get("data", {}).get("job_id", result.get("job_id", ""))
        _log(f"回测已提交: job_id={job_id}")
        return result
    except Exception as e:
        _log(f"提交回测失败: {e}")
        raise


def poll_job(job_id: str, timeout_sec: int = 300, interval_sec: int = 3) -> dict[str, Any]:
    """轮询回测任务直到完成。

    Returns:
        完成后的 job 数据（含 result 字段）
    """
    cfg = _get_config()
    token = cfg["agent_token"]
    if not token:
        raise ValueError("缺少 QUANTDINGER_AGENT_TOKEN 环境变量")

    url = f"{cfg['gateway_base']}/api/agent/v1/jobs/{job_id}"
    start = time.time()

    while time.time() - start < timeout_sec:
        try:
            result = _api_request("GET", url, token)
            data = result.get("data", result)
            status = data.get("status", "unknown")

            if status == "succeeded":
                _log(f"回测完成: job_id={job_id}")
                return data
            elif status in ("failed", "cancelled", "error"):
                error_msg = data.get("error", "未知错误")
                _log(f"回测失败: job_id={job_id}, error={error_msg}")
                return data
            else:
                _log(f"回测进行中: job_id={job_id}, status={status}")
        except Exception as e:
            _log(f"轮询异常: {e}")

        time.sleep(interval_sec)

    _log(f"回测超时: job_id={job_id}, timeout={timeout_sec}s")
    return {"status": "timeout", "job_id": job_id, "error": f"轮询超时 {timeout_sec}s"}


_EXIT_REASON_CN = {
    "stop": "止损",
    "profit": "止盈",
    "trailing": "移动止损",
    "liquidation": "爆仓",
    "": "信号平仓",
}


def _normalize_trades(events: list[dict], timeframe: str | None = None) -> list[dict]:
    """把 QD 引擎的事件流（open_*/close_* 各一条）配对成前端友好的「回合制」记录。

    QD 每条事件形如 {time, type: open_long|close_long_stop|.., price, amount, profit, balance}；
    桌面端表格与 analyzer 期望 {direction, entry_time, exit_time, entry_price, exit_price,
    pnl, bars_held, ...}。若事件本身已是回合制（带 entry_price），原样透传。
    """
    if not events:
        return []
    if any(t.get("entry_price") is not None or t.get("entryPrice") is not None for t in events):
        return list(events)  # 已是回合制结构，勿重复加工

    tf_min = _timeframe_minutes(timeframe or "") or 0

    def _bars_between(t0: str, t1: str) -> float:
        if not tf_min:
            return 0
        try:
            d0 = datetime.strptime(str(t0), "%Y-%m-%d %H:%M")
            d1 = datetime.strptime(str(t1), "%Y-%m-%d %H:%M")
            return max(0.0, round((d1 - d0).total_seconds() / 60 / tf_min, 1))
        except (ValueError, TypeError):
            return 0

    rounds: list[dict] = []
    open_pos: dict | None = None
    for ev in events:
        etype = str(ev.get("type", "") or "")
        if etype.startswith("open_"):
            # 极端情况下连续两个 open（不应发生）：先把前一个记为未平仓回合
            if open_pos is not None:
                rounds.append(open_pos | {"status": "open"})
            open_pos = {
                "direction": "long" if "long" in etype else "short",
                "entry_time": ev.get("time"),
                "entry_price": ev.get("price"),
                "amount": ev.get("amount"),
                "exit_time": None,
                "exit_price": None,
                "pnl": 0.0,
                "exit_reason": None,
                "balance": ev.get("balance"),
                "status": "open",
            }
            continue
        if etype.startswith("close_") or etype == "liquidation":
            direction = "long" if "long" in etype else ("short" if "short" in etype else
                                                        (open_pos or {}).get("direction", "long"))
            suffix = etype.rsplit("_", 1)[-1] if "_" in etype else ""
            reason = "liquidation" if etype == "liquidation" else (
                suffix if suffix in ("stop", "profit", "trailing") else "")
            rnd = {
                "direction": direction,
                "entry_time": (open_pos or {}).get("entry_time"),
                "entry_price": (open_pos or {}).get("entry_price"),
                "amount": ev.get("amount", (open_pos or {}).get("amount")),
                "exit_time": ev.get("time"),
                "exit_price": ev.get("price"),
                "pnl": float(ev.get("profit", 0) or 0),
                "exit_reason": _EXIT_REASON_CN.get(reason, "信号平仓"),
                "balance": ev.get("balance"),
                "status": "closed",
            }
            if rnd["entry_time"] and rnd["exit_time"]:
                rnd["bars_held"] = _bars_between(rnd["entry_time"], rnd["exit_time"])
            rounds.append(rnd)
            open_pos = None
            continue
        # 未知事件类型：原样保留，避免吞数据
        rounds.append(dict(ev))
    if open_pos is not None:
        rounds.append(open_pos)
    return rounds


def _diagnose_no_trades(
    timeframe: str | None,
    start_date: str | None,
    end_date: str | None,
    bar_count: int | None,
) -> str:
    """0 成交时生成用户友好诊断：区分「K 线不足/指标预热」与「策略本身无信号」。"""
    WARMUP_BARS = 200  # 常见指标（EMA200/rolling200）预热需求
    tf = timeframe or "?"
    tf_min = _timeframe_minutes(timeframe or "")
    est_bars = None
    if tf_min and start_date and end_date:
        try:
            d0 = datetime.strptime(start_date, "%Y-%m-%d")
            d1 = datetime.strptime(end_date, "%Y-%m-%d")
            est_bars = max(0, int((d1 - d0).total_seconds() / 60 / tf_min))
        except (ValueError, TypeError):
            est_bars = None
    bars = bar_count if bar_count else est_bars
    if bars is not None and bars < WARMUP_BARS + 50:
        need_days = ""
        if tf_min:
            days = int((WARMUP_BARS + 100) * tf_min / 1440) + 1
            need_days = f"，建议把回测区间拉长到 {days} 天以上"
        return (
            f"{tf} 周期该区间约 {bars} 根 K 线，常见指标预热需要约 {WARMUP_BARS} 根，"
            f"信号区几乎被预热吃光{need_days}。"
        )
    return (
        "回测区间 K 线充足，策略在该区间没有触发进场信号，属于策略行为而非系统故障；"
        "可尝试放宽进场条件（AND 改 OR / 降低阈值）或更换因子组合。"
    )


def parse_backtest_result(
    job_data: dict[str, Any],
    timeframe: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """解析 QD 回测结果为标准化格式。

    Returns:
        {
            "status": "succeeded" | "failed" | ...,
            "total_return_pct": float,
            "win_rate": float,
            "profit_factor": float,
            "max_drawdown_pct": float,
            "sharpe_ratio": float,
            "total_trades": int,
            "avg_trade_pnl": float,
            "avg_bars_held": float,
            "trades": [...],   # 逐笔交易（回合制：direction/entry_*/exit_*/pnl）
            "diagnosis": str,  # 仅 0 成交时给出的友好诊断
            "raw": {...},      # 原始返回（含 QD 原始事件流与资金曲线）
        }
    """
    status = job_data.get("status", "unknown")
    if status != "succeeded":
        return {
            "status": status,
            "error": job_data.get("error", ""),
            "total_return_pct": 0,
            "win_rate": 0,
            "profit_factor": 0,
            "max_drawdown_pct": 0,
            "sharpe_ratio": 0,
            "total_trades": 0,
            "avg_trade_pnl": 0,
            "avg_bars_held": 0,
            "trades": [],
            "raw": job_data,
        }

    result = job_data.get("result", {})
    # QD（run_aligned → _format_result）把指标平铺在 result 顶层、采用驼峰命名，
    # 并无 "metrics"/"summary" 包裹层；缺包裹层时回退到 result 本身，否则全部读成 0。
    metrics = result.get("metrics") or result.get("summary") or result
    raw_trades = result.get("trades", result.get("trade_list", []))
    trades = _normalize_trades(raw_trades, timeframe)

    total_return = metrics.get(
        "total_return_pct",
        metrics.get("totalReturnPct", metrics.get("totalReturn", 0)),
    )
    win_rate = metrics.get("win_rate", metrics.get("winRate", 0))
    profit_factor = metrics.get("profit_factor", metrics.get("profitFactor", 0))
    max_dd = metrics.get(
        "max_drawdown_pct",
        metrics.get("maxDrawdownPct", metrics.get("maxDrawdown", 0)),
    )
    sharpe = metrics.get("sharpe_ratio", metrics.get("sharpeRatio", 0))
    total_trades = metrics.get("total_trades", metrics.get("totalTrades", len(trades)))

    avg_pnl = 0.0
    avg_bars = 0.0
    closed = [t for t in trades if t.get("status") != "open"]
    if closed:
        pnls = [t.get("pnl", t.get("profit", 0)) for t in closed]
        bars = [t.get("bars_held", t.get("barsHeld", 0)) for t in closed]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0
        avg_bars = sum(bars) / len(bars) if bars else 0

    out = {
        "status": "succeeded",
        "total_return_pct": float(total_return),
        "win_rate": float(win_rate),
        "profit_factor": float(profit_factor),
        "max_drawdown_pct": float(max_dd),
        "sharpe_ratio": float(sharpe),
        "total_trades": int(total_trades),
        "avg_trade_pnl": float(avg_pnl),
        "avg_bars_held": float(avg_bars),
        "trades": trades,
        "raw": result,
    }
    if int(total_trades) == 0:
        equity = result.get("equity_curve") or result.get("equityCurve") or []
        out["diagnosis"] = _diagnose_no_trades(
            timeframe, start_date, end_date, len(equity) or None,
        )
    return out


def run_backtest(
    rule: dict[str, Any] | None = None,
    code: str | None = None,
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    start_date: str = "2025-01-01",
    end_date: str = "2026-06-01",
    initial_capital: float = 10000,
) -> dict[str, Any]:
    """完整回测流程：规则/代码 → 提交 → 轮询 → 解析。

    Args:
        rule: LLM 生成的规则 JSON（与 code 二选一）
        code: 策略代码字符串（与 rule 二选一）
        symbol: 交易对
        timeframe: K线周期
        start_date: 回测起始
        end_date: 回测结束
        initial_capital: 初始资金

    Returns:
        标准化的回测结果字典
    """
    if rule and not code:
        gen = codegen.rule_to_code(rule)
        if not gen["valid"]:
            return {
                "status": "code_error",
                "error": f"代码生成失败: {gen['errors']}",
                "total_return_pct": 0,
                "win_rate": 0,
                "profit_factor": 0,
                "max_drawdown_pct": 0,
                "sharpe_ratio": 0,
                "total_trades": 0,
                "avg_trade_pnl": 0,
                "avg_bars_held": 0,
                "trades": [],
                "raw": {},
            }
        code = gen["code"]
        strategy_name = gen["name"]
    elif code:
        strategy_name = "custom_code"
    else:
        raise ValueError("必须提供 rule 或 code 之一")

    submit_result = submit_backtest(
        code=code,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        strategy_name=strategy_name,
    )

    data = submit_result.get("data", submit_result)
    job_id = data.get("job_id", "")
    if not job_id:
        return {
            "status": "submit_error",
            "error": f"提交回测未返回 job_id: {submit_result}",
            "total_return_pct": 0,
            "win_rate": 0,
            "profit_factor": 0,
            "max_drawdown_pct": 0,
            "sharpe_ratio": 0,
            "total_trades": 0,
            "avg_trade_pnl": 0,
            "avg_bars_held": 0,
            "trades": [],
            "raw": submit_result,
        }

    cfg = _get_config()
    job_data = poll_job(
        job_id,
        timeout_sec=cfg["poll_timeout_sec"],
        interval_sec=cfg["poll_interval_sec"],
    )

    # 传入区间与周期：0 成交时才能给出「K 线不足/预热」类友好诊断
    return parse_backtest_result(
        job_data, timeframe=timeframe, start_date=start_date, end_date=end_date,
    )


def check_qd_health() -> dict[str, Any]:
    """检查 QD 服务是否可用。"""
    cfg = _get_config()
    try:
        resp = requests.get(f"{cfg['gateway_base']}/api/agent/v1/health", timeout=10)
        return {"healthy": resp.status_code == 200, "data": resp.json()}
    except Exception as e:
        return {"healthy": False, "error": str(e)}


def check_token() -> dict[str, Any]:
    """检查 Agent Token 是否有效。"""
    cfg = _get_config()
    token = cfg["agent_token"]
    if not token:
        return {"valid": False, "error": "未配置 QUANTDINGER_AGENT_TOKEN"}
    try:
        resp = requests.get(
            f"{cfg['gateway_base']}/api/agent/v1/whoami",
            headers=_headers(token),
            timeout=10,
        )
        data = resp.json()
        return {"valid": resp.status_code == 200, "data": data}
    except Exception as e:
        return {"valid": False, "error": str(e)}


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="QD API 回测桥接")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="提交并执行回测")
    grp = p_run.add_mutually_exclusive_group(required=True)
    grp.add_argument("--code", help="策略代码文件路径")
    grp.add_argument("--rule", help="规则 JSON 文件路径")
    p_run.add_argument("--symbol", default="BTC/USDT")
    p_run.add_argument("--timeframe", default="15m")
    p_run.add_argument("--start", default="2025-01-01")
    p_run.add_argument("--end", default="2026-06-01")
    p_run.add_argument("--capital", type=float, default=10000)

    p_status = sub.add_parser("status", help="查询回测任务状态")
    p_status.add_argument("--job-id", required=True)

    sub.add_parser("health", help="检查 QD 服务状态")
    sub.add_parser("whoami", help="检查 Token 信息")

    args = parser.parse_args()

    if args.cmd == "run":
        rule_data = None
        code_str = None
        if args.rule:
            with open(args.rule, encoding="utf-8") as f:
                rule_data = json.load(f)
        elif args.code:
            with open(args.code, encoding="utf-8") as f:
                code_str = f.read()

        result = run_backtest(
            rule=rule_data,
            code=code_str,
            symbol=args.symbol,
            timeframe=args.timeframe,
            start_date=args.start,
            end_date=args.end,
            initial_capital=args.capital,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.cmd == "status":
        cfg = _get_config()
        job_data = poll_job(args.job_id)
        print(json.dumps(job_data, ensure_ascii=False, indent=2, default=str))

    elif args.cmd == "health":
        result = check_qd_health()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cmd == "whoami":
        result = check_token()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
