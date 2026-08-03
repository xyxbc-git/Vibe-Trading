// 币安 USDT 永续合约真实行情源（零依赖：原生 fetch + WebSocket）。
//
// 数据源三层（供 dataService 编排）：
//   实时流   —— WS `${WS_BASE}/stream?streams=<sym>@aggTrade/<sym>@kline_30m/...`
//               aggTrade 逐笔喂足迹聚合；kline 流维护大周期 ohlcOnly 末柱。
//               断线指数重连（1s→30s 封顶）；重连后按 aggTradeId 用 REST
//               正向补档，先回放补档再回放 WS 缓冲，对外保证连续有序。
//   近期历史 —— REST /fapi/v1/aggTrades 逆序分页（fromId 后退），覆盖近窗
//               逐笔用于 1m/5m/15m 足迹回填；预算受限（weight 20/请求）。
//   远期历史 —— REST /fapi/v1/klines 仅 OHLCV + takerBuy 量：delta 用
//               2*takerBuy − vol 反推（taker买 − taker卖），levels 置空并标
//               ohlcOnly（契约 v2.1 可选字段，UI 退化画蜡烛）。
//
// 口径：aggTrade.m === true → 买方是 maker → 主动卖（side='sell'）；
//       m === false → 主动买（side='buy'）。与 mock 源/聚合器口径一致。

import type { FootprintBar, Timeframe, Trade } from '../../types/footprint';

const REST_BASE = 'https://fapi.binance.com';
const WS_BASE = 'wss://fstream.binance.com';

// ------------------------------------------------------------------ 源开关

export type FootprintSource = 'real' | 'mock';

const SOURCE_KEY = 'jarvis-footprint-source';

/** 数据源开关：localStorage `jarvis-footprint-source`，缺省 real（改后刷新生效） */
export function getFootprintSource(): FootprintSource {
  try {
    const v = localStorage.getItem(SOURCE_KEY);
    if (v === 'mock' || v === 'real') return v;
  } catch {
    // node（vitest）无 localStorage——用默认值
  }
  return 'real';
}

export function setFootprintSource(source: FootprintSource): void {
  localStorage.setItem(SOURCE_KEY, source);
}

// ------------------------------------------------------------------ 类型

/** 带 aggTradeId 的逐笔成交（id 连续递增，断线补档 & 去重锚点） */
export interface AggTradeTick extends Trade {
  aggId: number;
}

/** kline 统一形状（REST 数组行 / WS k 对象都归一到这） */
export interface KlineData {
  openTime: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  /** taker 主动买量（base 币）；delta = 2*takerBuyVolume - volume */
  takerBuyVolume: number;
  closed: boolean;
}

/** kline 的 delta：taker买 − taker卖 = 2*takerBuy − 总量 */
export function klineDelta(k: KlineData): number {
  return 2 * k.takerBuyVolume - k.volume;
}

/** kline → ohlcOnly FootprintBar（cumDelta 由调用方按序累计后传入） */
export function klineToBar(
  symbol: string,
  timeframe: Timeframe,
  k: KlineData,
  cumDelta: number,
): FootprintBar {
  return {
    symbol,
    time: k.openTime,
    timeframe,
    open: k.open,
    high: k.high,
    low: k.low,
    close: k.close,
    levels: [],
    totalVol: k.volume,
    delta: klineDelta(k),
    cumDelta,
    poc: k.close,
    ohlcOnly: true,
  };
}

interface RawAggTrade {
  a: number;
  p: string;
  q: string;
  T: number;
  m: boolean;
}

/** 币安原始 aggTrade（REST/WS 同构字段）→ 内部 Tick */
export function toTick(raw: RawAggTrade): AggTradeTick {
  return {
    aggId: Number(raw.a),
    time: Number(raw.T),
    price: Number(raw.p),
    size: Number(raw.q),
    side: raw.m ? 'sell' : 'buy',
  };
}

// ------------------------------------------------------------------ REST

const REST_TIMEOUT_MS = 10_000;
/** 分页请求间隔：aggTrades weight=20，限流预算内匀速 */
const REST_GAP_MS = 120;
const RETRY_MAX = 4;
const RETRY_BASE_MS = 1_000;

// ---- 限流/封禁防护（模块级全局，覆盖所有 REST 调用方）----
// 币安规则：持续超频先回 429（带 Retry-After）；不收手则按 IP 封禁并回 418，
// 封禁时长递增（2min→3天），封禁期内继续请求会延长封禁。因此：
//   418 → 全局短路闸门（零请求），封禁截止点持久化跨页面刷新生效；
//   429 → 读 Retry-After 全局冷却，冷却期新请求等待而非发出；
//   权重预算 → 本地先行限流，从源头避免触发 429/418。

