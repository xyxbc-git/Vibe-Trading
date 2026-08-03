import { describe, it, expect } from "vitest";
import {
  mockTrapSignals,
  buildTrapMarks,
  TRAP_LABELS,
  type TrapBar,
  type TrapSignalsResponse,
} from "../trapSignals";

// 构造 15m 周期的等距 K 线：默认平稳震荡（high 100.5 / low 99.5，永不破前高前低）
const TF_SEC = 900;
const T0 = 1_700_000_000;

function makeFlatBars(n: number): TrapBar[] {
  const bars: TrapBar[] = [];
  for (let i = 0; i < n; i++) {
    bars.push({
      timeSec: T0 + i * TF_SEC,
      open: 100,
      high: 100.5,
      low: 99.5,
      close: 100.1,
      volume: 100,
    });
  }
  return bars;
}

/** 末根改成「上破前高又收回 + 长上影 + 放量」的诱多形态 */
function withBullTrapTail(bars: TrapBar[]): TrapBar[] {
  const out = bars.slice();
  const last = out.length - 1;
  out[last] = {
    ...out[last],
    open: 100,
    high: 103, // 上破前 20 根高点 100.5
    low: 99.5,
    close: 99.8, // 收盘收回突破位下方
    volume: 300, // 放量（均量 100 的 3 倍）
  };
  return out;
}

/** 末根改成「下破前低又收回 + 长下影」的诱空形态 */
function withBearTrapTail(bars: TrapBar[]): TrapBar[] {
  const out = bars.slice();
  const last = out.length - 1;
  out[last] = {
    ...out[last],
    open: 100,
    high: 100.4,
    low: 97, // 下破前 20 根低点 99.5
    close: 100.2, // 收盘拉回破位上方
    volume: 300,
  };
  return out;
}

describe("mockTrapSignals", () => {
  it("returns null when bars window is too small", () => {
    expect(mockTrapSignals("BTCUSDT", "15m", makeFlatBars(10))).toBeNull();
  });

  it("finds no signal in a flat range-bound series", () => {
    const resp = mockTrapSignals("BTCUSDT", "15m", makeFlatBars(60));
    expect(resp).not.toBeNull();
    expect(resp!.ok).toBe(true);
    expect(resp!.mock).toBe(true);
    expect(resp!.signals).toEqual([]);
  });

  it("detects a bull trap (fake breakout above prior high) with full fields", () => {
    const bars = withBullTrapTail(makeFlatBars(40));
    const resp = mockTrapSignals("BTCUSDT", "15m", bars);
    expect(resp).not.toBeNull();
    const signals = resp!.signals;
    expect(signals.length).toBe(1);
    const s = signals[0];
    expect(s.type).toBe("bull_trap");
    expect(s.ts).toBe(bars[bars.length - 1].timeSec);
    expect(s.price).toBe(103); // 陷阱价位 = 冲高点
    expect(s.confidence).toBeGreaterThan(0);
    expect(s.confidence).toBeLessThanOrEqual(0.95);
    expect(s.id).toBe(`trap-bull-${s.ts}`);
    // 中文证据逐条 + 放量描述 + 操作建议
    expect(s.reasons.length).toBeGreaterThanOrEqual(3);
    expect(s.reasons[0]).toContain("上破");
    expect(s.reasons.some((r) => r.includes("放量"))).toBe(true);
    expect(s.suggestion).toContain("不宜在此追多");
  });

  it("detects a bear trap (fake breakdown below prior low)", () => {
    const bars = withBearTrapTail(makeFlatBars(40));
    const resp = mockTrapSignals("BTCUSDT", "15m", bars);
    const signals = resp!.signals;
    expect(signals.length).toBe(1);
    const s = signals[0];
    expect(s.type).toBe("bear_trap");
    expect(s.price).toBe(97); // 陷阱价位 = 杀跌点
    expect(s.reasons[0]).toContain("下破");
    expect(s.suggestion).toContain("不宜在此追空");
  });

  it("is deterministic for identical input (polling idempotent)", () => {
    const bars = withBullTrapTail(makeFlatBars(40));
    const a = mockTrapSignals("BTCUSDT", "15m", bars);
    const b = mockTrapSignals("BTCUSDT", "15m", bars);
    expect(a).toEqual(b);
  });
});

describe("buildTrapMarks", () => {
  it("anchors marks to the signal bar's high/low and builds a tooltip", () => {
    const bars = withBullTrapTail(makeFlatBars(40));
    const resp = mockTrapSignals("BTCUSDT", "15m", bars);
    const anchors = bars.map((b) => ({ timeSec: b.timeSec, high: b.high, low: b.low }));
    const marks = buildTrapMarks(resp, anchors);
    expect(marks.length).toBe(1);
    const m = marks[0];
    expect(m.timeSec).toBe(bars[bars.length - 1].timeSec);
    expect(m.anchorHigh).toBe(103);
    expect(m.anchorLow).toBe(99.5);
    expect(m.mock).toBe(true);
    expect(m.tooltip).toContain(TRAP_LABELS.bull_trap);
    expect(m.tooltip).toContain("演示数据");
  });

  it("drops signals outside the visible bar window", () => {
    const anchors = makeFlatBars(30).map((b) => ({
      timeSec: b.timeSec,
      high: b.high,
      low: b.low,
    }));
    const resp: TrapSignalsResponse = {
      ok: true,
      signals: [
        {
          id: "trap-bull-1",
          ts: T0 - 10 * TF_SEC, // 窗口之前
          price: 100,
          type: "bull_trap",
          confidence: 0.8,
          reasons: ["r"],
          suggestion: "s",
        },
      ],
    };
    expect(buildTrapMarks(resp, anchors)).toEqual([]);
  });

  it("returns empty for null response or empty bars", () => {
    expect(buildTrapMarks(null, [{ timeSec: T0, high: 1, low: 0 }])).toEqual([]);
    const resp: TrapSignalsResponse = { ok: true, signals: [] };
    expect(buildTrapMarks(resp, [])).toEqual([]);
  });
});
