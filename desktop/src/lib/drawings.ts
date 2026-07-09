// Auto-drawing engine for the desktop K-line chart (lightweight-charts).
//
// Ported from frontend/src/lib/drawings.ts. The scoring / tuning / smart-level
// layers are kept verbatim (pure functions over OHLC arrays). The only real
// difference is the render payload: the web engine emits echarts markLine /
// markArea fragments, while lightweight-charts has no such concept — so
// computeDrawings here returns a renderer-agnostic payload:
//
//   - segments : sloped two-point lines (trend lines, channel edges) keyed by
//                bar INDEX; the renderer maps index → bar time and draws each
//                as a 2-point LineSeries.
//   - hlines   : horizontal levels (S/R clusters, fib ratios) drawn full-width
//                via series.createPriceLine.
//   - bands    : time-bounded boxes (consolidation rectangle) drawn as their
//                top/bottom edge segments.
//
// Self-learning layer (Phase A/B): besides drawing, the engine can *score* how
// well each line type would have predicted the future (split the bars into a
// train segment that defines the lines and a held-out segment that validates
// them), and grid-search the tunable parameters to pick the set with the best
// historical hit rate per symbol/interval.

export type DrawMode = "trend" | "sr" | "fib" | "channel" | "rect";

export interface BaseData {
  dates: string[];
  closes: number[];
  highs: number[];
  lows: number[];
}

export interface DrawColors {
  up: string;
  down: string;
  sr: string;
  fib: string;
  channel: string;
  rect: string;
  rectFill: string;
}

export type DrawLineStyle = "solid" | "dashed" | "dotted";

// Structured role of a line, so consumers can filter without string-matching
// labels (which silently breaks when copy changes). Optional for backwards
// compatibility — consumers fall back to label matching when absent.
//   drawings:  "trend" | "sr" | "fib" | "channel-mid" | "channel-edge" | "rect"
//   tradePlan: "entry" | "sl" | "tp1" | "tp2" | "tp"
export type DrawLineKind =
  | "trend" | "sr" | "fib" | "channel-mid" | "channel-edge" | "rect"
  | "entry" | "sl" | "tp1" | "tp2" | "tp";

// A sloped line between two bars, in bar-index space (renderer maps to time).
export interface DrawSegment {
  i1: number;
  p1: number;
  i2: number;
  p2: number;
  color: string;
  width: number; // fractional; renderer clamps to the library's 1..4 range
  opacity?: number; // 0..1, applied into the color by the renderer
  style?: DrawLineStyle;
  label?: string;
  kind?: DrawLineKind;
}

// A horizontal level across the whole chart (price line).
export interface DrawHLine {
  price: number;
  color: string;
  width: number;
  opacity?: number;
  style?: DrawLineStyle;
  label?: string;
  kind?: DrawLineKind;
}

// A time-bounded price box, rendered as its top/bottom edges.
export interface DrawBand {
  i1: number;
  i2: number;
  top: number;
  bottom: number;
  color: string;
  opacity?: number;
  label?: string;
}

export interface DrawingResult {
  segments: DrawSegment[];
  hlines: DrawHLine[];
  bands: DrawBand[];
}

// A named key level supplied by an external signal system (e.g. the twelve
// technical systems endpoint), rendered as a price line by the chart.
export interface KeyLevel {
  label: string;
  price: number;
  color?: string;
  width?: number;
}

export interface Swing {
  index: number;
  price: number;
  type: "high" | "low";
}

// Tunable parameters, threaded through every drawing/scoring function so the
// grid-search can optimise them.
export interface DrawParams {
  swingLookback: number;
  fibWindow: number;
  channelWindow: number;
  rectWindow: number;
  srTolPct: number; // S/R cluster tolerance as a fraction of the price range
}

export const DEFAULT_PARAMS: DrawParams = {
  swingLookback: 5,
  fibWindow: 120,
  channelWindow: 100,
  rectWindow: 40,
  srTolPct: 0.012,
};

const FIB_RATIOS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

