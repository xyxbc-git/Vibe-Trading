#!/usr/bin/env python3
"""贾维斯 JARVIS — LLM 策略进化引擎 v2（S-05 核心）。

v2 升级：
  - 从「单策略」进化升级为「策略组合」进化
  - 进化数据源增加实盘表现数据（最近 N 笔交易）
  - 教训系统升级为结构化教训（关联行情类型 + 市场条件）
  - 支持自动触发再进化（性能衰退时）

核心机制：
  1. LLM 根据因子菜单 + 结构化教训 + 实盘数据生成策略组合
  2. 策略组合 = {震荡策略, 趋势策略, 突破策略} 三合一
  3. 查重门禁：与墓地中策略相似度 > 阈值则重新生成
  4. 代码生成器拼装 QD 策略代码
  5. QD API 回测（模拟行情切换验证组合表现）
  6. 分析器归类结果
  7. 达标 → 名人堂 + OOS 验证；未达标 → 墓地 + 结构化教训

数据存储：
  - ~/.vibe-trading/scalper_graveyard.json     — 失败策略墓地
  - ~/.vibe-trading/scalper_hall_of_fame.json   — 达标策略名人堂
  - ~/.vibe-trading/structured_lessons.json     — 结构化教训库
  - ~/.vibe-trading/combo_hall_of_fame.json     — 策略组合名人堂（v2 新增）

用法：
  python jarvis_scalper_evolve.py evolve --symbol BTCUSDT --rounds 10
  python jarvis_scalper_evolve.py evolve-combo --symbol BTCUSDT --rounds 5
  python jarvis_scalper_evolve.py hall-of-fame
  python jarvis_scalper_evolve.py graveyard
  python jarvis_scalper_evolve.py lessons
  python jarvis_scalper_evolve.py status
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict, field
from typing import Any

import jarvis_scalper_features as sf
import jarvis_scalper_codegen as codegen
import jarvis_scalper_backtest as bt
import jarvis_scalper_analyzer as analyzer

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
GRAVEYARD_PATH = os.path.join(CONFIG_DIR, "scalper_graveyard.json")
HOF_PATH = os.path.join(CONFIG_DIR, "scalper_hall_of_fame.json")
COMBO_HOF_PATH = os.path.join(CONFIG_DIR, "combo_hall_of_fame.json")
LESSONS_PATH = os.path.join(CONFIG_DIR, "structured_lessons.json")
LOG_PATH = os.path.join(CONFIG_DIR, "jarvis_scalper_evolve.log")

DEFAULT_CRITERIA = {
    "min_win_rate": 52.0,
    "min_profit_factor": 1.2,
    "max_drawdown_pct": 15.0,
    "min_total_return_pct": 0.0,
    "min_trades": 20,
}

REGIMES = ("ranging", "trending", "breakout")


def _log(msg: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [EVOLVE] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ═══════════════════════════ 结构化教训系统 v2 ═══════════════════════════

@dataclass
class StructuredLesson:
    """结构化教训条目。"""
    lesson_id: str
    strategy: str
    failure_regime: str            # trending / ranging / breakout
    failure_condition: dict        # {"adx": ">35", "trend": "bearish", ...}
    failure_type: str              # 逆势追涨 / 过度加仓 / 假突破 / ...
    root_cause: str                # 根因分析
    fix_applied: str               # 应对措施
    fix_effective: bool | None     # 修复是否有效（None = 未验证）
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def load_lessons() -> list[dict]:
    return _load_json(LESSONS_PATH)


def save_lessons(data: list[dict]) -> None:
    _save_json(LESSONS_PATH, data)


def add_lesson(lesson: StructuredLesson) -> None:
    """添加结构化教训。"""
    lessons = load_lessons()
    lesson.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    lesson.lesson_id = f"L{len(lessons)+1:03d}"
    lessons.append(lesson.to_dict())
    save_lessons(lessons)
    _log(f"教训入库: {lesson.lesson_id} [{lesson.failure_regime}] {lesson.failure_type}")


def _derive_lesson(
    strategy_name: str,
    rule: dict,
    report: dict,
    regime_hint: str = "",
) -> StructuredLesson:
    """从回测报告中提炼结构化教训。"""
    failure_type = report.get("failure_type", "未达标")
    hints = report.get("improvement_hints", [])
    loss_breakdown = report.get("loss_breakdown", {})

    top_loss_type = ""
    if loss_breakdown:
        sorted_losses = sorted(loss_breakdown.items(), key=lambda x: -x[1].get("count", 0))
        if sorted_losses:
            top_loss_type = sorted_losses[0][1].get("label", "")

    conditions = rule.get("entry", {}).get("conditions", [])
    params = rule.get("params", {})

    condition_dict: dict[str, str] = {}
    if regime_hint:
        condition_dict["target_regime"] = regime_hint
    for cond in conditions[:3]:
        condition_dict[cond] = "used"
    for k, v in list(params.items())[:3]:
        condition_dict[k] = str(v)

    root_cause = top_loss_type or failure_type
    if hints:
        root_cause += f"；{hints[0]}"

    fix = "避免相同因子组合 + 参数"
    if "追" in root_cause or "逆势" in root_cause:
        fix = "增加趋势过滤层，ADX>30 时禁止逆势入场"
    elif "加仓" in root_cause or "马丁" in root_cause:
        fix = "限制马丁加仓次数，强趋势时关闭加仓"
    elif "假突破" in root_cause or "突破" in root_cause:
        fix = "提高成交量确认阈值，增加突破后回测验证"
    elif "回撤" in root_cause:
        fix = "缩小止损倍数，增加移动止损"

    return StructuredLesson(
        lesson_id="",
        strategy=strategy_name,
        failure_regime=regime_hint or "unknown",
        failure_condition=condition_dict,
        failure_type=failure_type,
        root_cause=root_cause,
        fix_applied=fix,
        fix_effective=None,
    )


# ═══════════════════════════ 策略组合（v2 新增） ═══════════════════════════

@dataclass
class StrategyCombo:
    """策略组合：每种行情一套策略配置。"""
    name: str
    version: int
    ranging_rule: dict     # 震荡行情策略规则
    trending_rule: dict    # 趋势行情策略规则
    breakout_rule: dict    # 突破行情策略规则
    composite_config: dict  # 复合策略配置（AttackLayer/RescueLayer/ProtectLayer 参数）
    reasoning: str
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def load_combo_hof() -> list[dict]:
    return _load_json(COMBO_HOF_PATH)


def save_combo_hof(data: list[dict]) -> None:
    _save_json(COMBO_HOF_PATH, data)


def add_combo_to_hof(combo: StrategyCombo, results: dict) -> None:
    """将达标策略组合加入组合名人堂。"""
    hof = load_combo_hof()
    combo.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = combo.to_dict()
    entry["results"] = results
    hof.append(entry)
    save_combo_hof(hof)
    _log(f"策略组合入名人堂: {combo.name}")


def get_best_combo() -> dict | None:
    """获取最佳策略组合。"""
    hof = load_combo_hof()
    if not hof:
        return None
    return max(
        hof,
        key=lambda c: c.get("results", {}).get("overall", {}).get("sharpe_ratio", -999),
    )


# ═══════════════════════════ 实盘数据获取 ═══════════════════════════

def _get_live_performance_summary() -> dict:
    """从表现追踪器获取最近实盘数据摘要。"""
    try:
        from jarvis_performance_tracker import PerformanceTracker
        tracker = PerformanceTracker()
        if not tracker._history:
            return {"available": False, "reason": "无实盘交易记录"}

        report = tracker.evaluate(recent_n=50)
        regime_summary = {}
        for regime, stats in report.regime_breakdown.items():
            regime_summary[regime] = {
                "trades": stats["count"],
                "win_rate": stats["win_rate"],
                "pnl": stats["total_pnl"],
            }

        weakest_regime = None
        weakest_wr = 100.0
        for regime, stats in regime_summary.items():
            if stats["trades"] >= 5 and stats["win_rate"] < weakest_wr:
                weakest_wr = stats["win_rate"]
                weakest_regime = regime

        return {
            "available": True,
            "total_trades": report.total_trades,
            "win_rate": report.win_rate,
            "profit_factor": report.profit_factor,
            "max_consecutive_loss": report.max_consecutive_loss,
            "sharpe": report.sharpe_estimate,
            "regime_breakdown": regime_summary,
            "weakest_regime": weakest_regime,
            "is_decaying": report.is_decaying,
            "decay_signals": report.decay_signals,
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


# ═══════════════════════════ LLM 调用 ═══════════════════════════

def _llm_config() -> dict[str, str] | None:
    """读取 LLM 配置（与 jarvis_dashboard 一致）。"""
    ds_key = os.environ.get("DEEPSEEK_API_KEY")
    key = os.environ.get("JARVIS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ds_key
    if not key:
        return None
    base = os.environ.get("JARVIS_LLM_BASE_URL")
    if not base:
        base = "https://api.deepseek.com" if ds_key else "https://api.openai.com/v1"
    base = base.rstrip("/")
    is_deepseek = "deepseek" in base.lower()
    model = os.environ.get("JARVIS_LLM_MODEL") or ("deepseek-chat" if is_deepseek else "gpt-4o-mini")
    return {"key": key, "base": base, "model": model}


def _call_llm(prompt: str, system: str = "", temperature: float = 0.7) -> str:
    """调用 LLM，返回文本响应。"""
    cfg = _llm_config()
    if not cfg:
        raise RuntimeError(
            "未配置 LLM。请设置环境变量：\n"
            "  export DEEPSEEK_API_KEY=your_key\n"
            "或\n"
            "  export JARVIS_LLM_API_KEY=your_key\n"
            "  export JARVIS_LLM_BASE_URL=https://api.xxx.com"
        )

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 2000,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        _log(f"LLM 调用失败: {e}")
        raise


# ═══════════════════════════ 墓地/名人堂 ═══════════════════════════

def _load_json(path: str) -> list[dict]:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_json(path: str, data: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_graveyard() -> list[dict]:
    return _load_json(GRAVEYARD_PATH)


def save_graveyard(data: list[dict]) -> None:
    _save_json(GRAVEYARD_PATH, data)


def load_hall_of_fame() -> list[dict]:
    return _load_json(HOF_PATH)


def save_hall_of_fame(data: list[dict]) -> None:
    _save_json(HOF_PATH, data)


def add_to_graveyard(entry: dict) -> None:
    gy = load_graveyard()
    entry["buried_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    gy.append(entry)
    save_graveyard(gy)
    _log(f"策略入墓: {entry.get('name', '?')}")


def add_to_hall_of_fame(entry: dict) -> None:
    hof = load_hall_of_fame()
    entry["inducted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    hof.append(entry)
    save_hall_of_fame(hof)
    _log(f"策略入名人堂: {entry.get('name', '?')}")


# ═══════════════════════════ 查重门禁 ═══════════════════════════

def _rule_signature(rule: dict) -> set[str]:
    """提取规则的特征签名用于相似度比对。"""
    conditions = rule.get("entry", {}).get("conditions", [])
    params = rule.get("params", {})
    sig = set(conditions)
    for k, v in params.items():
        sig.add(f"{k}={v}")
    return sig


def check_similarity(rule: dict, graveyard: list[dict], threshold: float = 0.8) -> tuple[bool, str]:
    """检查新规则是否与墓地中的策略太相似。

    Returns:
        (is_too_similar, most_similar_name)
    """
    new_sig = _rule_signature(rule)
    if not new_sig:
        return False, ""

    max_sim = 0.0
    most_similar = ""

    for entry in graveyard:
        old_rule = entry.get("rule", {})
        old_sig = _rule_signature(old_rule)
        if not old_sig:
            continue
        intersection = new_sig & old_sig
        union = new_sig | old_sig
        sim = len(intersection) / len(union) if union else 0
        if sim > max_sim:
            max_sim = sim
            most_similar = entry.get("name", "?")

    return max_sim >= threshold, most_similar


# ═══════════════════════════ LLM 策略生成 ═══════════════════════════

SYSTEM_PROMPT = """你是一个加密货币短线策略研究员，专注于 15 分钟级别的量化交易。
你的任务是根据可用因子菜单和历史教训，设计出有效的交易策略规则。
你必须避免重复过去失败的错误。

