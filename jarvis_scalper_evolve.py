#!/usr/bin/env python3
"""贾维斯 JARVIS — LLM 策略进化引擎（S-05 核心）。

自动循环「生成策略 → 回测 → 分析 → 调整」，每轮不重复犯错。

核心机制：
  1. LLM 根据因子菜单 + 历史教训生成新策略规则
  2. 查重门禁：与墓地中策略相似度 > 阈值则重新生成
  3. 代码生成器拼装 QD 策略代码
  4. QD API 回测
  5. 分析器归类结果
  6. 达标 → 名人堂 + OOS 验证；未达标 → 墓地 + 下一轮

数据存储：
  - ~/.vibe-trading/scalper_graveyard.json  — 失败策略墓地
  - ~/.vibe-trading/scalper_hall_of_fame.json — 达标策略名人堂

用法：
  python jarvis_scalper_evolve.py evolve --symbol BTCUSDT --rounds 10
  python jarvis_scalper_evolve.py hall-of-fame
  python jarvis_scalper_evolve.py graveyard
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
from typing import Any

import jarvis_scalper_features as sf
import jarvis_scalper_codegen as codegen
import jarvis_scalper_backtest as bt
import jarvis_scalper_analyzer as analyzer

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
GRAVEYARD_PATH = os.path.join(CONFIG_DIR, "scalper_graveyard.json")
HOF_PATH = os.path.join(CONFIG_DIR, "scalper_hall_of_fame.json")
LOG_PATH = os.path.join(CONFIG_DIR, "jarvis_scalper_evolve.log")

DEFAULT_CRITERIA = {
    "min_win_rate": 52.0,
    "min_profit_factor": 1.2,
    "max_drawdown_pct": 15.0,
    "min_total_return_pct": 0.0,
    "min_trades": 20,
}


def _log(msg: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [EVOLVE] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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


def _build_evolve_prompt(
    round_num: int,
    last_analysis: dict | None,
    graveyard: list[dict],
    hof: list[dict],
) -> str:
    """构建进化 prompt。"""
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
    """用 LLM 生成一个新策略规则。

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


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="LLM 策略进化引擎")
    sub = parser.add_subparsers(dest="cmd")

    p_evolve = sub.add_parser("evolve", help="启动进化循环")
    p_evolve.add_argument("--symbol", default="BTCUSDT")
    p_evolve.add_argument("--rounds", type=int, default=10)
    p_evolve.add_argument("--timeframe", default="15m")
    p_evolve.add_argument("--start", default="2025-01-01")
    p_evolve.add_argument("--end", default="2026-06-01")

    sub.add_parser("hall-of-fame", help="查看达标策略")
    sub.add_parser("graveyard", help="查看失败策略墓地")
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

    elif args.cmd == "status":
        gy = load_graveyard()
        hof = load_hall_of_fame()
        llm = _llm_config()
        qd = bt.check_qd_health()

        print("=== 进化引擎状态 ===")
        print(f"  LLM: {'已配置 (' + llm['model'] + ')' if llm else '未配置'}")
        print(f"  QD:  {'在线' if qd.get('healthy') else '离线'}")
        print(f"  名人堂: {len(hof)} 个策略")
        print(f"  墓地:   {len(gy)} 个策略")

        if hof:
            best = get_best_strategy()
            if best:
                print(f"  最佳策略: {best['name']} (夏普 {best.get('result',{}).get('sharpe_ratio',0):.2f})")

    elif args.cmd == "clear":
        confirm = input("确认清空所有墓地和名人堂数据？(yes/no): ")
        if confirm.lower() == "yes":
            save_graveyard([])
            save_hall_of_fame([])
            print("已清空")
        else:
            print("取消")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
