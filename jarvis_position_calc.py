#!/usr/bin/env python3
"""贾维斯 JARVIS - 合约仓位与风控计算器（Task #3）。

针对小本金高杠杆合约用户（如 130 USDT 本金、100x 杠杆），把系统信号的
trade_plan 换算成可直接执行的下单建议：

  1) 建议仓位（两种口径二选一）：
     - 保证金法（margin_pct 提供时，桌面端默认）：保证金 = 本金 × 保证金%，
       名义仓位 = 保证金 × 杠杆；止损触发亏损额与风险% 变为派生输出
     - 风险法（legacy，margin_pct 缺省时）：单笔风险 = 本金 × 风险%（建议 1%~3%），
       名义仓位 = 风险额 / 止损距离%，保证金 = 名义 / 杠杆
  2) 入场价区间：沿用信号共识的 entry_zone
  3) 止损价：含「距爆仓价安全边距」检查
  4) 分档止盈：按 1:1.5 / 1:2 / 1:3 盈亏比给三档价位与对应盈利
  5) 爆仓价：Binance USDT 本位永续（逐仓/单向）官方公式
     LP = (隔仓保证金 + 速算额 − 方向×数量×入场价) / (数量×(MMR − 方向))，
     维持保证金率 MMR 与速算额按名义价值分层（BTCUSDT 内置官方分层表，
     其它币种用通用保守档），档位按爆仓时名义定点迭代选取；
     高杠杆下止损比爆仓更远时必须醒目警告

设计原则（与 jarvis_sizing / jarvis_config 一致）：
  - 纯函数、永不抛出：非法输入返回 {"ok": False, "error": ...}
  - 只做建议不下单；所有数值均为近似（忽略资金费率，费用单列提示）
  - 杠杆安全上限：给出「止损打得到、爆仓打不到」的最大安全杠杆

用法（库）：
  from jarvis_position_calc import advice_from_plan, compute_advice
  out = compute_advice(capital_usdt=130, leverage=100, risk_pct=2,
                       side="long", entry=60000, stop_loss=59400)

用法（CLI 自测）：
  python jarvis_position_calc.py --capital 130 --leverage 100 --risk-pct 2 \
      --side long --entry 60000 --sl 59400
"""

from __future__ import annotations

import argparse
import json
import math
import sys

# ── Binance USDT 本位永续 维持保证金分层表（逐仓爆仓价用）─────────────────
# 来源：Binance Futures「杠杆与保证金」分层表（USDⓈ-M BTCUSDT 永续），
# 抄录于 2026-07-10；速算额 cum 按档位边界连续性推导（MM = 名义×MMR − cum，
# 与官方速算额构造一致）。Binance 会不定期调档（如 2026-02-27 公告），
# 调档后需同步更新本表。
# 行结构：(该档名义价值上限 USDT, 维持保证金率 MMR, 速算额 cum USDT)
BINANCE_MM_TIERS: dict[str, tuple[tuple[float, float, float], ...]] = {
    "BTC": (
        (300_000.0, 0.0040, 0.0),
        (800_000.0, 0.0050, 300.0),
        (3_000_000.0, 0.0065, 1_500.0),
        (12_000_000.0, 0.0100, 12_000.0),
        (70_000_000.0, 0.0200, 132_000.0),
        (100_000_000.0, 0.0250, 482_000.0),
        (float("inf"), 0.0500, 2_982_000.0),
    ),
}

# 无内置分层表币种的通用保守档：MMR 1%、速算额 0（主流山寨低档位多在
# 0.5%~1%，取上沿保守估计——爆仓价更贴近入场价，风险提示偏严）
GENERIC_MM_TIERS: tuple[tuple[float, float, float], ...] = (
    (float("inf"), 0.0100, 0.0),
)

# taker 手续费近似%（单边 0.05%，开平双边 0.1%；限价 maker 更低）
TAKER_FEE_PCT = 0.05

