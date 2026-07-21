// 拟真逐笔成交流 v2（mock 行情源，多币种）。
//
// 微观结构模拟：
//   价格 —— tick 网格上的随机游走 + 趋势/震荡 regime 切换（趋势期方向概率
//           偏移，震荡期均值回归），波动率偶发放大；
//   到达 —— 泊松过程（指数分布到达间隔），activity burst 时 λ 放大；
//   量能 —— 对数正态小单为主 + 帕累托尾部大单，单量按币价量级缩放
//           （保持各币单笔名义价值可比）；
//   失衡 —— 低概率触发「扫盘」事件：同方向连续 3~8 笔放量成交并推动价格
//           1~3 tick，制造足迹图上的对角失衡（截图中绿框高亮的效果来源）。
//
// v2 变更：
//   - 按 symbol 取基准价（BASE_PRICES）、自适应 tickSize、seed 掺入 symbol
//     哈希——各币种数据流独立且同 seed 可复现；
//   - 回填支持「按年龄分层的到达密度」（DENSITY_TIERS）：近 50h 全密度喂
//     1m/5m/15m 细节，越久越稀疏——1d 回填 120 天历史时成交量级从千万级
//     压到 ~百万级以下，首载不卡；
//   - backfillStream 生成器流式产出（不物化大数组），大回填内存 O(1)。
//
// 同一 seed 的回填输出恒定（便于 UI 联调对数）；实时推送接续回填末态价格，
// 保证历史与实时曲线连续。

import type { Trade } from '../../types/footprint';
import { inferTickSize } from './aggregator';

/** 各币种 mock 基准价（量级贴近 2026 常态行情，精确值不重要） */
export const BASE_PRICES: Record<string, number> = {
  BTCUSDT: 64000,
  ETHUSDT: 3400,
  SOLUSDT: 145,
  BNBUSDT: 590,
  XRPUSDT: 0.52,
  DOGEUSDT: 0.12,
  ADAUSDT: 0.38,
};
const FALLBACK_BASE_PRICE = 100;

export function basePriceOf(symbol: string): number {
  return BASE_PRICES[symbol.toUpperCase()] ?? FALLBACK_BASE_PRICE;
}

const HOUR = 3_600_000;
const DAY = 86_400_000;

/** 回填到达密度分层：age = 距回填终点的时间。近段全密度，远段稀疏。 */
export interface DensityTier {
  /** 本层适用的最大年龄（毫秒，含）；层按 maxAgeMs 升序 */
  maxAgeMs: number;
  tradesPerSec: number;
}

export const DEFAULT_DENSITY_TIERS: DensityTier[] = [
  { maxAgeMs: 50 * HOUR, tradesPerSec: 1.6 },   // 1m/5m/15m 细节窗
  { maxAgeMs: 100 * HOUR, tradesPerSec: 0.5 },  // 30m 窗
  { maxAgeMs: 30 * DAY, tradesPerSec: 0.15 },   // 4h 窗
  { maxAgeMs: 120 * DAY, tradesPerSec: 0.04 },  // 1d 窗
];

export interface MockFeedOptions {
  /** 交易对；决定默认基准价 / tickSize / seed 扰动，默认 BTCUSDT */
  symbol?: string;
  /** 初始价格；缺省按 symbol 查 BASE_PRICES */
  basePrice?: number;
  /** 价格网格步长；缺省按基准价 inferTickSize 推导 */
  tickSize?: number;
  /** 随机种子，默认 20260719；实际生效 seed 会掺入 symbol 哈希 */
  seed?: number;
  /** 实时/近段平均成交到达强度（笔/秒），默认 1.6 */
  avgTradesPerSec?: number;
}

/** mulberry32：小而稳定的种子 PRNG，保证同 seed 回填结果可复现 */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** djb2 字符串哈希：把 symbol 掺进 seed，各币种流独立可复现 */
function hashStr(s: string): number {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return h >>> 0;
}

