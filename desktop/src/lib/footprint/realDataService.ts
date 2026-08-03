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
  binanceBanLiftedAgoMs,
  binanceBanRemainingMs,
  BinanceLiveFeed,
  fetchKlines,
  fetchRecentAggTrades,
  klineToBar,
  type AggTradeTick,
  type KlineData,
  klineDelta,
} from './binanceFeed';
import { fetchFootprintSeed, fetchOhlcSeed } from './backendSeed';
import { createFootprintStorage, type FootprintStorage } from './storage';

/** 真实源 IndexedDB 库名（与 mock 的 jarvis-footprint 隔离） */
export const REAL_DB_NAME = 'jarvis-footprint-real';

/** 逐笔聚合出足迹格的周期（近窗内）；4h/1d 始终 ohlcOnly */
const AGG_TFS: readonly Timeframe[] = ['1m', '5m', '15m', '30m'];
/** WS kline 流维护 ohlcOnly 当前柱的周期（30m 为近窗不足时的兜底） */
const KLINE_LIVE_TFS: readonly Timeframe[] = ['30m', '4h', '1d'];

/** 后端足迹 seed 每周期根数（后端上限 60，覆盖当前屏） */
const SEED_FOOT_LIMIT = 60;
/** 走后端 OHLC seed 的大周期（无足迹档位，画蜡烛） */
const OHLC_SEED_TFS: readonly Timeframe[] = ['4h', '1d'];
/** 后端 OHLC seed 每周期根数 */
const OHLC_SEED_LIMIT = 240;

/**
 * 各周期 klines 铺底根数（币安兜底路径专用，已大幅缩减到「当前屏」量级；
 * 常规链路走后端 seed，不会触发这里）。
 */
const KLINE_SPAN_BARS: Record<Timeframe, number> = {
  '1m': 240, // 4h
  '5m': 240, // 20h
  '15m': 240, // 2.5d
  '30m': 240, // 5d
  '4h': 180, // 30d
  '1d': 120, // 4mo
};

/**
 * aggTrades 近窗回填预算（币安兜底路径专用）：10 页 ×1000 笔（weight 20/页，
 * 预算 200）。常规链路走后端 seed，不会触发这里。
 */
const AGG_TRADES_MAX_PAGES = 10;
/** 兜底近窗目标跨度（预算内尽力） */
const AGG_TRADES_WINDOW_MS = 40 * 60_000;