# 张数换算面值（OKX USDT 本位永续 ctVal 惯例；Binance 直接按币数下单无张数概念）。
# 不在表内的币种只报币数。
CONTRACT_SIZES: dict[str, float] = {
    "BTC": 0.01,
    "ETH": 0.1,
    "SOL": 1.0,
    "XRP": 100.0,
    "DOGE": 1000.0,
    "ADA": 100.0,
    "BNB": 0.01,
}

# 分档止盈盈亏比（需求指定 1:1.5 / 1:2 / 1:3）
TP_LADDER_RR = (1.5, 2.0, 3.0)

# 安全边距系数：要求 爆仓距离 ≥ 止损距离 × 1.25 才算「止损在爆仓内侧且留有余量」
SAFETY_FACTOR = 1.25


# ── [Sprint1 T1.2] 止损隐蔽化：避开整数关口 / 摆动点扫单区 ─────────────────────

def _round_step(price: float) -> float:
    """整数关口步长：约为价格的 2%，规整到 {1,2,5}×10^n 心理刻度。

    例：BTC 60000 → 1000；ETH 3000 → 50；SOL 96 → 2；DOGE 0.2 → 0.005。
    规整避免了纯对数量级口径对 9 开头小价位过细（如把 96.5 也当关口）的误伤。
    """
    target = price * 0.02
    n = math.floor(math.log10(target))
    best, best_diff = None, float("inf")
    for m in (1.0, 2.0, 5.0):
        for k in (n, n + 1):
            c = m * 10.0 ** k
            d = abs(math.log(c / target))
            if d < best_diff:
                best, best_diff = c, d
    return best or target


def _round_levels_near(price: float) -> list[float]:
    """价格附近的「整数关口」候选：基础步长与更粗一档，各取上下最近关口。"""
    if not (price and math.isfinite(price) and price > 0):
        return []
    base = _round_step(price)
    out: set[float] = set()
    for step in (base, base * 2.0):
        if step <= 0:
            continue
        below = math.floor(price / step) * step
        for lv in (below, below + step):
            if lv > 0:
                out.add(round(lv, 10))
    return sorted(out)