重要约束：
1. 只使用因子菜单中列出的因子（用 feature_id 引用）
2. 参数必须在因子定义的合法范围内
3. 不得与墓地中的策略组合+参数相同
4. 必须输出标准 JSON 格式"""

SYSTEM_PROMPT_V2 = """你是一个加密货币复合策略架构师，专注于 15 分钟级别的量化交易。
你的任务是为不同行情类型（震荡/趋势/突破）分别设计最优策略，组成攻守兼备的策略组合。

核心理念：
- 马丁/网格策略适合震荡行情（高频小利），但在趋势行情中致命
- 趋势跟踪策略适合单边行情（追涨杀跌），但在震荡中频繁止损
- 突破策略抓爆发行情（放量突破），但假突破会造成亏损
- 最优解：识别当前行情 → 自动切换对应策略

你必须基于结构化教训避免重蹈覆辙，并参考实盘表现数据找到薄弱环节。

重要约束：
1. 只使用因子菜单中列出的因子
2. 参数必须在合法范围内
3. 每种行情的策略必须有差异化设计
4. 输出标准 JSON"""


def _build_evolve_prompt(
    round_num: int,
    last_analysis: dict | None,
    graveyard: list[dict],
    hof: list[dict],
) -> str:
    """构建单策略进化 prompt（保留 v1 兼容）。"""
    feature_menu = sf.get_menu_json()

    graveyard_summary = "无（首轮）"
    if graveyard:
        summaries = []
        for g in graveyard[-10:]:
            summaries.append(
                f"- {g['name']}: 因子={g.get('rule',{}).get('entry',{}).get('conditions',[])} "
                f"| 结果: 收益={g.get('result',{}).get('total_return_pct',0):.1f}% "
                f"胜率={g.get('result',{}).get('win_rate',0):.1f}% "
                f"| 失败原因: {g.get('failure_type','?')} "
                f"| 教训: {g.get('lesson','?')}"
            )
        graveyard_summary = "\n".join(summaries)

    hof_summary = "无（尚无达标策略）"
    if hof:
        summaries = []
        for h in hof:
            summaries.append(
                f"- {h['name']}: 因子={h.get('rule',{}).get('entry',{}).get('conditions',[])} "
                f"| 收益={h.get('result',{}).get('total_return_pct',0):.1f}% "
                f"胜率={h.get('result',{}).get('win_rate',0):.1f}% "
                f"夏普={h.get('result',{}).get('sharpe_ratio',0):.2f}"
            )
        hof_summary = "\n".join(summaries)

    last_round = "无（首轮）"
    if last_analysis:
        last_round = json.dumps(last_analysis, ensure_ascii=False, indent=2)

    return f"""这是第 {round_num} 轮策略进化。请生成一个新的 15 分钟短线策略规则。

