// 共识交易计划 → K 线图「交易区间带」的纯函数映射。
//
// tradePlan.ts 把计划映射成水平价格线（边界 + 轴标签）；本模块把同一份计划
// 映射成时间锚定的半透明矩形区域（supply/demand zone 画法，不再全屏铺满）：
//   入场区带  entry_zone 上下沿之间（蓝）
//   止损区带  止损价 ↔ 入场区近端之间的风险段（红）
//   止盈区带  入场区远端 ↔ TP1 的收益段（绿）；TP1→TP2 为更淡的延伸带
// 几何随多空方向镜像（多单止损在下方，空单止损在上方）。
// 价格几何由 planToZones 给出；横向时间窗口由 zoneTimeWindow 给出。
// 保持纯函数（无 IO、无图表依赖），渲染交给 TradeZonesPrimitive。

import type { ConsensusTradePlan } from "../api/client";
import { PLAN_COLORS, fmt } from "./tradePlan";

export type TradeZoneKind = "entry" | "stop" | "profit" | "profit2";

export interface TradeZone {
  kind: TradeZoneKind;
  /** 区间上沿价（恒 > bottom） */
  top: number;
  /** 区间下沿价 */
  bottom: number;
  color: string;
  /** 填充透明度 0..1（画在蜡烛层下方，保持低透明不遮 K 线） */
  fillAlpha: number;
  /** 带内文字标注（含边界价格） */
  label: string;
  /** 悬停提示（含盈亏比） */
  tooltip: string;
}

// 填充透明度：入场带略强调，延伸带最淡
const ZONE_ALPHA: Record<TradeZoneKind, number> = {
  entry: 0.1,
  stop: 0.07,
  profit: 0.07,
  profit2: 0.04,
};

// ── 时间窗口（矩形横向边界） ────────────────────────────────────────────
//
// ConsensusTradePlan 不携带信号形成时间戳（仅 source_tf），无法精确锚定
// 「区间形成的那根 K 线」；按「最近 N 根 K 线」回退：左边界锚定倒数第
// ZONE_LOOKBACK_BARS 根，右边界越过最新 K 线再向右延伸 ZONE_EXTEND_BARS 根
// （bar 逻辑索引允许越过最后一根，天然支持右侧留白）。窗口随新 K 线滑动，
// 与计划本身 90s 轮询刷新的语义一致。后端未来补充锚点时间戳时，只需把
// from 换成对应 bar 索引，渲染层无需改动。

export interface ZoneTimeWindow {
  /** 左边界 bar 逻辑索引（含，≥0） */
  from: number;
  /** 右边界 bar 逻辑索引（可越过最后一根 K 线，实现向右延伸） */
  to: number;
}

/** 矩形覆盖的最近 K 线根数（信号形成段的可视近似） */
export const ZONE_LOOKBACK_BARS = 24;
/** 矩形越过最新 K 线后向右延伸的根数（任务口径 5–10 根取中值） */
export const ZONE_EXTEND_BARS = 6;

/**
 * 由 K 线总根数推导区间带矩形的时间窗口：覆盖最近 min(lookback, barCount)
 * 根 K 线 + 右侧 extend 根延伸；barCount ≤ 0 时返回 null（无从锚定不绘制）。
 */
export function zoneTimeWindow(
  barCount: number,
  lookback: number = ZONE_LOOKBACK_BARS,
  extend: number = ZONE_EXTEND_BARS,
): ZoneTimeWindow | null {
  if (!Number.isFinite(barCount) || barCount <= 0) return null;
  const lastIdx = Math.floor(barCount) - 1;
  return {
    from: Math.max(0, lastIdx - Math.max(1, Math.floor(lookback)) + 1),
    to: lastIdx + Math.max(0, Math.floor(extend)),
  };
}

function validPrice(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && v > 0;
}

/** 盈亏比：优先用后端 rr，缺失时按 入场中值/止损/TP1 推算；无法推算返回 null。 */
export function planRR(plan: ConsensusTradePlan | null | undefined): number | null {
  if (!plan) return null;
  if (plan.rr != null && Number.isFinite(plan.rr) && plan.rr > 0) return Number(plan.rr);

  const zone = Array.isArray(plan.entry_zone) ? plan.entry_zone.filter(validPrice) : [];
  if (zone.length === 0) return null;
  const mid = zone.reduce((a, b) => a + b, 0) / zone.length;
  const sl = validPrice(plan.stop_loss) ? plan.stop_loss : null;
  const tp = validPrice(plan.take_profit_1)
    ? plan.take_profit_1
    : validPrice(plan.take_profit_2)
      ? plan.take_profit_2
      : null;
  if (sl === null || tp === null) return null;
  const risk = Math.abs(mid - sl);
  if (risk <= 0) return null;
  return Math.abs(tp - mid) / risk;
}

