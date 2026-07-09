import { describe, it, expect } from "vitest";
import {
  composeChartView,
  relabelPlanLinesPlain,
  smartToPlainLines,
  signalsToKeyLevels,
  trimDrawings,
  isStaleEcho,
  isTp2Line,
  isChannelMid,
  SIMPLE_BUDGET,
  ADVANCED_BUDGET,
  type ComposeInput,
} from "../chartView";
import { PLAN_COLORS } from "../tradePlan";
import { computeDrawings, DEFAULT_PARAMS, type BaseData, type DrawMode, type DrawColors, type SmartLevels } from "../drawings";
import type { ConsensusTradePlan, TwelveSignal } from "../../api/client";

const COLORS: DrawColors = {
  up: "#3fb950", down: "#f85149", sr: "#d29922", fib: "#a855f7",
  channel: "#58a6ff", rect: "#58a6ff", rectFill: "#58a6ff1f",
};

function makeBars(n: number): BaseData {
  const dates: string[] = [];
  const closes: number[] = [];
  const highs: number[] = [];
  const lows: number[] = [];
  for (let i = 0; i < n; i++) {
    const base = 100 + 15 * Math.sin(i / 9) + 6 * Math.sin(i / 3.3) + (i % 7) * 0.4;
    dates.push(String(1700000000 + i * 60));
    closes.push(+base.toFixed(2));
    highs.push(+(base + 1.5 + (i % 3)).toFixed(2));
    lows.push(+(base - 1.5 - (i % 4)).toFixed(2));
  }
  return { dates, closes, highs, lows };
}

const plan: ConsensusTradePlan = {
  entry_zone: [100, 102],
  stop_loss: 96,
  take_profit_1: 108,
  take_profit_2: 114,
  rr: 2.5,
  position_pct: 15,
};

const smart: SmartLevels = {
  price: 101,
  support: { kind: "support", level: 97, lower: 96.6, upper: 97.4, touches: 4 },
  resistance: { kind: "resistance", level: 106, lower: 105.6, upper: 106.4, touches: 3 },
};

const mkSignal = (system: string, strength: number, prices: number[], direction = "bullish"): TwelveSignal => ({
  system,
  name_cn: system,
  direction: direction as TwelveSignal["direction"],
  strength,
  key_levels: prices.map((p, i) => ({ label: `L${i}`, price: p })),
});

function baseInput(mode: ComposeInput["mode"]): ComposeInput {
  return {
    mode,
    tradePlan: plan,
    planDirection: "bullish",
    smart,
    fullDrawings: null,
    perTypeDrawings: null,
    reliability: undefined,
    price: 101,
    signals: [],
    twelveOn: false,
  };
}

describe("simple mode (default beginner view)", () => {
  it("draws at most 6 lines: plan (without TP2) + strong S/R, all plain-language", () => {
    const c = composeChartView(baseInput("simple"));
    expect(c.lineCount).toBeLessThanOrEqual(SIMPLE_BUDGET);
    expect(c.drawings).toBeNull();
    expect(c.keyLevels).toHaveLength(0);
    expect(c.smartLevels).toBeNull(); // 原始智能层不渲染，改用大白话线

    const labels = c.planLines.map((l) => l.label ?? "");
    expect(labels.some((s) => s.includes("建议买入区"))).toBe(true);
    expect(labels.some((s) => s.includes("止损") && s.includes("跌破就跑"))).toBe(true);
    expect(labels.some((s) => s.includes("止盈目标"))).toBe(true);
    expect(labels.some((s) => s.includes("强支撑") && s.includes("反弹"))).toBe(true);
    expect(labels.some((s) => s.includes("强压力") && s.includes("回落"))).toBe(true);
    expect(labels.some((s) => s.includes("TP2"))).toBe(false); // TP2 被省略
    // 图例覆盖当前可见线型且含大白话解释
    expect(c.legend.length).toBeGreaterThanOrEqual(4);
    expect(c.legend.every((e) => e.explain.length > 0)).toBe(true);
  });

  it("short plan flips entry/stop wording", () => {
    const lines = relabelPlanLinesPlain(
      [{ price: 100, color: PLAN_COLORS.entry, width: 2, style: "dashed", label: "x" },
       { price: 106, color: PLAN_COLORS.stop, width: 2, style: "solid", label: "x" }],
      "bearish",
    );
    expect(lines[0].label).toContain("建议做空区");
    expect(lines[1].label).toContain("涨破就跑");
  });

  it("degrades to only strong S/R when plan is null", () => {
    const c = composeChartView({ ...baseInput("simple"), tradePlan: null });
    expect(c.planLines).toHaveLength(2); // 支撑 + 压力
    expect(c.lineCount).toBe(2);
    expect(smartToPlainLines(null)).toHaveLength(0);
  });
});

