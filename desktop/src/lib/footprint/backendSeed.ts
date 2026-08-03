// 实时足迹的「后端冷启动种子」：让桌面端打开/切换币种时不再直连币安 REST
// （klines + aggTrades）铺底——那正是触发 418 IP 封禁的「冒启风暴」。
//
// jarvis 后端（jarvis_ws_stream）本就用 WS 收 aggTrade 并落库足迹档位，历史都在
// 本地。本模块读 /api/tape/footprint（近期模式）与 /api/tape/bars，把后端的
// **USD 名义额**聚合换算成**币量**（coin qty）FootprintBar，使其与实时 WS 聚合器
// （binanceFeed/aggregator，口径为币量）拼接时单位自洽、无量纲断层。
//
// 换算：后端每档 buy/sell 为 USD 名义额 → 币量 = USD / 该档价格。
//   askVol(主动买币量) = buy_usd / price；bidVol(主动卖币量) = sell_usd / price。
// 无足迹档位的历史桶（rows=[]）与纯 K 线桶（/api/tape/bars）按整桶均价近似：
//   币量 ≈ USD / close，标 ohlcOnly（UI 退化画蜡烛）。
// cvd(USD) 不可直接用，币量 CVD 在本模块内按币量 delta 逐桶累计（会话级 base 0，
// 与聚合器 initialCumDelta 接续口径一致）。

import type { FootprintBar, PriceLevel, Timeframe } from '../../types/footprint';

/** 有真足迹档位的周期（后端 FOOT_INTERVALS_S）；其余走 OHLC 种子 */
export const SEED_FOOT_TFS: readonly Timeframe[] = ['1m', '5m', '15m', '30m'];

const SEED_TIMEOUT_MS = 12_000;

// ── 后端响应形状（与 jarvis_tape_classify.footprint / bars 对齐）──

interface ApiFpRow {
  price: number;
  buy: number; // USD 名义额
  sell: number; // USD 名义额
  flag: 'buy_imb' | 'sell_imb' | null;
}
interface ApiFpBar {
  ts: number; // 桶起点 epoch 秒
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  total: number;
  buy: number;
  sell: number;
  delta: number;
  cvd: number;
  trades?: number;
  rows: ApiFpRow[];
}
interface ApiFpResp {
  ok: boolean;
  bars?: ApiFpBar[];
  error?: string;
}

interface ApiTapeBar {
  ts: number; // 桶起点 epoch 秒
  buy: number; // USD
  sell: number; // USD
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  trades?: number;
}
interface ApiTapeResp {
  ok: boolean;
  bars?: ApiTapeBar[];
  error?: string;
}

/** 可注入的 fetch（默认全局 fetch；测试可替身） */
export type FetchLike = (url: string, init?: { signal?: AbortSignal }) => Promise<{
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}>;

function resolveFetch(f?: FetchLike): FetchLike {
  if (f) return f;
  return globalThis.fetch as unknown as FetchLike;
}

