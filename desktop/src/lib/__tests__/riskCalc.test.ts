import { describe, it, expect } from "vitest";
import {
  calcRisk,
  marginPctForRisk,
  maxSafeLeverage,
  LOSS_HARD_CAP_PCT,
} from "../riskCalc";

// 合约风控计算器公式锁定：样例数字与任务验收口径一致
// （维持保证金率缺省 0.5%，强平安全系数 1.5）。
describe("calcRisk", () => {
  it("样例1：本金1000/比例10%/杠杆20x/止损1.5%/止盈3% → 无危险项", () => {
    const r = calcRisk({
      capital: 1000,
      pctOfCapital: 10,
      leverage: 20,
      direction: "long",
      entryPrice: 50_000,
      slPct: 1.5,
      tpPct: 3,
    });
    expect(r).not.toBeNull();
    // 保证金 = 1000×10% = 100；名义 = 100×20 = 2000
    expect(r!.marginUsdt).toBe(100);
    expect(r!.notionalUsdt).toBe(2000);
    expect(r!.qtyCoin).toBeCloseTo(0.04, 8);
    // 强平距离 = 100/20 − 0.5 = 4.5%；与止损差 3%（安全）
    expect(r!.liqDistPct).toBe(4.5);
    expect(r!.slLiqGapPct).toBe(3);
    expect(r!.liqPrice).toBeCloseTo(50_000 * (1 - 0.045), 4);
    // 止损亏损 = 2000×1.5% = 30 U = 本金 3%
    expect(r!.slLossUsdt).toBe(30);
    expect(r!.slLossPctOfCapital).toBe(3);
    // 止盈落袋 = 2000×3% = 60 U；RR = 3/1.5 = 2
    expect(r!.tpProfitUsdt).toBe(60);
    expect(r!.rr).toBe(2);
    // 安全杠杆上限 = floor(100/(1.5×1.5+0.5)) = 36
    expect(r!.maxSafeLeverage).toBe(36);
    expect(r!.dangers).toHaveLength(0);
  });

  it("样例2：本金1000/比例50%/杠杆100x/止损2% → 三项危险全触发", () => {
    const r = calcRisk({
      capital: 1000,
      pctOfCapital: 50,
      leverage: 100,
      direction: "long",
      entryPrice: 50_000,
      slPct: 2,
      tpPct: 4,
    });
    expect(r).not.toBeNull();
    // 强平距离 = 100/100 − 0.5 = 0.5% < 止损 2% → 先爆仓后止损
    expect(r!.liqDistPct).toBe(0.5);
    expect(r!.slLiqGapPct).toBe(-1.5);
    // 止损亏损 = 名义50000×2% = 1000 U = 本金 100% > 硬上限
    expect(r!.slLossUsdt).toBe(1000);
    expect(r!.slLossPctOfCapital).toBeGreaterThan(LOSS_HARD_CAP_PCT);
    const kinds = r!.dangers.map((d) => d.kind).sort();
    expect(kinds).toEqual([
      "leverage-over-safe",
      "loss-over-threshold",
      "sl-beyond-liq",
    ]);
  });

  it("空头方向：止损价在上方、强平价在上方", () => {
    const r = calcRisk({
      capital: 500,
      pctOfCapital: 20,
      leverage: 10,
      direction: "short",
      entryPrice: 3000,
      slPct: 2,
      tpPct: 4,
    });
    expect(r).not.toBeNull();
    expect(r!.slPrice).toBeCloseTo(3000 * 1.02, 8);
    expect(r!.tpPrice).toBeCloseTo(3000 * 0.96, 8);
    // 强平距离 = 10 − 0.5 = 9.5%（价格上涨方向）
    expect(r!.liqPrice).toBeCloseTo(3000 * 1.095, 4);
  });

  it("非法输入返回 null", () => {
    const base = {
      capital: 1000,
      pctOfCapital: 10,
      leverage: 20,
      direction: "long" as const,
      entryPrice: 50_000,
      slPct: 1.5,
      tpPct: 3,
    };
    expect(calcRisk({ ...base, capital: 0 })).toBeNull();
    expect(calcRisk({ ...base, entryPrice: NaN })).toBeNull();
    expect(calcRisk({ ...base, slPct: -1 })).toBeNull();
    expect(calcRisk({ ...base, tpPct: 0 })).toBeNull();
  });
});

describe("marginPctForRisk（风险档反推下单比例）", () => {
  it("风险2%/杠杆20x/止损1.5% → 比例≈6.67%，闭环校验亏损=2%本金", () => {
    const pct = marginPctForRisk(2, 20, 1.5);
    expect(pct).toBeCloseTo(6.67, 2);
    // 闭环：本金1000×6.67% = 66.7 保证金 ×20 = 1334 名义 ×1.5% ≈ 20 U ≈ 2% 本金
    const loss = ((1000 * pct!) / 100) * 20 * (1.5 / 100);
    expect(loss / 1000).toBeCloseTo(0.02, 3);
  });

  it("反推结果夹紧到 100% 上限", () => {
    // 风险5%/杠杆1x/止损0.5% → 理论 1000%，夹紧 100
    expect(marginPctForRisk(5, 1, 0.5)).toBe(100);
  });

  it("非法输入返回 null", () => {
    expect(marginPctForRisk(0, 20, 1.5)).toBeNull();
    expect(marginPctForRisk(2, NaN, 1.5)).toBeNull();
  });
});

describe("maxSafeLeverage", () => {
  it("止损1.5% → 36x；止损4% → 15x", () => {
    expect(maxSafeLeverage(1.5)).toBe(36);
    expect(maxSafeLeverage(4)).toBe(15);
  });

  it("非法止损回退 125x；极大止损夹到 1x", () => {
    expect(maxSafeLeverage(NaN)).toBe(125);
    expect(maxSafeLeverage(80)).toBe(1);
  });
});