def stealth_stop_loss(sl: float, direction: str, atr: float | None,
                      *, anchor: float | None = None,
                      entry: float | None = None,
                      enabled: bool | None = None,
                      buffer_mult: float | None = None) -> tuple[float, str | None]:
    """把系统默认止损从「明显扫单位」挪开：整数关口 / 摆动锚点，加 ATR 缓冲远离。

    仅用于**系统生成**的默认 SL；用户自定义 SL 不得经过本函数（调用方保证）。

    参数：
      sl        原止损价；direction bullish(多，SL 在下)/bearish(空，SL 在上)
      atr       ATR 绝对值；缺失时用 sl 的 0.5% 兜底（避让幅度退化但不失效）
      anchor    结构锚点（摆动低/高点价，可选）：确保 SL 距锚点 ≥ buffer
      entry     入场价（可选）：方向边界兜底——多单 SL 不得 ≥ entry、空单不得 ≤ entry
      enabled / buffer_mult 缺省时读 jarvis_config（sl_avoid_round_levels /
      sl_atr_buffer_mult），读取失败回退 True / 0.3——保持纯函数可测性。

    安全兜底（P1-1）：调整后价格 ≤0、非有限数、或越过入场价（止损语义被破坏）
    时一律放弃调整、回退原 SL——宁可暴露在扫单区，不能让止损静默失效。

    返回 (新止损价, 调整说明|None)。未调整时原样返回 (sl, None)。永不抛出。
    """
    try:
        if enabled is None or buffer_mult is None:
            try:
                import jarvis_config as _jc
                if enabled is None:
                    enabled = bool(_jc.get("sl_avoid_round_levels"))
                if buffer_mult is None:
                    buffer_mult = float(_jc.get("sl_atr_buffer_mult") or 0.3)
            except Exception:  # noqa: BLE001 — 配置层异常不拖垮信号链
                enabled = True if enabled is None else enabled
                buffer_mult = 0.3 if buffer_mult is None else buffer_mult
        if not enabled or direction not in ("bullish", "bearish"):
            return sl, None
        s = _f(sl)
        if s is None or s <= 0:
            return sl, None
        a = _f(atr)
        buf = (a if a and a > 0 else s * 0.005) * max(0.0, float(buffer_mult))
        if buf <= 0:
            return sl, None
        # 多单 SL 向下避让（挪到扫单区下方），空单向上——远离止损猎杀密集区
        away = -1.0 if direction == "bullish" else 1.0
        near_zone = buf * 0.5  # 「贴近」判定半径：缓冲的一半
        adjusted = s
        reasons: list[str] = []
        for lv in _round_levels_near(s):
            if abs(adjusted - lv) < near_zone:
                adjusted = lv + away * buf
                reasons.append(f"避开整数关口 {_round_price(lv)}")
                break
        if anchor is not None:
            an = _f(anchor)
            if an and an > 0 and abs(adjusted - an) < buf and \
                    (adjusted - an) * away <= 0:
                # SL 落在锚点扫单侧且距离不足 → 挪到锚点外侧 buf 处
                adjusted = an + away * buf
                reasons.append(f"远离摆动点 {_round_price(an)}")
        if not reasons or adjusted == s:
            return sl, None
        # ── [P1-1] 安全兜底：调整不得破坏止损语义，否则整体放弃回退原 SL ──
        # 1) 价格必须为正的有限数（极端 ATR 会把多单 SL 推成 ≤0 → 永不触发）
        if not (math.isfinite(adjusted) and adjusted > 0):
            return sl, None
        # 2) 不得越过入场价：多单 SL 必须 < entry，空单 SL 必须 > entry
        e = _f(entry)
        if e and e > 0:
            if direction == "bullish" and adjusted >= e:
                return sl, None
            if direction == "bearish" and adjusted <= e:
                return sl, None
        # 3) 无 entry 时的对称合理性兜底：调整幅度不得超过原 SL 的 50%
        #    （关口/锚点避让的合理量级是零点几个 ATR，超过一半价格必为异常输入）
        if abs(adjusted - s) > s * 0.5:
            return sl, None
        return _round_price(adjusted), "；".join(reasons) + f"（缓冲 {buffer_mult:g}×ATR）"
    except Exception:  # noqa: BLE001 — 隐蔽化失败宁可用原 SL，不断信号链
        return sl, None


def _f(v) -> float | None:
    """温和转 float；非法返回 None。"""
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _round_price(p: float) -> float:
    """价格动态精度：≥1 两位小数；0.01~1 四位；更小取 6 位有效数字。"""
    if p >= 1:
        return round(p, 2)
    if p >= 0.01:
        return round(p, 4)
    return float(f"{p:.6g}")


def base_coin(symbol: str) -> str:
    """BTCUSDT → BTC（用于查张数面值表）。"""
    s = (symbol or "").upper().replace("-", "").replace("/", "")
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote):
            return s[: -len(quote)]
    return s


def mm_tier(symbol: str, notional: float) -> tuple[float, float, str]:
    """按名义价值查 Binance 维持保证金档位：返回 (MMR 小数, 速算额, 表来源)。

    表来源：binance = 内置官方分层表（当前仅 BTCUSDT）；
            generic = 通用保守档（MMR 1%、速算额 0）。
    """
    tiers = BINANCE_MM_TIERS.get(base_coin(symbol))
    source = "binance" if tiers else "generic"
    for cap, mmr, cum in (tiers or GENERIC_MM_TIERS):
        if notional <= cap:
            return mmr, cum, source
    mmr, cum = (tiers or GENERIC_MM_TIERS)[-1][1:]
    return mmr, cum, source


