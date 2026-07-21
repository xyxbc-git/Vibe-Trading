import { describe, it, expect } from "vitest";
import {
  FootprintAggregator,
  aggregateTrades,
  inferTickSize,
  TIMEFRAME_MS,
} from "../footprint/aggregator";
import { BASE_PRICES, MockTradeFeed } from "../footprint/mockFeed";
import type { Trade } from "../../types/footprint";

const T0 = 1_760_000_040_000; // 任意非整分起点，验证柱对齐
const MIN = 60_000;
const SYM = "BTCUSDT";

function trade(offsetMs: number, price: number, size: number, side: Trade["side"]): Trade {
  return { time: T0 + offsetMs, price, size, side };
}

describe("FootprintAggregator", () => {
  it("aggregates OHLC / levels / delta / poc within one bar", () => {
    const bars = aggregateTrades(
      [
        trade(0, 100, 10, "buy"),   // askVol@100 = 10
        trade(1000, 101, 5, "buy"), // askVol@101 = 5
        trade(2000, 101, 8, "sell"),// bidVol@101 = 8
        trade(3000, 99, 20, "sell"),// bidVol@99 = 20 → poc=99
      ],
      SYM,
      "1m",
    );

    expect(bars).toHaveLength(1);
    const bar = bars[0];
    expect(bar.symbol).toBe(SYM);
    expect(bar.time).toBe(Math.floor(T0 / MIN) * MIN); // 对齐整分
    expect(bar.timeframe).toBe("1m");
    expect([bar.open, bar.high, bar.low, bar.close]).toEqual([100, 101, 99, 99]);
    // levels 按 price 降序
    expect(bar.levels.map((l) => l.price)).toEqual([101, 100, 99]);
    expect(bar.levels[0]).toEqual({ price: 101, bidVol: 8, askVol: 5 });
    expect(bar.levels[2]).toEqual({ price: 99, bidVol: 20, askVol: 0 });
    expect(bar.totalVol).toBe(43);
    expect(bar.delta).toBe(10 + 5 - 8 - 20); // askΣ − bidΣ = -13
    expect(bar.poc).toBe(99);
  });

  it("rolls bars at timeframe boundary and accumulates cumDelta across bars", () => {
    const agg = new FootprintAggregator(SYM, "1m");
    const r1 = agg.ingest(trade(0, 100, 10, "buy"));
    expect(r1.isNewBar).toBe(true);
    expect(r1.completedBar).toBeNull();

    const r2 = agg.ingest(trade(2 * MIN, 100, 4, "sell")); // 跨柱
    expect(r2.isNewBar).toBe(true);
    expect(r2.completedBar?.delta).toBe(10);
    expect(r2.completedBar?.cumDelta).toBe(10); // 快照含跨柱累计
    expect(r2.bar.delta).toBe(-4);
    expect(r2.bar.cumDelta).toBe(6); // 10 - 4
  });

  it("ingestFast matches ingest aggregation results (lazy snapshot path)", () => {
    const trades = [
      trade(0, 100, 10, "buy"),
      trade(30_000, 101, 5, "sell"),
      trade(2 * MIN, 99, 7, "buy"),
    ];
    const fast = new FootprintAggregator(SYM, "1m");
    const slow = new FootprintAggregator(SYM, "1m");
    const fastCompleted = trades.map((t) => fast.ingestFast(t)).filter((b) => b !== null);
    const slowCompleted = trades.map((t) => slow.ingest(t).completedBar).filter((b) => b !== null);
    expect(fastCompleted).toEqual(slowCompleted);
    expect(fast.snapshotCurrent()).toEqual(slow.snapshotCurrent());
  });

  it("snaps prices to tickSize grid", () => {
    const bars = aggregateTrades(
      [trade(0, 100.4, 1, "buy"), trade(1000, 100.6, 2, "sell")],
      SYM,
      "1m",
      { tickSize: 1 },
    );
    expect(bars[0].levels.map((l) => l.price)).toEqual([101, 100]);
  });

  it("supports 5m/15m timeframes with the same trade stream", () => {
    // 基准对齐 15m 边界（1_760_000_400_000 % 900_000 === 0），6min 后仍在同一 15m 桶
    const base = 1_760_000_400_000;
    const trades: Trade[] = [
      { time: base, price: 100, size: 1, side: "buy" },
      { time: base + 6 * MIN, price: 101, size: 2, side: "buy" },
    ];
    expect(aggregateTrades(trades, SYM, "5m")).toHaveLength(2);
    expect(aggregateTrades(trades, SYM, "15m")).toHaveLength(1);
  });

  it("supports new 30m/4h/1d timeframes with correct bucket alignment", () => {
    expect(TIMEFRAME_MS["30m"]).toBe(1_800_000);
    expect(TIMEFRAME_MS["4h"]).toBe(14_400_000);
    expect(TIMEFRAME_MS["1d"]).toBe(86_400_000);

    const base = 1_760_054_400_000; // 86_400_000 的整数倍：三周期共同边界
    const mk = (off: number): Trade => ({ time: base + off, price: 100, size: 1, side: "buy" });

    // 30m：25min 同桶，35min 跨桶
    expect(aggregateTrades([mk(0), mk(25 * MIN)], SYM, "30m")).toHaveLength(1);
    expect(aggregateTrades([mk(0), mk(35 * MIN)], SYM, "30m")).toHaveLength(2);
    // 4h：3h 同桶，5h 跨桶
    expect(aggregateTrades([mk(0), mk(180 * MIN)], SYM, "4h")).toHaveLength(1);
    expect(aggregateTrades([mk(0), mk(300 * MIN)], SYM, "4h")).toHaveLength(2);
    // 1d：20h 同桶，26h 跨桶；且柱起点对齐日界
    const sameDay = aggregateTrades([mk(0), mk(20 * 60 * MIN)], SYM, "1d");
    expect(sameDay).toHaveLength(1);
    expect(sameDay[0].time).toBe(base);
    expect(aggregateTrades([mk(0), mk(26 * 60 * MIN)], SYM, "1d")).toHaveLength(2);
  });
});