type Regime = 'up' | 'down' | 'chop';

export class MockTradeFeed {
  readonly symbol: string;
  readonly basePrice: number;
  readonly tickSize: number;

  private readonly lambda: number;
  private readonly sizeScale: number;
  private readonly priceDecimals: number;
  private readonly rng: () => number;

  private price: number;
  private regime: Regime = 'chop';
  private regimeLeft = 0;
  private volBurstLeft = 0;

  private subscribers = new Set<(t: Trade) => void>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  /** 进行中的扫盘事件：剩余笔数与方向 */
  private sweepLeft = 0;
  private sweepSide: Trade['side'] = 'buy';

  constructor(options: MockFeedOptions = {}) {
    this.symbol = (options.symbol ?? 'BTCUSDT').toUpperCase();
    this.basePrice = options.basePrice ?? basePriceOf(this.symbol);
    this.tickSize = options.tickSize ?? inferTickSize(this.basePrice);
    this.lambda = options.avgTradesPerSec ?? 1.6;
    // 单量缩放：各币单笔名义价值与 BTC 基线可比（64000/币价）
    this.sizeScale = Math.max(1, Math.round(64000 / this.basePrice));
    this.priceDecimals = this.tickSize >= 1 ? 0 : Math.min(8, Math.round(-Math.log10(this.tickSize)));
    this.rng = mulberry32((options.seed ?? 20260719) ^ hashStr(this.symbol));
    this.price = this.snap(this.basePrice);
  }

  // ------------------------------------------------------------- 随机分布

  private snap(p: number): number {
    const raw = Math.round(p / this.tickSize) * this.tickSize;
    const f = 10 ** this.priceDecimals;
    return Math.round(raw * f) / f;
  }

  private gauss(): number {
    // Box-Muller
    const u = Math.max(this.rng(), 1e-12);
    const v = this.rng();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }

  /** 指数分布到达间隔（毫秒）；lambdaOverride 供回填密度分层 */
  private nextIntervalMs(lambdaOverride?: number): number {
    const lambda = lambdaOverride ?? this.lambda;
    const burst = this.volBurstLeft > 0 ? 3 : 1;
    const u = Math.max(this.rng(), 1e-12);
    return Math.min(60_000, (-Math.log(u) / (lambda * burst)) * 1000);
  }

  /** 单笔手数：对数正态主体 + 帕累托尾部（sweep 时放大），按币价缩放 */
  private nextSize(inSweep: boolean): number {
    let size: number;
    if (this.rng() < 0.04) {
      // 大单尾部：pareto(alpha=1.5)，基线量级 60~600
      size = 60 / Math.max(this.rng(), 1e-3) ** (1 / 1.5);
    } else {
      size = Math.exp(this.gauss() * 0.9 + 2.2); // 基线中位 ~9，主体 2~40
    }
    if (inSweep) size *= 2.5;
    const scaled = Math.min(size, 999) * this.sizeScale;
    return Math.max(1, Math.round(scaled));
  }

  // ------------------------------------------------------------- 状态演化

  private stepRegime(): void {
    if (this.regimeLeft <= 0) {
      const r = this.rng();
      this.regime = r < 0.3 ? 'up' : r < 0.6 ? 'down' : 'chop';
      this.regimeLeft = 120 + Math.floor(this.rng() * 500); // 持续 120~620 笔
    }
    this.regimeLeft -= 1;

    if (this.volBurstLeft > 0) this.volBurstLeft -= 1;
    else if (this.rng() < 0.002) this.volBurstLeft = 40 + Math.floor(this.rng() * 120);
  }