// Phase C: modulate line emphasis by reliability (0..1). When `rel` is
// undefined the original styling is preserved.
function relWidth(baseWidth: number, rel?: number): number {
  return rel === undefined ? baseWidth : +(baseWidth * (0.7 + 0.9 * rel)).toFixed(2);
}
function relOpacity(rel?: number): number | undefined {
  return rel === undefined ? undefined : +(0.3 + 0.7 * rel).toFixed(2);
}
function relLabel(label: string, rel?: number): string {
  return rel === undefined ? label : `${label} ${(rel * 100).toFixed(0)}%`;
}

// Fractal swing detection: index i is a swing high/low when its high/low is the
// strict extreme within [i-lookback, i+lookback].
export function detectSwings(highs: number[], lows: number[], lookback = DEFAULT_PARAMS.swingLookback): Swing[] {
  const swings: Swing[] = [];
  const n = highs.length;
  for (let i = lookback; i < n - lookback; i++) {
    let isHigh = true;
    let isLow = true;
    for (let j = i - lookback; j <= i + lookback; j++) {
      if (j === i) continue;
      if (highs[j] >= highs[i]) isHigh = false;
      if (lows[j] <= lows[i]) isLow = false;
    }
    if (isHigh) swings.push({ index: i, price: highs[i], type: "high" });
    else if (isLow) swings.push({ index: i, price: lows[i], type: "low" });
  }
  return swings;
}

function leastSquares(ys: number[]): { slope: number; intercept: number } {
  const n = ys.length;
  let sx = 0;
  let sy = 0;
  let sxx = 0;
  let sxy = 0;
  for (let i = 0; i < n; i++) {
    sx += i;
    sy += ys[i];
    sxx += i * i;
    sxy += i * ys[i];
  }
  const denom = n * sxx - sx * sx;
  const slope = denom === 0 ? 0 : (n * sxy - sx * sy) / denom;
  const intercept = (sy - slope * sx) / n;
  return { slope, intercept };
}

// Cluster swing pivots within a price tolerance into horizontal S/R levels.
export function clusterLevels(swings: Swing[], tol: number): { level: number; count: number }[] {
  if (swings.length < 2) return [];
  const prices = swings.map(s => s.price).sort((a, b) => a - b);
  const clusters: { sum: number; count: number; level: number }[] = [];
  for (const p of prices) {
    const tail = clusters[clusters.length - 1];
    if (tail && Math.abs(p - tail.level) <= tol) {
      tail.sum += p;
      tail.count++;
      tail.level = tail.sum / tail.count;
    } else {
      clusters.push({ sum: p, count: 1, level: p });
    }
  }
  return clusters
    .filter(cl => cl.count >= 2)
    .sort((a, b) => b.count - a.count)
    .map(cl => ({ level: cl.level, count: cl.count }));
}

// Trend lines: connect the two most recent swing lows (support) and swing highs
// (resistance), then project each to the latest bar so the line keeps extending.
function trendSegments(base: BaseData, c: DrawColors, p: DrawParams, rel?: number): DrawSegment[] {
  const { highs, lows } = base;
  const out: DrawSegment[] = [];
  const swings = detectSwings(highs, lows, p.swingLookback);
  const lo2 = swings.filter(s => s.type === "low").slice(-2);
  const hi2 = swings.filter(s => s.type === "high").slice(-2);
  const last = base.dates.length - 1;

  const make = (a: Swing, b: Swing, color: string, label: string): DrawSegment => {
    const slope = (b.price - a.price) / (b.index - a.index);
    const yEnd = b.price + slope * (last - b.index);
    return {
      i1: a.index,
      p1: a.price,
      i2: last,
      p2: yEnd,
      color,
      width: relWidth(1.5, rel),
      opacity: relOpacity(rel),
      style: "solid",
      label: relLabel(label, rel),
      kind: "trend",
    };
  };

  if (lo2.length === 2) out.push(make(lo2[0], lo2[1], c.up, "支撑趋势"));
  if (hi2.length === 2) out.push(make(hi2[0], hi2[1], c.down, "压力趋势"));
  return out;
}

