// Footprint 统一数据服务 v2：组合行情源 + aggregator（聚合）+
// storage（IndexedDB 持久化），实现 src/types/footprint.ts 的
// FootprintDataService 契约（多币种 + 6 周期），导出单例 footprintDataService。
//
// v2.1 源开关：localStorage `jarvis-footprint-source` = 'real'（默认，
// 币安 USDT 永续合约真实行情，见 realDataService.ts）或 'mock'（本文件的
// FootprintDataServiceImpl 拟真源）。两源 IndexedDB 库名隔离。
// 以下 mock 版生命周期说明仅适用于 source='mock'：
//
// v2 生命周期（按 symbol 懒启动，互不影响）：
//   1. 首次请求某 symbol（getBars/subscribe）时创建该币种运行时并回填——
//      避免一次性拉 7 币 × 6 周期；
//   2. 回填跨度按周期分档（BACKFILL_SPAN_MS）：1m/5m/15m 共用近 50h 细节窗，
//      30m 100h（≥200 根）、4h 30 天（≥180 根）、1d 120 天（≥120 根）；
//      行情源按「年龄分层密度」流式生成 120 天逐笔（近密远疏，约 1M 笔/币），
//      各周期聚合器只消费自己跨度内的成交（懒快照 ingestFast，首载不卡）；
//   3. IndexedDB 存量新鲜（最后 1m 柱距今 ≤10min）时接续：各周期续用存量
//      末柱 cumDelta，只补断档区间；过旧则 clearSymbol 重建；
//   4. 实时流按 symbol 只跑一套：首个订阅者出现时启动，喂全部 6 个周期聚合
//      器并节流写库；该 symbol 订阅者清零时停流省资源，下次订阅/查询先补
//      停流期间的成交缺口（catch-up）再继续。
//
// 兼容过渡（MCP-4 并行改 UI 期间）：getBars/subscribe 保留 v1 旧签名重载
// （缺 symbol 参数时默认 BTCUSDT 并 console.warn 一次），UI 全量切换后移除。

import type { FootprintBar, FootprintDataService, Timeframe, Trade } from '../../types/footprint';
import { FootprintAggregator, TIMEFRAME_MS, TIMEFRAMES } from './aggregator';
import { getFootprintSource } from './binanceFeed';
import { basePriceOf, MockTradeFeed } from './mockFeed';
import { RealFootprintDataService } from './realDataService';
import { createFootprintStorage, type FootprintStorage } from './storage';

const HOUR = 3_600_000;
const DAY = 86_400_000;

/** 各周期回填跨度：保证缩小后能看走势（30m≥200 / 4h≥180 / 1d≥120 根） */
export const BACKFILL_SPAN_MS: Record<Timeframe, number> = {
  '1m': 50 * HOUR,   // 3000 根
  '5m': 50 * HOUR,   // 600 根
  '15m': 50 * HOUR,  // 200 根
  '30m': 100 * HOUR, // 200 根
  '4h': 30 * DAY,    // 180 根
  '1d': 120 * DAY,   // 120 根
};
const MAX_SPAN_MS = Math.max(...Object.values(BACKFILL_SPAN_MS));

/** 存量末 1m 柱距今在此阈值内则接续，否则清该币种重建 */
const RESUME_MAX_GAP_MS = 10 * 60_000;
/** 脏柱写回 IndexedDB 的节流间隔 */
const FLUSH_INTERVAL_MS = 800;
/** 停流后超过该间隔的再次访问才做 catch-up（避免高频小补） */
const CATCHUP_MIN_GAP_MS = 1_500;

const DEFAULT_SYMBOL = 'BTCUSDT';

/** 单币种运行时：行情源 + 6 周期聚合器 + 订阅者 */
interface SymbolRuntime {
  symbol: string;
  feed: MockTradeFeed;
  aggs: Map<Timeframe, FootprintAggregator>;
  listeners: Map<Timeframe, Set<(bar: FootprintBar) => void>>;
  startPromise: Promise<void> | null;
  /** 已消费到的成交时间（毫秒）；停流补档的锚点 */
  lastTradeTime: number;
  liveUnsub: (() => void) | null;
}

function isTimeframe(v: unknown): v is Timeframe {
  return typeof v === 'string' && v in TIMEFRAME_MS;
}

class FootprintDataServiceImpl implements FootprintDataService {
  private readonly storage: FootprintStorage = createFootprintStorage();
  private readonly runtimes = new Map<string, SymbolRuntime>();
  private readonly dirtyBars = new Map<string, FootprintBar>();
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private legacyWarned = false;

  // ------------------------------------------------------------ 对外契约

