import type { FootprintBar, PriceLevel } from "@/types/footprint";
import type { FpSignal } from "./insight/signals";
import type { VolumeProfile } from "./profile";

/**
 * 大周期远期历史可能只有 OHLC 无逐笔（数据层新增可选标记，契约文件由
 * MCP-2 维护）；渲染层本地窄化读取，这类柱直接画蜡烛，不画空格子。
 */
export const isOhlcOnly = (bar: FootprintBar): boolean =>
  (bar as FootprintBar & { ohlcOnly?: boolean }).ohlcOnly === true ||
  bar.levels.length === 0;

/**
 * 视口状态（物理引擎版，全部存 ref，不进 React state）。
 * zoomX/zoomY 各自向 target 指数趋近实现平滑缩放（滚轮同缩、拖轴单缩）；
 * velX 为横向惯性速度（px/s）；centerPrice=null 表示纵向自动取可见范围
 * 中点（跟随模式），拖拽后物化为具体价格；follow=true 时横向贴住最新柱
 * 右侧的留白锚点（rightGapBars 柱宽，TradingView 式右侧空白，拖拽可调）。
 */
export interface ViewportState {
  zoomX: number;
  zoomY: number;
  zoomTargetX: number;
  zoomTargetY: number;
  /** 缩放锚点（画布坐标），保证缩放时光标下的柱位/价格不飘移 */
  anchor: { mx: number; my: number } | null;
  scrollX: number;
  velX: number;
  centerPrice: number | null;
  follow: boolean;
  dragging: boolean;
  /** follow 态下最新柱与价格轴之间保留的空白（单位：柱宽倍数） */
  rightGapBars: number;
}

/** 右侧留白默认值/上限（柱宽倍数） */
export const RIGHT_GAP_BARS_DEFAULT = 6;
export const RIGHT_GAP_BARS_MAX = 30;

export type HoverInfo =
  | {
      kind: "cell";
      barIndex: number;
      price: number;
      level: PriceLevel | null;
      isPoc: boolean;
    }
  | { kind: "stats"; row: number; barIndex: number }
  | null;

export interface BadgeBox {
  x: number;
  y: number;
  r: number;
  signal: FpSignal;
}

export type RenderMode = "full" | "cells" | "candles";

export interface Layout {
  width: number;
  height: number;
  chartW: number;
  plotH: number;
  barW: number;
  rowH: number;
  scrollX: number;
  maxScroll: number;
  tick: number;
  centerPrice: number;
  visStart: number;
  visEnd: number;
  mode: RenderMode;
}

export const BASE_BAR_W = 88;
export const BASE_ROW_H = 17;
export const AXIS_W = 62;
export const TIME_H = 20;
export const MIN_ZOOM = 0.04;
export const MAX_ZOOM = 4;

/** 底部统计区：四行（成交量 / Delta / Delta% / 累计Δ） */
export const STATS_ROWS = [
  { key: "vol", label: "成交量" },
  { key: "delta", label: "Delta" },
  { key: "deltaPct", label: "Delta%" },
  { key: "cum", label: "累计Δ" },
] as const;
export const STATS_ROW_H = 22;
export const STATS_H = STATS_ROWS.length * STATS_ROW_H;

/** 失衡判定：一侧 ≥ IMBALANCE_RATIO × 对侧，且量级不低于可见最大格的 5%（滤噪） */
export const IMBALANCE_RATIO = 3;

export const COLORS = {
  bg: "#0b1e2d",
  panel: "#0a1b29",
  border: "#1e3a52",
  grid: "rgba(148,163,184,0.07)",
  colSep: "rgba(148,163,184,0.05)",
  up: "#3b82f6",
  down: "#ef4444",
  upText: "#93c5fd",
  downText: "#fda4af",
  text: "#cbd5e1",
  dim: "#64748b",
  imbalance: "#22c55e",
  poc: "#eab308",
  last: "#e2e8f0",
  sweep: "#f59e0b",
  divergence: "#bc8cff",
  profileBar: "rgba(148,163,184,0.30)",
  profileBarInVa: "rgba(96,165,250,0.38)",
  valueArea: "rgba(96,165,250,0.06)",
  vaEdge: "rgba(96,165,250,0.45)",
} as const;

