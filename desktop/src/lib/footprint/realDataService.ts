// Footprint 真实数据服务：币安 USDT 永续合约（binanceFeed）+ aggregator +
// storage，实现 FootprintDataService 契约。与 mock 版（dataService.ts 内
// FootprintDataServiceImpl）并列，由源开关（getFootprintSource）选择单例。
//
// 每 symbol 生命周期（懒启动，会话内一次）：
//   1. 清库重建（真实库 `jarvis-footprint-real` 与 mock 隔离）；
//   2. 六周期 klines 铺底 → ohlcOnly 柱（OHLCV + taker 反推 delta，
//      cumDelta 序列内累计，levels 空，UI 画蜡烛）；
//   3. aggTrades 近窗逐笔（预算 60 页 ×1000 笔）灌入 1m/5m/15m/30m 聚合器，
//      覆盖近窗内起点完整的柱为真足迹柱（cumDelta 接续 klines 基线）；
//   4. 实时 WS：aggTrade 喂聚合器推足迹柱（当前柱快照 100ms 节流）；
//      kline 流维护聚合覆盖不到的大周期当前柱（30m 残缺兜底 + 4h/1d）；
//   5. 断线重连由 BinanceLiveFeed 自治（指数退避 + aggId 锚点 REST 补档）；
//      不可精确补齐时（onGap）整币种重建，订阅不断。

import type { FootprintBar, FootprintDataService, Timeframe, Trade } from '../../types/footprint';
import { FootprintAggregator, inferTickSize, TIMEFRAME_MS, TIMEFRAMES } from './aggregator';
import {
  BinanceLiveFeed,
  fetchKlines,
  fetchRecentAggTrades,
  klineToBar,
  type AggTradeTick,
  type KlineData,
  klineDelta,
} from './binanceFeed';
import { createFootprintStorage, type FootprintStorage } from './storage';

/** 真实源 IndexedDB 库名（与 mock 的 jarvis-footprint 隔离） */
export const REAL_DB_NAME = 'jarvis-footprint-real';

/** 逐笔聚合出足迹格的周期（近窗内）；4h/1d 始终 ohlcOnly */
const AGG_TFS: readonly Timeframe[] = ['1m', '5m', '15m', '30m'];
/** WS kline 流维护 ohlcOnly 当前柱的周期（30m 为近窗不足时的兜底） */
const KLINE_LIVE_TFS: readonly Timeframe[] = ['30m', '4h', '1d'];

/** 各周期 klines 铺底根数（单页 1500，1m 需 2 页，其余 1 页） */
const KLINE_SPAN_BARS: Record<Timeframe, number> = {
  '1m': 3000, // 50h
  '5m': 1000, // ~3.5d
  '15m': 1000, // ~10d
  '30m': 1000, // ~20d
  '4h': 720, // 120d
  '1d': 365, // 1y
};

/** aggTrades 近窗回填预算：60 页 ×1000 笔（weight 20/页，预算 1200） */
const AGG_TRADES_MAX_PAGES = 60;
/** 近窗目标跨度（预算内尽力；活跃行情下实际覆盖以回执为准） */
const AGG_TRADES_WINDOW_MS = 4 * 3_600_000;

/** 脏柱写库节流（与 mock 版一致） */
const FLUSH_INTERVAL_MS = 800;
/** 实时当前柱快照推送节流（完结柱不节流） */
const SNAPSHOT_THROTTLE_MS = 100;

/** kline 流 cumDelta 维护：cumBase = 当前柱开始前的累计 delta */
interface KlineTfState {
  curTime: number;
  cumBase: number;
  lastDelta: number;
}

