#!/usr/bin/env python3
"""贾维斯 JARVIS — MFE/MAE 止盈止损调优分析器（只读分析，不接入交易链路）。

回答一个问题：**每格（体系×TF）的计划 TP/SL 摆位，与行情实际给到的空间
（MFE/MAE 分布）相比，存在多少结构性浪费？**

两类结构性浪费（本工具的定位对象）：
  ① 利润回吐型：TP 设在 MFE（最大有利偏移）分布够不到的位置——
     行情给过利润，但没走到 TP 就回吐，最终以 sl/flip/timeout 出局；
  ② 冤枉止损型：SL 设在 MAE（最大不利偏移）常规波动带内——
     常规扰动打掉仓位，随后行情在固定视界内走到原 TP。

⚠ 措辞纪律（裁决 3，写死）：止损距离在历史样本上**不携带期望值信息**——
  胜率与赔率在同一条等期望线上滑动（A/B 分组期望差 -0.0073U，p=0.973）。
  因此本工具的交付物是「减少结构性浪费 / 改善实现质量」，
  不承诺、不暗示「提高期望」。所有推荐点位仅为建议，不自动生效。

⚠ T1 纪律（写死）：台账 `exit_price` 携带止损结算伪影（悲观偏差 51.35U，
  占净亏 28%，134/240 笔止损劣于计划位、止盈恒等于计划位）。本模块**禁止**
  用 exit_price 计算任何偏移量——SQL 里根本不 SELECT 它，物理杜绝误用。
  一律用币安 1m K 线原始 high/low 重建价格路径。

只读：pg 仅 SELECT（`_query` 有前缀断言）；不写库、不下单、不改任何现有文件。
出网：仅经 `jarvis_crypto_data._get` 拉 1m K 线——与
  `jarvis_twelve_systems.fetch_klines_df` 同源同参（FAPI /fapi/v1/klines），
  继承其限频三道闸（TTL 直出 / 封禁短路 / 分钟预算）。fetch_klines_df 本身
  只能取最近 500 根、覆盖不了历史区间，故本模块经同一链路带 startTime 分页，
  并另建**不可变本地页缓存**（历史 bar 永不变，缓存永久有效）支持断点续跑。

用法：
    python3 jarvis_tpsl_advisor.py run --days 30 --symbol ETHUSDT
    python3 jarvis_tpsl_advisor.py run --days 30 --symbol ETHUSDT --by-direction
    python3 jarvis_tpsl_advisor.py run --offline          # 只用本地缓存，不出网
    python3 jarvis_tpsl_advisor.py run --max-requests 10  # 限出网页数，分批断点续跑

JSON 产物：~/.vibe-trading/tpsl_advisor/{SYMBOL}.json（若依端展示可直接接）。

────────────────────────── 预登记（跑数之前锁定） ──────────────────────────
本节在看任何结果之前写定，`PREREG` 常量随每份报告一并打印。
改动本节必须同时改 `PREREG_VERSION` 并重跑全部历史结论。

P1 价格源
    MFE/MAE 一律用 1m bar 原始 high/low 计算；**禁止** exit_price（T1 伪影）。
    主源 = 币安 USDⓈ-M 1m K 线（交易所权威口径）；
    降级源 = 本地 `tape_minute_bars`（IP 限频封禁/离线时按分钟补缺；其 bar 由
    本地采集的逐笔聚合而来，只含采到的部分 → high/low 可能窄于交易所真值，
    MFE/MAE 因此是**下界估计**：浪费①「TP 过远」的证据会被夸大、
    浪费②「冤枉止损」的证据只会偏弱——覆盖率低时结论只作方向性参考）。
    entry 用台账 entry_price（成交价，不受结算伪影污染）；SL/TP 用计划位原值。

P2 窗口
    自然窗口 = [entry 所在分钟 +1, exit 所在分钟]（首根 bar 含 entry 前行情，排除；
    粒度损失 ≤1 分钟，方向为轻微低估 MFE 与 MAE，两侧对称不引入方向性偏差）。
    固定视界对照 = [entry 所在分钟 +1, entry + 24 × TF 根 bar]，尾部截到数据末端。
    自然窗口空（开平同分钟）→ 该笔自然窗口记「不可测」，固定视界照算。

P3 R 归一
    R = |entry − 计划 stop_loss| ÷ entry（计划口径）。R ≤ 0 → 剔除并计数。
    TP 距离同法归一为 R 倍数；take_profit 缺失或距离 ≤ 0 → TP 相关统计剔除并计数。

P4 聚合与样本充分性
    分位数 P25/P50/P60/P75/P90；格子（体系×TF）n < 30 → 「不可判定」，
    **禁止输出任何推荐数字**（对齐 jarvis_signal_floor 的预登记纪律）。

P5 先后重放（冤枉止损判定）
    固定视界内逐 1m bar 扫描原计划 SL/TP 价位；同一根 bar 内两者皆可触发时
    按「先 SL」保守口径（回测悲观标准）。冤枉止损 = exit_reason='sl' 且
    重放中先触 SL、其后固定视界内价格达到过原 TP。

P6 推荐口径（仅建议，不自动生效）
    TP 建议 = 固定视界 MFE_R 分布的 [P55, P65]（任务书区间，取 P60 为中值）。
    SL 建议 = 「TP-hitters（固定视界内曾达建议 TP 中值的交易）」的
              **峰前 MAE_R**（到达利润峰值之前的最大不利偏移；峰值到手后的
              回撤与护损无关，用全视界 MAE 会系统性夸大 SL 需求）P90 + 扫单缓冲；
              hitters 子集 n<10 时回退全体峰前 MAE_R P90 并标注。
    扫单缓冲 = 该 TF 的 ATR14% 中位 × 1.0（对齐 jarvis_stop_hunt.ATR_MULT——
    插针深度 ≤ 1×ATR 是典型扫单特征）÷ 该格计划 R% 中位，换算成 R 倍数。
    峰值所在 bar 计入峰前窗口（bar 内先后不可知，保守含入，与 P5 同哲学）。
    SL 放宽必须同比例缩仓保持 1R 名义风险恒定（T2 契约），本工具只给点位。

P7 措辞纪律
    输出禁用「提高期望/提升期望/改善期望/期望转正」；只说
    「减少结构性浪费 / 改善实现质量」；结论必须带样本量；
    比例类结论带 Wilson 95% 置信区间。`_discipline_check` 在打印前强制自检。
────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field

import numpy as np

import jarvis_db
from jarvis_stop_hunt import ATR_MULT, ATR_PERIOD

PREREG_VERSION = "tpsl-advisor-prereg-v1"

PREREG = {
    "version": PREREG_VERSION,
    "P1_price_source": "1m bar 原始 high/low（主源币安K线，降级源本地tape=下界估计）；"
                       "禁止 exit_price（T1 伪影）；SL/TP 用计划位原值",
    "P2_window": "自然=[entry分钟+1, exit分钟]；固定视界=entry后24根TF bar，尾部截断",
    "P3_normalize": "R=|entry−计划SL|/entry；R≤0 或 TP距离≤0 剔除并计数",
    "P4_sufficiency": "P25/P50/P60/P75/P90；格子 n<30 不可判定，禁止给数字",
    "P5_replay": "固定视界逐1m bar；同bar双触按先SL（悲观）",
    "P6_suggest": "TP=[P55,P65] of MFE_R(固定)；SL=hitters 峰前MAE_R P90+1×ATR缓冲",
    "P7_wording": "只说减少结构性浪费/改善实现质量；比例带Wilson 95%CI",
}

TF_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
FIXED_HORIZON_BARS = 24     # 固定视界对照：开仓后 24 根该 TF 的 bar
MIN_CELL_N = 30             # P4：格子样本充分性门槛
MIN_HITTERS_N = 10          # P6：TP-hitters 子集最小样本，不足回退全体
PCTS = (25, 50, 60, 75, 90)

PAGE_MINUTES = 500          # 每页 500 根 1m bar（对齐 fetch_klines_df 的 500 上限）
DEFAULT_MAX_REQUESTS = 40   # 单轮出网页数上限（限频纪律，超出留到下轮续跑）

ADVISOR_DIR = os.path.expanduser("~/.vibe-trading/tpsl_advisor")
KLINE_CACHE_DIR = os.path.join(ADVISOR_DIR, "kline_cache")

# P7：打印前强制自检的禁词（裁决 3 措辞纪律）
FORBIDDEN_PHRASES = ("提高期望", "提升期望", "改善期望", "期望转正", "期望值提升")


# ─────────────────────────── 只读数据访问 ───────────────────────────

def _query(sql: str, params: tuple = ()) -> list[dict]:
    """跑一条 SELECT 并返回 dict 行。非 SELECT/WITH 直接拒绝（只读红线）。"""
    head = sql.lstrip()[:6].upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise ValueError(f"jarvis_tpsl_advisor 只允许 SELECT，收到：{sql.lstrip()[:40]!r}")
    dsn = jarvis_db.db_url()
    if not dsn:
        raise RuntimeError("未配置 PostgreSQL（~/.vibe-trading/db.json 或 JARVIS_DB_URL）")
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return list(conn.execute(sql, params).fetchall())


def load_trades(symbol: str, days: int) -> list[dict]:
    """载入窗口内已平仓单。**不 SELECT exit_price**——T1 伪影列物理隔离。"""
    since = time.time() - days * 86400.0
    return _query(
        "SELECT id, symbol, tf, system, direction, entry_price, entry_ts, exit_ts, "
        "stop_loss, take_profit, exit_reason "
        "FROM twelve_sim_trade WHERE symbol = %s AND exit_ts >= %s ORDER BY entry_ts",
        (symbol, since))


# ─────────────────────────── 1m K 线：分页拉取 + 不可变缓存 ───────────────────────────

def _page_path(symbol: str, page_start: int) -> str:
    return os.path.join(KLINE_CACHE_DIR, f"{symbol}_1m_p{page_start}.json")


def _fetch_page(symbol: str, page_start: int) -> dict[int, tuple] | None:
    """出网拉一页（500 根 1m bar），经 jcd._get 限频三道闸。失败返回 None。"""
    import jarvis_crypto_data as jcd
    raw = jcd._get(jcd.FAPI + "/fapi/v1/klines",
                   {"symbol": symbol, "interval": "1m",
                    "startTime": page_start * 60_000, "limit": PAGE_MINUTES},
                   ttl=3600)
    if not isinstance(raw, list):
        return None
    out: dict[int, tuple] = {}
    for k in raw:
        try:
            out[int(k[0]) // 60_000] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def load_minute_bars(symbol: str, start_ts: float, end_ts: float, *,
                     offline: bool = False,
                     max_requests: int = DEFAULT_MAX_REQUESTS) -> tuple[dict[int, tuple], dict]:
    """载入 [start_ts, end_ts] 的 1m bar：{minute: (o,h,l,c)}。

    页缓存纪律：只有「整页都已成为历史」（页末 < 当前分钟-1）的完整页才落盘，
    历史 K 线不可变 → 缓存永久有效，断点续跑零出网。
    返回 (bars, 取数统计)。
    """
    os.makedirs(KLINE_CACHE_DIR, exist_ok=True)
    now_min = int(time.time() // 60)
    lo = int(start_ts // 60) // PAGE_MINUTES * PAGE_MINUTES
    hi = min(int(end_ts // 60), now_min - 1)
    bars: dict[int, tuple] = {}
    stats = {"pages_total": 0, "pages_cached": 0, "pages_fetched": 0,
             "pages_failed": 0, "pages_deferred": 0}
    page = lo
    while page <= hi:
        stats["pages_total"] += 1
        path = _page_path(symbol, page)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    bars.update({int(k): tuple(v) for k, v in json.load(f).items()})
                stats["pages_cached"] += 1
                page += PAGE_MINUTES
                continue
            except (OSError, ValueError):
                pass  # 缓存页损坏 → 当缺页走出网
        if offline or stats["pages_fetched"] >= max_requests:
            stats["pages_deferred"] += 1
            page += PAGE_MINUTES
            continue
        got = _fetch_page(symbol, page)
        if got is None:
            stats["pages_failed"] += 1
            page += PAGE_MINUTES
            continue
        stats["pages_fetched"] += 1
        bars.update(got)
        if page + PAGE_MINUTES - 1 < now_min - 1 and got:
            # 完整历史页 → 永久落盘（原子写，防半截文件被下轮误读）
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({str(k): list(v) for k, v in got.items()}, f)
            os.replace(tmp, path)
        page += PAGE_MINUTES
    return bars, stats


def load_tape_bars(symbol: str, start_min: int, end_min: int) -> dict[int, tuple]:
    """降级源：本地 tape_minute_bars 的分钟 OHLC（P1：仅补币安缺口，下界估计）。

    `minute` 是 epoch 分钟索引（jarvis_signal_floor 踩过的坑：×60 才是 epoch 秒）。
    """
    rows = _query(
        "SELECT minute, open_price, high_price, low_price, close_price "
        "FROM tape_minute_bars WHERE symbol = %s AND minute BETWEEN %s AND %s "
        "AND high_price IS NOT NULL AND low_price IS NOT NULL",
        (symbol, start_min, end_min))
    return {int(r["minute"]): (float(r["open_price"] or r["close_price"]),
                               float(r["high_price"]), float(r["low_price"]),
                               float(r["close_price"]))
            for r in rows}


# ─────────────────────────── 每笔交易：MFE/MAE 与先后重放（纯函数） ───────────────────────────

@dataclass
class TradeExcursion:
    """一笔已平仓单在 1m 价格路径上的偏移画像（全部按 R 归一）。"""
    system: str
    tf: str
    direction: str
    exit_reason: str
    r_pct: float                    # 计划风险距离%（P3）
    tp_r: float | None              # 计划 TP 距离（R 倍数）；无有效 TP 为 None
    mfe_nat_r: float | None = None  # 自然窗口（不可测为 None）
    mae_nat_r: float | None = None
    mfe_fix_r: float | None = None  # 固定视界（无 bar 覆盖为 None）
    mae_fix_r: float | None = None
    mae_fix_prepeak_r: float | None = None  # 固定视界峰前 MAE（P6 护损口径）
    first_touch: str = "none"       # 固定视界重放：sl / tp / none
    tp_after_sl: bool = False       # 先 SL 后达原 TP（冤枉止损证据）
    bars_nat: int = 0
    bars_fix: int = 0
    cov_fix: float = 0.0            # 固定视界 bar 覆盖率（缺口=下界估计的程度）


def _excursion(entry: float, direction: str, bars: dict[int, tuple],
               m_from: int, m_to: int) -> tuple[float, float, int, float]:
    """[m_from, m_to] 闭区间内按方向算 (MFE%, MAE%, 覆盖bar数, 峰前MAE%)。

    峰前 MAE = 首次到达 MFE 峰值的那根 bar（含）之前的最大不利偏移——
    护住赢家所需的止损深度只由「利润到手之前」的回撤决定（P6）。
    """
    hi = lo = None
    n = 0
    peak_m = None
    for m in range(m_from, m_to + 1):
        b = bars.get(m)
        if b is None:
            continue
        n += 1
        if hi is None or b[1] > hi:
            hi = b[1]
            if direction == "long":
                peak_m = m
        if lo is None or b[2] < lo:
            lo = b[2]
            if direction == "short":
                peak_m = m
    if n == 0:
        return 0.0, 0.0, 0, 0.0
    if direction == "long":
        mfe = max(0.0, (hi - entry) / entry * 100.0)
        mae = max(0.0, (entry - lo) / entry * 100.0)
        pre_lo = min((bars[m][2] for m in range(m_from, peak_m + 1) if m in bars),
                     default=entry)
        mae_pre = max(0.0, (entry - pre_lo) / entry * 100.0)
    else:
        mfe = max(0.0, (entry - lo) / entry * 100.0)
        mae = max(0.0, (hi - entry) / entry * 100.0)
        pre_hi = max((bars[m][1] for m in range(m_from, peak_m + 1) if m in bars),
                     default=entry)
        mae_pre = max(0.0, (pre_hi - entry) / entry * 100.0)
    return mfe, mae, n, mae_pre


def _replay_first_touch(direction: str, sl: float, tp: float | None,
                        bars: dict[int, tuple], m_from: int, m_to: int) -> tuple[str, bool]:
    """固定视界逐 bar 重放原计划 SL/TP 价位（P5：同 bar 双触先 SL）。

    返回 (first_touch, tp_after_sl)。tp 为 None 时只可能触 SL。
    """
    first = "none"
    tp_after_sl = False
    for m in range(m_from, m_to + 1):
        b = bars.get(m)
        if b is None:
            continue
        h, l = b[1], b[2]
        hit_sl = (l <= sl) if direction == "long" else (h >= sl)
        hit_tp = tp is not None and ((h >= tp) if direction == "long" else (l <= tp))
        if first == "none":
            if hit_sl:
                first = "sl"          # P5：双触保守判先 SL
            elif hit_tp:
                first = "tp"
                break
        elif first == "sl" and hit_tp:
            tp_after_sl = True
            break
    return first, tp_after_sl


def analyze_trade(trade: dict, bars: dict[int, tuple],
                  data_end_min: int) -> TradeExcursion | None:
    """单笔交易 → 偏移画像。R≤0 剔除（P3），返回 None。"""
    entry = float(trade["entry_price"])
    sl = trade.get("stop_loss")
    if entry <= 0 or sl is None:
        return None
    r_pct = abs(entry - float(sl)) / entry * 100.0
    if r_pct <= 0:
        return None
    direction = str(trade["direction"])
    tf_min = TF_MINUTES.get(str(trade["tf"]), 60)

    tp = trade.get("take_profit")
    tp_r: float | None = None
    if tp is not None:
        tp_dist = abs(float(tp) - entry) / entry * 100.0
        # 方向校验：TP 必须在有利侧（flip 类留痕可能出现 TP=entry 或反侧）
        good_side = (float(tp) > entry) if direction == "long" else (float(tp) < entry)
        if tp_dist > 0 and good_side:
            tp_r = tp_dist / r_pct

    entry_min = int(float(trade["entry_ts"]) // 60)
    exit_min = int(float(trade["exit_ts"]) // 60)
    m_from = entry_min + 1                                   # P2：排除开仓 bar
    fix_to = min(entry_min + FIXED_HORIZON_BARS * tf_min, data_end_min)

    exc = TradeExcursion(system=str(trade["system"]), tf=str(trade["tf"]),
                         direction=direction, exit_reason=str(trade["exit_reason"]),
                         r_pct=r_pct, tp_r=tp_r)
    if exit_min >= m_from:
        mfe, mae, n, _ = _excursion(entry, direction, bars, m_from, exit_min)
        if n:
            exc.mfe_nat_r, exc.mae_nat_r, exc.bars_nat = mfe / r_pct, mae / r_pct, n
    if fix_to >= m_from:
        mfe, mae, n, mae_pre = _excursion(entry, direction, bars, m_from, fix_to)
        if n:
            exc.mfe_fix_r, exc.mae_fix_r, exc.bars_fix = mfe / r_pct, mae / r_pct, n
            exc.mae_fix_prepeak_r = mae_pre / r_pct
            exc.cov_fix = n / (fix_to - m_from + 1)
            exc.first_touch, exc.tp_after_sl = _replay_first_touch(
                direction, float(sl),
                float(tp) if tp_r is not None else None, bars, m_from, fix_to)
    return exc


# ─────────────────────────── ATR 缓冲（复用 stop_hunt 口径） ───────────────────────────

def tf_atr_pct_median(bars: dict[int, tuple], tf_min: int) -> float | None:
    """1m bar 聚合成 TF bar 后，滚动 ATR14 相对收盘价（%）的中位数。

    扫单缓冲的量纲来源：jarvis_stop_hunt 判定「插针深度 ≤ ATR_MULT×ATR」
    为典型扫单，故 SL 建议在结构位之外再留 1×ATR。
    """
    if not bars:
        return None
    groups: dict[int, list[tuple]] = {}
    for m in sorted(bars):
        groups.setdefault(m // tf_min, []).append(bars[m])
    keys = sorted(groups)
    if len(keys) < ATR_PERIOD + 2:
        return None
    ohlc = []
    for k in keys:
        rows = groups[k]
        ohlc.append((rows[0][0], max(r[1] for r in rows),
                     min(r[2] for r in rows), rows[-1][3]))
    trs = []
    for i in range(1, len(ohlc)):
        h, l, pc = ohlc[i][1], ohlc[i][2], ohlc[i - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_pcts = []
    for i in range(ATR_PERIOD - 1, len(trs)):
        atr = sum(trs[i - ATR_PERIOD + 1: i + 1]) / ATR_PERIOD
        close = ohlc[i + 1][3]
        if close > 0:
            atr_pcts.append(atr / close * 100.0)
    return float(np.median(atr_pcts)) if atr_pcts else None


# ─────────────────────────── 聚合与推荐（纯函数） ───────────────────────────

def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """比例的 Wilson 95% 置信区间（P7：比例类结论必须带 CI）。"""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _pcts(vals: list[float]) -> dict[str, float]:
    arr = np.percentile(np.array(vals), PCTS)
    return {f"p{p}": round(float(v), 4) for p, v in zip(PCTS, arr)}


@dataclass
class CellReport:
    """一个（体系×TF[×方向]）格子的聚合结论。"""
    system: str
    tf: str
    direction: str          # "all" 或 long/short
    n: int
    verdict: str            # OK / INSUFFICIENT（不可判定）
    need_n: int = MIN_CELL_N
    n_no_tp: int = 0
    n_sl: int = 0
    mfe_nat_r: dict | None = None
    mae_nat_r: dict | None = None
    mfe_fix_r: dict | None = None
    mae_fix_r: dict | None = None
    mae_prepeak_r: dict | None = None   # 峰前 MAE 分位（P6 护损口径）
    plan_tp_r_p50: float | None = None
    tp_reach_rate: float | None = None      # 固定视界 MFE ≥ 自身计划 TP 的比例
    tp_reach_ci: tuple | None = None
    sl_touch_rate: float | None = None      # 固定视界 MAE ≥ 1R 的比例
    unjust_sl_rate: float | None = None     # sl 单中先 SL 后达原 TP 的比例
    unjust_ci: tuple | None = None
    n_unjust: int = 0
    avg_cov_fix: float | None = None    # 平均固定视界覆盖率（<1 时 MFE/MAE 为下界）
    suggest: dict | None = None
    notes: list[str] = field(default_factory=list)


def aggregate_cell(excs: list[TradeExcursion], *, direction: str = "all",
                   atr_pct: float | None = None) -> CellReport:
    """把一格的逐笔画像聚成结论；n<30 一律「不可判定」（P4）。"""
    sysname, tf = excs[0].system, excs[0].tf
    rep = CellReport(system=sysname, tf=tf, direction=direction, n=len(excs),
                     verdict="OK" if len(excs) >= MIN_CELL_N else "INSUFFICIENT")
    rep.n_no_tp = sum(1 for e in excs if e.tp_r is None)
    sl_excs = [e for e in excs if e.exit_reason == "sl"]
    rep.n_sl = len(sl_excs)
    if rep.verdict != "OK":
        rep.notes.append(f"样本不足：n={rep.n} < {MIN_CELL_N}，不可判定，还差 {MIN_CELL_N - rep.n} 笔")
        return rep

    nat_mfe = [e.mfe_nat_r for e in excs if e.mfe_nat_r is not None]
    nat_mae = [e.mae_nat_r for e in excs if e.mae_nat_r is not None]
    fix = [(e.mfe_fix_r, e.mae_fix_r) for e in excs if e.mfe_fix_r is not None]
    covs = [e.cov_fix for e in excs if e.bars_fix > 0]
    if covs:
        rep.avg_cov_fix = round(float(np.mean(covs)), 4)
        if rep.avg_cov_fix < 0.9:
            rep.notes.append(
                f"固定视界 bar 覆盖率均值 {rep.avg_cov_fix:.0%}<90%：MFE/MAE 为下界估计，"
                "浪费①证据可能被夸大、浪费②证据只会偏弱（P1），结论作方向性参考")
    if nat_mfe:
        rep.mfe_nat_r, rep.mae_nat_r = _pcts(nat_mfe), _pcts(nat_mae)
    if fix:
        rep.mfe_fix_r = _pcts([x[0] for x in fix])
        rep.mae_fix_r = _pcts([x[1] for x in fix])
        pre = [e.mae_fix_prepeak_r for e in excs if e.mae_fix_prepeak_r is not None]
        if pre:
            rep.mae_prepeak_r = _pcts(pre)

    with_tp = [e for e in excs if e.tp_r is not None and e.mfe_fix_r is not None]
    if with_tp:
        rep.plan_tp_r_p50 = round(float(np.median([e.tp_r for e in with_tp])), 4)
        reach = sum(1 for e in with_tp if e.mfe_fix_r >= e.tp_r)
        rep.tp_reach_rate = round(reach / len(with_tp), 4)
        rep.tp_reach_ci = tuple(round(x, 4) for x in wilson_ci(reach, len(with_tp)))
    fix_mae = [e.mae_fix_r for e in excs if e.mae_fix_r is not None]
    if fix_mae:
        rep.sl_touch_rate = round(sum(1 for v in fix_mae if v >= 1.0) / len(fix_mae), 4)

    replayable_sl = [e for e in sl_excs if e.mfe_fix_r is not None and e.tp_r is not None]
    if replayable_sl:
        rep.n_unjust = sum(1 for e in replayable_sl if e.first_touch == "sl" and e.tp_after_sl)
        rep.unjust_sl_rate = round(rep.n_unjust / len(replayable_sl), 4)
        rep.unjust_ci = tuple(round(x, 4)
                              for x in wilson_ci(rep.n_unjust, len(replayable_sl)))

    rep.suggest = _make_suggestion(excs, rep, atr_pct)
    return rep


def _make_suggestion(excs: list[TradeExcursion], rep: CellReport,
                     atr_pct: float | None) -> dict | None:
    """P6 推荐口径。只对 verdict=OK 的格子产出；全部是分布描述，不承诺期望。"""
    fix = [(e.mfe_fix_r, e.mae_fix_r, e.mae_fix_prepeak_r)
           for e in excs if e.mfe_fix_r is not None]
    if len(fix) < MIN_CELL_N:
        return None
    mfe_arr = np.array([x[0] for x in fix])
    mae_arr = np.array([x[1] for x in fix])
    pre_arr = np.array([x[2] for x in fix])
    tp_lo, tp_mid, tp_hi = (float(x) for x in np.percentile(mfe_arr, (55, 60, 65)))

    # P6：护损深度由「利润峰值到手之前」的回撤决定（全视界 MAE 会把
    # 先到 TP 后行情反转的回撤也算进护损需求，系统性夸大 SL）
    hitters_pre = pre_arr[mfe_arr >= tp_mid]
    fallback = len(hitters_pre) < MIN_HITTERS_N
    sl_base = float(np.percentile(pre_arr if fallback else hitters_pre, 90))

    r_pct_med = float(np.median([e.r_pct for e in excs]))
    atr_buffer_r = round(atr_pct * ATR_MULT / r_pct_med, 4) if (atr_pct and r_pct_med > 0) else 0.0
    sl_r = round(sl_base + atr_buffer_r, 4)

    # 方向说明：按历史分布反查触达率/碰撞率的变化方向（描述，不是承诺）
    notes = []
    if rep.plan_tp_r_p50 is not None:
        reach_now = float(np.mean(mfe_arr >= rep.plan_tp_r_p50))
        reach_new = float(np.mean(mfe_arr >= tp_mid))
        closer = tp_mid < rep.plan_tp_r_p50
        notes.append(
            f"TP {rep.plan_tp_r_p50:.2f}R→{tp_mid:.2f}R（{'收近' if closer else '放远'}）："
            f"历史触达率 {reach_now:.0%}→{reach_new:.0%}，"
            f"胜率方向{'↑' if reach_new > reach_now else '↓'}、单笔盈利幅度方向{'↓' if closer else '↑'}")
    touch_now = float(np.mean(mae_arr >= 1.0))
    touch_new = float(np.mean(mae_arr >= sl_r))
    notes.append(
        f"SL 1.00R→{sl_r:.2f}R（护峰基线 {sl_base:.2f}R + 扫单缓冲 {atr_buffer_r:.2f}R）："
        f"视界内碰 SL 比例（全视界 MAE 口径）{touch_now:.0%}→{touch_new:.0%}，"
        f"胜率方向{'↑' if touch_new < touch_now else '↓'}、赔率方向{'↓' if sl_r > 1 else '↑'}；"
        f"须同比例缩仓保持 1R 恒定（T2 契约）")
    if fallback:
        notes.append(f"hitters 子集 n={len(hitters_pre)} < {MIN_HITTERS_N}，"
                     f"SL 基线回退全体峰前 MAE_R P90")
    notes.append("以上为分布描述，非期望改善承诺（裁决3：TP/SL 摆位在等期望线上滑动）")
    return {"tp_r_lo": round(tp_lo, 4), "tp_r_mid": round(tp_mid, 4),
            "tp_r_hi": round(tp_hi, 4), "sl_r": sl_r,
            "atr_buffer_r": atr_buffer_r, "sl_base_fallback": fallback,
            "notes": notes}


# ─────────────────────────── 报告 ───────────────────────────

def _discipline_check(text: str) -> str:
    """P7：打印前强制自检——禁词命中直接抛错，宁可不出报告不违纪。"""
    for w in FORBIDDEN_PHRASES:
        if w in text:
            raise ValueError(f"措辞纪律违规（裁决3）：输出包含禁词「{w}」")
    return text


def _fmt_pcts(d: dict | None) -> str:
    if not d:
        return "（无可测样本）"
    return " ".join(f"P{p}={d[f'p{p}']:.2f}" for p in PCTS)


def render_report(cells: list[CellReport], meta: dict) -> str:
    """控制台可读报告（同时作为 JSON 之外的人读产物）。"""
    out: list[str] = []
    ap = out.append
    ap("=" * 84)
    ap(f"MFE/MAE 止盈止损调优分析器 · {meta['symbol']} · 近 {meta['days']} 天 "
       f"· {meta['n_trades_analyzed']}/{meta['n_trades_total']} 笔可分析")
    b = meta["bars"]
    ap(f"预登记 {PREREG_VERSION}（结果不得与口径分离）；"
       f"K线页 缓存{b['pages_cached']}/出网{b['pages_fetched']}"
       f"/失败{b['pages_failed']}/待续跑{b['pages_deferred']}")
    if "bars_binance" in b:
        ap(f"数据源构成：币安 1m bar {b['bars_binance']:,} 根 + 本地 tape 降级补缺 "
           f"{b['bars_tape']:,} 根；区间分钟覆盖率 {b.get('minute_coverage', 0):.0%}")
        if b.get("minute_coverage", 1.0) < 0.9:
            ap("⚠ 覆盖率 <90%：MFE/MAE 为下界估计（P1）——浪费①「TP 过远」证据可能被夸大、"
               "浪费②「冤枉止损」证据只会偏弱；本轮结论作方向性参考，币安解封后重跑收敛")
    ap("交付物定位：减少结构性浪费、改善实现质量——不构成期望改善承诺（裁决3）")
    ap("=" * 84)

    ok = [c for c in cells if c.verdict == "OK"]
    ins = [c for c in cells if c.verdict != "OK"]

    ap("\n【MFE/MAE 分布】（R 归一；nat=开仓→平仓实走，fix=开仓后固定 24 根 TF bar 对照）")
    for c in sorted(ok, key=lambda x: -x.n):
        ap(f"  ▎{c.system} × {c.tf}（n={c.n}，sl 单 {c.n_sl}，无有效 TP {c.n_no_tp}）")
        ap(f"    MFE nat: {_fmt_pcts(c.mfe_nat_r)}")
        ap(f"    MFE fix: {_fmt_pcts(c.mfe_fix_r)}")
        ap(f"    MAE nat: {_fmt_pcts(c.mae_nat_r)}")
        ap(f"    MAE fix: {_fmt_pcts(c.mae_fix_r)}")
        ap(f"    MAE 峰前: {_fmt_pcts(c.mae_prepeak_r)}（护损口径：利润峰值到手前的回撤）")

    ap("\n【对照表】计划 TP/SL vs 行情实际给到的空间（两类结构性浪费）")
    header = (f"  {'格子':<20}{'n':>5}{'计划TP(R)':>10}{'MFE_fix P60':>12}"
              f"{'TP触达率':>10}{'MAE≥1R':>9}{'冤枉止损率':>17}{'视界覆盖':>9}")
    ap(header)
    for c in sorted(ok, key=lambda x: -x.n):
        cell = f"{c.system}×{c.tf}"
        tp = f"{c.plan_tp_r_p50:.2f}" if c.plan_tp_r_p50 is not None else "—"
        p60 = f"{c.mfe_fix_r['p60']:.2f}" if c.mfe_fix_r else "—"
        reach = f"{c.tp_reach_rate:.0%}" if c.tp_reach_rate is not None else "—"
        touch = f"{c.sl_touch_rate:.0%}" if c.sl_touch_rate is not None else "—"
        if c.unjust_sl_rate is not None:
            unjust = f"{c.unjust_sl_rate:.0%} [{c.unjust_ci[0]:.0%},{c.unjust_ci[1]:.0%}]"
        else:
            unjust = "—"
        cov = f"{c.avg_cov_fix:.0%}" if c.avg_cov_fix is not None else "—"
        ap(f"  {cell:<20}{c.n:>5}{tp:>10}{p60:>12}{reach:>10}{touch:>9}{unjust:>17}{cov:>9}")
    ap("  · TP触达率低 + 计划TP(R) > MFE_fix P60 → ①利润回吐型浪费（TP 摆在分布够不到处）")
    ap("  · 冤枉止损率高（先触 SL 后达原 TP）→ ②冤枉止损型浪费（SL 在常规波动带内）")

    ap("\n【推荐点位】（仅建议不自动生效；n<30 格子一律不可判定，不给数字）")
    for c in sorted(ok, key=lambda x: -x.n):
        if not c.suggest:
            continue
        s = c.suggest
        ap(f"  ▎{c.system} × {c.tf}（n={c.n}）")
        ap(f"    TP 建议 {s['tp_r_mid']:.2f}R（区间 [{s['tp_r_lo']:.2f}, {s['tp_r_hi']:.2f}]，"
           f"= MFE_fix 的 P55–P65 内侧）")
        ap(f"    SL 建议 {s['sl_r']:.2f}R（hitters 峰前 MAE P90 + 扫单缓冲 {s['atr_buffer_r']:.2f}R）")
        for note in s["notes"]:
            ap(f"    · {note}")

    if ins:
        ap(f"\n【不可判定格子】共 {len(ins)} 个（n < {MIN_CELL_N}，禁止给数字）：")
        for c in sorted(ins, key=lambda x: -x.n):
            ap(f"    {c.system}×{c.tf}  n={c.n}（还差 {MIN_CELL_N - c.n} 笔）")

    ap("\n" + "=" * 84)
    return _discipline_check("\n".join(out))


def top_waste(cells: list[CellReport], k: int = 3) -> list[dict]:
    """可判定格子中两类浪费的 TOP 榜（供回执与 JSON）。"""
    ok = [c for c in cells if c.verdict == "OK"]
    waste1 = [{"cell": f"{c.system}×{c.tf}", "type": "利润回吐型",
               "n": c.n, "plan_tp_r": c.plan_tp_r_p50,
               "mfe_fix_p60": c.mfe_fix_r["p60"] if c.mfe_fix_r else None,
               "tp_reach_rate": c.tp_reach_rate, "ci": c.tp_reach_ci}
              for c in ok if c.tp_reach_rate is not None and c.plan_tp_r_p50 is not None
              and c.mfe_fix_r and c.plan_tp_r_p50 > c.mfe_fix_r["p60"]]
    waste1.sort(key=lambda x: x["tp_reach_rate"])
    waste2 = [{"cell": f"{c.system}×{c.tf}", "type": "冤枉止损型",
               "n": c.n, "n_sl_replayable": c.n_sl,
               "unjust_sl_rate": c.unjust_sl_rate, "ci": c.unjust_ci}
              for c in ok if c.unjust_sl_rate is not None]
    waste2.sort(key=lambda x: -(x["unjust_sl_rate"] or 0))
    return (waste1[:k]) + (waste2[:k])


# ─────────────────────────── 主流程 ───────────────────────────

def run(symbol: str, days: int, *, by_direction: bool = False, offline: bool = False,
        max_requests: int = DEFAULT_MAX_REQUESTS, system: str | None = None) -> dict:
    trades = load_trades(symbol, days)
    if system:
        trades = [t for t in trades if t["system"] == system]
    if not trades:
        print(f"窗口内无已平仓单（{symbol}，近 {days} 天），退出。")
        return {}

    # 取数区间：最早开仓 → max(最晚平仓, 最晚开仓+最长固定视界)，尾部截到当前
    start_ts = min(float(t["entry_ts"]) for t in trades)
    end_need = max(max(float(t["exit_ts"]) for t in trades),
                   max(float(t["entry_ts"])
                       + FIXED_HORIZON_BARS * TF_MINUTES.get(str(t["tf"]), 60) * 60
                       for t in trades))
    bars, bar_stats = load_minute_bars(symbol, start_ts, end_need,
                                       offline=offline, max_requests=max_requests)
    bar_stats["bars_binance"] = len(bars)
    # P1 降级：币安取数有缺口（限频封禁/离线/限流延后）时，用本地 tape 分钟 bar 补缺
    bar_stats["bars_tape"] = 0
    now_min = int(time.time() // 60)
    lo_min, hi_min = int(start_ts // 60), min(int(end_need // 60), now_min - 1)
    if bar_stats["pages_failed"] or bar_stats["pages_deferred"]:
        for m, b in load_tape_bars(symbol, lo_min, hi_min).items():
            if m not in bars:
                bars[m] = b
                bar_stats["bars_tape"] += 1
    need_minutes = max(hi_min - lo_min + 1, 1)
    bar_stats["minute_coverage"] = round(len(bars) / need_minutes, 4)
    data_end_min = max(bars) if bars else 0

    excs: list[TradeExcursion] = []
    skipped_zero_r = skipped_no_bars = 0
    for t in trades:
        e = analyze_trade(t, bars, data_end_min)
        if e is None:
            skipped_zero_r += 1
        elif e.bars_fix == 0 and e.bars_nat == 0:
            skipped_no_bars += 1
        else:
            excs.append(e)

    # ATR 缓冲：每个 TF 一个中位 ATR%（复用 stop_hunt 的 ATR 口径与 1×ATR 扫单深度）
    atr_by_tf = {tf: tf_atr_pct_median(bars, m) for tf, m in TF_MINUTES.items()}

    groups: dict[tuple, list[TradeExcursion]] = {}
    for e in excs:
        key = (e.system, e.tf, e.direction) if by_direction else (e.system, e.tf, "all")
        groups.setdefault(key, []).append(e)
    cells = [aggregate_cell(v, direction=k[2], atr_pct=atr_by_tf.get(k[1]))
             for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))]

    meta = {"symbol": symbol, "days": days, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "prereg": PREREG, "n_trades_total": len(trades),
            "n_trades_analyzed": len(excs), "skipped_zero_r": skipped_zero_r,
            "skipped_no_bars": skipped_no_bars, "bars": bar_stats,
            "by_direction": by_direction,
            "atr_pct_by_tf": {k: (round(v, 4) if v else None) for k, v in atr_by_tf.items()}}
    print(render_report(cells, meta))

    payload = {"meta": meta,
               "cells": [{**c.__dict__} for c in cells],
               "top_waste": top_waste(cells)}
    os.makedirs(ADVISOR_DIR, exist_ok=True)
    out_path = os.path.join(ADVISOR_DIR, f"{symbol}.json")
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, out_path)
    print(f"JSON 已落盘：{out_path}")
    if bar_stats["pages_deferred"]:
        print(f"⚠ 有 {bar_stats['pages_deferred']} 页 K 线本轮未取（限频/离线），"
              f"再跑一次同命令即断点续跑。")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="MFE/MAE 止盈止损调优分析器（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="跑一轮分析")
    p.add_argument("--symbol", default="ETHUSDT")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--system", help="只分析指定体系")
    p.add_argument("--by-direction", action="store_true", help="格子细分到方向")
    p.add_argument("--offline", action="store_true", help="只用本地缓存，不出网")
    p.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS,
                   help=f"单轮出网页数上限（默认 {DEFAULT_MAX_REQUESTS}，超出留到下轮续跑）")
    args = ap.parse_args()
    if args.cmd == "run":
        run(args.symbol.upper(), args.days, by_direction=args.by_direction,
            offline=args.offline, max_requests=args.max_requests, system=args.system)
    return 0


if __name__ == "__main__":
    sys.exit(main())
