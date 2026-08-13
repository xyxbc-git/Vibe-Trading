#!/usr/bin/env python3
"""贾维斯 JARVIS — 情绪风控导师 · AI 解释层（小白话 + 每币种独立交易原则）。

[任务 N] 与 jarvis_trade_mentor（agent-8：确定性裁决核心 + mentor_plan 表）配套：
本模块不做裁决，只负责把「计划 + 逐条证据裁决 + 币种原则档案 + 情绪信号」
拼成导师口吻的小白话解释输入（SSE 端点见 jarvis_dashboard /api/mentor/explain/stream）。

三块职责：
1. 每币种原则档案：~/.vibe-trading/mentor_profiles/<SYMBOL>.md——用户可直接编辑，
   首次访问自动生成模板；「波动特性 / 典型扫单深度」小节在有本地 K 线时实算填入
   （扫单深度口径对齐 jarvis_stop_hunt：刺破前高/前低后收回的插针，ATR 归一）。
2. 证据包获取：fetch_plan_bundle(plan_id) try-import jarvis_trade_mentor 容错；
   agent-8 未就绪时返回 None，联调用 sample_bundle() 自造样例。
3. Prompt 组装：build_messages(bundle) —— 纪律：只引用证据包内数据不编造；
   结尾固定免责；emotion.score ≥ 60 先共情后约束。

不引新依赖；LLM 调用走 jarvis_llm_config（由 dashboard 端点执行，本模块零出网）。
"""

from __future__ import annotations

import json
import math
import os
import re
import time

import pandas as pd

PROFILE_DIR = os.path.expanduser("~/.vibe-trading/mentor_profiles")

# 扫单深度统计口径（对齐 jarvis_stop_hunt：LOOKBACK_BARS=20 前极值参考窗）
SWEEP_LOOKBACK = 20
# 情绪自评 1-5，≥4 判「上头」→ 导师先共情后讲规则（对齐 jarvis_trade_mentor.EMOTION_HOT）
EMOTION_HOT_SCORE = 4

_PLACEHOLDER = "⏳待补充（有本地 K 线数据时刷新档案自动实算填入）"


# ─────────────────────────── 扫单深度实算（纯函数） ───────────────────────────

