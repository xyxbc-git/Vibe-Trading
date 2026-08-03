// 形态 → 桌面图表渲染载荷（DrawingResult + StructureMarker）转换与合并测试。

import { describe, it, expect } from "vitest";
import { detectPatterns } from "../patterns";
import {
  patternToChartOverlay,
  mergePatternDrawings,
  mergePatternMarkers,
  PATTERN_COLORS,
} from "../patternOverlay";
import type { BaseData, DrawingResult } from "../drawings";
import type { StructureMarker } from "../../api/client";

// 与 patterns.test.ts 同款锚点造数器（dates 为 unix 秒字符串，与 Chart.tsx 口径一致）
function barsFromAnchors(anchors: [number, number][]): BaseData {
  const n = anchors[anchors.length - 1][0] + 1;
  const closes: number[] = [];
  for (let i = 0; i < n; i++) {
    let ai = 0;
    while (ai < anchors.length - 1 && i > anchors[ai + 1][0]) ai++;
    const [x0, y0] = anchors[ai];
    const [x1, y1] = anchors[Math.min(ai + 1, anchors.length - 1)];
    const t = x1 === x0 ? 0 : (i - x0) / (x1 - x0);
    closes.push(+(y0 + (y1 - y0) * t).toFixed(6));
  }
  return {
    dates: closes.map((_, i) => String(1_700_000_000 + i * 900)),
    closes,
    highs: closes.map(c => +(c * 1.001).toFixed(6)),
    lows: closes.map(c => +(c * 0.999).toFixed(6)),
  };
}

// 上升三角形样本（bullish，keyPoints 上下轨触点齐全）
const TRI_BASE = barsFromAnchors([
  [0, 104], [4, 100], [8, 110], [12, 103], [16, 110], [20, 106], [24, 110], [28, 108],
]);

describe("patternToChartOverlay", () => {
  it("maps boundaries → segments, key levels → labelled hlines, key points → markers", () => {
    const list = detectPatterns(TRI_BASE);
    expect(list.length).toBeGreaterThan(0);
    const p = list[0];
    const ov = patternToChartOverlay(p, TRI_BASE.dates);

    // 上下轨两条斜线段，方向色=看涨绿
    expect(ov.drawings.segments.length).toBe(p.boundaries.length);
    for (const seg of ov.drawings.segments) {
      expect(seg.color).toBe(PATTERN_COLORS[p.direction]);
      expect(seg.label).toContain(p.nameCn);
    }

    // 突破/量度目标/建议止损三条水平线，label 带中文与价格
    expect(ov.drawings.hlines).toHaveLength(3);
    const labels = ov.drawings.hlines.map(l => l.label ?? "").join("|");
    expect(labels).toContain("形态突破");
    expect(labels).toContain("量度目标");
    expect(labels).toContain("建议止损");

    // 不占用 bands 通道（矩形整理引擎的专属通道，避免混淆）
    expect(ov.drawings.bands).toEqual([]);

    // 触点 marker：数量与 keyPoints 一致，ts 映射为对应 bar 的 unix 秒
    expect(ov.markers).toHaveLength(p.keyPoints.length);
    for (let i = 0; i < ov.markers.length; i++) {
      const kp = p.keyPoints[i];
      expect(ov.markers[i].ts).toBe(Number(TRI_BASE.dates[kp.index]));
      expect(ov.markers[i].shape).toBe("circle");
      // marker 文案是去掉价格后的短标注（"上轨触点"/"下轨触点"）
      expect(ov.markers[i].text).not.toMatch(/[\d,]+\.?\d*$/);
    }

    // 上轨触点挂 K 线上方、下轨触点挂下方
    for (let i = 0; i < ov.markers.length; i++) {
      const kp = p.keyPoints[i];
      if (kp.label.startsWith("上轨")) expect(ov.markers[i].position).toBe("above");
      if (kp.label.startsWith("下轨")) expect(ov.markers[i].position).toBe("below");
    }
  });

  it("anchors reversal-family markers by structure (双底：底在下、颈线点在上)", () => {
    const base = barsFromAnchors([
      [0, 112], [6, 100], [12, 108], [18, 100.2], [24, 109], [27, 110.5],
    ]);
    const w = detectPatterns(base).find(p => p.type === "double_bottom");
    expect(w).toBeDefined();
    const ov = patternToChartOverlay(w!, base.dates);
    const byText = new Map(ov.markers.map(m => [m.text ?? "", m.position]));
    expect(byText.get("底1")).toBe("below");
    expect(byText.get("底2")).toBe("below");
    expect(byText.get("颈线点")).toBe("above");
  });

  it("returns an empty overlay for null pattern or empty dates", () => {
    const empty = patternToChartOverlay(null, TRI_BASE.dates);
    expect(empty.drawings.segments).toEqual([]);
    expect(empty.drawings.hlines).toEqual([]);
    expect(empty.markers).toEqual([]);

    const p = detectPatterns(TRI_BASE)[0];
    const noDates = patternToChartOverlay(p, []);
    expect(noDates.markers).toEqual([]);
  });

  it("skips markers when dates are not unix seconds (defensive)", () => {
    const p = detectPatterns(TRI_BASE)[0];
    const weirdDates = TRI_BASE.dates.map((_, i) => `2024-01-${String(i + 1).padStart(2, "0")}`);
    const ov = patternToChartOverlay(p, weirdDates);
    expect(ov.markers).toEqual([]); // NaN ts 全部跳过
    expect(ov.drawings.segments.length).toBeGreaterThan(0); // 画线不受影响
  });
});

describe("mergePatternDrawings / mergePatternMarkers", () => {
  const patternDrawings: DrawingResult = {
    segments: [{ i1: 0, p1: 1, i2: 5, p2: 2, color: "#fff", width: 1 }],
    hlines: [{ price: 100, color: "#fff", width: 1 }],
    bands: [],
  };
  const emptyDrawings: DrawingResult = { segments: [], hlines: [], bands: [] };
  const marker: StructureMarker = { ts: 1, position: "above", shape: "circle" };

  it("empty pattern payload keeps the base value untouched (null passthrough)", () => {
    expect(mergePatternDrawings(null, emptyDrawings)).toBeNull();
    const base: DrawingResult = { segments: [], hlines: [{ price: 1, color: "#000", width: 1 }], bands: [] };
    expect(mergePatternDrawings(base, emptyDrawings)).toBe(base);
    expect(mergePatternMarkers(undefined, [])).toBeUndefined();
    const baseMarkers = [marker];
    expect(mergePatternMarkers(baseMarkers, [])).toBe(baseMarkers);
  });

  it("merges pattern payload after the base payload", () => {
    const base: DrawingResult = {
      segments: [],
      hlines: [{ price: 1, color: "#000", width: 1 }],
      bands: [],
    };
    const merged = mergePatternDrawings(base, patternDrawings)!;
    expect(merged.hlines).toHaveLength(2);
    expect(merged.hlines[1].price).toBe(100);
    expect(merged.segments).toHaveLength(1);

    const mergedMarkers = mergePatternMarkers([marker], [{ ...marker, ts: 2 }])!;
    expect(mergedMarkers.map(m => m.ts)).toEqual([1, 2]);
  });

  it("uses the pattern payload alone when base is absent", () => {
    expect(mergePatternDrawings(null, patternDrawings)).toEqual(patternDrawings);
    expect(mergePatternMarkers(undefined, [marker])).toEqual([marker]);
  });
});