/** 币安合约 IP 权重预算 2400/min；留 1/4 余量给同 IP 其它进程 */
const WEIGHT_BUDGET_PER_MIN = 1_800;
/**
 * 解封后冷却窗（防「解封→回补风暴→秒被再封」死循环）：封禁解除后的这段时间
 * 内把权重预算压到极低，任何兜底 REST 都被强制错峰、单窗限额，杜绝瞬时打满。
 */
const POST_BAN_GRACE_MS = 60_000;
const POST_BAN_WEIGHT_BUDGET = 200;
/** 429 无 Retry-After 头时的保守全局冷却 */
const DEFAULT_429_COOLDOWN_MS = 10_000;
/** 418 无 Retry-After 头时的保守封禁时长（币安最短封 2min） */
const DEFAULT_418_BAN_MS = 120_000;
const BAN_STORE_KEY = 'jarvis-binance-ban-until';

function fmtClock(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** IP 已被币安封禁（418）：封禁解除前所有 REST 调用直接短路抛本错误（零网络请求） */
export class BinanceBanError extends Error {
  constructor(readonly banUntil: number) {
    super(
      `币安已临时封禁本机 IP（418 超频），预计 ${fmtClock(banUntil)} 自动解除，` +
        `封禁期内已停止全部行情请求`,
    );
  }
}

function readStoredBanUntil(): number {
  try {
    // 保留已过期的封禁截止点：解封后的「首次兜底加随机延迟」判定需要它
    // （封禁门本身用 banUntil > now 判断，过期值不会误拦请求）
    const v = Number(localStorage.getItem(BAN_STORE_KEY));
    return Number.isFinite(v) && v > 0 ? v : 0;
  } catch {
    return 0; // node（vitest）无 localStorage
  }
}

let banUntil = readStoredBanUntil();
let cooldownUntil = 0;
let weightWindowStart = 0;
let weightWindowUsed = 0;
// 解封后冷却截止点：封禁解除后 POST_BAN_GRACE_MS 内压低预算（跨刷新持久）
let postBanCooldownUntil = banUntil > 0 ? banUntil + POST_BAN_GRACE_MS : 0;

function setBan(durationMs: number): void {
  banUntil = Date.now() + durationMs;
  postBanCooldownUntil = banUntil + POST_BAN_GRACE_MS;
  try {
    localStorage.setItem(BAN_STORE_KEY, String(banUntil));
  } catch {
    // node（vitest）无 localStorage
  }
}

/** 距离 418 封禁解除的剩余毫秒（0 = 未被封禁）；UI/降级逻辑用 */
export function binanceBanRemainingMs(): number {
  return Math.max(0, banUntil - Date.now());
}

/**
 * 距离上次 418 封禁解除已过去的毫秒；从未封禁（或仍在封禁中）返回 Infinity。
 * 调用方据此对「解封后的首次 REST 兜底」加随机延迟错峰，
 * 避免「解封瞬间恢复风暴 → 秒被再封」的死循环。
 */
export function binanceBanLiftedAgoMs(): number {
  if (banUntil <= 0 || banUntil > Date.now()) return Number.POSITIVE_INFINITY;
  return Date.now() - banUntil;
}

/** 测试专用：解封后冷却窗截止点（0 = 无） */
export function postBanCooldownUntilForTest(): number {
  return postBanCooldownUntil;
}

/** 测试专用：重置模块级限流/封禁状态 */
export function resetRestGuardForTest(): void {
  banUntil = 0;
  cooldownUntil = 0;
  weightWindowStart = 0;
  weightWindowUsed = 0;
  postBanCooldownUntil = 0;
  try {
    localStorage.removeItem(BAN_STORE_KEY);
  } catch {
    // node（vitest）无 localStorage
  }
}

/** 当前分钟窗口的权重预算：解封冷却窗内压到极低，其余用常规预算 */
function currentWeightBudget(now: number): number {
  return now < postBanCooldownUntil ? POST_BAN_WEIGHT_BUDGET : WEIGHT_BUDGET_PER_MIN;
}

/** 本地权重预算：分钟窗口内超预算则等到下一窗口，从源头不触发 429 */
async function acquireWeight(weight: number): Promise<void> {
  for (;;) {
    const now = Date.now();
    if (now - weightWindowStart >= 60_000) {
      weightWindowStart = now;
      weightWindowUsed = 0;
    }
    if (weightWindowUsed + weight <= currentWeightBudget(now)) {
      weightWindowUsed += weight;
      return;
    }
    await sleep(weightWindowStart + 60_000 - now + 50);
  }
}

/** 解析 Retry-After 响应头（秒）；测试替身可能无 headers，全程防御访问 */
function retryAfterMs(res: { headers?: { get?: (k: string) => string | null } }): number | null {
  const raw = res.headers?.get?.('retry-after');
  if (raw == null) return null;
  const sec = Number(raw);
  return Number.isFinite(sec) && sec >= 0 ? sec * 1000 : null;
}

/** 4xx 参数类错误：不重试直接抛 */
class FatalRestError extends Error {}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function restJson(
  path: string,
  params: Record<string, string | number>,
  weight: number,
): Promise<unknown> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) qs.set(k, String(v));
  const url = `${REST_BASE}${path}?${qs.toString()}`;

  for (let attempt = 0; ; attempt++) {
    // 418 封禁期：直接短路，绝不发请求（继续请求会延长封禁）
    if (banUntil > Date.now()) throw new BinanceBanError(banUntil);
    // 429 冷却期：等待而非发出（全局生效，含并行分页的其它调用）
    const cooldownWait = cooldownUntil - Date.now();
    if (cooldownWait > 0) await sleep(cooldownWait);
    await acquireWeight(weight);

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), REST_TIMEOUT_MS);
    try {
      const res = await fetch(url, { signal: ctrl.signal });
      if (res.ok) return await res.json();
      if (res.status === 418) {
        // IP 已被封禁：重试只会延长封禁——设全局闸门后立即失败
        setBan(retryAfterMs(res) ?? DEFAULT_418_BAN_MS);
        throw new BinanceBanError(banUntil);
      }
      if (res.status === 429) {
        // 超频警告：按 Retry-After 全局冷却；继续加压会升级为 418 封禁
        const wait = retryAfterMs(res) ?? DEFAULT_429_COOLDOWN_MS;
        cooldownUntil = Math.max(cooldownUntil, Date.now() + wait);
        if (attempt >= RETRY_MAX) throw new Error(`binance 429: ${path}`);
        continue;
      }
      if (res.status < 500) throw new FatalRestError(`binance ${res.status}: ${path}`);
      throw new Error(`binance ${res.status}: ${path}`);
    } catch (err) {
      if (err instanceof FatalRestError || err instanceof BinanceBanError || attempt >= RETRY_MAX) {
        throw err;
      }
      await sleep(RETRY_BASE_MS * 2 ** attempt);
    } finally {
      clearTimeout(timer);
    }
  }
}

