import { describe, it, expect } from "vitest";
import {
  computeDrawings,
  gridSearchParams,
  evaluateParams,
  scoreDrawings,
  walkForwardScore,
  computeSmartLevels,
  computeBias,
  fibWaveIsUp,
  SMART_BAND_PCT,
  WALK_FORWARD_RATIOS,
  DEFAULT_PARAMS,
  type BaseData,
  type DrawMode,
  type DrawParams,
  type DrawColors,
} from "../drawings";
import { mergeSamples, summarize, blendReliability, type DrawingSample } from "../drawingLog";
import {
  extractFeatures,
  trainModel,
  predictProba,
  buildTrainingSet,
  MODEL_MIN_SAMPLES,
} from "../drawingModel";

const MODES = ["trend", "sr", "fib", "channel", "rect"] as const;

const COLORS: DrawColors = {
  up: "#3fb950",
  down: "#f85149",
  sr: "#d29922",
  fib: "#a855f7",
  channel: "#58a6ff",
  rect: "#58a6ff",
  rectFill: "#58a6ff1f",
};

// Deterministic synthetic OHLC: a noisy sine wave so swing detection, S/R
// clustering and channels all have something to chew on.
function makeBars(n: number): BaseData {
  const dates: string[] = [];
  const closes: number[] = [];
  const highs: number[] = [];
  const lows: number[] = [];
  for (let i = 0; i < n; i++) {
    const base = 100 + 15 * Math.sin(i / 9) + 6 * Math.sin(i / 3.3) + (i % 7) * 0.4;
    dates.push(String(1700000000 + i * 60));
    closes.push(+base.toFixed(2));
    highs.push(+(base + 1.5 + (i % 3)).toFixed(2));
    lows.push(+(base - 1.5 - (i % 4)).toFixed(2));
  }
  return { dates, closes, highs, lows };
}

describe("computeDrawings render payload (lightweight-charts adaptation)", () => {
  it("returns empty payload when no modes are active or no bars", () => {
    const bars = makeBars(120);
    expect(computeDrawings(new Set(), bars, COLORS)).toEqual({ segments: [], hlines: [], bands: [] });
    const empty: BaseData = { dates: [], closes: [], highs: [], lows: [] };
    expect(computeDrawings(new Set(["trend"]), empty, COLORS)).toEqual({ segments: [], hlines: [], bands: [] });
  });

  it("trend mode emits sloped segments projected to the last bar", () => {
    const bars = makeBars(160);
    const res = computeDrawings(new Set<DrawMode>(["trend"]), bars, COLORS);
    expect(res.hlines).toHaveLength(0);
    expect(res.bands).toHaveLength(0);
    expect(res.segments.length).toBeGreaterThanOrEqual(1);
    for (const s of res.segments) {
      expect(s.i2).toBe(bars.dates.length - 1); // projected to latest bar
      expect(s.i1).toBeLessThan(s.i2);
      expect(Number.isFinite(s.p1)).toBe(true);
      expect(Number.isFinite(s.p2)).toBe(true);
    }
  });

  it("sr + fib modes emit horizontal price lines; channel emits 3 segments; rect emits a band", () => {
    const bars = makeBars(200);
    const res = computeDrawings(new Set<DrawMode>(["sr", "fib", "channel", "rect"]), bars, COLORS);
    // sr: up to 3 clusters; fib: exactly 7 ratio levels
    expect(res.hlines.length).toBeGreaterThanOrEqual(7);
    expect(res.hlines.length).toBeLessThanOrEqual(10);
    for (const hl of res.hlines) expect(Number.isFinite(hl.price)).toBe(true);
    // channel: upper / mid / lower
    expect(res.segments).toHaveLength(3);
    // rect: one consolidation box with top above bottom
    expect(res.bands).toHaveLength(1);
    expect(res.bands[0].top).toBeGreaterThan(res.bands[0].bottom);
    expect(res.bands[0].i1).toBeLessThan(res.bands[0].i2);
  });

  it("reliability modulates width/opacity and appends hit-rate % to labels", () => {
    const bars = makeBars(160);
    const plain = computeDrawings(new Set<DrawMode>(["trend"]), bars, COLORS);
    const scored = computeDrawings(new Set<DrawMode>(["trend"]), bars, COLORS, DEFAULT_PARAMS, { trend: 1 });
    const faded = computeDrawings(new Set<DrawMode>(["trend"]), bars, COLORS, DEFAULT_PARAMS, { trend: 0 });
    expect(plain.segments[0].opacity).toBeUndefined();
    expect(scored.segments[0].opacity).toBeCloseTo(1, 5);
    expect(faded.segments[0].opacity).toBeCloseTo(0.3, 5);
    // rel=1 → width × 1.6, rel=0 → width × 0.7
    expect(scored.segments[0].width).toBeGreaterThan(faded.segments[0].width);
    expect(scored.segments[0].label).toMatch(/100%$/);
    expect(faded.segments[0].label).toMatch(/0%$/);
  });
});