describe("inferTickSize", () => {
  it("derives tick by price magnitude per task table", () => {
    expect(inferTickSize(BASE_PRICES.BTCUSDT)).toBe(1);      // 64000
    expect(inferTickSize(BASE_PRICES.ETHUSDT)).toBe(0.1);    // 3400
    expect(inferTickSize(BASE_PRICES.SOLUSDT)).toBe(0.01);   // 145
    expect(inferTickSize(BASE_PRICES.BNBUSDT)).toBe(0.01);   // 590
    expect(inferTickSize(BASE_PRICES.XRPUSDT)).toBe(0.00001);// 0.52
    expect(inferTickSize(BASE_PRICES.DOGEUSDT)).toBe(0.00001);
    expect(inferTickSize(BASE_PRICES.ADAUSDT)).toBe(0.00001);
    expect(inferTickSize(0)).toBe(1); // 兜底
  });

  it("keeps level count per bar reasonable for low-price symbols", () => {
    const feed = new MockTradeFeed({ symbol: "SOLUSDT", seed: 5 });
    const end = T0 + 120 * MIN;
    const trades = feed.backfill(60 * MIN, end);
    const bars = aggregateTrades(trades, "SOLUSDT", "15m", { tickSize: feed.tickSize });
    for (const bar of bars) {
      expect(bar.levels.length).toBeGreaterThan(0);
      expect(bar.levels.length).toBeLessThan(120); // 价位层不过密
    }
  });
});

