// 后端冷启动 seed 单测：核心验证 USD 名义额 → 币量 的换算正确、cumDelta 逐桶累计、
// rows=[] 降级 ohlcOnly、以及 /api/tape/footprint 与 /api/tape/bars 的拉取容错。
// 另覆盖左滚分页回补：区间模式拉取（fetchFootprintRange/fetchOhlcRange）与
// mergeOlderBars 的去重拼接 + cumDelta 重定基。
import { describe, expect, it } from 'vitest';
import type { FootprintBar } from '../../types/footprint';
import {
  fetchFootprintRange,
  fetchFootprintSeed,
  fetchOhlcRange,
  fetchOhlcSeed,
  fpBarToCoin,
  mergeOlderBars,
  type FetchLike,
} from '../footprint/backendSeed';

const FP_BAR = {
  ts: 1_720_000_000 - (1_720_000_000 % 60),
  open: 100.0,
  high: 101.0,
  low: 99.5,
  close: 100.5,
  total: 7750,
  buy: 5500,
  sell: 2250,
  delta: 3250,
  cvd: 3250,
  trades: 12,
  rows: [
    { price: 100.0, buy: 1000, sell: 500, flag: null }, // 乱序：应重排为降序
    { price: 100.1, buy: 2000, sell: 1000, flag: null },
  ],
};

/** 构造一个返回固定 JSON 的 FetchLike，并记录被请求的 URL */
function stubFetch(body: unknown, urls: string[] = []): FetchLike {
  return (url: string) => {
    urls.push(url);
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(body),
    });
  };
}

describe('fpBarToCoin：USD → 币量换算', () => {
  it('每档 askVol=buy/price、bidVol=sell/price；levels 降序；poc=币量最大档', () => {
    const conv = fpBarToCoin('BTCUSDT', '1m', FP_BAR, 0)!;
    expect(conv).not.toBeNull();
    const bar = conv.bar;
    expect(bar.levels.map((l) => l.price)).toEqual([100.1, 100.0]);
    // 100.1 档：买 2000/100.1、卖 1000/100.1
    expect(bar.levels[0].askVol).toBeCloseTo(2000 / 100.1, 9);
    expect(bar.levels[0].bidVol).toBeCloseTo(1000 / 100.1, 9);
    // 100.0 档：买 1000/100、卖 500/100
    expect(bar.levels[1].askVol).toBeCloseTo(10, 9);
    expect(bar.levels[1].bidVol).toBeCloseTo(5, 9);
    // totalVol = 所有档币量之和
    const expTotal = 2000 / 100.1 + 1000 / 100.1 + 10 + 5;
    expect(bar.totalVol).toBeCloseTo(expTotal, 9);
    // delta = Σ(askVol - bidVol)
    const expDelta = 2000 / 100.1 - 1000 / 100.1 + 10 - 5;
    expect(bar.delta).toBeCloseTo(expDelta, 9);
    // poc：100.1 档币量(约29.97) > 100.0 档(15) → 100.1
    expect(bar.poc).toBe(100.1);
    expect(bar.ohlcOnly).toBeUndefined();
    // cumBefore=0 → cumDelta=delta
    expect(conv.cumDelta).toBeCloseTo(expDelta, 9);
  });

  it('cumBefore 接续：cumDelta = cumBefore + 本柱 delta', () => {
    const conv = fpBarToCoin('BTCUSDT', '1m', FP_BAR, 1000)!;
    const expDelta = 2000 / 100.1 - 1000 / 100.1 + 10 - 5;
    expect(conv.cumDelta).toBeCloseTo(1000 + expDelta, 6);
    expect(conv.bar.cumDelta).toBeCloseTo(1000 + expDelta, 6);
  });

  it('rows=[] → ohlcOnly，整桶 USD 按 close 近似成币量', () => {
    const conv = fpBarToCoin(
      'BTCUSDT',
      '15m',
      { ...FP_BAR, rows: [], total: 2010, buy: 1005, sell: 1005, delta: 0 },
      500,
    )!;
    expect(conv.bar.ohlcOnly).toBe(true);
    expect(conv.bar.levels).toEqual([]);
    expect(conv.bar.totalVol).toBeCloseTo(2010 / 100.5, 9); // 20
    expect(conv.bar.delta).toBeCloseTo(0, 9);
    expect(conv.bar.poc).toBe(100.5);
    expect(conv.bar.cumDelta).toBeCloseTo(500, 9);
  });

  it('OHLC 全缺且无档位 → 返回 null（不可渲染）', () => {
    expect(
      fpBarToCoin('BTCUSDT', '1m', {
        ...FP_BAR,
        open: null,
        high: null,
        low: null,
        close: null,
        rows: [],
      }, 0),
    ).toBeNull();
  });
});

