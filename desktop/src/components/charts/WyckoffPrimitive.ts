// WyckoffPrimitive — lightweight-charts series primitive，威科夫阶段引擎的
// 图上渲染通道（与 TrapSignalsPrimitive 同型结构，复用 marker 通道机制）：
//   阶段带  bottom 层：交易区间 [start_ts→now] × [low, high] 半透明矩形，
//           acc 绿 / dist 红，透明度按 Phase A-E 深浅（wyckoff.ts 已折算进
//           fill 色），上下边缘细线 + 带内左上角标签
//   事件徽章 top 层：12 事件的缩写牌（SC/AR/ST/SPR/TST/SOS/LPS/BC/UT/UTAD/
//           SOW/LPSY），低点事件挂锚定 bar 低点下方、高点事件挂高点上方，
//           填充透明度随置信度加深，画在影线之外不遮 K 线本体
// 缩放/平移由 updateAllViews() 重投影（time→x、price→y），跟手不错位；
// 悬停命中经 markAt(x,y) 供 KlineChart 的 crosshair 浮层查询。

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
import type { WyckoffMark, WyckoffOverlay } from "@/lib/wyckoff";

type RenderTarget = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type AttachedChart = SeriesAttachedParameter<Time, SeriesType>["chart"];

/** 徽章与影线端点的间距（px，媒体坐标） */
const BADGE_GAP = 5;
/** 命中测试外扩（px） */
const HIT_PAD = 3;
/** 徽章高度（px） */
const BADGE_H = 14;

/** 阶段带投影结果（像素坐标矩形） */
interface BandBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  fill: string;
  edge: string;
  label: string;
}

/** 事件徽章投影结果（bounding box 命中测试用） */
interface MarkBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  mark: WyckoffMark;
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

// #rrggbb → #rrggbbaa（置信度映射徽章填充透明度）
function withAlpha(color: string, alpha: number): string {
  const a = clamp01(alpha);
  return /^#[0-9a-fA-F]{6}$/.test(color)
    ? color + Math.round(a * 255).toString(16).padStart(2, "0")
    : color;
}

/** bottom 层：阶段背景带（垫在蜡烛下，不遮 K 线与其它叠加） */
class WyckoffBandRenderer implements ISeriesPrimitivePaneRenderer {
  private readonly _band: BandBox;

  constructor(band: BandBox) {
    this._band = band;
  }

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const b = this._band;
      if (b.x1 <= 0 || b.x0 >= mediaSize.width) return;
      const w = b.x1 - b.x0;
      const h = b.y1 - b.y0;
      if (w <= 0 || h <= 0) return;

      ctx.fillStyle = b.fill;
      ctx.fillRect(b.x0, b.y0, w, h);

      // 上下边缘细线（区间高/低位一目了然）
      ctx.strokeStyle = b.edge;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(b.x0, b.y0);
      ctx.lineTo(b.x1, b.y0);
      ctx.moveTo(b.x0, b.y1);
      ctx.lineTo(b.x1, b.y1);
      ctx.stroke();

      // 带内左上角标签（带太窄不画，避免文字溢出）
      if (w > 90) {
        ctx.fillStyle = b.edge;
        ctx.font =
          "11px -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(b.label, Math.max(b.x0, 0) + 6, b.y0 + 4);
        ctx.textBaseline = "alphabetic";
      }
    });
  }
}

/** top 层：事件缩写徽章 */
class WyckoffMarksRenderer implements ISeriesPrimitivePaneRenderer {
  private readonly _boxes: MarkBox[];

  constructor(boxes: MarkBox[]) {
    this._boxes = boxes;
  }

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const W = mediaSize.width;
      const H = mediaSize.height;
      ctx.font =
        "bold 9px -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";

      for (const b of this._boxes) {
        if (b.x1 < 0 || b.x0 > W || b.y1 < 0 || b.y0 > H) continue;
        const conf = clamp01(Number(b.mark.event.confidence ?? 0));
        const w = b.x1 - b.x0;
        const h = b.y1 - b.y0;
        const r = 3;

        // 圆角矩形牌面：底色随事件侧（吸筹绿/派发红），透明度随置信度加深
        ctx.beginPath();
        ctx.moveTo(b.x0 + r, b.y0);
        ctx.lineTo(b.x1 - r, b.y0);
        ctx.arcTo(b.x1, b.y0, b.x1, b.y0 + r, r);
        ctx.lineTo(b.x1, b.y1 - r);
        ctx.arcTo(b.x1, b.y1, b.x1 - r, b.y1, r);
        ctx.lineTo(b.x0 + r, b.y1);
        ctx.arcTo(b.x0, b.y1, b.x0, b.y1 - r, r);
        ctx.lineTo(b.x0, b.y0 + r);
        ctx.arcTo(b.x0, b.y0, b.x0 + r, b.y0, r);
        ctx.closePath();
        ctx.fillStyle = withAlpha(b.mark.color, 0.55 + 0.45 * conf);
        ctx.fill();
        ctx.strokeStyle = "#0d1117";
        ctx.lineWidth = 1;
        ctx.stroke();

        ctx.fillStyle = "#ffffff";
        ctx.fillText(b.mark.abbr, b.x0 + w / 2, b.y0 + h / 2 + 0.5);
      }
      ctx.textAlign = "start";
      ctx.textBaseline = "alphabetic";
    });
  }
}

