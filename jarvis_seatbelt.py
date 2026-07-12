#!/usr/bin/env python3
"""贾维斯 JARVIS — 「安全带」确认层（Delta/CVD 吸收背离 × 12 系统信号）。

核心思想（用户口径）：纯 K 线指标给出的反转信号可能是「假反转」，只有
**Delta 与价格背离（吸收证据）**才说明有真实的大单在接货/派发——吸收订单
出现，行情才拐弯。本模块把 Delta 引擎（jarvis_delta_flow，MCP-5）的背离/
吸收证据叠加到十二套技术共识上，做三态确认：

  confirmed   信号方向与同向吸收背离共振 → 反转可信度高，置信度小幅加成
  no-evidence 信号有方向但无吸收证据 → 可能是指标假反转，提示谨慎
  conflict    信号方向与反向派发/吸收背离顶撞 → 显著降级 + 红色警示

与 jarvis_sentiment.apply_to_consensus 同风格：
  - 纯函数核心 evaluate()/apply_to_consensus()：只吃 dict、不联网，可单测
  - 不改共识原 direction/confidence，修正值放 seatbelt.adjusted_confidence
  - Delta 引擎缺失/取数失败 → status='unavailable'，不影响共识主体
"""

from __future__ import annotations

import time

# 置信度修正上限（与 sentiment 层同量级：确认小幅加成、冲突降级更重）
CONFIRM_DELTA = {"strong": 0.10, "moderate": 0.07, "weak": 0.04}
CONFLICT_DELTA = {"strong": -0.15, "moderate": -0.12, "weak": -0.08}

_STATUS_CN = {
    "confirmed": "吸收证据确认",
    "no-evidence": "无吸收证据",
    "conflict": "背离冲突",
    "idle": "中性无需确认",
    "unavailable": "Delta 引擎未就绪",
}


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _strength_grade(strength) -> str:
    """契约 strength 兼容数值（0~1）与字符串（strong/moderate/weak）两种形态。"""
    if isinstance(strength, str):
        s = strength.lower()
        return s if s in ("strong", "moderate", "weak") else "moderate"
    try:
        v = float(strength)
    except (TypeError, ValueError):
        return "moderate"
    if v >= 0.66:
        return "strong"
    if v >= 0.33:
        return "moderate"
    return "weak"


def _div_side(delta_payload: dict, side: str) -> dict | None:
    """取 divergence.bullish / divergence.bearish；未激活返回 None。"""
    div = (delta_payload or {}).get("divergence") or {}
    d = div.get(side) or {}
    return d if d.get("active") else None


def evaluate(direction: str, delta_payload: dict | None) -> dict:
    """技术面方向 × Delta 背离证据 → 安全带三态判定（纯函数）。

    Args:
        direction: 共识方向 bullish | bearish | neutral
        delta_payload: /api/delta 契约体（{divergence, absorption, ...}）；
                       None = 引擎不可用
    Returns:
        {status, grade, confidence_delta, note, divergence_note, absorption}
    """
    if not delta_payload or not delta_payload.get("ok", True) is True:
        return {"status": "unavailable", "grade": None, "confidence_delta": 0.0,
                "note": "Delta/CVD 引擎未就绪，安全带确认层暂不参与判定",
                "divergence_note": None, "absorption": None}

    absorption = (delta_payload.get("absorption") or {}) or None
    if direction not in ("bullish", "bearish"):
        return {"status": "idle", "grade": None, "confidence_delta": 0.0,
                "note": "共识中性，无方向可确认", "divergence_note": None,
                "absorption": absorption}

    same = _div_side(delta_payload, "bullish" if direction == "bullish" else "bearish")
    opposite = _div_side(delta_payload, "bearish" if direction == "bullish" else "bullish")
    dir_cn = "看涨" if direction == "bullish" else "看跌"

    # 反向背离优先级最高：即便同向也有弱背离，顶撞证据必须先亮红灯
    if opposite:
        grade = _strength_grade(opposite.get("strength"))
        return {
            "status": "conflict", "grade": grade,
            "confidence_delta": CONFLICT_DELTA[grade],
            "note": (f"技术面{dir_cn}，但 Delta 出现反向{grade} 级背离——"
                     "对手方吸收/派发证据顶撞信号方向，谨防假突破，建议观望或轻仓"),
            "divergence_note": opposite.get("note"),
            "absorption": absorption,
        }
    if same:
        grade = _strength_grade(same.get("strength"))
        return {
            "status": "confirmed", "grade": grade,
            "confidence_delta": CONFIRM_DELTA[grade],
            "note": (f"技术面{dir_cn} + 同向{grade} 级吸收背离——"
                     "吸收订单已出现，反转可信度高"),
            "divergence_note": same.get("note"),
            "absorption": absorption,
        }
    return {
        "status": "no-evidence", "grade": None, "confidence_delta": 0.0,
        "note": (f"技术面{dir_cn}但未见 Delta 吸收证据——"
                 "可能是指标假反转，谨慎进场、等吸收背离出现再加仓"),
        "divergence_note": None,
        "absorption": absorption,
    }


def apply_to_consensus(cons: dict, delta_payload: dict | None) -> dict:
    """把安全带判定叠加到共识（consensus/consensus_multi_tf 输出）上。

    返回 cons 浅拷贝 + seatbelt 键；原 direction/confidence 不改写，
    修正后的建议置信度放 seatbelt.adjusted_confidence 由消费方自行采纳。
    """
    out = dict(cons)
    sb = evaluate(cons.get("direction") or "neutral", delta_payload)
    confidence = float(cons.get("confidence") or 0.0)
    out["seatbelt"] = {
        **sb,
        "adjusted_confidence": round(_clamp01(confidence + sb["confidence_delta"]), 3),
        "status_cn": _STATUS_CN[sb["status"]],
    }
    if sb["status"] in ("confirmed", "conflict") and out.get("reasoning"):
        out["reasoning"] = f"{out['reasoning']}。安全带：{sb['note']}"
    return out


# ─────────────────── 强背离 → 页内提醒（带内存去重节流） ───────────────────

# {(symbol, side): last_ts}；同币同向 30 分钟内不重复入列
_ALERT_COOLDOWN_SEC = 1800
_last_alert: dict[tuple, float] = {}


def maybe_alert_strong_divergence(symbol: str, delta_payload: dict | None) -> None:
    """强吸收背离出现时向页内提醒中心落一条事件（jarvis_alert_center）。

    仅 strong 级触发；同 symbol+方向 30 分钟节流；alert_center 不可用时静默。
    不发邮件（邮件归通知模块管）。
    """
    if not delta_payload:
        return
    for side, side_cn in (("bullish", "看涨"), ("bearish", "看跌")):
        d = _div_side(delta_payload, side)
        if not d or _strength_grade(d.get("strength")) != "strong":
            continue
        key = (symbol, side)
        now = time.time()
        if now - _last_alert.get(key, 0.0) < _ALERT_COOLDOWN_SEC:
            continue
        _last_alert[key] = now
        try:
            import jarvis_alert_center as jac
            jac.add_event(
                kind="delta_divergence", symbol=symbol,
                title=f"{symbol} 出现{side_cn}强吸收背离",
                detail=str(d.get("note") or f"Delta 与价格{side_cn}背离（strong）——吸收订单出现，关注反转窗口"),
                severity="warning",
            )
        except Exception:  # noqa: BLE001 — 提醒失败不影响主链路
            return
