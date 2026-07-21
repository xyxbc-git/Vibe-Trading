// IchimokuCloudPrimitive — lightweight-charts series primitive，绘制一目均衡
// 表的云带（Kumo）：SpanA/SpanB 两条先行带边线 + 之间的双色半透明填充
// （A≥B 绿云=看涨支撑、A<B 红云=看跌压力），交叉处按线性插值精确分段变色。
//
// 为什么用 primitive 而不是 LineSeries：云的未来段（前移 26 根）越过最后
// 一根 K 线，LineSeries 无法锚定不存在的 time；primitive 用 logical 索引
// 经 timeScale.logicalToCoordinate 外推 x（与 PositionZonePrimitive 同机制），
// 未来留白区照画。历史段与未来段同一条管线，无接缝。
//
// zOrder "normal"：云垫在蜡烛同层背景（低透明度不遮 K 线主体），
// 缩放/平移/新数据由库 updateAllViews 回调驱动重投影，天然不错位。

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
import type { CloudPoint } from "@/lib/ichimoku";

type RenderTarget = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type AttachedChart = SeriesAttachedParameter<Time, SeriesType>["chart"];

/** 项目暗色主题色（与 tailwind jarvis-green/red 对齐） */
const COLOR_BULL = "#3fb950";
const COLOR_BEAR = "#f85149";
const FILL_ALPHA = 0.12;
/** 未来云段填充再淡一档（预告性质，视觉上与已实现区分） */
const FUTURE_FILL_ALPHA = 0.07;
const EDGE_ALPHA = 0.55;

function rgba(hex: string, alpha: number): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/** 投影后的云顶点：x 像素、A/B 两条边的 y 像素、是否未来段 */
interface ProjectedPoint {
  x: number;
  yA: number;
  yB: number;
  /** 价格域符号：A−B（用于判色与交叉插值，像素 y 轴反向不可靠） */
  diff: number;
  future: boolean;
}

class IchimokuCloudRenderer implements ISeriesPrimitivePaneRenderer {
  constructor(private readonly pts: readonly ProjectedPoint[]) {}

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const pts = this.pts;
      if (pts.length < 2) return;
      const { width } = mediaSize;

      // 相邻点对逐段填充；A/B 交叉的段按插值拆成两段，颜色精确切换
      for (let i = 1; i < pts.length; i++) {
        const p1 = pts[i - 1];
        const p2 = pts[i];
        if (p2.x < 0 || p1.x > width) continue;

        const segs: { a: ProjectedPoint; b: ProjectedPoint }[] = [];
        if (p1.diff === 0 || p2.diff === 0 || p1.diff > 0 === p2.diff > 0) {
          segs.push({ a: p1, b: p2 });
        } else {
          // 交叉：t 处 A=B（按价格差插值；x/y 同一 t 线性）
          const t = p1.diff / (p1.diff - p2.diff);
          const mid: ProjectedPoint = {
            x: p1.x + (p2.x - p1.x) * t,
            yA: p1.yA + (p2.yA - p1.yA) * t,
            yB: p1.yB + (p2.yB - p1.yB) * t,
            diff: 0,
            future: p1.future,
          };
          segs.push({ a: p1, b: mid }, { a: mid, b: p2 });
        }

        for (const s of segs) {
          const refDiff = s.a.diff !== 0 ? s.a.diff : s.b.diff;
          const color = refDiff >= 0 ? COLOR_BULL : COLOR_BEAR;
          const alpha = s.a.future || s.b.future ? FUTURE_FILL_ALPHA : FILL_ALPHA;
          ctx.fillStyle = rgba(color, alpha);
          ctx.beginPath();
          ctx.moveTo(s.a.x, s.a.yA);
          ctx.lineTo(s.b.x, s.b.yA);
          ctx.lineTo(s.b.x, s.b.yB);
          ctx.lineTo(s.a.x, s.a.yB);
          ctx.closePath();
          ctx.fill();
        }
      }

      // SpanA / SpanB 边线（A 绿 B 红，未来段虚线）
      const stroke = (pick: (p: ProjectedPoint) => number, color: string) => {
        ctx.strokeStyle = rgba(color, EDGE_ALPHA);
        ctx.lineWidth = 1;
        let started = false;
        let dashed: boolean | null = null;
        for (let i = 0; i < pts.length; i++) {
          const p = pts[i];
          const wantDash = p.future;
          if (dashed !== wantDash) {
            if (started) ctx.stroke();
            ctx.setLineDash(wantDash ? [4, 3] : []);
            ctx.beginPath();
            if (i > 0) ctx.moveTo(pts[i - 1].x, pick(pts[i - 1]));
            started = true;
            dashed = wantDash;
          }
          if (i === 0) ctx.moveTo(p.x, pick(p));
          else ctx.lineTo(p.x, pick(p));
        }
        if (started) ctx.stroke();
        ctx.setLineDash([]);
      };
      stroke((p) => p.yA, COLOR_BULL);
      stroke((p) => p.yB, COLOR_BEAR);
    });
  }
}

class IchimokuCloudPaneView implements ISeriesPrimitivePaneView {
  constructor(private readonly source: IchimokuCloudPrimitive) {}

  zOrder(): SeriesPrimitivePaneViewZOrder {
    // 云是背景区域：normal 层画在蜡烛之下的同格栅层，低透明不喧宾夺主
    return "normal";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const pts = this.source.projected();
    return pts.length >= 2 ? new IchimokuCloudRenderer(pts) : null;
  }
}

export class IchimokuCloudPrimitive implements ISeriesPrimitive<Time> {
  private cloud: readonly CloudPoint[] = [];
  private futureStart: number | null = null;
  private projectedPts: ProjectedPoint[] = [];
  private readonly views: readonly ISeriesPrimitivePaneView[];
  private series: ISeriesApi<SeriesType, Time> | null = null;
  private chart: AttachedChart | null = null;
  private requestUpdate: (() => void) | null = null;

  constructor() {
    this.views = [new IchimokuCloudPaneView(this)];
  }

  attached(param: SeriesAttachedParameter<Time, SeriesType>): void {
    this.series = param.series;
    this.chart = param.chart;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.series = null;
    this.chart = null;
    this.requestUpdate = null;
  }

  /** 设置/替换云点列（含未来段）；空数组即清除 */
  setCloud(cloud: readonly CloudPoint[], futureStart: number | null): void {
    this.cloud = cloud;
    this.futureStart = futureStart;
    this.requestUpdate?.();
  }

  projected(): readonly ProjectedPoint[] {
    return this.projectedPts;
  }

  updateAllViews(): void {
    this.projectedPts = [];
    const { series, chart } = this;
    if (!series || !chart || this.cloud.length < 2) return;
    const timeScale = chart.timeScale();
    for (const p of this.cloud) {
      const x = timeScale.logicalToCoordinate(p.logical as Logical);
      if (x === null) continue;
      const yA = series.priceToCoordinate(p.a);
      const yB = series.priceToCoordinate(p.b);
      if (yA === null || yB === null) continue;
      this.projectedPts.push({
        x,
        yA,
        yB,
        diff: p.a - p.b,
        future: this.futureStart !== null && p.logical >= this.futureStart,
      });
    }
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this.views;
  }
}
