import { describe, it, expect } from "vitest";
import {
  tfToSeconds,
  normalizeProbability,
  mockPredict,
  buildPredictionOverlay,
  type PredictBar,
  type PredictResponse,
} from "../predict";

// 构造 15m 周期的等距 K 线：从 t0 开始 n 根，收盘价由 closeAt 给出
const TF_SEC = 900;
const T0 = 1_700_000_000;

function makeBars(n: number, closeAt: (i: number) => number): PredictBar[] {
  const bars: PredictBar[] = [];
  for (let i = 0; i < n; i++) {
    const close = closeAt(i);
    bars.push({
      timeSec: T0 + i * TF_SEC,
      close,
      high: close * 1.002,
      low: close * 0.998,
    });
  }
  return bars;
}

describe("tfToSeconds", () => {
  it("parses minute/hour/day/week units", () => {
    expect(tfToSeconds("1m")).toBe(60);
    expect(tfToSeconds("15m")).toBe(900);
    expect(tfToSeconds("1h")).toBe(3600);
    expect(tfToSeconds("4h")).toBe(14400);
    expect(tfToSeconds("1d")).toBe(86400);
    expect(tfToSeconds("1w")).toBe(604800);
  });

  it("returns 0 for unparsable input (caller falls back to bar gap)", () => {
    expect(tfToSeconds("abc")).toBe(0);
    expect(tfToSeconds("")).toBe(0);
  });
});

describe("normalizeProbability", () => {
  it("normalizes to sum 1", () => {
    const p = normalizeProbability({ up: 2, down: 1, sideways: 1 });
    expect(p.up).toBeCloseTo(0.5);
    expect(p.down).toBeCloseTo(0.25);
    expect(p.sideways).toBeCloseTo(0.25);
    expect(p.up + p.down + p.sideways).toBeCloseTo(1);
  });

  it("falls back to uniform on invalid/zero input", () => {
    const p = normalizeProbability({ up: 0, down: 0, sideways: 0 });
    expect(p.up).toBeCloseTo(1 / 3);
    const q = normalizeProbability({ up: NaN, down: -5, sideways: 0 });
    expect(q.up + q.down + q.sideways).toBeCloseTo(1);
  });
});

describe("mockPredict", () => {
  it("is deterministic for the same kline input (poll-refresh idempotent)", () => {
    const bars = makeBars(60, (i) => 100 + i * 0.5);
    const a = mockPredict("BTCUSDT", "15m", 16, bars);
    const b = mockPredict("BTCUSDT", "15m", 16, bars);
    expect(a).toEqual(b);
  });

  it("tilts up on rising momentum and down on falling momentum", () => {
    const up = mockPredict("BTCUSDT", "15m", 16, makeBars(60, (i) => 100 + i))!;
    expect(up.direction).toBe("up");
    expect(up.probability.up).toBeGreaterThan(up.probability.down);

    const down = mockPredict("BTCUSDT", "15m", 16, makeBars(60, (i) => 200 - i))!;
    expect(down.direction).toBe("down");
    expect(down.probability.down).toBeGreaterThan(down.probability.up);
  });

  it("produces horizon path points marching into the future and a sane zone", () => {
    const bars = makeBars(60, (i) => 100 + i * 0.5);
    const r = mockPredict("BTCUSDT", "15m", 16, bars)!;
    expect(r.path).toHaveLength(16);
    expect(r.mock).toBe(true);
    expect(r.targetZone.high).toBeGreaterThan(r.targetZone.low);
    // path 时间严格递增且都在最后一根 K 线之后
    const lastSec = bars[bars.length - 1].timeSec;
    let prev = lastSec;
    for (const p of r.path) {
      const sec = Date.parse(p.t) / 1000;
      expect(sec).toBeGreaterThan(prev);
      prev = sec;
    }
    // 概率三档归一
    const s = r.probability.up + r.probability.down + r.probability.sideways;
    expect(s).toBeCloseTo(1);
  });

  it("returns null when bars are insufficient", () => {
    expect(mockPredict("BTCUSDT", "15m", 16, [])).toBeNull();
    expect(mockPredict("BTCUSDT", "15m", 0, makeBars(60, () => 100))).toBeNull();
  });
});

