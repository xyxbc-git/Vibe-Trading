#!/usr/bin/env python3
"""贾维斯 JARVIS - 统一交易配置中心（T-15）。

把原先散落在各脚本里的「交易运行参数」集中成一份可改不改码的配置：
币种池、信心阈值、仓位上限、组合风险红线、止损/止盈/时间止损规则、
入场区间带、sizing 方式（固定比例 / 凯利，配合 T-11）。

与 `jarvis_weights.py` 的分工：
  - jarvis_weights：**因子权重 + 方向阈值**（由 jarvis_retrain 自学习覆盖）。
  - jarvis_config ：**交易执行/风控旋钮 + 币种池**（人工运营调参，改配置即生效）。

默认值（DEFAULTS）= 各脚本改造前的硬编码原值，因此「配置文件不存在 /
损坏 / 缺键」时行为与改造前**完全一致**（零回归、可一键回退）。

存储：~/.vibe-trading/jarvis_config.json
  { "<key>": <value>, ..., "meta": {...} }

设计原则（与 jarvis_weights 一致）：
  - 永不抛出：任何读取异常都回退内置默认（决策链不能被配置拖垮）。
  - 缺键补全：只覆盖配置里出现的键，其余用默认，便于增量演进。
  - 护栏夹紧：写入时把关键风控旋钮夹到安全区间，避免改出离谱值。

用法：
  python jarvis_config.py show              # 查看当前生效配置（含来源 default/file）
  python jarvis_config.py get watchlist     # 取单个键
  python jarvis_config.py set min_conviction 0.85    # 改单个键（自动夹护栏）
  python jarvis_config.py diff              # 对比当前 vs 内置默认
  python jarvis_config.py reset             # 删除配置，恢复内置默认
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

CONFIG_DIR = os.path.expanduser("~/.vibe-trading")
CONFIG_PATH = os.path.join(CONFIG_DIR, "jarvis_config.json")

# ── 内置默认 = 各脚本改造前的硬编码原值（改这里会直接改变运行口径）────────────
DEFAULTS: dict = {
    # 币种池（与 jarvis_radar.DEFAULT_WATCHLIST 原值一致）
    "watchlist": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"],
    # 执行 / 雷达共享旋钮
    "min_conviction": 0.8,            # 偏多达标信心阈值（executor 护栏 + radar）
    "max_position_pct": 40.0,         # 单笔仓位上限%（与 brief 弱因子上限一致）
    "max_portfolio_risk_pct": 1.5,    # 组合最大风险红线%（仓位×止损幅度）
    "max_effective_pct": 50.0,        # 多币相关性有效敞口上限%（T-10）
    "account_equity_usdt": 1000.0,    # 账户权益（sizing 用）
    # 止损 / 止盈 / 时间止损规则（brief 原硬编码 0.90 / 1.08 / 30）
    "stop_loss_drop_pct": 10.0,       # 硬止损：入场价 ×(1-10%)
    "take_profit_pct": 8.0,           # 参考止盈：入场价 ×(1+8%)
    "time_stop_days": 30,             # 时间止损（因子是 30 天均值回归）
    # 入场区间带（brief 原 price*0.985 ~ price*1.005）
    "entry_band_below_pct": 1.5,      # 区间下沿：低于现价的百分比
    "entry_band_above_pct": 0.5,      # 区间上沿：高于现价的百分比
    # sizing（T-11 动态仓位）
    "sizing_method": "fixed",         # fixed=固定比例（默认，零回归）| kelly=分数凯利
    "kelly_fraction": 0.5,            # 分数凯利系数（0~1，越小越保守）
    # ── 4h 盘中引擎（jarvis_intraday_trader，扁平键便于 clamp 护栏）──────────
    "intraday_enabled": True,             # 总开关（关=心跳里跳过 4h 轮）
    "intraday_min_prob": 0.60,            # 开仓最低预测概率
    "intraday_risk_pct_per_trade": 1.0,   # 单笔风险占权益%（按止损距离反推仓位）
    "intraday_max_open_positions": 3,     # 同时最多持仓数
    "intraday_stop_atr_mult": 1.2,        # 止损 = 入场 ∓ 1.2×ATR
    "intraday_take_atr_mult": 1.8,        # 止盈 = 入场 ± 1.8×ATR
    "intraday_time_stop_bars": 6,         # 时间止损（6 根 4h = 24h）
    "intraday_cooldown_bars": 1,          # 平仓后同币冷却根数
    "intraday_max_consecutive_losses": 3, # 连亏 N 笔熔断停开仓
}

# 关键风控旋钮的安全区间（写入时夹紧；未列的键不夹）。
BOUNDS: dict[str, tuple[float, float]] = {
    "min_conviction": (0.0, 2.0),
    "max_position_pct": (0.0, 100.0),
    "max_portfolio_risk_pct": (0.1, 20.0),
    "max_effective_pct": (1.0, 100.0),
    "account_equity_usdt": (1.0, 1e9),
    "stop_loss_drop_pct": (0.5, 90.0),
    "take_profit_pct": (0.5, 500.0),
    "time_stop_days": (1, 3650),
    "entry_band_below_pct": (0.0, 50.0),
    "entry_band_above_pct": (0.0, 50.0),
    "kelly_fraction": (0.0, 1.0),
    "intraday_min_prob": (0.5, 0.99),
    "intraday_risk_pct_per_trade": (0.1, 5.0),
    "intraday_max_open_positions": (1, 10),
    "intraday_stop_atr_mult": (0.3, 5.0),
    "intraday_take_atr_mult": (0.3, 10.0),
    "intraday_time_stop_bars": (1, 42),
    "intraday_cooldown_bars": (0, 12),
    "intraday_max_consecutive_losses": (1, 20),
}

# 允许的枚举键。
ENUMS: dict[str, tuple[str, ...]] = {
    "sizing_method": ("fixed", "kelly"),
}


def default_config() -> dict:
    """返回内置默认配置的深拷贝（外部可安全修改）。"""
    cfg = copy.deepcopy(DEFAULTS)
    cfg["meta"] = {"version": 0, "updated_at": None, "source": "builtin-default", "note": ""}
    return cfg


def _coerce(key: str, value):
    """把值按默认键的类型温和转换；失败则原样返回（由调用方决定取舍）。"""
    dv = DEFAULTS.get(key)
    try:
        if isinstance(dv, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "开")
            return bool(value)
        if isinstance(dv, int) and not isinstance(dv, bool):
            return int(value)
        if isinstance(dv, float):
            return float(value)
        if isinstance(dv, list):
            if isinstance(value, list):
                return [str(x).strip().upper() for x in value if str(x).strip()]
            return [s.strip().upper() for s in str(value).split(",") if s.strip()]
        return value
    except Exception:  # noqa: BLE001
        return value


def clamp(key: str, value):
    """把数值键夹到安全区间；枚举键校验；其余原样返回。永不抛出。"""
    v = _coerce(key, value)
    try:
        if key in ENUMS:
            return v if v in ENUMS[key] else DEFAULTS.get(key)
        if key in BOUNDS and isinstance(v, (int, float)) and not isinstance(v, bool):
            lo, hi = BOUNDS[key]
            v2 = max(lo, min(hi, v))
            return int(v2) if isinstance(DEFAULTS.get(key), int) else round(float(v2), 6)
    except Exception:  # noqa: BLE001
        return DEFAULTS.get(key, value)
    return v


def load(path: str | None = None) -> dict:
    """读取生效配置；缺失/损坏/缺键回退默认。永不抛出。

    返回结构恒含全部默认键 + meta；文件里多出的未知键保留（前向兼容）。
    """
    cfg = default_config()
    p = path or CONFIG_PATH
    try:
        if not os.path.exists(p):
            return cfg
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # noqa: BLE001 — 配置异常绝不能拖垮决策
        cfg["meta"]["source"] = "default(load-error)"
        return cfg
    if not isinstance(raw, dict):
        cfg["meta"]["source"] = "default(bad-shape)"
        return cfg
    for k, v in raw.items():
        if k == "meta":
            if isinstance(v, dict):
                cfg["meta"].update(v)
            continue
        if k in DEFAULTS:
            cfg[k] = clamp(k, v)
        else:
            cfg[k] = v  # 未知键原样保留
    if cfg["meta"].get("source") in ("builtin-default", None):
        cfg["meta"]["source"] = "file"
    return cfg


def get(key: str, default=None, path: str | None = None):
    """取单个配置项；缺失时回退（显式 default > 内置默认）。"""
    cfg = load(path)
    if key in cfg:
        return cfg[key]
    return default if default is not None else DEFAULTS.get(key)


def get_all(path: str | None = None) -> dict:
    """取全部生效配置（含 meta）。"""
    return load(path)


def save(updates: dict, *, source: str = "manual", note: str = "", path: str | None = None) -> dict:
    """合并写入若干配置项：自动夹护栏 + 累加 version + 记录来源/时间。原子落盘。"""
    p = path or CONFIG_PATH
    prev = load(p)
    cfg = {k: v for k, v in prev.items() if k != "meta"}
    for k, v in (updates or {}).items():
        cfg[k] = clamp(k, v) if k in DEFAULTS else v
    cfg["meta"] = {
        "version": int(prev.get("meta", {}).get("version", 0) or 0) + 1,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "note": note,
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
    out: dict = {}
    for k, dv in DEFAULTS.items():
        cv = cur.get(k, dv)
        if cv != dv:
            out[k] = {"default": dv, "current": cv}
    out["meta"] = cur.get("meta")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯统一交易配置中心")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="查看当前生效配置")
    g = sub.add_parser("get", help="取单个键")
    g.add_argument("key")
    s = sub.add_parser("set", help="改单个键（自动夹护栏）")
    s.add_argument("key")
    s.add_argument("value")
    sub.add_parser("diff", help="对比当前 vs 内置默认")
    sub.add_parser("reset", help="删除配置，恢复内置默认")
    args = ap.parse_args()

    if args.cmd == "show":
        print(json.dumps(load(), ensure_ascii=False, indent=2))
    elif args.cmd == "get":
        print(json.dumps(get(args.key), ensure_ascii=False))
    elif args.cmd == "set":
        if args.key not in DEFAULTS:
            print(f"⚠️ 未知配置键 '{args.key}'（仍写入，但不在内置默认表）")
        cfg = save({args.key: args.value}, source="cli-set", note=f"set {args.key}")
        print(f"✅ 已写入 {args.key} = {cfg.get(args.key)}（version {cfg['meta']['version']}）")
    elif args.cmd == "diff":
        print(json.dumps(diff_from_default(), ensure_ascii=False, indent=2))
    elif args.cmd == "reset":
        print("✅ 已删除配置，恢复内置默认" if reset() else "（配置本就不存在，当前即内置默认）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