/** Volume Profile 横向条最大占图区宽度比例 */
export const PROFILE_MAX_W_RATIO = 0.18;

const FONT_MONO = '"SF Mono", Menlo, Monaco, monospace';

export const clamp = (v: number, lo: number, hi: number): number =>
  Math.min(hi, Math.max(lo, v));

/** zoomX/zoomY → 柱宽/格高（物理引擎与布局共用，保证锚点计算与实际渲染一致） */
export const barWOf = (zoomX: number): number => clamp(BASE_BAR_W * zoomX, 2, 420);
export const rowHOf = (zoomY: number): number => clamp(BASE_ROW_H * zoomY, 1.2, 64);

const trimZero = (s: string): string => (s.endsWith(".0") ? s.slice(0, -2) : s);

/** 千位缩写：809 → 809，1234 → 1.2k，2456000 → 2.5m（保留符号） */
export function fmtK(n: number): string {
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  if (a >= 1e6) return sign + trimZero((a / 1e6).toFixed(1)) + "m";
  if (a >= 1000) return sign + trimZero((a / 1e3).toFixed(1)) + "k";
  return sign + String(Math.round(a));
}

export const fmtSigned = (n: number): string => (n > 0 ? "+" : "") + fmtK(n);

export function fmtPrice(p: number, tick: number): string {
  const dec = tick >= 1 ? 0 : Math.min(6, Math.ceil(-Math.log10(tick)));
  return p.toLocaleString("en-US", {
    minimumFractionDigits: dec,
    maximumFractionDigits: dec,
  });
}

