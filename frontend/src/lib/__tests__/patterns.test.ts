import { describe, it, expect } from "vitest";
import { detectPatterns, patternsToOverlay, PATTERN_NAMES, type DetectedPattern } from "../patterns";
import type { BaseData } from "../drawings";

const COLORS = { bull: "#22c55e", bear: "#ef4444", neutral: "#f59e0b" };

// Build a deterministic OHLC series from price anchors [barIndex, close],
// linearly interpolated. Wicks are a thin ±0.1% band so detectSwings finds
// pivots exactly where the zigzag turns.
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
    dates: closes.map((_, i) => `2024-01-${String(i + 1).padStart(2, "0")} 00:00`),
    closes,
    highs: closes.map(c => +(c * 1.001).toFixed(6)),
    lows: closes.map(c => +(c * 0.999).toFixed(6)),
  };
}

function byType(list: DetectedPattern[], type: string): DetectedPattern | undefined {
  return list.find(p => p.type === type);
}

// ---------------------------------------------------------------------------
// 正样本：楔形 / 矩形 / 三角形 / 旗形 各一例
// ---------------------------------------------------------------------------

describe("detectPatterns positive samples", () => {
  it("detects a rising wedge (上升楔形, bearish)", () => {
    // 低点抬升快于高点 → 上行动能衰竭，收敛楔形。
    const base = barsFromAnchors([
      [0, 102], [4, 100], [8, 110], [12, 106], [16, 114], [20, 112], [24, 118], [28, 116],
    ]);
    const wedge = byType(detectPatterns(base), "wedge_rising");
    expect(wedge).toBeDefined();
    expect(wedge!.direction).toBe("bearish");
    expect(wedge!.nameCn).toBe(PATTERN_NAMES.wedge_rising);
    // 跌破下边界回楔形起点：目标应显著低于突破位上方的止损。
    expect(wedge!.target).toBeLessThan(wedge!.stop);
    expect(wedge!.summary).toContain("楔形");
    expect(wedge!.keyPoints.length).toBeGreaterThanOrEqual(3);
  });

  it("detects a rectangle (矩形箱体, neutral before breakout)", () => {
    // 高点持平 110、低点持平 100，末尾停在箱体中部。
    const base = barsFromAnchors([
      [0, 105], [4, 100], [8, 110], [12, 100], [16, 110], [20, 100], [24, 110], [28, 105],
    ]);
    const rect = byType(detectPatterns(base), "rectangle");
    expect(rect).toBeDefined();
    expect(rect!.direction).toBe("neutral");
    // 箱体上下轨在 ±0.1% 蜡烛影线范围内贴近 110 / 100。
    const top = Math.max(rect!.breakout, rect!.stop);
    const bottom = Math.min(rect!.breakout, rect!.stop);
    expect(top).toBeGreaterThan(108);
    expect(top).toBeLessThan(112);
    expect(bottom).toBeGreaterThan(98);
    expect(bottom).toBeLessThan(102);
    expect(rect!.summary).toContain("箱体");
  });

  it("detects an ascending triangle (上升三角形, bullish)", () => {
    // 高点被压在 110 一线，低点 100→103→106 不断抬高。
    const base = barsFromAnchors([
      [0, 104], [4, 100], [8, 110], [12, 103], [16, 110], [20, 106], [24, 110], [28, 108],
    ]);
    const tri = byType(detectPatterns(base), "triangle_ascending");
    expect(tri).toBeDefined();
    expect(tri!.direction).toBe("bullish");
    // 突破位≈平顶 110，目标 = 突破位 + 最宽处高度 > 突破位。
    expect(tri!.breakout).toBeGreaterThan(108);
    expect(tri!.target).toBeGreaterThan(tri!.breakout);
    expect(tri!.stop).toBeLessThan(tri!.breakout);
    expect(tri!.summary).toContain("低点不断抬高");
  });

  it("detects a bull flag after a steep pole (看涨旗形)", () => {
    // 0→8 旗杆 100→130（+30%），随后小幅下倾的平行旗面。
    const base = barsFromAnchors([
      [0, 100], [8, 130], [11, 127], [14, 128.5], [17, 125.5], [20, 127], [23, 124], [26, 125.5],
    ]);
    const list = detectPatterns(base);
    const flag = byType(list, "flag_bull") ?? byType(list, "pennant_bull");
    expect(flag).toBeDefined();
    expect(flag!.direction).toBe("bullish");
    // 量度目标 = 突破位 + 旗杆高度，应明显高于旗面。
    expect(flag!.target).toBeGreaterThan(flag!.breakout + 15);
    expect(flag!.stop).toBeLessThan(flag!.breakout);
    expect(flag!.summary).toContain("旗杆");
  });

  it("detects a falling wedge (下降楔形, bullish)", () => {
    // 高点降得快于低点 → 抛压衰竭，收敛下倾楔形；末尾已上破上边界。
    const base = barsFromAnchors([
      [0, 118], [4, 120], [8, 110], [12, 114], [16, 106], [20, 108], [24, 102], [28, 104],
    ]);
    const wedge = byType(detectPatterns(base), "wedge_falling");
    expect(wedge).toBeDefined();
    expect(wedge!.direction).toBe("bullish");
    expect(wedge!.nameCn).toBe(PATTERN_NAMES.wedge_falling);
    // 量度目标回楔形起点，应高于突破位；止损在下边界，应低于突破位。
    expect(wedge!.target).toBeGreaterThan(wedge!.breakout);
    expect(wedge!.stop).toBeLessThan(wedge!.breakout);
    expect(wedge!.summary).toContain("楔形");
    expect(wedge!.keyPoints.length).toBeGreaterThanOrEqual(3);
    // 上下轨端点：上轨向下倾斜（y0 > y1），下轨同向。
    const upper = wedge!.boundaries.find(b => b.kind === "upper")!;
    const lower = wedge!.boundaries.find(b => b.kind === "lower")!;
    expect(upper.y0).toBeGreaterThan(upper.y1);
    expect(lower.y0).toBeGreaterThan(lower.y1);
  });

  it("detects a descending triangle (下降三角形, bearish)", () => {
    // 低点撑平 100 一线，高点 110→107→104 不断降低。
    const base = barsFromAnchors([
      [0, 106], [4, 110], [8, 100], [12, 107], [16, 100], [20, 104], [24, 100], [28, 102],
    ]);
    const tri = byType(detectPatterns(base), "triangle_descending");
    expect(tri).toBeDefined();
    expect(tri!.direction).toBe("bearish");
    expect(tri!.nameCn).toBe(PATTERN_NAMES.triangle_descending);
    // 突破位≈平底 100，目标 = 突破位 - 最宽处高度 < 突破位，止损在下降上轨。
    expect(tri!.breakout).toBeGreaterThan(98);
    expect(tri!.breakout).toBeLessThan(102);
    expect(tri!.target).toBeLessThan(tri!.breakout);
    expect(tri!.stop).toBeGreaterThan(tri!.breakout);
    expect(tri!.summary).toContain("高点不断降低");
  });

  it("detects a symmetric triangle (对称三角形, neutral before breakout)", () => {
    // 高点 112→110→108 降低、低点 100→102→104 抬高，同步向中间收敛；
    // 入场前无明显趋势、末尾停在三角形内部 → 方向待突破。
    const base = barsFromAnchors([
      [0, 101], [4, 100], [8, 112], [12, 102], [16, 110], [20, 104], [24, 108], [28, 106.5],
    ]);
    const tri = byType(detectPatterns(base), "triangle_symmetric");
    expect(tri).toBeDefined();
    expect(tri!.direction).toBe("neutral");
    expect(tri!.nameCn).toBe(PATTERN_NAMES.triangle_symmetric);
    // 中性时默认按向上突破给出量度：目标 > 突破位 > 止损。
    expect(tri!.target).toBeGreaterThan(tri!.breakout);
    expect(tri!.stop).toBeLessThan(tri!.breakout);
    expect(tri!.summary).toContain("对称三角形");
    // 上下轨触点应两侧都有（收敛需要两侧多次确认）。
    expect(tri!.keyPoints.length).toBeGreaterThanOrEqual(4);
    const upper = tri!.boundaries.find(b => b.kind === "upper")!;
    const lower = tri!.boundaries.find(b => b.kind === "lower")!;
    expect(upper.y0).toBeGreaterThan(upper.y1); // 上轨下倾
    expect(lower.y0).toBeLessThan(lower.y1); // 下轨上倾
  });

  it("detects a bear flag after a steep drop (看跌旗形)", () => {
    // 0→8 旗杆 130→100（-23%），随后小幅上倾的平行旗面反抽。
    const base = barsFromAnchors([
      [0, 130], [8, 100], [11, 103], [14, 101.5], [17, 104.5], [20, 103], [23, 106], [26, 104.5],
    ]);
    const list = detectPatterns(base);
    const flag = byType(list, "flag_bear") ?? byType(list, "pennant_bear");
    expect(flag).toBeDefined();
    expect(flag!.direction).toBe("bearish");
    // 量度目标 = 突破位 - 旗杆高度，应明显低于旗面；止损在旗面上沿。
    expect(flag!.target).toBeLessThan(flag!.breakout - 15);
    expect(flag!.stop).toBeGreaterThan(flag!.breakout);
    expect(flag!.summary).toContain("旗杆");
  });
});

