// 渲染层纯函数测试：hoverEq 去重判定（mousemove 重渲染风暴修复的守卫）——
// 同格移动必须判等（零 React 渲染），跨格/跨行/内容变化必须判不等（更新 tooltip）
import { describe, expect, it } from "vitest";
import { hoverEq, type HoverInfo } from "../renderer";

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