describe("buildPredictionOverlay", () => {
  const bars = makeBars(60, (i) => 100 + i * 0.5);
  const lastIdx = bars.length - 1;
  const lastBar = bars[lastIdx];

  function resp(overrides?: Partial<PredictResponse>): PredictResponse {
    return {
      ok: true,
      symbol: "BTCUSDT",
      timeframe: "15m",
      generatedAt: new Date(lastBar.timeSec * 1000).toISOString(),
      horizon: 8,
      direction: "up",
      probability: { up: 0.42, down: 0.33, sideways: 0.25 },
      targetZone: { high: 140, low: 130 },
      path: [1, 2, 3, 4].map((k) => ({
        t: new Date((lastBar.timeSec + k * TF_SEC) * 1000).toISOString(),
        price: 130 + k,
      })),
      confidence: 0.62,
      rationale: "测试",
      signals: ["dow-rule123"],
      mock: false,
      ...overrides,
    };
  }

  it("anchors at the generatedAt bar and maps path times to future logical indexes", () => {
    const o = buildPredictionOverlay(resp(), bars)!;
    expect(o.anchorIdx).toBe(lastIdx);
    expect(o.anchorPrice).toBe(lastBar.close);
    expect(o.points.map((p) => p.idx)).toEqual([lastIdx + 1, lastIdx + 2, lastIdx + 3, lastIdx + 4]);
    expect(o.horizon).toBe(8);
    expect(o.mock).toBe(false);
    expect(o.genKey).toContain("BTCUSDT|15m|");
  });

  it("keeps the anchor pinned when new bars arrive after generation (no drift)", () => {
    const r = resp();
    const grown = [...bars, ...makeBars(3, (i) => 130 + i).map((b, i) => ({
      ...b,
      timeSec: lastBar.timeSec + (i + 1) * TF_SEC,
    }))];
    const o = buildPredictionOverlay(r, grown)!;
    // anchor 仍指向生成时刻那根 bar，而非新的最后一根
    expect(o.anchorIdx).toBe(lastIdx);
    expect(o.points[0].idx).toBe(lastIdx + 1);
  });

  it("drops non-future path points and normalizes swapped zone bounds", () => {
    const r = resp({
      // 第一个点在 anchor 时刻（rel=0，历史口径）应被丢弃
      path: [
        { t: new Date(lastBar.timeSec * 1000).toISOString(), price: 128 },
        { t: new Date((lastBar.timeSec + TF_SEC) * 1000).toISOString(), price: 131 },
      ],
      targetZone: { high: 130, low: 140 },
    });
    const o = buildPredictionOverlay(r, bars)!;
    expect(o.points).toHaveLength(1);
    expect(o.points[0].idx).toBe(lastIdx + 1);
    expect(o.targetZone.high).toBe(140);
    expect(o.targetZone.low).toBe(130);
  });

  it("normalizes probability and clamps confidence", () => {
    const o = buildPredictionOverlay(
      resp({ probability: { up: 2, down: 1, sideways: 1 }, confidence: 1.7 }),
      bars,
    )!;
    expect(o.probability.up).toBeCloseTo(0.5);
    expect(o.confidence).toBe(1);
  });

  it("returns null on empty bars or invalid zone", () => {
    expect(buildPredictionOverlay(resp(), [])).toBeNull();
    expect(
      buildPredictionOverlay(resp({ targetZone: { high: NaN, low: 1 } }), bars),
    ).toBeNull();
  });

  it("marks mock responses and reflects it in title/tooltip", () => {
    const o = buildPredictionOverlay(resp({ mock: true }), bars)!;
    expect(o.mock).toBe(true);
    expect(o.title).toContain("演示");
    expect(o.tooltip).toContain("演示数据");
  });
});
