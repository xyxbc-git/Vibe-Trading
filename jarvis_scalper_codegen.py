#!/usr/bin/env python3
"""贾维斯 JARVIS — 策略代码生成器（S-02）。

把 LLM 生成的规则 JSON 拼装成符合 QuantDinger IndicatorStrategy 规范的
Python 代码字符串，可直接提交给 QD 回测 API。

规则 JSON 格式：
  {
    "name": "supertrend_vol_v3",
    "entry": {
      "conditions": ["supertrend", "volume_spike"],
      "logic": "AND"
    },
    "exit": {
      "stop_loss_atr_mult": 1.5,
      "take_profit_atr_mult": 2.5,
      "time_stop_bars": 12
    },
    "params": {
      "supertrend_period": 10,
      "supertrend_multiplier": 3.0,
      "volume_spike_period": 20,
      "volume_spike_spike_ratio": 1.5
    },
    "direction": "both"
  }

生成的代码遵循 QD 规范：
  - 形态 B 四路信号（open_long / close_long / open_short / close_short）
  - exit_owner: engine（止盈止损由引擎管理）
  - df.copy() 开头、output 结尾
  - 边缘触发
  - 沙盒安全（无 import/open/eval）

用法：
  python jarvis_scalper_codegen.py generate --rule rule.json
  python jarvis_scalper_codegen.py validate --code strategy.py
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import jarvis_scalper_features as sf


def generate_strategy_code(rule: dict[str, Any]) -> str:
    """根据规则 JSON 生成 QD IndicatorStrategy Python 代码。

    Args:
        rule: LLM 生成的策略规则字典

    Returns:
        可直接提交 QD 的 Python 代码字符串
    """
    name = rule.get("name", "unnamed_strategy")
    entry = rule.get("entry", {})
    exit_cfg = rule.get("exit", {})
    direction = rule.get("direction", "both")
    rule_params = rule.get("params", {})

    conditions = entry.get("conditions", [])
    logic = entry.get("logic", "AND").upper()

    stop_loss_atr = exit_cfg.get("stop_loss_atr_mult", 1.5)
    take_profit_atr = exit_cfg.get("take_profit_atr_mult", 2.5)

    stop_loss_pct = round(stop_loss_atr * 0.01, 4)
    take_profit_pct = round(take_profit_atr * 0.01, 4)

    header = _build_header(name, direction, stop_loss_pct, take_profit_pct)
    indicator_code = _build_indicators(conditions, rule_params)
    signal_code = _build_signals(conditions, logic, direction)
    output_code = _build_output(name)

    return "\n".join([header, indicator_code, signal_code, output_code])


def _build_header(name: str, direction: str, sl_pct: float, tp_pct: float) -> str:
    """生成脚本头部：元数据和策略配置。"""
    return f'''my_indicator_name = "{name}"
my_indicator_description = "Auto-generated 15m scalper strategy by Jarvis Evolution Engine"

# signal_form: four_way
# exit_owner: engine

# @strategy stopLossPct {sl_pct}
# @strategy takeProfitPct {tp_pct}
# @strategy entryPct 0.95
# @strategy tradeDirection {direction}

df = df.copy()
'''


def _build_indicators(conditions: list[str], rule_params: dict[str, Any]) -> str:
    """生成指标计算代码。"""
    lines = ["# ── 指标计算 ──"]

    # ATR（通用，用于止损止盈）
    lines.append(
        "_tr = pd.concat([\n"
        "    df['high'] - df['low'],\n"
        "    (df['high'] - df['close'].shift(1)).abs(),\n"
        "    (df['low'] - df['close'].shift(1)).abs()\n"
        "], axis=1).max(axis=1)\n"
        "_atr_14 = _tr.rolling(14).mean()"
    )

    for cond_id in conditions:
        feature = sf.get_feature(cond_id)
        if not feature:
            lines.append(f"# 警告：因子 '{cond_id}' 不存在，跳过")
            continue

        params = _extract_params(cond_id, feature, rule_params)

        long_code = sf.get_code_snippet(cond_id, "long", params)
        short_code = sf.get_code_snippet(cond_id, "short", params)

        lines.append(f"\n# ── {feature['name']} ({cond_id}) ──")
        if long_code:
            lines.append(_assign_multiline(f"_sig_long_{cond_id}", long_code))
        if short_code:
            lines.append(_assign_multiline(f"_sig_short_{cond_id}", short_code))

    return "\n".join(lines) + "\n"


def _assign_multiline(var_name: str, code: str) -> str:
    """把多行代码片段拆成：前置计算行 + 最后一行赋值给变量。

    单行代码：直接 var = expr
    多行代码：前面的行原样输出，最后一行赋值给 var
    """
    code_lines = code.strip().split("\n")
    if len(code_lines) == 1:
        return f"{var_name} = {code_lines[0]}"
    setup = "\n".join(code_lines[:-1])
    final_expr = code_lines[-1]
    return f"{setup}\n{var_name} = {final_expr}"


def _extract_params(cond_id: str, feature: dict[str, Any], rule_params: dict[str, Any]) -> dict[str, Any]:
    """从规则参数中提取对应因子的参数。

    规则参数命名规则：{因子ID}_{参数名}，如 supertrend_period。
    """
    params: dict[str, Any] = {}
    for pname, default_val in feature["params"].items():
        key = f"{cond_id}_{pname}"
        params[pname] = rule_params.get(key, default_val)
    return params


def _build_signals(conditions: list[str], logic: str, direction: str) -> str:
    """生成四路信号代码。"""
    lines = ["\n# ── 信号组合 ──"]

    valid_long = [c for c in conditions if sf.get_feature(c) and sf.get_code_snippet(c, "long")]
    valid_short = [c for c in conditions if sf.get_feature(c) and sf.get_code_snippet(c, "short")]

    if not valid_long:
        lines.append("_combined_long = pd.Series(False, index=df.index)")
    else:
        joiner = " & " if logic == "AND" else " | "
        expr = joiner.join(f"_sig_long_{c}" for c in valid_long)
        lines.append(f"_combined_long = ({expr}).fillna(False)")

    if not valid_short:
        lines.append("_combined_short = pd.Series(False, index=df.index)")
    else:
        joiner = " & " if logic == "AND" else " | "
        expr = joiner.join(f"_sig_short_{c}" for c in valid_short)
        lines.append(f"_combined_short = ({expr}).fillna(False)")

    lines.append("""
