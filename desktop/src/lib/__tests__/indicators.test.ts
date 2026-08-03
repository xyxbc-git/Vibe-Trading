import { describe, it, expect } from "vitest";
import { calcEMA, calcMACD } from "../indicators";

describe("calcEMA", () => {
  it("matches hand-computed values (period 3, SMA seed then recursion)", () => {
    // k = 2/(3+1) = 0.5；seed = SMA(1,2,3) = 2
    // ema[3] = 4*0.5 + 2*0.5 = 3；ema[4] = 5*0.5 + 3*0.5 = 4
    expect(calcEMA([1, 2, 3, 4, 5], 3)).toEqual([null, null, 2, 3, 4]);
  });

  it("stays flat on a constant series", () => {
    const out = calcEMA(new Array(30).fill(7), 12);
    expect(out.slice(0, 11)).toEqual(new Array(11).fill(null));
    for (const v of out.slice(11)) expect(v).toBeCloseTo(7, 12);
  });
});

describe("calcMACD", () => {
  it("matches hand-computed values on small params (2/3/2)", () => {
    // closes = 1..6
    // EMA2 = [null, 1.5, 2.5, 3.5, 4.5, 5.5]；EMA3 = [null, null, 2, 3, 4, 5]
    // DIF  = [null, null, 0.5, 0.5, 0.5, 0.5]
    // DEA  = EMA2(非空 DIF)：首个有效值在第二个非空 DIF 处 = 0.5，之后恒 0.5
    // HIST = DIF - DEA = 0
    const { dif, dea, hist } = calcMACD([1, 2, 3, 4, 5, 6], 2, 3, 2);
    expect(dif).toEqual([null, null, 0.5, 0.5, 0.5, 0.5]);
    expect(dea).toEqual([null, null, null, 0.5, 0.5, 0.5]);
    expect(hist).toEqual([null, null, null, 0, 0, 0]);
  });

  it("respects standard 12/26/9 warmup boundaries", () => {
    const closes = Array.from({ length: 60 }, (_, i) => 100 + Math.sin(i / 5) * 10);
    const { dif, dea, hist } = calcMACD(closes);
    // DIF 首个有效值 = EMA26 预热完成处（下标 25）
    expect(dif[24]).toBeNull();
    expect(dif[25]).not.toBeNull();
    // DEA 在 DIF 有效后再预热 9 根（下标 25+8=33）
    expect(dea[32]).toBeNull();
    expect(dea[33]).not.toBeNull();
    expect(hist[32]).toBeNull();
    expect(hist[33]).toBeCloseTo((dif[33] as number) - (dea[33] as number), 12);
  });

  it("converges to the analytic steady state on a linear ramp", () => {
    // x_t = t 时 EMA_p 的稳态滞后为 (p-1)/2，
    // 故 DIF → (26-1)/2 - (12-1)/2 = 7，HIST → 0（对标标准公式的解析检验）
    const closes = Array.from({ length: 400 }, (_, i) => i);
    const { dif, hist } = calcMACD(closes);
    expect(dif[399] as number).toBeCloseTo(7, 3);
    expect(hist[399] as number).toBeCloseTo(0, 3);
  });

  it("returns all-null arrays when the series is shorter than the slow period", () => {
    const { dif, dea, hist } = calcMACD([1, 2, 3]);
    expect(dif).toEqual([null, null, null]);
    expect(dea).toEqual([null, null, null]);
    expect(hist).toEqual([null, null, null]);
  });
});
