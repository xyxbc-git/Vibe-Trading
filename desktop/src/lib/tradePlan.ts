// 共识交易计划 → 图表水平线 overlay 的纯函数映射。
//
// 后端 /api/twelve/consensus 的 consensus.trade_plan（ConsensusTradePlan）给出
// 入场区间 / 止损 / 止盈价位；本模块把它映射成 KlineChart 能直接渲染的
// DrawHLine 数组（createPriceLine 一条一线）。保持纯函数（无 IO、无组件依赖），
// 方便单测锁定映射关系与方向自洽性。

import type { ConsensusTradePlan } from "../api/client";
import type { DrawHLine } from "./drawings";

export interface PlanOverlay {
  hlines: DrawHLine[];
}

export const PLAN_COLORS = {
  entry: "#58a6ff", // 入场区 · 蓝
  stop: "#f85149",  // 止损 · 红
  profit: "#3fb950", // 止盈 · 绿
} as const;

// 动态精度：≥1 两位小数；0.01~1 四位小数；<0.01 六位有效数字（微价币种
// 如 PEPE/SHIB 的标签不能显示成 0.00）。
export function fmt(v: number): string {
  if (v >= 1) return v.toFixed(2);
  if (v >= 0.01) return v.toFixed(4);
  return Number(v.toPrecision(6)).toString();
}

function validPrice(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && v > 0;
}

// 把共识计划映射为水平线集合：
//   入场区上下沿 → 蓝色虚线 ×2（区间退化成单价时只画一条）
//   止损        → 红色实线「止损 SL」
//   TP1         → 绿色实线「止盈 TP1」
//   TP2（可选） → 绿色虚线「止盈 TP2」
// 任一价位缺失 / 非法（NaN、<=0）时跳过该线，其余照画；plan 为空返回空集合，
// 调用方据此显示「无计划（中性）」而不是崩溃。
export function planToOverlay(plan: ConsensusTradePlan | null | undefined): PlanOverlay {
  const hlines: DrawHLine[] = [];
  if (!plan) return { hlines };

  const zone = Array.isArray(plan.entry_zone) ? plan.entry_zone : [];
  const zonePrices = zone.filter(validPrice).sort((a, b) => a - b);
  if (zonePrices.length >= 2) {
    const lo = zonePrices[0];
    const hi = zonePrices[zonePrices.length - 1];
    if (hi > lo) {
      hlines.push(
        { price: lo, color: PLAN_COLORS.entry, width: 2, style: "dashed", label: `入场区 ${fmt(lo)}`, kind: "entry" },
        { price: hi, color: PLAN_COLORS.entry, width: 2, style: "dashed", label: `入场区 ${fmt(hi)}`, kind: "entry" },
      );
    } else {
      hlines.push({ price: lo, color: PLAN_COLORS.entry, width: 2, style: "dashed", label: `入场 ${fmt(lo)}`, kind: "entry" });
    }
  } else if (zonePrices.length === 1) {
    hlines.push({ price: zonePrices[0], color: PLAN_COLORS.entry, width: 2, style: "dashed", label: `入场 ${fmt(zonePrices[0])}`, kind: "entry" });
  }

  if (validPrice(plan.stop_loss)) {
    hlines.push({ price: plan.stop_loss, color: PLAN_COLORS.stop, width: 2, style: "solid", label: `止损 SL ${fmt(plan.stop_loss)}`, kind: "sl" });
  }
  // TP2 缺失（null，后端在两目标相等时输出 null）或与 TP1 同价 → 只画一条
  // 「止盈 TP」，避免重叠双线双标签。
  const tp1 = validPrice(plan.take_profit_1) ? plan.take_profit_1 : null;
  const tp2 = validPrice(plan.take_profit_2) ? plan.take_profit_2 : null;
  if (tp1 !== null && tp2 !== null && tp2 !== tp1) {
    hlines.push({ price: tp1, color: PLAN_COLORS.profit, width: 2, style: "solid", label: `止盈 TP1 ${fmt(tp1)}`, kind: "tp1" });
    hlines.push({ price: tp2, color: PLAN_COLORS.profit, width: 1, style: "dashed", label: `止盈 TP2 ${fmt(tp2)}`, kind: "tp2" });
  } else if (tp1 !== null) {
    hlines.push({ price: tp1, color: PLAN_COLORS.profit, width: 2, style: "solid", label: `止盈 TP ${fmt(tp1)}`, kind: "tp" });
  } else if (tp2 !== null) {
    hlines.push({ price: tp2, color: PLAN_COLORS.profit, width: 2, style: "solid", label: `止盈 TP ${fmt(tp2)}`, kind: "tp" });
  }

  return { hlines };
}