## 可用因子菜单
{feature_menu}

## 上一轮分析结果
{last_round}

## 失败策略墓地（禁止重复这些组合）
{graveyard_summary}

## 达标策略参考
{hof_summary}

## 输出要求
请严格按以下 JSON 格式输出（不要添加任何其他文字，只输出 JSON）：

```json
{{
  "name": "策略英文名_v版本号",
  "entry": {{
    "conditions": ["因子ID1", "因子ID2"],
    "logic": "AND"
  }},
  "exit": {{
    "stop_loss_atr_mult": 1.5,
    "take_profit_atr_mult": 2.5,
    "time_stop_bars": 12
  }},
  "params": {{
    "因子ID1_参数名": 值,
    "因子ID2_参数名": 值
  }},
  "direction": "both",
  "reasoning": "选择这个组合的原因，以及它为什么不会重蹈覆辙"
}}
```

注意：
- conditions 中的因子 ID 必须从菜单中选择
- 建议选 2~4 个因子组合
- logic 可以是 "AND" 或 "OR"
- direction 可以是 "both"、"long" 或 "short"
- params 键名格式为 "因子ID_参数名"，如 "ema_cross_fast"
- reasoning 字段必须解释为什么这个组合有效且不会重复失败"""


def _build_combo_prompt(
    round_num: int,
    last_analysis: dict | None,
    graveyard: list[dict],
    hof: list[dict],
) -> str:
    """构建策略组合进化 prompt（v2 新增）。"""
    feature_menu = sf.get_menu_json()

    # 结构化教训
    lessons = load_lessons()
    lesson_text = "无教训记录"
    if lessons:
        lesson_lines = []
        for ls in lessons[-15:]:
            lesson_lines.append(
                f"- [{ls.get('lesson_id','?')}] 策略={ls.get('strategy','?')} "
                f"行情={ls.get('failure_regime','?')} "
                f"失败类型={ls.get('failure_type','?')} "
                f"根因={ls.get('root_cause','?')} "
                f"修复={ls.get('fix_applied','?')} "
                f"有效={'✓' if ls.get('fix_effective') else '✗' if ls.get('fix_effective') is False else '?'}"
            )
        lesson_text = "\n".join(lesson_lines)

    # 实盘数据
    live_data = _get_live_performance_summary()
    live_text = "无实盘数据（系统刚启动或无交易记录）"
    if live_data.get("available"):
        live_lines = [
            f"最近 {live_data['total_trades']} 笔交易:",
            f"  胜率: {live_data['win_rate']:.1f}%",
            f"  盈亏比: {live_data['profit_factor']:.2f}",
            f"  最大连亏: {live_data['max_consecutive_loss']}",
            f"  夏普: {live_data['sharpe']:.2f}",
        ]
        if live_data.get("regime_breakdown"):
            live_lines.append("  各行情表现:")
            for regime, stats in live_data["regime_breakdown"].items():
                live_lines.append(
                    f"    {regime}: {stats['trades']}笔 胜率{stats['win_rate']:.0f}% "
                    f"盈亏{stats['pnl']:+.2f}"
                )
        if live_data.get("weakest_regime"):
            live_lines.append(f"  ⚠ 最薄弱行情: {live_data['weakest_regime']}（优先改进）")
        if live_data.get("is_decaying"):
            live_lines.append(f"  🔴 策略衰退中: {', '.join(live_data.get('decay_signals', []))}")
        live_text = "\n".join(live_lines)

    # 墓地摘要
    graveyard_summary = "无"
    if graveyard:
        summaries = []
        for g in graveyard[-8:]:
            summaries.append(
                f"- {g['name']}: {g.get('failure_type','?')} | {g.get('lesson','?')}"
            )
        graveyard_summary = "\n".join(summaries)

    # 组合名人堂
    combo_hof = load_combo_hof()
    combo_hof_text = "无"
    if combo_hof:
        summaries = []
        for c in combo_hof[-3:]:
            r = c.get("results", {}).get("overall", {})
            summaries.append(
                f"- {c['name']}: 总收益{r.get('total_return_pct',0):.1f}% "
                f"胜率{r.get('win_rate',0):.1f}% 夏普{r.get('sharpe_ratio',0):.2f}"
            )
        combo_hof_text = "\n".join(summaries)

    last_round = "无（首轮）"
    if last_analysis:
        last_round = json.dumps(last_analysis, ensure_ascii=False, indent=2)

    return f"""这是第 {round_num} 轮「策略组合」进化。你需要为三种行情分别设计策略。

