// 渲染层纯函数测试：hoverEq 去重判定（mousemove 重渲染风暴修复的守卫）——
// 同格移动必须判等（零 React 渲染），跨格/跨行/内容变化必须判不等（更新 tooltip）；
// R5：timeAxisLabels 跨天/断档标签 + sanitizeBars 渲染序列防线。
import { describe, expect, it } from "vitest";
import { hoverEq, sanitizeBars, timeAxisLabels, type HoverInfo } from "../renderer";
import type { FootprintBar } from "../../../types/footprint";

const cell = (over: Partial<Extract<NonNullable<HoverInfo>, { kind: "cell" }>> = {}): HoverInfo => ({
  kind: "cell",
  barIndex: 3,
  price: 100.5,
  level: { price: 100.5, bidVol: 10, askVol: 20 },
  isPoc: false,
  ...over,
});

describe("hoverEq hover 去重判定", () => {
  it("双 null / 同引用判等", () => {
    expect(hoverEq(null, null)).toBe(true);
    const a = cell();
    expect(hoverEq(a, a)).toBe(true);
  });

  it("null 与非 null 判不等", () => {
    expect(hoverEq(null, cell())).toBe(false);
    expect(hoverEq(cell(), null)).toBe(false);
  });

  it("同格不同对象引用判等（hitTest 每次返回新对象）", () => {
    expect(hoverEq(cell(), cell())).toBe(true);
    // level 引用不同但价位相同 → 同格
    expect(
      hoverEq(cell({ level: { price: 100.5, bidVol: 1, askVol: 2 } }), cell()),
    ).toBe(true);
  });

  it("跨柱 / 跨价位 / POC 变化判不等", () => {
    expect(hoverEq(cell(), cell({ barIndex: 4 }))).toBe(false);
    expect(hoverEq(cell(), cell({ price: 100.6 }))).toBe(false);
    expect(hoverEq(cell(), cell({ isPoc: true }))).toBe(false);
    expect(hoverEq(cell(), cell({ level: null }))).toBe(false);
  });

  it("stats 行：同行判等、跨行/跨柱判不等、与 cell 判不等", () => {
    const s = (row: number, barIndex = 3): HoverInfo => ({ kind: "stats", row, barIndex });
    expect(hoverEq(s(1), s(1))).toBe(true);
    expect(hoverEq(s(1), s(2))).toBe(false);
    expect(hoverEq(s(1), s(1, 9))).toBe(false);
    expect(hoverEq(s(1), cell())).toBe(false);
  });
});

// ── R5 回归：时间轴「乱序」错觉修复 + 渲染序列最后防线 ──────────────────
const M30 = 1_800_000;
const bar = (time: number): FootprintBar => ({
  symbol: "BTCUSDT",
  time,
  timeframe: "30m",
  open: 100,
  high: 105,
  low: 95,
  close: 102,
  levels: [],
  totalVol: 10,
  delta: 1,
  cumDelta: 1,
  poc: 100,
});

describe("timeAxisLabels 跨天/断档标签（R5）", () => {
  // 本地时区某日 00:00 起点（用 Date 构造避免时区假设）
  const day1 = new Date(2026, 7, 11, 20, 0).getTime(); // 08-11 20:00
  it("多天断档数据：断档处强制打标+gap 标记+带日期，标签时间随索引单调", () => {
    // 08-11 20:00~21:30（4根）→ 断档 → 08-13 11:00~12:30（4根）
    const day3 = new Date(2026, 7, 13, 11, 0).getTime();
    const bars = [
      ...Array.from({ length: 4 }, (_, i) => bar(day1 + i * M30)),
      ...Array.from({ length: 4 }, (_, i) => bar(day3 + i * M30)),
    ];
    const labels = timeAxisLabels(bars, 0, bars.length, 88); // barW=88 → m=1 全打标
    // 断档柱（index 4）必须存在且 gap=true、带日期
    const gapLb = labels.find((l) => l.i === 4);
    expect(gapLb?.gap).toBe(true);
    expect(gapLb?.text).toContain("08-13");
    // 可见范围跨天：首标签带日期锚定
    expect(labels[0].text).toContain("08-11");
    // 标签对应的柱时间随索引单调递增（「时间轴乱序」不复存在——乱的是无日期观感）
    const times = labels.map((l) => bars[l.i].time);
    expect([...times].sort((a, b) => a - b)).toEqual(times);
  });

  it("单日连续数据：不带日期、无 gap（现状行为保持）", () => {
    const bars = Array.from({ length: 6 }, (_, i) => bar(day1 + i * M30));
    const labels = timeAxisLabels(bars, 0, bars.length, 88);
    expect(labels.every((l) => !l.gap)).toBe(true);
    expect(labels.every((l) => !l.text.includes("-"))).toBe(true);
  });

  it("barW 极小时按采样密度稀疏打标（不逐根打）", () => {
    const bars = Array.from({ length: 100 }, (_, i) => bar(day1 + i * M30));
    const labels = timeAxisLabels(bars, 0, bars.length, 3); // m=ceil(56/3)=19
    expect(labels.length).toBeLessThan(10);
  });
});

describe("sanitizeBars 渲染序列最后防线（R5）", () => {
  it("乱序输入 → 升序修正并报告 fixed", () => {
    const t0 = 1_700_000_000_000;
    const messy = [bar(t0 + 2 * M30), bar(t0), bar(t0 + M30)];
    const { bars: clean, fixed } = sanitizeBars(messy);
    expect(fixed).toBe(true);
    expect(clean.map((b) => b.time)).toEqual([t0, t0 + M30, t0 + 2 * M30]);
  });

  it("同 time 重复 → 去重保留后写入", () => {
    const t0 = 1_700_000_000_000;
    const dup1 = { ...bar(t0 + M30), close: 111 };
    const dup2 = { ...bar(t0 + M30), close: 222 };
    const { bars: clean, fixed } = sanitizeBars([bar(t0), dup1, dup2]);
    expect(fixed).toBe(true);
    expect(clean).toHaveLength(2);
    expect(clean[1].close).toBe(222);
  });

  it("已有序输入零开销直返（同引用，不触发多余重渲染）", () => {
    const t0 = 1_700_000_000_000;
    const ok = [bar(t0), bar(t0 + M30), bar(t0 + 2 * M30)];
    const res = sanitizeBars(ok);
    expect(res.fixed).toBe(false);
    expect(res.bars).toBe(ok);
  });
});
