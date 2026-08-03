import { describe, it, expect } from "vitest";
import {
  extractKlineRows,
  mergeKlineRows,
  olderPageCursor,
  type KlineRow,
} from "../klineHistory";

// ts 用毫秒（与 /api/kline rows 契约一致）
const T0 = 1_700_000_000_000;
const STEP = 900_000; // 15m

function row(i: number, close = 100): KlineRow {
  return { ts: T0 + i * STEP, o: close, h: close + 1, l: close - 1, c: close, v: 10 };
}

describe("extractKlineRows", () => {
  it("extracts valid rows and drops malformed ones", () => {
    const raw = {
      rows: [
        { t: "01-01 00:00", ts: T0, o: 1, h: 2, l: 0.5, c: 1.5, v: 9 },
        { ts: "not-a-number", o: 1, h: 2, l: 0.5, c: 1.5 }, // 非法 ts
        { ts: T0 + STEP, o: 1, h: 2, l: 0.5 }, // 缺 c
        { ts: T0 + 2 * STEP, o: 1, h: 2, l: 0.5, c: 1.2 }, // 无 v（合法）
      ],
    };
    const rows = extractKlineRows(raw);
    expect(rows.length).toBe(2);
    expect(rows[0].ts).toBe(T0);
    expect(rows[0].v).toBe(9);
    expect(rows[1].ts).toBe(T0 + 2 * STEP);
    expect(rows[1].v).toBeUndefined();
  });

  it("returns empty for envelope/error/non-array shapes", () => {
    expect(extractKlineRows(null)).toEqual([]);
    expect(extractKlineRows({ error: "x", rows: [] })).toEqual([]);
    expect(extractKlineRows({ rows: "nope" })).toEqual([]);
  });
});

describe("mergeKlineRows", () => {
  it("prepends older pages and keeps ascending order", () => {
    const older = [row(0), row(1)];
    const live = [row(2), row(3)];
    const merged = mergeKlineRows(older, live);
    expect(merged.map((r) => r.ts)).toEqual([0, 1, 2, 3].map((i) => T0 + i * STEP));
  });

  it("dedupes by ts with live winning (unclosed candle keeps updating)", () => {
    const older = [row(0), { ...row(1), c: 100 }];
    const live = [{ ...row(1), c: 999 }, row(2)];
    const merged = mergeKlineRows(older, live);
    expect(merged.length).toBe(3);
    expect(merged[1].c).toBe(999); // live 覆盖同 ts 历史行
  });

  it("passes through when either side is empty", () => {
    const rows = [row(0)];
    expect(mergeKlineRows([], rows)).toBe(rows);
    expect(mergeKlineRows(rows, [])).toBe(rows);
  });
});

describe("olderPageCursor", () => {
  it("returns earliest ts minus 1ms", () => {
    expect(olderPageCursor([row(2), row(0), row(1)])).toBe(T0 - 1);
  });

  it("returns null for empty rows", () => {
    expect(olderPageCursor([])).toBeNull();
  });
});
