// 非散户占比本机基线测试（U2）：包络合并/过期裁剪/区间位置/累积中降级/节流。
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  BASELINE_MIN_DAYS,
  epochDay,
  mergeSample,
  rangeInfo,
  recordShare,
  shareBaseline,
  type DayEnvelope,
} from "../tapeBaseline";

const DAY = 86_400_000;
const T0 = 1_755_000_000_000;

describe("mergeSample 包络合并", () => {
  it("同日扩展 min/max，跨日新增", () => {
    let days: DayEnvelope[] = [];
    days = mergeSample(days, 30, T0);
    days = mergeSample(days, 22, T0 + 3_600_000);
    days = mergeSample(days, 41, T0 + 7_200_000);
    expect(days).toHaveLength(1);
    expect(days[0]).toMatchObject({ lo: 22, hi: 41 });
    days = mergeSample(days, 35, T0 + DAY);
    expect(days).toHaveLength(2);
  });
  it("超 30 日裁剪 + 越界值夹紧 + 非法值忽略", () => {
    let days: DayEnvelope[] = [{ d: epochDay(T0) - 45, lo: 10, hi: 20 }];
    days = mergeSample(days, 150, T0);
    expect(days).toHaveLength(1);
    expect(days[0].hi).toBe(100);
    expect(mergeSample(days, NaN, T0)).toEqual(days);
  });
});

describe("rangeInfo 区间画像（诚实降级）", () => {
  const mk = (n: number): DayEnvelope[] =>
    Array.from({ length: n }, (_, i) => ({ d: epochDay(T0) - i, lo: 20 + i, hi: 40 + i }));
  it(`不足 ${BASELINE_MIN_DAYS} 日只给区间不给位置`, () => {
    const r = rangeInfo(mk(2), 30);
    expect(r).not.toBeNull();
    expect(r!.days).toBe(2);
    expect(r!.posPct).toBeNull();
  });
  it("达标后给出区间位置 0-100", () => {
    const r = rangeInfo(mk(5), 30);
    // 区间 [20, 44]：30 → (30-20)/24 ≈ 42%
    expect(r!.lo).toBe(20);
    expect(r!.hi).toBe(44);
    expect(r!.posPct).toBe(42);
  });
  it("区间退化（几乎恒定）→ 位置 50；空记录 → null", () => {
    const flat: DayEnvelope[] = Array.from({ length: 4 }, (_, i) => ({
      d: epochDay(T0) - i,
      lo: 28,
      hi: 28.3,
    }));
    expect(rangeInfo(flat, 28.1)!.posPct).toBe(50);
    expect(rangeInfo([], 30)).toBeNull();
  });
});

describe("recordShare/shareBaseline storage 层", () => {
  let store: Map<string, string>;
  beforeEach(() => {
    store = new Map();
    (globalThis as Record<string, unknown>).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
    };
  });
  afterEach(() => {
    delete (globalThis as Record<string, unknown>).localStorage;
  });

  it("round-trip：记录后可读回区间；60s 节流丢弃高频写", () => {
    recordShare("BTCUSDT", 30, T0);
    recordShare("BTCUSDT", 90, T0 + 3_000); // 节流窗口内，应被丢弃
    recordShare("BTCUSDT", 45, T0 + 61_000);
    const r = shareBaseline("BTCUSDT", 40, T0 + 61_000);
    expect(r).not.toBeNull();
    expect(r!.hi).toBe(45); // 90 被节流掉
    expect(r!.lo).toBe(30);
  });
  it("按币种隔离；无记录返回 null；storage 不可用不抛", () => {
    recordShare("BTCUSDT", 30, T0);
    expect(shareBaseline("ETHUSDT", 30, T0)).toBeNull();
    delete (globalThis as Record<string, unknown>).localStorage;
    expect(() => recordShare("BTCUSDT", 31, T0 + 120_000)).not.toThrow();
    expect(shareBaseline("BTCUSDT", 30, T0)).toBeNull();
  });
});