function parseKlineRow(row: (string | number)[]): KlineData {
  return {
    openTime: Number(row[0]),
    open: Number(row[1]),
    high: Number(row[2]),
    low: Number(row[3]),
    close: Number(row[4]),
    volume: Number(row[5]),
    takerBuyVolume: Number(row[9]),
    closed: Number(row[6]) <= Date.now(), // closeTime 已过 = 已收盘
  };
}

/** 拉取 [startTime, endTime] 的 K 线（自动分页，单页上限 1500） */
export async function fetchKlines(
  symbol: string,
  timeframe: Timeframe,
  startTime: number,
  endTime: number,
): Promise<KlineData[]> {
  const out: KlineData[] = [];
  let cursor = startTime;
  for (;;) {
    const rows = (await restJson(
      '/fapi/v1/klines',
      { symbol, interval: timeframe, startTime: cursor, endTime, limit: 1500 },
      10, // klines limit 1500 → weight 10
    )) as (string | number)[][];
    if (!Array.isArray(rows) || rows.length === 0) break;
    for (const r of rows) out.push(parseKlineRow(r));
    if (rows.length < 1500) break;
    cursor = Number(rows[rows.length - 1][0]) + 1;
    await sleep(REST_GAP_MS);
  }
  return out;
}

/** 最新 limit 根 K 线（WS 重连后覆盖大周期末柱用） */
export async function fetchLatestKlines(
  symbol: string,
  timeframe: Timeframe,
  limit: number,
): Promise<KlineData[]> {
  const rows = (await restJson(
    '/fapi/v1/klines',
    { symbol, interval: timeframe, limit },
    5, // klines limit ≤499 → weight 5（重连重放仅拉 2 根）
  )) as (string | number)[][];
  return Array.isArray(rows) ? rows.map(parseKlineRow) : [];
}

export interface RecentTradesResult {
  /** 按 aggId（=时间）升序 */
  ticks: AggTradeTick[];
  /** 最后一笔 aggId；无数据为 -1 */
  lastAggId: number;
  /** 预算耗尽仍未覆盖到目标窗口起点 */
  truncated: boolean;
}

