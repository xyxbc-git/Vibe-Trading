// TrapSignalsPrimitive — lightweight-charts series primitive，把诱多/诱空
// 陷阱信号画成三角警示牌（内置 series markers 已被信号结构买卖点占用，且
// 画不出「三角牌 + 感叹号 + 置信度透明度」的组合，故走 primitive 自绘通道）：
//   诱多  红色倒三角 ▽（假涨警示，别追多），尖端指向信号 K 线高点上方
//   诱空  绿色正三角 △（假跌警示，别追空），尖端指向信号 K 线低点下方
//   牌面  白色粗体「!」，填充透明度随置信度加深（低置信淡、高置信实）
// 尺寸随 barSpacing 自适应，缩放/平移由 updateAllViews() 重投影（time→x、
// price→y），跟手不错位；标记画在影线之外，不遮挡 K 线本体。
// 悬停/点击命中经 markAt(x,y) 供 KlineChart 的 crosshair 浮层与 click 查询。

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
import type { TrapMark } from "@/lib/trapSignals";
import { TRAP_COLORS } from "@/lib/trapSignals";

type RenderTarget = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type AttachedChart = SeriesAttachedParameter<Time, SeriesType>["chart"];

/** 三角尖端与影线端点的间距（px，媒体坐标） */
const TIP_GAP = 5;
/** 命中测试外扩（px） */
const HIT_PAD = 4;

interface TrapBox {
  /** 三角 bounding box（命中测试用） */
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  /** 三角中心 x / 尖端 y / 牌面高宽 */
  cx: number;
  tipY: number;
  w: number;
  h: number;
  mark: TrapMark;
}

// #rrggbb → #rrggbbaa（置信度映射填充透明度：低置信淡、高置信实）
function withAlpha(color: string, alpha: number): string {
  const a = Math.max(0, Math.min(1, alpha));
  return /^#[0-9a-fA-F]{6}$/.test(color)
    ? color + Math.round(a * 255).toString(16).padStart(2, "0")
    : color;
}

class TrapSignalsRenderer implements ISeriesPrimitivePaneRenderer {
  private readonly _boxes: TrapBox[];

  constructor(boxes: TrapBox[]) {
    this._boxes = boxes;
  }

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const W = mediaSize.width;
      const H = mediaSize.height;

      for (const b of this._boxes) {
        if (b.x1 < 0 || b.x0 > W || b.y1 < 0 || b.y0 > H) continue;
        const isBull = b.mark.signal.type === "bull_trap";
        const color = TRAP_COLORS[b.mark.signal.type];
        const conf = Math.max(0, Math.min(1, b.mark.signal.confidence));

        // 1 · 三角牌面：诱多倒三角（尖朝下指向冲高点）/ 诱空正三角（尖朝上指向杀跌点）
        ctx.beginPath();
        if (isBull) {
          ctx.moveTo(b.cx, b.tipY);
          ctx.lineTo(b.cx - b.w / 2, b.tipY - b.h);
          ctx.lineTo(b.cx + b.w / 2, b.tipY - b.h);
        } else {
          ctx.moveTo(b.cx, b.tipY);
          ctx.lineTo(b.cx - b.w / 2, b.tipY + b.h);
          ctx.lineTo(b.cx + b.w / 2, b.tipY + b.h);
        }
        ctx.closePath();
        ctx.fillStyle = withAlpha(color, 0.55 + 0.45 * conf);
        ctx.fill();
        ctx.strokeStyle = "#0d1117";
        ctx.lineWidth = 1.5;
        ctx.stroke();

        // 2 · 白色粗体「!」（倒三角可视重心偏上、正三角偏下）
        ctx.fillStyle = "#ffffff";
        ctx.font = `bold ${Math.round(b.h * 0.55)}px -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const textY = isBull ? b.tipY - b.h * 0.62 : b.tipY + b.h * 0.62;
        ctx.fillText("!", b.cx, textY);
      }
      ctx.textAlign = "start";
      ctx.textBaseline = "alphabetic";
    });
  }
}

class TrapSignalsPaneView implements ISeriesPrimitivePaneView {
  private readonly _source: TrapSignalsPrimitive;

  constructor(source: TrapSignalsPrimitive) {
    this._source = source;
  }

  zOrder(): SeriesPrimitivePaneViewZOrder {
    return "top";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const boxes = this._source.boxes();
    return boxes.length > 0 ? new TrapSignalsRenderer(boxes) : null;
  }
}

export class TrapSignalsPrimitive implements ISeriesPrimitive<Time> {
  private _marks: TrapMark[] = [];
  private _boxes: TrapBox[] = [];
  private readonly _views: readonly ISeriesPrimitivePaneView[];
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _chart: AttachedChart | null = null;
  private _requestUpdate: (() => void) | null = null;

  constructor() {
    this._views = [new TrapSignalsPaneView(this)];
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

  setMarks(marks: TrapMark[]): void {
    this._marks = marks;
    this._requestUpdate?.();
  }

  boxes(): TrapBox[] {
    return this._boxes;
  }

  /** 面板像素坐标命中测试（三角 bounding box + 外扩），供悬停浮层与点击弹卡片。 */
  markAt(x: number, y: number): TrapMark | null {
    for (const b of this._boxes) {
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

  // 每次视口变化（缩放/平移/新数据/resize）重投影：time→x、price→y；
  // 三角尺寸随 barSpacing 自适应（约 K 线宽 2 倍出头，14~26px 夹取）。
  updateAllViews(): void {
    this._boxes = [];
    const series = this._series;
    const chart = this._chart;
    if (!series || !chart || this._marks.length === 0) return;
    const timeScale = chart.timeScale();
    let barSpacing = 8;
    try {
      const opts = timeScale.options() as { barSpacing?: number };
      if (typeof opts.barSpacing === "number" && opts.barSpacing > 0) barSpacing = opts.barSpacing;
    } catch {
      // options 不可读时用默认牌面尺寸
    }
    const w = Math.max(14, Math.min(26, Math.round(barSpacing * 2.2)));
    const h = Math.round(w * 0.9);

    for (const m of this._marks) {
      const x = timeScale.timeToCoordinate(m.timeSec as Time);
      if (x === null) continue;
      if (m.signal.type === "bull_trap") {
        // 诱多：倒三角挂在信号 bar 高点上方，尖端向下指向冲高点
        const yHigh = series.priceToCoordinate(m.anchorHigh);
        if (yHigh === null) continue;
        const tipY = yHigh - TIP_GAP;
        this._boxes.push({
          x0: x - w / 2,
          y0: tipY - h,
          x1: x + w / 2,
          y1: tipY,
          cx: x,
          tipY,
          w,
          h,
          mark: m,
        });
      } else {
        // 诱空：正三角挂在信号 bar 低点下方，尖端向上指向杀跌点
        const yLow = series.priceToCoordinate(m.anchorLow);
        if (yLow === null) continue;
        const tipY = yLow + TIP_GAP;
        this._boxes.push({
          x0: x - w / 2,
          y0: tipY,
          x1: x + w / 2,
          y1: tipY + h,
          cx: x,
          tipY,
          w,
          h,
          mark: m,
        });
      }
    }
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._views;
  }
}