## 可用因子菜单
{feature_menu}

## 上一轮分析结果
{last_round}

## 结构化教训库（必须逐条避雷）
{lesson_text}

## 实盘表现数据（重点参考）
{live_text}

## 失败策略墓地
{graveyard_summary}

## 历史最佳策略组合
{combo_hof_text}

## 输出要求
请生成一个策略组合，包含三种行情各自的交易规则。
严格按以下 JSON 格式输出（只输出 JSON，不要其他文字）：

```json
{{
  "name": "combo_策略名_v版本号",
  "ranging": {{
    "conditions": ["因子ID1", "因子ID2"],
    "logic": "AND",
    "direction": "both",
    "params": {{"因子ID_参数名": 值}},
    "sl_atr_mult": 1.0,
    "tp_atr_mult": 1.5,
    "martin_max_adds": 3,
    "martin_add_threshold_atr": 1.0,
    "reasoning": "震荡行情策略理由"
  }},
  "trending": {{
    "conditions": ["因子ID3", "因子ID4"],
    "logic": "AND",
    "direction": "trend_follow",
    "params": {{"因子ID_参数名": 值}},
    "sl_atr_mult": 1.5,
    "tp_atr_mult": 3.0,
    "trailing": true,
    "trailing_step_atr": 0.5,
    "reasoning": "趋势行情策略理由"
  }},
  "breakout": {{
    "conditions": ["因子ID5", "因子ID6"],
    "logic": "AND",
    "direction": "breakout_follow",
    "params": {{"因子ID_参数名": 值}},
    "sl_atr_mult": 1.2,
    "tp_atr_mult": 2.0,
    "volume_confirm": true,
    "reasoning": "突破行情策略理由"
  }},
  "composite_config": {{
    "protect_strong_trend_adx": 35,
    "protect_suppress_martin_adx": 40,
    "rescue_hedge_threshold_pct": -3.0,
    "rescue_hedge_size_ratio": 0.5
  }},
  "reasoning": "整体组合设计理由 + 如何避免历史教训"
}}
```

