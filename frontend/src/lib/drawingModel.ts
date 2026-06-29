// Phase D-3 · structured-feature online model.
//
// D-1 shrinks each line's reliability toward its own historical mean, and D-2
// remembers the best params. Neither asks *why* a line was respected. D-3 adds a
// tiny online logistic regression that learns, from market-context features
// extracted at sample time (volatility, trend slope, range position, touch
// evidence), whether a drawing tends to be "respected" (hitRate >= 0.5) in that
// regime. Its prediction is a third reliability signal, blended conservatively
// and gated by sample count so it only speaks up once it has seen enough.
//
// Everything here is pure + JSON-serialisable so it is fully unit-testable and
// can be trained incrementally on the accumulated results log. The log is the
// source of truth, so the model is simply re-trained from samples on load — no
// extra persistence needed.

import type { BaseData } from "./drawings";
import type { DrawingSample } from "./drawingLog";

export const FEATURE_DIM = 4;

// --- feature extraction (market context, independent of the line's outcome) ---

// Population std of simple returns over the last `win` closes, squashed to ~[0,1].
function recentVolatility(closes: number[], win = 20): number {
  const n = closes.length;
  const start = Math.max(1, n - win);
  const rets: number[] = [];
  for (let i = start; i < n; i++) {
    const prev = closes[i - 1];
    if (prev !== 0) rets.push((closes[i] - prev) / prev);
  }
  if (rets.length === 0) return 0;
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const varc = rets.reduce((a, r) => a + (r - mean) ** 2, 0) / rets.length;
  const std = Math.sqrt(varc);
  // Typical crypto/stock daily std is a few %; tanh(std*25) maps that into ~[0,1].
  return Math.tanh(std * 25);
}

// Normalised regression slope over the last `win` closes, in (-1,1) via tanh.
function trendSlopeNorm(closes: number[], win = 40): number {
  const n = closes.length;
  const start = Math.max(0, n - win);
  const seg = closes.slice(start);
  const m = seg.length;
  if (m < 2) return 0;
  let sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (let i = 0; i < m; i++) {
    sx += i; sy += seg[i]; sxx += i * i; sxy += i * seg[i];
  }
  const denom = m * sxx - sx * sx;
  const slope = denom === 0 ? 0 : (m * sxy - sx * sy) / denom;
  const avg = sy / m || 1;
  // slope per bar as a fraction of price level, scaled so a steady trend ~±1.
  return Math.tanh((slope / avg) * 100);
}

// Where the latest close sits inside the recent high/low range, in [0,1].
function rangePosition(base: BaseData, win = 40): number {
  const n = base.closes.length;
  if (n === 0) return 0.5;
  const start = Math.max(0, n - win);
  let hi = -Infinity, lo = Infinity;
  for (let i = start; i < n; i++) {
    if (base.highs[i] > hi) hi = base.highs[i];
    if (base.lows[i] < lo) lo = base.lows[i];
  }
  const span = hi - lo;
  if (!Number.isFinite(span) || span <= 0) return 0.5;
  const pos = (base.closes[n - 1] - lo) / span;
  return pos < 0 ? 0 : pos > 1 ? 1 : pos;
}

// Extract the fixed-length feature vector used by the model. `touches` is the
// future-segment interaction count for the mode (capped evidence weight).
export function extractFeatures(base: BaseData, touches: number, win = 40): number[] {
  return [
    recentVolatility(base.closes),
    trendSlopeNorm(base.closes, win),
    rangePosition(base, win),
    Math.min(Math.max(touches, 0), 20) / 20,
  ];
}

// --- online logistic regression --------------------------------------------

export interface LogisticModel {
  w: number[];
  b: number;
}

export function createModel(dim = FEATURE_DIM): LogisticModel {
  return { w: new Array(dim).fill(0), b: 0 };
}

function sigmoid(z: number): number {
  if (z >= 0) return 1 / (1 + Math.exp(-z));
  const e = Math.exp(z);
  return e / (1 + e);
}

export function predictProba(model: LogisticModel, x: number[]): number {
  let z = model.b;
  const k = Math.min(model.w.length, x.length);
  for (let i = 0; i < k; i++) z += model.w[i] * x[i];
  return sigmoid(z);
}

// One SGD step minimising binary cross-entropy. Mutates the model in place.
export function update(model: LogisticModel, x: number[], y: number, lr = 0.1): void {
  const p = predictProba(model, x);
  const err = p - y; // dBCE/dz
  const k = Math.min(model.w.length, x.length);
  for (let i = 0; i < k; i++) model.w[i] -= lr * err * x[i];
  model.b -= lr * err;
}

export interface TrainSample {
  x: number[];
  y: number; // 0 or 1
}

// Train a fresh model over the data for a few epochs. Deterministic (no shuffle)
// so unit tests are stable; the dataset is small (≤ MAX_SAMPLES).
export function trainModel(data: TrainSample[], epochs = 8, lr = 0.1, dim = FEATURE_DIM): LogisticModel {
  const model = createModel(dim);
  for (let e = 0; e < epochs; e++) {
    for (const d of data) update(model, d.x, d.y, lr);
  }
  return model;
}

// Build a training set from accumulated samples. Only samples that were actually
// tested (touches > 0) carry signal — a line the future never touched is neither
// "respected" nor "failed", and including it (hitRate = 0 → y = 0) would bias the
// model toward pessimism. Label = was the line respected on a majority of touches.
export function buildTrainingSet(samples: DrawingSample[]): TrainSample[] {
  const out: TrainSample[] = [];
  for (const s of samples) {
    if (s.touches > 0 && s.features && s.features.length) {
      out.push({ x: s.features, y: s.hitRate >= 0.5 ? 1 : 0 });
    }
  }
  return out;
}
