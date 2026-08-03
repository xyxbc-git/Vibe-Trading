// 历史足迹回看数据层：后端 GET /api/tape/footprint?start&end（tape_footprint_minutes
// 持久化 + tape_minute_bars 分钟聚合）→ FootprintBar 转换。
//
// 与实时源（binanceFeed 直连币安）互补：实时源的真足迹只覆盖 aggTrades 预算内的
// 近几小时且每次会话清库重建；本服务读的是后端长期落库（保留天数走
// tape_retention_days，默认 30 天），只要 jarvis 后端在运行期间收过流，任意
// 历史日期都能回看。库里只有分钟聚合、没有价格档位的时段由后端降级为
// rows=[] 的纯 OHLC 柱，这里映射成 ohlcOnly（UI 自动退化画蜡烛）。
//
// 单位说明：后端足迹以 USD 名义额聚合（buy/sell 为美元额），实时源为币量；
// 同一视图内自洽（delta/失衡/POC 均为比例语义），跨模式不做换算。

import type { FootprintBar, PriceLevel, Timeframe } from '../../types/footprint';

/** 后端足迹区间查询支持的周期（jarvis FOOT_INTERVALS_S） */
export const HISTORY_TIMEFRAMES = ['1m', '5m', '15m', '30m'] as const;
export type HistoryTimeframe = (typeof HISTORY_TIMEFRAMES)[number];

export function isHistoryTimeframe(tf: Timeframe): tf is HistoryTimeframe {
  return (HISTORY_TIMEFRAMES as readonly string[]).includes(tf);
}

const FETCH_TIMEOUT_MS = 20_000;

// ── 后端响应形状（与 jarvis_tape_classify.footprint 区间模式对齐）──

interface ApiFootprintRow {
  price: number;
  buy: number;
  sell: number;
  flag: 'buy_imb' | 'sell_imb' | null;
}

interface ApiFootprintBar {
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
  rows: ApiFootprintRow[];
}

interface ApiFootprintResponse {
  ok: boolean;
  bars?: ApiFootprintBar[];
  bucket?: number | null;
  active?: boolean;
  source?: string;
  range?: { start: number; end: number; truncated: boolean };
  error?: string;
}

/** 单根后端柱 → FootprintBar；OHLC 全缺且无档位时返回 null（不可渲染） */
export function toFootprintBar(
  symbol: string,
  timeframe: HistoryTimeframe,
  b: ApiFootprintBar,
): FootprintBar | null {
  const rows = Array.isArray(b.rows) ? b.rows : [];
  // 后端 price 降序；防御性再排一次，渲染层依赖降序遍历
  const levels: PriceLevel[] = rows
    .map((r) => ({ price: r.price, bidVol: r.sell, askVol: r.buy }))
    .sort((a, c) => c.price - a.price);

  let poc = Number.NaN;
  let best = -1;
  for (const lv of levels) {
    const v = lv.bidVol + lv.askVol;
    if (v > best) {
      best = v;
      poc = lv.price;
    }
  }

  // OHLC 兜底链：分钟行缺价（极早期数据）时用档位价格域撑起蜡烛
  const close = b.close ?? b.open ?? (Number.isFinite(poc) ? poc : null);
  if (close === null) return null;
  const open = b.open ?? close;
  const high = b.high ?? Math.max(open, close, ...levels.map((l) => l.price));
  const low = b.low ?? Math.min(open, close, ...levels.map((l) => l.price));

  return {
    symbol,
    time: b.ts * 1000,
    timeframe,
    open,
    high,
    low,
    close,
    levels,
    totalVol: b.total ?? 0,
    delta: b.delta ?? 0,
    cumDelta: b.cvd ?? 0,
    poc: Number.isFinite(poc) ? poc : close,
    ...(levels.length === 0 ? { ohlcOnly: true as const } : {}),
  };
}

/**
 * 拉取某历史区间的足迹柱（时间升序）。
 * @param fromMs/toMs 毫秒时间戳（转 epoch 秒传后端）
 * @throws 网络失败 / 超时 / 后端 ok=false 时抛 Error（调用方展示 loadError 卡）
 */
export async function fetchHistoryBars(
  symbol: string,
  timeframe: HistoryTimeframe,
  fromMs: number,
  toMs: number,
  buckets = 60,
): Promise<FootprintBar[]> {
  const qs = new URLSearchParams({
    symbol,
    interval: timeframe,
    buckets: String(buckets),
    start: String(Math.floor(fromMs / 1000)),
    end: String(Math.floor(toMs / 1000)),
  });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  let data: ApiFootprintResponse;
  try {
    const res = await fetch(`/api/tape/footprint?${qs}`, { signal: controller.signal });
    if (!res.ok) throw new Error(`后端 HTTP ${res.status}`);
    data = (await res.json()) as ApiFootprintResponse;
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      throw new Error('后端请求超时（20s）');
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
  if (!data.ok) throw new Error(data.error ?? '后端返回 ok=false');
  const out: FootprintBar[] = [];
  for (const b of data.bars ?? []) {
    const bar = toFootprintBar(symbol, timeframe, b);
    if (bar) out.push(bar);
  }
  return out.sort((a, c) => a.time - c.time);
}

// ── 日期工具（本地时区；histDate 用 YYYY-MM-DD 字符串在 UI 与请求间传递）──

export function fmtLocalDate(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

/** 'YYYY-MM-DD'（本地时区）→ [当日 0 点, min(次日 0 点, 现在)] 毫秒区间 */
export function dayRangeMs(dateStr: string): { fromMs: number; toMs: number } {
  const fromMs = new Date(`${dateStr}T00:00:00`).getTime();
  return { fromMs, toMs: Math.min(fromMs + 86_400_000, Date.now()) };
}
