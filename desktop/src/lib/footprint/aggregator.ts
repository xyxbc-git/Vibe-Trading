// Footprint 聚合引擎 v2：输入逐笔成交（Trade 流），按 timeframe + tickSize
// 聚合为 FootprintBar（价位分层 bidVol/askVol、OHLC、totalVol、delta、
// cumDelta、poc）。纯内存计算，无 IO / 图表依赖。
//
// v2 变更：
//   - 周期扩为 1m/5m/15m/30m/4h/1d；
//   - FootprintBar 带 symbol（由构造参数传入）；
//   - tickSize 支持按价格量级自适应推导（inferTickSize）；
//   - 新增 ingestFast 懒快照路径：回填百万级成交时只在换柱时做一次快照，
//     避免 ingest 每笔都 O(levels·log) 生成快照拖垮首载。
//
// 口径（与 src/types/footprint.ts 契约一致）：
//   side === 'buy'  → 主动买，量记入该价位 askVol（足迹格右列）
//   side === 'sell' → 主动卖，量记入该价位 bidVol（足迹格左列）
//   delta = ΣaskVol − ΣbidVol；cumDelta 由聚合器跨柱累计（会话级）。

import type { FootprintBar, PriceLevel, Timeframe, Trade } from '../../types/footprint';

export const TIMEFRAME_MS: Record<Timeframe, number> = {
  '1m': 60_000,
  '5m': 300_000,
  '15m': 900_000,
  '30m': 1_800_000,
  '4h': 14_400_000,
  '1d': 86_400_000,
};

export const TIMEFRAMES: Timeframe[] = ['1m', '5m', '15m', '30m', '4h', '1d'];

/**
 * 按价格量级自适应 tickSize：10^(floor(log10(price)) - 4)，夹在 [1e-5, 10]。
 * 例：BTC 64000→1、ETH 3400→0.1、SOL 145→0.01、BNB 590→0.01、
 *     XRP 0.52→0.00001、DOGE 0.12→0.00001。
 * 目标是各币种「每 tick 相对价格」量级一致（~2e-5），价位层疏密接近。
 */
export function inferTickSize(price: number): number {
  if (!(price > 0)) return 1;
  const exp = Math.floor(Math.log10(price)) - 4;
  const clamped = Math.min(1, Math.max(-5, exp));
  // 10 ** -5 的双精度值 ≠ 字面量 1e-5；走十进制字符串解析拿精确值
  return Number(`1e${clamped}`);
}

export interface AggregatorOptions {
  /** 价位分层步长；缺省按 basePrice 推导（inferTickSize），再缺省 1 */
  tickSize?: number;
  /** 用于推导 tickSize 的参考价（通常是币种基准价/首笔价） */
  basePrice?: number;
  /** cumDelta 起始值（接续历史会话时使用），默认 0 */
  initialCumDelta?: number;
}

/** 单笔 ingest 的结果：当前柱快照 + 是否开启了新柱（旧柱在 completedBar） */
export interface IngestResult {
  bar: FootprintBar;
  isNewBar: boolean;
  completedBar: FootprintBar | null;
}

function decimalsOf(step: number): number {
  // 支持 1e-5 这类科学计数字面量：直接数量级换算，避免 String() 出 "1e-5"
  if (step >= 1) return 0;
  return Math.min(8, Math.max(0, Math.round(-Math.log10(step))));
}

function roundTo(value: number, decimals: number): number {
  const f = 10 ** decimals;
  return Math.round(value * f) / f;
}

/** 内部累加桶：Map<priceKey, {bid, ask}>，快照时转 PriceLevel[] */
interface MutableBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  buckets: Map<number, { bid: number; ask: number }>;
}

export class FootprintAggregator {
  readonly symbol: string;
  readonly timeframe: Timeframe;
  readonly tickSize: number;

  private readonly tfMs: number;
  private readonly priceDecimals: number;
  private cumDelta: number;
  private current: MutableBar | null = null;