describe("fib retracement follows market convention (TradingView anchoring)", () => {
  // Monotonic trend bars: up=true → low precedes high (upswing);
  // up=false → high precedes low (downswing).
  function makeTrendBars(n: number, up: boolean): BaseData {
    const dates: string[] = [];
    const closes: number[] = [];
    const highs: number[] = [];
    const lows: number[] = [];
    for (let i = 0; i < n; i++) {
      const base = up ? 100 + i : 100 + (n - 1 - i);
      dates.push(String(1700000000 + i * 60));
      closes.push(base);
      highs.push(base + 1);
      lows.push(base - 1);
    }
    return { dates, closes, highs, lows };
  }

  function fibLines(bars: BaseData) {
    return computeDrawings(new Set<DrawMode>(["fib"]), bars, COLORS).hlines;
  }

  it("uptrend (low precedes high): ratio 0 sits at the swing HIGH, 1 at the low", () => {
    const bars = makeTrendBars(120, true);
    const lines = fibLines(bars);
    expect(lines).toHaveLength(7);
    const hi = Math.max(...bars.highs);
    const lo = Math.min(...bars.lows);
    const byLabel = new Map(lines.map((l) => [l.label, l.price]));
    expect(byLabel.get("Fib 0")).toBeCloseTo(hi, 2);
    expect(byLabel.get("Fib 1")).toBeCloseTo(lo, 2);
    // Deeper pullback ratios sit lower: 0.618 below 0.382 (measured from high)
    expect(byLabel.get("Fib 0.618")!).toBeLessThan(byLabel.get("Fib 0.382")!);
    expect(byLabel.get("Fib 0.618")).toBeCloseTo(hi - (hi - lo) * 0.618, 2);
  });

  it("downtrend (high precedes low): ratio 0 sits at the swing LOW, 1 at the high", () => {
    const bars = makeTrendBars(120, false);
    const lines = fibLines(bars);
    expect(lines).toHaveLength(7);
    const hi = Math.max(...bars.highs);
    const lo = Math.min(...bars.lows);
    const byLabel = new Map(lines.map((l) => [l.label, l.price]));
    expect(byLabel.get("Fib 0")).toBeCloseTo(lo, 2);
    expect(byLabel.get("Fib 1")).toBeCloseTo(hi, 2);
    // Bounce ratios measured up from the low: 0.618 above 0.382
    expect(byLabel.get("Fib 0.618")!).toBeGreaterThan(byLabel.get("Fib 0.382")!);
    expect(byLabel.get("Fib 0.618")).toBeCloseTo(lo + (hi - lo) * 0.618, 2);
  });

  it("labels use decimal ratios (no percent sign), kind stays 'fib'", () => {
    const lines = fibLines(makeBars(200));
    expect(lines).toHaveLength(7);
    for (const l of lines) {
      expect(l.label).toMatch(/^Fib (0|1|0\.\d+)$/);
      expect(l.label).not.toContain("%");
      expect(l.kind).toBe("fib");
    }
  });

  it("level price set matches the anchoring formula (labels only name, never move lines)", () => {
    const bars = makeBars(200);
    const lines = fibLines(bars);
    const win = DEFAULT_PARAMS.fibWindow;
    const n = bars.highs.length;
    const start = Math.max(0, n - win);
    let hiIdx = start;
    let loIdx = start;
    for (let i = start; i < n; i++) {
      if (bars.highs[i] > bars.highs[hiIdx]) hiIdx = i;
      if (bars.lows[i] < bars.lows[loIdx]) loIdx = i;
    }
    const hi = bars.highs[hiIdx];
    const lo = bars.lows[loIdx];
    const up = fibWaveIsUp(hiIdx, loIdx, hi, lo, bars.closes[n - 1]);
    const expected = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1]
      .map((r) => +(up ? hi - (hi - lo) * r : lo + (hi - lo) * r).toFixed(2))
      .sort((a, b) => a - b);
    const actual = lines.map((l) => l.price).sort((a, b) => a - b);
    expect(actual).toEqual(expected);
  });

  // f9 regression: the wave direction must reflect the ACTIVE swing the user
  // sees, not merely which extreme index came last in the window.
  describe("active-wave re-anchoring (f9: user-visible swing beats extreme order)", () => {
    // Piecewise path builder: walks close through the given waypoints.
    function makePathBars(waypoints: number[], stepsPer = 30): BaseData {
      const dates: string[] = [];
      const closes: number[] = [];
      const highs: number[] = [];
      const lows: number[] = [];
      let t = 0;
      for (let w = 0; w < waypoints.length - 1; w++) {
        const from = waypoints[w];
        const to = waypoints[w + 1];
        for (let s = 0; s < stepsPer; s++) {
          const v = from + ((to - from) * s) / stepsPer;
          dates.push(String(1700000000 + t++ * 60));
          closes.push(+v.toFixed(2));
          highs.push(+(v + 0.5).toFixed(2));
          lows.push(+(v - 0.5).toFixed(2));
        }
      }
      return { dates, closes, highs, lows };
    }

    it("user's scenario: rally → deep dip (new window low) → bounce reclaiming >50% anchors UP", () => {
      // Mirrors the real BTC 4h shape: peak 65622 comes BEFORE the 57800 low,
      // but price has bounced back above the midline → fib must anchor to the
      // upswing: 0 at the high, 0.618 in the lower half.
      const bars = makePathBars([62000, 65622, 57800, 61848]);
      const lines = fibLines(bars);
      const byLabel = new Map(lines.map((l) => [l.label, l.price]));
      const hi = Math.max(...bars.highs);
      const lo = Math.min(...bars.lows);
      expect(byLabel.get("Fib 0")).toBeCloseTo(hi, 1);
      expect(byLabel.get("Fib 1")).toBeCloseTo(lo, 1);
      expect(byLabel.get("Fib 0.618")).toBeCloseTo(hi - (hi - lo) * 0.618, 1);
      // golden pullback sits in the LOWER half of the wave
      expect(byLabel.get("Fib 0.618")!).toBeLessThan((hi + lo) / 2);
    });

    it("weak bounce (<50% of the drop) keeps the downswing anchoring (0 at the low)", () => {
      const bars = makePathBars([65000, 57000, 59000]); // bounce ≈ 25%
      const lines = fibLines(bars);
      const byLabel = new Map(lines.map((l) => [l.label, l.price]));
      const hi = Math.max(...bars.highs);
      const lo = Math.min(...bars.lows);
      expect(byLabel.get("Fib 0")).toBeCloseTo(lo, 1);
      expect(byLabel.get("Fib 1")).toBeCloseTo(hi, 1);
    });

    it("mirror: decline → strong rally (new window high) → pullback >50% anchors DOWN", () => {
      // Low precedes high, but price has already fallen back below the
      // midline → the upswing is invalidated, anchor to the downswing.
      const bars = makePathBars([64000, 57000, 65000, 60000]); // retrace ≈ 62%
      const lines = fibLines(bars);
      const byLabel = new Map(lines.map((l) => [l.label, l.price]));
      const hi = Math.max(...bars.highs);
      const lo = Math.min(...bars.lows);
      expect(byLabel.get("Fib 0")).toBeCloseTo(lo, 1);
      expect(byLabel.get("Fib 1")).toBeCloseTo(hi, 1);
    });

    it("shallow pullback (~30%) after a rally keeps the upswing anchoring (0 at the high)", () => {
      const bars = makePathBars([57800, 65622, 63275]); // pullback ≈ 30%
      const lines = fibLines(bars);
      const byLabel = new Map(lines.map((l) => [l.label, l.price]));
      const hi = Math.max(...bars.highs);
      const lo = Math.min(...bars.lows);
      expect(byLabel.get("Fib 0")).toBeCloseTo(hi, 1);
      expect(byLabel.get("Fib 1")).toBeCloseTo(lo, 1);
      expect(byLabel.get("Fib 0.618")!).toBeLessThan((hi + lo) / 2);
    });
  });
});

