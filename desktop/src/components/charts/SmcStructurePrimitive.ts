// SmcStructurePrimitive — lightweight-charts series primitive（R8 追加）：
// SMC 市场结构事件（BOS 结构突破 / CHoCH 结构转换）线段叠加层。
//
// 每个事件画一条素雅细水平线：从被突破的 swing 点延伸到突破那根蜡烛，
// 突破端挂小字标签「BOS」/「CHoCH」（看涨绿 / 看跌红 / CHoCH 琥珀色）。
// zOrder "top" 但线宽 1px 小字 9px，不构成视觉负担；与 FVG 矩形带共存。
// hover 命中 eventAt(x,y)（线段 ±4px 容差）由 KlineChart crosshair 管线出摘要。

import type {
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitivePaneRenderer,
  ISeriesPrimitivePaneView,
  SeriesPrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";

type RenderTarget = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type AttachedChart = SeriesAttachedParameter<Time, SeriesType>["chart"];

/** Chart 层组装好的展示单元 */
export interface StructureEventView {
  kind: "bos" | "choch";
  direction: "bullish" | "bearish";
  /** 被突破的 swing 价位（线的 y） */
  level: number;
  /** 被突破 swing 点 bar 开盘时间（epoch 秒） */
  swingTimeSec: number;
  /** 突破 bar 开盘时间（epoch 秒） */
  breakTimeSec: number;
  tooltip: string;
}

const COLOR_BULL = "#3fb950";
const COLOR_BEAR = "#f85149";
const COLOR_CHOCH = "#d29922"; // 琥珀：结构转换警示（与视频流派配色一致）
const LINE_ALPHA = 0.65;
const HIT_TOLERANCE_PX = 4;

interface EventLayout {
  x1: number;
  x2: number;
  y: number;
  view: StructureEventView;
}

function rgba(hex: string, alpha: number): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

function colorOf(v: StructureEventView): string {
  if (v.kind === "choch") return COLOR_CHOCH;
  return v.direction === "bullish" ? COLOR_BULL : COLOR_BEAR;
}

class SmcRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(private readonly _layouts: readonly EventLayout[]) {}

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const { width, height } = mediaSize;
      ctx.font = "600 9px -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif";
      for (const L of this._layouts) {
        if (L.y < -10 || L.y > height + 10 || L.x2 < 0 || L.x1 > width) continue;
        const color = colorOf(L.view);
        // 素雅细线：swing 点 → 突破蜡烛
        ctx.strokeStyle = rgba(color, LINE_ALPHA);
        ctx.lineWidth = 1;
        ctx.setLineDash(L.view.kind === "choch" ? [4, 3] : []);
        ctx.beginPath();
        ctx.moveTo(Math.max(L.x1, 0), L.y + 0.5);
        ctx.lineTo(Math.min(L.x2, width), L.y + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        // 突破端小字标签（贴线，看涨在上方 / 看跌在下方避让蜡烛）
        const label = L.view.kind === "bos" ? "BOS" : "CHoCH";
        const lx = Math.min(L.x2 + 3, width - 34);
        ctx.fillStyle = rgba(color, 0.95);
        ctx.textBaseline = L.view.direction === "bullish" ? "bottom" : "top";
        ctx.fillText(label, lx, L.view.direction === "bullish" ? L.y - 2 : L.y + 2);
      }
    });
  }
}

class SmcPaneView implements ISeriesPrimitivePaneView {
  constructor(private readonly _source: SmcStructurePrimitive) {}

  zOrder(): SeriesPrimitivePaneViewZOrder {
    return "top";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const layouts = this._source.layouts();
    return layouts.length > 0 ? new SmcRenderer(layouts) : null;
  }
}

export class SmcStructurePrimitive implements ISeriesPrimitive<Time> {
  private _events: StructureEventView[] = [];
  private _layouts: EventLayout[] = [];
  private readonly _views: readonly ISeriesPrimitivePaneView[];
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _chart: AttachedChart | null = null;
  private _requestUpdate: (() => void) | null = null;

  constructor() {
    this._views = [new SmcPaneView(this)];
  }

  attached(param: SeriesAttachedParameter<Time, SeriesType>): void {
    this._series = param.series;
    this._chart = param.chart;
    this._requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this._series = null;
    this._chart = null;
    this._requestUpdate = null;
  }

  /** 替换当前事件集（空数组即清除） */
  setEvents(events: StructureEventView[]): void {
    this._events = events;
    this._requestUpdate?.();
  }

  layouts(): readonly EventLayout[] {
    return this._layouts;
  }

  /** hover 命中：线段 ±4px 容差，返回 tooltip 文本 */
  eventAt(x: number, y: number): string | null {
    for (const L of this._layouts) {
      if (
        Math.abs(y - L.y) <= HIT_TOLERANCE_PX &&
        x >= L.x1 - HIT_TOLERANCE_PX &&
        x <= L.x2 + 30 // 覆盖标签区
      ) {
        return L.view.tooltip;
      }
    }
    return null;
  }

  updateAllViews(): void {
    this._layouts = [];
    const series = this._series;
    const chart = this._chart;
    if (!series || !chart || this._events.length === 0) return;
    const timeScale = chart.timeScale();
    for (const e of this._events) {
      const x1 = timeScale.timeToCoordinate(e.swingTimeSec as Time);
      const x2 = timeScale.timeToCoordinate(e.breakTimeSec as Time);
      const y = series.priceToCoordinate(e.level);
      if (x2 === null || y === null) continue; // 突破 bar 不在已加载数据内则跳过
      this._layouts.push({ x1: x1 ?? 0, x2, y, view: e });
    }
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._views;
  }
}