describe('fetchFootprintSeed', () => {
  it('时间升序、cumDelta 跨桶累计、请求走 /api/tape/footprint 近期模式', async () => {
    const urls: string[] = [];
    const f = stubFetch(
      {
        ok: true,
        bars: [
          { ...FP_BAR, ts: FP_BAR.ts + 60 }, // 乱序：更晚在前
          { ...FP_BAR },
        ],
      },
      urls,
    );
    const bars = await fetchFootprintSeed('BTCUSDT', '5m', 60, f);
    expect(bars).toHaveLength(2);
    expect(bars[0].time).toBeLessThan(bars[1].time);
    // 第二根 cumDelta = 第一根 delta + 第二根 delta（累计）
    expect(bars[1].cumDelta).toBeCloseTo(bars[0].delta + bars[1].delta, 6);
    expect(urls[0]).toContain('/api/tape/footprint?');
    expect(urls[0]).toContain('interval=5m');
    expect(urls[0]).toContain('limit=60');
  });

  it('后端 ok=false / 空 bars → 返回 []（调用方据此回退币安）', async () => {
    expect(await fetchFootprintSeed('BTCUSDT', '1m', 60, stubFetch({ ok: false }))).toEqual([]);
    expect(
      await fetchFootprintSeed('BTCUSDT', '1m', 60, stubFetch({ ok: true, bars: [] })),
    ).toEqual([]);
  });
});

describe('fetchOhlcSeed', () => {
  it('/api/tape/bars → ohlcOnly 币量柱，delta=(buy-sell)/close 累计', async () => {
    const urls: string[] = [];
    const f = stubFetch(
      {
        ok: true,
        bars: [
          { ts: 1000, buy: 2000, sell: 1000, open: 100, high: 101, low: 99, close: 100 },
          { ts: 1000 + 14400, buy: 3000, sell: 3000, open: 100, high: 102, low: 98, close: 100 },
        ],
      },
      urls,
    );
    const bars = await fetchOhlcSeed('BTCUSDT', '4h', 240, f);
    expect(bars).toHaveLength(2);
    expect(bars.every((b) => b.ohlcOnly === true && b.levels.length === 0)).toBe(true);
    expect(bars[0].delta).toBeCloseTo((2000 - 1000) / 100, 9); // 10
    expect(bars[0].totalVol).toBeCloseTo((2000 + 1000) / 100, 9); // 30
    expect(bars[1].delta).toBeCloseTo(0, 9);
    // cumDelta 累计：第二根 = 10 + 0
    expect(bars[1].cumDelta).toBeCloseTo(10, 6);
    expect(urls[0]).toContain('/api/tape/bars?');
    expect(urls[0]).toContain('interval=4h');
  });

  it('close 缺失/非正 → 跳过该桶（避免除零）', async () => {
    const f = stubFetch({
      ok: true,
      bars: [{ ts: 1, buy: 1, sell: 1, open: null, high: null, low: null, close: null }],
    });
    expect(await fetchOhlcSeed('BTCUSDT', '1d', 240, f)).toEqual([]);
  });
});

describe('fetchFootprintRange（左滚分页·区间模式）', () => {
  it('请求带 start/end（epoch 秒），换算同 seed、时间升序、cumDelta 从 0 累计', async () => {
    const urls: string[] = [];
    const f = stubFetch(
      {
        ok: true,
        bars: [
          { ...FP_BAR, ts: FP_BAR.ts + 60 }, // 乱序：应重排为升序
          { ...FP_BAR },
        ],
      },
      urls,
    );
    const fromMs = (FP_BAR.ts - 180 * 60) * 1000;
    const toMs = FP_BAR.ts * 1000 + 59_999;
    const bars = await fetchFootprintRange('BTCUSDT', '1m', fromMs, toMs, f);
    expect(bars).toHaveLength(2);
    expect(bars[0].time).toBeLessThan(bars[1].time);
    expect(bars[1].cumDelta).toBeCloseTo(bars[0].delta + bars[1].delta, 6);
    expect(urls[0]).toContain('/api/tape/footprint?');
    expect(urls[0]).toContain(`start=${Math.floor(fromMs / 1000)}`);
    expect(urls[0]).toContain(`end=${Math.floor(toMs / 1000)}`);
  });

  it('区间内无数据 → 返回 []（调用方视为到达本地起点）', async () => {
    expect(await fetchFootprintRange('BTCUSDT', '1m', 1000, 2000, stubFetch({ ok: true, bars: [] }))).toEqual([]);
  });

  it('后端 ok=false → 抛错（调用方做冷却重试，与 seed 的静默 [] 不同）', async () => {
    await expect(
      fetchFootprintRange('BTCUSDT', '1m', 1000, 2000, stubFetch({ ok: false, error: 'db gone' })),
    ).rejects.toThrow('db gone');
  });
});

