#!/usr/bin/env python3
"""贾维斯 JARVIS — 自然语言生成回测策略（AI 策略工坊核心）。

面向完全不会写策略的用户：
  用一句大白话描述想法（如"跌得恐慌的时候买，涨回来就卖"）
  → LLM 从因子菜单中挑选组合，产出规则 JSON
  → 校验因子/参数合法性（越界自动夹到合法范围）
  → jarvis_scalper_codegen 拼装成 QD 可回测代码并做沙盒安全校验
  → 返回 {rule, code, explain}，代码可直接提交 jarvis_scalper_backtest 回测

与 jarvis_scalper_evolve 的区别：
  - evolve 是多轮自动进化（LLM 自己找方向），本模块是"听用户的想法"单次生成
  - 生成结果不自动入名人堂/墓地，由用户决定是否保存

用法：
  python jarvis_strategy_gen.py generate --desc "均线金叉加放量就做多" [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any

import jarvis_llm_config as llm
import jarvis_scalper_codegen as codegen
import jarvis_scalper_features as sf

SYSTEM_PROMPT = """你是一个加密货币量化策略研究员，专长是把普通用户的自然语言交易想法翻译成可回测的策略规则。
用户完全不懂编程和量化，你必须：
1. 理解用户想法的核心逻辑（什么时候进场、什么时候离场、做多还是做空）
2. 从因子菜单中挑选最贴合用户想法的 2~4 个因子（用 feature_id 引用，禁止自造）
3. 参数必须在因子定义的合法范围（param_ranges）内
4. 用大白话向用户解释你的设计（explain 字段，不要出现代码术语）
5. 严格输出标准 JSON，不要输出任何其他文字"""


def _build_prompt(description: str, symbol: str, timeframe: str) -> str:
    feature_menu = sf.get_menu_json()
    return f"""用户想做一个 {symbol} {timeframe} 周期的交易策略，下面是用户的原话：

「{description}」

## 可用因子菜单（只能从这里选）
{feature_menu}

## 输出要求
严格按以下 JSON 格式输出（只输出 JSON，不要其他文字）：

```json
{{
  "name": "英文小写下划线策略名",
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
  "reasoning": "面向研究员的专业设计说明",
  "explain": "面向小白用户的大白话解释：这个策略什么时候买、什么时候卖、为什么贴合他的想法（100 字以内）"
}}
```

