#!/usr/bin/env python3
"""贾维斯 JARVIS - 可学习因子权重中心（自进化闭环的地基）。

把原先散落在 `jarvis_brief.score_and_plan` / `jarvis_journal._price_only_decision`
里的硬编码因子权重与方向阈值，集中成一份「可被重训覆盖」的配置：

  默认值（DEFAULT_WEIGHTS / DEFAULT_THRESHOLDS）= 现有硬编码原值，
  因此「配置文件不存在 / 损坏 / 缺键」时行为与改造前**完全一致**（零回归、可回退）。

自进化闭环里它扮演「记忆参数层」：
  jarvis_retrain 读历史战绩 → 温和调整这些权重 → save() 落盘 →
  jarvis_brief 下次 load() 即用新权重决策 → 越跑越贴合真实表现。

存储：~/.vibe-trading/jarvis_weights.json
  {
    "weights":    {<因子名>: float, ...},
    "thresholds": {"long": 0.8, "short": -0.8},
    "meta":       {"version": int, "updated_at": str, "source": str, "note": str}
  }

设计原则：
  - 永不抛出：任何读取异常都回退到内置默认（决策链不能被配置拖垮）。
  - 缺键补全：只覆盖配置里出现的键，其余用默认，便于增量演进。
  - 可追责：每次 save 累加 version 并记 updated_at/source，便于审计与回退。

用法：
  python jarvis_weights.py show           # 查看当前生效权重（含来源：default/file）
  python jarvis_weights.py diff           # 对比当前配置 vs 内置默认
  python jarvis_weights.py reset          # 删除配置文件，恢复内置默认
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
CONFIG_PATH = os.path.join(CONFIG_DIR, "jarvis_weights.json")

# ── 内置默认 = 改造前 jarvis_brief / jarvis_journal 的硬编码原值 ───────────────
# 改这里要同步 brief / journal 的语义注释；数值变化会直接改变决策口径。
DEFAULT_WEIGHTS: dict[str, float] = {
    "dd30_dip": 0.5,               # 距高点回撤≤-30%（弱抄底因子，样本外 +0.46%/胜率55%）
    "fear_in_downtrend": 0.6,      # F&G<20 且 价<200MA（下跌中恐惧，历史30天胜率62.9%）
    "fear_in_uptrend": -0.3,       # F&G<20 且 价>200MA（牛市中恐惧，历史为利空）
    "funding_overheated_short": 0.4,   # 资金费率深度转负（空头拥挤，易轧空）
    "funding_overheated_long": -0.4,   # 资金费率过热（多头拥挤，回调风险）
    "ma200_above": 0.3,            # 价>200MA（中期多头结构）
    "ma200_below": -0.2,           # 价<200MA（中期偏弱）
    "breakout_20d": 0.6,           # 创20日新高·正交动量因子（T-04）
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "long": 0.8,    # 信心分 ≥ long → 偏多（战术）
    "short": -0.8,  # 信心分 ≤ short → 偏空/观望
}

# 价格-only 因子集合（jarvis_journal 历史回填只能复现这些；衍生品 funding 难回溯）。
PRICE_ONLY_FACTORS = ("dd30_dip", "fear_in_downtrend", "fear_in_uptrend", "ma200_above", "ma200_below")

# 重训护栏：单因子权重允许的绝对区间，避免重训把权重调到离谱或反号失控。
WEIGHT_BOUNDS: dict[str, tuple[float, float]] = {
    "dd30_dip": (0.0, 1.2),
    "fear_in_downtrend": (0.0, 1.2),
    "fear_in_uptrend": (-1.0, 0.0),
    "funding_overheated_short": (0.0, 1.0),
    "funding_overheated_long": (-1.0, 0.0),
    "ma200_above": (0.0, 0.8),
    "ma200_below": (-0.8, 0.0),
    "breakout_20d": (0.0, 1.2),
}


def default_config() -> dict:
    """返回内置默认配置的深拷贝（外部可安全修改）。"""
    return {
        "weights": copy.deepcopy(DEFAULT_WEIGHTS),
        "thresholds": copy.deepcopy(DEFAULT_THRESHOLDS),
        "meta": {"version": 0, "updated_at": None, "source": "builtin-default", "note": ""},
    }


def load(path: str | None = None) -> dict:
    """读取生效配置；缺失/损坏/缺键时回退默认。永不抛出。

    返回结构恒含 weights / thresholds / meta 三键，且 weights/thresholds
    一定包含全部默认键（文件里多出的未知键会被保留，便于前向兼容）。
    """
    cfg = default_config()
    p = path or CONFIG_PATH
    try:
        if not os.path.exists(p):
            return cfg
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # noqa: BLE001 — 配置异常绝不能拖垮决策，回退默认
        cfg["meta"]["source"] = "default(load-error)"
        return cfg

    if not isinstance(raw, dict):
        cfg["meta"]["source"] = "default(bad-shape)"
        return cfg

    fw = raw.get("weights")
    if isinstance(fw, dict):
        for k, v in fw.items():
            if isinstance(v, (int, float)):
                cfg["weights"][k] = float(v)
    ft = raw.get("thresholds")
    if isinstance(ft, dict):
        for k, v in ft.items():
            if isinstance(v, (int, float)):
                cfg["thresholds"][k] = float(v)
    fm = raw.get("meta")
    if isinstance(fm, dict):
        cfg["meta"].update(fm)
    cfg["meta"]["source"] = cfg["meta"].get("source") or "file"
    if cfg["meta"].get("source") in ("builtin-default", None):
        cfg["meta"]["source"] = "file"
    return cfg


def get_weights(path: str | None = None) -> dict[str, float]:
    """便捷：只取生效的权重字典。"""
    return load(path)["weights"]


def get_thresholds(path: str | None = None) -> dict[str, float]:
    """便捷：只取生效的方向阈值。"""
    return load(path)["thresholds"]


def clamp_weight(name: str, value: float) -> float:
    """把某因子权重夹到护栏区间内（无定义边界则原样返回）。"""
    lo, hi = WEIGHT_BOUNDS.get(name, (-2.0, 2.0))
    return round(max(lo, min(hi, float(value))), 4)


def save(
    weights: dict[str, float],
    thresholds: dict[str, float] | None = None,
    *,
    source: str = "manual",
    note: str = "",
    path: str | None = None,
) -> dict:
    """落盘新配置：自动夹护栏 + 累加 version + 记录来源/时间。返回写入后的配置。"""
    p = path or CONFIG_PATH
    prev = load(p)
    new_weights = dict(prev["weights"])
    for k, v in (weights or {}).items():
        if isinstance(v, (int, float)):
            new_weights[k] = clamp_weight(k, v)
    new_thresholds = dict(prev["thresholds"])
    for k, v in (thresholds or {}).items():
        if isinstance(v, (int, float)):
            new_thresholds[k] = float(v)
    cfg = {
        "weights": new_weights,
        "thresholds": new_thresholds,
        "meta": {
            "version": int(prev["meta"].get("version", 0) or 0) + 1,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "note": note,
        },
    }
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)  # 原子替换，避免半写坏配置
    return cfg


def reset(path: str | None = None) -> bool:
    """删除配置文件，恢复内置默认。返回是否真的删了文件。"""
    p = path or CONFIG_PATH
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def diff_from_default(path: str | None = None) -> dict:
    """当前生效配置相对内置默认的差异（只列变化项），用于审计。"""
    cur = load(path)
    out: dict[str, dict] = {"weights": {}, "thresholds": {}}
    for k, dv in DEFAULT_WEIGHTS.items():
        cv = cur["weights"].get(k, dv)
        if round(cv, 6) != round(dv, 6):
            out["weights"][k] = {"default": dv, "current": cv, "delta": round(cv - dv, 4)}
    for k, dv in DEFAULT_THRESHOLDS.items():
        cv = cur["thresholds"].get(k, dv)
        if round(cv, 6) != round(dv, 6):
            out["thresholds"][k] = {"default": dv, "current": cv, "delta": round(cv - dv, 4)}
    out["meta"] = cur["meta"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯可学习因子权重中心")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="查看当前生效权重")
    sub.add_parser("diff", help="对比当前配置 vs 内置默认")
    sub.add_parser("reset", help="删除配置，恢复内置默认")
    args = ap.parse_args()

    if args.cmd == "show":
        cfg = load()
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
    elif args.cmd == "diff":
        print(json.dumps(diff_from_default(), ensure_ascii=False, indent=2))
    elif args.cmd == "reset":
        removed = reset()
        print("✅ 已删除配置，恢复内置默认" if removed else "（配置本就不存在，当前即内置默认）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