// Support / resistance: cluster swing pivots within a price tolerance, then keep
// the 3 clusters touched most often as horizontal levels.
function srHLines(base: BaseData, c: DrawColors, p: DrawParams, rel?: number): DrawHLine[] {
  const { highs, lows } = base;
  const swings = detectSwings(highs, lows, p.swingLookback);
  if (swings.length < 2) return [];

  const min = Math.min(...lows);
  const max = Math.max(...highs);
  const tol = (max - min || 1) * p.srTolPct;

  return clusterLevels(swings, tol)
    .slice(0, 3)
    .map(cl => ({
      price: +cl.level.toFixed(2),
      color: c.sr,
      width: relWidth(1, rel),
      opacity: relOpacity(rel),
      style: "dashed" as const,
      label: relLabel(`S/R ${cl.level.toFixed(2)}`, rel),
      kind: "sr" as const,
    }));
}

// Counter-move ratio beyond which the prior swing is considered invalidated:
// once a pullback/bounce reclaims more than half of the wave (the 50% midline,
// the classic Dow/Gann strength divider), traders re-anchor the fib to the new
// direction instead of keeping the stale swing.
const FIB_REANCHOR = 0.5;

// Decide the ACTIVE wave direction for fib anchoring, market-convention style.
// Not just "which extreme came last": after a high→low decline, a bounce that
// has already reclaimed over half of the drop invalidates the downswing, so
// the fib re-anchors as an upswing retracement (and mirrored for pullbacks).
export function fibWaveIsUp(
  hiIdx: number,
  loIdx: number,
  hi: number,
  lo: number,
  lastClose: number,
): boolean {
  const range = Math.max(hi - lo, 1e-9);
  if (hiIdx >= loIdx) {
    // Low precedes high (upswing), price now pulling back from the high.
    // Pullback beyond the midline → treat as a new downswing.
    const retrace = (hi - lastClose) / range;
    return retrace <= FIB_REANCHOR;
  }
  // High precedes low (downswing), price now bouncing off the low.
  // Bounce reclaiming over the midline → downswing failed, anchor as upswing.
  const rebound = (lastClose - lo) / range;
  return rebound > FIB_REANCHOR;
}

// Fibonacci retracement: take the highest high and lowest low within the recent
// window, infer the ACTIVE wave direction (see fibWaveIsUp), and lay out the
// standard ratio levels.
//
// Anchoring follows market convention (TradingView / exchange default):
//   upswing   → 0 sits AT THE HIGH, 1 at the low; each ratio measures pullback
//               depth from the swing high (0.618 = golden pullback, lower half).
//   downswing → 0 sits at the low, 1 at the high; ratios measure bounce height
//               from the swing low (0.618 bounce sits in the upper half).
// Labels use decimal ratios ("Fib 0.618"), not percentages.
function fibHLines(base: BaseData, c: DrawColors, p: DrawParams, rel?: number): DrawHLine[] {
  const { highs, lows, closes } = base;
  const n = highs.length;
  const start = Math.max(0, n - p.fibWindow);
  let hiIdx = start;
  let loIdx = start;
  for (let i = start; i < n; i++) {
    if (highs[i] > highs[hiIdx]) hiIdx = i;
    if (lows[i] < lows[loIdx]) loIdx = i;
  }
  const hi = highs[hiIdx];
  const lo = lows[loIdx];
  if (hi <= lo) return [];

  const uptrend = fibWaveIsUp(hiIdx, loIdx, hi, lo, closes[n - 1]);
  return FIB_RATIOS.map(r => {
    const level = uptrend ? hi - (hi - lo) * r : lo + (hi - lo) * r;
    return {
      price: +level.toFixed(2),
      color: c.fib,
      width: relWidth(0.8, rel),
      opacity: relOpacity(rel),
      style: "dotted" as const,
      label: `Fib ${r}`,
      kind: "fib" as const,
    };
  });
}

