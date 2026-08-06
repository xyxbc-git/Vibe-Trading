// 威科夫阶段引擎前端契约 + 数据变换：GET /api/wyckoff 冻结契约（开发计划
// §T2.5）的 TS 类型、12 事件 type→图标/配色映射、阶段带与事件标记的图表
// 载荷换算。全部纯函数（无 IO、无图表依赖），渲染交给 WyckoffPrimitive
// （与 trapSignals.ts → TrapSignalsPrimitive 同型的 marker 通道结构）。
//
// 契约来源：威科夫阶段引擎 GET /api/wyckoff?symbol=&interval=（P2 后端，
// 并行开发中，契约已冻结）。事件 type 约定小写：sc/ar/st/spring/test/sos/
// lps/bc/ut/utad/sow/lpsy；range=null 表示趋势段（无交易区间，不画阶段带）。

import { fmtTrapTime, type TrapAnchorBar } from "./trapSignals";

// ── GET /api/wyckoff 冻结契约类型 ──────────────────────────────────────

export type WyckoffSide = "acc" | "dist" | "trend" | "unknown";
export type WyckoffPhase = "A" | "B" | "C" | "D" | "E";

/** 12 威科夫事件（小写约定，与后端引擎一致） */
export type WyckoffEventType =
  | "sc"
  | "ar"
  | "st"
  | "spring"
  | "test"
  | "sos"
  | "lps"
  | "bc"
  | "ut"
  | "utad"
  | "sow"
  | "lpsy";

/** 交易区间；null = 趋势段（detect_range 未成立） */
export interface WyckoffRange {
  high: number;
  low: number;
  /** 区间起点（unix 秒） */
  start_ts: number;
  mid?: number;
  bars_in_range?: number;
  atr?: number;
}

/** 阶段状态机输出（resolve_phase） */
export interface WyckoffState {
  side: WyckoffSide;
  /** 无区间（trend/unknown）时可能缺失 */
  phase?: WyckoffPhase | null;
  since_ts?: number | null;
}

/** 单个威科夫事件（已绑定订单流证据的置信度） */
export interface WyckoffEvent {
  type: WyckoffEventType;
  /** unix 秒（事件所在 K 线的开盘时间） */
  ts: number;
  price: number;
  /** 0..1（叙事与订单流证据打架时会被压低） */
  confidence?: number;
  /** 事件窗口的供需裁决方向（P1 sd-verdict 口径） */
  sd_bias?: "accumulation" | "distribution" | "neutral" | null;
  /** 中文逐条证据 */
  reasons?: string[];
}

/** GET /api/wyckoff 响应封套（ok:false 时 HTTP 仍为 200，调用方须自行判断） */
export interface WyckoffResponse {
  ok?: boolean;
  error?: string;
  symbol?: string;
  interval?: string;
  range?: WyckoffRange | null;
  state?: WyckoffState | null;
  events?: WyckoffEvent[];
  /** 一句话研判提示（如「Phase C 弹簧已确认…等待 SOS/LPS 入场结构」） */
  verdict_hint?: string | null;
  stale?: boolean;
}

// ── 事件 type → 图标/文案/方位映射 ─────────────────────────────────────

export interface WyckoffEventMeta {
  /** 徽章牌面缩写（图标文字） */
  abbr: string;
  /** 中文名 */
  label: string;
  /** 事件归属侧：acc=吸筹叙事 / dist=派发叙事（决定徽章配色） */
  side: "acc" | "dist";
  /** 徽章挂靠方位：below=锚定 bar 低点下方（低点事件）/ above=高点上方 */
  anchor: "above" | "below";
}

