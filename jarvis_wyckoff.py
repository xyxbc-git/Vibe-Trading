#!/usr/bin/env python3
"""贾维斯 JARVIS — 威科夫阶段引擎（P2 · T2.1 区间 / T2.2 事件 / T2.3 阶段 /
T2.4 证据绑定 / T2.6 回测）。

威科夫叙事：市场在「交易区间」内完成吸筹（accumulation）或派发
（distribution），关键事件（SC/AR/ST/Spring/Test/SOS/LPS 与其派发镜像
BC/UT/UTAD/SOW/LPSY）揭示主力意图，阶段 A→E 推进到趋势离场。
本模块只管「区间/事件/阶段叙事」；微观实锤由 jarvis_supply_demand 提供
（单向依赖，enrich 时调用其 collect_evidence/verdict）。

性能纪律（开发计划 §四，验收要查）：
  - 取数只走 jarvis_delta_flow.fetch_bars（TTL 缓存 + 防限频三道闸）；
    本模块零新增出网端点，判定核心全部纯函数（吃 bars list 离线可测）。
  - 防前瞻：fetch_bars 已丢进行中最后一根；swing 确认额外滞后 k 根；
    事件全部在「确认 bar」时刻落地（如 Spring 记在收回区间那根，而非下探根）。
  - 复杂度：swing O(n·k)（k=5 常数）；事件扫描单次前向遍历 O(n×12)；
    区间候选起点 ≤ MAX_RANGE_STARTS，禁止两两配对 O(n²)。
  - analyze() 以最后一根已收盘 bar 的 open_time 做缓存指纹，新 bar 才重算。

事件时间口径：事件 ts 为**确认 bar 的开盘时间（秒）**，与 trap-signals 的
marker 通道一致（前端可直接复用）；price 为标注价（Spring/UT 取扫针极值）。

用法：
  python jarvis_wyckoff.py BTCUSDT --interval 1h [--json]
  python jarvis_wyckoff.py BTCUSDT --interval 1h --backtest --days 90
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

DISCLAIMER = ("威科夫事件/阶段为规则化概率叙事而非确定结论；样本不足时回测"
              "输出 insufficient_samples，不作小样本推断。非投资建议。")

# ── 阈值（初版按经典口径拍定，T2.6 回测校准后可调；集中在此便于审查）────────
SWING_K = 5                  # swing 极值窗口（±5 根，确认滞后 5 根）
ATR_N = 14                   # ATR 窗口（前置窗口，不含当前 bar）
VOL_MA_N = 20                # 均量窗口（前置窗口，不含当前 bar）
WARMUP = max(ATR_N, VOL_MA_N) + 1   # 指标就绪前不判定事件

RANGE_HIST_MIN = 120         # bars 总数低于此不识别区间（新品种历史短→None）
RANGE_MIN_BARS = 30          # 区间最短根数
RANGE_INSIDE_RATIO = 0.80    # 收盘价落在区间内的最低时间占比
RANGE_MAX_ATR_MULT = 8.0     # (high-low)/ATR14 上限（横盘密集，非趋势段）
RANGE_LOOKBACK = 240         # 只在最近 N 根内找区间
MAX_RANGE_STARTS = 12        # 候选起点上限（复杂度护栏：O(12·n)）
RANGE_MIN_SWINGS = 2         # 区间两侧各需 ≥2 个 swing 点（结构成立）
RANGE_FORM_BARS = 40         # 边界只取「形成期」（起点后 N 根）的 swing 点：
                             # 威科夫区间边界由高潮后的 AR/ST 结构确立，后期的
                             # Spring/UT 扫针与离场段（markup/markdown 尾巴）不得
                             # 反过来改写边界，否则 mid/low 漂移导致事件误判
BOUND_Q = 0.75               # 边界聚类分位：高边取 swing 高点 75 分位（低边镜像 25），
                             # 天然剔除 SC/BC 扫针极值，不吃单点噪声

EVENT_GAP = 5                # 同型事件最小间隔（根），防重复刷屏
CLIMAX_PREROLL = 15          # SC/BC/AR/ST 允许早于区间起点 N 根（高潮催生区间）
CLIMAX_RANGE_ATR = 2.0       # SC/BC：bar 振幅 ≥ 2×ATR
CLIMAX_VOL_RATIO = 2.5       # SC/BC：量 ≥ 2.5×均量
CLIMAX_VOL_BONUS = 3.0       # SC/BC 加分：量 ≥ 3×均量
AR_WITHIN = 10               # AR 须在 SC/BC 后 ≤10 根内
AR_MIN_RETRACE = 0.5         # AR 反弹 ≥ 0.5×SC 振幅
ST_WITHIN = 40               # ST 须在 SC 后 ≤40 根内
ST_TOL_ATR = 0.5             # ST 回踩 SC 低点 ±0.5×ATR
ST_VOL_RATIO = 0.7           # ST 量 < 0.7×SC 量
SPRING_RECLAIM = 3           # Spring/UT：破位后 ≤3 根收回
SHAKE_MATURITY = 30          # Spring/UT 属 Phase B/C 摇仓事件：区间发育 <30 根时
                             # 的破位收回是区间形成期噪声（如 SC bar 自身的下探），
                             # 不作弹簧/上冲判定
FOLLOW_WITHIN = 15           # Test/LPS/LPSY 须在前置事件后 ≤15 根内
QUIET_VOL_RATIO = 0.8        # 缩量判定：量 < 0.8×均量
QUIET_VOL_BONUS = 0.5        # 缩量加分：量 < 0.5×均量
SOS_VOL_RATIO = 1.5          # SOS/SOW：放量 ≥ 1.5×均量
SOS_VOL_BONUS = 2.5          # SOS/SOW 加分：量 ≥ 2.5×均量
CLOSE_POS_STRONG = 2.0 / 3.0  # 收在 bar 上 1/3（SOS）/ 下 1/3（SOW 镜像）
LPS_TOL_ATR = 0.25           # LPS/LPSY 回踩容差（×ATR）
LPS_NEAR_ATR = 0.5           # LPS/LPSY 须真的回踩到位（接近度 ×ATR）
UTAD_MATURITY = 30           # UTAD 要求区间已发育 ≥30 根（Phase C 位置）
CVD_LOOKBACK = 20            # UTAD 的 CVD 背离回看窗口

ACC_TYPES = frozenset({"sc", "ar", "st", "spring", "test", "sos", "lps"})
DIST_TYPES = frozenset({"bc", "ut", "utad", "sow", "lpsy"})

BACKTEST_HORIZON = 12        # 回测：触发后 N 根收盘对比触发价
BACKTEST_MIN_SAMPLES = 30    # 低于此输出 insufficient_samples
BACKTEST_BULL = ("spring", "lps")
BACKTEST_BEAR = ("utad", "lpsy")

ANALYZE_LIMIT = 300          # analyze 默认取数根数（≤ fetch_bars 上限 500）


# ═══════════════════════════ 基础指标（纯函数，O(n)） ═══════════════════════════


def _atr_prev(bars: list[dict]) -> list[float]:
    """前置 ATR：atr[i] = 前 ATR_N 根 TR 均值（不含 bar i 自身，防自稀释）。
    就绪前置 0.0（调用方须以 WARMUP 起判）。"""
    n = len(bars)
    tr = [0.0] * n
    for i in range(n):
        hi, lo = bars[i]["high"], bars[i]["low"]
        if i == 0:
            tr[i] = hi - lo
        else:
            pc = bars[i - 1]["close"]
            tr[i] = max(hi - lo, abs(hi - pc), abs(lo - pc))
    out = [0.0] * n
    acc = 0.0
    for i in range(n):
        if i >= ATR_N:
            out[i] = acc / ATR_N
        acc += tr[i]
        if i >= ATR_N:
            acc -= tr[i - ATR_N]
    return out


def _vol_ma_prev(bars: list[dict]) -> list[float]:
    """前置均量：vma[i] = 前 VOL_MA_N 根量均值（不含 bar i，量比口径同 P1）。"""
    n = len(bars)
    out = [0.0] * n
    acc = 0.0
    for i in range(n):
        if i >= VOL_MA_N:
            out[i] = acc / VOL_MA_N
        acc += bars[i]["volume"]
        if i >= VOL_MA_N:
            acc -= bars[i - VOL_MA_N]["volume"]
    return out


def _swing_points(bars: list[dict], k: int = SWING_K) -> tuple[list[int], list[int]]:
    """确认 swing 高/低点下标（±k 根内极值，平台取首个；确认滞后 k 根）。"""
    n = len(bars)
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    sh: list[int] = []
    sl: list[int] = []
    for i in range(k, n - k):
        seg_h = highs[i - k: i + k + 1]
        if highs[i] == max(seg_h) and not (sh and sh[-1] == i - 1 and highs[i] == highs[i - 1]):
            sh.append(i)
        seg_l = lows[i - k: i + k + 1]
        if lows[i] == min(seg_l) and not (sl and sl[-1] == i - 1 and lows[i] == lows[i - 1]):
            sl.append(i)
    return sh, sl


def _cvd_series(bars: list[dict]) -> list[float] | None:
    """CVD 序列（bars 缺 taker_buy 字段时返回 None，UTAD 的 CVD 背离降级）。"""
    try:
        out = []
        cvd = 0.0
        for b in bars:
            cvd += 2.0 * float(b["taker_buy"]) - float(b["volume"])
            out.append(cvd)
        return out
    except (KeyError, TypeError, ValueError):
        return None


def _quantile_bound(prices: list[float], upper: bool) -> float:
    """边界聚类（分位法）：高边取 75 分位、低边取 25 分位。
    威科夫区间的 SC/BC/Spring/UT 都会把针扎到区间外，均值/极值边界会被
    污染；分位边界自动落在「被反复访问的价格簇」上，确定性 O(m log m)。"""
    xs = sorted(prices)
    q = BOUND_Q if upper else (1.0 - BOUND_Q)
    idx = min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))
    return xs[idx]


# ═══════════════════════════ T2.1 交易区间识别 ═══════════════════════════


def detect_range(bars: list) -> dict | None:
    """近端交易区间：swing 高低点（k=5 滚动窗口，O(n)）分位聚类定边界；
    区间成立 = 最近 ≥30 根内，收盘价 ≥80% 时间落在 [low, high] 且
    (high-low)/ATR14 ≤ 8（横盘密集，非趋势段）。
    边界只由候选起点后 RANGE_FORM_BARS 根内（形成期）的 swing 点决定——
    后期 Spring/UT 扫针与离场段不改写边界；inside-ratio 与宽度校验仍对
    整段执行，因此把前置趋势段吸进来的候选会因边界失真被自然淘汰。
    返回 {"high","low","mid","start_ts","bars_in_range","atr"}（含辅助键
    "start_idx"）或 None（趋势段/历史不足）。"""
    n = len(bars) if bars else 0
    if n < RANGE_HIST_MIN:
        return None
    atr_arr = _atr_prev(bars)
    atr = atr_arr[-1]
    if atr <= 0:
        return None
    lo_bound = max(0, n - RANGE_LOOKBACK)
    sh, sl = _swing_points(bars)
    sh = [i for i in sh if i >= lo_bound]
    sl = [i for i in sl if i >= lo_bound]
    if len(sh) < RANGE_MIN_SWINGS or len(sl) < RANGE_MIN_SWINGS:
        return None

    # 候选起点：全部 swing 点按时间均匀采样 ≤MAX_RANGE_STARTS 个（必含最老/最新，
    # 复杂度护栏 O(12·n)），取通过校验的最长段
    cand = sorted({*sh, *sl})
    if len(cand) > MAX_RANGE_STARTS:
        step = (len(cand) - 1) / (MAX_RANGE_STARTS - 1)
        cand = sorted({cand[round(k * step)] for k in range(MAX_RANGE_STARTS)})
    starts = cand
    best: dict | None = None
    for s in starts:
        seg_len = n - s
        if seg_len < RANGE_MIN_BARS:
            continue
        form_end = s + RANGE_FORM_BARS
        seg_sh = [bars[i]["high"] for i in sh if s <= i < form_end]
        seg_sl = [bars[i]["low"] for i in sl if s <= i < form_end]
        if len(seg_sh) < RANGE_MIN_SWINGS or len(seg_sl) < RANGE_MIN_SWINGS:
            continue
        high = _quantile_bound(seg_sh, upper=True)
        low = _quantile_bound(seg_sl, upper=False)
        if high <= low:
            continue
        if (high - low) / atr > RANGE_MAX_ATR_MULT:
            continue
        inside = sum(1 for i in range(s, n) if low <= bars[i]["close"] <= high)
        if inside / seg_len < RANGE_INSIDE_RATIO:
            continue
        if best is None or seg_len > best["bars_in_range"]:
            best = {
                "high": round(high, 8), "low": round(low, 8),
                "mid": round((high + low) / 2.0, 8),
                "start_ts": int(bars[s]["ts"] / 1000),
                "bars_in_range": seg_len,
                "atr": round(atr, 8),
                "start_idx": s,
            }
    return best


# ═══════════════════════════ T2.2 事件检测（12 事件规则引擎） ═══════════════════════════


def _mk_event(bars: list, i: int, typ: str, price: float, score: float) -> dict:
    return {"type": typ, "ts": int(bars[i]["ts"] / 1000), "price": round(price, 8),
            "bar_idx": i, "raw_score": round(min(1.0, score), 2),
            "side": "acc" if typ in ACC_TYPES else "dist"}


def detect_events(bars: list, rng: dict | None) -> list[dict]:
    """12 事件规则引擎：单次前向遍历（O(n×12)），只吃已收盘 bars + 区间。
    每事件在「确认 bar」落地（防前瞻）；raw_score = 主条件 0.6 + 加分项各 0.2。
    无区间（趋势段）→ []（事件依附区间语义，见开发计划 §T2.3 容错）。"""
    if not rng or not bars:
        return []
    n = len(bars)
    start = rng.get("start_idx")
    if start is None:  # 契约兜底：调用方只给最小键时按 start_ts 回推
        start_ts_ms = int(rng.get("start_ts", 0)) * 1000
        start = next((i for i, b in enumerate(bars) if b["ts"] >= start_ts_ms), 0)
    start = int(start)
    # 高潮事件（SC/BC/AR/ST）催生区间，允许早于区间起点 CLIMAX_PREROLL 根；
    # 其余事件依附已成立的区间边界，只在区间内扫描。
    climax_from = max(start - CLIMAX_PREROLL, WARMUP)
    scan_from = climax_from
    if scan_from >= n:
        return []

    atr_arr = _atr_prev(bars)
    vma_arr = _vol_ma_prev(bars)
    cvd = _cvd_series(bars)
    r_high, r_low, r_mid = rng["high"], rng["low"], rng["mid"]
    span = max(r_high - r_low, 1e-12)

    events: list[dict] = []
    last_emit: dict[str, int] = {}

    def _gap_ok(typ: str, i: int) -> bool:
        return i - last_emit.get(typ, -10**9) >= EVENT_GAP

    def _emit(i: int, typ: str, price: float, score: float) -> None:
        last_emit[typ] = i
        events.append(_mk_event(bars, i, typ, price, score))

    # 事件间状态（全部只依赖过去 bar）
    sc = ar = st = spring = sos = None          # 吸筹侧
    bc = sow = None                              # 派发侧
    ar_done = st_done = test_done = lps_done = lpsy_done = False
    spring_ep: dict | None = None                # 下破区间低点的进行中回合
    ut_ep: dict | None = None                    # 上破区间高点的进行中回合
    sos_armed = True                             # SOS 触发后须回到中轨下方再武装
    sow_armed = True
    hi_since_sow = 0.0

    for i in range(scan_from, n):
        b = bars[i]
        o, h, l, c, v = b["open"], b["high"], b["low"], b["close"], b["volume"]
        atr, vma = atr_arr[i], vma_arr[i]
        if atr <= 0 or vma <= 0:
            continue
        bar_rng = max(h - l, 1e-12)
        pos = (c - l) / bar_rng            # 0=收在最低，1=收在最高

        # ── SC 恐慌抛售：大阴线 2×ATR + 量 ≥2.5×均量 + 收在 bar 下 1/3 之外 ──
        if (c < o and bar_rng >= CLIMAX_RANGE_ATR * atr and v >= CLIMAX_VOL_RATIO * vma
                and pos >= 1.0 / 3.0 and l <= r_low + 0.5 * span and _gap_ok("sc", i)):
            score = 0.6
            if v >= CLIMAX_VOL_BONUS * vma:
                score += 0.2
            if (min(o, c) - l) >= 0.5 * bar_rng:   # 长下影 = 承接更实
                score += 0.2
            _emit(i, "sc", l, score)
            sc = {"idx": i, "low": l, "high": h, "amp": bar_rng, "vol": v, "hi": h}
            ar_done = st_done = False

        # ── BC 抢购高潮（SC 镜像）：大阳 + 巨量 + 上影（收在 bar 上 1/3 之外）──
        if (c > o and bar_rng >= CLIMAX_RANGE_ATR * atr and v >= CLIMAX_VOL_RATIO * vma
                and pos <= 2.0 / 3.0 and h >= r_high - 0.5 * span and _gap_ok("bc", i)):
            score = 0.6
            if v >= CLIMAX_VOL_BONUS * vma:
                score += 0.2
            if (h - max(o, c)) >= 0.5 * bar_rng:   # 长上影 = 抛压更实
                score += 0.2
            _emit(i, "bc", h, score)
            bc = {"idx": i, "high": h, "amp": bar_rng, "vol": v}

        # ── AR 自动反弹：SC 后 ≤10 根内反弹 ≥0.5×SC 振幅 ──
        if sc and not ar_done and sc["idx"] < i <= sc["idx"] + AR_WITHIN:
            sc["hi"] = max(sc["hi"], h)
            if h - sc["low"] >= AR_MIN_RETRACE * sc["amp"]:
                score = 0.6
                if h - sc["low"] >= 1.0 * sc["amp"]:
                    score += 0.2
                if c > r_mid:
                    score += 0.2
                _emit(i, "ar", h, score)
                ar = {"idx": i, "high": h}
                ar_done = True

        # ── ST 二次测试：回踩 SC 低点 ±0.5×ATR，量 <0.7×SC 量 ──
        if (sc and not st_done and i > (ar["idx"] if ar else sc["idx"])
                and i <= sc["idx"] + ST_WITHIN
                and abs(l - sc["low"]) <= ST_TOL_ATR * atr
                and v < ST_VOL_RATIO * sc["vol"] and _gap_ok("st", i)):
            score = 0.6
            if c > o:
                score += 0.2
            if v < 0.5 * sc["vol"]:
                score += 0.2
            _emit(i, "st", l, score)
            st = {"idx": i}
            st_done = True

        # ── Spring 弹簧：破区间低点后 ≤3 根收回区间内（区间须已发育，
        #    形成期的下探如 SC bar 自身不算弹簧）──
        if spring_ep is None and i - start >= SHAKE_MATURITY and l < r_low and c < r_low:
            spring_ep = {"start": i, "low": l, "vol": v, "poke_high": h}
        elif spring_ep is not None:
            spring_ep["low"] = min(spring_ep["low"], l)
            if c > r_low:                        # 收回区间内 = 确认
                if i - spring_ep["start"] <= SPRING_RECLAIM and _gap_ok("spring", i):
                    score = 0.6
                    if spring_ep["vol"] < vma_arr[spring_ep["start"]]:  # 破位无供给跟随
                        score += 0.2
                    if c > spring_ep["poke_high"]:                      # 强力收复
                        score += 0.2
                    _emit(i, "spring", spring_ep["low"], score)
                    spring = {"idx": i, "low": spring_ep["low"]}
                    test_done = False
                spring_ep = None
            elif i - spring_ep["start"] > SPRING_RECLAIM:
                spring_ep = None                 # 收不回来 = 真破位，交给 SOW
        elif i - start >= SHAKE_MATURITY and l < r_low <= c and _gap_ok("spring", i):
            # 单根下探即收回（影线弹簧）：破位与确认同根
            score = 0.6
            if v < vma:
                score += 0.2
            if c > o:
                score += 0.2
            _emit(i, "spring", l, score)
            spring = {"idx": i, "low": l}
            test_done = False

        # ── Test 测试：Spring 后回踩不破 Spring 低点且缩量 ──
        if (spring and not test_done and spring["idx"] < i <= spring["idx"] + FOLLOW_WITHIN
                and l > spring["low"] and l <= r_low + LPS_NEAR_ATR * atr
                and v < QUIET_VOL_RATIO * vma and _gap_ok("test", i)):
            score = 0.6
            if v < QUIET_VOL_BONUS * vma:
                score += 0.2
            if c > o:
                score += 0.2
            _emit(i, "test", l, score)
            test_done = True

        # ── SOS 强势信号：放量 ≥1.5×均量突破区间中轨/高点且收在 bar 上 1/3 ──
        if c < r_mid:
            sos_armed = True                     # 回到中轨下方重新武装
        if (sos_armed and i >= start and c > r_mid and v >= SOS_VOL_RATIO * vma
                and pos >= CLOSE_POS_STRONG and _gap_ok("sos", i)):
            level = r_high if c > r_high else r_mid
            score = 0.6
            if c > r_high:
                score += 0.2
            if v >= SOS_VOL_BONUS * vma:
                score += 0.2
            _emit(i, "sos", c, score)
            sos = {"idx": i, "level": level}
            lps_done = False
            sos_armed = False

        # ── LPS 最后支撑：SOS 后回踩缩量不破突破位 ──
        if (sos and not lps_done and sos["idx"] < i <= sos["idx"] + FOLLOW_WITHIN
                and l >= sos["level"] - LPS_TOL_ATR * atr
                and l <= sos["level"] + LPS_NEAR_ATR * atr
                and v < QUIET_VOL_RATIO * vma and _gap_ok("lps", i)):
            score = 0.6
            if l >= sos["level"]:
                score += 0.2
            if v < QUIET_VOL_BONUS * vma:
                score += 0.2
            _emit(i, "lps", l, score)
            lps_done = True

        # ── UT 上冲回落 / UTAD 派发上冲（Phase C 位置的 UT + 量价背离；
        #    与 Spring 镜像，区间发育 <SHAKE_MATURITY 根不判定）──
        if ut_ep is None and i - start >= SHAKE_MATURITY and h > r_high and c > r_high:
            ut_ep = {"start": i, "high": h, "vol": v, "poke_low": l}
        elif ut_ep is not None:
            ut_ep["high"] = max(ut_ep["high"], h)
            if c < r_high:                       # 收回区间内 = 确认
                if i - ut_ep["start"] <= SPRING_RECLAIM:
                    _emit_ut_or_utad(bars, i, ut_ep, rng, atr_arr, vma_arr, cvd,
                                     events, last_emit, _gap_ok)
                ut_ep = None
            elif i - ut_ep["start"] > SPRING_RECLAIM:
                ut_ep = None                     # 收不回来 = 真突破，交给 SOS
        elif (i - start >= SHAKE_MATURITY and h > r_high >= c
                and _gap_ok("ut", i) and _gap_ok("utad", i)):
            one = {"start": i, "high": h, "vol": v, "poke_low": l}
            _emit_ut_or_utad(bars, i, one, rng, atr_arr, vma_arr, cvd,
                             events, last_emit, _gap_ok)

        # ── SOW 弱势信号：放量跌破区间中轨/低点 ──
        if c > r_mid:
            sow_armed = True
        if (sow_armed and i >= start and c < r_mid and v >= SOS_VOL_RATIO * vma
                and pos <= 1.0 - CLOSE_POS_STRONG and _gap_ok("sow", i)):
            level = r_low if c < r_low else r_mid
            score = 0.6
            if c < r_low:
                score += 0.2
            if v >= SOS_VOL_BONUS * vma:
                score += 0.2
            _emit(i, "sow", c, score)
            sow = {"idx": i, "level": level}
            lpsy_done = False
            sow_armed = False
            hi_since_sow = h

        # ── LPSY 最后供给：SOW 后无力反弹（缩量 + 高点降低）──
        if sow and sow["idx"] < i:
            if (not lpsy_done and i <= sow["idx"] + FOLLOW_WITHIN
                    and h <= sow["level"] + LPS_TOL_ATR * atr
                    and h >= sow["level"] - LPS_NEAR_ATR * atr
                    and h < hi_since_sow
                    and v < QUIET_VOL_RATIO * vma and _gap_ok("lpsy", i)):
                score = 0.6
                if h <= sow["level"]:
                    score += 0.2
                if v < QUIET_VOL_BONUS * vma:
                    score += 0.2
                _emit(i, "lpsy", h, score)
                lpsy_done = True
            hi_since_sow = max(hi_since_sow, h)

    return events


def _emit_ut_or_utad(bars: list, i: int, ep: dict, rng: dict,
                     atr_arr: list, vma_arr: list, cvd: list | None,
                     events: list, last_emit: dict, gap_ok) -> None:
    """UT 确认时判定是否升级 UTAD：区间成熟（Phase C 位置）+ 量价背离。"""
    start_idx = int(rng.get("start_idx") or 0)
    poke = ep["start"]
    vma_poke = vma_arr[poke] or 1e-12
    mature = poke - start_idx >= UTAD_MATURITY
    stall = ep["vol"] >= SOS_VOL_RATIO * vma_poke and bars[i]["close"] < bars[poke]["open"]
    cvd_div = False
    if cvd is not None and poke >= CVD_LOOKBACK:
        prior_hi = max(b["high"] for b in bars[poke - CVD_LOOKBACK:poke])
        prior_cvd_hi = max(cvd[poke - CVD_LOOKBACK:poke])
        cvd_div = ep["high"] >= prior_hi and cvd[poke] < prior_cvd_hi
    if mature and (stall or cvd_div):
        if not gap_ok("utad", i):
            return
        score = 0.6
        if cvd_div:
            score += 0.2
        if ep["vol"] >= 2.0 * vma_poke:
            score += 0.2
        last_emit["utad"] = i
        events.append(_mk_event(bars, i, "utad", ep["high"], score))
        return
    if not gap_ok("ut", i):
        return
    score = 0.6
    if ep["vol"] < vma_poke:                     # 破位无需求跟随
        score += 0.2
    if bars[i]["close"] < ep["poke_low"]:        # 强力回落
        score += 0.2
    last_emit["ut"] = i
    events.append(_mk_event(bars, i, "ut", ep["high"], score))


# ═══════════════════════════ T2.3 阶段状态机 ═══════════════════════════

# 每档：(判定模式, 事件集)。all = 全部出现才算完成（B 档 AR+ST 双确认，
# 文档 §T2.3 口径）；any = 任一出现即确立。
_ACC_STAGE_EVENTS = {"A": ("any", ("sc",)), "B": ("all", ("ar", "st")),
                     "C": ("any", ("spring",)), "D": ("any", ("sos",)),
                     "E": ("any", ("lps",))}
_DIST_STAGE_EVENTS = {"A": ("any", ("bc",)), "B": ("any", ("ut",)),
                      "C": ("any", ("utad",)), "D": ("any", ("sow",)),
                      "E": ("any", ("lpsy",))}
_PHASE_ORDER = ("A", "B", "C", "D", "E")


def _track_stage(events: list[dict], stage_map: dict) -> tuple[str | None, dict | None, list[str]]:
    """单侧轨道：已出现事件 → 最高完成阶段 + 确立事件 + 跳过的档位。"""
    seen: dict[str, dict] = {}
    for e in events:
        typ = e["type"]
        if typ not in seen:
            seen[typ] = e
    phase, anchor = None, None
    for ph in _PHASE_ORDER:
        mode, types = stage_map[ph]
        hits = [seen[t] for t in types if t in seen]
        done = (len(hits) == len(types)) if mode == "all" else bool(hits)
        if done:
            phase = ph
            anchor = max(hits, key=lambda e: e["bar_idx"])
    if phase is None:
        return None, None, []
    reached = _PHASE_ORDER[:_PHASE_ORDER.index(phase) + 1]
    skipped = []
    for ph in reached:
        mode, types = stage_map[ph]
        hits = [t for t in types if t in seen]
        done = (len(hits) == len(types)) if mode == "all" else bool(hits)
        if not done:
            skipped.append(ph)
    return phase, anchor, skipped


def resolve_phase(events: list, rng: dict | None, last_close: float | None = None) -> dict:
    """事件序列 → {"side": "acc|dist|trend|unknown", "phase": "A|B|C|D|E",
    "since_ts": ...}。推进规则：
      A = SC/BC 出现；B = AR+ST 完成（派发侧以 UT 为 B 档测试）；
      C = Spring/UTAD 出现；D = SOS/SOW 确认；E = LPS/LPSY 后离开区间。
    容错：事件缺失允许跳档但 side 置信降一档（confidence 字段）；
    无区间 → trend；有区间无事件 → unknown。last_close 用于 E 档
    「离开区间」判定（缺省时封顶 D 档，phase_note 说明）。"""
    if not rng:
        return {"side": "trend", "phase": None, "since_ts": None,
                "confidence": "low", "skipped": [],
                "note": "无有效交易区间（趋势段），威科夫阶段不适用"}
    if not events:
        return {"side": "unknown", "phase": None, "since_ts": int(rng["start_ts"]),
                "confidence": "low", "skipped": [],
                "note": "区间内尚未出现可识别的威科夫事件"}

    acc_ev = [e for e in events if e["type"] in ACC_TYPES]
    dist_ev = [e for e in events if e["type"] in DIST_TYPES]
    acc_ph, acc_anchor, acc_skip = _track_stage(acc_ev, _ACC_STAGE_EVENTS)
    dist_ph, dist_anchor, dist_skip = _track_stage(dist_ev, _DIST_STAGE_EVENTS)

    cands = []
    if acc_ph:
        cands.append(("acc", acc_ph, acc_anchor, acc_skip))
    if dist_ph:
        cands.append(("dist", dist_ph, dist_anchor, dist_skip))
    if not cands:
        return {"side": "unknown", "phase": None, "since_ts": int(rng["start_ts"]),
                "confidence": "low", "skipped": [],
                "note": "区间内尚未出现可识别的威科夫事件"}
    # 双轨并存：最近确立事件者胜；同 bar 则取阶段更深的一侧
    side, phase, anchor, skipped = max(
        cands, key=lambda c: (c[2]["bar_idx"], _PHASE_ORDER.index(c[1])))

    note = ""
    if phase == "E":
        left = None
        if last_close is not None:
            left = last_close > rng["high"] if side == "acc" else last_close < rng["low"]
        if left is False:
            phase = "D"
            note = "LPS/LPSY 已现但价格尚未离开区间，阶段回落 D 档观察"
        elif left is None:
            phase = "D"
            note = "缺少最新收盘价，无法确认已离开区间，暂按 D 档"

    conf = {0: "high", 1: "medium"}.get(len(skipped), "low")
    return {"side": side, "phase": phase, "since_ts": int(anchor["ts"]),
            "confidence": conf, "skipped": skipped,
            "last_event": {"type": anchor["type"], "ts": int(anchor["ts"]),
                           "price": anchor["price"]},
            "note": note or f"依据 {anchor['type'].upper()} 等 {len(events)} 个事件推进"}


# ═══════════════════════════ T2.4 事件×订单流证据绑定 ═══════════════════════════


def enrich(events: list, sd: dict | None, last_bar_idx: int,
           window: int = 5) -> list[dict]:
    """事件 × 订单流证据绑定（纯函数：吃现成的 supply_demand 裁决结果）。
    只绑定最近事件（bar_idx 距最新 ≤window 根，供需证据是「此刻」口径，
    不能穿越回老事件）；方向一致 confidence = min(1, raw×(1+0.5×sd_conf))，
    相逆 = raw×0.4（叙事与实锤打架不给高分），中性/无证据 = raw 原样。"""
    sd_ok = bool(sd and sd.get("ok"))
    bias = sd.get("bias") if sd_ok else None
    sd_conf = float(sd.get("confidence") or 0.0) if sd_ok else 0.0
    reasons: list[str] = []
    if sd_ok:
        reasons = [e["detail"] for e in (sd.get("evidence_chain") or [])
                   if e.get("direction")][:3]
        bc = sd.get("breakout_check") or {}
        if bc.get("active") and bc.get("reasons"):
            reasons = (bc["reasons"] + reasons)[:4]

    out: list[dict] = []
    for e in events:
        item = dict(e)
        raw = float(e["raw_score"])
        recent = (last_bar_idx - int(e["bar_idx"])) <= window
        if recent and sd_ok and bias in ("accumulation", "distribution"):
            match = (bias == "accumulation") == (e["type"] in ACC_TYPES)
            if match:
                item["confidence"] = round(min(1.0, raw * (1.0 + 0.5 * sd_conf)), 3)
            else:
                item["confidence"] = round(raw * 0.4, 3)
            item["sd_bias"] = bias
            item["reasons"] = reasons
        else:
            item["confidence"] = round(raw, 3)
            item["sd_bias"] = bias if (recent and sd_ok) else None
            item["reasons"] = reasons if (recent and sd_ok) else []
        out.append(item)
    return out


# ═══════════════════════════ T2.6 历史回放回测（诚实口径） ═══════════════════════════


def run_backtest(symbol: str = "BTCUSDT", interval: str = "1h", days: int = 90,
                 horizon: int = BACKTEST_HORIZON, bars: list | None = None) -> dict:
    """逐 bar 滚动重放 detect_events（窗口内重算区间+事件，只认「确认于窗口
    最后一根」的新事件 = 当时真实可得的信号，防前瞻）。
    命中口径：Spring/LPS 触发后 N=12 根收盘 > 触发 bar 收盘；UTAD/LPSY 镜像。
    基线 = 全体可评估 bar 的同口径概率（市场自身漂移）。
    样本 <30 输出 insufficient_samples，禁止小样本吹牛（同 delta_flow 纪律）。
    bars 可注入（离线冒烟）；缺省经 delta_flow.fetch_bars 拉取（≤500 根）。"""
    import jarvis_delta_flow as jdf
    sym = jdf._norm_symbol(symbol)
    tf = jdf._norm_tf(interval)
    if bars is None:
        per_day = 86400.0 / jdf.TF_SECONDS[tf]
        want = min(jdf.LIMIT_MAX, max(RANGE_HIST_MIN + horizon + 20,
                                      int(days * per_day) + horizon))
        bars = jdf.fetch_bars(sym, tf, want, max_n=jdf.LIMIT_MAX)
    n = len(bars) if bars else 0
    if n < RANGE_HIST_MIN + horizon + 10:
        return {"ok": False, "symbol": sym, "interval": tf,
                "error": f"历史数据不足（{n} 根 < {RANGE_HIST_MIN + horizon + 10}），无法回测"}

    win = min(RANGE_LOOKBACK, n)
    watch = set(BACKTEST_BULL) | set(BACKTEST_BEAR)
    signals: list[tuple[str, int]] = []          # (type, 绝对 bar_idx)
    seen: set[tuple[str, int]] = set()
    for i in range(RANGE_HIST_MIN - 1, n):
        lo = max(0, i - win + 1)
        seg = bars[lo:i + 1]
        rng = detect_range(seg)
        if not rng:
            continue
        for e in detect_events(seg, rng):
            if e["type"] not in watch or e["bar_idx"] != len(seg) - 1:
                continue                          # 只认确认于「当前 bar」的新信号
            key = (e["type"], e["ts"])
            if key in seen:
                continue
            seen.add(key)
            signals.append((e["type"], i))

    evaluable = range(RANGE_HIST_MIN - 1, n - horizon)
    up_base_hits = sum(1 for i in evaluable if bars[i + horizon]["close"] > bars[i]["close"])
    dn_base_hits = sum(1 for i in evaluable if bars[i + horizon]["close"] < bars[i]["close"])
    base_total = max(0, n - horizon - (RANGE_HIST_MIN - 1))
    up_base = up_base_hits / base_total if base_total else None
    dn_base = dn_base_hits / base_total if base_total else None

    per_event: dict[str, dict] = {}
    total_samples = 0
    for typ in (*BACKTEST_BULL, *BACKTEST_BEAR):
        bull = typ in BACKTEST_BULL
        idxs = [i for t, i in signals if t == typ and i + horizon < n]
        hits = sum(1 for i in idxs
                   if (bars[i + horizon]["close"] > bars[i]["close"]) == bull)
        samples = len(idxs)
        total_samples += samples
        base = up_base if bull else dn_base
        blk = {"samples": samples, "hits": hits,
               "hit_rate": round(hits / samples, 4) if samples else None,
               "baseline": round(base, 4) if base is not None else None,
               "uplift": (round(hits / samples - base, 4)
                          if samples and base is not None else None)}
        if samples < BACKTEST_MIN_SAMPLES:
            blk["verdict"] = "insufficient_samples"
        per_event[typ] = blk

    status = "ok" if total_samples >= BACKTEST_MIN_SAMPLES else "insufficient_samples"
    span_days = (bars[-1]["ts"] - bars[0]["ts"]) / 1000.0 / 86400.0
    return {
        "ok": True, "symbol": sym, "interval": tf, "bars": n,
        "span_days": round(span_days, 1), "horizon": horizon,
        "total_samples": total_samples, "status": status,
        "per_event": per_event,
        "basis": ("信号=滚动重放中确认于当根的 Spring/LPS（看涨）与 UTAD/LPSY（看跌）；"
                  f"命中=确认后 {horizon} 根收盘价按事件方向优于触发收盘；"
                  "基线=全体可评估 bar 同口径概率；uplift=命中率−基线。"
                  f"任一侧样本 <{BACKTEST_MIN_SAMPLES} 视为 insufficient_samples，"
                  "不作统计结论。"),
        "generatedAt": int(time.time()),
        "disclaimer": DISCLAIMER,
    }


# ═══════════════════════════ 门面（指纹缓存 + 供 /api/wyckoff 消费） ═══════════════════════════

_CACHE: dict[tuple[str, str], dict] = {}
_CACHE_LOCK = threading.Lock()
MAX_EVENTS_OUT = 40

_HINTS = {
    ("acc", "A"): "Phase A 恐慌抛售已现：等待 AR/ST 确认区间，不抄底不追空",
    ("acc", "B"): "Phase B 区间构筑中：高抛低吸区，等待 Spring/SOS 方向确认",
    ("acc", "C"): "Phase C 弹簧已确认 + 吸筹证据链支持：等待 SOS/LPS 入场结构",
    ("acc", "D"): "Phase D 强势信号确认：回踩 LPS 是顺势多头入场结构",
    ("acc", "E"): "Phase E 离开区间进入上升趋势：持有为主，回踩区间顶不破前多头占优",
    ("dist", "A"): "Phase A 抢购高潮已现：追涨风险高，等待区间确认",
    ("dist", "B"): "Phase B 派发区间构筑中：反弹至区间上沿谨慎追多",
    ("dist", "C"): "Phase C UTAD 派发上冲确认：假突破风险兑现，反弹是减仓/试空结构",
    ("dist", "D"): "Phase D 弱势信号确认：反抽 LPSY 是顺势空头入场结构",
    ("dist", "E"): "Phase E 离开区间进入下降趋势：反弹不过区间底前空头占优",
}


def analyze(symbol: str, interval: str = "1h", limit: int = ANALYZE_LIMIT) -> dict:
    """/api/wyckoff 消费入口：取数 → 区间 → 事件 → 阶段 → 证据绑定。永不抛出。
    以最后一根已收盘 bar 的 open_time 做指纹，新 bar 出现才重算（性能纪律 §四.3）。"""
    try:
        import jarvis_delta_flow as jdf
        sym = jdf._norm_symbol(symbol)
        tf = jdf._norm_tf(interval)
        bars = jdf.fetch_bars(sym, tf, min(int(limit), jdf.LIMIT_MAX))
        if not bars or len(bars) < RANGE_HIST_MIN:
            return {"ok": False, "symbol": sym, "interval": tf,
                    "error": f"K线不足（{len(bars) if bars else 0} 根 < {RANGE_HIST_MIN}），"
                             "无法识别威科夫结构", "disclaimer": DISCLAIMER}
        fp = int(bars[-1]["ts"])
        key = (sym, tf)
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and hit.get("_fp") == fp:
                return {k: v for k, v in hit.items() if k != "_fp"}

        rng = detect_range(bars)
        events = detect_events(bars, rng)
        state = resolve_phase(events, rng, last_close=bars[-1]["close"])

        sd = None
        try:
            import jarvis_supply_demand as jsd
            got = jsd.analyze(sym, tf)
            if got.get("ok"):
                sd = got
        except Exception:  # noqa: BLE001 — 证据缺失降级，不拖垮叙事层
            sd = None
        events_rich = enrich(events, sd, last_bar_idx=len(bars) - 1)

        hint = _HINTS.get((state.get("side"), state.get("phase")))
        if not hint:
            hint = {"trend": "趋势段无区间结构：威科夫阶段不适用，跟随趋势纪律",
                    "unknown": "区间成立但事件未现：观察等待，不预判方向"}.get(
                        state.get("side"), "结构不明确，保持观察")
        if sd and state.get("side") in ("acc", "dist"):
            agree = (sd["bias"] == "accumulation") == (state["side"] == "acc")
            if sd["bias"] != "neutral":
                hint += "（订单流证据" + ("同向支持" if agree else "反向警示，降信处理") + "）"

        out = {
            "ok": True, "symbol": sym, "interval": tf, "ts": time.time(),
            "range": ({"high": rng["high"], "low": rng["low"], "mid": rng["mid"],
                       "start_ts": rng["start_ts"], "bars_in_range": rng["bars_in_range"],
                       "atr": rng["atr"]} if rng else None),
            "state": state,
            "events": [{k: v for k, v in e.items() if k != "side"}
                       for e in events_rich[-MAX_EVENTS_OUT:]],
            "verdict_hint": hint,
            "sd_attached": bool(sd),
            "disclaimer": DISCLAIMER,
        }
        with _CACHE_LOCK:
            _CACHE[key] = {**out, "_fp": fp}
        return out
    except Exception as exc:  # noqa: BLE001 — 引擎层绝不拖垮 dashboard
        return {"ok": False, "symbol": (symbol or "").upper(), "interval": interval,
                "error": repr(exc)[:200], "disclaimer": DISCLAIMER}


# ═══════════════════════════ CLI ═══════════════════════════


def main() -> int:
    ap = argparse.ArgumentParser(description="威科夫阶段引擎（区间/事件/阶段/回测）")
    ap.add_argument("symbol", nargs="?", default="BTCUSDT")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--limit", type=int, default=ANALYZE_LIMIT)
    ap.add_argument("--backtest", action="store_true", help="历史回放回测")
    ap.add_argument("--days", type=int, default=90, help="回测跨度（受 500 根上限约束）")
    ap.add_argument("--horizon", type=int, default=BACKTEST_HORIZON)
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = ap.parse_args()

    if args.backtest:
        out = run_backtest(args.symbol, args.interval, days=args.days,
                           horizon=args.horizon)
        print(json.dumps(out, ensure_ascii=False, indent=None if args.json else 2))
        return 0 if out.get("ok") else 1

    out = analyze(args.symbol, args.interval, args.limit)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    if not out.get("ok"):
        print(f"❌ 分析失败：{out.get('error')}")
        return 1
    st = out["state"]
    side_cn = {"acc": "🟢 吸筹", "dist": "🔴 派发", "trend": "➡️ 趋势",
               "unknown": "⚪ 未明"}[st["side"]]
    rng = out["range"]
    print(f"{out['symbol']} {out['interval']}  {side_cn}"
          f"{(' Phase ' + st['phase']) if st.get('phase') else ''}"
          f"  置信={st.get('confidence')}")
    if rng:
        print(f"区间 [{rng['low']} ~ {rng['high']}] mid={rng['mid']} "
              f"({rng['bars_in_range']} 根, ATR={rng['atr']})")
    print(f"提示：{out['verdict_hint']}")
    for e in out["events"][-10:]:
        t = time.strftime("%m-%d %H:%M", time.localtime(e["ts"]))
        sd_tag = f" sd={e['sd_bias']}" if e.get("sd_bias") else ""
        print(f"  [{e['type']:<6}] {t} @{e['price']} raw={e['raw_score']}"
              f" conf={e['confidence']}{sd_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