def liquidation_price(*, entry: float, qty: float, margin: float, side: str,
                      symbol: str = "BTCUSDT",
                      max_iter: int = 8) -> tuple[float, float, float, float, str]:
    """Binance USDT 本位永续 逐仓爆仓价（单向持仓，忽略资金费率/未实现盈亏挪用）。

    官方公式（equity(P) = 维持保证金(P) 解 P）：
      隔仓保证金 + 方向×数量×(P − 入场价) = 数量×P×MMR − 速算额
      → LP = (隔仓保证金 + 速算额 − 方向×数量×入场价) / (数量×(MMR − 方向))
    方向：多=+1、空=−1。MMR/速算额按「爆仓价名义 qty×LP」所在档位定点迭代选取
    （先按入场名义取档 → 算 LP → 按 LP 名义重取档 → 收敛即停）。

    返回 (爆仓价, 爆仓距离%, MMR 小数, 速算额, 表来源)；
    低杠杆多单可能解出负价（价格归零也不爆仓），此时爆仓价钳为 0、距离 100%。
    """
    sign = 1.0 if side == "long" else -1.0
    mmr, cum, src = mm_tier(symbol, qty * entry)
    lp = None
    for _ in range(max_iter):
        lp_new = (margin + cum - sign * qty * entry) / (qty * (mmr - sign))
        lp_new = max(0.0, lp_new)
        if lp is not None and abs(lp_new - lp) <= max(1e-9, lp * 1e-12):
            break
        lp = lp_new
        mmr2, cum2, src = mm_tier(symbol, qty * lp)
        if (mmr2, cum2) == (mmr, cum):
            break
        mmr, cum = mmr2, cum2
    dist_pct = abs(entry - (lp or 0.0)) / entry * 100.0
    return (lp or 0.0), dist_pct, mmr, cum, src


def max_safe_leverage(sl_dist_pct: float, *, side: str = "long",
                      symbol: str = "BTCUSDT", notional: float | None = None,
                      safety: float = SAFETY_FACTOR) -> int:
    """满足「爆仓距离 ≥ 止损距离×safety」的最大整数杠杆（1~125）。

    按 Binance 逐仓口径（保证金=名义/杠杆）解析反推：
      爆仓距离 = (1/杠杆 − MMR + 速算额/名义) / (1 ∓ MMR)   多取−、空取+
    档位近似取当前名义所在档（advisory 用途；名义未知时按最低档）。
    """
    if sl_dist_pct <= 0:
        return 1
    mmr, cum, _ = mm_tier(symbol, notional or 0.0)
    sign = 1.0 if side == "long" else -1.0
    k = sl_dist_pct / 100.0 * safety
    denom = k * (1.0 - sign * mmr) + mmr - (cum / notional if notional else 0.0)
    if denom <= 0:
        return 125
    return max(1, min(125, int(1.0 / denom)))