// Regression channel: fit close prices over the recent window, then offset the
// regression line by the max positive/negative residual to form the channel.
function channelSegments(base: BaseData, c: DrawColors, p: DrawParams, rel?: number): DrawSegment[] {
  const { closes } = base;
  const n = closes.length;
  const start = Math.max(0, n - p.channelWindow);
  const seg = closes.slice(start);
  if (seg.length < 3) return [];

  const { slope, intercept } = leastSquares(seg);
  let maxPos = -Infinity;
  let maxNeg = Infinity;
  for (let i = 0; i < seg.length; i++) {
    const res = seg[i] - (slope * i + intercept);
    if (res > maxPos) maxPos = res;
    if (res < maxNeg) maxNeg = res;
  }

  const last = seg.length - 1;
  const mid0 = intercept;
  const mid1 = slope * last + intercept;

  const line = (y0: number, y1: number, width: number, label: string, style: DrawLineStyle, kind: "channel-mid" | "channel-edge"): DrawSegment => ({
    i1: start,
    p1: y0,
    i2: n - 1,
    p2: y1,
    color: c.channel,
    width: relWidth(width, rel),
    opacity: relOpacity(rel),
    style,
    label: relLabel(label, rel),
    kind,
  });

  return [
    line(mid0 + maxPos, mid1 + maxPos, 0.8, "通道上轨", "dashed", "channel-edge"),
    line(mid0, mid1, 1.2, "回归中轴", "solid", "channel-mid"),
    line(mid0 + maxNeg, mid1 + maxNeg, 0.8, "通道下轨", "dashed", "channel-edge"),
  ];
}

// Rectangle: highlight the consolidation box (high/low extremes) of the recent
// window. Rendered by the chart as the box's top/bottom edges.
function rectBands(base: BaseData, c: DrawColors, p: DrawParams, rel?: number): DrawBand[] {
  const { highs, lows } = base;
  const n = highs.length;
  const start = Math.max(0, n - p.rectWindow);
  let hi = -Infinity;
  let lo = Infinity;
  for (let i = start; i < n; i++) {
    if (highs[i] > hi) hi = highs[i];
    if (lows[i] < lo) lo = lows[i];
  }
  if (hi <= lo || start >= n - 1) return [];

  return [{
    i1: start,
    i2: n - 1,
    top: +hi.toFixed(2),
    bottom: +lo.toFixed(2),
    color: c.rect,
    opacity: relOpacity(rel),
    label: relLabel("整理区间", rel),
  }];
}

// ---------------------------------------------------------------------------
// Smart levels — the beginner-friendly view: surface only the nearest strong
// resistance ABOVE the current price and the nearest strong support BELOW it,
// as zones with a plain-language label and a touch count.
// ---------------------------------------------------------------------------

export interface SmartZone {
  kind: "support" | "resistance";
  level: number; // cluster centre price
  lower: number; // band bottom
  upper: number; // band top
  touches: number; // how many swing pivots formed the cluster (conviction)
}

export interface SmartLevels {
  price: number; // current price (last close)
  resistance: SmartZone | null; // nearest strong zone above price
  support: SmartZone | null; // nearest strong zone below price
}

// Half-width of a zone band as a fraction of price (≈0.4% each side).
export const SMART_BAND_PCT = 0.004;

export function computeSmartLevels(
  base: BaseData,
  params: DrawParams = DEFAULT_PARAMS,
  bandPct: number = SMART_BAND_PCT,
): SmartLevels {
  const { closes, highs, lows } = base;
  const n = closes.length;
  if (n === 0) return { price: 0, resistance: null, support: null };

  const price = closes[n - 1];
  const swings = detectSwings(highs, lows, params.swingLookback);
  const min = Math.min(...lows);
  const max = Math.max(...highs);
  const tol = (max - min || 1) * params.srTolPct;
  const clusters = clusterLevels(swings, tol); // count >= 2, sorted by count desc

  const toZone = (level: number, touches: number): SmartZone => ({
    kind: level >= price ? "resistance" : "support",
    level: +level.toFixed(2),
    lower: +(level * (1 - bandPct)).toFixed(2),
    upper: +(level * (1 + bandPct)).toFixed(2),
    touches,
  });

  let resistance: SmartZone | null = null; // nearest above price
  let support: SmartZone | null = null; // nearest below price
  for (const cl of clusters) {
    if (cl.level >= price) {
      if (!resistance || cl.level < resistance.level) resistance = toZone(cl.level, cl.count);
    } else {
      if (!support || cl.level > support.level) support = toZone(cl.level, cl.count);
    }
  }
  return { price: +price.toFixed(2), resistance, support };
}

// ---------------------------------------------------------------------------
// Directional read — 偏多 / 偏空 / 观望 from where price sits vs the zones.
// ---------------------------------------------------------------------------