class WyckoffBandPaneView implements ISeriesPrimitivePaneView {
  private readonly _source: WyckoffPrimitive;

  constructor(source: WyckoffPrimitive) {
    this._source = source;
  }

  zOrder(): SeriesPrimitivePaneViewZOrder {
    return "bottom";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const band = this._source.bandBox();
    return band ? new WyckoffBandRenderer(band) : null;
  }
}

class WyckoffMarksPaneView implements ISeriesPrimitivePaneView {
  private readonly _source: WyckoffPrimitive;

  constructor(source: WyckoffPrimitive) {
    this._source = source;
  }

  zOrder(): SeriesPrimitivePaneViewZOrder {
    return "top";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const boxes = this._source.markBoxes();
    return boxes.length > 0 ? new WyckoffMarksRenderer(boxes) : null;
  }
}

export class WyckoffPrimitive implements ISeriesPrimitive<Time> {
  private _overlay: WyckoffOverlay | null = null;
  private _bandBox: BandBox | null = null;
  private _markBoxes: MarkBox[] = [];
  private readonly _views: readonly ISeriesPrimitivePaneView[];
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _chart: AttachedChart | null = null;
  private _requestUpdate: (() => void) | null = null;

  constructor() {
    this._views = [new WyckoffBandPaneView(this), new WyckoffMarksPaneView(this)];
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

  setOverlay(overlay: WyckoffOverlay | null): void {
    this._overlay = overlay;
    this._requestUpdate?.();
  }

  bandBox(): BandBox | null {
    return this._bandBox;
  }

  markBoxes(): MarkBox[] {
    return this._markBoxes;
  }

  /** 面板像素坐标命中测试（徽章 bounding box + 外扩），供悬停浮层。 */
  markAt(x: number, y: number): WyckoffMark | null {
    for (const b of this._markBoxes) {
      if (
        x >= b.x0 - HIT_PAD &&
        x <= b.x1 + HIT_PAD &&
        y >= b.y0 - HIT_PAD &&
        y <= b.y1 + HIT_PAD
      ) {
        return b.mark;
      }
    }
    return null;
  }

  // 每次视口变化（缩放/平移/新数据/resize）重投影：time→x、price→y。
  updateAllViews(): void {
    this._bandBox = null;
    this._markBoxes = [];
    const series = this._series;
    const chart = this._chart;
    const overlay = this._overlay;
    if (!series || !chart || !overlay) return;
    const timeScale = chart.timeScale();

    let barSpacing = 8;
    try {
      const opts = timeScale.options() as { barSpacing?: number };
      if (typeof opts.barSpacing === "number" && opts.barSpacing > 0) {
        barSpacing = opts.barSpacing;
      }
    } catch {
      // options 不可读时用默认尺寸
    }

    // ── 阶段带：端点被平移出可见范围时 timeToCoordinate 返 null，
    // 夹到可见数据范围（getVisibleRange 的边缘就是数据时间）再投影
    const band = overlay.band;
    if (band) {
      let fromSec = band.fromSec;
      let toSec = band.toSec;
      try {
        const vis = timeScale.getVisibleRange();
        if (vis) {
          fromSec = Math.max(fromSec, Number(vis.from));
          toSec = Math.min(toSec, Number(vis.to));
        }
      } catch {
        // getVisibleRange 不可用时按原端点尝试
      }
      if (fromSec <= toSec) {
        const x0 = timeScale.timeToCoordinate(fromSec as Time);
        const x1 = timeScale.timeToCoordinate(toSec as Time);
        const yTop = series.priceToCoordinate(band.high);
        const yBot = series.priceToCoordinate(band.low);
        if (x0 !== null && x1 !== null && yTop !== null && yBot !== null) {
          this._bandBox = {
            x0: x0 - barSpacing / 2,
            // 右缘补半根 bar 宽，盖满最新一根蜡烛
            x1: x1 + barSpacing / 2,
            y0: Math.min(yTop, yBot),
            y1: Math.max(yTop, yBot),
            fill: band.fill,
            edge: band.edge,
            label: band.label,
          };
        }
      }
    }

    // ── 事件徽章：宽度随缩写长度，位置挂影线之外
    for (const m of overlay.marks) {
      const x = timeScale.timeToCoordinate(m.timeSec as Time);
      if (x === null) continue;
      const w = Math.max(18, m.abbr.length * 7 + 6);
      if (m.position === "above") {
        const yHigh = series.priceToCoordinate(m.anchorHigh);
        if (yHigh === null) continue;
        const y1 = yHigh - BADGE_GAP;
        this._markBoxes.push({
          x0: x - w / 2,
          y0: y1 - BADGE_H,
          x1: x + w / 2,
          y1,
          mark: m,
        });
      } else {
        const yLow = series.priceToCoordinate(m.anchorLow);
        if (yLow === null) continue;
        const y0 = yLow + BADGE_GAP;
        this._markBoxes.push({
          x0: x - w / 2,
          y0,
          x1: x + w / 2,
          y1: y0 + BADGE_H,
          mark: m,
        });
      }
    }
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._views;
  }
}
