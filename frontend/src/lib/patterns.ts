// Classic chart-pattern recognition for candlestick charts.
// Pure functions in the same spirit as drawings.ts: take OHLC arrays, return
// structured pattern descriptions + echarts markLine/markArea/markPoint data.
//
// Supported patterns (判定要件提炼自《K线起涨形态》参考图 01-06)：
//   必做：楔形（上升/下降）、矩形（箱体）、旗形/三角旗、三角形（上升/下降/对称）
//   加分：头肩顶/头肩底、双顶/双底（W底）
//
// Every detected pattern explains its key levels（触点/边界线/颈线/突破位/
// 量度目标位/建议止损位，带价格数字）and an explicit bullish/bearish call with
// a plain-language one-liner, so beginners can read it at a glance.

import { detectSwings, type BaseData, type Swing } from "./drawings";

export type PatternType =
  | "wedge_rising"
  | "wedge_falling"
  | "rectangle"
  | "flag_bull"
  | "flag_bear"
  | "pennant_bull"
  | "pennant_bear"
  | "triangle_ascending"
  | "triangle_descending"
  | "triangle_symmetric"
  | "double_top"
  | "double_bottom"
  | "head_shoulders_top"
  | "head_shoulders_bottom";

export type PatternDirection = "bullish" | "bearish" | "neutral";

export interface PatternKeyPoint {
  index: number; // bar index
  ts: string; // dates[index]
  price: number;
  label: string; // e.g. 触点1 / 左肩 / 颈线点
  note: string; // plain-language explanation
}