describe('fetchOhlcRange（大周期左滚分页）', () => {
  it('请求 /api/tape/bars 带 start/end，返回 ohlcOnly 币量柱', async () => {
    const urls: string[] = [];
    const f = stubFetch(
      {
        ok: true,
        bars: [{ ts: 1000, buy: 2000, sell: 1000, open: 100, high: 101, low: 99, close: 100 }],
      },
      urls,
    );
    const bars = await fetchOhlcRange('BTCUSDT', '4h', 1_000_000, 2_000_000, f);
    expect(bars).toHaveLength(1);
    expect(bars[0].ohlcOnly).toBe(true);
    expect(bars[0].delta).toBeCloseTo(10, 9);
    expect(urls[0]).toContain('/api/tape/bars?');
    expect(urls[0]).toContain('start=1000');
    expect(urls[0]).toContain('end=2000');
  });

  it('后端 ok=false → 抛错', async () => {
    await expect(
      fetchOhlcRange('BTCUSDT', '1d', 1000, 2000, stubFetch({ ok: false })),
    ).rejects.toThrow();
  });
});

describe('mergeOlderBars（拼接去重 + cumDelta 重定基）', () => {
  /** 造一根最小可用柱：cumDelta 由调用方给（模拟 chunk 内自洽累计） */
  const mkBar = (time: number, delta: number, cumDelta: number): FootprintBar => ({
    symbol: 'BTCUSDT',
    time,
    timeframe: '1m',
    open: 100,
    high: 101,
    low: 99,
    close: 100,
    levels: [],
    totalVol: Math.abs(delta) * 2,
    delta,
    cumDelta,
    poc: 100,
    ohlcOnly: true,
  });

  it('去重：time >= 既有首柱的 older 柱被丢弃，不产生重叠 bar', () => {
    const existing = [mkBar(600_000, 1, 1), mkBar(660_000, 2, 3)];
    const older = [mkBar(540_000, 5, 5), mkBar(600_000, 9, 14)]; // 第二根与首柱同桶
    const { bars, prepended } = mergeOlderBars(existing, older);
    expect(prepended).toBe(1);
    expect(bars.map((b) => b.time)).toEqual([540_000, 600_000, 660_000]);
    // 无重复 time
    expect(new Set(bars.map((b) => b.time)).size).toBe(bars.length);
  });

  it('cumDelta 重定基：拼接后每柱 cumDelta 增量仍等于该柱 delta（累计曲线无跳变）', () => {
    // 既有序列：seed 起点累计 = 10（首柱 delta 4 → cum 10，前置基线 6）
    const existing = [mkBar(600_000, 4, 10), mkBar(660_000, -1, 9)];
    // chunk 内自洽（从 0 起）：delta 2 → cum 2；delta 3 → cum 5
    const older = [mkBar(480_000, 2, 2), mkBar(540_000, 3, 5)];
    const { bars, prepended } = mergeOlderBars(existing, older);
    expect(prepended).toBe(2);
    // chunk 末柱重定基到既有首柱前置基线：10 - 4 = 6 → offset = 6 - 5 = 1
    expect(bars[0].cumDelta).toBeCloseTo(3, 9); // 2 + 1
    expect(bars[1].cumDelta).toBeCloseTo(6, 9); // 5 + 1
    // 跨界处：首柱 cumDelta(10) - chunk 末柱 cumDelta(6) = 首柱 delta(4)
    for (let i = 1; i < bars.length; i++) {
      expect(bars[i].cumDelta - bars[i - 1].cumDelta).toBeCloseTo(bars[i].delta, 9);
    }
    // 既有序列对象未被改动（纯函数）
    expect(existing[0].cumDelta).toBe(10);
  });

  it('older 全部与既有重叠或为空 → prepended=0（到达本地数据起点）', () => {
    const existing = [mkBar(600_000, 1, 1)];
    expect(mergeOlderBars(existing, []).prepended).toBe(0);
    expect(mergeOlderBars(existing, [mkBar(600_000, 1, 1)]).prepended).toBe(0);
    expect(mergeOlderBars(existing, [mkBar(700_000, 1, 1)]).prepended).toBe(0);
  });

  it('既有为空 → older 原样返回（首次加载前的极端时序）', () => {
    const older = [mkBar(480_000, 2, 2)];
    const { bars, prepended } = mergeOlderBars([], older);
    expect(prepended).toBe(1);
    expect(bars).toEqual(older);
  });
});