/** 解封后视为「敏感期」的窗口：期内首次币安兜底加随机延迟错峰 */
const UNBAN_SENSITIVE_WINDOW_MS = 10 * 60_000;
/** 敏感期兜底的随机延迟区间（3~15s） */
const UNBAN_JITTER_MIN_MS = 3_000;
const UNBAN_JITTER_SPAN_MS = 12_000;

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
    await this.storage.clearSymbol(rt.symbol);

    // 后端优先：jarvis 本地已用 WS 落库足迹档位，零币安权重铺底当前屏。
    let seeded = false;
    try {
      seeded = await this.seedFromBackend(rt, now);
    } catch (err) {
      console.warn(`[footprint] ${rt.symbol} 后端 seed 失败，尝试币安兜底:`, err);
    }
    if (seeded) return;

    // 后端无数据且币安封禁中：放弃铺底，仅靠 WS 实时（订阅不断，聚合器随首笔懒建）。
    if (binanceBanRemainingMs() > 0) {
      console.warn(
        `[footprint] ${rt.symbol} 后端无数据且币安 REST 封禁中——跳过铺底，` +
          `仅 WS 实时（聚合器随首笔懒建）`,
      );
      rt.dataStart = Number.POSITIVE_INFINITY;
      rt.lastAggId = -1;
      rt.aggs.clear();
      rt.klineState.clear();
      return;
    }

    // 兜底：仅后端不可用时才走币安 REST（预算已大幅缩减）。
    // 刚解封的敏感期内加随机延迟错峰——封禁门到期后的自动恢复路径（onGap
    // 重建 / 用户重试）不得在解封瞬间齐发 REST，否则秒被再封形成死循环。
    const liftedAgo = binanceBanLiftedAgoMs();
    if (liftedAgo < UNBAN_SENSITIVE_WINDOW_MS) {
      const delay = UNBAN_JITTER_MIN_MS + Math.random() * UNBAN_JITTER_SPAN_MS;
      console.warn(
        `[footprint] ${rt.symbol} 币安封禁刚解除 ${Math.round(liftedAgo / 1000)}s，` +
          `兜底请求延迟 ${Math.round(delay / 1000)}s 错峰发出`,
      );
      await new Promise((r) => setTimeout(r, delay));
      // 延迟期间可能被其它调用再次触发封禁：再查一次门，封禁中直接降级仅 WS
      if (binanceBanRemainingMs() > 0) {
        console.warn(`[footprint] ${rt.symbol} 延迟期间再次进入封禁——降级仅 WS 实时`);
        rt.dataStart = Number.POSITIVE_INFINITY;
        rt.lastAggId = -1;
        rt.aggs.clear();
        rt.klineState.clear();
        return;
      }
    }
    await this.backfillFromBinance(rt, now);
  }

  /**
   * 从 jarvis 后端拉近期足迹（1m/5m/15m/30m 含真档位）+ 大周期 OHLC（4h/1d）
   * 铺底当前屏，USD→币量换算后落库；聚合器 cumDelta 接续 seed 基线，dataStart
   * 取最早 seed 柱以便实时柱正常发出，lastAggId=-1 让 WS 全新起（无 REST 锚点）。
   * 后端无任何数据返回 false（调用方决定是否走币安兜底）。
   */
  private async seedFromBackend(rt: RealSymbolRuntime, now: number): Promise<boolean> {
    const sym = rt.symbol;
    const fpByTf = new Map<Timeframe, FootprintBar[]>();
    const ohlcByTf = new Map<Timeframe, FootprintBar[]>();
    await Promise.all([
      ...AGG_TFS.map(async (tf) => {
        fpByTf.set(tf, await fetchFootprintSeed(sym, tf, SEED_FOOT_LIMIT));
      }),
      ...OHLC_SEED_TFS.map(async (tf) => {
        ohlcByTf.set(tf, await fetchOhlcSeed(sym, tf, OHLC_SEED_LIMIT));
      }),
    ]);

    const anyData =
      [...fpByTf.values()].some((b) => b.length > 0) ||
      [...ohlcByTf.values()].some((b) => b.length > 0);
    if (!anyData) return false;

    const cumBaseByTf = new Map<Timeframe, number>();
    let earliest = Number.POSITIVE_INFINITY;
    let refPrice = 0;
    for (const tf of AGG_TFS) {
      const bars = fpByTf.get(tf) ?? [];
      if (bars.length === 0) continue;
      await this.storage.putBars(bars);
      const curBucket = Math.floor(now / TIMEFRAME_MS[tf]) * TIMEFRAME_MS[tf];
      const lastCompleted = bars.filter((b) => b.time < curBucket).at(-1);
      cumBaseByTf.set(tf, lastCompleted?.cumDelta ?? 0);
      earliest = Math.min(earliest, bars[0].time);
      refPrice = bars[bars.length - 1].close;
    }
    for (const tf of OHLC_SEED_TFS) {
      const bars = ohlcByTf.get(tf) ?? [];
      if (bars.length === 0) continue;
      await this.storage.putBars(bars);
      if (!(refPrice > 0)) refPrice = bars[bars.length - 1].close;
    }
    if (!(refPrice > 0)) return false;

    rt.dataStart = Number.isFinite(earliest) ? earliest : now;
    rt.lastAggId = -1;
    this.initAggs(rt, refPrice, cumBaseByTf);

    // kline 流状态：大周期以各自末柱为累计基线（30m 复用其足迹末柱）
    rt.klineState.clear();
    for (const tf of KLINE_LIVE_TFS) {
      const bars = tf === '30m' ? (fpByTf.get('30m') ?? []) : (ohlcByTf.get(tf) ?? []);
      const last = bars[bars.length - 1];
      rt.klineState.set(
        tf,
        last
          ? { curTime: last.time, cumBase: last.cumDelta - last.delta, lastDelta: last.delta }
          : { curTime: -1, cumBase: 0, lastDelta: 0 },
      );
    }
    console.info(`[footprint] ${sym} 后端 seed 成功（零币安 REST 铺底）`);
    return true;
  }

  /** 按参考价建 AGG_TFS 聚合器（tickSize 自适应，cumDelta 接续 seed 基线） */
  private initAggs(
    rt: RealSymbolRuntime,
    refPrice: number,
    cumBaseByTf: Map<Timeframe, number>,
  ): void {
    const tickSize = inferTickSize(refPrice);
    rt.aggs.clear();
    for (const tf of AGG_TFS) {
      rt.aggs.set(
        tf,
        new FootprintAggregator(rt.symbol, tf, {
          tickSize,
          initialCumDelta: cumBaseByTf.get(tf) ?? 0,
        }),
      );
    }
  }

  /**
   * 币安 REST 铺底（仅后端不可用时兜底；klines 与 aggTrades 预算已大幅缩减）。
   */
  private async backfillFromBinance(rt: RealSymbolRuntime, now: number): Promise<void> {
    const { symbol } = rt;
    // 兜底路径可能在「解封瞬间」被多币种同时触发：加随机抖动错峰，配合 binanceFeed
    // 解封冷却窗的低预算，杜绝「解封→回补风暴→秒被再封」死循环。
    await new Promise((r) => setTimeout(r, 200 + Math.floor(Math.random() * 1000)));
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
      // 大周期当前柱已由后端 seed 覆盖，连接时不再用币安 REST 重放（WS 自愈）
      { replayKlinesOnConnect: false },
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
    // 聚合器缺失（后端 seed 与币安兜底都没建成，如封禁+后端离线）：以首笔价懒建
    if (rt.aggs.size === 0) this.initAggs(rt, tick.price, new Map());
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
    // start() 后端优先：即便币安 REST 封禁中，也能靠本地后端零权重重建；
    // 后端也不可用时 start() 内部自动降级为「仅 WS 实时」保住页面可用。
    console.warn(`[footprint] ${rt.symbol} 实时流断档（${reason}），后端优先重建`);
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