interface RealSymbolRuntime {
  symbol: string;
  startPromise: Promise<void> | null;
  aggs: Map<Timeframe, FootprintAggregator>;
  listeners: Map<Timeframe, Set<(bar: FootprintBar) => void>>;
  klineState: Map<Timeframe, KlineTfState>;
  feed: BinanceLiveFeed | null;
  /** 逐笔数据流起点：早于它开始的柱是残缺柱，走 ohlcOnly */
  dataStart: number;
  /** 实时流断档补档锚点（最后消费的 aggTradeId） */
  lastAggId: number;
  /** 当前柱快照节流：待推送的周期集合 */
  snapshotPending: Set<Timeframe>;
  snapshotTimer: ReturnType<typeof setTimeout> | null;
}

function isTimeframe(v: unknown): v is Timeframe {
  return typeof v === 'string' && v in TIMEFRAME_MS;
}

export class RealFootprintDataService implements FootprintDataService {
  private readonly storage: FootprintStorage = createFootprintStorage(REAL_DB_NAME);
  private readonly runtimes = new Map<string, RealSymbolRuntime>();
  private readonly dirtyBars = new Map<string, FootprintBar>();
  private flushTimer: ReturnType<typeof setTimeout> | null = null;

  // ------------------------------------------------------------ 对外契约

  async getBars(
    symbol: string,
    timeframe: Timeframe,
    from: number,
    to: number,
  ): Promise<FootprintBar[]> {
    if (!isTimeframe(timeframe)) throw new Error(`unsupported timeframe: ${String(timeframe)}`);
    const rt = this.runtime(String(symbol).toUpperCase());
    await this.ensureStarted(rt);
    await this.flushDirty();
    return this.storage.getBars(rt.symbol, timeframe, from, to);
  }

  subscribe(symbol: string, timeframe: Timeframe, cb: (bar: FootprintBar) => void): () => void {
    if (!isTimeframe(timeframe)) throw new Error(`unsupported timeframe: ${String(timeframe)}`);
    const rt = this.runtime(String(symbol).toUpperCase());
    rt.listeners.get(timeframe)!.add(cb);

    void this.ensureStarted(rt)
      .then(() => {
        if (this.totalListeners(rt) > 0) this.ensureLive(rt);
      })
      .catch((err) => {
        console.error(`[footprint] ${rt.symbol} real data service start failed:`, err);
      });

    return () => {
      rt.listeners.get(timeframe)!.delete(cb);
      if (this.totalListeners(rt) === 0) this.stopLive(rt);
    };
  }

  // ------------------------------------------------------------ 运行时

  private runtime(symbol: string): RealSymbolRuntime {
    let rt = this.runtimes.get(symbol);
    if (!rt) {
      rt = {
        symbol,
        startPromise: null,
        aggs: new Map(),
        listeners: new Map(TIMEFRAMES.map((tf) => [tf, new Set()])),
        klineState: new Map(),
        feed: null,
        dataStart: Number.POSITIVE_INFINITY,
        lastAggId: -1,
        snapshotPending: new Set(),
        snapshotTimer: null,
      };
      this.runtimes.set(symbol, rt);
    }
    return rt;
  }

  private totalListeners(rt: RealSymbolRuntime): number {
    let n = 0;
    for (const set of rt.listeners.values()) n += set.size;
    return n;
  }

  private ensureStarted(rt: RealSymbolRuntime): Promise<void> {
    if (!rt.startPromise) {
      rt.startPromise = this.start(rt);
      rt.startPromise.catch(() => {
        rt.startPromise = null; // 失败允许下次重试
      });
    }
    return rt.startPromise;
  }

  // ------------------------------------------------------------ 启动回填