/**
 * 近窗逐笔回填：从最新开始按 fromId 向历史翻页，直到覆盖 windowMs 或耗尽
 * maxRequests 预算。aggId 每 symbol 连续递增，逆序页无缝无重。
 */
export async function fetchRecentAggTrades(
  symbol: string,
  windowMs: number,
  maxRequests: number,
): Promise<RecentTradesResult> {
  const targetStart = Date.now() - windowMs;
  const pages: AggTradeTick[][] = [];
  let fromId: number | null = null;
  let truncated = true;

  for (let i = 0; i < maxRequests; i++) {
    const params: Record<string, string | number> = { symbol, limit: 1000 };
    if (fromId !== null) params.fromId = fromId;
    const raw = (await restJson('/fapi/v1/aggTrades', params, 20)) as RawAggTrade[];
    if (!Array.isArray(raw) || raw.length === 0) {
      truncated = false;
      break;
    }
    const page = raw.map(toTick);
    pages.push(page);
    const first = page[0];
    if (first.time <= targetStart || first.aggId <= 0) {
      truncated = false;
      break;
    }
    fromId = Math.max(0, first.aggId - 1000);
    await sleep(REST_GAP_MS);
  }

  pages.reverse(); // 页间从旧到新
  const merged: AggTradeTick[] = [];
  let lastId = -1;
  for (const page of pages) {
    for (const t of page) {
      if (t.aggId <= lastId) continue; // 页界重叠去重
      merged.push(t);
      lastId = t.aggId;
    }
  }
  // 裁掉窗口外的更早部分（最后一页可能超出目标窗口）
  const startIdx = merged.findIndex((t) => t.time >= targetStart);
  const ticks = startIdx > 0 ? merged.slice(startIdx) : merged;
  return { ticks, lastAggId: lastId, truncated };
}

/**
 * 正向补档：从 fromId 开始拉到追平实时（返回条数 < 1000 视为追平）。
 * 预算耗尽返回 complete=false，调用方按缺口处理。
 */
export async function fetchAggTradesFrom(
  symbol: string,
  fromId: number,
  maxRequests: number,
): Promise<{ ticks: AggTradeTick[]; complete: boolean }> {
  const out: AggTradeTick[] = [];
  let cursor = fromId;
  for (let i = 0; i < maxRequests; i++) {
    const raw = (await restJson(
      '/fapi/v1/aggTrades',
      { symbol, fromId: cursor, limit: 1000 },
      20,
    )) as RawAggTrade[];
    if (!Array.isArray(raw) || raw.length === 0) return { ticks: out, complete: true };
    for (const r of raw) out.push(toTick(r));
    if (raw.length < 1000) return { ticks: out, complete: true };
    cursor = out[out.length - 1].aggId + 1;
    await sleep(REST_GAP_MS);
  }
  return { ticks: out, complete: false };
}

// ------------------------------------------------------------------ 实时流

/**
 * WS 重连补档的分页预算：1000 笔/页 ×8 ≈ 覆盖秒级~分钟级正常重连缺口。
 * 更长断档由 onGap 触发上层「后端重建」（零币安权重），不再靠币安逆序翻页，
 * 从源头杜绝重连时的币安 REST 请求风暴。
 */
const GAP_FILL_MAX_REQUESTS = 8;
const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

export interface LiveFeedHandlers {
  /** 连续有序（含补档回放）的逐笔成交，aggId 严格递增 */
  onTrade(tick: AggTradeTick): void;
  /** 大周期 kline 当前柱/收盘柱（重连后会重放最近 2 根用于覆盖） */
  onKline(timeframe: Timeframe, k: KlineData): void;
  /** 断档无法精确补齐（REST 失败/预算耗尽），上层应触发重建 */
  onGap(reason: string): void;
}

/** BinanceLiveFeed 可选行为 */
export interface LiveFeedOptions {
  /**
   * 连接（含重连）后是否用 REST 重放大周期最近 2 根 K 线覆盖末柱。
   * 默认 true（保持历史行为）；当上层已从后端 seed 大周期当前柱时应传 false，
   * 避免每次连接都发币安 REST（WS kline 流会在数秒内自愈末柱）。
   */
  replayKlinesOnConnect?: boolean;
}

/**
 * 币安合约实时流：单 WS 复用 aggTrade + 大周期 kline。
 * start() 后自治运行（断线自动重连+补档），stop() 彻底停止。
 */
