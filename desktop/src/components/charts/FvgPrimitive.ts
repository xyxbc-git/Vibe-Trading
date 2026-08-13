// FvgPrimitive — lightweight-charts series primitive（R8）：FVG（Fair Value
// Gap，三根 K 线价格失衡缺口）矩形带叠加层。
//
// 每个未回补/部分回补的缺口画成半透明色带：看涨 FVG 绿色、看跌红色，从形成
// 那根蜡烛（created_ts 时间锚点，历史前插不漂移）向右延伸到图右缘；部分回补
// 的在带内叠一层更淡的「已回补」覆盖并在左缘标注回补百分比。zOrder "normal"
// 垫在蜡烛下方（区域背景语义，不遮 K 线主体），与支撑压力/趋势线/区间图等
// 现有叠加层共存互不影响。hover 命中走 zoneAt(x,y)，由 KlineChart 的
// crosshair 管线统一展示 tooltip。

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

/** Chart 层组装好的展示单元（tooltip 文案在组装侧生成） */
export interface FvgZoneView {
  type: "bullish" | "bearish";
  top: number;
  bottom: number;
  /** 形成 bar 开盘时间（epoch 秒，与蜡烛 time 同源对齐） */
  timeSec: number;
  /** 最深回踩占缺口高度 0~100 */
  fillPct: number;
  tooltip: string;
}

const COLOR_BULL = "#3fb950";
const COLOR_BEAR = "#f85149";
const FILL_ALPHA = 0.1;
const EDGE_ALPHA = 0.4;
const FILLED_OVERLAY_ALPHA = 0.1; // 已回补部分的「冲淡」覆盖

interface ZoneLayout {
  xLeft: number;
  yTop: number;
  yBottom: number;
  view: FvgZoneView;
}

function rgba(hex: string, alpha: number): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

class FvgRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(private readonly _layouts: readonly ZoneLayout[]) {}

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const { width, height } = mediaSize;
      ctx.font = "9px -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif";
      for (const L of this._layouts) {
        if (L.xLeft > width) continue;
        const top = Math.min(L.yTop, L.yBottom);
        const h = Math.abs(L.yBottom - L.yTop);
        if (h <= 0 || top > height || top + h < 0) continue;
        const color = L.view.type === "bullish" ? COLOR_BULL : COLOR_BEAR;
        const x = Math.max(L.xLeft, 0);
        const w = width - x; // 延伸到图右缘（未回补缺口持续有效）

        ctx.fillStyle = rgba(color, FILL_ALPHA);
        ctx.fillRect(x, top, w, h);
        // 上下边界细线（辨识缺口精确价位）
        ctx.strokeStyle = rgba(color, EDGE_ALPHA);
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(x, top + 0.5);
        ctx.lineTo(width, top + 0.5);
        ctx.moveTo(x, top + h - 0.5);
        ctx.lineTo(width, top + h - 0.5);
        ctx.stroke();
        ctx.setLineDash([]);

        // 部分回补：从「被回踩的一侧」叠深色覆盖表达已消耗比例
        // （看涨缺口自上而下被回补，看跌自下而上）
        const fillFrac = Math.max(0, Math.min(1, L.view.fillPct / 100));
        if (fillFrac > 0) {
          const fh = h * fillFrac;
          const fy = L.view.type === "bullish" ? top : top + h - fh;
          ctx.fillStyle = `rgba(13,17,23,${FILLED_OVERLAY_ALPHA + fillFrac * 0.12})`;
          ctx.fillRect(x, fy, w, fh);
        }

        // 左缘标签：方向 + 回补百分比（带太矮时跳过）
        if (h >= 12 && x + 4 < width) {
          ctx.fillStyle = rgba(color, 0.9);
          ctx.textBaseline = "middle";
          const label =
            (L.view.type === "bullish" ? "FVG↑" : "FVG↓") +
            (L.view.fillPct > 0 ? ` ${Math.round(L.view.fillPct)}%回补` : "");
          ctx.fillText(label, x + 4, top + h / 2);
        }
      }
    });
  }
}

class FvgPaneView implements ISeriesPrimitivePaneView {
  constructor(private readonly _source: FvgPrimitive) {}

  zOrder(): SeriesPrimitivePaneViewZOrder {
    // 垫在蜡烛下：FVG 是区域背景语义，不该遮住 K 线与其它焦点叠加层
    return "normal";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const layouts = this._source.layouts();
    return layouts.length > 0 ? new FvgRenderer(layouts) : null;
  }
}

export class FvgPrimitive implements ISeriesPrimitive<Time> {
  private _zones: FvgZoneView[] = [];
  private _layouts: ZoneLayout[] = [];
  private readonly _views: readonly ISeriesPrimitivePaneView[];
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _chart: AttachedChart | null = null;
  private _requestUpdate: (() => void) | null = null;

  constructor() {
    this._views = [new FvgPaneView(this)];
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

  /** 替换当前 zone 集（空数组即清除） */
  setZones(zones: FvgZoneView[]): void {
    this._zones = zones;
    this._requestUpdate?.();
  }

  layouts(): readonly ZoneLayout[] {
    return this._layouts;
  }

  /** hover 命中：返回命中 zone 的 tooltip（多带重叠取最新形成的=数组靠前者） */
  zoneAt(x: number, y: number): string | null {
    for (const L of this._layouts) {
      const top = Math.min(L.yTop, L.yBottom);
      const bottom = Math.max(L.yTop, L.yBottom);
      if (x >= L.xLeft && y >= top && y <= bottom) return L.view.tooltip;
    }
    return null;
  }

  // 缩放/平移/新数据由库回调重投影：time → x（时间锚点），price → y
  updateAllViews(): void {
    this._layouts = [];
    const series = this._series;
    const chart = this._chart;
    if (!series || !chart || this._zones.length === 0) return;
    const timeScale = chart.timeScale();
    for (const z of this._zones) {
      const x = timeScale.timeToCoordinate(z.timeSec as Time);
      const yTop = series.priceToCoordinate(z.top);
      const yBottom = series.priceToCoordinate(z.bottom);
      // 形成 bar 已滚出已加载数据左缘时 x=null：带从图左缘起画（缺口仍有效）
      if (yTop === null || yBottom === null) continue;
      this._layouts.push({ xLeft: x ?? 0, yTop, yBottom, view: z });
    }
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._views;
  }
}