def compute_advice(*, capital_usdt, leverage, risk_pct, side, entry,
                   stop_loss, entry_zone=None, symbol: str = "BTCUSDT",
                   margin_pct=None) -> dict:
    """核心计算：仓位 / 止损安全边距 / 分档止盈 / 爆仓价 / 警告。永不抛出。

    仓位口径二选一：
      margin_pct 提供（0<x≤100）→ 保证金法：保证金=本金×保证金%，名义=保证金×杠杆，
        risk_pct 输入被忽略，风险额/风险% 变为派生输出（止损触发时的实际亏损）
      margin_pct 缺省 → 风险法（legacy）：名义 = 本金×风险% / 止损距离%

    爆仓价：Binance USDT 本位永续逐仓官方公式 + 分层维持保证金（见 liquidation_price）。

    Returns（ok=True 时）：
      side, entry, entry_zone, capital_usdt, leverage, risk_pct, sizing_mode,
      margin_pct(保证金法时), risk_usdt          单笔计划亏损额（止损触发时）
      sl: {price, dist_pct, safety, safety_margin_pct}   safety∈ok|warning|danger
      liquidation: {price, dist_pct, loss_usdt}          爆仓时损失≈全部保证金
      mm: {mmr_pct, cum_usdt, tier_source}               生效维持保证金档位
      position: {notional_usdt, margin_usdt, qty_coin, contracts, contract_size,
                 capital_used_pct}
      take_profits: [{rr, price, profit_usdt}]           1:1.5 / 1:2 / 1:3
      max_safe_leverage, est_fee_usdt, warnings[], note
    """
    cap = _f(capital_usdt)
    lev = _f(leverage)
    rp = _f(risk_pct)
    e = _f(entry)
    sl = _f(stop_loss)
    mp = _f(margin_pct) if margin_pct is not None else None
    if cap is None or cap <= 0:
        return {"ok": False, "error": f"本金非法: {capital_usdt}"}
    if lev is None or not (1 <= lev <= 125):
        return {"ok": False, "error": f"杠杆非法(1~125): {leverage}"}
    if margin_pct is not None and (mp is None or not (0 < mp <= 100)):
        return {"ok": False, "error": f"保证金百分比非法(0~100]: {margin_pct}"}
    if mp is None and (rp is None or not (0 < rp <= 50)):
        return {"ok": False, "error": f"风险百分比非法: {risk_pct}"}
    if side not in ("long", "short"):
        return {"ok": False, "error": f"方向非法(long|short): {side}"}
    if e is None or e <= 0 or sl is None or sl <= 0:
        return {"ok": False, "error": f"入场/止损价非法: entry={entry} sl={stop_loss}"}
    if side == "long" and sl >= e:
        return {"ok": False, "error": f"多单止损必须低于入场价: entry={e} sl={sl}"}
    if side == "short" and sl <= e:
        return {"ok": False, "error": f"空单止损必须高于入场价: entry={e} sl={sl}"}

    warnings: list[str] = []
    sl_dist_pct = abs(e - sl) / e * 100.0
    capped = False

    if mp is not None:
        # ── 1a. 保证金法：保证金 = 本金×保证金%，名义 = 保证金×杠杆 ──
        margin_target = cap * mp / 100.0
        notional = margin_target * lev
        risk_usdt = notional * sl_dist_pct / 100.0
        rp = risk_usdt / cap * 100.0  # 派生风险%（供展示与激进度判断）
        if rp > 3.0:
            warnings.append(
                f"按当前保证金 {mp:g}% × 杠杆 {lev:g}x，止损触发将亏 {risk_usdt:.2f} USDT"
                f"（占本金 {rp:.1f}%），超出建议区间 1%~3%，属激进配置")
    else:
        # ── 1b. 风险法（legacy）：名义 = 风险额 / 止损距离% ──
        if rp > 3.0:
            warnings.append(f"风险 {rp:g}% 超出建议区间 1%~3%，属激进配置")
        risk_usdt = cap * rp / 100.0
        notional = risk_usdt / (sl_dist_pct / 100.0)

        # 本金 × 杠杆 封顶（风险法仓位超出可开上限时按上限缩，风险随之缩小）
        max_notional = cap * lev
        if notional > max_notional:
            capped = True
            notional = max_notional
            risk_usdt = notional * sl_dist_pct / 100.0
            warnings.append(
                f"目标风险需要的仓位超过 本金×杠杆 上限，已按上限 {notional:.2f} USDT 开满，"
                f"实际单笔风险降为 {risk_usdt:.2f} USDT")

    margin = notional / lev
    qty_coin = notional / e
    coin = base_coin(symbol)
    ct_size = CONTRACT_SIZES.get(coin)
    contracts = round(qty_coin / ct_size, 2) if ct_size else None
    if contracts is not None and contracts < 1:
        warnings.append(
            f"折合 {contracts:g} 张不足 1 张（OKX 口径 1 张={ct_size:g} {coin}），"
            f"整张交易所需下 1 张并接受风险放大，或改用 Binance 按 {qty_coin:.6g} {coin} 下单")

    # ── 2. 爆仓价与安全边距（Binance 逐仓官方公式）──
    liq_price, liq_dist_pct, mmr, cum, mm_src = liquidation_price(
        entry=e, qty=qty_coin, margin=margin, side=side, symbol=symbol)
    if sl_dist_pct >= liq_dist_pct:
        safety = "danger"
        warnings.append(
            f"🚨 止损距离 {sl_dist_pct:.2f}% ≥ 爆仓距离 {liq_dist_pct:.2f}%——"
            f"价格会先打爆仓({_round_price(liq_price)})再到止损({_round_price(sl)})，"
            f"止损形同虚设，实际最大亏损≈全部保证金 {margin:.2f} USDT")
    elif sl_dist_pct >= liq_dist_pct / SAFETY_FACTOR:
        safety = "warning"
        warnings.append(
            f"⚠️ 止损距爆仓价过近（止损 {sl_dist_pct:.2f}% vs 爆仓 {liq_dist_pct:.2f}%，"
            f"边距不足 {SAFETY_FACTOR:g} 倍），插针可能先爆仓")
    else:
        safety = "ok"

    if lev >= 50:
        warnings.append(
            f"⚠️ {lev:g}x 杠杆下爆仓距离仅约 {liq_dist_pct:.2f}%，"
            f"正常波动即可爆仓，强烈建议降杠杆")

    safe_lev = max_safe_leverage(sl_dist_pct, side=side, symbol=symbol,
                                 notional=notional)
    if lev > safe_lev:
        warnings.append(
            f"按本次止损距离 {sl_dist_pct:.2f}%，最大安全杠杆约 {safe_lev}x"
            f"（要求爆仓距离≥止损×{SAFETY_FACTOR:g}），当前 {lev:g}x 超限")

    # ── 3. 分档止盈（1:1.5 / 1:2 / 1:3）──
    risk_dist = abs(e - sl)
    sign = 1.0 if side == "long" else -1.0
    take_profits = [
        {"rr": rr,
         "price": _round_price(e + sign * rr * risk_dist),
         "profit_usdt": round(risk_usdt * rr, 2)}
        for rr in TP_LADDER_RR
    ]

    est_fee = notional * TAKER_FEE_PCT / 100.0 * 2

    zone = None
    if isinstance(entry_zone, (list, tuple)) and len(entry_zone) >= 2:
        lo, hi = _f(entry_zone[0]), _f(entry_zone[1])
        if lo and hi and lo > 0 and hi > 0:
            zone = [_round_price(min(lo, hi)), _round_price(max(lo, hi))]

    return {
        "ok": True,
        "symbol": (symbol or "").upper(),
        "side": side,
        "entry": _round_price(e),
        "entry_zone": zone or [_round_price(e), _round_price(e)],
        "capital_usdt": round(cap, 2),
        "leverage": round(lev, 1),
        "sizing_mode": "margin" if mp is not None else "risk",
        "margin_pct": round(mp, 2) if mp is not None else None,
        "risk_pct": round(rp, 2),
        "risk_usdt": round(risk_usdt, 2),
        "sl": {
            "price": _round_price(sl),
            "dist_pct": round(sl_dist_pct, 3),
            "safety": safety,
            "safety_margin_pct": round(liq_dist_pct - sl_dist_pct, 3),
        },
        "liquidation": {
            "price": _round_price(liq_price),
            "dist_pct": round(liq_dist_pct, 3),
            "loss_usdt": round(margin, 2),
        },
        "position": {
            "notional_usdt": round(notional, 2),
            "margin_usdt": round(margin, 2),
            "qty_coin": float(f"{qty_coin:.6g}"),
            "contracts": contracts,
            "contract_size": ct_size,
            "capital_used_pct": round(margin / cap * 100.0, 2),
            "capped": capped,
        },
        "take_profits": take_profits,
        "max_safe_leverage": safe_lev,
        "est_fee_usdt": round(est_fee, 3),
        "mm": {"mmr_pct": round(mmr * 100.0, 3), "cum_usdt": round(cum, 2),
               "tier_source": mm_src},
        "warnings": warnings,
        "note": ((f"保证金法仓位：名义=本金{cap:g}×保证金{mp:g}%×杠杆{lev:g}x；"
                  f"止损触发亏{risk_usdt:.2f} USDT（占本金 {rp:.1f}%）；"
                  if mp is not None else
                  f"风险法仓位：名义=风险额{risk_usdt:.2f}/止损距离{sl_dist_pct:.2f}%；")
                 + f"爆仓价按 Binance 分层维持保证金官方公式计算（逐仓，"
                 + (f"BTCUSDT 官方分层表 MMR {mmr * 100:g}%"
                    if mm_src == "binance" else
                    f"通用保守档 MMR {mmr * 100:g}%")
                 + (f"、速算额 {cum:g} U" if cum else "")
                 + f"，表抄录 2026-07，忽略资金费率，以交易所实时档位为准）；"
                 f"盈利额未扣双边手续费≈{est_fee:.3f} USDT"),
    }