/** bar.time 兼容秒/毫秒时间戳 */
export function fmtTime(t: number): string {
  const ms = t < 1e12 ? t * 1000 : t;
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** 从数据推导最小价格步长（tick），用于对齐价格网格 */
export function estimateTick(bars: FootprintBar[]): number {
  let best = Infinity;
  const start = Math.max(0, bars.length - 40);
  for (let i = start; i < bars.length; i++) {
    const prices = bars[i].levels.map((l) => l.price).sort((a, b) => a - b);
    for (let j = 1; j < prices.length; j++) {
      const d = prices[j] - prices[j - 1];
      if (d > 1e-9 && d < best) best = d;
    }
  }
  if (Number.isFinite(best) && best > 0) return best;
  const last = bars[bars.length - 1];
  if (last && last.high > last.low) {
    return (last.high - last.low) / Math.max(1, last.levels.length - 1 || 8);
  }
  return 1;
}

export const xOfBar = (l: Layout, i: number): number => i * l.barW - l.scrollX;

export const yOfPrice = (l: Layout, p: number): number =>
  l.plotH / 2 + ((l.centerPrice - p) / l.tick) * l.rowH;

export function computeLayout(
  vp: ViewportState,
  bars: FootprintBar[],
  width: number,
  height: number,
  tick: number,
): Layout {
  const barW = barWOf(vp.zoomX);
  const rowH = rowHOf(vp.zoomY);
  const chartW = Math.max(40, width - AXIS_W);
  const plotH = Math.max(40, height - TIME_H - STATS_H);
  const n = bars.length;
  // maxScroll = follow 态停靠点：最新柱右缘与价格轴之间留 rightGapBars 柱宽空白；
  // 像素上限 60% 图区宽，防止高倍放大时留白吞掉整屏（6 柱 × 大 barW > chartW）
  const gapPx = Math.min(
    clamp(vp.rightGapBars, 0, RIGHT_GAP_BARS_MAX) * barW,
    chartW * 0.6,
  );
  const maxScroll = n * barW - chartW + gapPx;

  let scrollX: number;
  if (vp.follow || n === 0) {
    scrollX = maxScroll;
  } else {
    const lo = Math.min(maxScroll, 0) - chartW * 0.25;
    // 右界放宽到「最新柱完全离开左缘」前，允许把留白拖得更大
    scrollX = clamp(vp.scrollX, lo, n * barW - barW);
  }

  const visStart = clamp(Math.floor(scrollX / barW), 0, n);
  const visEnd = clamp(Math.ceil((scrollX + chartW) / barW) + 1, visStart, n);

  let centerPrice = vp.centerPrice ?? Number.NaN;
  if (!Number.isFinite(centerPrice)) {
    let lo = Infinity;
    let hi = -Infinity;
    for (let i = visStart; i < visEnd; i++) {
      lo = Math.min(lo, bars[i].low);
      hi = Math.max(hi, bars[i].high);
    }
    if (!Number.isFinite(lo) && n > 0) {
      lo = bars[n - 1].low;
      hi = bars[n - 1].high;
    }
    centerPrice = Number.isFinite(lo) ? (lo + hi) / 2 : 0;
  }

  const mode: RenderMode =
    rowH >= 13.5 && barW >= 52 ? "full" : rowH >= 3 && barW >= 14 ? "cells" : "candles";

  const t = tick > 0 && Number.isFinite(tick) ? tick : 1;
  return {
    width,
    height,
    chartW,
    plotH,
    barW,
    rowH,
    scrollX,
    maxScroll,
    tick: t,
    centerPrice,
    visStart,
    visEnd,
    mode,
  };
}

/** 命中测试：图区返回价位格，底部统计区返回行+柱 */
export function hitTest(
  l: Layout,
  bars: FootprintBar[],
  x: number,
  y: number,
): HoverInfo {
  if (x < 0 || x >= l.chartW || bars.length === 0) return null;
  const i = Math.floor((x + l.scrollX) / l.barW);
  if (i < 0 || i >= bars.length) return null;

  const statsTop = l.plotH + TIME_H;
  if (y >= statsTop && y < statsTop + STATS_H) {
    const row = clamp(Math.floor((y - statsTop) / STATS_ROW_H), 0, STATS_ROWS.length - 1);
    return { kind: "stats", row, barIndex: i };
  }
  if (y < 0 || y >= l.plotH) return null;

  const bar = bars[i];
  let best: PriceLevel | null = null;
  let bd = Infinity;
  for (const lv of bar.levels) {
    const d = Math.abs(yOfPrice(l, lv.price) - y);
    if (d < bd) {
      bd = d;
      best = lv;
    }
  }
  const level = best && bd <= l.rowH / 2 ? best : null;
  const price =
    level?.price ?? l.centerPrice - ((y - l.plotH / 2) * l.tick) / l.rowH;
  const isPoc = level != null && Math.abs(level.price - bar.poc) < l.tick / 2;
  return { kind: "cell", barIndex: i, price, level, isPoc };
}

interface AxisLabel {
  y: number;
  text: string;
}

function drawGrid(ctx: CanvasRenderingContext2D, l: Layout): AxisLabel[] {
  const labels: AxisLabel[] = [];
  const kStep = Math.max(1, Math.ceil(24 / l.rowH));
  const step = l.tick * kStep;
  const pTop = l.centerPrice + ((l.plotH / 2) * l.tick) / l.rowH;
  const pBot = l.centerPrice - ((l.plotH / 2) * l.tick) / l.rowH;
  let m = Math.floor(pTop / step);
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let guard = 0; guard < 400; guard++, m--) {
    const price = m * step;
    if (price < pBot - l.tick) break;
    const y = Math.round(yOfPrice(l, price)) + 0.5;
    if (y < -2 || y > l.plotH + 2) continue;
    ctx.moveTo(0, y);
    ctx.lineTo(l.chartW, y);
    labels.push({ y, text: fmtPrice(price, l.tick) });
  }
  ctx.stroke();
  return labels;
}

function drawCandle(
  ctx: CanvasRenderingContext2D,
  l: Layout,
  bar: FootprintBar,
  x: number,
): void {
  const up = bar.close >= bar.open;
  const color = up ? COLORS.up : COLORS.down;
  const cx = x + l.barW / 2;
  const yH = yOfPrice(l, bar.high);
  const yL = yOfPrice(l, bar.low);
  const yO = yOfPrice(l, bar.open);
  const yC = yOfPrice(l, bar.close);

  ctx.strokeStyle = color;
  ctx.globalAlpha = 0.9;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx, yH);
  ctx.lineTo(cx, yL);
  ctx.stroke();

  const bodyW = clamp(l.barW * 0.66, 1, 13);
  const top = Math.min(yO, yC);
  const h = Math.max(1, Math.abs(yO - yC));
  ctx.fillStyle = color;
  ctx.fillRect(cx - bodyW / 2, top, bodyW, h);
  ctx.globalAlpha = 1;
}

