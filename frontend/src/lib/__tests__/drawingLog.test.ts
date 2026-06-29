import { describe, it, expect } from "vitest";
import { mergeSamples, summarize, MAX_SAMPLES, type DrawingSample } from "../drawingLog";

function sample(over: Partial<DrawingSample> = {}): DrawingSample {
  return {
    ts: 1,
    bars: 100,
    mode: "trend",
    touches: 10,
    hits: 6,
    hitRate: 0.6,
    baselineHitRate: 0.5,
    uplift: 0.1,
    ...over,
  };
}

describe("mergeSamples", () => {
  it("dedupes by mode+bars (latest wins) so reloading the same data is idempotent", () => {
    const a = sample({ bars: 100, hitRate: 0.5 });
    const b = sample({ bars: 100, hitRate: 0.7 }); // same key, newer outcome
    const merged = mergeSamples([a], [b]);
    expect(merged).toHaveLength(1);
    expect(merged[0].hitRate).toBe(0.7);
  });

  it("accumulates distinct mode/bars samples and keeps them ordered by bars", () => {
    const merged = mergeSamples(
      [sample({ bars: 120 })],
      [sample({ bars: 100 }), sample({ bars: 100, mode: "sr" })],
    );
    expect(merged).toHaveLength(3);
    expect(merged.map(s => s.bars)).toEqual([100, 100, 120]);
  });

  it("caps the store as a ring buffer keeping the most recent samples", () => {
    const incoming = Array.from({ length: MAX_SAMPLES + 50 }, (_, i) => sample({ bars: i }));
    const merged = mergeSamples([], incoming);
    expect(merged).toHaveLength(MAX_SAMPLES);
    expect(merged[0].bars).toBe(50); // oldest 50 dropped
    expect(merged[merged.length - 1].bars).toBe(MAX_SAMPLES + 49);
  });
});

describe("summarize", () => {
  it("rolls up per-mode averages and an overall uplift", () => {
    const samples = [
      sample({ mode: "trend", bars: 1, hitRate: 0.4, uplift: 0.0 }),
      sample({ mode: "trend", bars: 2, hitRate: 0.6, uplift: 0.2 }),
      sample({ mode: "sr", bars: 1, hitRate: 0.8, uplift: 0.1 }),
    ];
    const s = summarize(samples);
    expect(s.count).toBe(3);
    expect(s.perMode.trend?.samples).toBe(2);
    expect(s.perMode.trend?.avgHitRate).toBeCloseTo(0.5, 9);
    expect(s.perMode.trend?.lastHitRate).toBe(0.6);
    expect(s.perMode.sr?.avgUplift).toBeCloseTo(0.1, 9);
    expect(s.avgUplift).toBeCloseTo((0.0 + 0.2 + 0.1) / 3, 9);
  });

  it("returns an empty summary for no samples", () => {
    const s = summarize([]);
    expect(s.count).toBe(0);
    expect(s.avgUplift).toBe(0);
    expect(s.perMode).toEqual({});
  });
});
