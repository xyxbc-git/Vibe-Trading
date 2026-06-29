import { describe, it, expect } from "vitest";
import {
  FEATURE_DIM,
  extractFeatures,
  createModel,
  predictProba,
  update,
  trainModel,
  type TrainSample,
} from "../drawingModel";
import type { BaseData } from "../drawings";

function makeBars(n: number, fn: (i: number) => number): BaseData {
  const dates: string[] = [];
  const closes: number[] = [];
  const highs: number[] = [];
  const lows: number[] = [];
  for (let i = 0; i < n; i++) {
    const c = fn(i);
    dates.push(`2024-01-${String(i + 1).padStart(3, "0")}`);
    closes.push(+c.toFixed(4));
    highs.push(+(c + 1).toFixed(4));
    lows.push(+(c - 1).toFixed(4));
  }
  return { dates, closes, highs, lows };
}

describe("extractFeatures", () => {
  it("returns a fixed-length vector with bounded components", () => {
    const bars = makeBars(120, i => 100 + 10 * Math.sin(i / 7));
    const f = extractFeatures(bars, 12);
    expect(f).toHaveLength(FEATURE_DIM);
    // volatility ∈ [0,1], slope ∈ (-1,1), range pos ∈ [0,1], touches/20 ∈ [0,1]
    expect(f[0]).toBeGreaterThanOrEqual(0);
    expect(f[0]).toBeLessThanOrEqual(1);
    expect(f[1]).toBeGreaterThan(-1);
    expect(f[1]).toBeLessThan(1);
    expect(f[2]).toBeGreaterThanOrEqual(0);
    expect(f[2]).toBeLessThanOrEqual(1);
    expect(f[3]).toBeCloseTo(12 / 20, 9);
  });

  it("caps the touch feature and is robust to flat/degenerate series", () => {
    const flat = makeBars(50, () => 100);
    const f = extractFeatures(flat, 999);
    expect(f[3]).toBe(1); // touches capped at 20 → 1.0
    for (const v of f) expect(Number.isFinite(v)).toBe(true);
    expect(f[0]).toBeCloseTo(0, 6); // no volatility on a flat line
    expect(f[2]).toBeCloseTo(0.5, 6); // zero range → neutral 0.5
  });
});

describe("logistic model", () => {
  it("a fresh model predicts 0.5 (z = 0)", () => {
    expect(predictProba(createModel(), [0.2, -0.3, 0.5, 0.4])).toBeCloseTo(0.5, 9);
  });

  it("a single update moves the prediction toward the label", () => {
    const m = createModel();
    const x = [0.8, 0.2, 0.9, 0.5];
    const before = predictProba(m, x);
    update(m, x, 1, 0.5);
    const after = predictProba(m, x);
    expect(after).toBeGreaterThan(before); // pushed toward y = 1
  });

  it("learns a linearly separable pattern (feature 0 decides the label)", () => {
    const data: TrainSample[] = [];
    for (let i = 0; i < 60; i++) {
      const hot = i % 2 === 0;
      // y = 1 when feature 0 is high, 0 when low; other features are noise-ish.
      data.push({ x: [hot ? 0.9 : 0.1, (i % 5) / 5, (i % 3) / 3, 0.5], y: hot ? 1 : 0 });
    }
    const model = trainModel(data, 40, 0.3);
    expect(predictProba(model, [0.9, 0.2, 0.3, 0.5])).toBeGreaterThan(0.6);
    expect(predictProba(model, [0.1, 0.2, 0.3, 0.5])).toBeLessThan(0.4);
  });

  it("predictProba stays within [0,1] for extreme inputs", () => {
    const m = { w: [50, -50, 50, -50], b: 10 };
    for (const x of [[1, 1, 1, 1], [-1, -1, -1, -1], [9, -9, 9, -9]]) {
      const p = predictProba(m, x);
      expect(p).toBeGreaterThanOrEqual(0);
      expect(p).toBeLessThanOrEqual(1);
    }
  });
});
