#!/usr/bin/env python3
"""贾维斯 JARVIS — 策略自动进化循环（AI 生成 → 回测 → AI 复盘 → 改进 → 再回测）。

面向完全不懂代码的用户：给一句大白话想法，贾维斯自动迭代 N 轮找更好的策略。

与既有模块的分工：
  - jarvis_strategy_gen：单次「听用户的想法」生成（本模块第 1 轮复用它）
  - jarvis_scalper_evolve：无用户想法的自由探索进化（名人堂/墓地体系）
  - 本模块：以用户想法为锚点的定向进化——每轮把回测指标喂回 LLM 做复盘改进，
    快照落盘可断点续跑，结果由用户决定是否存入名人堂

进化循环（每轮）：
  1. 第 1 轮：jarvis_strategy_gen 按用户想法生成初始策略
     后续轮：LLM 复盘（历史轮次指标表 + 当前最优规则 JSON）→ 输出改进规则
  2. 校验：因子合法性/参数夹回（strategy_gen 同款）+ codegen 沙盒安全校验
  3. QD 网关回测 → 标准化指标
  4. 评分（fitness）；连续 2 轮无改善提前停止

防过拟合护栏：
  - 复盘 prompt 固定注入「别只堆参数迷信单区间」提醒
  - 成交数 < MIN_TRADES 的策略视为无效样本（不参与最优评选）
  - 每轮快照（规则 JSON + 指标）写 ~/.vibe-trading/strategy_evolve_llm/<run_id>.json

用法（CLI）：
  python jarvis_strategy_evolve_llm.py run --desc "跌得恐慌就买，涨回来就卖" --rounds 3
  python jarvis_strategy_evolve_llm.py show --run-id <run_id>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import uuid
from typing import Any

import jarvis_llm_config as llm
import jarvis_scalper_backtest as jbt
import jarvis_scalper_codegen as codegen
import jarvis_scalper_features as sf
import jarvis_strategy_gen as jsg

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
RUNS_DIR = os.path.join(CONFIG_DIR, "strategy_evolve_llm")
LOG_PATH = os.path.join(CONFIG_DIR, "jarvis_strategy_evolve_llm.log")

MAX_ROUNDS = 10          # 轮数硬上限（控制 token 成本）
DEFAULT_ROUNDS = 5
NO_IMPROVE_STOP = 2      # 连续 N 轮无改善提前停止
MIN_TRADES = 5           # 成交数低于此值视为统计无效（防过拟合）
BACKTEST_TIMEOUT_S = 300


def _log(msg: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [EVOLVE-LLM] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ═══════════════════════════ 评分与指标摘要 ═══════════════════════════

def fitness(metrics: dict[str, Any]) -> float:
    """综合评分：收益为主，回撤惩罚，盈亏比/胜率加成；无效样本给极低分。

    评分只用于轮间比较（同区间同参数），不代表实盘期望。
    """
    if metrics.get("status") != "succeeded":
        return -1e9
    trades = int(metrics.get("total_trades") or 0)
    if trades < MIN_TRADES:
        return -1e6 + trades  # 交易太少：无效但保留排序信息
    ret = float(metrics.get("total_return_pct") or 0)
    dd = abs(float(metrics.get("max_drawdown_pct") or 0))
    pf = float(metrics.get("profit_factor") or 0)
    wr = float(metrics.get("win_rate") or 0)
    return ret - 0.5 * dd + 2.0 * min(pf, 5.0) + 0.1 * wr


def _metrics_brief(metrics: dict[str, Any]) -> dict[str, Any]:
    """只保留喂给 LLM / 存快照的关键指标（绝不带全量成交明细）。"""
    return {
        "status": metrics.get("status"),
        "total_return_pct": round(float(metrics.get("total_return_pct") or 0), 2),
        "win_rate": round(float(metrics.get("win_rate") or 0), 1),
        "profit_factor": round(float(metrics.get("profit_factor") or 0), 2),
        "max_drawdown_pct": round(float(metrics.get("max_drawdown_pct") or 0), 2),
        "sharpe_ratio": round(float(metrics.get("sharpe_ratio") or 0), 2),
        "total_trades": int(metrics.get("total_trades") or 0),
        "avg_trade_pnl": round(float(metrics.get("avg_trade_pnl") or 0), 4),
        "error": str(metrics.get("error") or "")[:200] or None,
    }


# ═══════════════════════════ 复盘改进 prompt ═══════════════════════════

REVIEW_SYSTEM = """你是加密货币量化策略研究员，正在做「回测复盘 → 定向改进」的迭代。
铁律：
1. 只能从因子菜单选因子（feature_id 引用，禁止自造）；参数必须落在 param_ranges 内
2. 改进要针对上一轮暴露的具体问题（回撤大→收紧止损或加过滤；交易太少→放宽条件或改 OR；胜率低→换/加确认因子）
3. 防过拟合：不要只微调参数去迎合单一回测区间；优先调整因子组合与出场结构这类"逻辑层"改动；参数保持整数/一位小数的自然值
4. 必须尊重用户的原始想法，不能改成完全无关的策略
5. 严格输出标准 JSON（与输入的规则同构，含 name/entry/exit/params/direction/reasoning/explain），不要输出其他文字"""


def _build_review_prompt(
    description: str,
    history: list[dict[str, Any]],
    best_rule: dict[str, Any],
    symbol: str,
    timeframe: str,
) -> str:
    """复盘 prompt：用户想法 + 历史轮次指标表 + 当前最优规则。控制长度：
    不喂成交明细、不喂策略代码，只喂规则 JSON 与关键指标。"""
    lines = []
    for h in history:
        m = h.get("metrics") or {}
        mark = " ←当前最优" if h.get("is_best") else ""
        if m.get("status") == "succeeded":
            lines.append(
                f"第{h['round']}轮 {h.get('name','?')}: 收益{m.get('total_return_pct')}% "
                f"胜率{m.get('win_rate')}% 盈亏比{m.get('profit_factor')} "
                f"回撤{m.get('max_drawdown_pct')}% 交易{m.get('total_trades')}笔"
                f" 评分{h.get('fitness')}{mark}"
            )
        else:
            lines.append(f"第{h['round']}轮 {h.get('name','?')}: 失败({m.get('error') or m.get('status')}){mark}")
    history_table = "\n".join(lines) or "（无）"

    rule_json = json.dumps(
        {k: best_rule.get(k) for k in ("name", "entry", "exit", "params", "direction") if k in best_rule},
        ensure_ascii=False,
    )
    feature_menu = sf.get_menu_json()

    return f"""用户的原始想法（{symbol} {timeframe}）：
