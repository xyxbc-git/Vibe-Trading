// PositionZonePrimitive — lightweight-charts series primitive，把单个信号的
// 多空点位画成 TradingView Long/Short Position 风格的区间图：
//   绿色半透明块 = 入场→止盈 收益区；红色半透明块 = 入场→止损 风险区
//   （多单收益区在入场上方，空单镜像）+ 入/损/盈三条边界线 + 价格标注
//   + 中央「多/空 · 盈亏比 1:x.x」徽标；区块从最新 K 线向右延伸固定根数。
//
// 独立图元：独立文件、独立实例、独立 zOrder（"top"，浮在蜡烛上方但高透明
// 不遮 K 线主体），与其它图层挂载/清除互不影响，缩放/平移由库的
// updateAllViews 回调驱动重投影。

import type {
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitivePaneRenderer,
  ISeriesPrimitivePaneView,
  SeriesPrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  SeriesType,
  Logical,
  Time,
} from "lightweight-charts";
import type { PositionZoneView } from "@/lib/positionZone";

type RenderTarget = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type AttachedChart = SeriesAttachedParameter<Time, SeriesType>["chart"];

/** 项目暗色主题色（与 tailwind jarvis-green/red 对齐） */
const COLOR_PROFIT = "#3fb950";
const COLOR_RISK = "#f85149";
const COLOR_ENTRY = "#c9d1d9";
const FILL_ALPHA = 0.14;
const EDGE_ALPHA = 0.75;

interface ZoneLayout {
  xLeft: number;
  xRight: number;
  yEntry: number;
  yStop: number;
  yTake: number;
}

function rgba(hex: string, alpha: number): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

function fmtPrice(v: number): string {
  return v.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

class PositionZoneRenderer implements ISeriesPrimitivePaneRenderer {
  private readonly _layout: ZoneLayout;
  private readonly _view: PositionZoneView;

  constructor(layout: ZoneLayout, view: PositionZoneView) {
    this._layout = layout;
    this._view = view;
  }

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const { width, height } = mediaSize;
      const L = this._layout;
      const v = this._view;
      if (L.xRight < 0 || L.xLeft > width) return;

      const drawBlock = (y1: number, y2: number, color: string) => {
        const top = Math.min(y1, y2);
        const h = Math.abs(y2 - y1);
        if (h <= 0 || top > height || top + h < 0) return;
        ctx.fillStyle = rgba(color, FILL_ALPHA);
        ctx.fillRect(L.xLeft, top, L.xRight - L.xLeft, h);
      };
      // 收益块（入场→止盈）与风险块（入场→止损），多空由 y 坐标天然镜像
      drawBlock(L.yEntry, L.yTake, COLOR_PROFIT);
      drawBlock(L.yEntry, L.yStop, COLOR_RISK);

      // 三条边界水平线：止盈实线绿 / 止损实线红 / 入场虚线灰白
      const drawEdge = (y: number, color: string, dashed: boolean) => {
        if (y < 0 || y > height) return;
        ctx.strokeStyle = rgba(color, EDGE_ALPHA);
        ctx.lineWidth = 1;
        ctx.setLineDash(dashed ? [4, 3] : []);
        ctx.beginPath();
        ctx.moveTo(L.xLeft, y);
        ctx.lineTo(L.xRight, y);
        ctx.stroke();
        ctx.setLineDash([]);
      };
      drawEdge(L.yTake, COLOR_PROFIT, false);
      drawEdge(L.yStop, COLOR_RISK, false);
      drawEdge(L.yEntry, COLOR_ENTRY, true);

      // 边界价格标注（区块内侧左对齐，贴线避让）
      ctx.font = "10px -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif";
      const labelX = L.xLeft + 6;
      const drawLabel = (
        text: string,
        y: number,
        color: string,
        above: boolean,
      ) => {
        if (y < -14 || y > height + 14) return;
        ctx.textBaseline = above ? "bottom" : "top";
        ctx.fillStyle = rgba(color, 0.95);
        ctx.fillText(text, labelX, above ? y - 3 : y + 3);
      };
      const long = v.side === "long";
      // 止盈标注写在收益块内、止损标注写在风险块内（多空自动换边）
      drawLabel(`止盈 ${fmtPrice(v.takeProfit)}`, L.yTake, COLOR_PROFIT, !long);
      drawLabel(`止损 ${fmtPrice(v.stopLoss)}`, L.yStop, COLOR_RISK, long);
      drawLabel(`入场 ${fmtPrice(v.entry)}`, L.yEntry, COLOR_ENTRY, long);

      // 中央徽标：方向 + 盈亏比（画在收益块中心，块太矮则跳过）
      const profitMid = (L.yEntry + L.yTake) / 2;
      if (
        Math.abs(L.yTake - L.yEntry) >= 26 &&
        profitMid >= 8 &&
        profitMid <= height - 8
      ) {
        const badge = `${long ? "多" : "空"} · 盈亏比 1:${v.rr}`;
        ctx.font =
          "600 11px -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif";
        const tw = ctx.measureText(badge).width;
        const bx = (L.xLeft + Math.min(L.xRight, width)) / 2 - tw / 2;
        ctx.fillStyle = "rgba(13,17,23,0.72)";
        const pad = 5;
        ctx.fillRect(bx - pad, profitMid - 9, tw + pad * 2, 18);
        ctx.textBaseline = "middle";
        ctx.fillStyle = long ? COLOR_PROFIT : COLOR_RISK;
        ctx.fillText(badge, bx, profitMid);
      }
    });
  }
}