export const WYCKOFF_EVENT_META: Record<WyckoffEventType, WyckoffEventMeta> = {
  sc: { abbr: "SC", label: "恐慌抛售", side: "acc", anchor: "below" },
  ar: { abbr: "AR", label: "自动反弹", side: "acc", anchor: "above" },
  st: { abbr: "ST", label: "二次测试", side: "acc", anchor: "below" },
  spring: { abbr: "SPR", label: "弹簧", side: "acc", anchor: "below" },
  test: { abbr: "TST", label: "弹簧测试", side: "acc", anchor: "below" },
  sos: { abbr: "SOS", label: "强势信号", side: "acc", anchor: "above" },
  lps: { abbr: "LPS", label: "最后支撑", side: "acc", anchor: "below" },
  bc: { abbr: "BC", label: "抢购高潮", side: "dist", anchor: "above" },
  ut: { abbr: "UT", label: "上冲回落", side: "dist", anchor: "above" },
  utad: { abbr: "UTAD", label: "派发上冲", side: "dist", anchor: "above" },
  sow: { abbr: "SOW", label: "弱势信号", side: "dist", anchor: "below" },
  lpsy: { abbr: "LPSY", label: "最后供给", side: "dist", anchor: "above" },
};

/** 阶段带/徽章配色：吸筹绿 / 派发红（与涨绿跌红铁律一致） */
export const WYCKOFF_SIDE_COLORS: Record<"acc" | "dist", string> = {
  acc: "#3fb950",
  dist: "#f85149",
};

export const WYCKOFF_SIDE_LABELS: Record<WyckoffSide, string> = {
  acc: "吸筹",
  dist: "派发",
  trend: "趋势段",
  unknown: "未知",
};

/** 阶段带填充透明度按 Phase A→E 递进加深（叙事越成熟带越实） */
export const WYCKOFF_PHASE_ALPHA: Record<WyckoffPhase, number> = {
  A: 0.06,
  B: 0.09,
  C: 0.12,
  D: 0.16,
  E: 0.2,
};

/** phase 缺失/非法时的阶段带默认透明度 */
const DEFAULT_BAND_ALPHA = 0.1;

/** 威科夫开关的 localStorage 键（K 线页记住用户偏好） */
export const WYCKOFF_TOGGLE_KEY = "jarvis.chart.wyckoff";

// ── 图表渲染载荷 ────────────────────────────────────────────────────────

/** 阶段背景带：时间 [fromSec, toSec] × 价格 [low, high] 半透明矩形 */
export interface WyckoffBandView {
  /** 吸附到窗口内 bar 开盘时间（unix 秒） */
  fromSec: number;
  toSec: number;
  high: number;
  low: number;
  /** 填充色 #rrggbbaa（side 色 × phase 透明度已折算） */
  fill: string;
  /** 边缘线/标签色（较实） */
  edge: string;
  /** 带内左上角标签（如「威科夫吸筹 · Phase C」） */
  label: string;
}

/** 事件标记载荷：徽章画在锚定 bar 影线之外（同 TrapMark 结构思路） */
export interface WyckoffMark {
  /** 吸附后的 bar 开盘时间（unix 秒） */
  timeSec: number;
  /** 锚定 bar 高点（above 事件挂其上方） */
  anchorHigh: number;
  /** 锚定 bar 低点（below 事件挂其下方） */
  anchorLow: number;
  position: "above" | "below";
  /** 徽章牌面文字 */
  abbr: string;
  /** 徽章底色（side 色） */
  color: string;
  event: WyckoffEvent;
  /** 悬停提示（单行） */
  tooltip: string;
}

/** 阶段带 + 事件标记合并载荷（KlineChart 的 wyckoff prop） */
export interface WyckoffOverlay {
  band: WyckoffBandView | null;
  marks: WyckoffMark[];
}

function fmtWyckoffPrice(v: number): string {
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

// #rrggbb → #rrggbbaa
function withAlpha(color: string, alpha: number): string {
  const a = clamp01(alpha);
  return /^#[0-9a-fA-F]{6}$/.test(color)
    ? color + Math.round(a * 255).toString(16).padStart(2, "0")
    : color;
}

/** bars 升序前提下，把 ts 吸附到最近 bar 的下标（同 buildTrapMarks 口径） */
function snapIdx(bars: { timeSec: number }[], ts: number): number {
  let lo = 0;
  let hi = bars.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid].timeSec < ts) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0) {
    const cur = bars[lo].timeSec;
    const prev = bars[lo - 1].timeSec;
    if (ts - prev <= cur - ts) return lo - 1;
  }
  return lo;
}

