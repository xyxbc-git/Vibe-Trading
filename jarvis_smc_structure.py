#!/usr/bin/env python3
"""SMC 市场结构检测（BOS / CHoCH）——K 线图叠加层数据源（R8 追加，纯函数零 IO）。

老交易员口径（改动语义请同步 _smc_structure_smoketest.py）：
- swing 高/低点：窗口法 window=5，j 为 [j-w, j+w] 内极值（与
  jarvis_twelve_systems._swing_points / jarvis_fvg._swing_points 同口径，
  本文件独立实现不改不引它们）；swing 点在其后第 window 根收盘后才「确认」，
  确认前不作为结构锚点（防未来函数）。
- BOS（Break of Structure，趋势延续确认）：结构方向向上时，**收盘价**向上突破
  最近已确认 swing 高点 → 看涨 BOS；下行镜像（收盘跌破最近确认 swing 低点）。
- CHoCH（Change of Character，结构转换警示）：首次逆结构方向突破——上升结构中
  收盘跌破最近已确认 swing 低点（higher-low 失守）→ 看跌 CHoCH；下行镜像。
  结构方向未定（起步阶段）时首个突破记 BOS 并确立方向。
- 只认收盘突破，影线触碰不算（防扫单假突破）；同一 swing 锚点只触发一次，
  突破后锚点作废、等待下一个 swing 确认后再接续判定。
"""

from __future__ import annotations

import pandas as pd

MIN_BARS = 30
_REQUIRED_COLS = ("high", "low", "close")


def _valid_df(df) -> str | None:
    """输入校验：返回不合格原因；None=合格（与 jarvis_fvg 同款口径）。"""
    if df is None or not isinstance(df, pd.DataFrame):
        return "输入不是 DataFrame"
    if any(c not in df.columns for c in _REQUIRED_COLS):
        missing = [c for c in _REQUIRED_COLS if c not in df.columns]
        return f"缺少必需列 {missing}"
    if len(df) < MIN_BARS:
        return f"数据不足（{len(df)} 根 < {MIN_BARS}）"
    return None


def _swing_points(df: pd.DataFrame, window: int = 5) -> tuple[list[int], list[int]]:
    """swing 高/低点下标（window 根内局部极值；平台去重与 fvg/twelve 同口径）。"""
    highs, lows = [], []
    h, l = df["high"].values, df["low"].values
    n = len(df)
    for i in range(window, n - window):
        if h[i] == max(h[i - window: i + window + 1]):
            if not highs or highs[-1] != i - 1 or h[i] != h[i - 1]:
                highs.append(i)
        if l[i] == min(l[i - window: i + window + 1]):
            if not lows or lows[-1] != i - 1 or l[i] != l[i - 1]:
                lows.append(i)
    return highs, lows


def detect_structure(df: pd.DataFrame, *, window: int = 5,
                     max_events: int = 8) -> dict:
    """扫描全段 K 线的 BOS/CHoCH 结构事件（纯函数）。

    返回 {ok, reason, direction: up|down|None（扫描结束时的结构方向）,
          events: [{kind: bos|choch, direction: bullish|bearish,
                    level（被突破 swing 价）, swing_i, break_i}]}
    events 按发生顺序，只保留最近 max_events 个（防图面拥挤）。
    """
    err = _valid_df(df)
    if err is not None:
        return {"ok": False, "reason": err, "direction": None, "events": []}
    w = max(2, int(window))
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    n = len(df)
    sh, sl = _swing_points(df, w)

    direction: str | None = None
    last_high: tuple[int, float] | None = None  # 最近已确认且未被突破的 swing 高
    last_low: tuple[int, float] | None = None
    events: list[dict] = []
    hi_ptr = lo_ptr = 0

    for i in range(n):
        # swing 点 j 在 j+w 根收盘时确认；新确认的锚点覆盖旧锚点（结构看「最近」）
        while hi_ptr < len(sh) and sh[hi_ptr] + w <= i:
            last_high = (sh[hi_ptr], float(highs[sh[hi_ptr]]))
            hi_ptr += 1
        while lo_ptr < len(sl) and sl[lo_ptr] + w <= i:
            last_low = (sl[lo_ptr], float(lows[sl[lo_ptr]]))
            lo_ptr += 1

        c = float(closes[i])
        if last_high is not None and c > last_high[1]:
            kind = "bos" if direction in (None, "up") else "choch"
            events.append({"kind": kind, "direction": "bullish",
                           "level": float(last_high[1]),
                           "swing_i": int(last_high[0]), "break_i": int(i)})
            direction = "up"
            last_high = None  # 锚点作废，等新 swing high 确认
        elif last_low is not None and c < last_low[1]:
            kind = "bos" if direction in (None, "down") else "choch"
            events.append({"kind": kind, "direction": "bearish",
                           "level": float(last_low[1]),
                           "swing_i": int(last_low[0]), "break_i": int(i)})
            direction = "down"
            last_low = None

    return {"ok": True, "reason": None, "direction": direction,
            "events": events[-max(1, int(max_events)):]}