class PositionZonePaneView implements ISeriesPrimitivePaneView {
  private readonly _source: PositionZonePrimitive;

  constructor(source: PositionZonePrimitive) {
    this._source = source;
  }

  zOrder(): SeriesPrimitivePaneViewZOrder {
    // 高于蜡烛：区间图是用户主动点开的焦点内容；填充透明度低不遮 K 线主体
    return "top";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const layout = this._source.layout();
    const view = this._source.view();
    return layout && view ? new PositionZoneRenderer(layout, view) : null;
  }
}

export class PositionZonePrimitive implements ISeriesPrimitive<Time> {
  private _view: PositionZoneView | null = null;
  private _window: { from: number; to: number } | null = null;
  private _layout: ZoneLayout | null = null;
  private readonly _views: readonly ISeriesPrimitivePaneView[];
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _chart: AttachedChart | null = null;
  private _requestUpdate: (() => void) | null = null;

  constructor() {
    this._views = [new PositionZonePaneView(this)];
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

  /** 设置/替换当前区间（同时只显示一个）；view 或 window 传 null 即清除。 */
  setZone(
    view: PositionZoneView | null,
    window: { from: number; to: number } | null,
  ): void {
    this._view = view;
    this._window = window;
    this._requestUpdate?.();
  }

  view(): PositionZoneView | null {
    return this._view;
  }

  layout(): ZoneLayout | null {
    return this._layout;
  }

  // 每次视口变化（缩放/平移/新数据/resize）由库回调：三价投影成 y、
  // bar 逻辑索引窗口投影成 x（右边界越过最新 K 线按 bar 间距外推）。
  updateAllViews(): void {
    this._layout = null;
    const series = this._series;
    const chart = this._chart;
    const view = this._view;
    const win = this._window;
    if (!series || !chart || !view || !win) return;
    const timeScale = chart.timeScale();
    const x1 = timeScale.logicalToCoordinate(win.from as Logical);
    const x2 = timeScale.logicalToCoordinate(win.to as Logical);
    if (x1 === null || x2 === null) return;
    const yEntry = series.priceToCoordinate(view.entry);
    const yStop = series.priceToCoordinate(view.stopLoss);
    const yTake = series.priceToCoordinate(view.takeProfit);
    if (yEntry === null || yStop === null || yTake === null) return;
    this._layout = {
      xLeft: Math.min(x1, x2),
      xRight: Math.max(x1, x2),
      yEntry,
      yStop,
      yTake,
    };
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._views;
  }
}