export type BiasDir = "long" | "short" | "neutral";

export interface SmartBias {
  dir: BiasDir;
  label: string; // 偏多 / 偏空 / 观望
  detail: string; // plain-language reason
}

// Within this fraction of price a level counts as "贴近" (about to react).
export const BIAS_NEAR_PCT = 0.015;

export function computeBias(levels: SmartLevels, nearPct: number = BIAS_NEAR_PCT): SmartBias {
  const { price, resistance: R, support: S } = levels;
  if (!price) return { dir: "neutral", label: "观望", detail: "数据不足" };
  if (R && price > R.upper) return { dir: "long", label: "偏多", detail: "突破压力 · 转多" };
  if (S && price < S.lower) return { dir: "short", label: "偏空", detail: "跌破支撑 · 转空" };
  const dR = R ? (R.level - price) / price : Infinity;
  const dS = S ? (price - S.level) / price : Infinity;
  if (dR <= nearPct && dR <= dS) return { dir: "short", label: "偏空", detail: "贴近压力 · 可考虑做空 / 减仓" };
  if (dS <= nearPct && dS < dR) return { dir: "long", label: "偏多", detail: "贴近支撑 · 可考虑做多" };
  return { dir: "neutral", label: "观望", detail: "区间中部 · 等突破方向" };
}

// Aggregate all active modes into a single render payload.
export function computeDrawings(
  modes: Set<DrawMode>,
  base: BaseData,
  colors: DrawColors,
  params: DrawParams = DEFAULT_PARAMS,
  reliability?: Partial<Record<DrawMode, number>>,
): DrawingResult {
  const segments: DrawSegment[] = [];
  const hlines: DrawHLine[] = [];
  const bands: DrawBand[] = [];
  if (!base.dates.length || modes.size === 0) return { segments, hlines, bands };

  const rel = (m: DrawMode) => reliability?.[m];

  if (modes.has("trend")) segments.push(...trendSegments(base, colors, params, rel("trend")));
  if (modes.has("sr")) hlines.push(...srHLines(base, colors, params, rel("sr")));
  if (modes.has("fib")) hlines.push(...fibHLines(base, colors, params, rel("fib")));
  if (modes.has("channel")) segments.push(...channelSegments(base, colors, params, rel("channel")));
  if (modes.has("rect")) bands.push(...rectBands(base, colors, params, rel("rect")));

  return { segments, hlines, bands };
}

// ---------------------------------------------------------------------------
// Phase A · Scoring layer — "is the line accurate?"
// ---------------------------------------------------------------------------

export interface ScoreResult {
  mode: DrawMode;
  touches: number; // how many future bars interacted with the line
  hits: number;    // how many of those interactions the line predicted correctly
  hitRate: number; // hits / touches, 0..1 (0 when no touches)
}

const CONFIRM_BARS = 3; // bars to look ahead after a touch to judge respect

// Evaluate a (possibly sloped) level over the held-out future segment.
// `levelAt` returns the level price at an absolute bar index.
function evalLevel(
  levelAt: (i: number) => number,
  kind: "support" | "resistance" | "neutral",
  base: BaseData,
  startIdx: number,
  tol: number,
): { touches: number; hits: number } {
  const { highs, lows, closes } = base;
  const n = highs.length;
  let touches = 0;
  let hits = 0;
  for (let i = startIdx; i < n; i++) {
    const lv = levelAt(i);
    if (lows[i] - tol <= lv && lv <= highs[i] + tol) {
      touches++;
      const j = Math.min(i + CONFIRM_BARS, n - 1);
      const after = closes[j];
      if (kind === "support" && after > lv + tol) hits++;
      else if (kind === "resistance" && after < lv - tol) hits++;
      else if (kind === "neutral") {
        // A horizontal level (S/R, fib) is "respected" only when price bounces
        // back to the side it approached from — a clean break-through means the
        // level FAILED, so it must NOT count as a hit.
        const before = closes[Math.max(i - 1, 0)];
        const sameSide = (before >= lv) === (after >= lv);
        if (sameSide && Math.abs(after - lv) > tol) hits++;
      }
    }
  }
  return { touches, hits };
}

