#!/usr/bin/env python3
"""贾维斯 JARVIS — 复合策略框架（Composite Strategy）。

三层复合结构：
  第一层（进攻）：根据行情类型选择交易模式
    - 震荡 → 网格/马丁均价加仓高频盈利
    - 趋势 → 突破追踪 + 移动止盈
    - 突破 → 快速入场 + 紧凑止盈
  第二层（解困）：单边行情启动利润对冲
    - 逆势持仓浮亏过大时，顺势开对冲仓
    - 对冲仓盈利覆盖原仓亏损
  第三层（保命）：趋势过滤抑制逆势操作
    - 三均线共振时禁止逆势开仓
    - ADX 强趋势时关闭马丁加仓
    - 多时间框架方向一致性检查

依赖：
  - jarvis_regime_classifier（行情分类）
  - jarvis_scalper_features（因子菜单）

用法：
  策略框架不直接运行，由 jarvis_scalper_trader.py 调用。
  测试：python jarvis_composite_strategy.py test --symbol BTCUSDT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np
import pandas as pd

import jarvis_regime_classifier as rc


# ═══════════════════════════ 数据结构 ═══════════════════════════

@dataclass
class Decision:
    """交易决策。"""
    action: str          # open_long / open_short / close_long / close_short /
                         # add_long / add_short / hedge_long / hedge_short / hold
    size_pct: float      # 仓位占比 (0.0~1.0)
    stop_loss: float     # 止损价
    take_profit: float   # 止盈价
    trailing: bool       # 是否启用移动止损
    trailing_step: float # 移动止损步进（ATR 倍数）
    urgency: str         # high / medium / low
    layer: str           # attack / rescue / protect — 哪一层发出的决策
    reasoning: str       # 决策理由

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Position:
    """持仓记录。"""
    id: str
    symbol: str
    direction: str       # long / short
    entry_price: float
    qty: float
    size_usdt: float
    open_time: str
    bars_held: int = 0
    add_count: int = 0   # 加仓次数
    avg_price: float = 0.0
    total_qty: float = 0.0
    total_size: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    trailing_high: float = 0.0  # 移动止损追踪最高价
    trailing_low: float = 999999.0
    is_hedge: bool = False  # 是否为对冲仓
    hedge_for: str = ""     # 对冲哪个持仓的 ID

    def unrealized_pnl(self, current_price: float) -> float:
        price = self.avg_price if self.avg_price > 0 else self.entry_price
        qty = self.total_qty if self.total_qty > 0 else self.qty
        if self.direction == "long":
            return (current_price - price) * qty
        return (price - current_price) * qty

    def unrealized_pnl_pct(self, current_price: float) -> float:
        size = self.total_size if self.total_size > 0 else self.size_usdt
        if size == 0:
            return 0.0
        return self.unrealized_pnl(current_price) / size * 100


# ═══════════════════════════ ATR 动态止损 ═══════════════════════════

REGIME_SL_TP = {
    "ranging":  {"sl_mult": 1.0, "tp_mult": 1.5, "trailing": False, "trail_step": 0.0},
    "trending": {"sl_mult": 1.5, "tp_mult": 3.0, "trailing": True,  "trail_step": 0.5},
    "breakout": {"sl_mult": 1.2, "tp_mult": 2.0, "trailing": True,  "trail_step": 0.3},
}


def calc_dynamic_stops(
    entry_price: float,
    direction: str,
    atr: float,
    regime: str,
) -> dict[str, float]:
    """根据行情类型和 ATR 计算动态止盈止损。"""
    params = REGIME_SL_TP.get(regime, REGIME_SL_TP["ranging"])
    sl_dist = atr * params["sl_mult"]
    tp_dist = atr * params["tp_mult"]

    if direction == "long":
        sl = entry_price - sl_dist
        tp = entry_price + tp_dist
    else:
        sl = entry_price + sl_dist
        tp = entry_price - tp_dist

    return {
        "stop_loss": round(sl, 2),
        "take_profit": round(tp, 2),
        "trailing": params["trailing"],
        "trailing_step": params["trail_step"],
    }


def update_trailing_stop(
    position: Position,
    current_price: float,
    atr: float,
    regime: str,
) -> float | None:
    """更新移动止损，返回新的止损价（None 表示不变）。"""
    params = REGIME_SL_TP.get(regime, REGIME_SL_TP["trending"])
    if not params["trailing"]:
        return None

    trail_dist = atr * params["sl_mult"]

    if position.direction == "long":
        if current_price > position.trailing_high:
            position.trailing_high = current_price
        new_sl = position.trailing_high - trail_dist
        if new_sl > position.stop_loss:
            return round(new_sl, 2)
    else:
        if current_price < position.trailing_low:
            position.trailing_low = current_price
        new_sl = position.trailing_low + trail_dist
        if new_sl < position.stop_loss:
            return round(new_sl, 2)

    return None


# ═══════════════════════════ 第一层：进攻 ═══════════════════════════

class AttackLayer:
    """进攻层：根据行情类型选择交易模式。"""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.martin_max_adds = cfg.get("martin_max_adds", 3)
        self.martin_add_threshold_atr = cfg.get("martin_add_threshold_atr", 1.0)
        self.grid_tp_atr = cfg.get("grid_tp_atr", 0.5)

    def evaluate(
        self,
        regime: rc.RegimeResult,
        signals: dict[str, bool],
        positions: list[Position],
        current_price: float,
        atr: float,
        balance: float,
    ) -> Decision | None:
        """进攻层决策。"""
        open_pos = [p for p in positions if not p.is_hedge]

        if regime.regime == "ranging":
            return self._ranging_attack(regime, signals, open_pos, current_price, atr, balance)
        elif regime.regime == "trending":
            return self._trending_attack(regime, signals, open_pos, current_price, atr, balance)
        elif regime.regime == "breakout":
            return self._breakout_attack(regime, signals, open_pos, current_price, atr, balance)
        return None

    def _ranging_attack(
        self, regime, signals, positions, price, atr, balance,
    ) -> Decision | None:
        """震荡行情：网格/马丁模式。"""
        existing = [p for p in positions if p.direction in ("long", "short")]

        for pos in existing:
            if pos.add_count < self.martin_max_adds:
                pnl_pct = pos.unrealized_pnl_pct(price)
                if pnl_pct < -(self.martin_add_threshold_atr * atr / price * 100):
                    stops = calc_dynamic_stops(price, pos.direction, atr, "ranging")
                    new_avg = (pos.avg_price * pos.total_qty + price * pos.qty) / (pos.total_qty + pos.qty) if pos.total_qty > 0 else price
                    return Decision(
                        action=f"add_{pos.direction}",
                        size_pct=0.005,
                        stop_loss=stops["stop_loss"],
                        take_profit=round(new_avg + self.grid_tp_atr * atr if pos.direction == "long" else new_avg - self.grid_tp_atr * atr, 2),
                        trailing=False,
                        trailing_step=0.0,
                        urgency="medium",
                        layer="attack",
                        reasoning=f"震荡行情马丁加仓（第{pos.add_count+1}次），浮亏{pnl_pct:.1f}%，均价止盈",
                    )

        if signals.get("open_long") and not any(p.direction == "long" for p in existing):
            stops = calc_dynamic_stops(price, "long", atr, "ranging")
            return Decision(
                action="open_long", size_pct=0.01,
                stop_loss=stops["stop_loss"], take_profit=stops["take_profit"],
                trailing=False, trailing_step=0.0,
                urgency="medium", layer="attack",
                reasoning=f"震荡行情做多信号，ATR止损{stops['stop_loss']:.0f}",
            )
        if signals.get("open_short") and not any(p.direction == "short" for p in existing):
            stops = calc_dynamic_stops(price, "short", atr, "ranging")
            return Decision(
                action="open_short", size_pct=0.01,
                stop_loss=stops["stop_loss"], take_profit=stops["take_profit"],
                trailing=False, trailing_step=0.0,
                urgency="medium", layer="attack",
                reasoning=f"震荡行情做空信号，ATR止损{stops['stop_loss']:.0f}",
            )
        return None

    def _trending_attack(
        self, regime, signals, positions, price, atr, balance,
    ) -> Decision | None:
        """趋势行情：顺势追踪。"""
        trend_dir = regime.direction

        if trend_dir == "bullish" and signals.get("open_long"):
            if not any(p.direction == "long" for p in positions):
                stops = calc_dynamic_stops(price, "long", atr, "trending")
                return Decision(
                    action="open_long", size_pct=0.015,
                    stop_loss=stops["stop_loss"], take_profit=stops["take_profit"],
                    trailing=True, trailing_step=stops["trailing_step"],
                    urgency="high", layer="attack",
                    reasoning=f"趋势行情顺势做多，移动止盈追踪趋势",
                )

        if trend_dir == "bearish" and signals.get("open_short"):
            if not any(p.direction == "short" for p in positions):
                stops = calc_dynamic_stops(price, "short", atr, "trending")
                return Decision(
                    action="open_short", size_pct=0.015,
                    stop_loss=stops["stop_loss"], take_profit=stops["take_profit"],
                    trailing=True, trailing_step=stops["trailing_step"],
                    urgency="high", layer="attack",
                    reasoning=f"趋势行情顺势做空，移动止盈追踪趋势",
                )

        return None

    def _breakout_attack(
        self, regime, signals, positions, price, atr, balance,
    ) -> Decision | None:
        """突破行情：快速入场。"""
        if signals.get("open_long") and regime.direction == "bullish":
            stops = calc_dynamic_stops(price, "long", atr, "breakout")
            return Decision(
                action="open_long", size_pct=0.012,
                stop_loss=stops["stop_loss"], take_profit=stops["take_profit"],
                trailing=True, trailing_step=stops["trailing_step"],
                urgency="high", layer="attack",
                reasoning=f"突破行情看多，放量确认，快速入场",
            )
        if signals.get("open_short") and regime.direction == "bearish":
            stops = calc_dynamic_stops(price, "short", atr, "breakout")
            return Decision(
                action="open_short", size_pct=0.012,
                stop_loss=stops["stop_loss"], take_profit=stops["take_profit"],
                trailing=True, trailing_step=stops["trailing_step"],
                urgency="high", layer="attack",
                reasoning=f"突破行情看空，放量确认，快速入场",
            )
        return None


# ═══════════════════════════ 第二层：解困 ═══════════════════════════

class RescueLayer:
    """解困层：利润对冲。
    
    当持仓浮亏超过阈值，且行情分类器判定为单边趋势：
    1. 在趋势方向开反向对冲仓
    2. 对冲仓盈利后，用利润覆盖原仓亏损
    3. 先处理亏损最大的持仓
    """

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.hedge_threshold_pct = cfg.get("hedge_threshold_pct", -3.0)
        self.hedge_size_ratio = cfg.get("hedge_size_ratio", 0.5)

    def evaluate(
        self,
        regime: rc.RegimeResult,
        positions: list[Position],
        current_price: float,
        atr: float,
    ) -> Decision | None:
        """解困层决策。"""
        if regime.regime != "trending" or regime.confidence < 0.5:
            return None

        existing_hedges = [p for p in positions if p.is_hedge]
        if existing_hedges:
            return None

        worst_pos = None
        worst_pnl = 0.0
        for pos in positions:
            if pos.is_hedge:
                continue
            pnl_pct = pos.unrealized_pnl_pct(current_price)
            if pnl_pct < self.hedge_threshold_pct and pnl_pct < worst_pnl:
                worst_pnl = pnl_pct
                worst_pos = pos

        if worst_pos is None:
            return None

        is_wrong_direction = (
            (worst_pos.direction == "long" and regime.direction == "bearish") or
            (worst_pos.direction == "short" and regime.direction == "bullish")
        )
        if not is_wrong_direction:
            return None

        hedge_dir = "short" if worst_pos.direction == "long" else "long"
        stops = calc_dynamic_stops(current_price, hedge_dir, atr, "trending")

        return Decision(
            action=f"hedge_{hedge_dir}",
            size_pct=worst_pos.size_usdt / max(current_price, 1) * self.hedge_size_ratio / 100,
            stop_loss=stops["stop_loss"],
            take_profit=stops["take_profit"],
            trailing=True,
            trailing_step=stops["trailing_step"],
            urgency="high",
            layer="rescue",
            reasoning=f"持仓 {worst_pos.id} 浮亏{worst_pnl:.1f}%，"
                      f"行情{regime.direction}趋势明确，"
                      f"开{hedge_dir}对冲仓解困",
        )


# ═══════════════════════════ 第三层：保命 ═══════════════════════════

class ProtectLayer:
    """保命层：趋势过滤。
    
    规则：
    1. 三均线共振确认强趋势时，禁止逆势开仓
    2. ADX > 40 的强趋势中，马丁加仓被抑制
    3. 多时间框架方向一致性检查
    """

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.strong_trend_adx = cfg.get("strong_trend_adx", 35)
        self.suppress_martin_adx = cfg.get("suppress_martin_adx", 40)

    def filter(
        self,
        decision: Decision | None,
        regime: rc.RegimeResult,
    ) -> Decision | None:
        """过滤进攻层和解困层的决策。

        Returns:
            通过过滤的原始决策，或 None（被拦截）
        """
        if decision is None:
            return None

        if decision.layer == "rescue":
            return decision

        adx = 0.0
        if "adx" in regime.indicators:
            adx = regime.indicators["adx"]
        elif "per_tf" in regime.indicators:
            for tf_data in regime.indicators["per_tf"].values():
                if "adx" in tf_data.get("indicators", {}):
                    adx = max(adx, tf_data["indicators"]["adx"])

        is_counter_trend = False
        if regime.regime == "trending" and regime.confidence > 0.5:
            if regime.direction == "bullish" and "short" in decision.action:
                is_counter_trend = True
            elif regime.direction == "bearish" and "long" in decision.action:
                is_counter_trend = True

        if is_counter_trend:
            ema = regime.indicators.get("ema_alignment", {})
            if not ema and "per_tf" in regime.indicators:
                for tf_data in regime.indicators["per_tf"].values():
                    ema = tf_data.get("indicators", {}).get("ema_alignment", {})
                    if ema:
                        break

            aligned = ema.get("aligned_bullish", False) or ema.get("aligned_bearish", False)
            if aligned and adx >= self.strong_trend_adx:
                return Decision(
                    action="hold",
                    size_pct=0.0,
                    stop_loss=0.0,
                    take_profit=0.0,
                    trailing=False,
                    trailing_step=0.0,
                    urgency="low",
                    layer="protect",
                    reasoning=f"🛡️ 保命层拦截：三线共振{regime.direction}+"
                              f"ADX={adx:.0f}，禁止逆势{decision.action}",
                )

        if "add_" in decision.action and adx >= self.suppress_martin_adx:
            return Decision(
                action="hold",
                size_pct=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                trailing=False,
                trailing_step=0.0,
                urgency="low",
                layer="protect",
                reasoning=f"🛡️ 保命层拦截：ADX={adx:.0f}强趋势中，抑制马丁加仓",
            )

        return decision


# ═══════════════════════════ 复合策略管理器 ═══════════════════════════

class CompositeStrategy:
    """三层复合策略管理器。"""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.attack = AttackLayer(cfg.get("attack"))
        self.rescue = RescueLayer(cfg.get("rescue"))
        self.protect = ProtectLayer(cfg.get("protect"))

    def decide(
        self,
        regime: rc.RegimeResult,
        signals: dict[str, bool],
        positions: list[Position],
        current_price: float,
        atr: float,
        balance: float,
    ) -> Decision:
        """三层协同决策。
        
        流程：
        1. 进攻层根据行情+信号生成决策
        2. 解困层检查是否需要对冲
        3. 保命层过滤逆势操作
        4. 解困决策优先级高于进攻决策
        """
        attack_decision = self.attack.evaluate(
            regime, signals, positions, current_price, atr, balance,
        )

        rescue_decision = self.rescue.evaluate(
            regime, positions, current_price, atr,
        )

        candidate = rescue_decision if rescue_decision else attack_decision

        final = self.protect.filter(candidate, regime)

        if final is None:
            return Decision(
                action="hold", size_pct=0.0,
                stop_loss=0.0, take_profit=0.0,
                trailing=False, trailing_step=0.0,
                urgency="low", layer="none",
                reasoning=f"无信号 | 行情: {regime.regime} {regime.direction} "
                          f"置信度{regime.confidence:.0%}",
            )

        return final

    def manage_position(
        self,
        position: Position,
        regime: rc.RegimeResult,
        current_price: float,
        atr: float,
        signals: dict[str, bool],
    ) -> Decision | None:
        """持仓管理：检查止盈止损 + 移动止损。"""
        if position.stop_loss > 0:
            if position.direction == "long" and current_price <= position.stop_loss:
                return Decision(
                    action="close_long", size_pct=1.0,
                    stop_loss=0, take_profit=0,
                    trailing=False, trailing_step=0,
                    urgency="high", layer="attack",
                    reasoning=f"触及止损 {position.stop_loss:.0f}",
                )
            if position.direction == "short" and current_price >= position.stop_loss:
                return Decision(
                    action="close_short", size_pct=1.0,
                    stop_loss=0, take_profit=0,
                    trailing=False, trailing_step=0,
                    urgency="high", layer="attack",
                    reasoning=f"触及止损 {position.stop_loss:.0f}",
                )

        if position.take_profit > 0:
            if position.direction == "long" and current_price >= position.take_profit:
                return Decision(
                    action="close_long", size_pct=1.0,
                    stop_loss=0, take_profit=0,
                    trailing=False, trailing_step=0,
                    urgency="high", layer="attack",
                    reasoning=f"触及止盈 {position.take_profit:.0f}",
                )
            if position.direction == "short" and current_price <= position.take_profit:
                return Decision(
                    action="close_short", size_pct=1.0,
                    stop_loss=0, take_profit=0,
                    trailing=False, trailing_step=0,
                    urgency="high", layer="attack",
                    reasoning=f"触及止盈 {position.take_profit:.0f}",
                )

        new_sl = update_trailing_stop(position, current_price, atr, regime.regime)
        if new_sl is not None:
            position.stop_loss = new_sl

        if position.direction == "long" and signals.get("close_long"):
            return Decision(
                action="close_long", size_pct=1.0,
                stop_loss=0, take_profit=0,
                trailing=False, trailing_step=0,
                urgency="medium", layer="attack",
                reasoning="信号反转平多",
            )
        if position.direction == "short" and signals.get("close_short"):
            return Decision(
                action="close_short", size_pct=1.0,
                stop_loss=0, take_profit=0,
                trailing=False, trailing_step=0,
                urgency="medium", layer="attack",
                reasoning="信号反转平空",
            )

        return None


# ═══════════════════════════ CLI 测试 ═══════════════════════════

def _cli() -> None:
    parser = argparse.ArgumentParser(description="复合策略测试")
    sub = parser.add_subparsers(dest="cmd")

    p_test = sub.add_parser("test", help="用实时数据测试决策")
    p_test.add_argument("--symbol", default="BTCUSDT")

    sub.add_parser("info", help="显示策略结构")

    args = parser.parse_args()

    if args.cmd == "test":
        print(f"正在分析 {args.symbol}...", flush=True)

        regime = rc.classify(args.symbol)
        print(f"\n行情分类: {regime.regime} | {regime.direction} | 置信度 {regime.confidence:.0%}")
        print(f"理由: {regime.reasoning}")

        klines = rc._fetch_klines(args.symbol, "15m", 200)
        if klines is None:
            print("K线数据拉取失败")
            return

        atr_val = rc._atr(klines["high"], klines["low"], klines["close"], 14).iloc[-1]
        price = klines["close"].iloc[-1]

        signals = {
            "open_long": regime.direction == "bullish" and regime.confidence > 0.5,
            "open_short": regime.direction == "bearish" and regime.confidence > 0.5,
            "close_long": False,
            "close_short": False,
        }

        strategy = CompositeStrategy()
        decision = strategy.decide(
            regime=regime,
            signals=signals,
            positions=[],
            current_price=price,
            atr=atr_val,
            balance=10000,
        )

        print(f"\n决策结果:")
        print(f"  动作:   {decision.action}")
        print(f"  仓位:   {decision.size_pct:.1%}")
        if decision.stop_loss:
            print(f"  止损:   {decision.stop_loss:.0f}")
        if decision.take_profit:
            print(f"  止盈:   {decision.take_profit:.0f}")
        print(f"  移动止损: {'是' if decision.trailing else '否'}")
        print(f"  层级:   {decision.layer}")
        print(f"  理由:   {decision.reasoning}")

    elif args.cmd == "info":
        print("=== 复合策略三层结构 ===")
        print()
        print("第一层 · 进攻 (AttackLayer)")
        print("  震荡 → 网格/马丁均价加仓，均价止盈")
        print("  趋势 → 顺势突破追踪，移动止盈")
        print("  突破 → 放量快速入场，紧凑止盈")
        print()
        print("第二层 · 解困 (RescueLayer)")
        print("  浮亏 > 3% + 趋势明确 → 顺势开对冲仓")
        print("  对冲仓盈利覆盖原仓亏损")
        print()
        print("第三层 · 保命 (ProtectLayer)")
        print("  三线共振 + ADX>35 → 禁止逆势开仓")
        print("  ADX>40 → 抑制马丁加仓")
        print()
        print("止损止盈倍数（ATR）:")
        for regime, params in REGIME_SL_TP.items():
            print(f"  {regime:10s} 止损{params['sl_mult']}x 止盈{params['tp_mult']}x "
                  f"移动止损{'是' if params['trailing'] else '否'}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