  private async start(rt: RealSymbolRuntime): Promise<void> {
    const now = Date.now();
    const { symbol } = rt;
    await this.storage.clearSymbol(symbol);

    // 1. 并行拉取：六周期 klines 铺底 + 近窗逐笔（各自内部匀速分页）
    const [klinesByTf, recent] = await Promise.all([
      (async () => {
        const map = new Map<Timeframe, KlineData[]>();
        for (const tf of TIMEFRAMES) {
          const from = now - KLINE_SPAN_BARS[tf] * TIMEFRAME_MS[tf];
          map.set(tf, await fetchKlines(symbol, tf, from, now));
        }
        return map;
      })(),
      fetchRecentAggTrades(symbol, AGG_TRADES_WINDOW_MS, AGG_TRADES_MAX_PAGES),
    ]);

    // 2. klines → ohlcOnly 柱（cumDelta 序列内累计），并记录序列供接续基线
    const klineBarsByTf = new Map<Timeframe, FootprintBar[]>();
    for (const tf of TIMEFRAMES) {
      const rows = klinesByTf.get(tf) ?? [];
      let cum = 0;
      const bars: FootprintBar[] = [];
      for (const k of rows) {
        cum += klineDelta(k);
        bars.push(klineToBar(symbol, tf, k, cum));
      }
      klineBarsByTf.set(tf, bars);
      await this.storage.putBars(bars);
    }

    // 3. 近窗逐笔覆盖：起点完整的柱升级为真足迹柱
    const ticks = recent.ticks;
    rt.dataStart = ticks.length > 0 ? ticks[0].time : Number.POSITIVE_INFINITY;
    rt.lastAggId = recent.lastAggId;

    const refPrice =
      ticks.length > 0
        ? ticks[0].price
        : (klineBarsByTf.get('1m')?.at(-1)?.close ?? 0);
    const tickSize = inferTickSize(refPrice);

    rt.aggs.clear();
    for (const tf of AGG_TFS) {
      rt.aggs.set(
        tf,
        new FootprintAggregator(symbol, tf, {
          tickSize,
          initialCumDelta: this.cumDeltaBefore(klineBarsByTf.get(tf) ?? [], rt.dataStart, tf),
        }),
      );
    }
    if (ticks.length > 0) {
      const upgraded: FootprintBar[] = [];
      for (const t of ticks) {
        for (const tf of AGG_TFS) {
          const done = rt.aggs.get(tf)!.ingestFast(t);
          if (done && done.time >= rt.dataStart) upgraded.push(done);
        }
      }
      for (const tf of AGG_TFS) {
        const cur = rt.aggs.get(tf)!.snapshotCurrent();
        if (cur && cur.time >= rt.dataStart) upgraded.push(cur);
      }
      await this.storage.putBars(upgraded);
    }

    // 4. kline 流 cumDelta 状态：以铺底序列末柱为基线
    rt.klineState.clear();
    for (const tf of KLINE_LIVE_TFS) {
      const bars = klineBarsByTf.get(tf) ?? [];
      const last = bars.at(-1);
      rt.klineState.set(
        tf,
        last
          ? { curTime: last.time, cumBase: last.cumDelta - last.delta, lastDelta: last.delta }
          : { curTime: -1, cumBase: 0, lastDelta: 0 },
      );
    }

    if (recent.truncated) {
      console.warn(
        `[footprint] ${symbol} aggTrades 预算耗尽：足迹窗实际覆盖 ` +
          `${ticks.length > 0 ? Math.round((now - rt.dataStart) / 60_000) : 0}min（目标 ${AGG_TRADES_WINDOW_MS / 3_600_000}h），更早历史为 K 线蜡烛`,
      );
    }
  }

  /** klines 序列中 boundary 之前最后一根柱的 cumDelta（足迹段接续基线） */
  private cumDeltaBefore(bars: readonly FootprintBar[], boundary: number, tf: Timeframe): number {
    const barStart = Math.floor(boundary / TIMEFRAME_MS[tf]) * TIMEFRAME_MS[tf];
    for (let i = bars.length - 1; i >= 0; i--) {
      if (bars[i].time < barStart) return bars[i].cumDelta;
    }
    return 0;
  }

  // ------------------------------------------------------------ 实时流