export class BinanceLiveFeed {
  private ws: WebSocket | null = null;
  private stopped = false;
  private reconnectDelay = RECONNECT_MIN_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  /** 已向外发出的最后 aggId（-1 = 尚无锚点，首条直接透传） */
  private lastAggId: number;
  private buffering = false;
  private buffer: AggTradeTick[] = [];

  constructor(
    private readonly symbol: string,
    private readonly klineTfs: readonly Timeframe[],
    private readonly handlers: LiveFeedHandlers,
    sinceAggId = -1,
    private readonly opts: LiveFeedOptions = {},
  ) {
    this.lastAggId = sinceAggId;
  }

  start(): void {
    if (this.stopped || this.ws) return;
    const sym = this.symbol.toLowerCase();
    const streams = [`${sym}@aggTrade`, ...this.klineTfs.map((tf) => `${sym}@kline_${tf}`)];
    const ws = new WebSocket(`${WS_BASE}/stream?streams=${streams.join('/')}`);
    this.ws = ws;
    // 有锚点则先缓冲 WS 推送，REST 补齐 [lastAggId+1, …] 后再回放，保证有序
    this.buffering = this.lastAggId >= 0;
    this.buffer = [];

    ws.onopen = () => {
      this.reconnectDelay = RECONNECT_MIN_MS;
      if (this.buffering) void this.fillGap();
      if (this.opts.replayKlinesOnConnect !== false) void this.replayKlines();
    };
    ws.onmessage = (ev) => this.onMessage(ev);
    ws.onclose = () => this.scheduleReconnect();
    ws.onerror = () => {
      // 统一走 onclose 重连路径
    };
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      try {
        ws.close();
      } catch {
        // 忽略关闭异常
      }
    }
  }

  isRunning(): boolean {
    return !this.stopped && this.ws !== null;
  }

  // ---------------------------------------------------------- 内部

  private onMessage(ev: MessageEvent): void {
    let payload: { data?: Record<string, unknown> };
    try {
      payload = JSON.parse(String(ev.data)) as { data?: Record<string, unknown> };
    } catch {
      return;
    }
    const data = payload.data;
    if (!data) return;

    if (data.e === 'aggTrade') {
      const tick = toTick(data as unknown as RawAggTrade);
      if (this.buffering) this.buffer.push(tick);
      else this.emitTrade(tick);
    } else if (data.e === 'kline') {
      const k = data.k as Record<string, unknown>;
      const tf = String(k.i) as Timeframe;
      if (!this.klineTfs.includes(tf)) return;
      this.handlers.onKline(tf, {
        openTime: Number(k.t),
        open: Number(k.o),
        high: Number(k.h),
        low: Number(k.l),
        close: Number(k.c),
        volume: Number(k.v),
        takerBuyVolume: Number(k.V),
        closed: k.x === true,
      });
    }
  }

  private emitTrade(tick: AggTradeTick): void {
    if (tick.aggId <= this.lastAggId) return; // 补档/缓冲重叠去重
    this.lastAggId = tick.aggId;
    this.handlers.onTrade(tick);
  }

  /** REST 正向补 [lastAggId+1 → 追平]，随后回放缓冲的 WS 推送 */
  private async fillGap(): Promise<void> {
    let gapReason: string | null = null;
    try {
      const { ticks, complete } = await fetchAggTradesFrom(
        this.symbol,
        this.lastAggId + 1,
        GAP_FILL_MAX_REQUESTS,
      );
      for (const t of ticks) this.emitTrade(t);
      if (!complete) gapReason = 'gap fill budget exhausted';
    } catch (err) {
      gapReason = `gap fill failed: ${err instanceof Error ? err.message : String(err)}`;
    } finally {
      const buffered = this.buffer;
      this.buffer = [];
      this.buffering = false;
      for (const t of buffered) this.emitTrade(t);
    }
    if (gapReason && !this.stopped) this.handlers.onGap(gapReason);
  }

  /** 重连（含首连）后重放大周期最近 2 根，覆盖断线期间的末柱状态 */
  private async replayKlines(): Promise<void> {
    for (const tf of this.klineTfs) {
      try {
        const rows = await fetchLatestKlines(this.symbol, tf, 2);
        if (this.stopped) return;
        for (const k of rows) this.handlers.onKline(tf, k);
      } catch {
        // 下一根 WS kline 推送会自愈
      }
    }
  }

  private scheduleReconnect(): void {
    this.ws = null;
    if (this.stopped || this.reconnectTimer) return;
    const delay = this.reconnectDelay;
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, RECONNECT_MAX_MS);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.start();
    }, delay);
  }
}
