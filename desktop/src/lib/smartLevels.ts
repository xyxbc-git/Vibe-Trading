// Beginner-friendly "smart levels" for the desktop K-line chart.
//
// Mirrors the web engine (frontend/src/lib/drawings.ts → computeSmartLevels):
// surface only the two levels that matter right now — the nearest strong
// resistance ABOVE the current price and the nearest strong support BELOW it —
// each as a zone (band) with a plain-language label and a touch count. Pure
// functions so they are trivially testable and stay in sync with the web side.

export interface Swing {
  index: number;
  price: number;
  type: "high" | "low";
}

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

// Defaults kept aligned with the web engine's DEFAULT_PARAMS.
export const SWING_LOOKBACK = 5;
export const SR_TOL_PCT = 0.012;
export const SMART_BAND_PCT = 0.004;

// Fractal swing detection: index i is a swing high/low when its high/low is the
// strict extreme within [i-lookback, i+lookback].
export function detectSwings(highs: number[], lows: number[], lookback = SWING_LOOKBACK): Swing[] {
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

// Cluster swing pivots within a price tolerance into horizontal levels, keeping
// only clusters touched at least twice, sorted by conviction (touch count).
export function clusterLevels(swings: Swing[], tol: number): { level: number; count: number }[] {
  if (swings.length < 2) return [];
  const prices = swings.map((s) => s.price).sort((a, b) => a - b);
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
    .filter((cl) => cl.count >= 2)
    .sort((a, b) => b.count - a.count)
    .map((cl) => ({ level: cl.level, count: cl.count }));
}

export function computeSmartLevels(
  highs: number[],
  lows: number[],
  closes: number[],
  bandPct: number = SMART_BAND_PCT,
): SmartLevels {
  const n = closes.length;
  if (n === 0) return { price: 0, resistance: null, support: null };

  const price = closes[n - 1];
  const swings = detectSwings(highs, lows, SWING_LOOKBACK);
  const min = Math.min(...lows);
  const max = Math.max(...highs);
  const tol = (max - min || 1) * SR_TOL_PCT;
  const clusters = clusterLevels(swings, tol);

  const toZone = (level: number, touches: number): SmartZone => ({
    kind: level >= price ? "resistance" : "support",
    level: +level.toFixed(2),
    lower: +(level * (1 - bandPct)).toFixed(2),
    upper: +(level * (1 + bandPct)).toFixed(2),
    touches,
  });

  let resistance: SmartZone | null = null;
  let support: SmartZone | null = null;
  for (const cl of clusters) {
    if (cl.level >= price) {
      if (!resistance || cl.level < resistance.level) resistance = toZone(cl.level, cl.count);
    } else {
      if (!support || cl.level > support.level) support = toZone(cl.level, cl.count);
    }
  }
  return { price: +price.toFixed(2), resistance, support };
}

// Directional read — turn the neutral support/resistance zones into a plain
// "偏多 / 偏空 / 观望" call so beginners get an explicit short hint, not just
// long. Pure geometry, bidirectional. Mirrors the web engine (computeBias).

export type BiasDir = "long" | "short" | "neutral";

export interface SmartBias {
  dir: BiasDir;
  label: string; // 偏多 / 偏空 / 观望
  detail: string; // plain-language reason
}

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