// 多空判定：后端 side 字段优先；缺失时按 SL/TP 相对入场区的几何位置派生；
// 都缺时回退共识 direction（"bearish" → 空）。
function resolveSide(
  plan: ConsensusTradePlan,
  eLo: number,
  eHi: number,
  direction?: string,
): "long" | "short" {
  if (plan.side === "long" || plan.side === "short") return plan.side;
  const mid = (eLo + eHi) / 2;
  if (validPrice(plan.stop_loss)) return plan.stop_loss < mid ? "long" : "short";
  const tp = validPrice(plan.take_profit_1)
    ? plan.take_profit_1
    : validPrice(plan.take_profit_2)
      ? plan.take_profit_2
      : null;
  if (tp !== null) return tp > mid ? "long" : "short";
  return direction === "bearish" ? "short" : "long";
}

const ZONE_NAME: Record<TradeZoneKind, string> = {
  entry: "入场区",
  stop: "止损区",
  profit: "止盈区",
  profit2: "止盈延伸 TP2",
};

/**
 * 把共识计划映射为区间带集合（按价格几何自动多空镜像）：
 *   多单： SL —红— 入场下沿 —蓝— 入场上沿 —绿— TP1 —淡绿— TP2
 *   空单： TP2 —淡绿— TP1 —绿— 入场下沿 —蓝— 入场上沿 —红— SL
 * 任一价位缺失/非法时跳过对应带，其余照画；plan 为空返回空数组。
 */
export function planToZones(
  plan: ConsensusTradePlan | null | undefined,
  direction?: string,
): TradeZone[] {
  const zones: TradeZone[] = [];
  if (!plan) return zones;

  const zonePrices = (Array.isArray(plan.entry_zone) ? plan.entry_zone : [])
    .filter(validPrice)
    .sort((a, b) => a - b);
  if (zonePrices.length === 0) return zones; // 无入场锚点 → 风险/收益段无从算起

  const eLo = zonePrices[0];
  const eHi = zonePrices[zonePrices.length - 1];
  const side = resolveSide(plan, eLo, eHi, direction);
  const short = side === "short";

  const rr = planRR(plan);
  const rrText = rr !== null ? `盈亏比 ${rr.toFixed(1)}` : "盈亏比 —";
  const dirText = short ? "方向 空" : "方向 多";

  const push = (kind: TradeZoneKind, bottom: number, top: number) => {
    if (!(top > bottom)) return; // 退化/倒挂区间不画
    const range = `${fmt(bottom)} – ${fmt(top)}`;
    zones.push({
      kind,
      top,
      bottom,
      color:
        kind === "entry"
          ? PLAN_COLORS.entry
          : kind === "stop"
            ? PLAN_COLORS.stop
            : PLAN_COLORS.profit,
      fillAlpha: ZONE_ALPHA[kind],
      label: `${ZONE_NAME[kind]} ${range}`,
      tooltip: `${ZONE_NAME[kind]} ${range} · ${dirText} · ${rrText}`,
    });
  };

  // 入场区带（单价退化时不画带，边界价格线仍由 tradePlan 覆盖）
  push("entry", eLo, eHi);

  // 止损风险带：多单在入场区下方，空单在上方
  if (validPrice(plan.stop_loss)) {
    if (short) push("stop", eHi, plan.stop_loss);
    else push("stop", plan.stop_loss, eLo);
  }

  // 止盈收益带：TP1 为主带；TP1 缺失时用 TP2 顶主带（与 planToOverlay 同口径退化）
  const tp1 = validPrice(plan.take_profit_1) ? plan.take_profit_1 : null;
  const tp2 = validPrice(plan.take_profit_2) ? plan.take_profit_2 : null;
  const mainTp = tp1 ?? tp2;
  if (mainTp !== null) {
    if (short) push("profit", mainTp, eLo);
    else push("profit", eHi, mainTp);
    // TP1→TP2 延伸带（两者都有效且不同价才画）
    if (tp1 !== null && tp2 !== null && tp2 !== tp1) {
      if (short) push("profit2", tp2, tp1);
      else push("profit2", tp1, tp2);
    }
  }

  return zones;
}
