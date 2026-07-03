#!/usr/bin/env python3
"""贾维斯 JARVIS - TradingAgents 辩论增强层（T-18 / 方案 B1 子进程 CLI）。

在 `jarvis_brief`（出计划）→ `jarvis_executor`（下单）之间做**可选的二次评审**：
把贾维斯的偏多计划交给 TradingAgents 多角色（多空研究员/风控/交易员）辩论一轮，
取其 BUY/HOLD/SELL 结论与贾维斯方向对照。

铁律（与 T-18 任务定义一致）：
  - **仅作否决/警示，不双重计分**：不改仓位、不改止损止盈，只可能拦下这一单。
  - **默认关闭 = 零回归**：`jarvis_config` 的 `debate_enabled` 默认 False。
  - **不可用即放行**：TradingAgents 目录缺失 / 依赖未装 / 无 LLM Key / 超时 /
    输出解析失败 → verdict="skipped"，绝不因为辩论层故障卡死交易链（哪怕
    veto 模式）。辩论层是增强不是单点。

结论映射（贾维斯计划都是「偏多（战术）」才会走到 executor）：
  TA=BUY  → agree（一致，放行）
  TA=HOLD → warn （中性分歧，放行但把警示写进结果）
  TA=SELL → warn 模式放行+强警示；veto 模式否决本单

配置（jarvis_config.py，改配置即生效）：
  debate_enabled      默认 False
  debate_mode         warn | veto（默认 warn）
  debate_timeout_sec  默认 300

用法：
  python jarvis_debate.py BTCUSDT            # 手动跑一轮评审（人读输出）
  python jarvis_debate.py BTCUSDT --json     # 机器读
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

# TradingAgents 仓库位置：与 Vibe-Trading 同级（AILiangH/TradingAgents）。
TA_DIR = os.environ.get(
    "JARVIS_TRADINGAGENTS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "TradingAgents"),
)

# 子进程里跑的最小 runner：propagate 一轮，最后一行输出 JSON。
# TradingAgents 自身日志走 stdout/stderr 皆有可能，故用哨兵行定位结果。
_RUNNER = r"""
import json, sys
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
cfg = DEFAULT_CONFIG.copy()
ta = TradingAgentsGraph(debug=False, config=cfg)
_, decision = ta.propagate(sys.argv[1], sys.argv[2])
print("JARVIS_DEBATE_RESULT " + json.dumps({"decision": str(decision)}, ensure_ascii=False))
"""


def ta_symbol(symbol: str) -> str:
    """BTCUSDT → BTC-USD（TradingAgents 数据面是 yfinance/finnhub 口径）。"""
    s = (symbol or "").upper().strip()
    for suffix in ("USDT", "USD"):
        if s.endswith(suffix):
            return s[: -len(suffix)] + "-USD"
    return s


def classify_signal(text: str) -> str | None:
    """从 TradingAgents 最终结论文本里抽 BUY/SELL/HOLD（大小写/中文兼容）。"""
    t = (text or "").upper()
    # 逆序找最终动词，避免摘要里同时出现多个词时误判（最终结论通常在句尾）
    best, best_pos = None, -1
    for word, sig in (("BUY", "BUY"), ("SELL", "SELL"), ("HOLD", "HOLD"),
                      ("买入", "BUY"), ("卖出", "SELL"), ("持有", "HOLD"), ("观望", "HOLD")):
        pos = t.rfind(word if word.isascii() else word)
        if pos > best_pos:
            best, best_pos = sig, pos
    return best


def _load_cfg(cfg: dict | None) -> dict:
    if cfg is not None:
        return cfg
    try:
        import jarvis_config as jcfg
        return jcfg.load()
    except Exception:  # noqa: BLE001
        return {}


def run_tradingagents(symbol: str, trade_date: str, timeout_sec: int) -> dict:
    """子进程跑一轮 TradingAgents 辩论。返回 {ok, decision?, error?}。"""
    if not os.path.isdir(TA_DIR):
        return {"ok": False, "error": f"TradingAgents 目录不存在: {TA_DIR}"}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _RUNNER, ta_symbol(symbol), trade_date],
            cwd=TA_DIR, capture_output=True, text=True, timeout=timeout_sec,
            env={**os.environ, "PYTHONPATH": TA_DIR},
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"辩论超时（>{timeout_sec}s）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"子进程启动失败: {exc!r}"[:300]}
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("JARVIS_DEBATE_RESULT "):
            try:
                payload = json.loads(line[len("JARVIS_DEBATE_RESULT "):])
                return {"ok": True, "decision": payload.get("decision", "")}
            except Exception:  # noqa: BLE001
                break
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    return {"ok": False, "error": ("无结果输出；尾部日志: " + " | ".join(err[-3:]))[:300]}


def review(symbol: str, decision: dict | None = None, cfg: dict | None = None,
           _runner=None) -> dict:
    """对一份偏多决策做辩论二次评审。永不抛出。

    Returns:
        {enabled, verdict: agree|warn|veto|skipped, ta_signal, ta_decision,
         mode, summary, elapsed_sec}
    """
    conf = _load_cfg(cfg)
    enabled = bool(conf.get("debate_enabled", False))
    mode = str(conf.get("debate_mode", "warn"))
    timeout_sec = int(conf.get("debate_timeout_sec", 300) or 300)
    out: dict = {"enabled": enabled, "mode": mode, "engine": "tradingagents",
                 "verdict": "skipped", "ta_signal": None, "ta_decision": None, "summary": ""}
    if not enabled:
        out["summary"] = "辩论层未启用（debate_enabled=false）"
        return out
    t0 = time.time()
    try:
        runner = _runner or run_tradingagents
        res = runner(symbol, time.strftime("%Y-%m-%d"), timeout_sec)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"评审异常: {exc!r}"[:300]}
    out["elapsed_sec"] = round(time.time() - t0, 1)
    if not res.get("ok"):
        out["verdict"] = "skipped"
        out["summary"] = f"辩论层不可用，放行（{res.get('error')}）"
        return out
    ta_text = str(res.get("decision") or "")
    sig = classify_signal(ta_text)
    out["ta_decision"] = ta_text[:500]
    out["ta_signal"] = sig
    if sig == "BUY":
        out["verdict"] = "agree"
        out["summary"] = "TradingAgents 辩论结论 BUY，与贾维斯偏多一致"
    elif sig == "HOLD":
        out["verdict"] = "warn"
        out["summary"] = "TradingAgents 辩论结论 HOLD（中性分歧）：建议复核，仍放行"
    elif sig == "SELL":
        out["verdict"] = "veto" if mode == "veto" else "warn"
        out["summary"] = ("TradingAgents 辩论结论 SELL 与偏多相反：" +
                          ("veto 模式，否决本单" if mode == "veto" else "warn 模式，强警示但放行"))
    else:
        out["verdict"] = "skipped"
        out["summary"] = "辩论输出无法解析出 BUY/SELL/HOLD，放行"
    return out


def gate(symbol: str, decision: dict | None = None, cfg: dict | None = None,
         _runner=None) -> dict:
    """executor 下单门禁封装：只回答「放不放行」。永不抛出。

    Returns:
        {allow: bool, review: dict}——仅 verdict==veto 时 allow=False。
    """
    try:
        rev = review(symbol, decision, cfg, _runner=_runner)
    except Exception as exc:  # noqa: BLE001
        rev = {"enabled": False, "verdict": "skipped",
               "summary": f"辩论层内部异常，放行: {exc!r}"[:200]}
    return {"allow": rev.get("verdict") != "veto", "review": rev}


def main() -> int:
    ap = argparse.ArgumentParser(description="TradingAgents 辩论二次评审（T-18）")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略 debate_enabled 开关强制跑一轮")
    args = ap.parse_args()
    cfg = _load_cfg(None)
    if args.force:
        cfg = {**cfg, "debate_enabled": True}
    rev = review(args.symbol, cfg=cfg)
    if args.json:
        print(json.dumps(rev, ensure_ascii=False, indent=2))
    else:
        print(f"[{rev.get('verdict')}] {rev.get('summary')}")
        if rev.get("ta_decision"):
            print("TA 结论:", rev["ta_decision"][:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
