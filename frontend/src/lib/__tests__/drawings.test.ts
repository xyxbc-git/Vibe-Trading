import { describe, it, expect } from "vitest";
import {
  gridSearchParams,
  scoreDrawings,
  walkForwardScore,
  WALK_FORWARD_RATIOS,
  DEFAULT_PARAMS,
  type BaseData,
} from "../drawings";

const MODES = ["trend", "sr", "fib", "channel", "rect"] as const;

// Deterministic synthetic OHLC: a noisy sine wave so swing detection, S/R
// clustering and channels all have something to chew on.
function makeBars(n: number): BaseData {
  const dates: string[] = [];
  const closes: number[] = [];
  const highs: number[] = [];
  const lows: number[] = [];
  for (let i = 0; i < n; i++) {
    const base = 100 + 15 * Math.sin(i / 9) + 6 * Math.sin(i / 3.3) + (i % 7) * 0.4;
    dates.push(`2024-01-${String(i + 1).padStart(3, "0")}`);
    closes.push(+base.toFixed(2));
    highs.push(+(base + 1.5 + (i % 3)).toFixed(2));
    lows.push(+(base - 1.5 - (i % 4)).toFixed(2));
  }
  return { dates, closes, highs, lows };
}

describe("gridSearchParams baseline / uplift", () => {
  it("exposes a baseline scored with DEFAULT_PARAMS and a non-negative uplift", () => {
    const bars = makeBars(220);
    const res = gridSearchParams(bars);

    // Baseline is the default-params score pooled over the walk-forward folds.
    const refBaseline = walkForwardScore(bars, DEFAULT_PARAMS);
    for (const m of MODES) {
      expect(res.baseline.perMode[m].hitRate).toBeCloseTo(refBaseline[m].hitRate, 6);
    }

    // Tuning can only match or beat the baseline (baseline is the initial best).
    expect(res.score).toBeGreaterThanOrEqual(res.baseline.score - 1e-9);
    expect(res.uplift).toBeCloseTo(res.score - res.baseline.score, 9);
    expect(res.uplift).toBeGreaterThanOrEqual(-1e-9);
  });

  it("keeps defaults with zero uplift when there are too few bars to validate", () => {
    const res = gridSearchParams(makeBars(20));
    expect(res.params).toEqual(DEFAULT_PARAMS);
    expect(res.uplift).toBe(0);
    expect(res.score).toBeCloseTo(res.baseline.score, 9);
  });
});

describe("walkForwardScore pooling", () => {
  it("pools touches/hits across folds (= sum of per-fold single-split scores)", () => {
    const bars = makeBars(220);
    const wf = walkForwardScore(bars, DEFAULT_PARAMS, WALK_FORWARD_RATIOS);

    for (const m of MODES) {
      let touches = 0;
      let hits = 0;
      for (const r of WALK_FORWARD_RATIOS) {
        const sm = scoreDrawings(bars, DEFAULT_PARAMS, r);
        touches += sm[m].touches;
        hits += sm[m].hits;
      }
      expect(wf[m].touches).toBe(touches);
      expect(wf[m].hits).toBe(hits);
      expect(wf[m].hitRate).toBeCloseTo(touches > 0 ? hits / touches : 0, 9);
    }
  });

  it("is deterministic and accumulates at least as much evidence as one split", () => {
    const bars = makeBars(180);
    const a = walkForwardScore(bars);
    const b = walkForwardScore(bars);
    const single = scoreDrawings(bars, DEFAULT_PARAMS, 0.7);
    for (const m of MODES) {
      expect(a[m]).toEqual(b[m]);
      // Multiple folds can only pool more (or equal) touch evidence than one.
      expect(a[m].touches).toBeGreaterThanOrEqual(single[m].touches);
    }
  });

  it("falls back to a single 0.7 split when given an empty fold list", () => {
    const bars = makeBars(160);
    const wf = walkForwardScore(bars, DEFAULT_PARAMS, []);
    const single = scoreDrawings(bars, DEFAULT_PARAMS, 0.7);
    for (const m of MODES) {
      expect(wf[m].touches).toBe(single[m].touches);
      expect(wf[m].hits).toBe(single[m].hits);
    }
  });
});
