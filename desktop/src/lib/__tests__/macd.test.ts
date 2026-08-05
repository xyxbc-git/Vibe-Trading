import { describe, expect, it } from "vitest";
import { computeMacd } from "../macd";

describe("computeMacd", () => {
  it("空输入返回空结果", () => {
    const r = computeMacd([]);
    expect(r.points).toEqual([]);
    expect(r.warmupIndex).toBe(-1);
  });

  it("常数序列 DIF/DEA/HIST 恒为 0", () => {
    const r = computeMacd(new Array(60).fill(100));
    for (const p of r.points) {
      expect(p.dif).toBeCloseTo(0, 10);
      expect(p.dea).toBeCloseTo(0, 10);
      expect(p.hist).toBeCloseTo(0, 10);
    }
    expect(r.warmupIndex).toBe(33);
  });

  it("EMA 递推与手算一致（fast=2, slow=4, signal=2 小参数便于手核）", () => {
    // closes: [1, 2, 3]
    // EMA2(k=2/3):  1, 1+2/3*(2-1)=1.6667, 1.6667+2/3*(3-1.6667)=2.5556
    // EMA4(k=2/5):  1, 1.4,               1.4+0.4*(3-1.4)=2.04
    // DIF:          0, 0.2667,            0.5156
    // DEA=EMA2(DIF):0, 0+2/3*0.2667=0.1778, 0.1778+2/3*(0.5156-0.1778)=0.4030
    const r = computeMacd([1, 2, 3], 2, 4, 2);
    expect(r.points[1].dif).toBeCloseTo(0.26667, 4);
    expect(r.points[2].dif).toBeCloseTo(0.51556, 4);
    expect(r.points[2].dea).toBeCloseTo(0.40296, 4);
    expect(r.points[2].hist).toBeCloseTo(0.51556 - 0.40296, 4);
  });

  it("上涨趋势中 DIF 为正且高于 DEA（柱体为正）", () => {
    const closes = Array.from({ length: 80 }, (_, i) => 100 + i * 2);
    const r = computeMacd(closes);
    const tail = r.points[r.points.length - 1];
    expect(tail.dif).toBeGreaterThan(0);
    expect(tail.dif).toBeGreaterThanOrEqual(tail.dea);
    expect(tail.hist).toBeGreaterThanOrEqual(0);
  });

  it("warmupIndex 输入不足时为 -1", () => {
    expect(computeMacd(new Array(20).fill(1)).warmupIndex).toBe(-1);
  });
});
