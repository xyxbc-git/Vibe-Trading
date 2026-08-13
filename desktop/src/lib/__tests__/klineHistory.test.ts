import { describe, it, expect } from "vitest";
import {
  extractKlineRows,
  fvgInvalidatedByClose,
  mergeKlineRows,
  olderPageCursor,
  rowsIntervalConsistent,
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

// ── R4 回归：切周期后数据集时间戳间隔一致性（防「新键旧数据」混拼家族） ──
describe("rowsIntervalConsistent", () => {
  const at = (ms: number): KlineRow => ({ ts: ms, o: 1, h: 2, l: 0.5, c: 1.5 });

  it("纯净周期数据（含合法缺口=整数倍间隔）判定一致", () => {
    const m30 = 1_800_000;
    expect(rowsIntervalConsistent([at(0), at(m30), at(2 * m30)], m30)).toBe(true);
    // 缺一根（gap=2×interval）是合法缺口，不算混拼
    expect(rowsIntervalConsistent([at(0), at(m30), at(3 * m30)], m30)).toBe(true);
    expect(rowsIntervalConsistent([], m30)).toBe(true);
    expect(rowsIntervalConsistent([at(0)], m30)).toBe(true);
  });

  it("旧周期历史页混入新周期数据集（切 TF 单帧窗口场景）判定不一致", () => {
    const h4 = 14_400_000;
    const m30 = 1_800_000;
    // 模拟：4h 历史页 + 30m 实时窗 merge 后的混拼数组
    const polluted = mergeKlineRows(
      [at(0), at(h4), at(2 * h4)],                        // 旧 4h 历史页残留
      [at(2 * h4 + m30), at(2 * h4 + 2 * m30)],           // 新 30m 实时窗
    );
    expect(rowsIntervalConsistent(polluted, h4)).toBe(false);  // 4h 口径下 30m 间隔非法
    // 30m 口径下 4h 间隔是 8 倍整数倍会误判一致——所以口径必须用「大周期」判定；
    // 同 ts 重复/乱序也必须暴露
    expect(rowsIntervalConsistent([at(0), at(0)], m30)).toBe(false);
    expect(rowsIntervalConsistent([at(m30), at(0)], m30)).toBe(false);
  });
});

// ── R14 回归：FVG 收盘穿透失效判定（收盘穿透→隐藏，影线穿透→保留） ──
describe("fvgInvalidatedByClose", () => {
  const T0 = 1_700_000_000_000;
  const bar = (i: number, close: number): KlineRow => ({
    ts: T0 + i * 900_000, o: close, h: close + 5, l: close - 5, c: close,
  });

  it("看涨 FVG：形成后收盘跌破下沿 → 失效", () => {
    const rows = [bar(0, 105), bar(1, 102), bar(2, 96)]; // bar2 收盘 96 < bottom 100
    expect(fvgInvalidatedByClose(rows, T0, "bullish", 103, 100)).toBe(true);
  });

  it("看涨 FVG：仅影线下探（收盘仍在下沿上方）→ 保留（防插针误杀）", () => {
    // bar1 low=97 刺穿 bottom=100，但收盘 102 > 100
    const rows = [bar(0, 105), bar(1, 102)];
    expect(fvgInvalidatedByClose(rows, T0, "bullish", 103, 100)).toBe(false);
  });

  it("看跌 FVG：收盘升破上沿 → 失效；影线上探 → 保留", () => {
    expect(fvgInvalidatedByClose([bar(1, 108)], T0, "bearish", 105, 102)).toBe(true);
    // 收盘 104 < top 105（high=109 只是影线）
    expect(fvgInvalidatedByClose([bar(1, 104)], T0, "bearish", 105, 102)).toBe(false);
  });

  it("形成之前的 K 线不参与判定（只看 created 之后）", () => {
    const rows = [bar(0, 90)]; // ts == createdTs 不算之后
    expect(fvgInvalidatedByClose(rows, T0, "bullish", 103, 100)).toBe(false);
    expect(fvgInvalidatedByClose([], T0, "bullish", 103, 100)).toBe(false);
  });
});