  getBars(symbol: string, timeframe: Timeframe, from: number, to: number): Promise<FootprintBar[]>;
  /** @deprecated v1 旧签名：缺 symbol，默认 BTCUSDT。UI 切换新契约后移除。 */
  getBars(timeframe: Timeframe, from: number, to: number): Promise<FootprintBar[]>;
  async getBars(
    a: string | Timeframe,
    b: Timeframe | number,
    c: number,
    d?: number,
  ): Promise<FootprintBar[]> {
    let symbol: string;
    let timeframe: Timeframe;
    let from: number;
    let to: number;
    if (isTimeframe(a) && typeof b === 'number' && d === undefined) {
      this.warnLegacy('getBars');
      symbol = DEFAULT_SYMBOL;
      timeframe = a;
      from = b;
      to = c;
    } else {
      symbol = String(a).toUpperCase();
      timeframe = b as Timeframe;
      from = c;
      to = d!;
    }
    if (!isTimeframe(timeframe)) throw new Error(`unsupported timeframe: ${String(timeframe)}`);

    const rt = this.runtime(symbol);
    await this.ensureStarted(rt);
    this.catchUp(rt);
    await this.flushDirty();
    return this.storage.getBars(symbol, timeframe, from, to);
  }

  subscribe(symbol: string, timeframe: Timeframe, cb: (bar: FootprintBar) => void): () => void;
  /** @deprecated v1 旧签名：缺 symbol，默认 BTCUSDT。UI 切换新契约后移除。 */
  subscribe(timeframe: Timeframe, cb: (bar: FootprintBar) => void): () => void;
  subscribe(
    a: string | Timeframe,
    b: Timeframe | ((bar: FootprintBar) => void),
    c?: (bar: FootprintBar) => void,
  ): () => void {
    let symbol: string;
    let timeframe: Timeframe;
    let cb: (bar: FootprintBar) => void;
    if (typeof b === 'function') {
      this.warnLegacy('subscribe');
      symbol = DEFAULT_SYMBOL;
      timeframe = a as Timeframe;
      cb = b;
    } else {
      symbol = String(a).toUpperCase();
      timeframe = b;
      cb = c!;
    }
    if (!isTimeframe(timeframe)) throw new Error(`unsupported timeframe: ${String(timeframe)}`);

    const rt = this.runtime(symbol);
    rt.listeners.get(timeframe)!.add(cb);

    // 懒启动 + 补档 + 起实时流（异常兜底打日志，订阅方收不到推送即启动失败）
    void this.ensureStarted(rt)
      .then(() => {
        this.catchUp(rt);
        this.ensureLive(rt);
      })
      .catch((err) => {
        console.error(`[footprint] ${symbol} data service start failed:`, err);
      });

    return () => {
      rt.listeners.get(timeframe)!.delete(cb);
      if (this.totalListeners(rt) === 0) this.stopLive(rt);
    };
  }

  // ------------------------------------------------------------ 运行时管理

  private runtime(symbol: string): SymbolRuntime {
    let rt = this.runtimes.get(symbol);
    if (!rt) {
      rt = {
        symbol,
        feed: new MockTradeFeed({ symbol }),
        aggs: new Map(),
        listeners: new Map(TIMEFRAMES.map((tf) => [tf, new Set()])),
        startPromise: null,
        lastTradeTime: 0,
        liveUnsub: null,
      };
      this.runtimes.set(symbol, rt);
    }
    return rt;
  }

  private totalListeners(rt: SymbolRuntime): number {
    let n = 0;
    for (const set of rt.listeners.values()) n += set.size;
    return n;
  }

  private warnLegacy(method: string): void {
    if (!this.legacyWarned) {
      this.legacyWarned = true;
      console.warn(
        `[footprint] ${method}() 旧签名（缺 symbol）已弃用，默认 ${DEFAULT_SYMBOL}；` +
          '请改用 (symbol, timeframe, ...) 新契约',
      );
    }
  }

  // ------------------------------------------------------------ 启动与回填

  private ensureStarted(rt: SymbolRuntime): Promise<void> {
    if (!rt.startPromise) {
      rt.startPromise = this.start(rt);
      rt.startPromise.catch(() => {
        rt.startPromise = null; // 失败允许下次重试
      });
    }
    return rt.startPromise;
  }