注意：
- conditions 因子 ID 必须从菜单选择，建议 2~4 个
- logic 为 "AND"（同时满足）或 "OR"（任一满足）；用户想法宽松用 OR，严格用 AND
- direction："both"（多空都做）/ "long"（只做多）/ "short"（只做空），按用户意图选
- params 键名格式 "因子ID_参数名"（如 "ema_cross_fast"），值必须落在该因子 param_ranges 内
- 用户想法里若提到止损/止盈幅度，映射到 stop_loss_atr_mult / take_profit_atr_mult（1 倍 ATR ≈ 1% 左右波动）
- explain 必须是不带术语的大白话"""


def _parse_llm_json(response: str) -> dict[str, Any] | None:
    """从 LLM 响应中提取 JSON（容错围栏/裸 JSON）。"""
    m = re.search(r"```json\s*(.*?)\s*```", response, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", response, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        return None


def _validate_and_fix_rule(rule: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """校验规则：剔除无效因子、参数越界夹回合法范围。返回 (修复后的规则, 问题列表)。"""
    issues: list[str] = []

    conditions = list(rule.get("entry", {}).get("conditions", []))
    valid_conditions = []
    for cid in conditions:
        if sf.get_feature(cid):
            valid_conditions.append(cid)
        else:
            issues.append(f"忽略了不存在的因子: {cid}")
    if not valid_conditions:
        raise ValueError("策略没有任何有效因子")
    rule.setdefault("entry", {})["conditions"] = valid_conditions

    logic = str(rule.get("entry", {}).get("logic", "AND")).upper()
    rule["entry"]["logic"] = logic if logic in ("AND", "OR") else "AND"

    direction = str(rule.get("direction", "both")).lower()
    rule["direction"] = direction if direction in ("both", "long", "short") else "both"

    # 参数夹回合法范围
    params = dict(rule.get("params", {}))
    for cid in valid_conditions:
        feature = sf.get_feature(cid)
        ranges = feature.get("param_ranges", {})
        for pname, (lo, hi) in ranges.items():
            key = f"{cid}_{pname}"
            if key not in params:
                continue
            try:
                val = float(params[key])
            except (TypeError, ValueError):
                issues.append(f"参数 {key} 非数字，改用默认值")
                params.pop(key)
                continue
            if val < lo or val > hi:
                clamped = min(max(val, lo), hi)
                issues.append(f"参数 {key}={val} 越界，已调整为 {clamped}")
                params[key] = clamped
    rule["params"] = params

    # 名称兜底
    name = str(rule.get("name", "") or "").strip()
    if not name:
        name = f"ai_strategy_{time.strftime('%m%d_%H%M%S')}"
    rule["name"] = re.sub(r"[^a-zA-Z0-9_]", "_", name)[:48]

    exit_cfg = rule.setdefault("exit", {})
    exit_cfg.setdefault("stop_loss_atr_mult", 1.5)
    exit_cfg.setdefault("take_profit_atr_mult", 2.5)
    exit_cfg.setdefault("time_stop_bars", 12)

    return rule, issues


def _friendly_summary(rule: dict[str, Any]) -> dict[str, Any]:
    """给前端的小白友好摘要：因子中文名、方向、止盈止损描述。"""
    conditions = rule.get("entry", {}).get("conditions", [])
    factors = []
    for cid in conditions:
        f = sf.get_feature(cid) or {}
        factors.append({
            "id": cid,
            "name": f.get("name", cid),
            "description": f.get("description", ""),
        })
    direction_cn = {"both": "多空都做", "long": "只做多", "short": "只做空"}.get(
        rule.get("direction", "both"), "多空都做"
    )
    logic_cn = "同时满足才进场" if rule.get("entry", {}).get("logic") == "AND" else "满足任意一个就进场"
    exit_cfg = rule.get("exit", {})
    return {
        "factors": factors,
        "direction": direction_cn,
        "logic": logic_cn,
        "stop_loss": f"约 {exit_cfg.get('stop_loss_atr_mult', 1.5):.1f} 倍波动幅度止损",
        "take_profit": f"约 {exit_cfg.get('take_profit_atr_mult', 2.5):.1f} 倍波动幅度止盈",
    }


def generate_from_description(
    description: str,
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    max_retries: int = 3,
) -> dict[str, Any]:
    """自然语言 → 可回测策略。

    Returns:
        {
          "ok": bool,
          "rule": dict,          # 策略规则 JSON（可存档/复现）
          "code": str,           # QD IndicatorStrategy 代码（可直接回测）
          "name": str,
          "explain": str,        # 大白话解释
          "reasoning": str,      # 专业说明
          "summary": dict,       # 因子中文摘要
          "issues": [str],       # 自动修复记录
          "error": str,          # ok=False 时的原因
        }
    """
    description = (description or "").strip()
    if not description:
        return {"ok": False, "error": "请先用一句话描述你的策略想法"}

    prompt = _build_prompt(description, symbol, timeframe)
    last_error = "生成失败"

    for attempt in range(1, max_retries + 1):
        try:
            response = llm.call_llm(prompt, system=SYSTEM_PROMPT, temperature=0.4,
                                    module="strategy_gen")
        except Exception as e:  # noqa: BLE001
            # 配置缺失/网络错误没有重试价值，直接返回
            return {"ok": False, "error": f"调用大模型失败: {e}"}

        rule = _parse_llm_json(response)
        if not rule:
            last_error = "大模型返回的内容无法解析为策略规则"
            continue

        try:
            rule, issues = _validate_and_fix_rule(rule)
        except ValueError as e:
            last_error = str(e)
            continue

        gen = codegen.rule_to_code(rule)
        if not gen["valid"]:
            last_error = "生成的代码未通过安全校验: " + "; ".join(gen["errors"])
            continue

        return {
            "ok": True,
            "rule": rule,
            "code": gen["code"],
            "name": gen["name"],
            "explain": str(rule.get("explain", "") or ""),
            "reasoning": str(rule.get("reasoning", "") or ""),
            "summary": _friendly_summary(rule),
            "issues": issues,
            "attempts": attempt,
        }

    return {"ok": False, "error": f"连续 {max_retries} 次生成失败：{last_error}"}


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="自然语言生成回测策略")
    sub = parser.add_subparsers(dest="cmd")

    p_gen = sub.add_parser("generate", help="从自然语言描述生成策略")
    p_gen.add_argument("--desc", required=True, help="策略想法（自然语言）")
    p_gen.add_argument("--symbol", default="BTC/USDT")
    p_gen.add_argument("--timeframe", default="15m")
    p_gen.add_argument("--json", action="store_true", help="输出完整 JSON")

    args = parser.parse_args()

    if args.cmd == "generate":
        result = generate_from_description(args.desc, args.symbol, args.timeframe)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result["ok"]:
            print(f"策略名: {result['name']}")
            print(f"解释: {result['explain']}")
            print(f"因子: {[f['name'] for f in result['summary']['factors']]}")
            if result["issues"]:
                print(f"自动修复: {result['issues']}")
            print("\n─── 策略代码 ───")
            print(result["code"])
        else:
            print(f"生成失败: {result['error']}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