describe("advanced mode budget & trimming", () => {
  it("keeps total lines within the 12-line budget with all sources active", () => {
    const bars = makeBars(220);
    const perType: Partial<Record<DrawMode, ReturnType<typeof computeDrawings>>> = {};
    for (const m of ["trend", "sr", "fib", "channel", "rect"] as DrawMode[]) {
      perType[m] = computeDrawings(new Set([m]), bars, COLORS, DEFAULT_PARAMS);
    }
    const signals = [
      mkSignal("turtle", 0.9, [95, 105]),
      mkSignal("dow", 0.7, [96, 104]),
      mkSignal("weak", 0.3, [90, 110]), // 低强度，应被过滤
    ];
    const c = composeChartView({
      ...baseInput("advanced"),
      perTypeDrawings: perType,
      reliability: { trend: 0.9, sr: 0.8, fib: 0.2, channel: 0.5, rect: 0.4 },
      signals,
    });
    expect(c.lineCount).toBeLessThanOrEqual(ADVANCED_BUDGET);
    // 计划核心线优先保留
    expect(c.planLines.some((l) => (l.label ?? "").includes("建议买入区"))).toBe(true);
  });

  it("trimDrawings caps each type and respects reliability priority + budget", () => {
    const bars = makeBars(220);
    const perType: Partial<Record<DrawMode, ReturnType<typeof computeDrawings>>> = {
      sr: computeDrawings(new Set<DrawMode>(["sr"]), bars, COLORS, DEFAULT_PARAMS),
      fib: computeDrawings(new Set<DrawMode>(["fib"]), bars, COLORS, DEFAULT_PARAMS),
    };
    // fib 可靠度更高 → 预算只够一类时 fib 先入
    const tight = trimDrawings(perType, { sr: 0.3, fib: 0.9 }, 100, 2);
    expect(tight.usedTypes[0]).toBe("fib");
    expect(tight.cost).toBeLessThanOrEqual(2);
    // fib 全集 7 档 → 裁到离现价最近的 2 档
    const roomy = trimDrawings(perType, { sr: 0.3, fib: 0.9 }, 100, 10);
    const fibLines = roomy.result.hlines.filter((l) => (l.label ?? "").startsWith("Fib"));
    expect(fibLines.length).toBeLessThanOrEqual(2);
    // S/R 最多 2 条
    const srLines = roomy.result.hlines.filter((l) => (l.label ?? "").startsWith("S/R"));
    expect(srLines.length).toBeLessThanOrEqual(2);
  });

  it("advanced-mode fib lines carry plain-language suffix; kind survives relabel", () => {
    const bars = makeBars(220);
    const perType: Partial<Record<DrawMode, ReturnType<typeof computeDrawings>>> = {
      fib: computeDrawings(new Set<DrawMode>(["fib"]), bars, COLORS, DEFAULT_PARAMS),
    };
    const trimmed = trimDrawings(perType, { fib: 0.9 }, 100, 10);
    const fibLines = trimmed.result.hlines.filter((l) => l.kind === "fib");
    expect(fibLines.length).toBeGreaterThan(0);
    for (const l of fibLines) {
      // 标准档位名 + 大白话后缀，如 "Fib 0.618 黄金回调位"
      expect(l.label).toMatch(/^Fib (0|1|0\.\d+) .+/);
      expect(l.label).not.toContain("%");
      expect(l.kind).toBe("fib");
    }
  });

  it("signalsToKeyLevels filters by strength and dedups near-identical prices", () => {
    const signals = [
      mkSignal("a", 0.9, [100.0]),
      mkSignal("b", 0.7, [100.2]),  // 与 100.0 相差 0.2% < 0.3% → 合并，保留强度高的 a
      mkSignal("c", 0.65, [110]),
      mkSignal("weak", 0.3, [120]), // 低于 0.6 → 过滤
    ];
    const levels = signalsToKeyLevels(signals, { minStrength: 0.6, dedupPct: 0.003 });
    expect(levels).toHaveLength(2);
    expect(levels[0].label).toContain("a");
    expect(levels.some((l) => l.price === 120)).toBe(false);
    // 不去重时全保留（除低强度）
    expect(signalsToKeyLevels(signals, { minStrength: 0.6 })).toHaveLength(3);
  });
});

describe("symbol echo validation (audit a5 M1)", () => {
  it("discards responses echoing a different symbol (stale after fast switching)", () => {
    expect(isStaleEcho("ETHUSDT", "BTCUSDT")).toBe(true);
    expect(isStaleEcho("BTCUSDT", "BTCUSDT")).toBe(false);
  });

  it("normalises case and separators; missing echo (old backend) is not stale", () => {
    expect(isStaleEcho("BTCUSDT", "btcusdt")).toBe(false);
    expect(isStaleEcho("BTCUSDT", "BTC-USDT")).toBe(false);
    expect(isStaleEcho("BTCUSDT", "BTC/USDT")).toBe(false);
    expect(isStaleEcho("BTCUSDT", undefined)).toBe(false);
    expect(isStaleEcho("BTCUSDT", null)).toBe(false);
    expect(isStaleEcho("BTCUSDT", "")).toBe(false);
  });
});