function mk(mode: DrawMode, touches: number, hits: number): ScoreResult {
  return { mode, touches, hits, hitRate: touches > 0 ? hits / touches : 0 };
}

// Split index for train(define lines) / future(validate) — defaults to 70%.
function splitIndex(n: number, ratio: number): number {
  return Math.max(2, Math.min(n - 1, Math.floor(n * ratio)));
}

function trainSlice(base: BaseData, split: number): BaseData {
  return {
    dates: base.dates.slice(0, split),
    closes: base.closes.slice(0, split),
    highs: base.highs.slice(0, split),
    lows: base.lows.slice(0, split),
  };
}

function scoreTrend(base: BaseData, p: DrawParams, split: number, tol: number): ScoreResult {
  const train = trainSlice(base, split);
  const sw = detectSwings(train.highs, train.lows, p.swingLookback);
  const lo2 = sw.filter(s => s.type === "low").slice(-2);
  const hi2 = sw.filter(s => s.type === "high").slice(-2);
  let touches = 0;
  let hits = 0;
  if (lo2.length === 2) {
    const slope = (lo2[1].price - lo2[0].price) / (lo2[1].index - lo2[0].index);
    const r = evalLevel(i => lo2[1].price + slope * (i - lo2[1].index), "support", base, split, tol);
    touches += r.touches; hits += r.hits;
  }
  if (hi2.length === 2) {
    const slope = (hi2[1].price - hi2[0].price) / (hi2[1].index - hi2[0].index);
    const r = evalLevel(i => hi2[1].price + slope * (i - hi2[1].index), "resistance", base, split, tol);
    touches += r.touches; hits += r.hits;
  }
  return mk("trend", touches, hits);
}

function scoreSR(base: BaseData, p: DrawParams, split: number, tol: number): ScoreResult {
  const train = trainSlice(base, split);
  const sw = detectSwings(train.highs, train.lows, p.swingLookback);
  const range = Math.max(...train.highs) - Math.min(...train.lows) || 1;
  const levels = clusterLevels(sw, range * p.srTolPct).slice(0, 3);
  let touches = 0;
  let hits = 0;
  for (const cl of levels) {
    const r = evalLevel(() => cl.level, "neutral", base, split, tol);
    touches += r.touches; hits += r.hits;
  }
  return mk("sr", touches, hits);
}

function scoreFib(base: BaseData, p: DrawParams, split: number, tol: number): ScoreResult {
  const train = trainSlice(base, split);
  const n = train.highs.length;
  const start = Math.max(0, n - p.fibWindow);
  let hiIdx = start;
  let loIdx = start;
  for (let i = start; i < n; i++) {
    if (train.highs[i] > train.highs[hiIdx]) hiIdx = i;
    if (train.lows[i] < train.lows[loIdx]) loIdx = i;
  }
  const hi = train.highs[hiIdx];
  const lo = train.lows[loIdx];
  if (hi <= lo) return mk("fib", 0, 0);
  // Same active-wave anchoring as fibHLines, so the scored levels are the
  // exact levels the chart draws.
  const uptrend = fibWaveIsUp(hiIdx, loIdx, hi, lo, train.closes[n - 1]);
  let touches = 0;
  let hits = 0;
  for (const ratio of FIB_RATIOS) {
    const level = uptrend ? hi - (hi - lo) * ratio : lo + (hi - lo) * ratio;
    const r = evalLevel(() => level, "neutral", base, split, tol);
    touches += r.touches; hits += r.hits;
  }
  return mk("fib", touches, hits);
}

function scoreChannel(base: BaseData, p: DrawParams, split: number): ScoreResult {
  const train = trainSlice(base, split);
  const n = train.closes.length;
  const start = Math.max(0, n - p.channelWindow);
  const seg = train.closes.slice(start);
  if (seg.length < 3) return mk("channel", 0, 0);
  const { slope, intercept } = leastSquares(seg);
  let maxPos = -Infinity;
  let maxNeg = Infinity;
  for (let i = 0; i < seg.length; i++) {
    const res = seg[i] - (slope * i + intercept);
    if (res > maxPos) maxPos = res;
    if (res < maxNeg) maxNeg = res;
  }
  // Containment rate: fraction of future closes that stay inside the projected
  // channel envelope.
  let touches = 0;
  let hits = 0;
  for (let i = split; i < base.closes.length; i++) {
    const mid = slope * (i - start) + intercept;
    const upper = mid + maxPos;
    const lower = mid + maxNeg;
    touches++;
    if (base.closes[i] <= upper && base.closes[i] >= lower) hits++;
  }
  return mk("channel", touches, hits);
}

