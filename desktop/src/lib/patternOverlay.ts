// Pattern → chart render payload for the desktop K-line (lightweight-charts).
//
// The web version emits echarts markLine/markArea; here we reuse the desktop
// chart's EXISTING primitives instead of inventing new ones:
//   - 上下轨 / 颈线      → DrawingResult.segments（KlineChart 已按 bar 索引画斜线）
//   - 突破位/目标位/止损位 → DrawingResult.hlines（createPriceLine，label 直接上图）
//   - 触点/关键点        → StructureMarker（原生 setMarkers 圆点 + 中文短标注）
//
// Everything is pattern-prefixed so it can't clash with the trap-marker layer
// being developed in parallel.

import type { DetectedPattern, PatternDirection, PatternType } from "./patterns";
import { fmtPrice } from "./patterns";
import type { DrawingResult, DrawSegment, DrawHLine } from "./drawings";
import type { StructureMarker } from "../api/client";

// 方向色与全站盈亏语义一致：看涨绿 / 看跌红 / 中性黄（深色底上灰线不可读）
export const PATTERN_COLORS: Record<PatternDirection, string> = {
  bullish: "#3fb950",
  bearish: "#f85149",
  neutral: "#d29922",
};

export interface PatternChartOverlay {
  drawings: DrawingResult;
  markers: StructureMarker[];
}

const EMPTY_OVERLAY: PatternChartOverlay = {
  drawings: { segments: [], hlines: [], bands: [] },
  markers: [],
};

// Key-point labels embed the formatted price ("顶1 3,250.00") — markers only
// need the short name; the price is visible from the point's position.
const stripTrailingPrice = (label: string) => label.replace(/\s[\d,.]+$/u, "");

// 触点标注挂 K 线上方还是下方：按形态结构语义精确挂靠（价格-中价比较在
// 收敛形态近端会误判，例如上升三角形末段的下轨触点已高于形态中价）。
function markerPosition(type: PatternType, label: string): "above" | "below" {
  // 整理家族：触点标签自带上/下轨前缀
  if (label.startsWith("上轨")) return "above";
  if (label.startsWith("下轨")) return "below";
  // 反转家族：顶/底直接判；肩/头随顶底结构；颈线点在结构的另一侧
  if (label.startsWith("顶")) return "above";
  if (label.startsWith("底")) return "below";
  const bottomStructure = type === "head_shoulders_bottom";
  if (label.startsWith("头") || label.startsWith("左肩") || label.startsWith("右肩")) {
    return bottomStructure ? "below" : "above";
  }
  if (label.startsWith("颈线点")) {
    // 双底/头肩底的颈线点是反弹高点 → 上方；双顶/头肩顶的是回撤低点 → 下方
    return type === "double_bottom" || bottomStructure ? "above" : "below";
  }
  return "above";
}

/**
 * Build the chart payload for ONE pattern (the one selected in the explain
 * card). Pass null (nothing selected / toggle off) to get an empty overlay,
 * which merges into the base payload as a no-op — that's the "关 → 彻底清除"
 * path, because KlineChart rebuilds all overlay series whenever props change.
 */
export function patternToChartOverlay(
  pattern: DetectedPattern | null,
  dates: string[],
): PatternChartOverlay {
  if (!pattern || dates.length === 0) return EMPTY_OVERLAY;

  const color = PATTERN_COLORS[pattern.direction];
  const lastIdx = dates.length - 1;
  const clampIdx = (i: number) => Math.max(0, Math.min(lastIdx, Math.round(i)));

  // 上下轨 / 颈线 → 斜线段
  const segments: DrawSegment[] = pattern.boundaries.map((bd) => ({
    i1: clampIdx(bd.x0),
    p1: bd.y0,
    i2: clampIdx(bd.x1),
    p2: bd.y1,
    color,
    width: 2,
    style: "solid",
    label: `${pattern.nameCn}·${bd.label}`,
  }));

  // 突破 / 量度目标 / 建议止损 → 水平价位线（label 带价格，直接显示在图上）
  const hline = (price: number, name: string, style: DrawHLine["style"]): DrawHLine => ({
    price,
    color,
    width: 1,
    style,
    label: `${name} ${fmtPrice(price)}`,
  });
  const hlines: DrawHLine[] = [
    hline(pattern.breakout, "形态突破", "dashed"),
    hline(pattern.target, "量度目标", "dotted"),
    hline(pattern.stop, "建议止损", "dotted"),
  ];

  // 触点/关键点 → 原生 marker 圆点，按结构语义挂 K 线上方/下方
  const markers: StructureMarker[] = [];
  for (const kp of pattern.keyPoints) {
    const ts = Number(dates[clampIdx(kp.index)]);
    if (!Number.isFinite(ts)) continue; // dates 非 unix 秒（异常数据）时跳过打点
    markers.push({
      ts,
      price: kp.price,
      position: markerPosition(pattern.type, kp.label),
      shape: "circle",
      color,
      text: stripTrailingPrice(kp.label),
    });
  }

  return { drawings: { segments, hlines, bands: [] }, markers };
}

/** 形态画线并入现有 drawings 通道；形态为空时原样返回 base（含 null）。 */
export function mergePatternDrawings(
  base: DrawingResult | null,
  pattern: DrawingResult,
): DrawingResult | null {
  const count = pattern.segments.length + pattern.hlines.length + pattern.bands.length;
  if (count === 0) return base;
  if (!base) return pattern;
  return {
    segments: [...base.segments, ...pattern.segments],
    hlines: [...base.hlines, ...pattern.hlines],
    bands: [...base.bands, ...pattern.bands],
  };
}

/** 形态触点并入现有 structMarkers 通道；形态为空时保持原值（undefined 不改变原行为）。 */
export function mergePatternMarkers(
  base: StructureMarker[] | null | undefined,
  pattern: StructureMarker[],
): StructureMarker[] | undefined {
  if (pattern.length === 0) return base ?? undefined;
  return [...(base ?? []), ...pattern];
}