export interface PatternBoundary {
  kind: "upper" | "lower" | "neckline";
  label: string; // 上边界 / 下边界 / 颈线
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface DetectedPattern {
  type: PatternType;
  nameCn: string;
  direction: PatternDirection;
  startIndex: number;
  endIndex: number;
  keyPoints: PatternKeyPoint[];
  boundaries: PatternBoundary[];
  breakout: number; // 突破位
  target: number; // 量度目标位（横有多长竖有多高 / 旗杆等高 / 颈线量度）
  stop: number; // 建议止损位
  confidence: number; // 0..1
  summary: string; // 一句话通俗解读（小白友好）
}

export interface PatternOptions {
  lookback?: number; // swing fractal lookback
  windowBars?: number; // how many recent bars to scan
  maxPatterns?: number;
}

const DEFAULTS: Required<PatternOptions> = {
  lookback: 3,
  windowBars: 120,
  maxPatterns: 3,
};

export const PATTERN_NAMES: Record<PatternType, string> = {
  wedge_rising: "上升楔形",
  wedge_falling: "下降楔形",
  rectangle: "矩形整理（箱体）",
  flag_bull: "看涨旗形",
  flag_bear: "看跌旗形",
  pennant_bull: "看涨三角旗",
  pennant_bear: "看跌三角旗",
  triangle_ascending: "上升三角形",
  triangle_descending: "下降三角形",
  triangle_symmetric: "对称三角形",
  double_top: "双顶（M头）",
  double_bottom: "双底（W底）",
  head_shoulders_top: "头肩顶",
  head_shoulders_bottom: "头肩底",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface LineFit {
  slope: number; // price per bar (absolute index space)
  intercept: number; // price at bar index 0
}

function fitThroughPivots(pivots: Swing[]): LineFit {
  const m = pivots.length;
  if (m === 1) return { slope: 0, intercept: pivots[0].price };
  let sx = 0;
  let sy = 0;
  let sxx = 0;
  let sxy = 0;
  for (const p of pivots) {
    sx += p.index;
    sy += p.price;
    sxx += p.index * p.index;
    sxy += p.index * p.price;
  }
  const denom = m * sxx - sx * sx;
  const slope = denom === 0 ? 0 : (m * sxy - sx * sy) / denom;
  const intercept = (sy - slope * sx) / m;
  return { slope, intercept };
}

function at(f: LineFit, i: number): number {
  return f.slope * i + f.intercept;
}

export function fmtPrice(v: number): string {
  const abs = Math.abs(v);
  const digits = abs >= 1000 ? 2 : abs >= 10 ? 2 : abs >= 0.1 ? 4 : 6;
  return v.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function clamp01(x: number): number {
  return x < 0 ? 0 : x > 1 ? 1 : x;
}

function dirLabel(d: PatternDirection): string {
  return d === "bullish" ? "看涨" : d === "bearish" ? "看跌" : "方向待突破";
}

// Count how many pivots sit close to the fitted boundary (touch quality).
function countTouches(f: LineFit, pivots: Swing[], tol: number): number {
  let c = 0;
  for (const p of pivots) if (Math.abs(p.price - at(f, p.index)) <= tol) c++;
  return c;
}

function touchPoints(
  f: LineFit,
  pivots: Swing[],
  tol: number,
  dates: string[],
  side: "上" | "下",
): PatternKeyPoint[] {
  const pts: PatternKeyPoint[] = [];
  for (const p of pivots) {
    if (Math.abs(p.price - at(f, p.index)) <= tol) {
      pts.push({
        index: p.index,
        ts: dates[p.index],
        price: +p.price.toFixed(8),
        label: `${side}轨触点 ${fmtPrice(p.price)}`,
        note: `价格第 ${pts.length + 1} 次触碰${side}边界后折返，边界有效性 +1`,
      });
    }
  }
  return pts;
}

// ---------------------------------------------------------------------------
// Consolidation family: rectangle / triangles / wedges (+ flag/pennant pole)
// ---------------------------------------------------------------------------

interface BoundaryAnalysis {
  his: Swing[];
  los: Swing[];
  upper: LineFit;
  lower: LineFit;
  startIndex: number;
  endIndex: number;
  gapStart: number; // vertical distance between boundaries at pattern start
  gapEnd: number; // ... at pattern end
  snormU: number; // upper slope normalised: range fraction traversed per window
  snormL: number;
  tol: number; // touch tolerance in price units
}

function analyseBoundaries(swings: Swing[], endIndex: number): BoundaryAnalysis | null {
  const his = swings.filter(s => s.type === "high").slice(-4);
  const los = swings.filter(s => s.type === "low").slice(-4);
  if (his.length < 2 || los.length < 2) return null;

  const startIndex = Math.min(his[0].index, los[0].index);
  const span = endIndex - startIndex;
  if (span < 6) return null;

  const upper = fitThroughPivots(his);
  const lower = fitThroughPivots(los);
  const gapStart = at(upper, startIndex) - at(lower, startIndex);
  const gapEnd = at(upper, endIndex) - at(lower, endIndex);
  if (gapStart <= 0) return null;

  // Normalise slopes: how much of the pattern's own height each boundary
  // traverses across the pattern's span. Scale-free, works for BTC or pennies.
  const height = Math.max(gapStart, gapEnd);
  const snormU = (upper.slope * span) / height;
  const snormL = (lower.slope * span) / height;
  const tol = height * 0.22;

  return { his, los, upper, lower, startIndex, endIndex, gapStart, gapEnd, snormU, snormL, tol };
}

const FLAT = 0.35; // |snorm| below this = 横向（边界基本走平）

// Prior trend heading into the pattern (for symmetric triangle / rectangle bias).
function priorTrend(base: BaseData, startIndex: number): PatternDirection {
  const back = Math.max(0, startIndex - 25);
  const before = base.closes[back];
  const enter = base.closes[startIndex];
  if (!before || !enter) return "neutral";
  const move = (enter - before) / before;
  if (move > 0.02) return "bullish";
  if (move < -0.02) return "bearish";
  return "neutral";
}

// Flag pole: strong one-way move ending where the consolidation begins.
// Returns signed pole height in price units (positive = up pole).
function detectPole(base: BaseData, startIndex: number, patternHeight: number): number {
  const back = Math.max(0, startIndex - 20);
  if (startIndex - back < 3) return 0;
  const lowsSeg = base.lows.slice(back, startIndex + 1);
  const highsSeg = base.highs.slice(back, startIndex + 1);
  const enter = base.closes[startIndex];
  const upPole = enter - Math.min(...lowsSeg);
  const downPole = Math.max(...highsSeg) - enter;
  const price = enter || 1;
  // Pole must dwarf the consolidation (旗面) and be a meaningful % move.
  if (upPole >= 2 * patternHeight && upPole / price >= 0.025 && upPole > downPole) return upPole;
  if (downPole >= 2 * patternHeight && downPole / price >= 0.025 && downPole > upPole) return -downPole;
  return 0;
}

function buildConsolidation(base: BaseData, b: BoundaryAnalysis): DetectedPattern | null {
  const { dates, closes } = base;
  const { upper, lower, startIndex, endIndex, gapStart, gapEnd, snormU, snormL, tol } = b;
  const lastClose = closes[endIndex];
  const converging = gapEnd < 0.72 * gapStart;
  const parallel = gapEnd >= 0.72 * gapStart && gapEnd <= 1.35 * gapStart;

  const upFlat = Math.abs(snormU) < FLAT;
  const loFlat = Math.abs(snormL) < FLAT;
  const upRising = snormU >= FLAT;
  const upFalling = snormU <= -FLAT;
  const loRising = snormL >= FLAT;
  const loFalling = snormL <= -FLAT;

  const uEnd = at(upper, endIndex);
  const lEnd = at(lower, endIndex);
  const height = gapStart;

  const touches = countTouches(upper, b.his, tol) + countTouches(lower, b.los, tol);
  const baseConf = clamp01(0.5 + 0.06 * Math.max(0, touches - 3));

  const keyPoints = [
    ...touchPoints(upper, b.his, tol, dates, "上"),
    ...touchPoints(lower, b.los, tol, dates, "下"),
  ];

  const boundaries: PatternBoundary[] = [
    { kind: "upper", label: "上边界", x0: startIndex, y0: at(upper, startIndex), x1: endIndex, y1: uEnd },
    { kind: "lower", label: "下边界", x0: startIndex, y0: at(lower, startIndex), x1: endIndex, y1: lEnd },
  ];

  const mk = (
    type: PatternType,
    direction: PatternDirection,
    breakout: number,
    target: number,
    stop: number,
    confBonus: number,
    summary: string,
  ): DetectedPattern => ({
    type,
    nameCn: PATTERN_NAMES[type],
    direction,
    startIndex,
    endIndex,
    keyPoints,
    boundaries,
    breakout: +breakout.toFixed(8),
    target: +target.toFixed(8),
    stop: +stop.toFixed(8),
    confidence: clamp01(baseConf + confBonus),
    summary,
  });

  // ---- Flag / pennant first: a dominant pole re-labels the consolidation as
  // a continuation pattern (量度目标 = 旗杆等高).
  const pole = detectPole(base, startIndex, height);
  const shortEnough = endIndex - startIndex <= 30;
  if (pole !== 0 && shortEnough && (parallel || converging)) {
    if (pole > 0 && snormU <= FLAT && snormL <= FLAT) {
      // 拉升后小幅横盘/回落整理 → 看涨中继
      const type: PatternType = converging ? "pennant_bull" : "flag_bull";
      const breakout = uEnd;
      const target = breakout + pole;
      const stop = Math.min(lEnd, at(lower, startIndex));
      const confirmed = lastClose > breakout ? 0.15 : 0;
      return mk(
        type,
        "bullish",
        breakout,
        target,
        stop,
        0.1 + confirmed,
        `旗杆式拉升后${converging ? "收敛三角旗" : "平行旗面"}整理，是典型的中继上涨蓄力；放量突破 ${fmtPrice(breakout)} 后按旗杆等高看向 ${fmtPrice(target)}，跌破旗面下沿 ${fmtPrice(stop)} 止损。`,
      );
    }
    if (pole < 0 && snormU >= -FLAT && snormL >= -FLAT) {
      const type: PatternType = converging ? "pennant_bear" : "flag_bear";
      const breakout = lEnd;
      const target = breakout + pole; // pole 为负 → 向下量度
      const stop = Math.max(uEnd, at(upper, startIndex));
      const confirmed = lastClose < breakout ? 0.15 : 0;
      return mk(
        type,
        "bearish",
        breakout,
        target,
        stop,
        0.1 + confirmed,
        `急跌后${converging ? "收敛三角旗" : "平行旗面"}反抽整理，是典型的中继下跌；跌破 ${fmtPrice(breakout)} 后按旗杆等高看向 ${fmtPrice(target)}，站上旗面上沿 ${fmtPrice(stop)} 止损。`,
      );
    }
  }

  // ---- Rectangle: 高点持平 + 低点持平，横有多长竖有多高。
  if (upFlat && loFlat && parallel) {
    const top = (at(upper, startIndex) + uEnd) / 2;
    const bottom = (at(lower, startIndex) + lEnd) / 2;
    const boxH = top - bottom;
    if (lastClose > top + tol * 0.5) {
      return mk("rectangle", "bullish", top, top + boxH, bottom, 0.15,
        `箱体横盘后已向上突破上轨 ${fmtPrice(top)}，按“横有多长竖有多高”量度看向 ${fmtPrice(top + boxH)}，回落跌破下轨 ${fmtPrice(bottom)} 止损。`);
    }
    if (lastClose < bottom - tol * 0.5) {
      return mk("rectangle", "bearish", bottom, bottom - boxH, top, 0.15,
        `箱体横盘后已跌破下轨 ${fmtPrice(bottom)}，量度目标 ${fmtPrice(bottom - boxH)}，收回箱体并站上上轨 ${fmtPrice(top)} 止损。`);
    }
    const bias = priorTrend(base, startIndex);
    const breakout = bias === "bearish" ? bottom : top;
    const target = bias === "bearish" ? bottom - boxH : top + boxH;
    const stop = bias === "bearish" ? top : bottom;
    return mk("rectangle", "neutral", breakout, target, stop, 0,
      `价格被夹在 ${fmtPrice(bottom)} ~ ${fmtPrice(top)} 的箱体里震荡蓄力，方向待突破：向上站稳 ${fmtPrice(top)} 看多、向下跌破 ${fmtPrice(bottom)} 看空，量度目标为一个箱体高度（${fmtPrice(boxH)}）。`);
  }

  // ---- Ascending triangle: 高点持平 + 低点抬高 → 看涨。
  if (upFlat && loRising) {
    const top = (at(upper, startIndex) + uEnd) / 2;
    const confirmed = lastClose > top + tol * 0.5 ? 0.15 : 0;
    return mk("triangle_ascending", "bullish", top, top + height, lEnd, 0.08 + confirmed,
      `高点被压在 ${fmtPrice(top)} 一线但低点不断抬高，买方力量持续增强；${confirmed ? "已" : "待"}突破 ${fmtPrice(top)} 后按最宽处等高看向 ${fmtPrice(top + height)}，跌破上升下轨 ${fmtPrice(lEnd)} 止损。`);
  }

  // ---- Descending triangle: 低点持平 + 高点降低 → 看跌。
  if (loFlat && upFalling) {
    const bottom = (at(lower, startIndex) + lEnd) / 2;
    const confirmed = lastClose < bottom - tol * 0.5 ? 0.15 : 0;
    return mk("triangle_descending", "bearish", bottom, bottom - height, uEnd, 0.08 + confirmed,
      `低点撑在 ${fmtPrice(bottom)} 一线但高点不断降低，卖压逐步占优；${confirmed ? "已" : "若"}跌破 ${fmtPrice(bottom)} 按最宽处等高看向 ${fmtPrice(bottom - height)}，站上下降上轨 ${fmtPrice(uEnd)} 止损。`);
  }

  // ---- Symmetric triangle: 高点降低 + 低点抬高，方向随前趋势/突破。
  if (upFalling && loRising && converging) {
    const bias = priorTrend(base, startIndex);
    let direction: PatternDirection = bias;
    if (lastClose > uEnd + tol * 0.5) direction = "bullish";
    else if (lastClose < lEnd - tol * 0.5) direction = "bearish";
    const bullish = direction !== "bearish";
    const breakout = bullish ? uEnd : lEnd;
    const target = bullish ? breakout + height : breakout - height;
    const stop = bullish ? lEnd : uEnd;
    return mk("triangle_symmetric", direction, breakout, target, stop, direction === "neutral" ? 0 : 0.08,
      `高低点同时向中间收敛成对称三角形，多空僵持、波动被压缩；${direction === "neutral" ? "方向待选择，" : `结合${bias === "bearish" ? "前跌势" : "前涨势"}偏${dirLabel(direction)}，`}${bullish ? "向上突破" : "向下跌破"} ${fmtPrice(breakout)} 后按最宽处等高看向 ${fmtPrice(target)}，反向越过 ${fmtPrice(stop)} 止损。`);
  }

  // ---- Rising wedge: 两条边界同向上但收敛 → 经典看跌（上涨动能衰竭）。
  if (upRising && loRising && converging && snormL > snormU) {
    const breakout = lEnd;
    const target = at(lower, startIndex); // 量度：回到楔形起点
    const stop = uEnd;
    const confirmed = lastClose < breakout - tol * 0.5 ? 0.15 : 0;
    return mk("wedge_rising", "bearish", breakout, target, stop, 0.05 + confirmed,
      `价格在向上倾斜且不断收窄的楔形里爬升，低点抬得比高点快、上涨动能在衰竭；${confirmed ? "已" : "若"}跌破下边界 ${fmtPrice(breakout)}，通常回到楔形起点 ${fmtPrice(target)}，站上上边界 ${fmtPrice(stop)} 止损。`);
  }

  // ---- Falling wedge: 两条边界同向下但收敛 → 经典看涨（下跌动能衰竭）。
  if (upFalling && loFalling && converging && snormU < snormL) {
    const breakout = uEnd;
    const target = at(upper, startIndex); // 量度：回到楔形起点
    const stop = lEnd;
    const confirmed = lastClose > breakout + tol * 0.5 ? 0.15 : 0;
    return mk("wedge_falling", "bullish", breakout, target, stop, 0.05 + confirmed,
      `价格在向下倾斜且不断收窄的楔形里阴跌，高点降得比低点快、抛压在衰竭；${confirmed ? "已" : "若"}向上突破上边界 ${fmtPrice(breakout)}，通常修复到楔形起点 ${fmtPrice(target)}，跌破下边界 ${fmtPrice(stop)} 止损。`);
  }

  return null;
}

// ---------------------------------------------------------------------------
// Reversal family: double top/bottom, head & shoulders
// ---------------------------------------------------------------------------

function buildDoubleTopBottom(base: BaseData, swings: Swing[], endIndex: number): DetectedPattern | null {
  const { dates, closes, highs, lows } = base;
  const n = closes.length;
  const range = Math.max(...highs.slice(Math.max(0, n - 120))) - Math.min(...lows.slice(Math.max(0, n - 120))) || 1;
  const tol = range * 0.06;
  const lastClose = closes[endIndex];

  // Walk the most recent pivot triplets: high-low-high (双顶) / low-high-low (双底).
  for (let i = swings.length - 1; i >= 2; i--) {
    const c = swings[i];
    const m = swings[i - 1];
    const a = swings[i - 2];
    // 双顶：两个几乎等高的峰，中间夹一个颈线低点
    if (a.type === "high" && m.type === "low" && c.type === "high" && Math.abs(a.price - c.price) <= tol) {
      const peak = Math.max(a.price, c.price);
      const neck = m.price;
      const h = peak - neck;
      if (h < range * 0.12) continue; // 峰太浅，噪音
      const confirmed = lastClose < neck - tol * 0.3;
      const summary = `价格两次冲高到 ${fmtPrice(peak)} 附近都被打回，形成 M 头；${confirmed ? "已" : "若"}跌破颈线 ${fmtPrice(neck)}，按双顶高度量度看向 ${fmtPrice(neck - h)}，重新站上 ${fmtPrice(peak)} 止损。`;
      return {
        type: "double_top",
        nameCn: PATTERN_NAMES.double_top,
        direction: "bearish",
        startIndex: a.index,
        endIndex,
        keyPoints: [
          { index: a.index, ts: dates[a.index], price: a.price, label: `顶1 ${fmtPrice(a.price)}`, note: "第一次冲高受阻回落" },
          { index: m.index, ts: dates[m.index], price: m.price, label: `颈线点 ${fmtPrice(m.price)}`, note: "两顶之间的回撤低点，跌破即确认双顶" },
          { index: c.index, ts: dates[c.index], price: c.price, label: `顶2 ${fmtPrice(c.price)}`, note: "第二次冲高再次失败，与顶1几乎等高" },
        ],
        boundaries: [
          { kind: "neckline", label: "颈线", x0: a.index, y0: neck, x1: endIndex, y1: neck },
        ],
        breakout: +neck.toFixed(8),
        target: +(neck - h).toFixed(8),
        stop: +peak.toFixed(8),
        confidence: clamp01(0.55 + (confirmed ? 0.2 : 0) + (1 - Math.abs(a.price - c.price) / tol) * 0.1),
        summary,
      };
    }
    // 双底：两个几乎等低的谷，中间夹一个颈线高点
    if (a.type === "low" && m.type === "high" && c.type === "low" && Math.abs(a.price - c.price) <= tol) {
      const trough = Math.min(a.price, c.price);
      const neck = m.price;
      const h = neck - trough;
      if (h < range * 0.12) continue;
      const confirmed = lastClose > neck + tol * 0.3;
      const summary = `价格两次回踩 ${fmtPrice(trough)} 附近都获得支撑，形成 W 底；${confirmed ? "已" : "若"}突破颈线 ${fmtPrice(neck)}，按双底高度量度看向 ${fmtPrice(neck + h)}，跌破 ${fmtPrice(trough)} 止损。`;
      return {
        type: "double_bottom",
        nameCn: PATTERN_NAMES.double_bottom,
        direction: "bullish",
        startIndex: a.index,
        endIndex,
        keyPoints: [
          { index: a.index, ts: dates[a.index], price: a.price, label: `底1 ${fmtPrice(a.price)}`, note: "第一次探底获得支撑" },
          { index: m.index, ts: dates[m.index], price: m.price, label: `颈线点 ${fmtPrice(m.price)}`, note: "两底之间的反弹高点，突破即确认双底" },
          { index: c.index, ts: dates[c.index], price: c.price, label: `底2 ${fmtPrice(c.price)}`, note: "第二次探底不破前低，与底1几乎等低" },
        ],
        boundaries: [
          { kind: "neckline", label: "颈线", x0: a.index, y0: neck, x1: endIndex, y1: neck },
        ],
        breakout: +neck.toFixed(8),
        target: +(neck + h).toFixed(8),
        stop: +trough.toFixed(8),
        confidence: clamp01(0.55 + (confirmed ? 0.2 : 0) + (1 - Math.abs(a.price - c.price) / tol) * 0.1),
        summary,
      };
    }
  }
  return null;
}

function buildHeadShoulders(base: BaseData, swings: Swing[], endIndex: number): DetectedPattern | null {
  const { dates, closes, highs, lows } = base;
  const n = closes.length;
  const range = Math.max(...highs.slice(Math.max(0, n - 120))) - Math.min(...lows.slice(Math.max(0, n - 120))) || 1;
  const tol = range * 0.08;
  const lastClose = closes[endIndex];

  // Need 5 alternating pivots: 肩-谷-头-谷-肩（顶）或镜像（底）。
  for (let i = swings.length - 1; i >= 4; i--) {
    const [s1, v1, hd, v2, s2] = [swings[i - 4], swings[i - 3], swings[i - 2], swings[i - 1], swings[i]];
    // 头肩顶
    if (
      s1.type === "high" && v1.type === "low" && hd.type === "high" && v2.type === "low" && s2.type === "high" &&
      hd.price > s1.price + tol * 0.4 && hd.price > s2.price + tol * 0.4 &&
      Math.abs(s1.price - s2.price) <= tol
    ) {
      const neckFit = fitThroughPivots([v1, v2]);
      const neckEnd = at(neckFit, endIndex);
      const h = hd.price - (v1.price + v2.price) / 2;
      const confirmed = lastClose < neckEnd - tol * 0.3;
      return {
        type: "head_shoulders_top",
        nameCn: PATTERN_NAMES.head_shoulders_top,
        direction: "bearish",
        startIndex: s1.index,
        endIndex,
        keyPoints: [
          { index: s1.index, ts: dates[s1.index], price: s1.price, label: `左肩 ${fmtPrice(s1.price)}`, note: "第一波冲高" },
          { index: v1.index, ts: dates[v1.index], price: v1.price, label: `颈线点1 ${fmtPrice(v1.price)}`, note: "左肩回撤低点" },
          { index: hd.index, ts: dates[hd.index], price: hd.price, label: `头部 ${fmtPrice(hd.price)}`, note: "最高点：最后的冲锋" },
          { index: v2.index, ts: dates[v2.index], price: v2.price, label: `颈线点2 ${fmtPrice(v2.price)}`, note: "头部回撤低点" },
          { index: s2.index, ts: dates[s2.index], price: s2.price, label: `右肩 ${fmtPrice(s2.price)}`, note: "反弹乏力，高度只到左肩附近" },
        ],
        boundaries: [
          { kind: "neckline", label: "颈线", x0: v1.index, y0: v1.price, x1: endIndex, y1: neckEnd },
        ],
        breakout: +neckEnd.toFixed(8),
        target: +(neckEnd - h).toFixed(8),
        stop: +s2.price.toFixed(8),
        confidence: clamp01(0.6 + (confirmed ? 0.2 : 0)),
        summary: `左肩-头-右肩三段冲高、头部最高且右肩明显乏力，是强烈的顶部反转结构；${confirmed ? "已" : "若"}跌破颈线 ${fmtPrice(neckEnd)}，按头到颈线的高度量度看向 ${fmtPrice(neckEnd - h)}，站回右肩 ${fmtPrice(s2.price)} 止损。`,
      };
    }
    // 头肩底
    if (
      s1.type === "low" && v1.type === "high" && hd.type === "low" && v2.type === "high" && s2.type === "low" &&
      hd.price < s1.price - tol * 0.4 && hd.price < s2.price - tol * 0.4 &&
      Math.abs(s1.price - s2.price) <= tol
    ) {
      const neckFit = fitThroughPivots([v1, v2]);
      const neckEnd = at(neckFit, endIndex);
      const h = (v1.price + v2.price) / 2 - hd.price;
      const confirmed = lastClose > neckEnd + tol * 0.3;
      return {
        type: "head_shoulders_bottom",
        nameCn: PATTERN_NAMES.head_shoulders_bottom,
        direction: "bullish",
        startIndex: s1.index,
        endIndex,
        keyPoints: [
          { index: s1.index, ts: dates[s1.index], price: s1.price, label: `左肩 ${fmtPrice(s1.price)}`, note: "第一波探底" },
          { index: v1.index, ts: dates[v1.index], price: v1.price, label: `颈线点1 ${fmtPrice(v1.price)}`, note: "左肩反弹高点" },
          { index: hd.index, ts: dates[hd.index], price: hd.price, label: `头部 ${fmtPrice(hd.price)}`, note: "最低点：恐慌性杀跌" },
          { index: v2.index, ts: dates[v2.index], price: v2.price, label: `颈线点2 ${fmtPrice(v2.price)}`, note: "头部反弹高点" },
          { index: s2.index, ts: dates[s2.index], price: s2.price, label: `右肩 ${fmtPrice(s2.price)}`, note: "再跌不破头部，抛压枯竭" },
        ],
        boundaries: [
          { kind: "neckline", label: "颈线", x0: v1.index, y0: v1.price, x1: endIndex, y1: neckEnd },
        ],
        breakout: +neckEnd.toFixed(8),
        target: +(neckEnd + h).toFixed(8),
        stop: +s2.price.toFixed(8),
        confidence: clamp01(0.6 + (confirmed ? 0.2 : 0)),
        summary: `左肩-头-右肩三段探底、头部最低且右肩不再创新低，市场情绪从恐慌逐步回暖，是极强的底部反转结构；${confirmed ? "已" : "若"}突破颈线 ${fmtPrice(neckEnd)}，按头到颈线的高度量度看向 ${fmtPrice(neckEnd + h)}，跌破右肩 ${fmtPrice(s2.price)} 止损。`,
      };
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export function detectPatterns(base: BaseData, options: PatternOptions = {}): DetectedPattern[] {
  const opts = { ...DEFAULTS, ...options };
  const n = base.closes.length;
  if (n < 20) return [];

  const endIndex = n - 1;
  const start = Math.max(0, n - opts.windowBars);
  const swings = detectSwings(base.highs, base.lows, opts.lookback).filter(s => s.index >= start);
  if (swings.length < 3) return [];

  const out: DetectedPattern[] = [];

  const hs = buildHeadShoulders(base, swings, endIndex);
  if (hs) out.push(hs);

  const dtb = buildDoubleTopBottom(base, swings, endIndex);
  // 头肩结构本身包含“两个等高峰”，避免同一段行情重复报双顶
  if (dtb && !out.some(p => Math.max(p.startIndex, dtb.startIndex) < Math.min(p.endIndex, dtb.endIndex))) out.push(dtb);

  const b = analyseBoundaries(swings, endIndex);
  if (b) {
    const cons = buildConsolidation(base, b);
    if (cons) out.push(cons);
  }

  return out
    .sort((x, y) => y.confidence - x.confidence)
    .slice(0, opts.maxPatterns);
}

// ---------------------------------------------------------------------------
// echarts overlay payload (markLine / markArea / markPoint), aligned with the
// output style of drawings.ts so the host component merges them directly.
// ---------------------------------------------------------------------------

export interface PatternColors {
  bull: string;
  bear: string;
  neutral: string;
}

export interface PatternOverlay {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  lines: any[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  areas: any[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  points: any[];
}

export function patternColor(direction: PatternDirection, colors: PatternColors): string {
  return direction === "bullish" ? colors.bull : direction === "bearish" ? colors.bear : colors.neutral;
}

export function patternsToOverlay(
  patterns: DetectedPattern[],
  dates: string[],
  colors: PatternColors,
): PatternOverlay {
  const overlay: PatternOverlay = { lines: [], areas: [], points: [] };
  if (patterns.length === 0 || dates.length === 0) return overlay;

  // Only the highest-confidence pattern is drawn to keep the chart readable;
  // the host UI lists the rest in the explanation card.
  const p = patterns[0];
  const color = patternColor(p.direction, colors);
  const clampIdx = (i: number) => Math.max(0, Math.min(dates.length - 1, Math.round(i)));

  // 边界线 / 颈线
  for (const bd of p.boundaries) {
    overlay.lines.push([
      {
        coord: [dates[clampIdx(bd.x0)], bd.y0],
        lineStyle: { color, width: 1.8 },
        label: { show: true, formatter: `${p.nameCn}·${bd.label}`, position: "start", color, fontSize: 10 },
      },
      { coord: [dates[clampIdx(bd.x1)], bd.y1] },
    ]);
  }

  // 突破位 / 量度目标位 / 建议止损位（水平虚线，带价格数字）
  const hline = (y: number, label: string, type: "dashed" | "dotted") => ({
    yAxis: +y.toFixed(8),
    lineStyle: { color, width: 1.2, type },
    label: { show: true, formatter: `${label} ${fmtPrice(y)}`, position: "insideEndTop", color, fontSize: 10, fontWeight: "bold" },
  });
  overlay.lines.push(hline(p.breakout, "突破位", "dashed"));
  overlay.lines.push(hline(p.target, "目标位", "dotted"));
  overlay.lines.push(hline(p.stop, "止损位", "dotted"));

  // 形态区域半透明填充（方向染色）
  const ys: number[] = [];
  for (const bd of p.boundaries) ys.push(bd.y0, bd.y1);
  for (const kp of p.keyPoints) ys.push(kp.price);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  overlay.areas.push([
    {
      coord: [dates[clampIdx(p.startIndex)], yMin],
      itemStyle: { color: color + "14" },
      label: { show: true, formatter: `${p.nameCn} · ${dirLabel(p.direction)}`, color, fontSize: 10, fontWeight: "bold", position: "insideTop" },
    },
    { coord: [dates[clampIdx(p.endIndex)], yMax] },
  ]);

  // 关键点打点
  for (const kp of p.keyPoints) {
    overlay.points.push({
      coord: [dates[clampIdx(kp.index)], kp.price],
      value: "",
      symbol: "circle",
      symbolSize: 9,
      itemStyle: { color, borderColor: "#fff", borderWidth: 1.5 },
      label: { show: true, formatter: kp.label, position: "top", color, fontSize: 9, fontWeight: "bold" },
      tooltip: { formatter: `${kp.label}<br/>${kp.note}` },
    });
  }

  return overlay;
}