describe("MockTradeFeed", () => {
  it("is deterministic for the same seed and ascending in time", () => {
    const end = T0 + 60 * MIN;
    const a = new MockTradeFeed({ seed: 42 }).backfill(30 * MIN, end);
    const b = new MockTradeFeed({ seed: 42 }).backfill(30 * MIN, end);
    expect(a.length).toBeGreaterThan(100);
    expect(a).toEqual(b);
    for (let i = 1; i < a.length; i++) expect(a[i].time).toBeGreaterThanOrEqual(a[i - 1].time);
  });

  it("produces independent per-symbol streams with symbol-scaled prices", () => {
    const end = T0 + 60 * MIN;
    const btc = new MockTradeFeed({ symbol: "BTCUSDT", seed: 42 }).backfill(30 * MIN, end);
    const sol = new MockTradeFeed({ symbol: "SOLUSDT", seed: 42 }).backfill(30 * MIN, end);
    // 同 seed 不同 symbol：流不同（seed 掺 symbol 哈希）
    expect(btc.map((t) => t.side).join("")).not.toBe(sol.map((t) => t.side).join(""));
    // 价格停留在各自基准价量级（±10%）
    for (const t of btc) expect(Math.abs(t.price - 64000) / 64000).toBeLessThan(0.1);
    for (const t of sol) expect(Math.abs(t.price - 145) / 145).toBeLessThan(0.1);
    // 单量按币价缩放：SOL 中位单量显著大于 BTC（名义价值可比）
    const med = (xs: number[]) => xs.slice().sort((x, y) => x - y)[Math.floor(xs.length / 2)];
    expect(med(sol.map((t) => t.size))).toBeGreaterThan(med(btc.map((t) => t.size)) * 50);
  });

  it("multi-symbol aggregation stays isolated (no cross-pollution)", () => {
    const end = T0 + 120 * MIN;
    const feeds = ["BTCUSDT", "ETHUSDT"].map((s) => new MockTradeFeed({ symbol: s, seed: 7 }));
    const barsBySym = feeds.map((f) =>
      aggregateTrades(f.backfill(60 * MIN, end), f.symbol, "5m", { tickSize: f.tickSize }),
    );
    for (const bars of barsBySym) expect(bars.length).toBeGreaterThan(0);
    expect(barsBySym[0].every((b) => b.symbol === "BTCUSDT")).toBe(true);
    expect(barsBySym[1].every((b) => b.symbol === "ETHUSDT")).toBe(true);
    // 各自 cumDelta 独立累计：末柱 cumDelta = 各自 delta 之和
    for (const bars of barsBySym) {
      const sum = bars.reduce((acc, b) => acc + b.delta, 0);
      expect(bars[bars.length - 1].cumDelta).toBeCloseTo(sum, 6);
    }
  });

  it("density-tiered backfillStream covers 120d and yields >=120 daily bars", () => {
    const DAY = 86_400_000;
    const feed = new MockTradeFeed({ symbol: "BTCUSDT", seed: 7 });
    const end = 1_760_054_400_000 + 120 * DAY;
    const agg = new FootprintAggregator("BTCUSDT", "1d", { tickSize: feed.tickSize });
    let bars = 0;
    let trades = 0;
    let lastTime = 0;
    for (const t of feed.backfillStream(120 * DAY, end)) {
      expect(t.time).toBeGreaterThanOrEqual(lastTime);
      lastTime = t.time;
      trades += 1;
      if (agg.ingestFast(t)) bars += 1;
    }
    if (agg.snapshotCurrent()) bars += 1;
    expect(bars).toBeGreaterThanOrEqual(120);
    // 分层密度控制总量：远段稀疏，总成交应在 ~200 万笔以内（全密度会 >1600 万）
    expect(trades).toBeLessThan(2_000_000);
    expect(trades).toBeGreaterThan(200_000);
  });

  it("produces >=200 bars for 15m and 30m service windows", () => {
    const HOUR = 3_600_000;
    const end = T0 + 200 * 30 * MIN;
    const feed = new MockTradeFeed({ symbol: "BTCUSDT", seed: 7 });
    const trades = [...feed.backfillStream(100 * HOUR, end)];
    const bars30 = aggregateTrades(trades, "BTCUSDT", "30m", { tickSize: feed.tickSize });
    expect(bars30.length).toBeGreaterThanOrEqual(200);
    const bars15 = aggregateTrades(
      trades.filter((t) => t.time >= end - 50 * HOUR),
      "BTCUSDT",
      "15m",
      { tickSize: feed.tickSize },
    );
    expect(bars15.length).toBeGreaterThanOrEqual(200);
    for (const bar of [...bars30, ...bars15]) {
      expect(bar.levels.length).toBeGreaterThan(0);
      expect(bar.totalVol).toBeGreaterThan(0);
    }
  });
});
