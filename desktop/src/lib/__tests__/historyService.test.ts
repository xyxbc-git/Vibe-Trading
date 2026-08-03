// 历史足迹回看数据层单测：后端响应 → FootprintBar 转换 + 日期区间工具 + 拉取容错。
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  dayRangeMs,
  fetchHistoryBars,
  fmtLocalDate,
  isHistoryTimeframe,
  toFootprintBar,
} from '../footprint/historyService';

const API_BAR = {
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
    { price: 100.05, buy: 800, sell: 200, flag: 'buy_imb' as const },
    { price: 100.0, buy: 100, sell: 50, flag: null },
    { price: 100.1, buy: 4600, sell: 2000, flag: null }, // 乱序：应被重排为降序
  ],
};

describe('toFootprintBar', () => {
  it('converts backend bar: levels 降序、bidVol=sell、askVol=buy、poc=量能最大档', () => {
    const bar = toFootprintBar('BTCUSDT', '1m', API_BAR)!;
    expect(bar).not.toBeNull();
    expect(bar.symbol).toBe('BTCUSDT');
    expect(bar.timeframe).toBe('1m');
    expect(bar.time).toBe(API_BAR.ts * 1000);
    expect(bar.levels.map((l) => l.price)).toEqual([100.1, 100.05, 100.0]);
    const top = bar.levels[0];
    expect(top.askVol).toBe(4600); // buy → askVol（右列）
    expect(top.bidVol).toBe(2000); // sell → bidVol（左列）
    expect(bar.poc).toBe(100.1); // 4600+2000 量能最大
    expect(bar.delta).toBe(3250);
    expect(bar.cumDelta).toBe(3250);
    expect(bar.totalVol).toBe(7750);
    expect(bar.ohlcOnly).toBeUndefined();
  });

  it('degrades rows=[] bar to ohlcOnly（历史时段只有分钟聚合）', () => {
    const bar = toFootprintBar('BTCUSDT', '15m', {
      ...API_BAR,
      rows: [],
      total: 1200,
      buy: 500,
      sell: 700,
      delta: -200,
      cvd: 3050,
    })!;
    expect(bar.ohlcOnly).toBe(true);
    expect(bar.levels).toEqual([]);
    expect(bar.poc).toBe(100.5); // 无档位 → close 兜底
    expect(bar.cumDelta).toBe(3050);
  });

  it('fills missing OHLC from levels，全缺且无档位时返回 null', () => {
    const noOhlc = toFootprintBar('BTCUSDT', '1m', {
      ...API_BAR,
      open: null,
      high: null,
      low: null,
      close: null,
    })!;
    expect(noOhlc).not.toBeNull();
    expect(noOhlc.close).toBe(100.1); // poc 兜底
    expect(noOhlc.high).toBeGreaterThanOrEqual(100.1);
    expect(noOhlc.low).toBeLessThanOrEqual(100.0);
    expect(
      toFootprintBar('BTCUSDT', '1m', {
        ...API_BAR,
        open: null,
        high: null,
        low: null,
        close: null,
        rows: [],
      }),
    ).toBeNull();
  });
});

describe('history timeframe / date helpers', () => {
  it('isHistoryTimeframe 只放行后端支持的 1m/5m/15m/30m', () => {
    expect(isHistoryTimeframe('1m')).toBe(true);
    expect(isHistoryTimeframe('30m')).toBe(true);
    expect(isHistoryTimeframe('4h')).toBe(false);
    expect(isHistoryTimeframe('1d')).toBe(false);
  });

  it('dayRangeMs：整天区间 = [0点, 次日0点)，未来部分截到现在', () => {
    const past = dayRangeMs('2020-01-02');
    expect(past.fromMs).toBe(new Date('2020-01-02T00:00:00').getTime());
    expect(past.toMs - past.fromMs).toBe(86_400_000);
    const today = dayRangeMs(fmtLocalDate(new Date()));
    expect(today.toMs).toBeLessThanOrEqual(Date.now() + 1000);
    expect(today.toMs).toBeGreaterThan(today.fromMs);
  });

  it('fmtLocalDate 补零', () => {
    expect(fmtLocalDate(new Date(2026, 0, 5))).toBe('2026-01-05');
  });
});

describe('fetchHistoryBars', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('拉取成功：解析 bars、时间升序、请求带 start/end 秒', async () => {
    const calls: string[] = [];
    vi.stubGlobal('fetch', (url: string) => {
      calls.push(String(url));
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            ok: true,
            bars: [
              { ...API_BAR, ts: API_BAR.ts + 60, cvd: 100 },
              { ...API_BAR },
            ],
          }),
      } as Response);
    });
    const bars = await fetchHistoryBars('BTCUSDT', '1m', 1_000_000_000_000, 1_000_000_060_000);
    expect(bars).toHaveLength(2);
    expect(bars[0].time).toBeLessThan(bars[1].time);
    expect(calls[0]).toContain('/api/tape/footprint?');
    expect(calls[0]).toContain('start=1000000000');
    expect(calls[0]).toContain('end=1000000060');
    expect(calls[0]).toContain('interval=1m');
  });

  it('后端 ok=false → 抛出带 error 文案的异常', async () => {
    vi.stubGlobal('fetch', () =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ok: false, error: 'interval 无效' }),
      } as Response),
    );
    await expect(fetchHistoryBars('BTCUSDT', '1m', 0, 60_000)).rejects.toThrow('interval 无效');
  });

  it('HTTP 非 2xx → 抛出状态码异常', async () => {
    vi.stubGlobal('fetch', () => Promise.resolve({ ok: false, status: 502 } as Response));
    await expect(fetchHistoryBars('BTCUSDT', '1m', 0, 60_000)).rejects.toThrow('502');
  });
});