注意：
- 三种行情的因子组合应有明显差异化
- 震荡策略适合均价止盈高频小利，可用马丁加仓
- 趋势策略应顺势而为，移动止盈追踪，禁止逆势
- 突破策略要求放量确认，快进快出
- composite_config 用于保命层和解困层参数
- 如有实盘数据中的薄弱行情，该行情的策略必须针对性优化"""


def _parse_llm_rule(response: str) -> dict[str, Any] | None:
    """从 LLM 响应中提取 JSON 规则。"""
    json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    json_match = re.search(r'\{[^{}]*"name"[^{}]*\}', response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        return None


def generate_strategy(
    round_num: int,
    last_analysis: dict | None = None,
    max_retries: int = 3,
    similarity_threshold: float = 0.8,
) -> dict[str, Any]:
    """用 LLM 生成一个新策略规则（v1 单策略模式）。

    内置查重门禁：与墓地相似度过高则重新生成。

    Returns:
        策略规则 JSON
    """
    graveyard = load_graveyard()
    hof = load_hall_of_fame()
    prompt = _build_evolve_prompt(round_num, last_analysis, graveyard, hof)

    for attempt in range(max_retries):
        _log(f"第 {round_num} 轮，第 {attempt+1} 次 LLM 生成...")
        response = _call_llm(prompt, system=SYSTEM_PROMPT)
        rule = _parse_llm_rule(response)

        if not rule:
            _log(f"LLM 响应解析失败，重试")
            continue

        conditions = rule.get("entry", {}).get("conditions", [])
        invalid = [c for c in conditions if not sf.get_feature(c)]
        if invalid:
            _log(f"无效因子 {invalid}，重试")
            continue

        too_similar, similar_to = check_similarity(rule, graveyard, similarity_threshold)
        if too_similar:
            _log(f"与墓地策略 '{similar_to}' 相似度过高，重试")
            continue

        _log(f"策略 '{rule.get('name', '?')}' 生成成功: {conditions}")
        return rule

    raise RuntimeError(f"LLM 连续 {max_retries} 次生成无效策略")


def _parse_combo_rule(response: str) -> dict[str, Any] | None:
    """从 LLM 响应中提取策略组合 JSON。"""
    json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if "ranging" in data and "trending" in data and "breakout" in data:
                return data
        except json.JSONDecodeError:
            pass

    json_match = re.search(r'\{[\s\S]*"ranging"[\s\S]*"trending"[\s\S]*"breakout"[\s\S]*\}', response)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    return _parse_llm_rule(response)


def _validate_combo(combo_data: dict) -> tuple[bool, str]:
    """校验策略组合结构的完整性。"""
    for regime in REGIMES:
        if regime not in combo_data:
            return False, f"缺少 {regime} 行情策略"
        rule = combo_data[regime]
        if not isinstance(rule, dict):
            return False, f"{regime} 策略不是字典"
        conditions = rule.get("conditions", [])
        if not conditions:
            return False, f"{regime} 策略没有因子条件"
        invalid = [c for c in conditions if not sf.get_feature(c)]
        if invalid:
            return False, f"{regime} 使用了无效因子: {invalid}"

    if "name" not in combo_data:
        return False, "缺少组合名称"

    return True, ""


def generate_strategy_combo(
    round_num: int,
    last_analysis: dict | None = None,
    max_retries: int = 3,
    similarity_threshold: float = 0.7,
) -> StrategyCombo:
    """用 LLM 生成策略组合（v2 核心）。

    Returns:
        StrategyCombo 对象
    """
    graveyard = load_graveyard()
    hof = load_hall_of_fame()
    prompt = _build_combo_prompt(round_num, last_analysis, graveyard, hof)

    for attempt in range(max_retries):
        _log(f"[组合进化] 第 {round_num} 轮，第 {attempt+1} 次 LLM 生成...")
        response = _call_llm(prompt, system=SYSTEM_PROMPT_V2, temperature=0.8)
        combo_data = _parse_combo_rule(response)

        if not combo_data:
            _log(f"LLM 响应解析失败，重试")
            continue

        valid, reason = _validate_combo(combo_data)
        if not valid:
            _log(f"组合校验失败: {reason}，重试")
            continue

        for regime in REGIMES:
            rule_wrapper = {"entry": {"conditions": combo_data[regime].get("conditions", [])},
                            "params": combo_data[regime].get("params", {})}
            too_similar, similar_to = check_similarity(rule_wrapper, graveyard, similarity_threshold)
            if too_similar:
                _log(f"{regime} 策略与墓地 '{similar_to}' 相似度过高，重试")
                valid = False
                break

        if not valid:
            continue

        name = combo_data.get("name", f"combo_r{round_num}_v1")
        combo = StrategyCombo(
            name=name,
            version=2,
            ranging_rule=combo_data["ranging"],
            trending_rule=combo_data["trending"],
            breakout_rule=combo_data["breakout"],
            composite_config=combo_data.get("composite_config", {}),
            reasoning=combo_data.get("reasoning", ""),
        )

        _log(f"策略组合 '{name}' 生成成功:")
        for regime in REGIMES:
            conds = combo_data[regime].get("conditions", [])
            _log(f"  {regime}: {conds}")
        return combo

    raise RuntimeError(f"LLM 连续 {max_retries} 次生成无效策略组合")


def apply_combo_to_composite_config(combo: StrategyCombo) -> dict:
    """将策略组合转换为 CompositeStrategy 可用的配置。"""
    return {
        "attack": {
            "martin_max_adds": combo.ranging_rule.get("martin_max_adds", 3),
            "martin_add_threshold_atr": combo.ranging_rule.get("martin_add_threshold_atr", 1.0),
            "grid_tp_atr": combo.ranging_rule.get("grid_tp_atr", 0.5),
        },
        "rescue": {
            "hedge_threshold_pct": combo.composite_config.get("rescue_hedge_threshold_pct", -3.0),
            "hedge_size_ratio": combo.composite_config.get("rescue_hedge_size_ratio", 0.5),
        },
        "protect": {
            "strong_trend_adx": combo.composite_config.get("protect_strong_trend_adx", 35),
            "suppress_martin_adx": combo.composite_config.get("protect_suppress_martin_adx", 40),
        },
        "regime_rules": {
            "ranging": combo.ranging_rule,
            "trending": combo.trending_rule,
            "breakout": combo.breakout_rule,
        },
    }


# ═══════════════════════════ 进化循环 ═══════════════════════════

def evolve(
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    max_rounds: int = 10,
    start_date: str = "2025-01-01",
    end_date: str = "2026-06-01",
    criteria: dict | None = None,
    similarity_threshold: float = 0.8,
) -> dict[str, Any]:
    """运行完整进化循环。

    Returns:
        {"rounds_run": int, "strategies_found": int,
         "hall_of_fame": [...], "graveyard_size": int}
    """
    crit = dict(DEFAULT_CRITERIA)
    if criteria:
        crit.update(criteria)

    _log(f"开始进化: symbol={symbol} timeframe={timeframe} max_rounds={max_rounds}")
    _log(f"达标条件: {crit}")

    last_analysis = None
    strategies_found = 0

    for round_num in range(1, max_rounds + 1):
        _log(f"\n{'='*60}")
        _log(f"第 {round_num}/{max_rounds} 轮进化")
        _log(f"{'='*60}")

        # 1. LLM 生成规则
        try:
            rule = generate_strategy(
                round_num,
                last_analysis=last_analysis,
                similarity_threshold=similarity_threshold,
            )
        except RuntimeError as e:
            _log(f"策略生成失败: {e}")
            continue

        strategy_name = rule.get("name", f"unnamed_r{round_num}")
        reasoning = rule.pop("reasoning", "无说明")
        _log(f"策略: {strategy_name}")
        _log(f"因子: {rule.get('entry', {}).get('conditions', [])}")
        _log(f"理由: {reasoning}")

        # 2. 代码生成
        gen_result = codegen.rule_to_code(rule)
        if not gen_result["valid"]:
            _log(f"代码生成失败: {gen_result['errors']}")
            add_to_graveyard({
                "name": strategy_name,
                "rule": rule,
                "result": {},
                "failure_type": "代码生成失败",
                "lesson": f"代码错误: {gen_result['errors']}",
            })
            continue

        _log("代码生成成功，提交回测...")

        # 3. QD 回测
        try:
            bt_result = bt.run_backtest(
                code=gen_result["code"],
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as e:
            _log(f"回测异常: {e}")
            add_to_graveyard({
                "name": strategy_name,
                "rule": rule,
                "result": {},
                "failure_type": "回测失败",
                "lesson": f"回测异常: {e}",
            })
            continue

        if bt_result.get("status") != "succeeded":
            _log(f"回测未成功: {bt_result.get('error', bt_result.get('status'))}")
            add_to_graveyard({
                "name": strategy_name,
                "rule": rule,
                "result": bt_result,
                "failure_type": "回测未成功",
                "lesson": bt_result.get("error", "未知原因"),
            })
            continue

        # 4. 分析
        report = analyzer.analyze(bt_result, criteria=crit)
        last_analysis = report
        _log(f"分析结果: {report['verdict']}")
        _log(f"  收益={report['total_return_pct']:.2f}% 胜率={report['win_rate']:.1f}% "
             f"盈亏比={report['profit_factor']:.2f} 回撤={report['max_drawdown_pct']:.1f}%")

        # 5. 判定
        if report["verdict"] == "PASS":
            _log(f"策略 '{strategy_name}' 达标！")
            add_to_hall_of_fame({
                "name": strategy_name,
                "rule": rule,
                "result": {
                    "total_return_pct": report["total_return_pct"],
                    "win_rate": report["win_rate"],
                    "profit_factor": report["profit_factor"],
                    "max_drawdown_pct": report["max_drawdown_pct"],
                    "sharpe_ratio": report["sharpe_ratio"],
                    "total_trades": report["total_trades"],
                },
                "code": gen_result["code"],
                "reasoning": reasoning,
            })
            strategies_found += 1
            _log(f"已找到 {strategies_found} 个达标策略，停止进化")
            break
        else:
            top_loss = ""
            if report.get("loss_breakdown"):
                top = sorted(report["loss_breakdown"].items(), key=lambda x: -x[1]["count"])
                if top:
                    top_loss = top[0][1]["label"]

            add_to_graveyard({
                "name": strategy_name,
                "rule": rule,
                "result": {
                    "total_return_pct": report["total_return_pct"],
                    "win_rate": report["win_rate"],
                    "profit_factor": report["profit_factor"],
                    "max_drawdown_pct": report["max_drawdown_pct"],
                },
                "failure_type": top_loss or "未达标",
                "lesson": "; ".join(report.get("improvement_hints", [])[:2]),
                "forbidden_combos": [
                    "+".join(rule.get("entry", {}).get("conditions", []))
                    + " (同参数)"
                ],
            })
            _log(f"策略入墓，继续下一轮...")

    graveyard = load_graveyard()
    hof = load_hall_of_fame()

    summary = {
        "rounds_run": round_num if 'round_num' in dir() else 0,
        "strategies_found": strategies_found,
        "hall_of_fame": hof,
        "graveyard_size": len(graveyard),
    }

    _log(f"\n进化完成: 运行 {summary['rounds_run']} 轮, "
         f"找到 {strategies_found} 个达标策略, "
         f"墓地 {len(graveyard)} 个")

    return summary


def get_best_strategy() -> dict[str, Any] | None:
    """获取名人堂中表现最好的策略。"""
    hof = load_hall_of_fame()
    if not hof:
        return None
    return max(hof, key=lambda s: s.get("result", {}).get("sharpe_ratio", -999))


# ═══════════════════════════ 组合进化循环 v2 ═══════════════════════════

def evolve_combo(
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    max_rounds: int = 5,
    start_date: str = "2025-01-01",
    end_date: str = "2026-06-01",
    criteria: dict | None = None,
    similarity_threshold: float = 0.7,
) -> dict[str, Any]:
    """运行策略组合进化循环（v2）。

    每轮生成一个完整策略组合（震荡+趋势+突破各一套），
    分别回测后综合评判。

    Returns:
        {"rounds_run": int, "combos_found": int, "best_combo": ..., ...}
    """
    crit = dict(DEFAULT_CRITERIA)
    if criteria:
        crit.update(criteria)

    _log(f"[组合进化] 开始: symbol={symbol} rounds={max_rounds}")
    _log(f"[组合进化] 达标条件: {crit}")

    last_analysis = None
    combos_found = 0
    round_num = 0

    for round_num in range(1, max_rounds + 1):
        _log(f"\n{'='*60}")
        _log(f"[组合进化] 第 {round_num}/{max_rounds} 轮")
        _log(f"{'='*60}")

        # 1. LLM 生成策略组合
        try:
            combo = generate_strategy_combo(
                round_num,
                last_analysis=last_analysis,
                similarity_threshold=similarity_threshold,
            )
        except RuntimeError as e:
            _log(f"策略组合生成失败: {e}")
            continue

        _log(f"策略组合: {combo.name}")
        _log(f"理由: {combo.reasoning[:200]}")

        # 2. 分别回测三种行情的策略
        regime_results: dict[str, dict] = {}
        all_pass = True

        for regime in REGIMES:
            regime_rule = getattr(combo, f"{regime}_rule")
            rule_for_codegen = {
                "name": f"{combo.name}_{regime}",
                "entry": {
                    "conditions": regime_rule.get("conditions", []),
                    "logic": regime_rule.get("logic", "AND"),
                },
                "exit": {
                    "stop_loss_atr_mult": regime_rule.get("sl_atr_mult", 1.5),
                    "take_profit_atr_mult": regime_rule.get("tp_atr_mult", 2.5),
                    "time_stop_bars": regime_rule.get("time_stop_bars", 12),
                },
                "params": regime_rule.get("params", {}),
                "direction": regime_rule.get("direction", "both"),
            }

            # 代码生成
            gen_result = codegen.rule_to_code(rule_for_codegen)
            if not gen_result["valid"]:
                _log(f"  [{regime}] 代码生成失败: {gen_result['errors']}")
                regime_results[regime] = {"status": "codegen_failed", "errors": gen_result["errors"]}

                add_lesson(_derive_lesson(
                    f"{combo.name}_{regime}", rule_for_codegen,
                    {"failure_type": "代码生成失败"},
                    regime_hint=regime,
                ))
                all_pass = False
                continue

            # 回测
            try:
                bt_result = bt.run_backtest(
                    code=gen_result["code"],
                    symbol=symbol,
                    timeframe=timeframe,
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as e:
                _log(f"  [{regime}] 回测异常: {e}")
                regime_results[regime] = {"status": "backtest_error", "error": str(e)}
                add_lesson(_derive_lesson(
                    f"{combo.name}_{regime}", rule_for_codegen,
                    {"failure_type": "回测异常"},
                    regime_hint=regime,
                ))
                all_pass = False
                continue

            if bt_result.get("status") != "succeeded":
                _log(f"  [{regime}] 回测未成功: {bt_result.get('error', bt_result.get('status'))}")
                regime_results[regime] = bt_result
                add_lesson(_derive_lesson(
                    f"{combo.name}_{regime}", rule_for_codegen,
                    {"failure_type": "回测未成功"},
                    regime_hint=regime,
                ))
                all_pass = False
                continue

            # 分析
            report = analyzer.analyze(bt_result, criteria=crit)
            regime_results[regime] = report
            _log(f"  [{regime}] {report['verdict']}: "
                 f"收益={report['total_return_pct']:.2f}% "
                 f"胜率={report['win_rate']:.1f}% "
                 f"盈亏比={report['profit_factor']:.2f}")

            if report["verdict"] != "PASS":
                all_pass = False
                add_lesson(_derive_lesson(
                    f"{combo.name}_{regime}", rule_for_codegen,
                    report, regime_hint=regime,
                ))

        last_analysis = {"regime_results": regime_results, "combo_name": combo.name}

        # 3. 综合判定
        pass_count = sum(
            1 for r in regime_results.values()
            if isinstance(r, dict) and r.get("verdict") == "PASS"
        )

        _log(f"综合评判: {pass_count}/{len(REGIMES)} 个行情策略达标")

        if pass_count >= 2:
            _log(f"策略组合 '{combo.name}' 达标（{pass_count}/3 通过）！")

            overall_stats = _calc_combo_overall(regime_results)
            add_combo_to_hof(combo, {
                "per_regime": regime_results,
                "overall": overall_stats,
                "pass_count": pass_count,
            })
            combos_found += 1
            break
        else:
            _log(f"组合未达标（仅 {pass_count}/3 通过），继续下一轮...")

            for regime in REGIMES:
                r = regime_results.get(regime, {})
                if isinstance(r, dict) and r.get("verdict") and r["verdict"] != "PASS":
                    regime_rule = getattr(combo, f"{regime}_rule")
                    add_to_graveyard({
                        "name": f"{combo.name}_{regime}",
                        "rule": {"entry": {"conditions": regime_rule.get("conditions", [])},
                                 "params": regime_rule.get("params", {})},
                        "result": {
                            "total_return_pct": r.get("total_return_pct", 0),
                            "win_rate": r.get("win_rate", 0),
                            "profit_factor": r.get("profit_factor", 0),
                        },
                        "failure_type": r.get("loss_breakdown", {}).get("top", "未达标"),
                        "lesson": f"[{regime}] " + "; ".join(r.get("improvement_hints", [])[:2]),
                    })

    combo_hof = load_combo_hof()
    summary = {
        "rounds_run": round_num,
        "combos_found": combos_found,
        "best_combo": get_best_combo(),
        "graveyard_size": len(load_graveyard()),
        "lessons_count": len(load_lessons()),
    }

    _log(f"\n[组合进化] 完成: {round_num} 轮, {combos_found} 个达标组合, "
         f"{summary['lessons_count']} 条教训")

    return summary


def _calc_combo_overall(regime_results: dict) -> dict:
    """计算策略组合的综合表现统计。"""
    total_return = 0.0
    total_trades = 0
    total_wins = 0
    total_pf_sum = 0.0
    sharpe_sum = 0.0
    count = 0

    for regime, r in regime_results.items():
        if not isinstance(r, dict) or "total_return_pct" not in r:
            continue
        total_return += r.get("total_return_pct", 0)
        trades = r.get("total_trades", 0)
        total_trades += trades
        win_rate = r.get("win_rate", 0) / 100
        total_wins += int(trades * win_rate)
        total_pf_sum += r.get("profit_factor", 0)
        sharpe_sum += r.get("sharpe_ratio", 0)
        count += 1

    return {
        "total_return_pct": round(total_return, 2),
        "total_trades": total_trades,
        "win_rate": round(total_wins / max(total_trades, 1) * 100, 1),
        "profit_factor": round(total_pf_sum / max(count, 1), 2),
        "sharpe_ratio": round(sharpe_sum / max(count, 1), 2),
    }


# ═══════════════════════════ 自动触发再进化 ═══════════════════════════

def check_and_trigger_re_evolve(
    symbol: str = "BTC/USDT",
    force: bool = False,
) -> dict[str, Any]:
    """检查实盘表现，必要时自动触发再进化。

    Returns:
        {"triggered": bool, "reason": str, ...}
    """
    live = _get_live_performance_summary()

    if not live.get("available"):
        return {"triggered": False, "reason": "无实盘数据"}

    if not force and not live.get("is_decaying"):
        return {
            "triggered": False,
            "reason": f"表现正常 — 胜率{live['win_rate']:.1f}% PF{live['profit_factor']:.2f}",
        }

    decay_reason = ", ".join(live.get("decay_signals", ["手动触发"]))
    _log(f"[自动再进化] 触发原因: {decay_reason}")

    try:
        result = evolve_combo(
            symbol=symbol,
            max_rounds=3,
        )
        return {
            "triggered": True,
            "reason": decay_reason,
            "evolve_result": result,
        }
    except Exception as e:
        _log(f"[自动再进化] 失败: {e}")
        return {
            "triggered": True,
            "reason": decay_reason,
            "error": str(e),
        }


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="LLM 策略进化引擎 v2")
    sub = parser.add_subparsers(dest="cmd")

    p_evolve = sub.add_parser("evolve", help="启动单策略进化循环（v1）")
    p_evolve.add_argument("--symbol", default="BTCUSDT")
    p_evolve.add_argument("--rounds", type=int, default=10)
    p_evolve.add_argument("--timeframe", default="15m")
    p_evolve.add_argument("--start", default="2025-01-01")
    p_evolve.add_argument("--end", default="2026-06-01")

    p_combo = sub.add_parser("evolve-combo", help="启动策略组合进化（v2）")
    p_combo.add_argument("--symbol", default="BTCUSDT")
    p_combo.add_argument("--rounds", type=int, default=5)
    p_combo.add_argument("--timeframe", default="15m")
    p_combo.add_argument("--start", default="2025-01-01")
    p_combo.add_argument("--end", default="2026-06-01")

    p_re = sub.add_parser("re-evolve", help="检查并触发自动再进化")
    p_re.add_argument("--symbol", default="BTCUSDT")
    p_re.add_argument("--force", action="store_true", help="强制触发")

    sub.add_parser("hall-of-fame", help="查看达标策略")
    sub.add_parser("combo-hof", help="查看策略组合名人堂")
    sub.add_parser("graveyard", help="查看失败策略墓地")
    sub.add_parser("lessons", help="查看结构化教训库")
    sub.add_parser("status", help="查看当前状态")
    sub.add_parser("clear", help="清空墓地和名人堂（危险）")

    args = parser.parse_args()

    if args.cmd == "evolve":
        qd_symbol = args.symbol
        if "/" not in qd_symbol:
            qd_symbol = qd_symbol.replace("USDT", "/USDT")
        result = evolve(
            symbol=qd_symbol,
            timeframe=args.timeframe,
            max_rounds=args.rounds,
            start_date=args.start,
            end_date=args.end,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.cmd == "evolve-combo":
        qd_symbol = args.symbol
        if "/" not in qd_symbol:
            qd_symbol = qd_symbol.replace("USDT", "/USDT")
        result = evolve_combo(
            symbol=qd_symbol,
            timeframe=args.timeframe,
            max_rounds=args.rounds,
            start_date=args.start,
            end_date=args.end,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.cmd == "re-evolve":
        qd_symbol = args.symbol
        if "/" not in qd_symbol:
            qd_symbol = qd_symbol.replace("USDT", "/USDT")
        result = check_and_trigger_re_evolve(symbol=qd_symbol, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.cmd == "hall-of-fame":
        hof = load_hall_of_fame()
        if not hof:
            print("名人堂为空，还没有达标策略")
        else:
            for i, s in enumerate(hof, 1):
                r = s.get("result", {})
                print(f"\n[{i}] {s['name']}")
                print(f"    因子: {s.get('rule',{}).get('entry',{}).get('conditions',[])}")
                print(f"    收益: {r.get('total_return_pct',0):.2f}%  "
                      f"胜率: {r.get('win_rate',0):.1f}%  "
                      f"盈亏比: {r.get('profit_factor',0):.2f}  "
                      f"夏普: {r.get('sharpe_ratio',0):.2f}")
                print(f"    入堂: {s.get('inducted_at', '?')}")

    elif args.cmd == "combo-hof":
        hof = load_combo_hof()
        if not hof:
            print("策略组合名人堂为空")
        else:
            for i, c in enumerate(hof, 1):
                r = c.get("results", {}).get("overall", {})
                print(f"\n[{i}] {c['name']}")
                for regime in REGIMES:
                    rule = c.get(f"{regime}_rule", {})
                    conds = rule.get("conditions", [])
                    regime_r = c.get("results", {}).get("per_regime", {}).get(regime, {})
                    wr = regime_r.get("win_rate", 0)
                    print(f"    {regime:10s}: {conds}  胜率 {wr:.1f}%")
                print(f"    综合: 收益{r.get('total_return_pct',0):.2f}%  "
                      f"胜率{r.get('win_rate',0):.1f}%  "
                      f"夏普{r.get('sharpe_ratio',0):.2f}")
                print(f"    创建: {c.get('created_at', '?')}")

    elif args.cmd == "graveyard":
        gy = load_graveyard()
        if not gy:
            print("墓地为空")
        else:
            print(f"共 {len(gy)} 个失败策略：")
            for i, g in enumerate(gy, 1):
                r = g.get("result", {})
                print(f"\n  [{i}] {g['name']}")
                print(f"      失败原因: {g.get('failure_type', '?')}")
                print(f"      教训: {g.get('lesson', '?')}")
                if r:
                    print(f"      收益: {r.get('total_return_pct',0):.1f}%  "
                          f"胜率: {r.get('win_rate',0):.1f}%")

    elif args.cmd == "lessons":
        lessons = load_lessons()
        if not lessons:
            print("教训库为空")
        else:
            print(f"共 {len(lessons)} 条结构化教训：\n")
            for ls in lessons:
                fix_mark = "✓" if ls.get("fix_effective") else "✗" if ls.get("fix_effective") is False else "?"
                print(f"  [{ls.get('lesson_id','?')}] {ls.get('strategy','?')}")
                print(f"      行情: {ls.get('failure_regime','?')}  |  "
                      f"类型: {ls.get('failure_type','?')}")
                print(f"      根因: {ls.get('root_cause','?')}")
                print(f"      修复: {ls.get('fix_applied','?')} [{fix_mark}]")

    elif args.cmd == "status":
        gy = load_graveyard()
        hof = load_hall_of_fame()
        combo_hof = load_combo_hof()
        lessons = load_lessons()
        llm = _llm_config()

        try:
            qd = bt.check_qd_health()
        except Exception:
            qd = {"healthy": False}

        print("=== 进化引擎 v2 状态 ===")
        print(f"  LLM:       {'已配置 (' + llm['model'] + ')' if llm else '未配置'}")
        print(f"  QD:        {'在线' if qd.get('healthy') else '离线'}")
        print(f"  单策略名人堂: {len(hof)} 个")
        print(f"  组合名人堂:   {len(combo_hof)} 个")
        print(f"  墓地:        {len(gy)} 个")
        print(f"  结构化教训:   {len(lessons)} 条")

        if combo_hof:
            best = get_best_combo()
            if best:
                r = best.get("results", {}).get("overall", {})
                print(f"  最佳组合: {best['name']} "
                      f"(夏普 {r.get('sharpe_ratio',0):.2f})")
        elif hof:
            best = get_best_strategy()
            if best:
                print(f"  最佳单策略: {best['name']} "
                      f"(夏普 {best.get('result',{}).get('sharpe_ratio',0):.2f})")

        live = _get_live_performance_summary()
        if live.get("available"):
            print(f"\n  === 实盘表现 ===")
            print(f"  交易数: {live['total_trades']}")
            print(f"  胜率:   {live['win_rate']:.1f}%")
            print(f"  盈亏比: {live['profit_factor']:.2f}")
            print(f"  衰退:   {'是 ⚠️' if live['is_decaying'] else '否 ✓'}")
            if live.get("weakest_regime"):
                print(f"  薄弱行情: {live['weakest_regime']}")

    elif args.cmd == "clear":
        confirm = input("确认清空所有数据（墓地+名人堂+教训库）？(yes/no): ")
        if confirm.lower() == "yes":
            save_graveyard([])
            save_hall_of_fame([])
            save_combo_hof([])
            save_lessons([])
            print("已清空")
        else:
            print("取消")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