  private ensureLive(rt: RealSymbolRuntime): void {
    if (rt.feed) return;
    rt.feed = new BinanceLiveFeed(
      rt.symbol,
      KLINE_LIVE_TFS,
      {
        onTrade: (tick) => this.onLiveTrade(rt, tick),
        onKline: (tf, k) => this.onLiveKline(rt, tf, k),
        onGap: (reason) => this.onFeedGap(rt, reason),
      },
      rt.lastAggId,
    );
    rt.feed.start();
  }

  private stopLive(rt: RealSymbolRuntime): void {
    if (rt.feed) {
      rt.feed.stop();
      rt.feed = null;
    }
    if (rt.snapshotTimer) {
      clearTimeout(rt.snapshotTimer);
      rt.snapshotTimer = null;
    }
    rt.snapshotPending.clear();
    void this.flushDirty();
  }

  private onLiveTrade(rt: RealSymbolRuntime, tick: AggTradeTick): void {
    if (rt.dataStart === Number.POSITIVE_INFINITY) rt.dataStart = tick.time;
    rt.lastAggId = tick.aggId;
    const trade: Trade = tick;
    for (const tf of AGG_TFS) {
      const agg = rt.aggs.get(tf);
      if (!agg) continue;
      const completed = agg.ingestFast(trade);
      if (completed && completed.time >= rt.dataStart) {
        this.markDirty(completed);
        this.notify(rt, tf, completed);
      }
      rt.snapshotPending.add(tf);
    }
    this.scheduleSnapshots(rt);
  }

  /** 当前柱快照按 100ms 节流推送（完结柱在 onLiveTrade 内即时推） */
  private scheduleSnapshots(rt: RealSymbolRuntime): void {
    if (rt.snapshotTimer) return;
    rt.snapshotTimer = setTimeout(() => {
      rt.snapshotTimer = null;
      const tfs = [...rt.snapshotPending];
      rt.snapshotPending.clear();
      for (const tf of tfs) {
        const cur = rt.aggs.get(tf)?.snapshotCurrent();
        if (cur && cur.time >= rt.dataStart) {
          this.markDirty(cur);
          this.notify(rt, tf, cur);
        }
      }
    }, SNAPSHOT_THROTTLE_MS);
  }

  private onLiveKline(rt: RealSymbolRuntime, tf: Timeframe, k: KlineData): void {
    // 聚合足迹覆盖得到的柱由 aggTrade 路径负责，kline 只管覆盖不到的
    if (AGG_TFS.includes(tf) && k.openTime >= rt.dataStart) return;
    const state = rt.klineState.get(tf);
    if (!state) return;

    let cumDelta: number;
    if (k.openTime < state.curTime) {
      // 重连重放的已收盘前柱：其累计即当前柱基线
      cumDelta = state.cumBase;
    } else {
      if (k.openTime > state.curTime) {
        if (state.curTime >= 0) state.cumBase += state.lastDelta;
        state.curTime = k.openTime;
      }
      state.lastDelta = klineDelta(k);
      cumDelta = state.cumBase + state.lastDelta;
    }
    const bar = klineToBar(rt.symbol, tf, k, cumDelta);
    this.markDirty(bar);
    this.notify(rt, tf, bar);
  }

  /** 断档不可精确补齐：整币种重建（保留订阅者，重建后推送自动续上） */
  private onFeedGap(rt: RealSymbolRuntime, reason: string): void {
    console.warn(`[footprint] ${rt.symbol} 实时流断档（${reason}），重建数据`);
    this.stopLive(rt);
    rt.startPromise = null;
    void this.ensureStarted(rt)
      .then(() => {
        if (this.totalListeners(rt) > 0) this.ensureLive(rt);
      })
      .catch((err) => {
        console.error(`[footprint] ${rt.symbol} 断档重建失败:`, err);
      });
  }

  private notify(rt: RealSymbolRuntime, tf: Timeframe, bar: FootprintBar): void {
    for (const cb of rt.listeners.get(tf)!) {
      try {
        cb(bar);
      } catch (err) {
        console.error('[footprint] subscriber error:', err);
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