function drawFootprintBar(
  ctx: CanvasRenderingContext2D,
  l: Layout,
  bar: FootprintBar,
  barIndex: number,
  x: number,
  maxCell: number,
  hover: HoverInfo,
): void {
  const up = bar.close >= bar.open;
  const dirColor = up ? COLORS.up : COLORS.down;
  const cx = x + l.barW / 2;

  // 影线先画，随后被单元格覆盖大部分，只在无格区域可见
  ctx.strokeStyle = dirColor;
  ctx.globalAlpha = 0.4;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx, yOfPrice(l, bar.high) - l.rowH / 2);
  ctx.lineTo(cx, yOfPrice(l, bar.low) + l.rowH / 2);
  ctx.stroke();
  ctx.globalAlpha = 1;

  const showText = l.mode === "full";
  const fontPx = clamp(Math.min(l.rowH - 6, l.barW / 7.2), 8, 12);
  const imbFloor = maxCell * 0.05;

  // 实体区间（open-close）：区间内格子饱满，影线区（实体外的 high-low）格子
  // 压暗淡化——放大后一眼区分「主战场」与「试探过的价位」（用户反馈项）
  const bodyHi = Math.max(bar.open, bar.close);
  const bodyLo = Math.min(bar.open, bar.close);

  for (const lv of bar.levels) {
    const yc = yOfPrice(l, lv.price);
    const y = yc - l.rowH / 2;
    if (y > l.plotH || y + l.rowH < 0) continue;

    const inBody = lv.price >= bodyLo - l.tick / 2 && lv.price <= bodyHi + l.tick / 2;
    const wickFade = inBody ? 1 : 0.35;

    const bid = lv.bidVol;
    const ask = lv.askVol;
    const vol = Math.max(bid, ask);
    if (vol > 0) {
      const t = Math.pow(Math.min(1, vol / maxCell), 0.55);
      const alpha = (0.06 + t * 0.5) * wickFade;
      ctx.fillStyle =
        ask >= bid
          ? `rgba(37,99,235,${alpha.toFixed(3)})`
          : `rgba(239,68,68,${alpha.toFixed(3)})`;
      ctx.fillRect(x + 0.5, y + 0.5, l.barW - 1, l.rowH - 1);
    }

    // POC 价位：琥珀描边
    if (Math.abs(lv.price - bar.poc) < l.tick / 2) {
      ctx.strokeStyle = COLORS.poc;
      ctx.lineWidth = 1;
      ctx.strokeRect(x + 0.5, y + 0.5, l.barW - 1, l.rowH - 1);
    }

    // 失衡格：绿色描边
    const askImb = ask >= IMBALANCE_RATIO * Math.max(bid, 1) && ask >= imbFloor;
    const bidImb = bid >= IMBALANCE_RATIO * Math.max(ask, 1) && bid >= imbFloor;
    if (askImb || bidImb) {
      ctx.strokeStyle = COLORS.imbalance;
      ctx.lineWidth = 1.2;
      ctx.strokeRect(x + 1.2, y + 1.2, l.barW - 2.4, l.rowH - 2.4);
    }

    // 悬停格：白色描边
    if (
      hover?.kind === "cell" &&
      hover.barIndex === barIndex &&
      hover.level &&
      Math.abs(hover.level.price - lv.price) < l.tick / 2
    ) {
      ctx.strokeStyle = "rgba(255,255,255,0.85)";
      ctx.lineWidth = 1;
      ctx.strokeRect(x + 0.5, y + 0.5, l.barW - 1, l.rowH - 1);
    }

    if (showText && l.rowH >= 12) {
      const mid = x + l.barW / 2;
      ctx.textBaseline = "middle";
      // 左列 bid（主动卖），右列 ask（主动买）；强势侧加粗；影线区文字同步淡化
      ctx.font = `${bid > ask ? "700" : "400"} ${fontPx}px ${FONT_MONO}`;
      ctx.fillStyle = bidImb ? COLORS.imbalance : COLORS.downText;
      ctx.globalAlpha = (bid >= ask ? 0.95 : 0.6) * wickFade;
      ctx.textAlign = "right";
      ctx.fillText(fmtK(bid), mid - 4, yc);

      ctx.font = `${ask > bid ? "700" : "400"} ${fontPx}px ${FONT_MONO}`;
      ctx.fillStyle = askImb ? COLORS.imbalance : COLORS.upText;
      ctx.globalAlpha = (ask >= bid ? 0.95 : 0.6) * wickFade;
      ctx.textAlign = "left";
      ctx.fillText(fmtK(ask), mid + 4, yc);
      ctx.globalAlpha = 1;
    }
  }

  // 实体轮廓（open-close 区间），呈现阳线/阴线走势
  const yA = yOfPrice(l, Math.max(bar.open, bar.close)) - l.rowH / 2;
  const yB = yOfPrice(l, Math.min(bar.open, bar.close)) + l.rowH / 2;
  ctx.strokeStyle = dirColor;
  ctx.globalAlpha = 0.8;
  ctx.lineWidth = 1;
  ctx.strokeRect(x + 0.5, yA, l.barW - 1, Math.max(1, yB - yA));
  ctx.globalAlpha = 1;

  // 柱顶汇总成交量
  if (l.barW >= 40) {
    const yTop = clamp(yOfPrice(l, bar.high) - l.rowH / 2 - 6, 9, l.plotH - 4);
    ctx.font = `600 10px ${FONT_MONO}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = bar.delta >= 0 ? COLORS.upText : COLORS.downText;
    ctx.globalAlpha = 0.9;
    ctx.fillText(fmtK(bar.totalVol), cx, yTop);
    ctx.globalAlpha = 1;
  }
}

/** 底部统计区（canvas 绘制，与柱像素级对齐；拖拽/缩放零 DOM 开销） */
function drawStats(
  ctx: CanvasRenderingContext2D,
  l: Layout,
  bars: FootprintBar[],
  hover: HoverInfo,
): void {
  const top = l.plotH + TIME_H;
  ctx.fillStyle = COLORS.panel;
  ctx.fillRect(0, top, l.width, STATS_H);
  ctx.strokeStyle = COLORS.border;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, top + 0.5);
  ctx.lineTo(l.width, top + 0.5);
  ctx.stroke();

  const showText = l.barW >= 34;

  ctx.save();
  ctx.beginPath();
  ctx.rect(0, top, l.chartW, STATS_H);
  ctx.clip();

  for (let i = l.visStart; i < l.visEnd; i++) {
    const bar = bars[i];
    const x = xOfBar(l, i);
    const dPct = bar.totalVol > 0 ? (bar.delta / bar.totalVol) * 100 : 0;
    const rows: { text: string; v: number; neutral?: boolean; strong?: boolean }[] = [
      { text: fmtK(bar.totalVol), v: 0, neutral: true },
      { text: fmtSigned(bar.delta), v: bar.delta, strong: Math.abs(dPct) >= 25 },
      { text: `${dPct >= 0 ? "+" : ""}${dPct.toFixed(0)}%`, v: dPct },
      { text: fmtK(bar.cumDelta), v: bar.cumDelta },
    ];
    for (let r = 0; r < rows.length; r++) {
      const y = top + r * STATS_ROW_H;
      const row = rows[r];
      if (!row.neutral) {
        const pos = row.v >= 0;
        const a = row.strong ? 0.28 : 0.12;
        ctx.fillStyle = pos ? `rgba(37,99,235,${a})` : `rgba(239,68,68,${a})`;
        ctx.fillRect(x, y, l.barW, STATS_ROW_H);
      }
      if (hover?.kind === "stats" && hover.barIndex === i && hover.row === r) {
        ctx.strokeStyle = "rgba(255,255,255,0.7)";
        ctx.strokeRect(x + 0.5, y + 0.5, l.barW - 1, STATS_ROW_H - 1);
      }
      if (showText) {
        ctx.font = `10px ${FONT_MONO}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = row.neutral
          ? COLORS.text
          : row.v >= 0
            ? COLORS.upText
            : COLORS.downText;
        ctx.fillText(row.text, x + l.barW / 2, y + STATS_ROW_H / 2);
      }
    }
    // 列分隔
    ctx.strokeStyle = "rgba(148,163,184,0.08)";
    ctx.beginPath();
    ctx.moveTo(Math.round(x + l.barW) + 0.5, top);
    ctx.lineTo(Math.round(x + l.barW) + 0.5, top + STATS_H);
    ctx.stroke();
  }
  ctx.restore();

  // 行分隔线 + 右侧行标签
  ctx.fillStyle = COLORS.panel;
  ctx.fillRect(l.chartW, top, l.width - l.chartW, STATS_H);
  ctx.strokeStyle = COLORS.border;
  ctx.beginPath();
  ctx.moveTo(l.chartW + 0.5, top);
  ctx.lineTo(l.chartW + 0.5, top + STATS_H);
  ctx.stroke();
  for (let r = 0; r < STATS_ROWS.length; r++) {
    const y = top + r * STATS_ROW_H;
    if (r > 0) {
      ctx.strokeStyle = "rgba(148,163,184,0.07)";
      ctx.beginPath();
      ctx.moveTo(0, y + 0.5);
      ctx.lineTo(l.width, y + 0.5);
      ctx.stroke();
    }
    ctx.font = `9px ${FONT_MONO}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = COLORS.dim;
    ctx.fillText(STATS_ROWS[r].label, l.chartW + 6, y + STATS_ROW_H / 2);
  }
}

/**
 * Volume Profile：图区左缘的横向价位量分布 + 价值区背景 + POC/VAH/VAL 线。
 * 在图区裁剪内调用（主图元素之下、徽标之上由调用方控制次序）。
 */
function drawVolumeProfile(
  ctx: CanvasRenderingContext2D,
  l: Layout,
  profile: VolumeProfile,
  splitColor: boolean,
): void {
  const maxW = l.chartW * PROFILE_MAX_W_RATIO;
  const rowPx = Math.max(1, Math.min(l.rowH - 1, 24));

  // 价值区背景（VAH-VAL 淡色带，横贯图区）
  const yVah = yOfPrice(l, profile.vah) - l.rowH / 2;
  const yVal = yOfPrice(l, profile.val) + l.rowH / 2;
  if (yVal > 0 && yVah < l.plotH) {
    ctx.fillStyle = COLORS.valueArea;
    ctx.fillRect(0, Math.max(0, yVah), l.chartW, Math.min(l.plotH, yVal) - Math.max(0, yVah));
    // VAH/VAL 边界虚线
    ctx.strokeStyle = COLORS.vaEdge;
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 3]);
    for (const p of [profile.vah, profile.val]) {
      const y = Math.round(yOfPrice(l, p)) + 0.5;
      if (y < 0 || y > l.plotH) continue;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(l.chartW, y);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  // 横向分布条（左缘向右，长度按 maxRowVol 归一）
  for (const row of profile.rows) {
    const yc = yOfPrice(l, row.price);
    const y = yc - rowPx / 2;
    if (y > l.plotH || y + rowPx < 0) continue;
    const w = (row.vol / profile.maxRowVol) * maxW;
    const inVa = row.price <= profile.vah && row.price >= profile.val;
    if (splitColor && row.vol > 0) {
      // 买卖分色：左段卖（红）右段买（蓝）
      const wBid = (row.bidVol / row.vol) * w;
      ctx.fillStyle = "rgba(239,68,68,0.34)";
      ctx.fillRect(0, y, wBid, rowPx - 0.5);
      ctx.fillStyle = "rgba(37,99,235,0.36)";
      ctx.fillRect(wBid, y, w - wBid, rowPx - 0.5);
    } else {
      ctx.fillStyle = inVa ? COLORS.profileBarInVa : COLORS.profileBar;
      ctx.fillRect(0, y, w, rowPx - 0.5);
    }
  }

  // POC 线：亮黄贯穿 + 左上标签
  const yPoc = Math.round(yOfPrice(l, profile.poc)) + 0.5;
  if (yPoc >= 0 && yPoc <= l.plotH) {
    ctx.strokeStyle = COLORS.poc;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(0, yPoc);
    ctx.lineTo(l.chartW, yPoc);
    ctx.stroke();
    ctx.font = `600 9px ${FONT_MONO}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillStyle = COLORS.poc;
    ctx.fillText(`POC ${fmtPrice(profile.poc, l.tick)}`, 4, yPoc - 2);
  }
}

