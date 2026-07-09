import { describe, it, expect } from "vitest";
import { planToOverlay, PLAN_COLORS, fmt } from "../tradePlan";
import type { ConsensusTradePlan } from "../../api/client";

// A well-formed LONG consensus plan: SL < entry zone < TP1 < TP2.
const longPlan: ConsensusTradePlan = {
  entry_zone: [100, 102],
  stop_loss: 96,
  take_profit_1: 108,
  take_profit_2: 114,
  rr: 2.5,
  position_pct: 15,
  basis: ["trend", "momentum"],
};

// A well-formed SHORT consensus plan: TP2 < TP1 < entry zone < SL.
const shortPlan: ConsensusTradePlan = {
  entry_zone: [102, 100],   // 后端可能不排序，消费侧要防御性排序
  stop_loss: 106,
  take_profit_1: 94,
  take_profit_2: 88,
  rr: 2.0,
  position_pct: 10,
  basis: ["turtle"],
};

describe("planToOverlay mapping", () => {
  it("maps a full plan to 5 hlines (entry ×2, SL, TP1, TP2) with the agreed styling", () => {
    const { hlines } = planToOverlay(longPlan);
    expect(hlines).toHaveLength(5);

    const entries = hlines.filter((l) => l.color === PLAN_COLORS.entry);
    const stops = hlines.filter((l) => l.color === PLAN_COLORS.stop);
    const profits = hlines.filter((l) => l.color === PLAN_COLORS.profit);
    expect(entries).toHaveLength(2);
    expect(stops).toHaveLength(1);
    expect(profits).toHaveLength(2);

    // 入场区两条蓝色虚线，价格排序后与 entry_zone 一致
    expect(entries.every((l) => l.style === "dashed")).toBe(true);
    expect(entries.map((l) => l.price).sort((a, b) => a - b)).toEqual([100, 102]);
    expect(entries.every((l) => l.label?.includes("入场区"))).toBe(true);

    // 止损红色实线；TP1 绿色实线、TP2 绿色虚线
    expect(stops[0].style).toBe("solid");
    expect(stops[0].label).toContain("止损 SL");
    expect(stops[0].label).toContain("96.00");
    const tp1 = profits.find((l) => l.label?.includes("TP1"));
    const tp2 = profits.find((l) => l.label?.includes("TP2"));
    expect(tp1?.style).toBe("solid");
    expect(tp2?.style).toBe("dashed");
    expect(tp1?.label).toContain("108.00");
  });

  it("direction self-consistency for a long plan: SL < entry zone < TP1 (<= TP2)", () => {
    const { hlines } = planToOverlay(longPlan);
    const sl = hlines.find((l) => l.label?.includes("SL"))!.price;
    const zone = hlines.filter((l) => l.color === PLAN_COLORS.entry).map((l) => l.price);
    const tp1 = hlines.find((l) => l.label?.includes("TP1"))!.price;
    const tp2 = hlines.find((l) => l.label?.includes("TP2"))!.price;

    expect(sl).toBeLessThan(Math.min(...zone));
    expect(tp1).toBeGreaterThan(Math.max(...zone));
    expect(tp2).toBeGreaterThanOrEqual(tp1);
  });

  it("degrades gracefully: null / missing plan → empty overlay (renders nothing)", () => {
    expect(planToOverlay(null).hlines).toHaveLength(0);
    expect(planToOverlay(undefined).hlines).toHaveLength(0);
  });

  it("skips invalid prices but keeps the rest of the plan drawable", () => {
    const partial: ConsensusTradePlan = {
      entry_zone: [NaN, 102] as [number, number], // one leg invalid → single 入场 line
      stop_loss: 0,                                // invalid (<=0) → dropped
      take_profit_1: 108,
      take_profit_2: null,
    };
    const { hlines } = planToOverlay(partial);
    expect(hlines.some((l) => l.label?.includes("SL"))).toBe(false);
    expect(hlines.some((l) => l.label?.includes("TP2"))).toBe(false);
    expect(hlines.filter((l) => l.color === PLAN_COLORS.entry)).toHaveLength(1);
    // TP2 缺失 → 单条止盈线按退化规则标「止盈 TP」（不带序号）
    expect(hlines.some((l) => l.label?.includes("止盈 TP "))).toBe(true);
  });

  it("collapses a degenerate entry zone (lo == hi) into a single entry line", () => {
    const flat: ConsensusTradePlan = {
      entry_zone: [100, 100],
      stop_loss: 95,
      take_profit_1: 110,
    };
    const { hlines } = planToOverlay(flat);
    const entries = hlines.filter((l) => l.color === PLAN_COLORS.entry);
    expect(entries).toHaveLength(1);
    expect(entries[0].price).toBe(100);
  });
});

