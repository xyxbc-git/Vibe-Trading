import { describe, it, expect } from "vitest";
import {
  planToZones,
  planRR,
  zoneTimeWindow,
  ZONE_LOOKBACK_BARS,
  ZONE_EXTEND_BARS,
  type TradeZone,
} from "../tradeZones";
import { PLAN_COLORS } from "../tradePlan";
import type { ConsensusTradePlan } from "../../api/client";

// 多单：SL < 入场区 < TP1 < TP2
const longPlan: ConsensusTradePlan = {
  entry_zone: [100, 102],
  stop_loss: 96,
  take_profit_1: 108,
  take_profit_2: 114,
  rr: 2.5,
  position_pct: 15,
};

// 空单：TP2 < TP1 < 入场区 < SL（entry_zone 逆序，消费侧防御性排序）
const shortPlan: ConsensusTradePlan = {
  entry_zone: [102, 100],
  stop_loss: 106,
  take_profit_1: 94,
  take_profit_2: 88,
  rr: 2.0,
};

const byKind = (zones: TradeZone[], kind: TradeZone["kind"]) =>
  zones.find((z) => z.kind === kind);

describe("planToZones geometry", () => {
  it("long plan: stop band below entry, profit band above, TP2 extension on top", () => {
    const zones = planToZones(longPlan, "bullish");
    expect(zones).toHaveLength(4);

    const entry = byKind(zones, "entry")!;
    const stop = byKind(zones, "stop")!;
    const profit = byKind(zones, "profit")!;
    const profit2 = byKind(zones, "profit2")!;

    expect([entry.bottom, entry.top]).toEqual([100, 102]);
    expect([stop.bottom, stop.top]).toEqual([96, 100]);   // SL → 入场下沿
    expect([profit.bottom, profit.top]).toEqual([102, 108]); // 入场上沿 → TP1
    expect([profit2.bottom, profit2.top]).toEqual([108, 114]); // TP1 → TP2

    // 颜色映射与计划线同源：入场蓝 / 止损红 / 止盈绿
    expect(entry.color).toBe(PLAN_COLORS.entry);
    expect(stop.color).toBe(PLAN_COLORS.stop);
    expect(profit.color).toBe(PLAN_COLORS.profit);
    expect(profit2.color).toBe(PLAN_COLORS.profit);
    // TP2 延伸带比主带更淡
    expect(profit2.fillAlpha).toBeLessThan(profit.fillAlpha);
  });

  it("short plan mirrors: stop band above entry, profit bands below", () => {
    const zones = planToZones(shortPlan, "bearish");
    expect(zones).toHaveLength(4);

    const entry = byKind(zones, "entry")!;
    const stop = byKind(zones, "stop")!;
    const profit = byKind(zones, "profit")!;
    const profit2 = byKind(zones, "profit2")!;

    expect([entry.bottom, entry.top]).toEqual([100, 102]); // 逆序输入被排序
    expect([stop.bottom, stop.top]).toEqual([102, 106]);   // 入场上沿 → SL（上方）
    expect([profit.bottom, profit.top]).toEqual([94, 100]); // TP1 → 入场下沿（下方）
    expect([profit2.bottom, profit2.top]).toEqual([88, 94]); // TP2 → TP1
  });

  it("all zones satisfy top > bottom (renderer invariant)", () => {
    for (const z of [...planToZones(longPlan), ...planToZones(shortPlan)]) {
      expect(z.top).toBeGreaterThan(z.bottom);
    }
  });
});