  /** 决定本笔方向 + 演化价格，返回成交方向 */
  private stepPrice(): Trade['side'] {
    this.stepRegime();

    // 扫盘事件：同向连续吃单推动价格
    if (this.sweepLeft === 0 && this.rng() < 0.006) {
      this.sweepLeft = 3 + Math.floor(this.rng() * 6);
      this.sweepSide = this.rng() < 0.5 ? 'buy' : 'sell';
    }

    let side: Trade['side'];
    if (this.sweepLeft > 0) {
      this.sweepLeft -= 1;
      side = this.sweepSide;
      // 扫盘大概率推动价格
      if (this.rng() < 0.65) {
        this.price = this.snap(this.price + (side === 'buy' ? 1 : -1) * this.tickSize);
      }
      return side;
    }

    // 常态：regime 给方向概率加偏移
    const buyProb = this.regime === 'up' ? 0.56 : this.regime === 'down' ? 0.44 : 0.5;
    side = this.rng() < buyProb ? 'buy' : 'sell';

    // 价格演化：约 45% 的成交触发 1 tick 移动，方向跟成交方向相关
    const moveProb = this.volBurstLeft > 0 ? 0.6 : 0.45;
    if (this.rng() < moveProb) {
      const followProb = 0.72; // 价格多数时候顺着主动方走
      const dir = this.rng() < followProb ? (side === 'buy' ? 1 : -1) : (side === 'buy' ? -1 : 1);
      this.price = this.snap(this.price + dir * this.tickSize);
    }
    return side;
  }

  private makeTrade(time: number): Trade {
    const inSweep = this.sweepLeft > 0;
    const side = this.stepPrice();
    return { time, price: this.price, size: this.nextSize(inSweep), side };
  }

  // ------------------------------------------------------------- 对外 API

  private densityAt(ageMs: number, tiers: readonly DensityTier[]): number {
    for (const tier of tiers) {
      if (ageMs <= tier.maxAgeMs) return tier.tradesPerSec;
    }
    return tiers[tiers.length - 1]?.tradesPerSec ?? this.lambda;
  }

  /**
   * 历史回填（流式）：生成 [endTime - durationMs, endTime) 区间的逐笔成交，
   * 按时间升序 yield。到达密度按「距 endTime 的年龄」分层（tiers），近段
   * 密、远段疏。回填结束后内部价格状态停留在末笔，实时推送无缝接续。
   */
  *backfillStream(
    durationMs: number,
    endTime: number = Date.now(),
    tiers: readonly DensityTier[] = DEFAULT_DENSITY_TIERS,
  ): Generator<Trade, void, undefined> {
    let t = endTime - durationMs;
    while (t < endTime) {
      yield this.makeTrade(Math.floor(t));
      t += this.nextIntervalMs(this.densityAt(endTime - t, tiers));
    }
  }

  /** 历史回填（数组版，恒定密度 = avgTradesPerSec；小区间/测试用） */
  backfill(durationMs: number, endTime: number = Date.now()): Trade[] {
    const trades: Trade[] = [];
    let t = endTime - durationMs;
    while (t < endTime) {
      trades.push(this.makeTrade(Math.floor(t)));
      t += this.nextIntervalMs();
    }
    return trades;
  }

  /** 实时推送逐笔成交；返回退订函数。多订阅者共享同一条成交流。 */
  subscribe(cb: (trade: Trade) => void): () => void {
    this.subscribers.add(cb);
    if (!this.timer) this.scheduleNext();
    return () => {
      this.subscribers.delete(cb);
      if (this.subscribers.size === 0 && this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
      }
    };
  }

  /** 是否有实时订阅在跑（dataService 判断停流后补档用） */
  isLive(): boolean {
    return this.timer !== null;
  }

  private scheduleNext(): void {
    this.timer = setTimeout(() => {
      const trade = this.makeTrade(Date.now());
      for (const cb of this.subscribers) {
        try {
          cb(trade);
        } catch (err) {
          console.error('[mockFeed] subscriber error:', err);
        }
      }
      if (this.subscribers.size > 0) this.scheduleNext();
      else this.timer = null;
    }, this.nextIntervalMs());
  }
}