// ---------------------------------------------------------------------------
// 加分项：双底
// ---------------------------------------------------------------------------

describe("detectPatterns bonus reversal patterns", () => {
  it("detects a confirmed double bottom (W底, bullish)", () => {
    // 两次探底 100 附近获支撑，中间反弹到 108（颈线），末尾突破颈线站上 110。
    const base = barsFromAnchors([
      [0, 112], [6, 100], [12, 108], [18, 100.2], [24, 109], [27, 110.5],
    ]);
    const w = byType(detectPatterns(base), "double_bottom");
    expect(w).toBeDefined();
    expect(w!.direction).toBe("bullish");
    // 颈线≈108，量度目标≈颈线+底深，止损≈双底低点。
    expect(w!.breakout).toBeGreaterThan(106);
    expect(w!.breakout).toBeLessThan(110);
    expect(w!.target).toBeGreaterThan(w!.breakout);
    expect(w!.stop).toBeLessThan(102);
    expect(w!.keyPoints.map(k => k.label).join()).toContain("颈线点");
    expect(w!.summary).toContain("W 底");
  });
});

// ---------------------------------------------------------------------------
// 负样本：同类数据但不满足判定要件 → 不得误报
// ---------------------------------------------------------------------------

describe("detectPatterns negative samples", () => {
  it("a parallel rising channel is NOT a wedge / rectangle / triangle", () => {
    // 高低点等速抬升（平行通道），既不收敛也不走平。
    const base = barsFromAnchors([
      [0, 103], [4, 100], [8, 110], [12, 104], [16, 114], [20, 108], [24, 118], [28, 114],
    ]);
    const list = detectPatterns(base);
    expect(byType(list, "wedge_rising")).toBeUndefined();
    expect(byType(list, "wedge_falling")).toBeUndefined();
    expect(byType(list, "rectangle")).toBeUndefined();
    expect(byType(list, "triangle_ascending")).toBeUndefined();
    expect(byType(list, "triangle_descending")).toBeUndefined();
    expect(byType(list, "triangle_symmetric")).toBeUndefined();
  });

  it("a drifting consolidation WITHOUT a pole is NOT a flag/pennant", () => {
    // 与看涨旗形同样的下倾旗面，但前面没有旗杆式拉升。
    const base = barsFromAnchors([
      [0, 130], [3, 127], [6, 128.5], [9, 125.5], [12, 127], [15, 124], [18, 125.5], [21, 122.5], [24, 124],
    ]);
    const list = detectPatterns(base);
    expect(byType(list, "flag_bull")).toBeUndefined();
    expect(byType(list, "pennant_bull")).toBeUndefined();
    expect(byType(list, "flag_bear")).toBeUndefined();
    expect(byType(list, "pennant_bear")).toBeUndefined();
  });

  it("a smooth monotonic trend with no swings yields no patterns", () => {
    const base = barsFromAnchors([[0, 100], [40, 140]]);
    expect(detectPatterns(base)).toEqual([]);
  });

  it("returns [] for series shorter than 20 bars", () => {
    const base = barsFromAnchors([[0, 100], [10, 110]]);
    expect(detectPatterns(base)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// overlay 输出结构
// ---------------------------------------------------------------------------

describe("patternsToOverlay", () => {
  it("emits boundary lines, key levels, area fill and key points for the top pattern", () => {
    const base = barsFromAnchors([
      [0, 104], [4, 100], [8, 110], [12, 103], [16, 110], [20, 106], [24, 110], [28, 108],
    ]);
    const list = detectPatterns(base);
    expect(list.length).toBeGreaterThan(0);
    const ov = patternsToOverlay(list, base.dates, COLORS);
    // 上下边界(2) + 突破/目标/止损水平线(3)
    expect(ov.lines.length).toBeGreaterThanOrEqual(5);
    expect(ov.areas.length).toBe(1);
    expect(ov.points.length).toBe(list[0].keyPoints.length);
    const flat = JSON.stringify(ov.lines);
    expect(flat).toContain("突破位");
    expect(flat).toContain("目标位");
    expect(flat).toContain("止损位");
  });

  it("returns an empty overlay when nothing is detected", () => {
    const ov = patternsToOverlay([], ["2024-01-01"], COLORS);
    expect(ov.lines).toEqual([]);
    expect(ov.areas).toEqual([]);
    expect(ov.points).toEqual([]);
  });
});