「{description}」

## 历史轮次战绩
{history_table}

## 当前最优策略规则
```json
{rule_json}
```

## 可用因子菜单（只能从这里选）
{feature_menu}

## 任务
基于历史战绩针对性改进当前最优策略，输出改进后的完整规则 JSON。
要求：
- name 用新名字（英文小写下划线，体现改动点，如原名加 _v2）
- reasoning 里先一句话点明「上一轮的主要问题 + 本轮改动如何应对」
- explain 用大白话（100 字内）说明这个策略什么时候买卖
- 防过拟合提醒：如果历史几轮都是小改参数没有本质提升，就换思路（换因子/换逻辑/换出场结构），不要继续堆参数
- 只输出 JSON"""


def _improve_rule(
    description: str,
    history: list[dict[str, Any]],
    best_rule: dict[str, Any],
    symbol: str,
    timeframe: str,
) -> dict[str, Any]:
    """LLM 复盘改进：返回过完校验的新规则。抛异常表示本轮生成失败。"""
    prompt = _build_review_prompt(description, history, best_rule, symbol, timeframe)
    response = llm.call_llm(
        prompt, system=REVIEW_SYSTEM, temperature=0.5, max_tokens=1600, timeout=120,
        module="strategy_evolve",
    )
    rule = jsg._parse_llm_json(response)  # noqa: SLF001 — 同仓库模块复用解析器
    if not rule:
        raise ValueError("复盘响应无法解析为规则 JSON")
    rule, issues = jsg._validate_and_fix_rule(rule)  # noqa: SLF001 — 复用同款校验
    if issues:
        _log(f"规则自动修复: {issues}")
    return rule


# ═══════════════════════════ 快照持久化（断点续跑） ═══════════════════════════

def _run_path(run_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", run_id)[:64]
    return os.path.join(RUNS_DIR, f"{safe}.json")


def _save_run(run: dict[str, Any]) -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    with open(_run_path(run["run_id"]), "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=2)


def load_run(run_id: str) -> dict[str, Any] | None:
    path = _run_path(run_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    """历史 run 摘要列表（倒序）。"""
    if not os.path.isdir(RUNS_DIR):
        return []
    out = []
    for name in os.listdir(RUNS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(RUNS_DIR, name), encoding="utf-8") as f:
                r = json.load(f)
            out.append({
                "run_id": r.get("run_id"),
                "description": str(r.get("description", ""))[:80],
                "symbol": r.get("symbol"),
                "timeframe": r.get("timeframe"),
                "status": r.get("status"),
                "rounds_done": len(r.get("history", [])),
                "rounds_planned": r.get("rounds"),
                "best_fitness": r.get("best", {}).get("fitness"),
                "updated_at": r.get("updated_at"),
            })
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return out[:max(1, min(int(limit), 100))]


# ═══════════════════════════ 进化主循环 ═══════════════════════════

def evolve_run(
    description: str,
    rounds: int = DEFAULT_ROUNDS,
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    start_date: str = "2025-07-01",
    end_date: str = "2026-06-01",
    initial_capital: float = 10000,
    run_id: str | None = None,
    stop_event: threading.Event | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """执行/续跑一次进化。同步阻塞（调用方决定放后台线程还是 CLI 直跑）。

    Args:
        run_id: 传已有 run_id 则断点续跑（沿用其参数与历史，继续补轮次）
        stop_event: 置位后当前轮结束即停
        on_progress: 每轮结束回调 fn(run_dict)（dashboard 用于推日志）

    Returns:
        run 字典（status: succeeded/stopped/failed + history + best + top3）
    """
    rounds = max(1, min(int(rounds), MAX_ROUNDS))

    # ── 载入或新建 run ──
    run: dict[str, Any] | None = None
    if run_id:
        run = load_run(run_id)
    if run:
        description = run.get("description") or description
        symbol = run.get("symbol") or symbol
        timeframe = run.get("timeframe") or timeframe
        start_date = run.get("start_date") or start_date
        end_date = run.get("end_date") or end_date
        initial_capital = float(run.get("initial_capital") or initial_capital)
        rounds = max(len(run.get("history", [])), min(int(run.get("rounds") or rounds), MAX_ROUNDS))
        _log(f"断点续跑 run={run['run_id']}，已有 {len(run.get('history', []))} 轮")
    else:
        run = {
            "run_id": run_id or f"ev_{time.strftime('%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}",
            "description": (description or "").strip(),
            "symbol": symbol,
            "timeframe": timeframe,
            "start_date": start_date,
            "end_date": end_date,
            "initial_capital": initial_capital,
            "rounds": rounds,
            "status": "running",
            "history": [],
            "best": None,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    if not run["description"]:
        run["status"] = "failed"
        run["error"] = "请先用一句话描述策略想法"
        return run

    run["rounds"] = rounds
    run["status"] = "running"
    run["error"] = None
    history: list[dict[str, Any]] = run["history"]

    def _touch(status: str | None = None) -> None:
        if status:
            run["status"] = status
        run["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_run(run)
        if on_progress:
            try:
                on_progress(run)
            except Exception:  # noqa: BLE001
                pass

    def _best_entry() -> dict[str, Any] | None:
        valid = [h for h in history if h.get("fitness") is not None]
        return max(valid, key=lambda h: h["fitness"]) if valid else None

    no_improve_streak = 0
    _touch()

    while len(history) < rounds:
        rnd = len(history) + 1
        if stop_event is not None and stop_event.is_set():
            _log(f"收到停止信号，第 {rnd} 轮不再执行")
            _touch("stopped")
            return run

        _log(f"── 第 {rnd}/{rounds} 轮 ──")
        prev_best = _best_entry()

        # 1. 生成 / 改进规则
        try:
            if prev_best is None:
                gen = jsg.generate_from_description(description, symbol=symbol, timeframe=timeframe)
                if not gen.get("ok"):
                    raise ValueError(gen.get("error", "初始策略生成失败"))
                rule = gen["rule"]
                code = gen["code"]
                explain = gen.get("explain", "")
            else:
                rule = _improve_rule(description, history, prev_best["rule"], symbol, timeframe)
                built = codegen.rule_to_code(rule)
                if not built["valid"]:
                    raise ValueError(f"改进代码未过安全校验: {built['errors']}")
                code = built["code"]
                explain = str(rule.get("explain", "") or "")
        except Exception as e:  # noqa: BLE001
            _log(f"第 {rnd} 轮生成失败: {e}")
            history.append({
                "round": rnd,
                "name": f"gen_failed_r{rnd}",
                "rule": None,
                "explain": "",
                "metrics": {"status": "gen_failed", "error": str(e)[:200]},
                "fitness": None,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            no_improve_streak += 1
            _touch()
            if no_improve_streak >= NO_IMPROVE_STOP:
                _log(f"连续 {no_improve_streak} 轮无改善（含失败轮），提前停止")
                break
            continue

        name = str(rule.get("name") or f"strategy_r{rnd}")
        _log(f"策略: {name} | 因子: {rule.get('entry', {}).get('conditions')}")

        # 2. 回测
        try:
            bt = jbt.run_backtest(
                code=code,
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
            )
        except Exception as e:  # noqa: BLE001
            bt = {"status": "backtest_error", "error": str(e)[:200]}

        brief = _metrics_brief(bt)
        score = fitness(bt) if brief["status"] == "succeeded" else None
        _log(
            f"回测: {brief['status']} 收益{brief['total_return_pct']}% "
            f"胜率{brief['win_rate']}% 回撤{brief['max_drawdown_pct']}% "
            f"交易{brief['total_trades']}笔 评分{score if score is None else round(score, 2)}"
        )

        # 3. 记录本轮快照（含 code：前端一键存名人堂直接可用）
        entry = {
            "round": rnd,
            "name": name,
            "rule": rule,
            "code": code,
            "explain": explain,
            "metrics": brief,
            "fitness": None if score is None else round(score, 3),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        history.append(entry)

        # 4. 改善判定（fitness 无效的轮当作无改善）
        new_best = _best_entry()
        improved = (
            score is not None
            and (prev_best is None or (new_best is not None and new_best is entry))
        )
        no_improve_streak = 0 if improved else no_improve_streak + 1
        _touch()

        if no_improve_streak >= NO_IMPROVE_STOP and len(history) < rounds:
            _log(f"连续 {no_improve_streak} 轮无改善，提前停止（防止无谓烧 token）")
            break

    # ── 汇总 ──
    best = _best_entry()
    # Top3 与"最优"只收统计有效的轮次（成交数达标；-1e5 以下是护栏标记的无效分）
    valid_sorted = sorted(
        [h for h in history if h.get("fitness") is not None and h["fitness"] > -1e5],
        key=lambda h: h["fitness"],
        reverse=True,
    )
    best = valid_sorted[0] if valid_sorted else None
    run["best"] = None if best is None else {
        "round": best["round"],
        "name": best["name"],
        "rule": best["rule"],
        "code": best.get("code", ""),
        "explain": best.get("explain", ""),
        "metrics": best["metrics"],
        "fitness": best["fitness"],
    }
    run["top3"] = [
        {
            "round": h["round"],
            "name": h["name"],
            "rule": h["rule"],
            "code": h.get("code", ""),
            "explain": h.get("explain", ""),
            "metrics": h["metrics"],
            "fitness": h["fitness"],
        }
        for h in valid_sorted[:3]
    ]
    _touch("succeeded" if best is not None else "failed")
    if best is None:
        run["error"] = "所有轮次均未产出有效回测结果"
        _save_run(run)
    _log(
        f"进化结束: {len(history)} 轮，最优 {best['name'] if best else '无'} "
        f"(评分 {best['fitness'] if best else '—'})"
    )
    return run


# ═══════════════════════════ FastAPI 路由（dashboard include 用） ═══════════════════════════

try:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    _HAS_FASTAPI = True
except ImportError:  # CLI 场景无需 fastapi
    _HAS_FASTAPI = False

if _HAS_FASTAPI:
    router = APIRouter(prefix="/api/strategy-evolve", tags=["strategy-evolve"])

    _EV_LOCK = threading.Lock()
    _EV_THREAD: threading.Thread | None = None
    _EV_STOP = threading.Event()
    _EV_STATE: dict[str, Any] = {
        "running": False,
        "run_id": None,
        "started_at": 0.0,
        "finished_at": 0.0,
        "error": None,
    }

    class EvolveStartReq(BaseModel):
        description: str = ""
        rounds: int = DEFAULT_ROUNDS
        symbol: str = "BTC/USDT"
        timeframe: str = "15m"
        start_date: str = "2025-07-01"
        end_date: str = "2026-06-01"
        initial_capital: float = 10000
        resume_run_id: str = ""   # 传入则断点续跑

    def _evolve_worker(req: EvolveStartReq, run_id: str) -> None:
        try:
            evolve_run(
                description=req.description,
                rounds=req.rounds,
                symbol=req.symbol,
                timeframe=req.timeframe,
                start_date=req.start_date,
                end_date=req.end_date,
                initial_capital=req.initial_capital,
                run_id=run_id,
                stop_event=_EV_STOP,
            )
        except Exception as e:  # noqa: BLE001
            _log(f"进化后台线程异常: {e}")
            with _EV_LOCK:
                _EV_STATE["error"] = str(e)[:300]
            run = load_run(run_id)
            if run is not None:
                run["status"] = "failed"
                run["error"] = str(e)[:300]
                _save_run(run)
        finally:
            with _EV_LOCK:
                _EV_STATE["running"] = False
                _EV_STATE["finished_at"] = time.time()

    @router.post("/start")
    def api_evolve_start(req: EvolveStartReq):
        """启动（或续跑）自动进化，后台线程执行，轮询 /status /result 取进展。"""
        global _EV_THREAD
        if not llm.get_llm_config():
            return JSONResponse(
                {"ok": False, "error": "未配置大模型，请先到「设置 → 大模型 (LLM)」填入 API Key"},
                status_code=400,
            )
        description = (req.description or "").strip()
        if not description and not req.resume_run_id:
            return JSONResponse({"ok": False, "error": "请先描述你的策略想法"}, status_code=400)
        with _EV_LOCK:
            if _EV_STATE["running"]:
                return JSONResponse({"ok": False, "error": "已有进化任务在运行中", "run_id": _EV_STATE["run_id"]})
            run_id = req.resume_run_id.strip() or f"ev_{time.strftime('%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            if req.resume_run_id and not load_run(run_id):
                return JSONResponse({"ok": False, "error": f"找不到要续跑的 run: {run_id}"}, status_code=404)
            _EV_STOP.clear()
            _EV_STATE.update({
                "running": True,
                "run_id": run_id,
                "started_at": time.time(),
                "finished_at": 0.0,
                "error": None,
            })
        _EV_THREAD = threading.Thread(target=_evolve_worker, args=(req, run_id), daemon=True)
        _EV_THREAD.start()
        _log(f"▶ 自动进化启动 run={run_id} rounds≤{min(req.rounds, MAX_ROUNDS)} desc={description[:50]}")
        return JSONResponse({"ok": True, "run_id": run_id})

    @router.get("/status")
    def api_evolve_status():
        """轮询进度：运行态 + 当前 run 的每轮指标（供进化曲线实时刷新）。"""
        with _EV_LOCK:
            st = dict(_EV_STATE)
        run = load_run(st["run_id"]) if st["run_id"] else None
        elapsed = 0.0
        if st["started_at"]:
            elapsed = max(0.0, (st["finished_at"] or time.time()) - st["started_at"])
        return JSONResponse({
            "running": st["running"],
            "run_id": st["run_id"],
            "elapsed_seconds": round(elapsed, 1),
            "error": st["error"] or (run or {}).get("error"),
            "run": None if run is None else {
                "status": run.get("status"),
                "rounds_planned": run.get("rounds"),
                "rounds_done": len(run.get("history", [])),
                "description": run.get("description"),
                "symbol": run.get("symbol"),
                "timeframe": run.get("timeframe"),
                "history": [
                    {k: h.get(k) for k in ("round", "name", "metrics", "fitness", "ts")}
                    for h in run.get("history", [])
                ],
                "best": run.get("best"),
            },
        })

    @router.get("/result")
    def api_evolve_result(run_id: str = ""):
        """取完整结果（含 Top3 与规则 JSON）；不传 run_id 用最近一次。"""
        rid = run_id.strip() or _EV_STATE.get("run_id") or ""
        if not rid:
            return JSONResponse({"ok": False, "error": "还没有进化记录"}, status_code=404)
        run = load_run(rid)
        if run is None:
            return JSONResponse({"ok": False, "error": f"找不到 run: {rid}"}, status_code=404)
        return JSONResponse({"ok": True, "run": run})

    @router.get("/runs")
    def api_evolve_runs(limit: int = 20):
        """历史进化任务列表（断点续跑入口）。"""
        return JSONResponse({"runs": list_runs(limit)})

    @router.post("/stop")
    def api_evolve_stop():
        """请求停止：当前轮跑完后停（保留已完成轮次快照，可续跑）。"""
        with _EV_LOCK:
            if not _EV_STATE["running"]:
                return JSONResponse({"ok": False, "error": "当前没有运行中的进化"})
        _EV_STOP.set()
        _log("■ 已请求停止自动进化（当前轮结束后生效）")
        return JSONResponse({"ok": True})


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="策略自动进化循环（LLM 复盘驱动）")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="执行进化")
    p_run.add_argument("--desc", required=True, help="策略想法（自然语言）")
    p_run.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    p_run.add_argument("--symbol", default="BTC/USDT")
    p_run.add_argument("--timeframe", default="15m")
    p_run.add_argument("--start", default="2025-07-01")
    p_run.add_argument("--end", default="2026-06-01")
    p_run.add_argument("--capital", type=float, default=10000)
    p_run.add_argument("--resume", default="", help="断点续跑的 run_id")

    p_show = sub.add_parser("show", help="查看某次进化结果")
    p_show.add_argument("--run-id", required=True)

    sub.add_parser("list", help="列出历史进化任务")

    args = parser.parse_args()

    if args.cmd == "run":
        run = evolve_run(
            description=args.desc,
            rounds=args.rounds,
            symbol=args.symbol,
            timeframe=args.timeframe,
            start_date=args.start,
            end_date=args.end,
            initial_capital=args.capital,
            run_id=args.resume or None,
        )
        print(json.dumps(
            {k: run.get(k) for k in ("run_id", "status", "best", "top3", "error")},
            ensure_ascii=False, indent=2,
        ))
    elif args.cmd == "show":
        run = load_run(args.run_id)
        print(json.dumps(run or {"error": "not found"}, ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        print(json.dumps(list_runs(), ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
