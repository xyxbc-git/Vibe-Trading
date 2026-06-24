#!/usr/bin/env python3
"""贾维斯 JARVIS — 15m 短线特征工厂（S-01）。

定义所有可用于 15 分钟短线交易的技术因子"菜单"，供 LLM 进化引擎和
策略代码生成器使用。每个因子包含：

  - 唯一 ID、分类、中文描述
  - 可调参数及其合法范围
  - 计算代码片段（Pandas，可直接嵌入 QD IndicatorStrategy）
  - 信号方向（做多 / 做空 / 双向）

设计原则：
  - 因子是最小可组合单元，LLM 从菜单中挑选 2-4 个组合成策略。
  - 参数范围经过 15m 级别校准（不是日线参数）。
  - 代码片段使用 df/pd/np，与 QD 沙盒环境兼容。

用法：
  python jarvis_scalper_features.py list            # 列出所有因子
  python jarvis_scalper_features.py show ema_cross   # 查看单个因子详情
  python jarvis_scalper_features.py menu --json      # 输出完整菜单 JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


# ═══════════════════════════ 因子注册表 ═══════════════════════════

FEATURES: dict[str, dict[str, Any]] = {}


def _reg(fid: str, **kw: Any) -> None:
    """注册一个因子到全局表。"""
    kw["feature_id"] = fid
    FEATURES[fid] = kw


# ────────────────────── 趋势类 ──────────────────────

_reg(
    "ema_cross",
    category="trend",
    name="EMA 金叉/死叉",
    description="快慢 EMA 交叉：快线上穿慢线做多，下穿做空",
    direction="both",
    params={"fast": 9, "slow": 21},
    param_ranges={"fast": [5, 30], "slow": [15, 60]},
    code_long="(df['close'].ewm(span={fast}).mean() > df['close'].ewm(span={slow}).mean())",
    code_short="(df['close'].ewm(span={fast}).mean() < df['close'].ewm(span={slow}).mean())",
)

_reg(
    "supertrend",
    category="trend",
    name="SuperTrend 趋势",
    description="基于 ATR 的自适应趋势通道，突破上轨做多、跌破下轨做空",
    direction="both",
    params={"period": 10, "multiplier": 3.0},
    param_ranges={"period": [7, 21], "multiplier": [1.5, 5.0]},
    code_long=(
        "# SuperTrend 计算\n"
        "_atr_st = df['close'].rolling({period}).std() * {multiplier}\n"
        "_mid_st = (df['high'] + df['low']) / 2\n"
        "_upper_st = _mid_st + _atr_st\n"
        "_lower_st = _mid_st - _atr_st\n"
        "(df['close'] > _lower_st)"
    ),
    code_short=(
        "(df['close'] < _upper_st)"
    ),
)

_reg(
    "macd_histogram",
    category="trend",
    name="MACD 柱状图",
    description="MACD 柱由负转正做多，由正转负做空",
    direction="both",
    params={"fast": 12, "slow": 26, "signal": 9},
    param_ranges={"fast": [8, 16], "slow": [20, 34], "signal": [7, 12]},
    code_long=(
        "_ema_f = df['close'].ewm(span={fast}).mean()\n"
        "_ema_s = df['close'].ewm(span={slow}).mean()\n"
        "_macd = _ema_f - _ema_s\n"
        "_macd_signal = _macd.ewm(span={signal}).mean()\n"
        "_macd_hist = _macd - _macd_signal\n"
        "(_macd_hist > 0) & (_macd_hist.shift(1) <= 0)"
    ),
    code_short=(
        "(_macd_hist < 0) & (_macd_hist.shift(1) >= 0)"
    ),
)

_reg(
    "triple_ema",
    category="trend",
    name="三重 EMA 排列",
    description="EMA5 > EMA13 > EMA34 多头排列做多，反之做空",
    direction="both",
    params={"e1": 5, "e2": 13, "e3": 34},
    param_ranges={"e1": [3, 8], "e2": [10, 18], "e3": [25, 50]},
    code_long=(
        "_e1 = df['close'].ewm(span={e1}).mean()\n"
        "_e2 = df['close'].ewm(span={e2}).mean()\n"
        "_e3 = df['close'].ewm(span={e3}).mean()\n"
        "(_e1 > _e2) & (_e2 > _e3)"
    ),
    code_short=(
        "(_e1 < _e2) & (_e2 < _e3)"
    ),
)

# ────────────────────── 动量类 ──────────────────────

_reg(
    "rsi_extreme",
    category="momentum",
    name="RSI 超买超卖",
    description="RSI 跌入超卖区后回升做多，进入超买区后回落做空",
    direction="both",
    params={"period": 14, "oversold": 30, "overbought": 70},
    param_ranges={"period": [6, 21], "oversold": [20, 35], "overbought": [65, 80]},
    code_long=(
        "_delta = df['close'].diff()\n"
        "_gain = _delta.clip(lower=0).rolling({period}).mean()\n"
        "_loss = (-_delta.clip(upper=0)).rolling({period}).mean()\n"
        "_rs = _gain / _loss.replace(0, 1e-10)\n"
        "_rsi = 100 - 100 / (1 + _rs)\n"
        "(_rsi > {oversold}) & (_rsi.shift(1) <= {oversold})"
    ),
    code_short=(
        "(_rsi < {overbought}) & (_rsi.shift(1) >= {overbought})"
    ),
)

_reg(
    "stochastic",
    category="momentum",
    name="Stochastic 随机指标",
    description="KD 金叉且 K 值从超卖区回升做多，死叉且从超买区回落做空",
    direction="both",
    params={"k_period": 14, "d_period": 3, "oversold": 20, "overbought": 80},
    param_ranges={"k_period": [5, 21], "d_period": [3, 5], "oversold": [15, 30], "overbought": [70, 85]},
    code_long=(
        "_low_min = df['low'].rolling({k_period}).min()\n"
        "_high_max = df['high'].rolling({k_period}).max()\n"
        "_k = 100 * (df['close'] - _low_min) / (_high_max - _low_min + 1e-10)\n"
        "_d = _k.rolling({d_period}).mean()\n"
        "(_k > _d) & (_k.shift(1) <= _d.shift(1)) & (_k < {overbought})"
    ),
    code_short=(
        "(_k < _d) & (_k.shift(1) >= _d.shift(1)) & (_k > {oversold})"
    ),
)

_reg(
    "williams_r",
    category="momentum",
    name="Williams %R",
    description="Williams %R 从超卖区（<-80）回升做多，从超买区（>-20）回落做空",
    direction="both",
    params={"period": 14, "oversold": -80, "overbought": -20},
    param_ranges={"period": [7, 21], "oversold": [-90, -70], "overbought": [-30, -10]},
    code_long=(
        "_hh = df['high'].rolling({period}).max()\n"
        "_ll = df['low'].rolling({period}).min()\n"
        "_wr = -100 * (_hh - df['close']) / (_hh - _ll + 1e-10)\n"
        "(_wr > {oversold}) & (_wr.shift(1) <= {oversold})"
    ),
    code_short=(
        "(_wr < {overbought}) & (_wr.shift(1) >= {overbought})"
    ),
)

_reg(
    "momentum_burst",
    category="momentum",
    name="动量突破",
    description="价格变化率突破阈值做多，跌破负阈值做空",
    direction="both",
    params={"period": 10, "threshold": 1.5},
    param_ranges={"period": [5, 20], "threshold": [0.8, 3.0]},
    code_long=(
        "_roc = (df['close'] / df['close'].shift({period}) - 1) * 100\n"
        "(_roc > {threshold}) & (_roc.shift(1) <= {threshold})"
    ),
    code_short=(
        "(_roc < -{threshold}) & (_roc.shift(1) >= -{threshold})"
    ),
)

# ────────────────────── 波动率类 ──────────────────────

_reg(
    "bollinger_breakout",
    category="volatility",
    name="布林带突破",
    description="价格突破布林带上轨做多，跌破下轨做空",
    direction="both",
    params={"period": 20, "std_dev": 2.0},
    param_ranges={"period": [14, 30], "std_dev": [1.5, 3.0]},
    code_long=(
        "_bb_mid = df['close'].rolling({period}).mean()\n"
        "_bb_std = df['close'].rolling({period}).std()\n"
        "_bb_upper = _bb_mid + {std_dev} * _bb_std\n"
        "_bb_lower = _bb_mid - {std_dev} * _bb_std\n"
        "(df['close'] > _bb_upper) & (df['close'].shift(1) <= _bb_upper.shift(1))"
    ),
    code_short=(
        "(df['close'] < _bb_lower) & (df['close'].shift(1) >= _bb_lower.shift(1))"
    ),
)

_reg(
    "bollinger_squeeze",
    category="volatility",
    name="布林带收缩后突破",
    description="布林带带宽收窄到阈值后价格突破方向做单",
    direction="both",
    params={"period": 20, "std_dev": 2.0, "squeeze_pct": 2.0},
    param_ranges={"period": [14, 30], "std_dev": [1.5, 3.0], "squeeze_pct": [1.0, 4.0]},
    code_long=(
        "_bb_mid_sq = df['close'].rolling({period}).mean()\n"
        "_bb_std_sq = df['close'].rolling({period}).std()\n"
        "_bb_width = (2 * {std_dev} * _bb_std_sq) / (_bb_mid_sq + 1e-10) * 100\n"
        "_squeeze = _bb_width < {squeeze_pct}\n"
        "_squeeze & (df['close'] > _bb_mid_sq)"
    ),
    code_short=(
        "_squeeze & (df['close'] < _bb_mid_sq)"
    ),
)

_reg(
    "atr_channel",
    category="volatility",
    name="ATR 通道突破",
    description="价格突破 EMA+ATR 倍数通道做多，跌破做空",
    direction="both",
    params={"ema_period": 20, "atr_period": 14, "atr_mult": 1.5},
    param_ranges={"ema_period": [10, 30], "atr_period": [7, 21], "atr_mult": [1.0, 3.0]},
    code_long=(
        "_ema_ch = df['close'].ewm(span={ema_period}).mean()\n"
        "_tr = pd.concat([df['high']-df['low'], (df['high']-df['close'].shift(1)).abs(), (df['low']-df['close'].shift(1)).abs()], axis=1).max(axis=1)\n"
        "_atr_ch = _tr.rolling({atr_period}).mean()\n"
        "_ch_upper = _ema_ch + {atr_mult} * _atr_ch\n"
        "_ch_lower = _ema_ch - {atr_mult} * _atr_ch\n"
        "(df['close'] > _ch_upper) & (df['close'].shift(1) <= _ch_upper.shift(1))"
    ),
    code_short=(
        "(df['close'] < _ch_lower) & (df['close'].shift(1) >= _ch_lower.shift(1))"
    ),
)

_reg(
    "keltner_channel",
    category="volatility",
    name="Keltner 通道",
    description="价格突破 Keltner 上轨做多，跌破下轨做空",
    direction="both",
    params={"ema_period": 20, "atr_period": 10, "atr_mult": 2.0},
    param_ranges={"ema_period": [10, 30], "atr_period": [7, 14], "atr_mult": [1.0, 3.0]},
    code_long=(
        "_ema_kc = df['close'].ewm(span={ema_period}).mean()\n"
        "_tr_kc = pd.concat([df['high']-df['low'], (df['high']-df['close'].shift(1)).abs(), (df['low']-df['close'].shift(1)).abs()], axis=1).max(axis=1)\n"
        "_atr_kc = _tr_kc.rolling({atr_period}).mean()\n"
        "_kc_upper = _ema_kc + {atr_mult} * _atr_kc\n"
        "_kc_lower = _ema_kc - {atr_mult} * _atr_kc\n"
        "(df['close'] > _kc_upper) & (df['close'].shift(1) <= _kc_upper.shift(1))"
    ),
    code_short=(
        "(df['close'] < _kc_lower) & (df['close'].shift(1) >= _kc_lower.shift(1))"
    ),
)

# ────────────────────── 成交量类 ──────────────────────

_reg(
    "volume_spike",
    category="volume",
    name="量比突增",
    description="当前成交量超过均量 N 倍，确认趋势有效性",
    direction="filter",
    params={"period": 20, "spike_ratio": 1.5},
    param_ranges={"period": [10, 50], "spike_ratio": [1.2, 3.0]},
    code_long=(
        "_vol_ma = df['volume'].rolling({period}).mean()\n"
        "(df['volume'] > {spike_ratio} * _vol_ma)"
    ),
    code_short=(
        "(df['volume'] > {spike_ratio} * _vol_ma)"
    ),
)

_reg(
    "obv_divergence",
    category="volume",
    name="OBV 背离",
    description="价格创新低但 OBV 不创新低做多（底背离），反之做空（顶背离）",
    direction="both",
    params={"lookback": 20},
    param_ranges={"lookback": [10, 40]},
    code_long=(
        "_obv = (np.sign(df['close'].diff()) * df['volume']).cumsum()\n"
        "_price_new_low = df['close'] == df['close'].rolling({lookback}).min()\n"
        "_obv_not_low = _obv > _obv.rolling({lookback}).min()\n"
        "_price_new_low & _obv_not_low"
    ),
    code_short=(
        "_price_new_high = df['close'] == df['close'].rolling({lookback}).max()\n"
        "_obv_not_high = _obv < _obv.rolling({lookback}).max()\n"
        "_price_new_high & _obv_not_high"
    ),
)

_reg(
    "vwap_reversion",
    category="volume",
    name="VWAP 回归",
    description="价格偏离 VWAP 后回归：跌破后回穿做多，突破后回落做空",
    direction="both",
    params={"deviation_pct": 0.5},
    param_ranges={"deviation_pct": [0.2, 1.5]},
    code_long=(
        "_vwap = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()\n"
        "_below = df['close'] < _vwap * (1 - {deviation_pct}/100)\n"
        "(df['close'] > _vwap) & _below.shift(1)"
    ),
    code_short=(
        "_above = df['close'] > _vwap * (1 + {deviation_pct}/100)\n"
        "(df['close'] < _vwap) & _above.shift(1)"
    ),
)

# ────────────────────── 微结构类（加密专属） ──────────────────────

_reg(
    "funding_rate_extreme",
    category="microstructure",
    name="资金费率极值",
    description="资金费率达到极端负值做多（空头拥挤），极端正值做空（多头拥挤）",
    direction="both",
    params={"threshold_pct": 0.05},
    param_ranges={"threshold_pct": [0.02, 0.15]},
    code_long="# 需要外部数据注入 df['funding_rate']\n(df.get('funding_rate', pd.Series(0, index=df.index)) < -{threshold_pct}/100)",
    code_short="(df.get('funding_rate', pd.Series(0, index=df.index)) > {threshold_pct}/100)",
)

_reg(
    "long_short_ratio_shift",
    category="microstructure",
    name="多空比突变",
    description="多空比骤降做多（逆向），骤升做空（逆向）",
    direction="both",
    params={"lookback": 10, "change_pct": 15},
    param_ranges={"lookback": [5, 20], "change_pct": [8, 25]},
    code_long="# 需要外部数据注入 df['ls_ratio']\n_ls = df.get('ls_ratio', pd.Series(1, index=df.index))\n_ls_change = (_ls / _ls.rolling({lookback}).mean() - 1) * 100\n(_ls_change < -{change_pct})",
    code_short="(_ls_change > {change_pct})",
)

_reg(
    "oi_surge",
    category="microstructure",
    name="OI 骤变",
    description="未平仓量急剧增加配合价格上涨做多，配合价格下跌做空",
    direction="both",
    params={"lookback": 10, "surge_pct": 10},
    param_ranges={"lookback": [5, 20], "surge_pct": [5, 25]},
    code_long="# 需要外部数据注入 df['open_interest']\n_oi = df.get('open_interest', pd.Series(0, index=df.index))\n_oi_chg = (_oi / _oi.rolling({lookback}).mean() - 1) * 100\n(_oi_chg > {surge_pct}) & (df['close'] > df['close'].shift(1))",
    code_short="(_oi_chg > {surge_pct}) & (df['close'] < df['close'].shift(1))",
)


# ═══════════════════════════ 公开 API ═══════════════════════════

def list_features() -> list[dict[str, Any]]:
    """返回所有因子的摘要列表。"""
    result = []
    for f in FEATURES.values():
        result.append({
            "feature_id": f["feature_id"],
            "category": f["category"],
            "name": f["name"],
            "direction": f["direction"],
            "description": f["description"],
        })
    return result


def get_feature(feature_id: str) -> dict[str, Any] | None:
    """返回单个因子的完整信息。"""
    return FEATURES.get(feature_id)


def get_menu_json() -> str:
    """返回完整因子菜单的 JSON 字符串（供 LLM 读取）。"""
    menu = []
    for f in FEATURES.values():
        menu.append({
            "feature_id": f["feature_id"],
            "category": f["category"],
            "name": f["name"],
            "description": f["description"],
            "direction": f["direction"],
            "params": f["params"],
            "param_ranges": f["param_ranges"],
        })
    return json.dumps(menu, ensure_ascii=False, indent=2)


def get_categories() -> dict[str, list[str]]:
    """返回 {分类: [因子ID列表]} 映射。"""
    cats: dict[str, list[str]] = {}
    for f in FEATURES.values():
        cats.setdefault(f["category"], []).append(f["feature_id"])
    return cats


def get_code_snippet(feature_id: str, direction: str, params: dict[str, Any] | None = None) -> str | None:
    """返回指定因子的代码片段，参数已替换。

    Args:
        feature_id: 因子 ID
        direction: "long" 或 "short"
        params: 参数字典（不传则用默认值）
    """
    f = FEATURES.get(feature_id)
    if not f:
        return None
    key = f"code_{direction}"
    template = f.get(key)
    if not template:
        return None
    p = dict(f["params"])
    if params:
        p.update(params)
    return template.format(**p)


# ═══════════════════════════ CLI ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="15m 短线特征工厂")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出所有因子")
    p_show = sub.add_parser("show", help="查看单个因子详情")
    p_show.add_argument("feature_id")
    p_menu = sub.add_parser("menu", help="输出完整菜单")
    p_menu.add_argument("--json", action="store_true", help="JSON 格式")

    args = parser.parse_args()

    if args.cmd == "list":
        cats = get_categories()
        for cat, ids in cats.items():
            print(f"\n{'='*40}")
            print(f"  {cat.upper()} ({len(ids)} 个因子)")
            print(f"{'='*40}")
            for fid in ids:
                f = FEATURES[fid]
                print(f"  {fid:25s} {f['name']:15s} {f['direction']:6s}  {f['description']}")
        print(f"\n共 {len(FEATURES)} 个因子，{len(cats)} 个分类")

    elif args.cmd == "show":
        f = get_feature(args.feature_id)
        if not f:
            print(f"因子 '{args.feature_id}' 不存在", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(f, ensure_ascii=False, indent=2, default=str))

    elif args.cmd == "menu":
        if args.json:
            print(get_menu_json())
        else:
            for f in list_features():
                print(f"[{f['category']:15s}] {f['feature_id']:25s} {f['name']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
