import { describe, it, expect } from "vitest";
import {
  mockDelta,
  normalizeDeltaResponse,
  strengthGrade,
  type DeltaKline,
  type DeltaResponse,
} from "../deltaFlow";

/** 构造一根 K 线（timeSec 按 60s 递增） */
function bar(i: number, open: number, close: number, high?: number, low?: number, volume = 100): DeltaKline {
  return {
    timeSec: 1_700_000_000 + i * 60,
    open,
    close,
    high: high ?? Math.max(open, close),
    low: low ?? Math.min(open, close),
    volume,
  };
}

/** 平盘序列（无背离）：小幅震荡 */
function flatBars(n: number): DeltaKline[] {
  return Array.from({ length: n }, (_, i) =>
    bar(i, 100 + (i % 2), 100 + ((i + 1) % 2)),
  );
}

describe("mockDelta", () => {
  it("returns null when bars are insufficient", () => {
    expect(mockDelta("BTCUSDT", "15m", [])).toBeNull();
    expect(mockDelta("BTCUSDT", "15m", [bar(0, 100, 101)])).toBeNull();
  });

  it("produces per-bar delta with sign following candle direction and cumulative cvd", () => {
    const resp = mockDelta("BTCUSDT", "15m", [
      bar(0, 100, 105, 106, 99), // 阳线 → delta > 0
      bar(1, 105, 101, 106, 100), // 阴线 → delta < 0
    ]);
    expect(resp).not.toBeNull();
    expect(resp!.mock).toBe(true);
    expect(resp!.bars).toHaveLength(2);
    expect(resp!.bars[0].delta).toBeGreaterThan(0);
    expect(resp!.bars[1].delta).toBeLessThan(0);
    // CVD 累计恒等：cvd[i] = cvd[i-1] + delta[i]
    expect(resp!.bars[1].cvd).toBeCloseTo(resp!.bars[0].cvd + resp!.bars[1].delta, 6);
  });

  it("is deterministic for the same input", () => {
    const bars = flatBars(60);
    const a = mockDelta("BTCUSDT", "15m", bars);
    const b = mockDelta("BTCUSDT", "15m", bars);
    expect(a).toEqual(b);
  });

  it("detects bullish absorption divergence (price lower low, cvd higher low)", () => {
    // 前半段：深跌大阴线压低 CVD 低点；后半段：价格创更低的低点，但阴线实体
    // 极小 + 阳线放量 → CVD 低点抬高 = 吸收
    const bars: DeltaKline[] = [];
    for (let i = 0; i < 20; i++) {
      // 前半：从 100 阴跌到 80（实体大、放量 → delta 深负）
      bars.push(bar(i, 100 - i, 100 - i - 1, 100 - i + 0.2, 100 - i - 1.2, 500));
    }
    for (let i = 20; i < 40; i++) {
      // 后半：价格更低（低点 75 一带）但都是放量阳线（delta 正 → CVD 抬升）
      const base = 76 + (i - 20) * 0.05;
      bars.push(bar(i, base, base + 1.5, base + 1.6, i === 30 ? 74.5 : base - 0.2, 800));
    }
    const resp = mockDelta("BTCUSDT", "15m", bars);
    expect(resp!.divergence.bullish.active).toBe(true);
    expect(resp!.divergence.bullish.anchors).toHaveLength(2);
    expect(resp!.absorption?.detected).toBe(true);
    expect(resp!.absorption?.side).toBe("buy");
  });

  it("reports no divergence on flat market", () => {
    const resp = mockDelta("BTCUSDT", "15m", flatBars(60));
    expect(resp!.divergence.bullish.active).toBe(false);
    expect(resp!.divergence.bearish.active).toBe(false);
    expect(resp!.absorption?.detected).toBe(false);
  });
});

describe("strengthGrade", () => {
  it("maps numeric strength to three grades", () => {
    expect(strengthGrade(0.9)).toBe("strong");
    expect(strengthGrade(0.5)).toBe("moderate");
    expect(strengthGrade(0.1)).toBe("weak");
  });
  it("passes through string grades and defaults invalid to moderate", () => {
    expect(strengthGrade("strong")).toBe("strong");
    expect(strengthGrade("weak")).toBe("weak");
    expect(strengthGrade("whatever")).toBe("moderate");
    expect(strengthGrade(undefined)).toBe("moderate");
  });
});

// s6-bugfix：后端引擎 t 为 ISO 字符串（"2026-07-11T04:00:00Z"），直接喂
// lightweight-charts 会按 yyyy-mm-dd 解析崩溃——归一化层必须统一转 unix 秒
describe("normalizeDeltaResponse", () => {
  const isoResp = {
    ok: true,
    symbol: "BTCUSDT",
    timeframe: "4h",
    bars: [
      { t: "2026-07-11T04:00:00Z", delta: 5, cvd: 5, volume: 10 },
      { t: "2026-07-11T08:00:00Z", delta: -2, cvd: 3, volume: 8 },
    ],
    divergence: {
      bullish: {
        active: true,
        strength: "strong",
        anchors: [
          { t: "2026-07-11T04:00:00Z", price: 100, cvd: 5 },
          { t: "2026-07-11T08:00:00Z", price: 95, cvd: 6 },
        ],
      },
      bearish: { active: false },
    },
  } as unknown as DeltaResponse;

  it("converts ISO string t to unix seconds for bars and anchors", () => {
    const norm = normalizeDeltaResponse(isoResp)!;
    expect(norm.bars[0].t).toBe(Date.parse("2026-07-11T04:00:00Z") / 1000);
    expect(norm.bars[1].t).toBe(Date.parse("2026-07-11T08:00:00Z") / 1000);
    expect(typeof norm.bars[0].t).toBe("number");
    expect(norm.divergence.bullish.anchors![0].t).toBe(
      Date.parse("2026-07-11T04:00:00Z") / 1000,
    );
    // 原始对象不被就地修改
    expect(typeof (isoResp.bars[0] as { t: unknown }).t).toBe("string");
  });

  it("keeps numeric t as-is (mock path) and converts ms to seconds", () => {
    const numResp = {
      ...isoResp,
      bars: [
        { t: 1_700_000_000, delta: 1, cvd: 1, volume: 1 },
        { t: 1_700_000_060_000, delta: 1, cvd: 2, volume: 1 }, // 毫秒误传
      ],
      divergence: { bullish: { active: false }, bearish: { active: false } },
    } as unknown as DeltaResponse;
    const norm = normalizeDeltaResponse(numResp)!;
    expect(norm.bars[0].t).toBe(1_700_000_000);
    expect(norm.bars[1].t).toBe(1_700_000_060);
  });

  it("drops unparseable bars and handles null input", () => {
    const badResp = {
      ...isoResp,
      bars: [
        { t: "not-a-date", delta: 1, cvd: 1, volume: 1 },
        { t: "2026-07-11T04:00:00Z", delta: 2, cvd: 3, volume: 1 },
      ],
      divergence: { bullish: { active: false }, bearish: { active: false } },
    } as unknown as DeltaResponse;
    const norm = normalizeDeltaResponse(badResp)!;
    expect(norm.bars).toHaveLength(1);
    expect(normalizeDeltaResponse(null)).toBeNull();
  });
});
