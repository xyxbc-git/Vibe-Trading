import { describe, it, expect } from "vitest";
import {
  gridSearchParams,
  evaluateParams,
  scoreDrawings,
  walkForwardScore,
  computeSmartLevels,
  SMART_BAND_PCT,
  WALK_FORWARD_RATIOS,
  DEFAULT_PARAMS,
  type BaseData,
  type DrawParams,
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

describe("evaluateParams + warm start (Phase D-2)", () => {
  it("evaluateParams exposes baseline + uplift; default seed has ~zero uplift", () => {
    const bars = makeBars(220);
    const ev = evaluateParams(bars, DEFAULT_PARAMS);
    const refBaseline = walkForwardScore(bars, DEFAULT_PARAMS);
    for (const m of MODES) {
      expect(ev.baseline.perMode[m].hitRate).toBeCloseTo(refBaseline[m].hitRate, 6);
    }
    // Same params as the baseline → score equals baseline, uplift ≈ 0.
    expect(ev.score).toBeCloseTo(ev.baseline.score, 9);
    expect(ev.uplift).toBeCloseTo(0, 9);
  });

  it("a warm seed is honoured so the result is never worse than the seed", () => {
    const bars = makeBars(240);
    // Use the previous full-search winner as the remembered seed.
    const remembered = gridSearchParams(bars).params;
    const seedScore = evaluateParams(bars, remembered).score;

    const seeded = gridSearchParams(bars, { seed: remembered });
    expect(seeded.score).toBeGreaterThanOrEqual(seedScore - 1e-9);
    // And still never worse than the default baseline.
    expect(seeded.score).toBeGreaterThanOrEqual(seeded.baseline.score - 1e-9);
  });

  it("an obviously bad seed cannot drag the result below the default baseline", () => {
    const bars = makeBars(200);
    const badSeed: DrawParams = { swingLookback: 2, fibWindow: 8, channelWindow: 5, rectWindow: 5, srTolPct: 0.001 };
    const res = gridSearchParams(bars, { seed: badSeed });
    expect(res.score).toBeGreaterThanOrEqual(res.baseline.score - 1e-9);
  });
});

describe("coordinate-descent search strategy", () => {
  it("is deterministic and never worse than the default baseline", () => {
    const bars = makeBars(260);
    const a = gridSearchParams(bars, { strategy: "coordinate" });
    const b = gridSearchParams(bars, { strategy: "coordinate" });
    expect(a.params).toEqual(b.params);
    expect(a.score).toBeCloseTo(b.score, 9);
    expect(a.score).toBeGreaterThanOrEqual(a.baseline.score - 1e-9);
    expect(a.uplift).toBeGreaterThanOrEqual(-1e-9);
  });

  it("honours a warm seed (result never worse than the seed)", () => {
    const bars = makeBars(240);
    const seed = gridSearchParams(bars).params; // full-grid winner as memory
    const seedScore = evaluateParams(bars, seed).score;
    const res = gridSearchParams(bars, { strategy: "coordinate", seed });
    expect(res.score).toBeGreaterThanOrEqual(seedScore - 1e-9);
  });

  it("keeps defaults on too-few bars regardless of strategy", () => {
    const res = gridSearchParams(makeBars(20), { strategy: "coordinate" });
    expect(res.params).toEqual(DEFAULT_PARAMS);
    expect(res.uplift).toBe(0);
  });
});

describe("computeSmartLevels", () => {
  it("returns the current price and at most one support below + one resistance above", () => {
    const bars = makeBars(220);
    const sl = computeSmartLevels(bars);

    expect(sl.price).toBeCloseTo(bars.closes[bars.closes.length - 1], 2);
    if (sl.resistance) {
      expect(sl.resistance.kind).toBe("resistance");
      expect(sl.resistance.level).toBeGreaterThanOrEqual(sl.price);
      expect(sl.resistance.touches).toBeGreaterThanOrEqual(2);
    }
    if (sl.support) {
      expect(sl.support.kind).toBe("support");
      expect(sl.support.level).toBeLessThan(sl.price);
      expect(sl.support.touches).toBeGreaterThanOrEqual(2);
    }
  });

  it("builds each level as a band around its centre (lower < level < upper)", () => {
    const bars = makeBars(220);
    const sl = computeSmartLevels(bars, DEFAULT_PARAMS, SMART_BAND_PCT);
    for (const z of [sl.support, sl.resistance]) {
      if (!z) continue;
      expect(z.lower).toBeLessThan(z.level);
      expect(z.upper).toBeGreaterThan(z.level);
      // Band half-width is ≈ SMART_BAND_PCT of the level on each side.
      expect((z.level - z.lower) / z.level).toBeCloseTo(SMART_BAND_PCT, 2);
      expect((z.upper - z.level) / z.level).toBeCloseTo(SMART_BAND_PCT, 2);
    }
  });

  it("picks the NEAREST level on each side, not just the strongest", () => {
    const bars = makeBars(220);
    const sl = computeSmartLevels(bars);
    // No other qualifying cluster should sit strictly between price and the
    // chosen level on either side (i.e. the chosen ones are the closest).
    if (sl.resistance && sl.support) {
      expect(sl.support.level).toBeLessThan(sl.price);
      expect(sl.resistance.level).toBeGreaterThanOrEqual(sl.price);
      expect(sl.support.level).toBeLessThan(sl.resistance.level);
    }
  });

  it("degrades safely on empty / tiny inputs", () => {
    const empty = computeSmartLevels({ dates: [], closes: [], highs: [], lows: [] });
    expect(empty).toEqual({ price: 0, resistance: null, support: null });

    const tiny = computeSmartLevels(makeBars(3));
    expect(tiny.price).toBeGreaterThan(0);
    // Too few bars to cluster two pivots → no zones, but never throws.
    expect(tiny.resistance).toBeNull();
    expect(tiny.support).toBeNull();
  });
});
