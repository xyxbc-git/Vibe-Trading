// binanceFeed 单测（纯 mock，不连网）：方向口径 / kline delta 反推 /
// 分页去重 / 断线重连补档的有序去重回放。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Timeframe } from '../../types/footprint';
import {
  BinanceLiveFeed,
  fetchRecentAggTrades,
  klineDelta,
  klineToBar,
  toTick,
  type AggTradeTick,
  type KlineData,
} from '../footprint/binanceFeed';

// ---------------------------------------------------------------- fakes

/** 可编程 WebSocket 替身：记录实例，测试端手动触发事件 */
class FakeWS {
  static instances: FakeWS[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWS.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  emitOpen(): void {
    this.onopen?.();
  }

  emitStreamData(data: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify({ stream: 's', data }) });
  }

  emitClose(): void {
    this.onclose?.();
  }
}

function aggTradeMsg(aggId: number, price: number, qty: number, maker: boolean, time = aggId * 10) {
  return { e: 'aggTrade', a: aggId, p: String(price), q: String(qty), T: time, m: maker };
}

/** fetch 替身：按 URL 子串路由到应答队列 */
function stubFetch(router: (url: string) => unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => router(String(url)),
    })),
  );
}

beforeEach(() => {
  FakeWS.instances = [];
  vi.stubGlobal('WebSocket', FakeWS as unknown as typeof WebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const flush = () => new Promise((r) => setTimeout(r, 0));

// ---------------------------------------------------------------- tests

describe('toTick 方向口径', () => {
  it('m=true 买方是 maker → 主动卖；m=false → 主动买', () => {
    expect(toTick({ a: 1, p: '1870.5', q: '0.3', T: 111, m: true }).side).toBe('sell');
    expect(toTick({ a: 2, p: '1870.6', q: '0.2', T: 112, m: false }).side).toBe('buy');
    const t = toTick({ a: 3, p: '1870.7', q: '1.5', T: 113, m: false });
    expect(t).toMatchObject({ aggId: 3, price: 1870.7, size: 1.5, time: 113 });
  });
});

describe('klineDelta / klineToBar', () => {
  const k: KlineData = {
    openTime: 60_000,
    open: 100,
    high: 110,
    low: 95,
    close: 105,
    volume: 1000,
    takerBuyVolume: 620,
    closed: true,
  };

  it('delta = taker买 − taker卖 = 2*takerBuy − vol', () => {
    expect(klineDelta(k)).toBeCloseTo(240); // 620 - 380
  });

  it('klineToBar 产出 ohlcOnly 柱：levels 空、poc=close', () => {
    const bar = klineToBar('ETHUSDT', '4h' as Timeframe, k, 999);
    expect(bar).toMatchObject({
      symbol: 'ETHUSDT',
      timeframe: '4h',
      time: 60_000,
      totalVol: 1000,
      delta: 240,
      cumDelta: 999,
      poc: 105,
      ohlcOnly: true,
    });
    expect(bar.levels).toEqual([]);
  });
});

describe('fetchRecentAggTrades 逆序分页', () => {
  it('两页拼接升序、页界去重、窗口外裁剪', async () => {
    const now = Date.now();
    // 新页：aggId 1000..1999，时间全在窗口内；
    // 旧页：aggId 0..999，时间 now-350s 起每 100ms 一笔 → 跨越窗口起点 now-300s
    const mkPage = (fromId: number, baseTime: number, stepMs: number) =>
      Array.from({ length: 1000 }, (_, i) => ({
        a: fromId + i,
        p: '1870',
        q: '0.1',
        T: baseTime + i * stepMs,
        m: i % 2 === 0,
      }));
    const newest = mkPage(1000, now - 100_000, 10);
    const older = mkPage(0, now - 350_000, 100);
    let call = 0;
    stubFetch((url) => {
      expect(url).toContain('/fapi/v1/aggTrades');
      call += 1;
      return call === 1 ? newest : older;
    });

    const res = await fetchRecentAggTrades('ETHUSDT', 300_000, 5);
    expect(res.lastAggId).toBe(1999);
    // 全序列 aggId 严格递增
    for (let i = 1; i < res.ticks.length; i++) {
      expect(res.ticks[i].aggId).toBeGreaterThan(res.ticks[i - 1].aggId);
    }
    // 旧页前半（< now-300s）被裁掉，后半保留：总量 (1000, 2000) 之间
    expect(res.ticks[0].time).toBeGreaterThanOrEqual(now - 300_000);
    expect(res.ticks.length).toBeLessThan(2000);
    expect(res.ticks.length).toBeGreaterThan(1000);
  });
});

describe('BinanceLiveFeed 断线重连补档', () => {
  it('无锚点首连：直接透传，aggId 严格递增去重', async () => {
    stubFetch((url) => (url.includes('/fapi/v1/klines') ? [] : []));
    const seen: AggTradeTick[] = [];
    const feed = new BinanceLiveFeed('ETHUSDT', [], {
      onTrade: (t) => seen.push(t),
      onKline: () => {},
      onGap: () => {},
    });
    feed.start();
    const ws = FakeWS.instances[0];
    ws.emitOpen();
    await flush();
    ws.emitStreamData(aggTradeMsg(10, 1870, 0.1, false));
    ws.emitStreamData(aggTradeMsg(11, 1870.1, 0.2, true));
    ws.emitStreamData(aggTradeMsg(11, 1870.1, 0.2, true)); // 重复推送
    ws.emitStreamData(aggTradeMsg(12, 1870.2, 0.3, false));
    expect(seen.map((t) => t.aggId)).toEqual([10, 11, 12]);
    feed.stop();
  });

  it('断线重连：REST 补 [锚点+1..]，与 WS 缓冲重叠去重、输出连续', async () => {
    // 断档区间 21..25 由 REST 补；重连后 WS 先缓冲 24..27，回放去重。
    // aggTrades mock 按 fromId 应答：fromId=21 → 21..25；更大 → 空（已追平）
    stubFetch((url) => {
      if (url.includes('/fapi/v1/aggTrades')) {
        const fromId = Number(new URL(url).searchParams.get('fromId'));
        if (fromId > 25) return [];
        return [21, 22, 23, 24, 25]
          .filter((a) => a >= fromId)
          .map((a) => ({ a, p: '1870', q: '0.1', T: a * 10, m: false }));
      }
      return []; // klines 重放（本测试 klineTfs 为空不会触发）
    });

    const seen: number[] = [];
    let gap: string | null = null;
    const feed = new BinanceLiveFeed(
      'ETHUSDT',
      [],
      {
        onTrade: (t) => seen.push(t.aggId),
        onKline: () => {},
        onGap: (r) => {
          gap = r;
        },
      },
      20, // 锚点：断线前最后消费 aggId=20
    );
    feed.start();
    const ws1 = FakeWS.instances[0];
    ws1.emitOpen();
    await flush();
    // 首连即有锚点 → fillGap 已跑：21..25 先到
    expect(seen).toEqual([21, 22, 23, 24, 25]);

    // 模拟断线 → 1s 后自动重连
    ws1.emitClose();
    await vi.waitFor(() => expect(FakeWS.instances.length).toBe(2), { timeout: 3_000 });
    const ws2 = FakeWS.instances[1];
    // 重连后 WS 缓冲期推送（与 REST 补档尾部重叠 + 新增）
    ws2.emitOpen();
    ws2.emitStreamData(aggTradeMsg(24, 1870, 0.1, false));
    ws2.emitStreamData(aggTradeMsg(26, 1870, 0.1, true));
    await flush(); // fillGap（再次从 26 拉，mock 返回同一批 21..25 全被去重）
    ws2.emitStreamData(aggTradeMsg(27, 1870.2, 0.5, false));
    await flush();

    // 全程无重复、严格递增
    expect(seen).toEqual([21, 22, 23, 24, 25, 26, 27]);
    expect(gap).toBeNull();
    feed.stop();
  });

  it('kline 流：解析 k 字段并回调对应周期', async () => {
    stubFetch(() => []);
    const klines: { tf: Timeframe; k: KlineData }[] = [];
    const feed = new BinanceLiveFeed('ETHUSDT', ['4h'], {
      onTrade: () => {},
      onKline: (tf, k) => klines.push({ tf, k }),
      onGap: () => {},
    });
    feed.start();
    const ws = FakeWS.instances[0];
    ws.emitOpen();
    await flush();
    ws.emitStreamData({
      e: 'kline',
      k: { i: '4h', t: 14_400_000, o: '100', h: '120', l: '90', c: '110', v: '5000', V: '2800', x: false },
    });
    expect(klines).toHaveLength(1);
    expect(klines[0].tf).toBe('4h');
    expect(klines[0].k).toMatchObject({
      openTime: 14_400_000,
      close: 110,
      volume: 5000,
      takerBuyVolume: 2800,
      closed: false,
    });
    // 不在订阅列表的周期忽略
    ws.emitStreamData({
      e: 'kline',
      k: { i: '1d', t: 0, o: '1', h: '1', l: '1', c: '1', v: '1', V: '1', x: true },
    });
    expect(klines).toHaveLength(1);
    feed.stop();
  });
});
