import { describe, it, expect } from "vitest";
import {
  riskReward,
  deriveLeverage,
  baseAsset,
  fmtQty,
  extractCardMetrics,
} from "../positionMetrics";

describe("riskReward", () => {
  it("computes long R:R = (tp-entry)/(entry-sl)", () => {
    // 多头：入场 100，止损 98（risk=2），止盈 106（reward=6）→ 1:3
    expect(riskReward("long", 100, 98, 106)).toBe(3);
  });

  it("computes short R:R with inverted legs", () => {
    // 空头：入场 100，止损 102（risk=2），止盈 94（reward=6）→ 1:3
    expect(riskReward("short", 100, 102, 94)).toBe(3);
  });

  it("rounds to 2 decimals", () => {
    // risk=3, reward=5 → 1.6666… → 1.67
    expect(riskReward("long", 100, 97, 105)).toBe(1.67);
  });

  it("returns null when any leg is missing", () => {
    expect(riskReward("long", 100, undefined, 106)).toBeNull();
    expect(riskReward("long", 100, 98, null)).toBeNull();
    expect(riskReward("long", undefined, 98, 106)).toBeNull();
  });

  it("returns null for direction-inconsistent prices", () => {
    // 多头止损在入场上方 → risk ≤ 0，不可计算
    expect(riskReward("long", 100, 103, 106)).toBeNull();
    // 多头止盈在入场下方 → reward ≤ 0
    expect(riskReward("long", 100, 98, 99)).toBeNull();
    // 空头止损在入场下方 → risk ≤ 0
    expect(riskReward("short", 100, 97, 94)).toBeNull();
  });
});

describe("deriveLeverage", () => {
  it("prefers plan leverage when present", () => {
    expect(deriveLeverage(100, 13_000, 130)).toBe(100);
  });

  it("falls back to notional/margin ratio rounded to 1 decimal", () => {
    expect(deriveLeverage(null, 1_000, 180)).toBe(5.6);
  });

  it("defaults to 1x (spot full-cash) without plan context", () => {
    expect(deriveLeverage(null, null, 100)).toBe(1);
    expect(deriveLeverage(undefined, undefined, undefined)).toBe(1);
  });

  it("ignores notional < margin (invalid ratio)", () => {
    expect(deriveLeverage(null, 50, 100)).toBe(1);
  });
});

describe("baseAsset", () => {
  it("strips USDT suffix", () => {
    expect(baseAsset("BTCUSDT")).toBe("BTC");
    expect(baseAsset("ETHUSDT")).toBe("ETH");
  });

  it("keeps non-USDT symbols as-is", () => {
    expect(baseAsset("BTC")).toBe("BTC");
    expect(baseAsset("USDT")).toBe("USDT");
  });
});

describe("fmtQty", () => {
  it("adapts precision by magnitude", () => {
    expect(fmtQty(12_345.6)).toBe("12,346");
    expect(fmtQty(12.3456)).toBe("12.35");
    expect(fmtQty(0.001234567)).toBe("0.001235");
  });

  it("renders missing/invalid as em-dash", () => {
    expect(fmtQty(undefined)).toBe("—");
    expect(fmtQty(null)).toBe("—");
    expect(fmtQty(0)).toBe("—");
    expect(fmtQty(NaN)).toBe("—");
  });
});

describe("extractCardMetrics", () => {
  it("uses plan snapshot columns when present (leveraged user-created order)", () => {
    const m = extractCardMetrics({
      qty: 0.0013,
      entry_price: 100_000,
      plan_leverage: 100,
      plan_margin_usdt: 130,
      plan_notional_usdt: 13_000,
      pnl_usdt: 4.2,
    });
    expect(m.qty).toBe(0.0013);
    expect(m.marginUsdt).toBe(130);
    expect(m.notionalUsdt).toBe(13_000);
    expect(m.leverage).toBe(100);
    expect(m.pnlUsdt).toBe(4.2);
  });

  it("falls back to spot cost qty×entry at 1x without plan context", () => {
    const m = extractCardMetrics({ qty: 0.5, entry_price: 3_000 });
    expect(m.marginUsdt).toBe(1_500);
    expect(m.notionalUsdt).toBe(1_500);
    expect(m.leverage).toBe(1);
    expect(m.pnlUsdt).toBeUndefined();
  });

  it("tolerates string-typed numeric fields (SQLite dynamic typing)", () => {
    const m = extractCardMetrics({
      qty: "2",
      entry_price: "150.5",
      plan_leverage: "10",
    });
    expect(m.qty).toBe(2);
    expect(m.marginUsdt).toBe(301);
    expect(m.leverage).toBe(10);
  });

  it("returns undefined metrics for empty row", () => {
    const m = extractCardMetrics({});
    expect(m.qty).toBeUndefined();
    expect(m.marginUsdt).toBeUndefined();
    expect(m.notionalUsdt).toBeUndefined();
    expect(m.leverage).toBe(1);
  });

  it("extracts T1.7 companion fields (sl/tp distance + warn + plan status)", () => {
    const m = extractCardMetrics({
      qty: 1,
      entry_price: 100,
      sl_dist_pct: 7.22,
      tp_dist_pct: 23.71,
      sl_remaining_pct: 70,
      sl_warn: false,
      plan_status: "valid",
    });
    expect(m.slDistPct).toBe(7.22);
    expect(m.tpDistPct).toBe(23.71);
    expect(m.slRemainingPct).toBe(70);
    expect(m.slWarn).toBe(false);
    expect(m.planStatus).toBe("valid");
  });

  it("passes through reversed/neutral plan status and warn flag", () => {
    expect(extractCardMetrics({ plan_status: "reversed", sl_warn: true }).planStatus).toBe(
      "reversed",
    );
    expect(extractCardMetrics({ plan_status: "reversed", sl_warn: true }).slWarn).toBe(true);
    expect(extractCardMetrics({ plan_status: "neutral" }).planStatus).toBe("neutral");
  });

  it("leaves companion fields undefined for manual orders / legacy rows", () => {
    // 手动/限价单 plan_status=null；老行完全没有陪伴字段 → 全部 undefined 不显示
    const m = extractCardMetrics({ qty: 1, entry_price: 100, plan_status: null });
    expect(m.slDistPct).toBeUndefined();
    expect(m.tpDistPct).toBeUndefined();
    expect(m.slWarn).toBeUndefined();
    expect(m.planStatus).toBeUndefined();
    // 非法字符串状态不透传（防后端脏数据渲染成灯）
    expect(extractCardMetrics({ plan_status: "garbage" }).planStatus).toBeUndefined();
  });

  it("tolerates negative distances (price breached SL/TP)", () => {
    const m = extractCardMetrics({ sl_dist_pct: -1.12, tp_dist_pct: -0.5 });
    expect(m.slDistPct).toBe(-1.12);
    expect(m.tpDistPct).toBe(-0.5);
  });
});