async function getJson<T>(url: string, fetchImpl: FetchLike): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEED_TIMEOUT_MS);
  try {
    const res = await fetchImpl(url, { signal: controller.signal });
    if (!res.ok) throw new Error(`后端 HTTP ${res.status}`);
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

/** 后端足迹柱(USD) → 币量 FootprintBar；OHLC 全缺且无档位时返回 null */
export function fpBarToCoin(
  symbol: string,
  timeframe: Timeframe,
  b: ApiFpBar,
  cumBefore: number,
): { bar: FootprintBar; cumDelta: number } | null {
  const rows = Array.isArray(b.rows) ? b.rows : [];
  const levels: PriceLevel[] = [];
  let totalVol = 0;
  let delta = 0;
  let poc = Number.NaN;
  let pocVol = -1;
  for (const r of rows) {
    const price = r.price;
    if (!(price > 0)) continue;
    const askVol = r.buy / price; // 主动买币量（右列）
    const bidVol = r.sell / price; // 主动卖币量（左列）
    levels.push({ price, bidVol, askVol });
    const vol = askVol + bidVol;
    totalVol += vol;
    delta += askVol - bidVol;
    if (vol > pocVol) {
      pocVol = vol;
      poc = price;
    }
  }
  levels.sort((a, c) => c.price - a.price); // 价降序

  // 收盘价兜底链：分钟聚合缺 OHLC 时用档位价撑起
  const close = b.close ?? b.open ?? (Number.isFinite(poc) ? poc : null);
  if (close === null) return null;
  const open = b.open ?? close;

  if (levels.length === 0) {
    // 无足迹档位的历史桶：整桶 USD 按 close 近似成币量，退化 ohlcOnly
    const totalUsd = b.total ?? 0;
    const deltaUsd = b.delta ?? 0;
    const coinTotal = close > 0 ? totalUsd / close : 0;
    const coinDelta = close > 0 ? deltaUsd / close : 0;
    const cumDelta = cumBefore + coinDelta;
    const high = b.high ?? Math.max(open, close);
    const low = b.low ?? Math.min(open, close);
    return {
      bar: {
        symbol,
        time: b.ts * 1000,
        timeframe,
        open,
        high,
        low,
        close,
        levels: [],
        totalVol: coinTotal,
        delta: coinDelta,
        cumDelta,
        poc: close,
        ohlcOnly: true,
      },
      cumDelta,
    };
  }

  const high = b.high ?? Math.max(open, close, ...levels.map((l) => l.price));
  const low = b.low ?? Math.min(open, close, ...levels.map((l) => l.price));
  const cumDelta = cumBefore + delta;
  return {
    bar: {
      symbol,
      time: b.ts * 1000,
      timeframe,
      open,
      high,
      low,
      close,
      levels,
      totalVol,
      delta,
      cumDelta,
      poc: Number.isFinite(poc) ? poc : close,
    },
    cumDelta,
  };
}

/** 后端足迹柱数组 → 币量柱：时间升序 + cumDelta 从 0 逐桶累计（chunk 内自洽） */
function convertFpBars(symbol: string, timeframe: Timeframe, bars: ApiFpBar[]): FootprintBar[] {
  const src = [...bars].sort((a, c) => a.ts - c.ts);
  const out: FootprintBar[] = [];
  let cum = 0;
  for (const b of src) {
    const conv = fpBarToCoin(symbol, timeframe, b, cum);
    if (!conv) continue;
    cum = conv.cumDelta;
    out.push(conv.bar);
  }
  return out;
}

/** 后端 OHLC 柱数组 → 币量 ohlcOnly 柱：时间升序 + cumDelta 从 0 逐桶累计 */
function convertOhlcBars(symbol: string, timeframe: Timeframe, bars: ApiTapeBar[]): FootprintBar[] {
  const src = [...bars].sort((a, c) => a.ts - c.ts);
  const out: FootprintBar[] = [];
  let cum = 0;
  for (const b of src) {
    const close = b.close ?? b.open;
    if (close === null || !(close > 0)) continue;
    const open = b.open ?? close;
    const coinTotal = (b.buy + b.sell) / close;
    const coinDelta = (b.buy - b.sell) / close;
    cum += coinDelta;
    out.push({
      symbol,
      time: b.ts * 1000,
      timeframe,
      open,
      high: b.high ?? Math.max(open, close),
      low: b.low ?? Math.min(open, close),
      close,
      levels: [],
      totalVol: coinTotal,
      delta: coinDelta,
      cumDelta: cum,
      poc: close,
      ohlcOnly: true,
    });
  }
  return out;
}

/**
 * 拉取某周期近 limit 根足迹柱（后端近期模式，含真档位），换算成币量、时间升序。
 * 失败/空返回 []（调用方据此决定是否回退币安）。
 */
export async function fetchFootprintSeed(
  symbol: string,
  timeframe: Timeframe,
  limit = 60,
  fetchImpl?: FetchLike,
): Promise<FootprintBar[]> {
  const f = resolveFetch(fetchImpl);
  const qs = new URLSearchParams({
    symbol,
    interval: timeframe,
    limit: String(Math.max(1, Math.min(limit, 60))),
    buckets: '60',
  });
  const data = await getJson<ApiFpResp>(`/api/tape/footprint?${qs}`, f);
  if (!data.ok || !Array.isArray(data.bars)) return [];
  return convertFpBars(symbol, timeframe, data.bars);
}

/**
 * 拉取某周期近 limit 根 OHLC 柱（后端 /api/tape/bars，无档位），换算成币量
 * ohlcOnly 柱、时间升序。用于 4h/1d 等无足迹档位的大周期铺底。
 */
export async function fetchOhlcSeed(
  symbol: string,
  timeframe: Timeframe,
  limit = 240,
  fetchImpl?: FetchLike,
): Promise<FootprintBar[]> {
  const f = resolveFetch(fetchImpl);
  const qs = new URLSearchParams({
    symbol,
    interval: timeframe,
    limit: String(Math.max(1, Math.min(limit, 500))),
  });
  const data = await getJson<ApiTapeResp>(`/api/tape/bars?${qs}`, f);
  if (!data.ok || !Array.isArray(data.bars)) return [];
  return convertOhlcBars(symbol, timeframe, data.bars);
}

// ── 左滚按需分页回补（实时态向历史翻页，只走本地后端）──
//
// 后端两个接口都支持 start/end（epoch 秒，闭区间）区间模式：
//   /api/tape/footprint：1m/5m/15m/30m，tape_footprint_minutes 落库 + 分钟聚合，
//     无档位时段降级 rows=[]（→ ohlcOnly），单次上限 1500 根；
//   /api/tape/bars：全周期分钟聚合（4h/1d 走这里），同样上限截尾。
// 返回 chunk 的 cumDelta 从 0 起（chunk 内自洽），由 mergeOlderBars 在拼接时
// 统一重定基接续既有序列，故这里不需要知道全局基线。

/**
 * 区间模式拉取足迹柱（1m/5m/15m/30m）：[fromMs, toMs] 内全部桶，币量、时间升序。
 * 失败抛错（调用方做冷却重试）；区间内无数据返回 []（调用方视为到达本地起点）。
 */
export async function fetchFootprintRange(
  symbol: string,
  timeframe: Timeframe,
  fromMs: number,
  toMs: number,
  fetchImpl?: FetchLike,
): Promise<FootprintBar[]> {
  const f = resolveFetch(fetchImpl);
  const qs = new URLSearchParams({
    symbol,
    interval: timeframe,
    buckets: '60',
    start: String(Math.max(1, Math.floor(fromMs / 1000))),
    end: String(Math.max(1, Math.floor(toMs / 1000))),
  });
  const data = await getJson<ApiFpResp>(`/api/tape/footprint?${qs}`, f);
  if (!data.ok) throw new Error(data.error ?? '后端返回 ok=false');
  if (!Array.isArray(data.bars)) return [];
  return convertFpBars(symbol, timeframe, data.bars);
}

/**
 * 区间模式拉取 OHLC 柱（4h/1d 等大周期）：[fromMs, toMs] 内全部桶，
 * 币量 ohlcOnly、时间升序。失败抛错；无数据返回 []。
 */
export async function fetchOhlcRange(
  symbol: string,
  timeframe: Timeframe,
  fromMs: number,
  toMs: number,
  fetchImpl?: FetchLike,
): Promise<FootprintBar[]> {
  const f = resolveFetch(fetchImpl);
  const qs = new URLSearchParams({
    symbol,
    interval: timeframe,
    limit: '500',
    start: String(Math.max(1, Math.floor(fromMs / 1000))),
    end: String(Math.max(1, Math.floor(toMs / 1000))),
  });
  const data = await getJson<ApiTapeResp>(`/api/tape/bars?${qs}`, f);
  if (!data.ok) throw new Error(data.error ?? '后端返回 ok=false');
  if (!Array.isArray(data.bars)) return [];
  return convertOhlcBars(symbol, timeframe, data.bars);
}

/**
 * 把更早的历史 chunk 拼接到既有序列前面（纯函数，供左滚分页回补用）：
 * 1. 去重：只保留 time 严格早于既有首柱的部分（区间闭端点可能与首柱同桶）；
 * 2. cumDelta 重定基：chunk 的累计是 chunk 内自洽（从 0 起）的，整体平移使
 *    「chunk 末柱累计 == 既有首柱的前置基线（首柱 cumDelta − 首柱 delta）」，
 *    拼接后跨界处 cumDelta 增量仍等于各柱 delta，累计Δ曲线无跳变；
 * 3. 两侧各自时间升序 → 结果整体升序，无重叠、无乱序。
 * @returns bars 拼接结果；prepended 实际接上的根数（0 = 区间内无更早数据）
 */
export function mergeOlderBars(
  existing: readonly FootprintBar[],
  older: readonly FootprintBar[],
): { bars: FootprintBar[]; prepended: number } {
  if (existing.length === 0) {
    return { bars: [...older], prepended: older.length };
  }
  const first = existing[0];
  const chunk = older.filter((b) => b.time < first.time);
  if (chunk.length === 0) {
    return { bars: [...existing], prepended: 0 };
  }
  const base = first.cumDelta - first.delta;
  const offset = base - chunk[chunk.length - 1].cumDelta;
  const rebased =
    offset === 0 ? [...chunk] : chunk.map((b) => ({ ...b, cumDelta: b.cumDelta + offset }));
  return { bars: [...rebased, ...existing], prepended: rebased.length };
}