/**
 * 响应 → 阶段背景带载荷。range=null（趋势段）/ side 非 acc·dist / 区间几何
 * 非法 / 区间起点在窗口右侧之外 → null（不画带）。fromSec 吸附到窗口内
 * bar 开盘时间，toSec 恒为最新 bar（区间延伸到当下）。
 */
export function buildWyckoffBand(
  resp: WyckoffResponse | null,
  bars: { timeSec: number }[],
): WyckoffBandView | null {
  if (!resp || resp.ok === false || bars.length === 0) return null;
  const rng = resp.range;
  const side = resp.state?.side;
  if (!rng || (side !== "acc" && side !== "dist")) return null;

  const high = Number(rng.high);
  const low = Number(rng.low);
  const startTs = Number(rng.start_ts);
  if (!Number.isFinite(high) || !Number.isFinite(low) || high <= low) return null;
  if (!Number.isFinite(startTs)) return null;

  const lastTs = bars[bars.length - 1].timeSec;
  if (startTs > lastTs) return null;

  const fromSec = bars[snapIdx(bars, Math.max(startTs, bars[0].timeSec))].timeSec;
  const phase = resp.state?.phase ?? null;
  const alpha =
    phase && phase in WYCKOFF_PHASE_ALPHA
      ? WYCKOFF_PHASE_ALPHA[phase as WyckoffPhase]
      : DEFAULT_BAND_ALPHA;
  const color = WYCKOFF_SIDE_COLORS[side];

  return {
    fromSec,
    toSec: lastTs,
    high,
    low,
    fill: withAlpha(color, alpha),
    edge: withAlpha(color, 0.55),
    label: `威科夫${WYCKOFF_SIDE_LABELS[side]}${phase ? ` · Phase ${phase}` : ""}`,
  };
}

/**
 * 响应 → 事件徽章载荷。未知事件 type / 非法 ts·price / 窗口外的事件丢弃；
 * 正常契约 ts 即 bar 开盘时间直接命中，二分吸附兜底容错。返回按时间升序。
 */
export function buildWyckoffMarks(
  resp: WyckoffResponse | null,
  bars: TrapAnchorBar[],
): WyckoffMark[] {
  if (!resp || resp.ok === false || !Array.isArray(resp.events) || bars.length === 0) {
    return [];
  }
  const firstTs = bars[0].timeSec;
  const lastTs = bars[bars.length - 1].timeSec;

  const marks: WyckoffMark[] = [];
  for (const ev of resp.events) {
    const meta = WYCKOFF_EVENT_META[ev?.type as WyckoffEventType];
    if (!meta) continue;
    const ts = Number(ev?.ts);
    const price = Number(ev?.price);
    if (!Number.isFinite(ts) || !Number.isFinite(price)) continue;
    if (ts < firstTs || ts > lastTs) continue;

    const bar = bars[snapIdx(bars, ts)];
    const conf = clamp01(Number(ev.confidence ?? 0));
    const biasNote =
      ev.sd_bias === "accumulation"
        ? " · 订单流吸筹佐证"
        : ev.sd_bias === "distribution"
          ? " · 订单流派发佐证"
          : "";
    marks.push({
      timeSec: bar.timeSec,
      anchorHigh: bar.high,
      anchorLow: bar.low,
      position: meta.anchor,
      abbr: meta.abbr,
      color: WYCKOFF_SIDE_COLORS[meta.side],
      event: ev,
      tooltip:
        `◆ 威科夫·${meta.label}（${meta.abbr}） · 置信 ${(conf * 100).toFixed(0)}%` +
        ` · ${fmtTrapTime(ts)} @ ${fmtWyckoffPrice(price)}${biasNote}`,
    });
  }
  marks.sort((a, b) => a.timeSec - b.timeSec);
  return marks;
}