describe("short-plan direction self-consistency (audit a4#3)", () => {
  it("maps a short plan with SL above and TPs below the entry zone", () => {
    const { hlines } = planToOverlay(shortPlan);
    expect(hlines).toHaveLength(5);

    const entries = hlines.filter((l) => l.color === PLAN_COLORS.entry);
    const sl = hlines.find((l) => l.label?.includes("SL"))!;
    const tp1 = hlines.find((l) => l.label?.includes("TP1"))!;
    const tp2 = hlines.find((l) => l.label?.includes("TP2"))!;

    // 入场区仍按升序两条（消费侧防御性排序处理了 [102,100] 逆序输入）
    expect(entries.map((l) => l.price).sort((a, b) => a - b)).toEqual([100, 102]);
    // 空单几何：SL 在入场区上方，TP 全在下方，TP2 比 TP1 更远
    expect(sl.price).toBeGreaterThan(Math.max(...entries.map((l) => l.price)));
    expect(tp1.price).toBeLessThan(Math.min(...entries.map((l) => l.price)));
    expect(tp2.price).toBeLessThan(tp1.price);
    // 颜色/样式不因方向而变：止损红、止盈绿、入场蓝
    expect(sl.color).toBe(PLAN_COLORS.stop);
    expect(tp1.color).toBe(PLAN_COLORS.profit);
    expect(tp2.color).toBe(PLAN_COLORS.profit);
    expect(sl.label).toContain("106.00");
  });
});

describe("TP degeneration (audit a4#4)", () => {
  it("TP1 == TP2 renders a single 「止盈 TP」 line (no overlapping duplicates)", () => {
    const { hlines } = planToOverlay({ ...longPlan, take_profit_2: 108 });
    const profits = hlines.filter((l) => l.color === PLAN_COLORS.profit);
    expect(profits).toHaveLength(1);
    expect(profits[0].label).toContain("止盈 TP ");
    expect(profits[0].label).not.toContain("TP1");
    expect(profits[0].style).toBe("solid");
  });

  it("take_profit_2 = null renders a single TP line as well", () => {
    const { hlines } = planToOverlay({ ...longPlan, take_profit_2: null });
    const profits = hlines.filter((l) => l.color === PLAN_COLORS.profit);
    expect(profits).toHaveLength(1);
    expect(profits[0].price).toBe(108);
    expect(profits[0].label).toContain("止盈 TP ");
  });
});

describe("dynamic price precision (audit a4#2)", () => {
  it("fmt keeps 2 decimals ≥1, 4 decimals in [0.01,1), 6 significant digits below", () => {
    expect(fmt(68123.456)).toBe("68123.46");
    expect(fmt(1)).toBe("1.00");
    expect(fmt(0.1234567)).toBe("0.1235");
    expect(fmt(0.01)).toBe("0.0100");
    expect(fmt(0.00001234567)).toBe("0.0000123457");
  });

  it("micro-price plans keep non-zero labels (PEPE-like)", () => {
    const micro: ConsensusTradePlan = {
      entry_zone: [0.00001, 0.0000102],
      stop_loss: 0.0000095,
      take_profit_1: 0.000011,
      take_profit_2: 0.000012,
    };
    const { hlines } = planToOverlay(micro);
    expect(hlines).toHaveLength(5);
    for (const l of hlines) {
      expect(l.label).not.toMatch(/ 0\.00$/);
      expect(l.label).toMatch(/0\.0000/);
    }
  });
});