def advice_from_plan(plan: dict | None, *, capital_usdt, leverage, risk_pct,
                     symbol: str = "BTCUSDT", side: str | None = None,
                     margin_pct=None, entry_override=None) -> dict:
    """从共识/单信号 trade_plan 换算下单建议。

    兼容两种计划结构：
      共识级: {side, entry_zone:[lo,hi], stop_loss, take_profit_1, ...}
      单信号: {side, entry, stop_loss, take_profit, ...}
    plan 缺失 / 点位不全 → {"ok": False, "error": ...}（调用方展示「暂无可执行计划」）。

    entry_override：用户手动指定入场价（桌面端输入框）。以它为基准时，
      止损/止盈随入场价平移（保持信号计划的止损距离%），爆仓价也随之重算。
    margin_pct：透传 compute_advice 的保证金法口径。
    """
    if not isinstance(plan, dict):
        return {"ok": False, "error": "无交易计划（共识中性或分歧）"}
    p_side = side or plan.get("side")
    if p_side not in ("long", "short"):
        return {"ok": False, "error": f"计划缺少方向: {p_side}"}

    zone = plan.get("entry_zone")
    entry = None
    if isinstance(zone, (list, tuple)) and len(zone) >= 2:
        lo, hi = _f(zone[0]), _f(zone[1])
        if lo and hi:
            entry = (lo + hi) / 2.0
    if entry is None:
        entry = _f(plan.get("entry"))
    sl = _f(plan.get("stop_loss"))
    if entry is None or sl is None:
        return {"ok": False, "error": "计划缺少入场价或止损价"}

    eo = _f(entry_override) if entry_override is not None else None
    entry_overridden = False
    if eo is not None and eo > 0 and abs(eo - entry) > 1e-12:
        # 止损随用户入场价平移：保持信号计划的止损距离%不变
        sl_dist_ratio = abs(entry - sl) / entry
        sl = eo * (1 - sl_dist_ratio) if p_side == "long" else eo * (1 + sl_dist_ratio)
        entry = eo
        zone = None  # 信号入场区间对手动价不再适用
        entry_overridden = True

    out = compute_advice(capital_usdt=capital_usdt, leverage=leverage,
                         risk_pct=risk_pct, side=p_side, entry=entry,
                         stop_loss=sl, entry_zone=zone, symbol=symbol,
                         margin_pct=margin_pct)
    if out.get("ok") and entry_overridden:
        out["entry_overridden"] = True
    if out.get("ok"):
        # 保留信号自身的止盈参考，便于前端对照展示
        tp1 = _f(plan.get("take_profit_1") or plan.get("take_profit"))
        if tp1:
            out["plan_tp_ref"] = _round_price(tp1)
        if plan.get("source_tf"):
            out["source_tf"] = plan.get("source_tf")
        if plan.get("basis"):
            out["basis"] = plan.get("basis")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="贾维斯合约仓位与风控计算器")
    ap.add_argument("--capital", type=float, default=130.0)
    ap.add_argument("--leverage", type=float, default=100.0)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--margin-pct", type=float, default=None,
                    help="保证金占本金%%；提供时用保证金法口径，忽略 --risk-pct")
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--sl", type=float, required=True)
    ap.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args()
    out = compute_advice(capital_usdt=args.capital, leverage=args.leverage,
                         risk_pct=args.risk_pct, side=args.side,
                         entry=args.entry, stop_loss=args.sl, symbol=args.symbol,
                         margin_pct=args.margin_pct)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