describe("gridSearchParams baseline / uplift", () => {
  it("exposes a baseline scored with DEFAULT_PARAMS and a non-negative uplift", () => {
    const bars = makeBars(220);
    const res = gridSearchParams(bars);

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

  it("honours a warm seed (result never worse than the seed or the baseline)", () => {
    const bars = makeBars(240);
    const remembered = gridSearchParams(bars).params;
    const seedScore = evaluateParams(bars, remembered).score;

    const seeded = gridSearchParams(bars, { seed: remembered });
    expect(seeded.score).toBeGreaterThanOrEqual(seedScore - 1e-9);
    expect(seeded.score).toBeGreaterThanOrEqual(seeded.baseline.score - 1e-9);

    const badSeed: DrawParams = { swingLookback: 2, fibWindow: 8, channelWindow: 5, rectWindow: 5, srTolPct: 0.001 };
    const res = gridSearchParams(bars, { seed: badSeed });
    expect(res.score).toBeGreaterThanOrEqual(res.baseline.score - 1e-9);
  });

  it("coordinate-descent strategy is deterministic and never worse than baseline", () => {
    const bars = makeBars(260);
    const a = gridSearchParams(bars, { strategy: "coordinate" });
    const b = gridSearchParams(bars, { strategy: "coordinate" });
    expect(a.params).toEqual(b.params);
    expect(a.score).toBeCloseTo(b.score, 9);
    expect(a.score).toBeGreaterThanOrEqual(a.baseline.score - 1e-9);
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

describe("computeSmartLevels + computeBias", () => {
  it("returns current price and at most one support below + one resistance above", () => {
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
      expect((z.level - z.lower) / z.level).toBeCloseTo(SMART_BAND_PCT, 2);
      expect((z.upper - z.level) / z.level).toBeCloseTo(SMART_BAND_PCT, 2);
    }
  });

  it("degrades safely on empty / tiny inputs", () => {
    const empty = computeSmartLevels({ dates: [], closes: [], highs: [], lows: [] });
    expect(empty).toEqual({ price: 0, resistance: null, support: null });

    const tiny = computeSmartLevels(makeBars(3));
    expect(tiny.price).toBeGreaterThan(0);
    expect(tiny.resistance).toBeNull();
    expect(tiny.support).toBeNull();
  });

  it("computeBias flips long above resistance and short below support", () => {
    const zone = (kind: "support" | "resistance", level: number) => ({
      kind, level, lower: level * 0.996, upper: level * 1.004, touches: 3,
    });
    expect(computeBias({ price: 110, resistance: zone("resistance", 105), support: null }).dir).toBe("long");
    expect(computeBias({ price: 90, resistance: null, support: zone("support", 95) }).dir).toBe("short");
    expect(computeBias({ price: 0, resistance: null, support: null }).dir).toBe("neutral");
    // Mid-range far from both zones → neutral 观望
    expect(
      computeBias({ price: 100, resistance: zone("resistance", 110), support: zone("support", 90) }).dir,
    ).toBe("neutral");
  });
});

describe("drawingLog merge / summary / blend", () => {
  const sample = (mode: DrawMode, bars: number, hitRate: number, uplift = 0): DrawingSample => ({
    ts: bars,
    bars,
    mode,
    touches: 10,
    hits: Math.round(hitRate * 10),
    hitRate,
    baselineHitRate: hitRate - uplift,
    uplift,
  });

  it("mergeSamples dedupes by mode:bars (latest wins) and caps the buffer", () => {
    const a = [sample("sr", 100, 0.5), sample("sr", 101, 0.6)];
    const b = [sample("sr", 101, 0.9), sample("trend", 102, 0.4)];
    const merged = mergeSamples(a, b);
    expect(merged).toHaveLength(3);
    expect(merged.find((s) => s.mode === "sr" && s.bars === 101)?.hitRate).toBe(0.9);

    const many = Array.from({ length: 30 }, (_, i) => sample("fib", i, 0.5));
    expect(mergeSamples([], many, 10)).toHaveLength(10);
  });

  it("summarize rolls up per-mode averages and overall uplift", () => {
    const s = summarize([sample("sr", 1, 0.4, 0.1), sample("sr", 2, 0.6, 0.3)]);
    expect(s.count).toBe(2);
    expect(s.perMode.sr?.avgHitRate).toBeCloseTo(0.5, 9);
    expect(s.perMode.sr?.avgUplift).toBeCloseTo(0.2, 9);
    expect(s.avgUplift).toBeCloseTo(0.2, 9);
  });

  it("blendReliability converges from live rate toward the historical mean", () => {
    expect(blendReliability(0.8, undefined)).toBe(0.8);
    const few = { samples: 1, avgHitRate: 0.2, avgUplift: 0, lastHitRate: 0.2 };
    const lots = { samples: 500, avgHitRate: 0.2, avgUplift: 0, lastHitRate: 0.2 };
    const withFew = blendReliability(0.8, few);
    const withLots = blendReliability(0.8, lots);
    expect(withFew).toBeGreaterThan(withLots); // more history → closer to 0.2
    expect(withLots).toBeCloseTo(0.2, 1);
  });
});

describe("drawingModel features + logistic regression", () => {
  it("extractFeatures returns a bounded 4-vector", () => {
    const f = extractFeatures(makeBars(120), 10);
    expect(f).toHaveLength(4);
    expect(f[0]).toBeGreaterThanOrEqual(0);
    expect(f[0]).toBeLessThanOrEqual(1);
    expect(f[1]).toBeGreaterThanOrEqual(-1);
    expect(f[1]).toBeLessThanOrEqual(1);
    expect(f[2]).toBeGreaterThanOrEqual(0);
    expect(f[2]).toBeLessThanOrEqual(1);
    expect(f[3]).toBeCloseTo(0.5, 9); // 10/20
  });

  it("trainModel learns a separable pattern", () => {
    const data = [
      ...Array.from({ length: 20 }, () => ({ x: [0.9, 0, 0.5, 0.5], y: 1 })),
      ...Array.from({ length: 20 }, () => ({ x: [0.1, 0, 0.5, 0.5], y: 0 })),
    ];
    const model = trainModel(data, 20);
    expect(predictProba(model, [0.9, 0, 0.5, 0.5])).toBeGreaterThan(0.5);
    expect(predictProba(model, [0.1, 0, 0.5, 0.5])).toBeLessThan(0.5);
  });

  it("buildTrainingSet keeps only touched samples that carry features", () => {
    const mkS = (touches: number, hitRate: number, features?: number[]): DrawingSample => ({
      ts: 0, bars: 0, mode: "sr", touches, hits: 0, hitRate, baselineHitRate: 0, uplift: 0, features,
    });
    const set = buildTrainingSet([
      mkS(0, 0.9, [1, 0, 0, 0]),   // untouched → dropped
      mkS(5, 0.9),                  // no features → dropped
      mkS(5, 0.9, [1, 0, 0, 0]),   // kept, y=1
      mkS(5, 0.2, [0, 1, 0, 0]),   // kept, y=0
    ]);
    expect(set).toHaveLength(2);
    expect(set[0].y).toBe(1);
    expect(set[1].y).toBe(0);
  });
});

describe("D-3 model activation with time-keyed samples (audit #4)", () => {
  // Desktop kline windows are fixed-size, so `bars` carries the LAST BAR'S
  // TIMESTAMP. Simulate the Chart.tsx accumulation loop: each new closed bar
  // appends one sample per active mode under a new time key.
  const featured = (mode: DrawMode, barTime: number, hitRate: number): DrawingSample => ({
    ts: barTime * 1000,
    bars: barTime, // last-bar epoch seconds — the dedupe/time key
    mode,
    touches: 8,
    hits: Math.round(hitRate * 8),
    hitRate,
    baselineHitRate: hitRate,
    uplift: 0,
    features: [0.5, 0.1, 0.5, 0.4],
  });

  it("reaches the training gate after enough distinct-time samples accumulate", () => {
    const activeModes: DrawMode[] = ["trend", "sr", "fib"];
    const t0 = 1700000000;
    const barSec = 900; // 15m bars

    let log: DrawingSample[] = [];
    // ceil(15 / 3 modes) = 5 closed bars is the minimum; run 6 to cross the gate.
    for (let bar = 0; bar < 6; bar++) {
      const barTime = t0 + bar * barSec;
      const incoming = activeModes.map((m) => featured(m, barTime, 0.6));
      // Polling refreshes within the same bar must stay idempotent:
      log = mergeSamples(log, incoming);
      log = mergeSamples(log, incoming);
    }

    expect(log).toHaveLength(6 * activeModes.length);
    const trainSet = buildTrainingSet(log);
    expect(trainSet.length).toBeGreaterThanOrEqual(MODEL_MIN_SAMPLES);

    // The gate crossed → the model actually trains and emits a probability.
    const model = trainModel(trainSet);
    const p = predictProba(model, [0.5, 0.1, 0.5, 0.4]);
    expect(p).toBeGreaterThan(0);
    expect(p).toBeLessThan(1);
  });

  it("regression guard: a FIXED key (the old bar-count bug) can never reach the gate", () => {
    const activeModes: DrawMode[] = ["trend", "sr", "fib", "channel", "rect"];
    let log: DrawingSample[] = [];
    // 50 polling rounds, but the key never changes (fixed window bar count).
    for (let round = 0; round < 50; round++) {
      log = mergeSamples(log, activeModes.map((m) => featured(m, 200, 0.6)));
    }
    // Dedupe collapses everything to one sample per mode — the D-3 gate is
    // unreachable, which is exactly why the key had to become time-based.
    expect(log).toHaveLength(activeModes.length);
    expect(buildTrainingSet(log).length).toBeLessThan(MODEL_MIN_SAMPLES);
  });
});
