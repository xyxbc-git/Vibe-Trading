import { describe, it, expect } from "vitest";
import { buildPlanOrder } from "../planOrder";
import type { PositionAdvice } from "../../api/client";

// 一份完整可执行的做多建议：130U 本金 × 100% 保证金 × 100x 杠杆
const longAdvice: PositionAdvice = {
  ok: true,
  symbol: "BTCUSDT",
  side: "long",
  entry: 100_000,
  entry_zone: [99_500, 100_500],
  capital_usdt: 130,
  leverage: 100,
  margin_pct: 100,
  sl: { price: 98_000, dist_pct: 2, safety: "ok", safety_margin_pct: 1 },
  liquidation: { price: 99_100, dist_pct: 0.9, loss_usdt: 130 },
  position: {
    notional_usdt: 13_000,
    margin_usdt: 130,
    qty_coin: 0.13,
    contracts: 1300,
    contract_size: 0.0001,
    capital_used_pct: 100,
    capped: false,
  },
  take_profits: [
    { rr: 1.5, price: 103_000, profit_usdt: 390 },
    { rr: 2, price: 104_000, profit_usdt: 520 },
    { rr: 3, price: 106_000, profit_usdt: 780 },
  ],
  plan_tp_ref: 105_000,
  source_tf: "4h",
};

describe("buildPlanOrder mapping", () => {
  it("maps a long advice to a user-created buy order (margin-based qty)", () => {
    const o = buildPlanOrder("BTCUSDT", "auto", longAdvice);
    expect(o).not.toBeNull();
    expect(o!.symbol).toBe("BTCUSDT");
    expect(o!.side).toBe("buy");
    expect(o!.price).toBe(100_000);
    // qty = 保证金 / 入场价（现货全额记账口径），非杠杆名义币数
    expect(o!.qty).toBeCloseTo(130 / 100_000, 10);
    expect(o!.stopLoss).toBe(98_000);
    // 止盈取第一档 1:1.5
    expect(o!.takeProfit).toBe(103_000);
    expect(o!.source).toBe("user-created");
  });

  it("maps a short advice to a sell order", () => {
    const o = buildPlanOrder("ETHUSDT", "1h", { ...longAdvice, side: "short" });
    expect(o!.side).toBe("sell");
  });

  it("serializes full plan context into note JSON", () => {
    const o = buildPlanOrder("BTCUSDT", "auto", longAdvice);
    const ctx = JSON.parse(o!.note);
    expect(ctx.kind).toBe("trade-plan");
    expect(ctx.tf).toBe("auto");
    expect(ctx.source_tf).toBe("4h");
    expect(ctx.leverage).toBe(100);
    expect(ctx.margin_usdt).toBe(130);
    expect(ctx.notional_usdt).toBe(13_000);
    expect(ctx.entry).toBe(100_000);
    expect(ctx.entry_zone).toEqual([99_500, 100_500]);
    expect(ctx.stop_loss).toBe(98_000);
    // 分档止盈全量保留，供邮件通知等下游消费
    expect(ctx.take_profits).toEqual([
      { rr: 1.5, price: 103_000 },
      { rr: 2, price: 104_000 },
      { rr: 3, price: 106_000 },
    ]);
    expect(ctx.liquidation).toBe(99_100);
  });

  it("falls back to plan_tp_ref when take_profits is empty", () => {
    const o = buildPlanOrder("BTCUSDT", "auto", {
      ...longAdvice,
      take_profits: [],
    });
    expect(o!.takeProfit).toBe(105_000);
  });

  it("omits SL/TP when missing instead of sending invalid values", () => {
    const o = buildPlanOrder("BTCUSDT", "auto", {
      ...longAdvice,
      sl: undefined,
      take_profits: [],
      plan_tp_ref: undefined,
    });
    expect(o!.stopLoss).toBeUndefined();
    expect(o!.takeProfit).toBeUndefined();
  });

  it("returns null for non-executable advice (neutral / missing fields / null)", () => {
    expect(buildPlanOrder("BTCUSDT", "auto", null)).toBeNull();
    expect(buildPlanOrder("BTCUSDT", "auto", undefined)).toBeNull();
    expect(buildPlanOrder("BTCUSDT", "auto", { ok: false })).toBeNull();
    // 无入场价 / 无保证金 → 不可下单
    expect(
      buildPlanOrder("BTCUSDT", "auto", { ...longAdvice, entry: undefined }),
    ).toBeNull();
    expect(
      buildPlanOrder("BTCUSDT", "auto", { ...longAdvice, position: undefined }),
    ).toBeNull();
  });
});