const BADGE_GLYPH: Record<FpSignal["type"], string> = {
  sweep: "⚡",
  stack: "≣",
  divergence: "◈",
};

function badgeColor(sig: FpSignal): string {
  if (sig.type === "sweep") return COLORS.sweep;
  if (sig.type === "divergence") return COLORS.divergence;
  return sig.side === "buy" ? COLORS.up : COLORS.down;
}

/** 信号徽标：买方信号画在柱下方，卖方信号画在柱上方；返回命中盒供点击 */
function drawBadges(
  ctx: CanvasRenderingContext2D,
  l: Layout,
  bars: FootprintBar[],
  signals: FpSignal[],
): BadgeBox[] {
  if (l.mode === "candles" || signals.length === 0) return [];

  // 可见信号按强度取 Top-K，再按位置去重（同侧相邻 26px 内只留最强）
  const visible = signals.filter(
    (s) => s.barIndex >= l.visStart && s.barIndex < l.visEnd,
  );
  visible.sort((a, b) => b.strength - a.strength);
  const maxN = Math.max(3, Math.floor(l.chartW / 150));
  const kept: FpSignal[] = [];
  for (const s of visible) {
    if (kept.length >= maxN) break;
    const x = xOfBar(l, s.barIndex) + l.barW / 2;
    const clashing = kept.some(
      (k) =>
        k.side === s.side &&
        Math.abs(xOfBar(l, k.barIndex) + l.barW / 2 - x) < 26,
    );
    if (!clashing) kept.push(s);
  }

  const boxes: BadgeBox[] = [];
  const laneOffset = new Map<string, number>();
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, l.chartW, l.plotH);
  ctx.clip();

  for (const sig of kept) {
    const bar = bars[sig.barIndex];
    if (!bar) continue;
    const cx = xOfBar(l, sig.barIndex) + l.barW / 2;
    const laneKey = `${sig.barIndex}:${sig.side}`;
    const n = laneOffset.get(laneKey) ?? 0;
    laneOffset.set(laneKey, n + 1);
    const r = 9;
    const gap = 14 + n * (r * 2 + 4);
    const cy =
      sig.side === "buy"
        ? yOfPrice(l, bar.low) + l.rowH / 2 + gap + r
        : yOfPrice(l, bar.high) - l.rowH / 2 - gap - r;
    if (cy < -r || cy > l.plotH + r) continue;

    const color = badgeColor(sig);
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(7,22,34,0.9)";
    ctx.fill();
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = color;
    ctx.stroke();
    ctx.font = `10px ${FONT_MONO}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = color;
    ctx.fillText(BADGE_GLYPH[sig.type], cx, cy + 0.5);

    boxes.push({ x: cx, y: cy, r: r + 3, signal: sig });
  }
  ctx.restore();
  return boxes;
}

export function drawFrame(
  ctx: CanvasRenderingContext2D,
  l: Layout,
  bars: FootprintBar[],
  hover: HoverInfo,
  signals: FpSignal[],
  profile: VolumeProfile | null = null,
  profileSplitColor = false,
): BadgeBox[] {
  ctx.fillStyle = COLORS.bg;
  ctx.fillRect(0, 0, l.width, l.height);

  const n = bars.length;

  // 底部时间条与右侧价格轴的底板
  ctx.fillStyle = COLORS.panel;
  ctx.fillRect(0, l.plotH, l.width, l.height - l.plotH);
  ctx.fillRect(l.chartW, 0, l.width - l.chartW, l.plotH);

  if (n === 0) {
    ctx.fillStyle = COLORS.dim;
    ctx.font = `12px ${FONT_MONO}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("暂无数据", l.chartW / 2, l.plotH / 2);
    return [];
  }

  // 图区裁剪，防止格子/文字溢出到轴区
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, l.chartW, l.plotH);
  ctx.clip();

  const axisLabels = drawGrid(ctx, l);

  // Volume Profile 画在网格之上、足迹柱之下（半透明不遮主图）
  if (profile) drawVolumeProfile(ctx, l, profile, profileSplitColor);

  // 可见范围内最大单侧格量，用于颜色浓度归一
  let maxCell = 0;
  for (let i = l.visStart; i < l.visEnd; i++) {
    for (const lv of bars[i].levels) {
      if (lv.bidVol > maxCell) maxCell = lv.bidVol;
      if (lv.askVol > maxCell) maxCell = lv.askVol;
    }
  }
  if (maxCell <= 0) maxCell = 1;

  // 列分隔线
  if (l.barW >= 14 && l.mode !== "candles") {
    ctx.strokeStyle = COLORS.colSep;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = l.visStart; i <= l.visEnd; i++) {
      const x = Math.round(xOfBar(l, i)) + 0.5;
      ctx.moveTo(x, 0);
      ctx.lineTo(x, l.plotH);
    }
    ctx.stroke();
  }

  for (let i = l.visStart; i < l.visEnd; i++) {
    const bar = bars[i];
    const x = xOfBar(l, i);
    if (l.mode === "candles" || isOhlcOnly(bar)) {
      drawCandle(ctx, l, bar, x);
    } else {
      drawFootprintBar(ctx, l, bar, i, x, maxCell, hover);
    }
  }

  // 最新价虚线
  const last = bars[n - 1];
  const yLast = yOfPrice(l, last.close);
  if (yLast >= 0 && yLast <= l.plotH) {
    ctx.strokeStyle = "rgba(226,232,240,0.45)";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(0, Math.round(yLast) + 0.5);
    ctx.lineTo(l.chartW, Math.round(yLast) + 0.5);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.restore();

  // 信号徽标（在图区裁剪内绘制）
  const boxes = drawBadges(ctx, l, bars, signals);

  // 价格轴
  ctx.strokeStyle = COLORS.border;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(l.chartW + 0.5, 0);
  ctx.lineTo(l.chartW + 0.5, l.plotH);
  ctx.stroke();

  ctx.font = `10px ${FONT_MONO}`;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillStyle = COLORS.dim;
  for (const lb of axisLabels) {
    ctx.fillText(lb.text, l.chartW + 6, lb.y);
  }

  // 最新价标签
  if (yLast >= 8 && yLast <= l.plotH - 8) {
    const up = last.close >= last.open;
    ctx.fillStyle = up ? COLORS.up : COLORS.down;
    ctx.fillRect(l.chartW + 1, yLast - 9, l.width - l.chartW - 1, 18);
    ctx.fillStyle = "#f8fafc";
    ctx.font = `600 10px ${FONT_MONO}`;
    ctx.fillText(fmtPrice(last.close, l.tick), l.chartW + 6, yLast);
  }

  // 时间条
  ctx.strokeStyle = COLORS.border;
  ctx.beginPath();
  ctx.moveTo(0, l.plotH + 0.5);
  ctx.lineTo(l.width, l.plotH + 0.5);
  ctx.stroke();

  const m = Math.max(1, Math.ceil(56 / l.barW));
  ctx.font = `10px ${FONT_MONO}`;
  ctx.textAlign = "center";
  const tyMid = l.plotH + TIME_H / 2 + 1;
  for (let i = l.visStart; i < l.visEnd; i++) {
    const isLast = i === n - 1;
    if (i % m !== 0 && !(isLast && l.barW >= 34)) continue;
    const x = xOfBar(l, i) + l.barW / 2;
    if (x < 0 || x > l.chartW) continue;
    ctx.fillStyle = isLast ? COLORS.upText : COLORS.dim;
    ctx.fillText(fmtTime(bars[i].time), x, tyMid);
  }

  // 底部统计
  drawStats(ctx, l, bars, hover);

  return boxes;
}