# 边缘触发（仅在信号从 False→True 时触发）
_edge_long = (_combined_long & ~_combined_long.shift(1).fillna(False)).astype(bool)
_edge_short = (_combined_short & ~_combined_short.shift(1).fillna(False)).astype(bool)
""")

    if direction == "both":
        lines.append("df['open_long'] = _edge_long")
        lines.append("df['close_long'] = _edge_short")
        lines.append("df['open_short'] = _edge_short")
        lines.append("df['close_short'] = _edge_long")
    elif direction == "long":
        lines.append("df['open_long'] = _edge_long")
        lines.append("df['close_long'] = _edge_short")
        lines.append("df['open_short'] = pd.Series(False, index=df.index)")
        lines.append("df['close_short'] = pd.Series(False, index=df.index)")
    else:
        lines.append("df['open_long'] = pd.Series(False, index=df.index)")
        lines.append("df['close_long'] = pd.Series(False, index=df.index)")
        lines.append("df['open_short'] = _edge_short")
        lines.append("df['close_short'] = _edge_long")

    lines.append("""
# 兼容两路（全 False，仅保留四路作为成交依据）
df['buy'] = pd.Series(False, index=df.index)
df['sell'] = pd.Series(False, index=df.index)
""")

    return "\n".join(lines)


def _build_output(name: str) -> str:
    """生成 output 字典。"""
    return f'''
# ── 图表输出 ──
output = {{
    "name": "{name}",
    "plots": [
        {{"name": "ATR(14)", "data": _atr_14.tolist(), "color": "#888888", "overlay": False}},
    ],
    "signals": [
        {{"type": "buy", "data": df['open_long'].tolist(), "text": "L", "color": "#00C853"}},
        {{"type": "sell", "data": df['open_short'].tolist(), "text": "S", "color": "#FF1744"}},
    ]
}}
'''


def validate_code(code: str) -> dict[str, Any]:
    """验证生成的代码是否安全且语法正确。

    Returns:
        {"valid": True/False, "errors": [...]}
    """
    errors = []

    forbidden = ["import ", "open(", "eval(", "exec(", "__import__",
                 "getattr(", "setattr(", "subprocess", "os."]
    for fb in forbidden:
        if fb in code:
            errors.append(f"包含禁止关键字: {fb}")

    try:
        compile(code, "<strategy>", "exec")
    except SyntaxError as e:
        errors.append(f"语法错误: {e}")

    required = ["df = df.copy()", "output"]
    for req in required:
        if req not in code:
            errors.append(f"缺少必要元素: {req}")

    return {"valid": len(errors) == 0, "errors": errors}


def rule_to_code(rule: dict[str, Any]) -> dict[str, Any]:
    """完整流程：规则 → 代码 → 验证。

    Returns:
        {"code": str, "valid": bool, "errors": list, "name": str}
    """
    code = generate_strategy_code(rule)
    check = validate_code(code)
    return {
        "code": code,
        "valid": check["valid"],
        "errors": check["errors"],
        "name": rule.get("name", "unnamed"),
    }


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="15m 策略代码生成器")
    sub = parser.add_subparsers(dest="cmd")

    p_gen = sub.add_parser("generate", help="从规则 JSON 生成策略代码")
    p_gen.add_argument("--rule", required=True, help="规则 JSON 文件路径")
    p_gen.add_argument("--output", help="输出文件路径（默认打印到终端）")

    p_val = sub.add_parser("validate", help="验证策略代码")
    p_val.add_argument("--code", required=True, help="代码文件路径")

    p_demo = sub.add_parser("demo", help="生成一个示例策略")

    args = parser.parse_args()

    if args.cmd == "generate":
        with open(args.rule, encoding="utf-8") as f:
            rule = json.load(f)
        result = rule_to_code(rule)
        if result["valid"]:
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(result["code"])
                print(f"已生成: {args.output}")
            else:
                print(result["code"])
        else:
            print("代码验证失败:", file=sys.stderr)
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "validate":
        with open(args.code, encoding="utf-8") as f:
            code = f.read()
        check = validate_code(code)
        if check["valid"]:
            print("验证通过")
        else:
            print("验证失败:")
            for e in check["errors"]:
                print(f"  - {e}")
            sys.exit(1)

    elif args.cmd == "demo":
        demo_rule = {
            "name": "demo_ema_rsi",
            "entry": {
                "conditions": ["ema_cross", "rsi_extreme"],
                "logic": "AND",
            },
            "exit": {
                "stop_loss_atr_mult": 1.5,
                "take_profit_atr_mult": 2.5,
                "time_stop_bars": 12,
            },
            "params": {
                "ema_cross_fast": 9,
                "ema_cross_slow": 21,
                "rsi_extreme_period": 14,
                "rsi_extreme_oversold": 30,
                "rsi_extreme_overbought": 70,
            },
            "direction": "both",
        }
        result = rule_to_code(demo_rule)
        print(result["code"])
        print(f"\n# 验证: {'通过' if result['valid'] else '失败 ' + str(result['errors'])}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