  constructor(symbol: string, timeframe: Timeframe, options: AggregatorOptions = {}) {
    this.symbol = symbol;
    this.timeframe = timeframe;
    this.tickSize =
      options.tickSize ?? (options.basePrice !== undefined ? inferTickSize(options.basePrice) : 1);
    if (!(this.tickSize > 0)) throw new Error(`invalid tickSize: ${this.tickSize}`);
    this.tfMs = TIMEFRAME_MS[timeframe];
    this.priceDecimals = decimalsOf(this.tickSize);
    this.cumDelta = options.initialCumDelta ?? 0;
  }

  /** 价格对齐到 tick 网格 */
  snapPrice(price: number): number {
    return roundTo(Math.round(price / this.tickSize) * this.tickSize, this.priceDecimals);
  }

  /**
   * 快路径：喂入一笔成交（按时间递增），只返回「换柱时完结的旧柱快照」。
   * 当前柱不做快照（O(1)/笔），适合历史回填的大批量灌入；需要当前柱
   * 状态时调 snapshotCurrent()。
   */
  ingestFast(trade: Trade): FootprintBar | null {
    const barTime = Math.floor(trade.time / this.tfMs) * this.tfMs;
    const price = this.snapPrice(trade.price);

    let completedBar: FootprintBar | null = null;
    if (!this.current || this.current.time !== barTime) {
      if (this.current) completedBar = this.snapshotOf(this.current);
      this.current = {
        time: barTime,
        open: price,
        high: price,
        low: price,
        close: price,
        buckets: new Map(),
      };
    }

    const bar = this.current;
    bar.close = price;
    if (price > bar.high) bar.high = price;
    if (price < bar.low) bar.low = price;

    let bucket = bar.buckets.get(price);
    if (!bucket) {
      bucket = { bid: 0, ask: 0 };
      bar.buckets.set(price, bucket);
    }
    if (trade.side === 'buy') {
      bucket.ask += trade.size;
      this.cumDelta += trade.size;
    } else {
      bucket.bid += trade.size;
      this.cumDelta -= trade.size;
    }
    return completedBar;
  }

  /** 喂入一笔成交并返回当前柱快照（实时推送路径，成本 O(levels·log)）。 */
  ingest(trade: Trade): IngestResult {
    const before = this.current?.time;
    const completedBar = this.ingestFast(trade);
    const isNewBar = completedBar !== null || before === undefined;
    return { bar: this.snapshotOf(this.current!), isNewBar, completedBar };
  }

  /** 当前未完成柱的快照（无成交时为 null） */
  snapshotCurrent(): FootprintBar | null {
    return this.current ? this.snapshotOf(this.current) : null;
  }

  /** 当前累计 delta（含未完成柱） */
  getCumDelta(): number {
    return roundTo(this.cumDelta, 8);
  }

  private snapshotOf(bar: MutableBar): FootprintBar {
    const levels: PriceLevel[] = [];
    let totalVol = 0;
    let delta = 0;
    let poc = bar.close;
    let pocVol = -1;

    for (const [price, { bid, ask }] of bar.buckets) {
      const bidVol = roundTo(bid, 8);
      const askVol = roundTo(ask, 8);
      levels.push({ price, bidVol, askVol });
      const vol = bidVol + askVol;
      totalVol += vol;
      delta += askVol - bidVol;
      if (vol > pocVol) {
        pocVol = vol;
        poc = price;
      }
    }
    levels.sort((a, b) => b.price - a.price);

    return {
      symbol: this.symbol,
      time: bar.time,
      timeframe: this.timeframe,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
      levels,
      totalVol: roundTo(totalVol, 8),
      delta: roundTo(delta, 8),
      cumDelta: this.getCumDelta(),
      poc,
    };
  }
}

/**
 * 批量聚合（历史回填用）：trades 按时间升序，返回全部柱（含最后一根可能
 * 未走完的柱）。cumDelta 从 initialCumDelta（默认 0）起累计。
 */
export function aggregateTrades(
  trades: readonly Trade[],
  symbol: string,
  timeframe: Timeframe,
  options: AggregatorOptions = {},
): FootprintBar[] {
  const agg = new FootprintAggregator(symbol, timeframe, options);
  const bars: FootprintBar[] = [];
  for (const t of trades) {
    const completed = agg.ingestFast(t);
    if (completed) bars.push(completed);
  }
  const last = agg.snapshotCurrent();
  if (last) bars.push(last);
  return bars;
}