function scoreRect(base: BaseData, p: DrawParams, split: number): ScoreResult {
  const train = trainSlice(base, split);
  const n = train.highs.length;
  const start = Math.max(0, n - p.rectWindow);
  let hi = -Infinity;
  let lo = Infinity;
  for (let i = start; i < n; i++) {
    if (train.highs[i] > hi) hi = train.highs[i];
    if (train.lows[i] < lo) lo = train.lows[i];
  }
  if (hi <= lo) return mk("rect", 0, 0);
  // Range respect: fraction of future closes inside the box.
  let touches = 0;
  let hits = 0;
  for (let i = split; i < base.closes.length; i++) {
    touches++;
    if (base.closes[i] <= hi && base.closes[i] >= lo) hits++;
  }
  return mk("rect", touches, hits);
}

// Score every drawing mode against the held-out future segment.
export function scoreDrawings(
  base: BaseData,
  params: DrawParams = DEFAULT_PARAMS,
  splitRatio = 0.7,
): Record<DrawMode, ScoreResult> {
  const n = base.closes.length;
  const split = splitIndex(n, splitRatio);
  const range = Math.max(...base.highs) - Math.min(...base.lows) || 1;
  const tol = range * 0.004; // touch tolerance for level interaction
  return {
    trend: scoreTrend(base, params, split, tol),
    sr: scoreSR(base, params, split, tol),
    fib: scoreFib(base, params, split, tol),
    channel: scoreChannel(base, params, split),
    rect: scoreRect(base, params, split),
  };
}

// ---------------------------------------------------------------------------
// Phase C-wf · Walk-forward validation — score across several expanding-window
// train/validate splits and pool the outcomes, so a param set is only judged
// "good" when it generalises across regimes rather than overfitting one cut.
// ---------------------------------------------------------------------------

export const WALK_FORWARD_RATIOS = [0.5, 0.6, 0.7, 0.8];

export function walkForwardScore(
  base: BaseData,
  params: DrawParams = DEFAULT_PARAMS,
  ratios: number[] = WALK_FORWARD_RATIOS,
): Record<DrawMode, ScoreResult> {
  const pooled: Record<DrawMode, { touches: number; hits: number }> = {
    trend: { touches: 0, hits: 0 },
    sr: { touches: 0, hits: 0 },
    fib: { touches: 0, hits: 0 },
    channel: { touches: 0, hits: 0 },
    rect: { touches: 0, hits: 0 },
  };
  const folds = ratios.length > 0 ? ratios : [0.7];
  for (const r of folds) {
    const sm = scoreDrawings(base, params, r);
    (Object.keys(pooled) as DrawMode[]).forEach(m => {
      pooled[m].touches += sm[m].touches;
      pooled[m].hits += sm[m].hits;
    });
  }
  return {
    trend: mk("trend", pooled.trend.touches, pooled.trend.hits),
    sr: mk("sr", pooled.sr.touches, pooled.sr.hits),
    fib: mk("fib", pooled.fib.touches, pooled.fib.hits),
    channel: mk("channel", pooled.channel.touches, pooled.channel.hits),
    rect: mk("rect", pooled.rect.touches, pooled.rect.hits),
  };
}

// ---------------------------------------------------------------------------
// Phase B · Parameter self-tuning — grid-search the params with the best
// historical hit rate for the given bars (per symbol/interval).
// ---------------------------------------------------------------------------

export interface TuneResult {
  params: DrawParams;
  score: number; // aggregate evidence-weighted hit rate, 0..1
  perMode: Record<DrawMode, ScoreResult>;
  // The DEFAULT_PARAMS score is the baseline every tuned result is measured
  // against — what makes "越画越准" provable rather than just claimed.
  baseline: { score: number; perMode: Record<DrawMode, ScoreResult> };
  uplift: number; // score - baseline.score, on the 0..1 hit-rate scale
}

