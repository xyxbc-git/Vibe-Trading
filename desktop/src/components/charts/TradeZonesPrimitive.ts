// TradeZonesPrimitive — lightweight-charts series primitive，把共识交易计划
// 画成时间锚定的半透明矩形区域（入场 / 止损 / 止盈），supply/demand zone 画法：
// 只覆盖区间对应的 K 线区段（最近 N 根 + 少量向右延伸），不再全屏铺满。
//
// 渲染层次：zOrder "bottom"，永远垫在蜡烛图层下方，不遮挡 K 线。
// 自适配：纵向锚定价格（series.priceToCoordinate），横向锚定 bar 逻辑索引
// （timeScale().logicalToCoordinate，逻辑索引可越过最新 K 线，实现右侧延伸）。
// 缩放 / 平移 / 切换周期时 updateAllViews() 在每次视口变化时重新投影两个维度，
// 矩形始终跟随对应的 K 线区段，不存在错位问题。

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
import type { TradeZone, ZoneTimeWindow } from "@/lib/tradeZones";

// fancy-canvas 的 CanvasRenderingTarget2D，从库类型反推，避免直接依赖传递包
type RenderTarget = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
// attached() 回调提供的 chart 句柄类型（IChartApiBase<Time>），同样从库类型反推
type AttachedChart = SeriesAttachedParameter<Time, SeriesType>["chart"];

interface ZoneBox {
  xLeft: number;
  xRight: number;
  yTop: number;
  yBottom: number;
  zone: TradeZone;
}

function rgba(hex: string, alpha: number): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

class TradeZonesRenderer implements ISeriesPrimitivePaneRenderer {
  private readonly _boxes: ZoneBox[];

  constructor(boxes: ZoneBox[]) {
    this._boxes = boxes;
  }

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const width = mediaSize.width;
      const height = mediaSize.height;
      for (const b of this._boxes) {
        const top = Math.min(b.yTop, b.yBottom);
        const bottom = Math.max(b.yTop, b.yBottom);
        if (bottom < 0 || top > height || bottom - top <= 0) continue;
        if (b.xRight < 0 || b.xLeft > width || b.xRight - b.xLeft <= 0) continue;

        const w = b.xRight - b.xLeft;
        const h = bottom - top;
        ctx.fillStyle = rgba(b.zone.color, b.zone.fillAlpha);
        ctx.fillRect(b.xLeft, top, w, h);

        // 矩形描边：透明度略高于填充，低透明填充下区域边界依然可读
        if (w >= 2 && h >= 2) {
          ctx.strokeStyle = rgba(b.zone.color, Math.min(1, b.zone.fillAlpha * 4));
          ctx.lineWidth = 1;
          ctx.strokeRect(b.xLeft + 0.5, top + 0.5, w - 1, h - 1);
        }

        // 标签（区域名 + 价格范围）贴在矩形可见部分的左上角；
        // 可见区域太矮 / 太窄时不画，避免文字溢出矩形
        const visTop = Math.max(top, 0);
        const visBottom = Math.min(bottom, height);
        const visLeft = Math.max(b.xLeft, 0);
        const visRight = Math.min(b.xRight, width);
        if (visBottom - visTop >= 14 && visRight - visLeft >= 60 && visTop <= height - 14) {
          ctx.font = "10px -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif";
          ctx.textBaseline = "top";
          ctx.fillStyle = b.zone.color;
          ctx.fillText(b.zone.label, visLeft + 6, visTop + 3);
        }
      }
    });
  }
}

class TradeZonesPaneView implements ISeriesPrimitivePaneView {
  private readonly _source: TradeZonesPrimitive;

  constructor(source: TradeZonesPrimitive) {
    this._source = source;
  }

  zOrder(): SeriesPrimitivePaneViewZOrder {
    return "bottom";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const boxes = this._source.boxes();
    return boxes.length > 0 ? new TradeZonesRenderer(boxes) : null;
  }
}

export class TradeZonesPrimitive implements ISeriesPrimitive<Time> {
  private _zones: TradeZone[] = [];
  private _window: ZoneTimeWindow | null = null;
  private _boxes: ZoneBox[] = [];
  private readonly _views: readonly ISeriesPrimitivePaneView[];
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _chart: AttachedChart | null = null;
  private _requestUpdate: (() => void) | null = null;

  constructor() {
    this._views = [new TradeZonesPaneView(this)];
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

  /** 更新区间集合与时间窗口；window 为 null（无 K 线）时不绘制任何矩形。 */
  setZones(zones: TradeZone[], window: ZoneTimeWindow | null): void {
    this._zones = zones;
    this._window = window;
    this._requestUpdate?.();
  }

  boxes(): ZoneBox[] {
    return this._boxes;
  }

  // 命名避开 ISeriesPrimitive 保留的 hitTest(x,y) 接口（签名要求返回
  // PrimitiveHoveredItem，与本用途不符）
  /** 面板像素坐标命中测试；多带重叠时取列表中最后命中的（画在最上层）。 */
  zoneAt(x: number, y: number): TradeZone | null {
    let hit: TradeZone | null = null;
    for (const b of this._boxes) {
      const top = Math.min(b.yTop, b.yBottom);
      const bottom = Math.max(b.yTop, b.yBottom);
      if (x >= b.xLeft && x <= b.xRight && y >= top && y <= bottom) hit = b.zone;
    }
    return hit;
  }

  // 库在每次视口变化（缩放/平移/新数据/resize）时回调：价格边界投影成 y，
  // bar 逻辑索引窗口投影成 x（右边界越过最新 K 线时按 bar 间距外推），
  // 渲染器只消费投影结果，保证跟手不错位。
  updateAllViews(): void {
    this._boxes = [];
    const series = this._series;
    const chart = this._chart;
    const win = this._window;
    if (!series || !chart || !win) return;
    const timeScale = chart.timeScale();
    const x1 = timeScale.logicalToCoordinate(win.from as Logical);
    const x2 = timeScale.logicalToCoordinate(win.to as Logical);
    if (x1 === null || x2 === null) return;
    const xLeft = Math.min(x1, x2);
    const xRight = Math.max(x1, x2);
    for (const z of this._zones) {
      const yTop = series.priceToCoordinate(z.top);
      const yBottom = series.priceToCoordinate(z.bottom);
      if (yTop === null || yBottom === null) continue;
      this._boxes.push({ xLeft, xRight, yTop, yBottom, zone: z });
    }
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._views;
  }
}
