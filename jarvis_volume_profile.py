#!/usr/bin/env python3
"""贾维斯 JARVIS — Volume Profile 分布结构引擎（高胜率反转四条件之 2+3）。

四大反转必备条件中本模块负责的两条：
  条件2 过程不稳定   ：上涨/下跌过程形成多个 VP 正态分布（台阶式蓄势），
                       而不是单边直拉（成交量均匀铺开、无密集成交区）
  条件3 三连分布确认 ：近期走势内至少 N 个（默认 3）相邻小正态分布，
                       结构健康，耐心等待回补到分布 POC 即为高概率入场区

实现拆解：
  1) Volume Profile：按价格分箱（bin 自适应 30~50）聚合成交量，K 线量按其
     高低区间与箱的重叠比例分摊；输出 POC（量峰价位）与 VAH/VAL（70% 价值区）
  2) 滑动分段：走势按固定窗口切段（窗口自适应 = 总长/6，clamp 20~60），
     每段独立算分布与形态
  3) 形态识别：normal（单峰居中、两侧衰减）/ skewed（峰贴边、单侧长尾）/
     flat（量均匀铺开无密集区 = 单边直拉特征）
  4) 结构判定：multi-distribution（台阶式多分布，健康）/ one-way（单边直拉，
     追高风险）/ mixed；tripleConfirmed = 相邻 normal 段连续数 ≥ 3

REST 契约（/api/volume-profile，MCP-6 四条件汇总面板消费，字段不可擅改；
t18b 增补字段只增不改）：
  { ok, symbol, timeframe,
    distributions: [{startT,endT,poc,vah,val,shape,volumeShare}],
    structure: {type, normalCount, tripleConfirmed, note},
    currentPosition: {vsLastPoc, pullbackTarget, nearestUnfilledGap, note},
    gaps: [{priceHigh,priceLow,betweenT,filled,note}],
    mergedProfile: {poc,vah,val,childCount,note} | null,
    updatedAt, disclaimer }

t18b 增补（「三个儿子合成一个爸爸」理论落地）：
  1) 低量缺口：相邻分布价值区之间的价格空隙（跳空推进段，量稀薄）；
     不稳定上涨的缺口大概率被回补 → 未回补缺口即回补目标参考
  2) 分布合并：相邻小分布价值区重叠/POC 接近 → 合并簇按原始 K 线重算
     一个大分布（三合一后的「爸爸」价值区更强）
  3) 不做反转铁律：one-way（稳定单边）结构 note 显式输出
     「单一分布难以回归，不做反转」

设计原则（与其它 jarvis 引擎同风格）：
  - 纯函数核心 analyze(rows, ...)：只吃 K 线 list[dict]，不联网，可单测
  - assess() 门面拉真实 K 线；mock_response() 出确定性假数据（?mock=1）
  - 失败封套 {ok:false}，绝不抛出

命令行（联网前本地验证）：
  python jarvis_volume_profile.py BTCUSDT 15m
  python jarvis_volume_profile.py --mock
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

# ═══════════════════════════ 参数口径（集中定义，单测锁定） ═══════════════════════════

BIN_MIN, BIN_MAX = 30, 50          # 价格分箱数自适应范围
VALUE_AREA_PCT = 0.70              # 价值区覆盖成交量比例
SEG_MIN, SEG_MAX = 20, 60          # 分段窗口自适应 clamp（根）
SEG_DIVISOR = 6                    # 窗口 = 总根数 / 6
MAX_SEGMENTS = 8                   # 最多输出最近 N 段
TRIPLE_N = 3                       # 三连分布确认阈值

# 形态判定阈值
FLAT_VA_RATIO = 0.62               # 价值区宽 / 全区间宽 > 0.62 → 量铺开无密集区（flat）
FLAT_PEAK_MULT = 1.8               # 峰值箱量 < 均匀值 ×1.8 → 峰不突出（flat）
SKEW_POC_EDGE = 0.22               # POC 相对位置 < 0.22 或 > 0.78 → 峰贴边（skewed）
NORMAL_DECAY = 0.55                # 峰两侧翼区均量 ≤ 峰值 ×0.55 → 两侧衰减成立
ONE_WAY_BAD_RATIO = 0.60           # flat+skewed 段占比 ≥ 0.6 → one-way
MULTI_BAD_CAP = 0.40               # multi-distribution 允许的非 normal 占比上限

DISCLAIMER = ("Volume Profile 结构分析基于历史成交分布，正态分布计数与回补目标"
              "仅为概率参考，不构成投资建议；请结合 Delta 背离与扫止损确认共同决策。")

_SHAPE_CN = {"normal": "正态", "skewed": "偏斜", "flat": "直拉/均匀"}


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# ═══════════════════════════ 1) 单窗口 Volume Profile（纯函数） ═══════════════════════════

def compute_profile(rows: list[dict], bins: int | None = None) -> dict | None:
    """一段 K 线 → 价格分箱成交量分布 + POC + 70% 价值区（VAH/VAL）。

    K 线行结构 {"time": ms, "open","high","low","close","volume"}；
    每根量按其 [low, high] 与各箱的重叠长度比例分摊（tick 级近似）。
    数据不足/区间退化返回 None。
    """
    if not rows or len(rows) < 5:
        return None
    lo = min(float(r["low"]) for r in rows)
    hi = max(float(r["high"]) for r in rows)
    if hi <= lo:
        return None
    n = bins or max(BIN_MIN, min(BIN_MAX, len(rows)))
    width = (hi - lo) / n
    vol = [0.0] * n

    for r in rows:
        rl, rh = float(r["low"]), float(r["high"])
        v = float(r.get("volume") or 0.0)
        if v <= 0:
            continue
        if rh <= rl:  # 一字线：全量落单箱
            idx = min(n - 1, int((rl - lo) / width))
            vol[idx] += v
            continue
        span = rh - rl
        i0 = max(0, min(n - 1, int((rl - lo) / width)))
        i1 = max(0, min(n - 1, int((rh - lo) / width)))
        for i in range(i0, i1 + 1):
            b_lo, b_hi = lo + i * width, lo + (i + 1) * width
            overlap = min(rh, b_hi) - max(rl, b_lo)
            if overlap > 0:
                vol[i] += v * overlap / span

    total = sum(vol)
    if total <= 0:
        return None

    poc_i = max(range(n), key=lambda i: vol[i])
    poc = lo + (poc_i + 0.5) * width

    # 价值区：自 POC 向两侧贪心扩展（每步取量大的一侧）至覆盖 ≥70% 总量
    covered = vol[poc_i]
    l, r = poc_i, poc_i
    while covered < total * VALUE_AREA_PCT and (l > 0 or r < n - 1):
        lv = vol[l - 1] if l > 0 else -1.0
        rv = vol[r + 1] if r < n - 1 else -1.0
        if rv >= lv:
            r += 1
            covered += vol[r]
        else:
            l -= 1
            covered += vol[l]
    val = lo + l * width
    vah = lo + (r + 1) * width

    return {"low": lo, "high": hi, "bins": n, "bin_width": width, "volumes": vol,
            "total_volume": total, "poc": poc, "poc_index": poc_i,
            "vah": vah, "val": val, "va_l": l, "va_r": r}


# ═══════════════════════════ 2) 形态识别（纯函数） ═══════════════════════════

def classify_shape(profile: dict) -> str:
    """分布形态：normal（单峰居中两侧衰减）/ skewed（峰贴边）/ flat（均匀铺开）。"""
    vol = profile["volumes"]
    n = profile["bins"]
    total = profile["total_volume"]
    poc_i = profile["poc_index"]
    peak = vol[poc_i]

    # flat：价值区铺得太宽（无密集成交区）或峰值不突出——单边直拉的量分布特征
    va_ratio = (profile["va_r"] - profile["va_l"] + 1) / n
    uniform = total / n
    if va_ratio > FLAT_VA_RATIO or peak < uniform * FLAT_PEAK_MULT:
        return "flat"

    # skewed：峰贴近区间边缘（单侧长尾）
    pos = poc_i / (n - 1) if n > 1 else 0.5
    if pos < SKEW_POC_EDGE or pos > 1.0 - SKEW_POC_EDGE:
        return "skewed"

    # normal：峰两侧翼区（价值区外）均量都显著低于峰值 → 两侧衰减
    left_wing = vol[:profile["va_l"]]
    right_wing = vol[profile["va_r"] + 1:]
    l_avg = sum(left_wing) / len(left_wing) if left_wing else 0.0
    r_avg = sum(right_wing) / len(right_wing) if right_wing else 0.0
    if l_avg <= peak * NORMAL_DECAY and r_avg <= peak * NORMAL_DECAY:
        return "normal"
    return "skewed"


# ═══════════════════════════ 3) 分段 + 结构判定（纯函数） ═══════════════════════════

def _segment(rows: list[dict]) -> list[list[dict]]:
    """固定窗口切段（窗口自适应 = len/6，clamp 20~60），只保留最近 MAX_SEGMENTS 段。

    从尾部（最新）往前切，保证「当前段」完整；最早一段不足窗口 2/3 时丢弃。
    """
    seg_len = max(SEG_MIN, min(SEG_MAX, len(rows) // SEG_DIVISOR))
    segs: list[list[dict]] = []
    end = len(rows)
    while end > 0 and len(segs) < MAX_SEGMENTS:
        start = max(0, end - seg_len)
        seg = rows[start:end]
        if len(seg) >= max(5, seg_len * 2 // 3):
            segs.append(seg)
        end = start
    segs.reverse()  # 时间正序
    return segs


def analyze(rows: list[dict], symbol: str = "BTCUSDT",
            timeframe: str = "15m") -> dict:
    """K 线 → 完整契约响应体（不含 ok/updatedAt，由门面补）。纯函数可单测。"""
    sym = (symbol if symbol.upper().endswith(("USDT", "USDC")) else symbol + "USDT").upper()
    segs = _segment(rows or [])
    grand_total = 0.0
    dists: list[dict] = []
    kept_segs: list[list[dict]] = []   # 与 dists 一一对应的原始段（合并重算用）
    for seg in segs:
        p = compute_profile(seg)
        if not p:
            continue
        shape = classify_shape(p)
        grand_total += p["total_volume"]
        kept_segs.append(seg)
        dists.append({
            "startT": _iso(float(seg[0]["time"])),
            "endT": _iso(float(seg[-1]["time"])),
            "poc": round(p["poc"], 8),
            "vah": round(p["vah"], 8),
            "val": round(p["val"], 8),
            "shape": shape,
            "_total": p["total_volume"],
        })
    for d in dists:
        d["volumeShare"] = round(d.pop("_total") / grand_total, 4) if grand_total > 0 else 0.0

    structure = _judge_structure(dists)
    gaps = _detect_gaps(rows, dists)
    merged = _merge_adjacent(dists, kept_segs)
    current = _current_position(rows, dists, gaps)
    return {"symbol": sym, "timeframe": timeframe,
            "distributions": dists, "structure": structure,
            "currentPosition": current, "gaps": gaps,
            "mergedProfile": merged, "disclaimer": DISCLAIMER}


def _adjacent_normal_cluster(shapes: list[str]) -> int:
    """最大「相邻正态簇」大小：normal 段间允许夹至多 1 个过渡段仍视为相邻。

    固定窗口切段时台阶交界处必然产生跨界过渡段（双峰/偏斜伪影），严格连续
    会漏判真实的台阶结构；gap≤1 的聚簇口径与用户「相邻小正态分布」本意一致。
    """
    idx = [i for i, s in enumerate(shapes) if s == "normal"]
    if not idx:
        return 0
    best = cur = 1
    for a, b in zip(idx, idx[1:]):
        cur = cur + 1 if b - a <= 2 else 1
        best = max(best, cur)
    return best


def _judge_structure(dists: list[dict]) -> dict:
    """多分布结构判定：multi-distribution / one-way / mixed + 三连确认。"""
    if not dists:
        return {"type": "mixed", "normalCount": 0, "tripleConfirmed": False,
                "note": "K 线数据不足，无法切分出有效分布段"}
    shapes = [d["shape"] for d in dists]
    normal_count = shapes.count("normal")
    bad_ratio = (shapes.count("flat") + shapes.count("skewed")) / len(shapes)

    cluster = _adjacent_normal_cluster(shapes)
    triple = cluster >= TRIPLE_N

    # 三连确认本身即最强的多分布证据；否则看正态段数量与非正态占比
    if triple or (normal_count >= 2 and bad_ratio < MULTI_BAD_CAP):
        stype = "multi-distribution"
    elif bad_ratio >= ONE_WAY_BAD_RATIO or normal_count <= 1:
        stype = "one-way"
    else:
        stype = "mixed"

    seq_cn = "→".join(_SHAPE_CN[s] for s in shapes[-min(len(shapes), 6):])
    if stype == "multi-distribution":
        note = (f"近 {len(shapes)} 段中 {normal_count} 段正态分布（{seq_cn}），"
                f"台阶式蓄势结构健康"
                + (f"；相邻正态簇达 {cluster} 段 ≥{TRIPLE_N}，三连分布确认成立，"
                   "可耐心等待回补至分布 POC 附近" if triple else
                   f"；相邻正态簇最大 {cluster} 段（<{TRIPLE_N}），三连确认未满足，继续等待"))
    elif stype == "one-way":
        # t18b 铁律：稳定上涨形成单一分布，难以回归 → 不做反转（MCP-6 面板直接展示）
        note = (f"近 {len(shapes)} 段以直拉/偏斜为主（{seq_cn}），单边行情无充分换手，"
                "过程不稳定条件不满足——稳定单边形成单一分布，难以回归，"
                "不做反转；追高风险大，等待回补形成密集区")
    else:
        note = (f"近 {len(shapes)} 段形态混合（{seq_cn}），结构过渡期，"
                f"正态 {normal_count} 段、相邻正态簇最大 {cluster} 段，暂不构成明确信号")
    return {"type": stype, "normalCount": normal_count,
            "tripleConfirmed": triple, "note": note}


# ═══════════════════════════ t18b 增补 1：低量缺口识别与回补 ═══════════════════════════

def _detect_gaps(rows: list[dict], dists: list[dict]) -> list[dict]:
    """相邻分布价值区之间的低量价格带（缺口）+ 回补状态。

    口径：
      - 时间相邻的两段，若价值区无重叠（前段 VAH < 后段 VAL 为上行缺口；
        前段 VAL > 后段 VAH 为下行缺口），中间价格带即低成交量缺口
      - 回补判定：缺口形成（后段起点）之后的 K 线触及缺口带远端
        （上行缺口=回落触及 priceLow；下行缺口=反弹触及 priceHigh）→ filled
      - 「不稳定上涨会形成多个小正态分布，缺口必被回补」→ 未回补缺口
        note 明示大概率被回补，可作回补目标参考
    """
    gaps: list[dict] = []
    if not rows or len(dists) < 2:
        return gaps
    for a, b in zip(dists, dists[1:]):
        if b["val"] > a["vah"]:          # 上行缺口：价格向上跳过的低量带
            lo_p, hi_p, upward = a["vah"], b["val"], True
        elif b["vah"] < a["val"]:        # 下行缺口
            lo_p, hi_p, upward = b["vah"], a["val"], False
        else:
            continue
        if hi_p - lo_p <= 0:
            continue
        # 缺口形成后的 K 线：时间 ≥ 后段起点
        b_start_iso = b["startT"]
        after = [r for r in rows if _iso(float(r["time"])) >= b_start_iso]
        if upward:
            filled = any(float(r["low"]) <= lo_p for r in after)
        else:
            filled = any(float(r["high"]) >= hi_p for r in after)
        dir_cn = "上行" if upward else "下行"
        note = (f"{dir_cn}缺口已回补（价格已穿越 {lo_p:,.2f}~{hi_p:,.2f} 低量带）" if filled
                else f"未回补{dir_cn}缺口 {lo_p:,.2f}~{hi_p:,.2f}，低量带缺乏换手支撑，大概率被回补")
        gaps.append({
            "priceHigh": round(hi_p, 8),
            "priceLow": round(lo_p, 8),
            "betweenT": [a["endT"], b["startT"]],
            "filled": filled,
            "note": note,
        })
    return gaps


# ═══════════════════════════ t18b 增补 2：分布合并（三个儿子合成一个爸爸） ═══════════════════════════

MERGE_POC_TOL = 0.5   # POC 接近判定：|ΔPOC| ≤ 两段价值区宽均值 × 0.5


def _mergeable(a: dict, b: dict) -> bool:
    """相邻两段可合并：价值区重叠，或 POC 距离小于平均价值区宽的一半。"""
    overlap = max(a["val"], b["val"]) <= min(a["vah"], b["vah"])
    avg_width = ((a["vah"] - a["val"]) + (b["vah"] - b["val"])) / 2.0
    poc_close = avg_width > 0 and abs(a["poc"] - b["poc"]) <= avg_width * MERGE_POC_TOL
    return overlap or poc_close


def _merge_adjacent(dists: list[dict], kept_segs: list[list[dict]]) -> dict | None:
    """最长相邻可合并链（≥2 段）→ 用原始 K 线合并重算一个大分布。

    「三个儿子合成一个爸爸」：多个小正态分布价值区纠缠 → 实为同一个更大的
    价值区域，合并后的 POC/VAH/VAL 支撑压力意义更强。无可合并返回 None。
    """
    n = len(dists)
    if n < 2 or len(kept_segs) != n:
        return None
    best_s, best_e = 0, 0   # [s, e] 闭区间，最长链
    s = 0
    for i in range(1, n):
        if _mergeable(dists[i - 1], dists[i]):
            if i - s > best_e - best_s:
                best_s, best_e = s, i
        else:
            s = i
    child_count = best_e - best_s + 1
    if child_count < 2:
        return None
    merged_rows = [r for k in range(best_s, best_e + 1) for r in kept_segs[k]]
    p = compute_profile(merged_rows)
    if not p:
        return None
    return {
        "poc": round(p["poc"], 8),
        "vah": round(p["vah"], 8),
        "val": round(p["val"], 8),
        "childCount": child_count,
        "note": (f"{child_count} 个儿子合成一个爸爸：{child_count} 段相邻分布"
                 f"价值区重叠/POC 接近，合并为大分布 POC {p['poc']:,.2f}、"
                 f"价值区 {p['val']:,.2f}~{p['vah']:,.2f}——合并后的支撑/压力"
                 "意义强于任何单段小分布"),
    }


def _current_position(rows: list[dict], dists: list[dict],
                      gaps: list[dict] | None = None) -> dict:
    """现价相对最近分布价值区的位置 + 回补目标位（最近一个正态段 POC）。"""
    if not rows or not dists:
        return {"vsLastPoc": "inside", "pullbackTarget": None,
                "nearestUnfilledGap": None,
                "note": "数据不足，无法给出位置参考"}
    price = float(rows[-1]["close"])
    last = dists[-1]
    if price > last["vah"]:
        pos = "above"
    elif price < last["val"]:
        pos = "below"
    else:
        pos = "inside"

    # 回补目标：最近的正态段 POC（above 找下方最近、below 找上方最近、inside 用当前段 POC）
    target = None
    normals = [d for d in dists if d["shape"] == "normal"]
    if pos == "above":
        below = [d["poc"] for d in normals if d["poc"] < price]
        target = max(below) if below else None
    elif pos == "below":
        above = [d["poc"] for d in normals if d["poc"] > price]
        target = min(above) if above else None
    else:
        target = last["poc"] if last["shape"] == "normal" else (
            normals[-1]["poc"] if normals else None)

    # t18b：最近的未回补缺口（按缺口带中心与现价距离取最近）——回补目标参考
    nearest_gap = None
    unfilled = [g for g in (gaps or []) if not g["filled"]]
    if unfilled:
        g = min(unfilled,
                key=lambda g: abs((g["priceHigh"] + g["priceLow"]) / 2.0 - price))
        nearest_gap = {"priceHigh": g["priceHigh"], "priceLow": g["priceLow"]}

    pos_cn = {"above": "价值区上方", "below": "价值区下方", "inside": "价值区内"}[pos]
    if target is not None:
        note = (f"现价 {price:,.2f} 处于最近分布{pos_cn}，"
                f"回补参考位 = 最近正态分布 POC {target:,.2f}"
                + ("（等待回踩确认支撑）" if pos == "above" else
                   "（等待反弹确认压力）" if pos == "below" else "（区内换手，观察方向选择）"))
    else:
        note = f"现价 {price:,.2f} 处于最近分布{pos_cn}；近段无正态分布，暂无可靠回补目标"
    if nearest_gap is not None:
        note += (f"；另有未回补低量缺口 {nearest_gap['priceLow']:,.2f}~"
                 f"{nearest_gap['priceHigh']:,.2f} 可作回补目标参考")
    return {"vsLastPoc": pos,
            "pullbackTarget": round(target, 8) if target is not None else None,
            "nearestUnfilledGap": nearest_gap,
            "note": note}


# ═══════════════════════════ mock（?mock=1 确定性假数据） ═══════════════════════════

def mock_response(symbol: str = "BTCUSDT", timeframe: str = "15m") -> dict:
    """确定性 mock：三段正态台阶上涨 + 一段直拉；t18b 增补字段样例齐全
    （缺口两例：一已回补一未回补；三合一 mergedProfile；nearestUnfilledGap）。"""
    base_ms = 1735689600000  # 2025-01-01T00:00:00Z，固定起点保证确定性
    seg_ms = 40 * 900_000    # 每段 40 根 15m
    mk = lambda i, poc, vah, val, shape, share: {  # noqa: E731
        "startT": _iso(base_ms + i * seg_ms),
        "endT": _iso(base_ms + (i + 1) * seg_ms - 900_000),
        "poc": poc, "vah": vah, "val": val, "shape": shape, "volumeShare": share,
    }
    dists = [
        mk(0, 60200.0, 60650.0, 59800.0, "normal", 0.22),
        mk(1, 61500.0, 61950.0, 61100.0, "normal", 0.26),
        mk(2, 62800.0, 63250.0, 62400.0, "normal", 0.31),
        mk(3, 64100.0, 64900.0, 63300.0, "flat", 0.21),
    ]
    gaps = [
        {"priceHigh": 61100.0, "priceLow": 60650.0,
         "betweenT": [dists[0]["endT"], dists[1]["startT"]], "filled": True,
         "note": "上行缺口已回补（价格已穿越 60,650.00~61,100.00 低量带）"},
        {"priceHigh": 62400.0, "priceLow": 61950.0,
         "betweenT": [dists[1]["endT"], dists[2]["startT"]], "filled": False,
         "note": "未回补上行缺口 61,950.00~62,400.00，低量带缺乏换手支撑，大概率被回补"},
    ]
    return {
        "ok": True, "symbol": symbol.upper(), "timeframe": timeframe, "mock": True,
        "distributions": dists,
        "structure": {"type": "multi-distribution", "normalCount": 3,
                      "tripleConfirmed": True,
                      "note": "近 4 段中 3 段正态分布（正态→正态→正态→直拉/均匀），"
                              "台阶式蓄势结构健康；已出现 3 连正态 ≥3，三连分布确认成立，"
                              "可耐心等待回补至分布 POC 附近"},
        "currentPosition": {"vsLastPoc": "above", "pullbackTarget": 62800.0,
                            "nearestUnfilledGap": {"priceHigh": 62400.0,
                                                   "priceLow": 61950.0},
                            "note": "现价 64,350.00 处于最近分布价值区上方，"
                                    "回补参考位 = 最近正态分布 POC 62,800.00（等待回踩确认支撑）；"
                                    "另有未回补低量缺口 61,950.00~62,400.00 可作回补目标参考"},
        "gaps": gaps,
        "mergedProfile": {"poc": 61800.0, "vah": 63250.0, "val": 59800.0,
                          "childCount": 3,
                          "note": "3 个儿子合成一个爸爸：3 段相邻分布价值区重叠/POC 接近，"
                                  "合并为大分布 POC 61,800.00、价值区 59,800.00~63,250.00"
                                  "——合并后的支撑/压力意义强于任何单段小分布"},
        "updatedAt": _iso(base_ms + 4 * seg_ms),
        "disclaimer": DISCLAIMER,
    }


# ═══════════════════════════ 门面（带 IO） ═══════════════════════════

def assess(symbol: str = "BTCUSDT", timeframe: str = "15m",
           limit: int = 300) -> dict:
    """拉真实 K 线 → analyze。失败 ok=False 不抛出。"""
    try:
        import jarvis_twelve_systems as jts
        df = jts.fetch_klines_df(symbol, timeframe, max(60, min(int(limit), 500)))
        if df is None or len(df) < 60:
            return {"ok": False, "symbol": symbol.upper(), "timeframe": timeframe,
                    "error": f"K 线数据不足（{0 if df is None else len(df)} 根，需 ≥60）"}
        rows = df.to_dict("records")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "symbol": symbol.upper(), "timeframe": timeframe,
                "error": repr(exc)[:200]}
    out = analyze(rows, symbol, timeframe)
    out.update({"ok": True,
                "updatedAt": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")})
    return out


if __name__ == "__main__":
    import json as _json
    import sys as _sys
    if "--mock" in _sys.argv:
        print(_json.dumps(mock_response(), ensure_ascii=False, indent=2))
    else:
        _sym = _sys.argv[1] if len(_sys.argv) > 1 else "BTCUSDT"
        _tf = _sys.argv[2] if len(_sys.argv) > 2 else "15m"
        print(_json.dumps(assess(_sym, _tf), ensure_ascii=False, indent=2))