const GRID = {
  swingLookback: [3, 5, 8, 13],
  fibWindow: [80, 120, 160],
  channelWindow: [60, 100, 140],
  rectWindow: [30, 40, 60],
  srTolPct: [0.008, 0.012, 0.018],
};

const TOUCH_CAP = 20; // cap evidence weight so noisy modes don't dominate

// Evidence-weighted aggregate so modes with more (capped) touches matter more,
// and modes that never interacted with price don't drag the score down.
function aggregate(perMode: Record<DrawMode, ScoreResult>): number {
  let wSum = 0;
  let acc = 0;
  for (const m of Object.values(perMode)) {
    const w = Math.min(m.touches, TOUCH_CAP);
    acc += m.hitRate * w;
    wSum += w;
  }
  return wSum > 0 ? acc / wSum : 0;
}

export type TuneStrategy = "full" | "coordinate";

export interface TuneOptions {
  ratios?: number[];
  // Phase D-2 · warm start: a remembered "best so far" param set. Evaluated as
  // a guaranteed candidate so the result is never worse than the memory.
  seed?: DrawParams;
  // "full" = exhaustive Cartesian grid (324 combos). "coordinate" = coordinate
  // descent (~32 evals over 2 passes), the cheap path for very large bar counts.
  strategy?: TuneStrategy;
}

const GRID_AXES: (keyof DrawParams)[] = [
  "swingLookback",
  "srTolPct",
  "fibWindow",
  "channelWindow",
  "rectWindow",
];

// Score one specific param set against the walk-forward folds, exposing the
// default-params baseline + uplift. Used for the cheap warm-start fast path.
export function evaluateParams(
  base: BaseData,
  params: DrawParams,
  ratios: number[] = WALK_FORWARD_RATIOS,
): TuneResult {
  const basePerMode = walkForwardScore(base, DEFAULT_PARAMS, ratios);
  const baselineScore = aggregate(basePerMode);
  const perMode = walkForwardScore(base, params, ratios);
  const score = aggregate(perMode);
  return {
    params,
    score,
    perMode,
    baseline: { score: baselineScore, perMode: basePerMode },
    uplift: score - baselineScore,
  };
}

export function gridSearchParams(base: BaseData, opts: TuneOptions = {}): TuneResult {
  const ratios = opts.ratios ?? WALK_FORWARD_RATIOS;
  const strategy: TuneStrategy = opts.strategy ?? "full";
  const basePerMode = walkForwardScore(base, DEFAULT_PARAMS, ratios);
  const baselineScore = aggregate(basePerMode);
  const baseline = { score: baselineScore, perMode: basePerMode };

  let best: TuneResult = {
    params: DEFAULT_PARAMS,
    score: baselineScore,
    perMode: basePerMode,
    baseline,
    uplift: 0,
  };

  // Evaluate a candidate and keep it if it strictly improves the running best.
  const consider = (params: DrawParams) => {
    const perMode = walkForwardScore(base, params, ratios);
    const score = aggregate(perMode);
    if (score > best.score) best = { params, score, perMode, baseline, uplift: score - baselineScore };
  };

  // Warm start: a remembered param set is honoured as a guaranteed candidate.
  if (opts.seed) consider(opts.seed);

  // Too few bars to validate meaningfully — keep current best (default or seed).
  if (base.closes.length < 40) return best;

  if (strategy === "coordinate") {
    // Coordinate descent: optimise one axis at a time around the running best.
    // Two passes give the axes a chance to settle.
    for (let pass = 0; pass < 2; pass++) {
      for (const axis of GRID_AXES) {
        const anchor = best.params;
        for (const v of GRID[axis]) consider({ ...anchor, [axis]: v });
      }
    }
    return best;
  }

  for (const swingLookback of GRID.swingLookback) {
    for (const srTolPct of GRID.srTolPct) {
      for (const fibWindow of GRID.fibWindow) {
        for (const channelWindow of GRID.channelWindow) {
          for (const rectWindow of GRID.rectWindow) {
            consider({ swingLookback, fibWindow, channelWindow, rectWindow, srTolPct });
          }
        }
      }
    }
  }
  return best;
}
