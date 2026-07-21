import { describe, expect, it } from "vitest";
import {
  computeIchimoku,
  ichimokuReadout,
  ichimokuTipAt,
  DEFAULT_ICHIMOKU_PARAMS,
  type IchimokuBar,
} from "../ichimoku";

/** 恒定价 K 线：所有中线 = 同一值，便于闭式验证 */
function flatBars(n: number, price: number): IchimokuBar[] {
  return Array.from({ length: n }, () => ({ high: price, low: price, close: price }));
}

/** 线性上涨：bar i 的 high=i+1、low=i、close=i+0.5 → 窗口 w 中线 = i - (w-1)/2 + 0.5 */
function trendBars(n: number): IchimokuBar[] {
  return Array.from({ length: n }, (_, i) => ({ high: i + 1, low: i, close: i + 0.5 }));
}

describe("computeIchimoku", () => {
  it("windows: null until enough bars, exact midline afterwards", () => {
    const bars = trendBars(80);
    const r = computeIchimoku(bars);

    // Tenkan(9)：i<8 为 null；i≥8 = ((i+1) + (i-8)) / 2 = i - 3.5
    expect(r.tenkan[7]).toBeNull();
    expect(r.tenkan[8]).toBeCloseTo(4.5);
    expect(r.tenkan[50]).toBeCloseTo(46.5);
    // Kijun(26)：i<25 null；i≥25 = i - 12
    expect(r.kijun[24]).toBeNull();
    expect(r.kijun[25]).toBeCloseTo(13);
    expect(r.kijun[60]).toBeCloseTo(48);
    // SpanA 前移 26：显示 i = 计算 i0+26，值 = (tenkan+kijun)/2 = i0 - 7.75
    // 显示索引 51 ← 计算索引 25：25-7.75 = 17.25
    expect(r.spanA[50]).toBeNull(); // 计算索引 24 时 kijun 还未就绪
    expect(r.spanA[51]).toBeCloseTo(17.25);
    // SpanB(52) 前移 26：计算 i≥51 才有值 → 显示 i≥77；值 = i0 - 25.5 + 0.5 = i0-25
    // 显示 77 ← 计算 51：(52 + 0)/2 = 26
    expect(r.spanB[76]).toBeNull();
    expect(r.spanB[77]).toBeCloseTo(26);
    // Chikou 后移 26：显示 i = 计算 i0−26；显示 0 ← close[26] = 26.5
    expect(r.chikou[0]).toBeCloseTo(26.5);
    expect(r.chikou[79 - 26]).toBeCloseTo(79.5);
    expect(r.chikou[79 - 25]).toBeNull(); // 尾部 26 根无迟行
  });

  it("flat market: all five lines equal price, cloud has zero thickness", () => {
    const bars = flatBars(120, 500);
    const r = computeIchimoku(bars);
    const i = 119;
    expect(r.tenkan[i]).toBe(500);
    expect(r.kijun[i]).toBe(500);
    expect(r.spanA[i]).toBe(500);
    expect(r.spanB[i]).toBe(500);
    expect(r.chikou[0]).toBe(500);
  });

  it("future cloud extends displacement bars past the last candle", () => {
    const bars = trendBars(120);
    const { displacement } = DEFAULT_ICHIMOKU_PARAMS;
    const r = computeIchimoku(bars);
    expect(r.futureStart).toBe(120);
    const future = r.cloud.filter((p) => p.logical >= 120);
    // 未来段应有 displacement 根（52 窗口在 n=120 时已全部就绪）
    expect(future.length).toBe(displacement);
    expect(future[0].logical).toBe(120);
    expect(future[future.length - 1].logical).toBe(120 + displacement - 1);
    // logical 严格递增
    for (let j = 1; j < r.cloud.length; j++) {
      expect(r.cloud[j].logical).toBeGreaterThan(r.cloud[j - 1].logical);
    }
  });

  it("uptrend produces green cloud (SpanA > SpanB)", () => {
    const bars = trendBars(150);
    const r = computeIchimoku(bars);
    const last = r.cloud[r.cloud.length - 1];
    expect(last.a).toBeGreaterThan(last.b); // 上涨中 A（短窗中线）高于 B（长窗中线）
  });

  it("short input yields empty cloud without crashing", () => {
    const r = computeIchimoku(trendBars(10));
    expect(r.cloud).toEqual([]);
    expect(r.futureStart).toBeNull();
    expect(computeIchimoku([]).cloud).toEqual([]);
  });
});

describe("ichimokuReadout", () => {
  it("price above cloud → bullish; below → bearish; inside → neutral", () => {
    // 用平盘构造云在 500，再改末柱 close 制造三种相对位置
    const mk = (lastClose: number) => {
      const bars = flatBars(120, 500);
      bars[119] = { high: Math.max(lastClose, 500), low: Math.min(lastClose, 500), close: lastClose };
      const r = computeIchimoku(bars);
      return ichimokuReadout(bars, r)!;
    };
    expect(mk(600).tone).toBe("bullish");
    expect(mk(600).text).toContain("云上");
    expect(mk(400).tone).toBe("bearish");
    expect(mk(400).text).toContain("云下");
    expect(mk(500).tone).toBe("neutral");
    expect(mk(500).text).toContain("观望");
  });

  it("returns null when cloud not ready", () => {
    const bars = trendBars(30); // 52+26 窗口远未就绪
    expect(ichimokuReadout(bars, computeIchimoku(bars))).toBeNull();
  });
});

describe("ichimokuTipAt", () => {
  it("joins available line values, null when nothing at index", () => {
    const bars = flatBars(120, 500);
    const r = computeIchimoku(bars);
    expect(ichimokuTipAt(r, 119)).toContain("转换 500");
    expect(ichimokuTipAt(r, 119)).toContain("先行B 500");
    const empty = computeIchimoku(trendBars(5));
    expect(ichimokuTipAt(empty, 2)).toBeNull();
  });
});