  private async start(rt: SymbolRuntime): Promise<void> {
    const now = Date.now();
    const { symbol } = rt;

    // 1. 探测存量（以 1m 为新鲜度基准）
    const recent1m = await this.storage.getBars(symbol, '1m', now - BACKFILL_SPAN_MS['1m'], now);
    const last1m = recent1m.length > 0 ? recent1m[recent1m.length - 1] : null;
    const resumable = last1m !== null && now - last1m.time <= RESUME_MAX_GAP_MS;

    let backfillFrom: number; // 各周期统一的回填起点（接续=断档起点）
    let perTfActivation: boolean; // 重建模式下按周期跨度过滤

    if (resumable) {
      // 2a. 接续：各周期续用存量末柱 cumDelta；行情源从存量收盘价起步
      backfillFrom = last1m.time + TIMEFRAME_MS['1m'];
      perTfActivation = false;
      rt.feed = new MockTradeFeed({ symbol, basePrice: last1m.close });
      for (const tf of TIMEFRAMES) {
        const stored = await this.storage.getBars(symbol, tf, now - BACKFILL_SPAN_MS[tf], now);
        const lastBar = stored.length > 0 ? stored[stored.length - 1] : null;
        rt.aggs.set(
          tf,
          new FootprintAggregator(symbol, tf, {
            tickSize: rt.feed.tickSize,
            initialCumDelta: lastBar ? lastBar.cumDelta : 0,
          }),
        );
      }
    } else {
      // 2b. 重建：清该币种全部周期，全量回填
      await this.storage.clearSymbol(symbol);
      backfillFrom = now - MAX_SPAN_MS;
      perTfActivation = true;
      rt.feed = new MockTradeFeed({ symbol });
      for (const tf of TIMEFRAMES) {
        rt.aggs.set(tf, new FootprintAggregator(symbol, tf, { tickSize: rt.feed.tickSize }));
      }
    }

    // 3. 流式回填：各周期只消费自己跨度内的成交（ingestFast 懒快照）
    const spanMs = now - backfillFrom;
    if (spanMs > 0) {
      const completed: FootprintBar[] = [];
      const activateAt: Record<Timeframe, number> = {} as Record<Timeframe, number>;
      for (const tf of TIMEFRAMES) {
        activateAt[tf] = perTfActivation ? now - BACKFILL_SPAN_MS[tf] : backfillFrom;
      }
      for (const trade of rt.feed.backfillStream(spanMs, now)) {
        for (const tf of TIMEFRAMES) {
          if (trade.time < activateAt[tf]) continue;
          const done = rt.aggs.get(tf)!.ingestFast(trade);
          if (done) completed.push(done);
        }
        rt.lastTradeTime = trade.time;
      }
      for (const tf of TIMEFRAMES) {
        const current = rt.aggs.get(tf)!.snapshotCurrent();
        if (current) completed.push(current);
      }
      await this.storage.putBars(completed);
    }
    if (rt.lastTradeTime === 0) rt.lastTradeTime = now;
  }

  // ------------------------------------------------------------ 实时与补档

  /** 停流期间的成交缺口补齐：同一条 feed 接续生成，价格/游走状态连续 */
  private catchUp(rt: SymbolRuntime): void {
    if (rt.feed.isLive() || rt.lastTradeTime === 0) return;
    const now = Date.now();
    const gap = now - rt.lastTradeTime;
    if (gap < CATCHUP_MIN_GAP_MS) return;
    for (const trade of rt.feed.backfillStream(gap, now)) {
      for (const tf of TIMEFRAMES) {
        const done = rt.aggs.get(tf)!.ingestFast(trade);
        if (done) this.markDirty(done);
      }
      rt.lastTradeTime = trade.time;
    }
    for (const tf of TIMEFRAMES) {
      const current = rt.aggs.get(tf)!.snapshotCurrent();
      if (current) this.markDirty(current);
    }
    rt.lastTradeTime = now;
  }

  private ensureLive(rt: SymbolRuntime): void {
    if (rt.liveUnsub) return;
    rt.liveUnsub = rt.feed.subscribe((trade) => this.onLiveTrade(rt, trade));
  }

  private stopLive(rt: SymbolRuntime): void {
    if (rt.liveUnsub) {
      rt.liveUnsub();
      rt.liveUnsub = null;
    }
    void this.flushDirty();
  }

  private onLiveTrade(rt: SymbolRuntime, trade: Trade): void {
    rt.lastTradeTime = trade.time;
    for (const tf of TIMEFRAMES) {
      const agg = rt.aggs.get(tf)!;
      const { bar, completedBar } = agg.ingest(trade);
      if (completedBar) this.markDirty(completedBar);
      this.markDirty(bar);
      for (const cb of rt.listeners.get(tf)!) {
        try {
          cb(bar);
        } catch (err) {
          console.error('[footprint] subscriber error:', err);
        }
      }
    }
  }

  // ------------------------------------------------------------ 持久化节流

  private markDirty(bar: FootprintBar): void {
    this.dirtyBars.set(`${bar.symbol}:${bar.timeframe}:${bar.time}`, bar);
    if (!this.flushTimer) {
      this.flushTimer = setTimeout(() => {
        void this.flushDirty();
      }, FLUSH_INTERVAL_MS);
    }
  }

  private async flushDirty(): Promise<void> {
    if (this.flushTimer) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }
    if (this.dirtyBars.size === 0) return;
    const bars = [...this.dirtyBars.values()];
    this.dirtyBars.clear();
    try {
      await this.storage.putBars(bars);
    } catch (err) {
      console.error('[footprint] persist failed:', err);
    }
  }
}

/**
 * 全局单例：图表层直接 import 使用。按源开关（localStorage
 * `jarvis-footprint-source`，默认 real）在模块加载时二选一；切换后刷新生效。
 */
export const footprintDataService: FootprintDataService =
  getFootprintSource() === 'mock' ? new FootprintDataServiceImpl() : new RealFootprintDataService();
