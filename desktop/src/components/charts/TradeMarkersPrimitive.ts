// TradeMarkersPrimitive — lightweight-charts series primitive，把信号历史逐笔
// 样本画成 QuantDinger 风格 L/S 字母徽章（内置 series markers 只有箭头/圆点，
// 画不出「方块字母 + 引线 + 盈亏角标」的组合，故走 primitive 自绘通道）：
//   入场徽章  圆角方块，绿底白字「L」= 做多 / 红底白字「S」= 做空；
//             锚点圆点贴在触发 K 线（多单锚 bar 低点下方、空单锚 bar 高点上方），
//             短引线连到徽章；徽章右上角标：盈 → 绿底「✓」/ 亏 → 红底「✕」
//   出场圆点  画在出场 bar 的出场价上（盈绿亏红，白描边）
// 尺寸随 barSpacing 自适应（约 K 线宽 2~3 倍，14~30px 夹取），缩放/平移由
// updateAllViews() 重投影（time→x、price→y），跟手不错位。
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
import type { TradeMark } from "@/lib/signalTrades";
import { TRADE_MARK_COLORS } from "@/lib/signalTrades";

type RenderTarget = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type AttachedChart = SeriesAttachedParameter<Time, SeriesType>["chart"];

/** 多/空徽章底色（与全局涨绿跌红语义一致，不随主题变） */
const SIDE_COLORS = { long: "#3fb950", short: "#f85149" } as const;

/** 锚点圆点半径 / 引线长度 / 出场圆点半径（px，媒体坐标） */
const ANCHOR_DOT_R = 2.5;
const LEADER_LEN = 7;
const EXIT_DOT_R = 3.5;

interface MarkBox {
  /** 徽章矩形（含字母），命中测试用 */
  bx: number;
  by: number;
  bs: number;
  /** 锚点圆点坐标（贴 K 线） */
  ax: number;
  ay: number;
  /** 出场圆点坐标；出场超窗时为 null */
  ex: number | null;
  ey: number | null;
  mark: TradeMark;
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

class TradeMarkersRenderer implements ISeriesPrimitivePaneRenderer {
  private readonly _boxes: MarkBox[];

  constructor(boxes: MarkBox[]) {
    this._boxes = boxes;
  }

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const W = mediaSize.width;
      const H = mediaSize.height;
      const font = (px: number, bold = true) =>
        `${bold ? "bold " : ""}${px}px -apple-system, BlinkMacSystemFont, PingFang SC, sans-serif`;

      for (const b of this._boxes) {
        if (b.bx + b.bs < 0 || b.bx > W || b.by + b.bs < 0 || b.by > H) {
          // 徽章整体在视口外：出场圆点可能仍在视口内，单独画
          this._drawExit(ctx, b, W, H);
          continue;
        }
        const sideColor = SIDE_COLORS[b.mark.side];
        const resColor = b.mark.win ? TRADE_MARK_COLORS.win : TRADE_MARK_COLORS.loss;

        // 1 · 锚点圆点（贴 K 线）+ 短引线连到徽章
        ctx.fillStyle = sideColor;
        ctx.beginPath();
        ctx.arc(b.ax, b.ay, ANCHOR_DOT_R, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = sideColor;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(b.ax, b.ay);
        // 多单徽章在下：引线向下接徽章顶边；空单反之接底边
        ctx.lineTo(b.ax, b.mark.side === "long" ? b.by : b.by + b.bs);
        ctx.stroke();

        // 2 · 徽章本体（圆角方块 + 白色粗体字母）
        roundRect(ctx, b.bx, b.by, b.bs, b.bs, Math.max(3, b.bs * 0.2));
        ctx.fillStyle = sideColor;
        ctx.fill();
        ctx.fillStyle = "#ffffff";
        ctx.font = font(Math.round(b.bs * 0.62));
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(b.mark.side === "long" ? "L" : "S", b.bx + b.bs / 2, b.by + b.bs / 2 + 0.5);

        // 3 · 盈亏角标（右上角，白圈隔离底色：绿勾 / 红叉）
        const cr = Math.max(4.5, b.bs * 0.26);
        const cx = b.bx + b.bs - 1;
        const cy = b.by + 1;
        ctx.beginPath();
        ctx.arc(cx, cy, cr, 0, Math.PI * 2);
        ctx.fillStyle = resColor;
        ctx.fill();
        ctx.strokeStyle = "#0d1117";
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.fillStyle = "#ffffff";
        ctx.font = font(Math.round(cr * 1.5));
        ctx.fillText(b.mark.win ? "✓" : "✕", cx, cy + 0.5);

        // 4 · 出场圆点
        this._drawExit(ctx, b, W, H);
      }
      ctx.textAlign = "start";
      ctx.textBaseline = "alphabetic";
    });
  }

  private _drawExit(ctx: CanvasRenderingContext2D, b: MarkBox, W: number, H: number): void {
    if (b.ex === null || b.ey === null) return;
    if (b.ex < -EXIT_DOT_R || b.ex > W + EXIT_DOT_R || b.ey < -EXIT_DOT_R || b.ey > H + EXIT_DOT_R) return;
    ctx.beginPath();
    ctx.arc(b.ex, b.ey, EXIT_DOT_R, 0, Math.PI * 2);
    ctx.fillStyle = b.mark.win ? TRADE_MARK_COLORS.win : TRADE_MARK_COLORS.loss;
    ctx.fill();
    ctx.strokeStyle = "#0d1117";
    ctx.lineWidth = 1;
    ctx.stroke();
  }
}