def _atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def sweep_depth_stats(df: pd.DataFrame | None) -> dict | None:
    """典型扫单深度统计：刺破前 20 根极值后收回的插针深度（×ATR 归一）。

    口径与 jarvis_stop_hunt 一致（prior extreme 刺破 + 收盘收回=扫单），但做
    的是全段分布统计而非单点检测：返回 {n, lookback_bars, median_atr, p90_atr,
    atr_pct_avg, max_move_pct}；df 缺失/数据不足返回 None（档案留待补充）。
    """
    try:
        if df is None or len(df) < SWEEP_LOOKBACK + 10:
            return None
        atr = _atr_series(df)
        highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
        depths: list[float] = []
        for i in range(SWEEP_LOOKBACK, len(df)):
            a = float(atr.iloc[i])
            if not (math.isfinite(a) and a > 0):
                continue
            prior_low = float(min(lows[i - SWEEP_LOOKBACK: i]))
            prior_high = float(max(highs[i - SWEEP_LOOKBACK: i]))
            lo, hi, c = float(lows[i]), float(highs[i]), float(closes[i])
            if lo < prior_low and c > prior_low:          # 刺破前低收回（扫多头止损）
                depths.append((prior_low - lo) / a)
            if hi > prior_high and c < prior_high:        # 刺破前高回落（扫空头止损）
                depths.append((hi - prior_high) / a)
        atr_pct = (atr / df["close"].replace(0, float("nan")) * 100).dropna()
        rng_pct = ((df["high"] - df["low"]) / df["close"].replace(0, float("nan"))
                   * 100).dropna()
        out = {
            "n": len(depths),
            "lookback_bars": int(len(df)),
            "atr_pct_avg": round(float(atr_pct.mean()), 4) if len(atr_pct) else None,
            "max_move_pct": round(float(rng_pct.max()), 4) if len(rng_pct) else None,
            "median_atr": None,
            "p90_atr": None,
        }
        if depths:
            s = sorted(depths)
            out["median_atr"] = round(s[len(s) // 2], 3)
            out["p90_atr"] = round(s[min(len(s) - 1, int(len(s) * 0.9))], 3)
        return out
    except Exception:  # noqa: BLE001 — 统计失败按无数据处理，档案留待补充
        return None


# ─────────────────────────── 每币种原则档案 ───────────────────────────

def _safe_symbol(symbol) -> str:
    """币种名 → 安全文件名主干（只留 A-Z0-9，防路径穿越）。"""
    s = re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())
    return s or "UNKNOWN"


def profile_path(symbol) -> str:
    return os.path.join(PROFILE_DIR, f"{_safe_symbol(symbol)}.md")


def _render_template(symbol: str, stats: dict | None, tf: str) -> str:
    sym = _safe_symbol(symbol)
    ts = time.strftime("%Y-%m-%d %H:%M")
    if stats:
        vol_lines = [
            f"- 参考周期：{tf}（统计样本 {stats['lookback_bars']} 根）",
            f"- ATR14 均幅：约 {stats['atr_pct_avg']}%/根" if stats.get("atr_pct_avg")
            is not None else f"- ATR14 均幅：{_PLACEHOLDER}",
            f"- 单根最大振幅：{stats['max_move_pct']}%" if stats.get("max_move_pct")
            is not None else f"- 单根最大振幅：{_PLACEHOLDER}",
        ]
        if stats.get("n") and stats.get("median_atr") is not None:
            sweep_lines = [
                f"- 插针样本：近 {stats['lookback_bars']} 根出现 {stats['n']} 次"
                "「刺破前高/前低后收回」",
                f"- 插针深度中位数：{stats['median_atr']}×ATR；"
                f"90 分位：{stats['p90_atr']}×ATR",
                f"- 提示：止损放在关键位外侧 ≥ {stats['p90_atr']}×ATR，"
                "别把止损喂给扫单",
            ]
        else:
            sweep_lines = [f"- 插针样本：统计窗口内 0 次（样本不足，仅供参考）",
                           f"- 插针深度：{_PLACEHOLDER}"]
    else:
        vol_lines = [f"- 参考周期：{tf}", f"- ATR14 均幅：{_PLACEHOLDER}",
                     f"- 单根最大振幅：{_PLACEHOLDER}"]
        sweep_lines = [f"- 插针样本：{_PLACEHOLDER}", f"- 插针深度：{_PLACEHOLDER}"]
    lines = [
        f"# {sym} · 交易原则档案",
        "",
        "> 本文件归你所有：直接编辑保存即生效，AI 导师每次解释前都会读取。",
        f"> 系统于 {ts} 自动生成模板；「我的纪律」区段 AI 只引用、不改写。",
        "",
        "## 波动特性",
        *vol_lines,
        "",
        "## 典型扫单深度",
        *sweep_lines,
        "",
        "## 适合周期",
        "- （模板默认，请按自己的作息修改）15m/1h 观察节奏，4h 定方向；",
        "  信号周期与持仓耐心不匹配是新手最常见的亏损放大器",
        "",
        "## 我的纪律",
        "- 单笔风险 ≤ 账户 1%",
        "- 亏损后 30 分钟内不开新单（防报复性交易）",
        "- 开仓前必须写清：入场理由 / 止损位 / 止盈位，缺一不下单",
        "- （在下方继续追加你自己的规则，AI 会逐条对照检查）",
        "",
    ]
    return "\n".join(lines)


def ensure_profile(symbol, df: pd.DataFrame | None = None,
                   tf: str = "15m") -> dict:
    """确保币种档案存在：已存在原样返回（绝不覆盖用户编辑），缺失则生成模板。

    df 可选：传入 K 线时「波动特性/典型扫单深度」实算填入，否则留 ⏳占位。
    返回 {path, created(bool 本次是否新建), content}；I/O 失败降级为内存模板
    （path=None），解释链路照常可用。
    """
    p = profile_path(symbol)
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return {"path": p, "created": False, "content": f.read()}
        content = _render_template(symbol, sweep_depth_stats(df), tf)
        os.makedirs(PROFILE_DIR, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, p)
        return {"path": p, "created": True, "content": content}
    except OSError:
        return {"path": None, "created": False,
                "content": _render_template(symbol, sweep_depth_stats(df), tf)}


def load_profile(symbol) -> str | None:
    """读取档案原文；不存在返回 None（不隐式建档，建档走 ensure_profile）。"""
    try:
        with open(profile_path(symbol), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# ─────────────────────────── 证据包（agent-8 对接面） ───────────────────────────

# 期望 jarvis_trade_mentor 暴露其中任一 getter：fn(plan_id) -> dict|None。
# agent-8 已落地 get_plan(plan_id:int)（mentor_plan 行 + verdict_json 解析），
# 预留别名探测保持向前兼容。
_BUNDLE_GETTERS = ("get_plan_bundle", "get_plan", "load_plan", "plan_bundle")


def fetch_plan_bundle(plan_id) -> dict | None:
    """从 jarvis_trade_mentor（agent-8）取 计划+裁决 证据包；未就绪返回 None。

    try-import 容错：模块缺失 / getter 缺失 / 调用异常 / 返回非 dict 一律 None，
    绝不抛出（mentor_plan 表归 agent-8 所有，本模块不直连 DB 猜 schema）。
    """
    try:
        import jarvis_trade_mentor as jtm  # noqa: PLC0415 — 运行时探测对接面
    except Exception:  # noqa: BLE001
        return None
    try:
        pid = int(plan_id)          # get_plan 契约为 int 主键
    except (TypeError, ValueError):
        pid = plan_id
    for name in _BUNDLE_GETTERS:
        fn = getattr(jtm, name, None)
        if not callable(fn):
            continue
        try:
            bundle = fn(pid)
        except Exception:  # noqa: BLE001 — 对接面异常不拖垮解释链
            return None
        return bundle if isinstance(bundle, dict) else None
    return None


def sample_bundle() -> dict:
    """自造联调样例：结构对齐 jarvis_trade_mentor.get_plan(plan_id) 真实输出
    （mentor_plan 打平行 + verdict：light/score/items/summary/vetoes/cooldown_min）。
    """
    return {
        "id": 0, "created_ts": 1786500600.0, "symbol": "BTCUSDT", "tf": "15m",
        "direction": "long", "entry": 61250.0, "stop_loss": 61180.0,
        "take_profit": 61600.0,
        "reason": "刚才跌了这么多，感觉要反弹了，快速搏一把",
        "emotion_score": 4, "light": "red", "score": 32.5,
        "cooldown_until": None, "status": "open", "followed": None,
        "result": None, "pnl_pct": None, "note": None, "closed_ts": None,
        "verdict": {
            "light": "red", "score": 32.5, "cooldown_min": 30,
            "items": [
                {"key": "trend", "level": "fail", "weight": 30,
                 "evidence": "12 系统共识看跌（置信 62%）而计划做多，逆势且无反转确认",
                 "detail": {}},
                {"key": "risk", "level": "fail", "weight": 25,
                 "evidence": "SL 距入场 0.11% < 15m 噪声带下限 0.7%，"
                             "大概率被常规插针扫掉；过路费占风险预算 0.9",
                 "detail": {}},
                {"key": "levels", "level": "fail", "weight": 20,
                 "evidence": "入场上方 0.4×ATR 处有未回补看跌 FVG [61350, 61420]，"
                             "正对压力区追多", "detail": {}},
                {"key": "structure", "level": "warn", "weight": 15,
                 "evidence": "威科夫阶段派发 C，多头结构证据不足", "detail": {}},
                {"key": "micro", "level": "unavailable", "weight": 10,
                 "evidence": "微观数据源未就绪，本条件不计入", "detail": {}},
            ],
            "vetoes": ["止损距离踩 R1 硬红线（<噪声带下限）"],
            "summary": "红灯（33 分）：证据不支持这单做多，强烈建议放弃。"
                       "✗✗ 止损距离踩 R1 硬红线",
            "coverage_note": None,
            "emotion_note": "情绪自评 4/5 且计划与共识反向——上头时最容易做的就是"
                            "逆势重仓。强制降档，建议冷静 30 分钟后重新提交裁决",
            "as_of": 1786500600.0,
            "prereg": {"weights": {"trend": 30, "risk": 25, "levels": 20,
                                   "structure": 15, "micro": 10},
                       "rr_hard_min": 1.5, "emotion_hot": 4, "cooldown_min": 30},
        },
    }


# ─────────────────────────── Prompt 组装 ───────────────────────────

MENTOR_SYS = (
    "你是一位情绪风控导师（教练），对象是刚接触合约交易的新手。输入 JSON 包含："
    "下单计划（direction/entry/stop_loss/take_profit/tf/reason=用户文字理由/"
    "emotion_score=情绪自评 1-5）、verdict（确定性规则引擎的裁决：light 灯色 "
    "green=证据成立可执行/yellow=有硬伤建议缩仓或等确认/red=证据不支持强烈建议放弃；"
    "score 0-100；items 逐条证据，level: pass=通过/warn=警示/fail=不过关/"
    "unavailable=证据源不可用不计分；vetoes=一票否决硬红线；cooldown_min=强制冷静"
    "分钟数）、profile_md（该币种交易原则档案，其中「我的纪律」区段是用户亲手写的"
    "规则）。\n"
    "输出 Markdown，300~500 字，按以下结构：\n"
    "1. **裁决一句话**：这单为什么是红/黄/绿灯，直说结论（灯色与分数以 verdict "
    "为准，不许自行改判）\n"
    "2. **每条证据在说什么**：把 verdict.items 逐条翻译成人话（每条 ≤1 行；"
    "fail 与 vetoes 一条都不许跳过；unavailable 要说明「证据不可用≠没问题」）\n"
    "3. **对照你自己的纪律**：引用档案「我的纪律」里与这单相关的条目，指出违反/"
    "符合了哪条；档案没写纪律就提醒用户去补\n"
    "4. **如果你坚持要开**：给出需要先满足的具体条件（止损改到什么类型的位置、"
    "等待什么确认信号、仓位/杠杆降到多少；有 cooldown_min 时明确先冷静多少分钟）\n"
    f"纪律：只引用输入数据里存在的数字与事实，严禁编造行情、价格或胜率；"
    f"emotion_score ≥ {EMOTION_HOT_SCORE}（1-5 自评，≥{EMOTION_HOT_SCORE}=上头）时"
    "第 1 段先用一两句话共情（认可亏损/焦虑的感受），再讲规则，语气坚定但不训斥；"
    "不要复述 JSON 字段名，说人话。\n"
    "思考过程尽量精简，把输出预算留给正式回答（这是流式教学场景，答案本体优先）。\n"
    "结尾固定输出一行：「以上为交易纪律教学，不构成投资建议。」"
)

# [R3] 返空重试用精简版：混合推理模型（deepseek-v4-flash 等）思考 token 走
# reasoning_content 且计入 max_tokens——全量档案+全部证据会诱发长思考，把
# 用户设置的输出预算（默认 900）整个烧光，正式回答一个字都没开始
# （实测 finish_reason=length、content 0 块）。精简版只保留裁决骨架。
MENTOR_SYS_LITE = (
    "你是情绪风控导师，对象是新手。输入是下单计划的系统裁决摘要"
    "（light: green=可执行/yellow=有硬伤/red=建议放弃；checks 逐条证据，"
    "pass=false 为不过关项；discipline 是用户自己写的纪律）。\n"
    "直接输出 Markdown 答案（不要长篇思考），200~350 字：\n"
    "1. 灯色结论一句话（以输入为准不改判）\n"
    "2. 逐条把 checks 翻译成人话（不过关项一条不许漏）\n"
    "3. 对照 discipline 指出违反/符合的条目\n"
    "4. 若坚持要开需先满足的具体条件\n"
    "只引用输入里的数字与事实，不编造；结尾固定一行："
    "「以上为交易纪律教学，不构成投资建议。」"
)

RETRY_MAX_TOKENS = 2000     # 重试档显式输出预算（钳制区间 100~8000 内）
_LITE_MAX_CHECKS = 5        # 精简证据条数上限
_LITE_EVIDENCE_CHARS = 100  # 单条证据截断长度
_LITE_REASON_CHARS = 120    # 用户理由截断长度
_LITE_DISCIPLINE_CHARS = 400


def build_messages(bundle: dict, profile_md: str | None = None) -> list[dict]:
    """证据包 + 币种档案 → LLM messages（system + user JSON digest）。

    profile_md 缺省时按 bundle.symbol 读档案，无档案则自动生成模板（无 K 线
    版，数值留待补充——解释可用性优先，不因档案缺失阻塞）。
    """
    symbol = (bundle or {}).get("symbol") or "UNKNOWN"
    if profile_md is None:
        profile_md = load_profile(symbol)
        if profile_md is None:
            profile_md = ensure_profile(symbol)["content"]
    digest = {"symbol": _safe_symbol(symbol), "profile_md": profile_md,
              **{k: v for k, v in (bundle or {}).items() if k != "symbol"}}
    return [
        {"role": "system", "content": MENTOR_SYS},
        {"role": "user", "content": json.dumps(digest, ensure_ascii=False,
                                               default=str)},
    ]


# ─────────────────────────── [R3] 返空重试 + 本地解读兜底 ───────────────────────────

def _resolve_profile(bundle: dict, profile_md: str | None) -> str:
    if profile_md is not None:
        return profile_md
    symbol = (bundle or {}).get("symbol") or "UNKNOWN"
    md = load_profile(symbol)
    return md if md is not None else ensure_profile(symbol)["content"]


def _discipline_lines(profile_md: str | None) -> list[str]:
    """从档案提取「## 我的纪律」区段的条目行（无档案/无区段返回空表）。"""
    if not profile_md:
        return []
    lines, hit = [], False
    for ln in profile_md.splitlines():
        if ln.strip().startswith("## "):
            hit = ln.strip() == "## 我的纪律"
            continue
        if hit and ln.strip().startswith("-"):
            lines.append(ln.strip())
    return lines


_LEVEL_RANK = {"fail": 0, "warn": 1, "unavailable": 2, "pass": 3}


def build_messages_lite(bundle: dict, profile_md: str | None = None) -> list[dict]:
    """精简版 messages（返空重试档）：只带裁决骨架，把推理负担降下来。

    内容：计划要点 + verdict 的 light/score/summary/vetoes + 按 fail→warn→
    unavailable→pass 排序的前 5 条证据（每条截 100 字）+ 档案「我的纪律」条目
    （截 400 字）。目标是让混合推理模型的思考远短于输出预算。
    """
    b = bundle or {}
    profile_md = _resolve_profile(b, profile_md)
    vd = b.get("verdict") or {}
    items = sorted([it for it in (vd.get("items") or []) if isinstance(it, dict)],
                   key=lambda it: _LEVEL_RANK.get(it.get("level"), 9))
    checks = [{"name": it.get("key"), "level": it.get("level"),
               "evidence": str(it.get("evidence") or "")[:_LITE_EVIDENCE_CHARS]}
              for it in items[:_LITE_MAX_CHECKS]]
    digest = {
        "symbol": _safe_symbol(b.get("symbol") or "UNKNOWN"),
        "plan": {k: b.get(k) for k in ("direction", "entry", "stop_loss",
                                       "take_profit", "tf", "emotion_score")},
        "reason": str(b.get("reason") or "")[:_LITE_REASON_CHARS],
        "verdict": {"light": vd.get("light") or b.get("light"),
                    "score": vd.get("score", b.get("score")),
                    "summary": vd.get("summary"),
                    "vetoes": vd.get("vetoes") or [],
                    "cooldown_min": vd.get("cooldown_min") or 0,
                    "checks": checks},
        "discipline": "\n".join(_discipline_lines(profile_md))[:_LITE_DISCIPLINE_CHARS],
    }
    return [
        {"role": "system", "content": MENTOR_SYS_LITE},
        {"role": "user", "content": json.dumps(digest, ensure_ascii=False,
                                               default=str)},
    ]


_LIGHT_CN = {"green": "🟢 绿灯：证据结构成立，可按计划执行",
             "yellow": "🟡 黄灯：有硬伤未解决，建议缩仓一半或等待确认",
             "red": "🔴 红灯：证据不支持这单，强烈建议放弃"}
_LEVEL_ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌", "unavailable": "❔"}
_KEY_CN = {"trend": "趋势/共识", "risk": "风险与止损", "levels": "关键价位",
           "structure": "结构证据", "micro": "微观资金流",
           "event_risk": "事件风险窗", "premium_discount": "折价/溢价区"}
_FAIL_ADVICE = {
    "trend": "等共识转到与你同向，或出现明确的反转确认再考虑进场",
    "risk": "把止损放到结构位外侧（参考档案的插针深度提示），并守住盈亏比门槛",
    "levels": "避开正对的压力/支撑与未回补缺口，等价格离开磁吸区再评估",
    "structure": "等结构证据（阶段/形态）与方向一致再进场",
    "micro": "等微观资金流不再与方向打架（主动买卖/大单不逆向）",
}


def local_explanation(bundle: dict, profile_md: str | None = None) -> str:
    """本地规则解读（零 LLM）：用 verdict 拼模板化小白话——AI 抽风时的最终兜底。

    永不抛出；证据缺失多少讲多少，开头固定标注「本地解读，AI 暂不可用」。
    """
    try:
        b = bundle or {}
        vd = b.get("verdict") or {}
        light = vd.get("light") or b.get("light") or "yellow"
        score = vd.get("score", b.get("score"))
        parts = ["> 🤖 **本地解读，AI 暂不可用**（以下由确定性规则引擎生成）", ""]
        head = _LIGHT_CN.get(light, f"裁决灯色：{light}")
        parts.append(f"**{head}**" + (f"（{score:.0f} 分）" if isinstance(
            score, (int, float)) else ""))
        if vd.get("summary"):
            parts.append(str(vd["summary"]))
        items = [it for it in (vd.get("items") or []) if isinstance(it, dict)]
        if items:
            parts.append("")
            parts.append("**每条证据在说什么**")
            for it in sorted(items, key=lambda x: _LEVEL_RANK.get(x.get("level"), 9)):
                icon = _LEVEL_ICON.get(it.get("level"), "•")
                name = _KEY_CN.get(it.get("key"), it.get("key") or "证据")
                ev = str(it.get("evidence") or "").strip()
                suffix = "（证据源不可用≠没问题）" if it.get("level") == "unavailable" else ""
                parts.append(f"- {icon} **{name}**：{ev}{suffix}")
        vetoes = vd.get("vetoes") or []
        if vetoes:
            parts.append("")
            parts.append("**一票否决（硬红线）**")
            parts += [f"- ✗✗ {v}" for v in vetoes]
        disc = _discipline_lines(_resolve_profile(b, profile_md))
        if disc:
            parts.append("")
            parts.append("**对照你自己的纪律（逐条自查）**")
            parts += disc[:6]
        fails = [it for it in items if it.get("level") == "fail"]
        if fails or vetoes:
            parts.append("")
            parts.append("**如果你坚持要开，先满足这些**")
            seen = set()
            for it in fails:
                adv = _FAIL_ADVICE.get(it.get("key"))
                if adv and adv not in seen:
                    seen.add(adv)
                    parts.append(f"- {adv}")
            cd = vd.get("cooldown_min") or 0
            if cd:
                parts.append(f"- 先冷静 {cd} 分钟再重新提交裁决（当前处于强制冷静期）")
        if vd.get("emotion_note"):
            parts.append("")
            parts.append(f"> {vd['emotion_note']}")
        parts.append("")
        parts.append("以上为交易纪律教学，不构成投资建议。")
        return "\n".join(parts)
    except Exception:  # noqa: BLE001 — 兜底的兜底：结构异常也要给出可读文本
        return ("> 🤖 **本地解读，AI 暂不可用**\n\n证据包结构异常，无法逐条解读；"
                "请以面板上的灯色与证据行为准。\n\n以上为交易纪律教学，不构成投资建议。")


def stream_explanation(bundle: dict, *, chat_stream_fn=None,
                       profile_md: str | None = None):
    """导师解释三级降级流水线（生成器，逐段 yield 文本）：

      ① 全量 prompt（档案+全部证据）流式；有内容即正常结束
      ② 返空 → 提示后用精简 prompt + 显式 max_tokens=2000 重试一次
         （根因：混合推理模型思考 token 计入输出预算，全量 prompt 诱发长思考
         把预算烧光，content 一个字没出就 finish_reason=length）
      ③ 仍空/调用失败 → 本地规则解读（标注「本地解读，AI 暂不可用」）
    chat_stream_fn(messages, **kw) 缺省用 jarvis_llm_config.chat_stream
    （module=mentor_explain 记账）；未配置 LLM 的 LLMNotConfigured 原样上抛
    （端点层已前置拦截，这里不吞）。除此之外本生成器自身不抛出。
    """
    from jarvis_llm_config import LLMCallError, LLMNotConfigured

    if chat_stream_fn is None:
        import jarvis_llm_config as _jlc

        def chat_stream_fn(messages, **kw):  # noqa: ANN001
            return _jlc.chat_stream(messages, timeout=90,
                                    module="mentor_explain", **kw)

    profile_md = _resolve_profile(bundle, profile_md)

    class _EmptyStream(Exception):
        pass

    def _attempt(messages, **kw):
        got = False
        for delta in chat_stream_fn(messages, **kw):
            got = True
            yield delta
        if not got:
            raise _EmptyStream()

    # ── ① 全量 prompt ──
    try:
        yield from _attempt(build_messages(bundle, profile_md))
        return
    except _EmptyStream:
        yield ("\n> ⚠️ AI 首次返回为空（推理占满输出预算），"
               "已自动用精简提示重试…\n\n")
    except LLMNotConfigured:
        raise
    except LLMCallError as exc:
        yield f"\n> ⚠️ AI 调用失败（{str(exc)[:120]}），以下为本地规则解读：\n\n"
        yield local_explanation(bundle, profile_md)
        return

    # ── ② 精简 prompt 重试（显式放大输出预算） ──
    try:
        yield from _attempt(build_messages_lite(bundle, profile_md),
                            max_tokens=RETRY_MAX_TOKENS)
        return
    except _EmptyStream:
        yield "\n> ⚠️ AI 重试仍未返回内容，已降级本地解读：\n\n"
    except LLMNotConfigured:
        raise
    except LLMCallError as exc:
        yield f"\n> ⚠️ AI 重试调用失败（{str(exc)[:120]}），以下为本地规则解读：\n\n"

    # ── ③ 本地规则解读兜底 ──
    yield local_explanation(bundle, profile_md)