describe("planToZones degradation", () => {
  it("null / missing plan → no zones", () => {
    expect(planToZones(null)).toHaveLength(0);
    expect(planToZones(undefined)).toHaveLength(0);
  });

  it("no valid entry zone → no zones at all (risk/reward segments need an anchor)", () => {
    const p: ConsensusTradePlan = {
      entry_zone: [NaN, 0] as [number, number],
      stop_loss: 96,
      take_profit_1: 108,
    };
    expect(planToZones(p)).toHaveLength(0);
  });

  it("degenerate entry zone (lo == hi): entry band skipped, stop/profit still drawn", () => {
    const p: ConsensusTradePlan = {
      entry_zone: [100, 100],
      stop_loss: 96,
      take_profit_1: 108,
    };
    const zones = planToZones(p);
    expect(byKind(zones, "entry")).toBeUndefined();
    expect(byKind(zones, "stop")).toBeDefined();
    expect(byKind(zones, "profit")).toBeDefined();
  });

  it("invalid SL dropped; TP1 missing falls back to TP2 as the main profit band", () => {
    const p: ConsensusTradePlan = {
      entry_zone: [100, 102],
      stop_loss: 0,
      take_profit_1: NaN as unknown as number,
      take_profit_2: 114,
    };
    const zones = planToZones(p, "bullish");
    expect(byKind(zones, "stop")).toBeUndefined();
    const profit = byKind(zones, "profit")!;
    expect([profit.bottom, profit.top]).toEqual([102, 114]);
    expect(byKind(zones, "profit2")).toBeUndefined();
  });

  it("TP1 == TP2 draws a single profit band (no zero-height extension)", () => {
    const zones = planToZones({ ...longPlan, take_profit_2: 108 });
    expect(byKind(zones, "profit")).toBeDefined();
    expect(byKind(zones, "profit2")).toBeUndefined();
  });
});

describe("side resolution without explicit side field", () => {
  it("derives short from SL above entry even when direction hint is missing", () => {
    const zones = planToZones(shortPlan);
    const stop = byKind(zones, "stop")!;
    expect(stop.bottom).toBeGreaterThanOrEqual(102); // 止损带在入场区上方 → 空单几何
  });

  it("explicit side field wins over geometry-ambiguous plans", () => {
    const p: ConsensusTradePlan = {
      side: "short",
      entry_zone: [100, 102],
      stop_loss: 106,
      take_profit_1: 94,
    };
    const zones = planToZones(p, "bullish"); // direction 提示与 side 冲突时以 side 为准
    const stop = byKind(zones, "stop")!;
    expect(stop.top).toBe(106);
    expect(stop.bottom).toBe(102);
  });
});

describe("zoneTimeWindow (time-anchored rectangle bounds)", () => {
  it("covers the last LOOKBACK bars and extends EXTEND bars past the latest bar", () => {
    const w = zoneTimeWindow(200)!;
    expect(w.from).toBe(200 - ZONE_LOOKBACK_BARS); // 含 from，共 LOOKBACK 根
    expect(w.to).toBe(199 + ZONE_EXTEND_BARS);     // 越过最后一根向右延伸
    expect(w.to).toBeGreaterThan(w.from);
  });

  it("clamps the left edge to bar 0 when history is shorter than the lookback", () => {
    const w = zoneTimeWindow(10)!;
    expect(w.from).toBe(0);
    expect(w.to).toBe(9 + ZONE_EXTEND_BARS);
  });

  it("returns null when there are no bars to anchor to", () => {
    expect(zoneTimeWindow(0)).toBeNull();
    expect(zoneTimeWindow(-5)).toBeNull();
    expect(zoneTimeWindow(NaN)).toBeNull();
  });

  it("honors custom lookback/extend overrides", () => {
    const w = zoneTimeWindow(100, 10, 3)!;
    expect(w.from).toBe(90);
    expect(w.to).toBe(102);
  });
});

describe("RR + tooltip", () => {
  it("uses backend rr when present", () => {
    expect(planRR(longPlan)).toBe(2.5);
  });

  it("derives rr from entry mid / SL / TP1 when backend rr missing", () => {
    const p: ConsensusTradePlan = {
      entry_zone: [100, 102],
      stop_loss: 96,
      take_profit_1: 111,
    };
    // mid=101, risk=5, reward=10 → rr=2
    expect(planRR(p)).toBe(2);
  });

  it("returns null when rr cannot be derived", () => {
    expect(planRR(null)).toBeNull();
    expect(planRR({ entry_zone: [100, 102] } as ConsensusTradePlan)).toBeNull();
  });

  it("every zone tooltip carries direction and RR text", () => {
    for (const z of planToZones(longPlan, "bullish")) {
      expect(z.tooltip).toContain("盈亏比 2.5");
      expect(z.tooltip).toContain("方向 多");
      expect(z.label.length).toBeGreaterThan(0);
    }
    for (const z of planToZones(shortPlan, "bearish")) {
      expect(z.tooltip).toContain("方向 空");
    }
  });
});