describe("strength-priority truncation (audit a5 minor#5)", () => {
  it("maxCount keeps the STRONGEST levels (not the lowest-priced), returned price-ascending", () => {
    const signals = [
      mkSignal("weakLow", 0.62, [90]),    // 价格最低但最弱 → 预算不足时应被丢
      mkSignal("strongMid", 0.95, [100]),
      mkSignal("strongHigh", 0.9, [110]),
    ];
    const levels = signalsToKeyLevels(signals, { minStrength: 0.6, maxCount: 2 });
    expect(levels).toHaveLength(2);
    expect(levels.map((l) => l.price)).toEqual([100, 110]); // 保强丢弱 + 价格升序
    expect(levels.some((l) => l.price === 90)).toBe(false);
  });

  it("maxCount larger than pool is a no-op; zero budget empties the list", () => {
    const signals = [mkSignal("a", 0.9, [100]), mkSignal("b", 0.8, [110])];
    expect(signalsToKeyLevels(signals, { maxCount: 10 })).toHaveLength(2);
    expect(signalsToKeyLevels(signals, { maxCount: 0 })).toHaveLength(0);
  });
});

describe("kind-based line classification with label fallback (audit a5 minor#4)", () => {
  it("planToOverlay stamps kinds; TP2 filter uses kind even if labels change", () => {
    const withTp2 = composeChartView(baseInput("simple"));
    // TP2（kind="tp2"）被省略；判断走 kind 而非 label
    expect(withTp2.planLines.every((l) => l.kind !== "tp2")).toBe(true);
    expect(isTp2Line({ price: 1, color: "#fff", width: 1, kind: "tp2", label: "改了文案也认得" })).toBe(true);
    expect(isTp2Line({ price: 1, color: "#fff", width: 1, kind: "tp1", label: "止盈 TP2 假标签" })).toBe(false);
    // 无 kind 的旧数据回退 label 判断
    expect(isTp2Line({ price: 1, color: "#fff", width: 1, label: "止盈 TP2 114.00" })).toBe(true);
    expect(isTp2Line({ price: 1, color: "#fff", width: 1, label: "止盈 TP1 108.00" })).toBe(false);
  });

  it("channel-mid selection prefers kind and falls back to label for legacy segments", () => {
    const seg = (kind: "channel-mid" | "channel-edge" | undefined, label: string) => ({
      i1: 0, p1: 100, i2: 10, p2: 101, color: "#58a6ff", width: 1, label, kind,
    });
    // kind 命中：label 随便改
    expect(isChannelMid(seg("channel-mid", "renamed axis"))).toBe(true);
    expect(isChannelMid(seg("channel-edge", "回归中轴 假标签"))).toBe(false);
    // legacy：无 kind 回退 label
    expect(isChannelMid(seg(undefined, "回归中轴 60%"))).toBe(true);
    expect(isChannelMid(seg(undefined, "通道上轨"))).toBe(false);

    const perType = {
      channel: {
        segments: [seg(undefined, "通道上轨"), seg(undefined, "回归中轴"), seg(undefined, "通道下轨")],
        hlines: [],
        bands: [],
      },
    };
    const trimmed = trimDrawings(perType, { channel: 0.9 }, 100, 5);
    expect(trimmed.result.segments).toHaveLength(1);
    expect(trimmed.result.segments[0].label).toContain("回归中轴");
  });
});

describe("pro mode passthrough", () => {
  it("keeps full drawings, raw labels, smart levels and unfiltered key levels", () => {
    const bars = makeBars(220);
    const full = computeDrawings(new Set<DrawMode>(["trend", "sr", "fib", "channel", "rect"]), bars, COLORS, DEFAULT_PARAMS);
    const signals = [mkSignal("a", 0.2, [100]), mkSignal("b", 0.9, [110])];
    const c = composeChartView({
      ...baseInput("pro"),
      fullDrawings: full,
      signals,
      twelveOn: true,
    });
    expect(c.drawings).toBe(full);           // 全量透传，不裁剪
    expect(c.smartLevels).toBe(smart);        // 原始智能层保留（含现价）
    expect(c.keyLevels).toHaveLength(2);      // 未过滤（0.2 也保留）
    expect(c.planLines.some((l) => (l.label ?? "").includes("入场区"))).toBe(true); // 原始 label
    expect(c.planLines.some((l) => (l.label ?? "").includes("建议买入区"))).toBe(false);
    expect(c.legend.some((e) => e.name === "信号关键位")).toBe(true);
  });

  it("twelveOn=false hides key levels in pro mode", () => {
    const c = composeChartView({ ...baseInput("pro"), signals: [mkSignal("a", 0.9, [100])], twelveOn: false });
    expect(c.keyLevels).toHaveLength(0);
  });
});