class TradeMarkersPaneView implements ISeriesPrimitivePaneView {
  private readonly _source: TradeMarkersPrimitive;

  constructor(source: TradeMarkersPrimitive) {
    this._source = source;
  }

  zOrder(): SeriesPrimitivePaneViewZOrder {
    return "top";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const boxes = this._source.boxes();
    return boxes.length > 0 ? new TradeMarkersRenderer(boxes) : null;
  }
}

export class TradeMarkersPrimitive implements ISeriesPrimitive<Time> {
  private _marks: TradeMark[] = [];
  private _boxes: MarkBox[] = [];
  private readonly _views: readonly ISeriesPrimitivePaneView[];
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _chart: AttachedChart | null = null;
  private _requestUpdate: (() => void) | null = null;

  constructor() {
    this._views = [new TradeMarkersPaneView(this)];
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

  setMarks(marks: TradeMark[]): void {
    this._marks = marks;
    this._requestUpdate?.();
  }

  boxes(): MarkBox[] {
    return this._boxes;
  }

  /** 面板像素坐标命中测试（徽章矩形 / 出场圆点），返回悬停文案。 */
  markAt(x: number, y: number): string | null {
    let hit: string | null = null;
    for (const b of this._boxes) {
      const pad = 2;
      if (x >= b.bx - pad && x <= b.bx + b.bs + pad && y >= b.by - pad && y <= b.by + b.bs + pad) {
        hit = b.mark.tooltip;
      }
      if (
        b.ex !== null && b.ey !== null &&
        Math.abs(x - b.ex) <= EXIT_DOT_R + 3 && Math.abs(y - b.ey) <= EXIT_DOT_R + 3
      ) {
        hit = b.mark.exitTooltip;
      }
    }
    return hit;
  }

  // 每次视口变化（缩放/平移/新数据/resize）重投影：time→x、price→y；
  // 徽章尺寸随 barSpacing 自适应（约 K 线宽 2~3 倍，14~30px 夹取）。
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
      // options 不可读时用默认徽章尺寸
    }
    const badge = Math.max(14, Math.min(30, Math.round(barSpacing * 2.4)));

    for (const m of this._marks) {
      const x = timeScale.timeToCoordinate(m.timeSec as Time);
      const yAnchor = series.priceToCoordinate(m.anchorPrice);
      if (x === null || yAnchor === null) continue;
      const gap = ANCHOR_DOT_R + LEADER_LEN;
      // 多单徽章挂在锚点下方，空单挂在上方
      const by = m.side === "long" ? yAnchor + gap : yAnchor - gap - badge;
      let ex: number | null = null;
      let ey: number | null = null;
      if (m.exitTimeSec !== null) {
        const xo = timeScale.timeToCoordinate(m.exitTimeSec as Time);
        const yo = series.priceToCoordinate(m.exitPrice);
        if (xo !== null && yo !== null) {
          ex = xo;
          ey = yo;
        }
      }
      this._boxes.push({
        bx: x - badge / 2,
        by,
        bs: badge,
        ax: x,
        ay: yAnchor,
        ex,
        ey,
        mark: m,
      });
    }
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._views;
  }
}
