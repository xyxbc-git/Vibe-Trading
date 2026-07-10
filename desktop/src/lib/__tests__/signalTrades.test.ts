import { describe, it, expect } from "vitest";
import { tradesToMarks, tradeTip } from "../signalTrades";
import type { SignalWinrateTrade } from "../../api/client";

const H4 = 4 * 3600 * 1000; // 4h in ms

function mkTrade(over: Partial<SignalWinrateTrade>): SignalWinrateTrade {
  return {
    t: 1_700_000_000_000,
    exit_t: 1_700_000_000_000 + 3 * H4,
    side: "long",
    entry: 100,
    sl: 95,
    tp: 110,
    exit_price: 110,
    win: true,
    pnl_pct: 10,
    bars_held: 3,
    mode: "plan",
    ...over,
  };
}

// 窗口秒（覆盖 mkTrade 默认时间前后）
const FROM = 1_699_000_000;
const TO = 1_701_000_000;

describe("tradesToMarks geometry", () => {
  it("maps entry/exit anchors: long win keeps side & result, exit dot at exit bar", () => {
    const { marks, visible, total } = tradesToMarks([mkTrade({})], "海龟", FROM, TO);
    expect(total).toBe(1);
    expect(visible).toBe(1);
    expect(marks).toHaveLength(1);

    const m = marks[0];
    expect(m.side).toBe("long");
    expect(m.win).toBe(true);
    expect(m.timeSec).toBe(1_700_000_000);
    expect(m.exitTimeSec).toBe(1_700_000_000 + 3 * 4 * 3600);
    expect(m.entry).toBe(100);
    expect(m.exitPrice).toBe(110);
    expect(m.tooltip).toContain("入场");
    expect(m.exitTooltip).toContain("出场");
  });

  it("anchors badge to bar low (long) / bar high (short) when bars map provided", () => {
    const bars = new Map([[1_700_000_000, { high: 105, low: 98 }]]);
    const longMark = tradesToMarks([mkTrade({})], "海龟", FROM, TO, bars).marks[0];
    expect(longMark.anchorPrice).toBe(98); // 多单锚低点，徽章挂下方

    const shortMark = tradesToMarks(
      [mkTrade({ side: "short", win: false, pnl_pct: -3 })],
      "海龟",
      FROM,
      TO,
      bars,
    ).marks[0];
    expect(shortMark.anchorPrice).toBe(105); // 空单锚高点，徽章挂上方
  });

  it("falls back to entry price as anchor when bar data is missing", () => {
    const { marks } = tradesToMarks([mkTrade({})], "海龟", FROM, TO);
    expect(marks[0].anchorPrice).toBe(100);
  });

  it("marks are sorted by trigger time", () => {
    const { marks } = tradesToMarks(
      [
        mkTrade({ t: 1_700_000_000_000 + 10 * H4, exit_t: 1_700_000_000_000 + 12 * H4 }),
        mkTrade({ t: 1_700_000_000_000, exit_t: 1_700_000_000_000 + 20 * H4 }),
      ],
      "海龟",
      FROM,
      TO,
    );
    const times = marks.map((m) => m.timeSec);
    expect([...times].sort((a, b) => a - b)).toEqual(times);
  });
});

describe("tradesToMarks window clipping", () => {
  it("entry outside the loaded kline window: whole trade skipped, counted in total only", () => {
    const { marks, visible, total } = tradesToMarks(
      [mkTrade({ t: (FROM - 100) * 1000, exit_t: (FROM - 50) * 1000 })],
      "海龟",
      FROM,
      TO,
    );
    expect(marks).toHaveLength(0);
    expect(visible).toBe(0);
    expect(total).toBe(1);
  });

  it("exit beyond the window: badge kept, exitTimeSec nulled (no exit dot)", () => {
    const { marks, visible } = tradesToMarks(
      [mkTrade({ exit_t: (TO + 100) * 1000 })],
      "海龟",
      FROM,
      TO,
    );
    expect(visible).toBe(1);
    expect(marks).toHaveLength(1);
    expect(marks[0].exitTimeSec).toBeNull();
  });

  it("empty input → empty result", () => {
    const r = tradesToMarks([], "海龟", FROM, TO);
    expect(r.marks).toHaveLength(0);
    expect(r.total).toBe(0);
  });
});

describe("tradeTip copy", () => {
  it("plan-mode win mentions 触止盈, includes prices and holding bars", () => {
    const tip = tradeTip(mkTrade({}), "海龟");
    expect(tip).toContain("海龟");
    expect(tip).toContain("做多");
    expect(tip).toContain("+10.00%");
    expect(tip).toContain("触止盈离场");
    expect(tip).toContain("持有 3 根");
  });

  it("plan-mode loss mentions 触止损; horizon mode mentions 满观察期", () => {
    expect(tradeTip(mkTrade({ win: false, pnl_pct: -5 }), "海龟")).toContain("触止损离场");
    expect(tradeTip(mkTrade({ mode: "horizon" }), "海龟")).toContain("满观察期收盘离场");
  });
});
