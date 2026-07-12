import { describe, it, expect } from "vitest";
import {
  buildPositionZoneView,
  positionZoneWindow,
  positionZoneToQuery,
  positionZoneFromQuery,
  stripPositionZoneQuery,
  POSITION_ZONE_EXTEND_BARS,
  type PositionZoneParams,
} from "../positionZone";

const longParams: PositionZoneParams = {
  side: "long",
  entry: 63_982.77,
  stopLoss: 62_634.58,
  takeProfit: 66_679.15,
  name: "艾略特波浪",
  tf: "4h",
};

const shortParams: PositionZoneParams = {
  side: "short",
  entry: 63_982.77,
  stopLoss: 64_740.2,
  takeProfit: 61_544.56,
  name: "道氏理论",
  tf: "4h",
};

describe("buildPositionZoneView", () => {
  it("builds long view: profit above entry, risk below", () => {
    const v = buildPositionZoneView(longParams);
    expect(v).not.toBeNull();
    expect(v!.profit.top).toBe(66_679.15);
    expect(v!.profit.bottom).toBe(63_982.77);
    expect(v!.risk.top).toBe(63_982.77);
    expect(v!.risk.bottom).toBe(62_634.58);
    // reward = 2696.38, risk = 1348.19 → 1:2
    expect(v!.rr).toBe(2);
  });

  it("builds short view: profit below entry, risk above (mirrored)", () => {
    const v = buildPositionZoneView(shortParams);
    expect(v).not.toBeNull();
    expect(v!.profit.top).toBe(63_982.77);
    expect(v!.profit.bottom).toBe(61_544.56);
    expect(v!.risk.top).toBe(64_740.2);
    expect(v!.risk.bottom).toBe(63_982.77);
    // reward = 2438.21, risk = 757.43 → 3.22
    expect(v!.rr).toBeCloseTo(3.22, 2);
  });

  it("rejects geometry inconsistent with direction", () => {
    // 多单止损在入场上方
    expect(
      buildPositionZoneView({ ...longParams, stopLoss: 65_000 }),
    ).toBeNull();
    // 多单止盈在入场下方
    expect(
      buildPositionZoneView({ ...longParams, takeProfit: 63_000 }),
    ).toBeNull();
    // 空单止损在入场下方
    expect(
      buildPositionZoneView({ ...shortParams, stopLoss: 63_000 }),
    ).toBeNull();
  });

  it("rejects missing/invalid prices and null input", () => {
    expect(buildPositionZoneView(null)).toBeNull();
    expect(buildPositionZoneView(undefined)).toBeNull();
    expect(
      buildPositionZoneView({ ...longParams, entry: NaN }),
    ).toBeNull();
    expect(
      buildPositionZoneView({ ...longParams, takeProfit: 0 }),
    ).toBeNull();
  });
});

describe("positionZoneWindow", () => {
  it("anchors at last bar and extends right", () => {
    const w = positionZoneWindow(200);
    expect(w).toEqual({ from: 199, to: 199 + POSITION_ZONE_EXTEND_BARS });
  });

  it("honours custom extend and clamps to ≥1", () => {
    expect(positionZoneWindow(100, 5)).toEqual({ from: 99, to: 104 });
    expect(positionZoneWindow(100, 0)).toEqual({ from: 99, to: 100 });
  });

  it("returns null without bars", () => {
    expect(positionZoneWindow(0)).toBeNull();
    expect(positionZoneWindow(NaN)).toBeNull();
  });
});

describe("query round-trip", () => {
  it("encodes and decodes params losslessly", () => {
    const q = positionZoneToQuery(longParams);
    const parsed = positionZoneFromQuery(q);
    expect(parsed).toEqual(longParams);
  });

  it("omits optional name/tf when absent", () => {
    const q = positionZoneToQuery({
      side: "short",
      entry: 100,
      stopLoss: 102,
      takeProfit: 94,
    });
    expect(q.has("pzname")).toBe(false);
    expect(q.has("pztf")).toBe(false);
    const parsed = positionZoneFromQuery(q);
    expect(parsed?.name).toBeUndefined();
    expect(parsed?.tf).toBeUndefined();
  });

  it("returns null on missing or malformed keys", () => {
    expect(positionZoneFromQuery(new URLSearchParams())).toBeNull();
    expect(
      positionZoneFromQuery(new URLSearchParams({ pzside: "long" })),
    ).toBeNull();
    expect(
      positionZoneFromQuery(
        new URLSearchParams({
          pzside: "up", // 非法方向
          pzentry: "100",
          pzsl: "98",
          pztp: "106",
        }),
      ),
    ).toBeNull();
    expect(
      positionZoneFromQuery(
        new URLSearchParams({
          pzside: "long",
          pzentry: "abc", // 非数值
          pzsl: "98",
          pztp: "106",
        }),
      ),
    ).toBeNull();
  });

  it("strips only pz* keys, preserving coexisting sig* params", () => {
    const q = positionZoneToQuery(longParams);
    q.set("sigmarks", "turtle");
    q.set("sigtf", "4h");
    const stripped = stripPositionZoneQuery(q);
    expect(stripped.get("sigmarks")).toBe("turtle");
    expect(stripped.get("sigtf")).toBe("4h");
    expect(stripped.has("pzside")).toBe(false);
    expect(stripped.has("pzentry")).toBe(false);
    expect(stripped.has("pzname")).toBe(false);
  });
});
