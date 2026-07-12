// PredictionPrimitive — lightweight-charts series primitive，把走势预测画进
// K 线图右侧未来区域（bar 逻辑索引可越过最新 K 线，天然支持未来投影）：
//   概率锥    anchor（预测生成时刻收盘价）向右张开到目标区间的半透明多边形，
//             上下边界虚线描边；终端画目标区间高低价小字
//   预测路径  anchor → path 各点的虚线折线（方向色：涨绿/跌红/震荡紫）
//   概率标签  「AI 预测 ↑42% →25% ↓33%」小标签盒贴在锥起点，风格与现有
//             分析标签一致（暗底、细边、10px 小字，不遮主 K 线）
// 渲染层次：锥体 zOrder "bottom"（垫在蜡烛下，虽然未来区域本无蜡烛，但左缘
// 可能与最新几根重叠）；路径与标签 "top"。缩放/平移/切周期由 updateAllViews()
// 重投影（逻辑索引→x、价格→y），跟手不错位。
// 悬停命中经 predictionAt(x,y) 供 KlineChart 的 crosshair 浮层查询。

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
import type { PredictionOverlay } from "@/lib/predict";
import { PREDICT_COLORS, fmtPredictPrice } from "@/lib/predict";

type RenderTarget = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type AttachedChart = SeriesAttachedParameter<Time, SeriesType>["chart"];

const FONT_STACK = "-apple-system, BlinkMacSystemFont, PingFang SC, sans-serif";

function rgba(hex: string, alpha: number): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/** 投影后的绘制几何（媒体像素坐标） */
interface PredictionLayout {
  ax: number;
  ay: number;
  /** 锥终端 x（horizon 末端） */
  ex: number;
  zoneTopY: number;
  zoneBottomY: number;
  pathPts: { x: number; y: number }[];
  /** 标签盒（命中测试 + label view 绘制共用） */
  label: { x: number; y: number; w: number; h: number; title: string; probLine: string };
  overlay: PredictionOverlay;
}

// ── 锥体（bottom 层）─────────────────────────────────────────────────────

class PredictionConeRenderer implements ISeriesPrimitivePaneRenderer {
  private readonly _l: PredictionLayout;

  constructor(l: PredictionLayout) {
    this._l = l;
  }

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const l = this._l;
      if (l.ex <= l.ax || l.ex < 0 || l.ax > mediaSize.width) return;

      // 1 · 锥体填充：anchor 点 → 终端目标区间上下沿（梯形张开）
      ctx.beginPath();
      ctx.moveTo(l.ax, l.ay);
      ctx.lineTo(l.ex, l.zoneTopY);
      ctx.lineTo(l.ex, l.zoneBottomY);
      ctx.closePath();
      ctx.fillStyle = rgba(PREDICT_COLORS.cone, 0.08);
      ctx.fill();

      // 2 · 锥上下边界虚线（透明度略高于填充，边界可读）
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = rgba(PREDICT_COLORS.cone, 0.4);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(l.ax, l.ay);
      ctx.lineTo(l.ex, l.zoneTopY);
      ctx.moveTo(l.ax, l.ay);
      ctx.lineTo(l.ex, l.zoneBottomY);
      ctx.stroke();

      // 3 · 终端目标区间竖线 + 高低价小字（画布右缘裁剪时省略文字）
      ctx.setLineDash([]);
      ctx.strokeStyle = rgba(PREDICT_COLORS.cone, 0.55);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(l.ex, l.zoneTopY);
      ctx.lineTo(l.ex, l.zoneBottomY);
      ctx.stroke();

      if (l.ex <= mediaSize.width - 4) {
        ctx.font = `10px ${FONT_STACK}`;
        ctx.textBaseline = "middle";
        ctx.textAlign = "right";
        ctx.fillStyle = rgba(PREDICT_COLORS.cone, 0.9);
        ctx.fillText(fmtPredictPrice(l.overlay.targetZone.high), l.ex - 4, l.zoneTopY - 7);
        ctx.fillText(fmtPredictPrice(l.overlay.targetZone.low), l.ex - 4, l.zoneBottomY + 8);
        ctx.textAlign = "start";
        ctx.textBaseline = "alphabetic";
      }
    });
  }
}

// ── 路径 + 标签（top 层）─────────────────────────────────────────────────

class PredictionPathRenderer implements ISeriesPrimitivePaneRenderer {
  private readonly _l: PredictionLayout;

  constructor(l: PredictionLayout) {
    this._l = l;
  }

  draw(target: RenderTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      const l = this._l;
      const dirColor = PREDICT_COLORS[l.overlay.direction];

      // 1 · 预测路径虚线（anchor → path 各点）
      if (l.pathPts.length > 0) {
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = rgba(dirColor, 0.85);
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(l.ax, l.ay);
        for (const p of l.pathPts) ctx.lineTo(p.x, p.y);
        ctx.stroke();
        ctx.setLineDash([]);

        // 路径终点小箭头方向点
        const lastPt = l.pathPts[l.pathPts.length - 1];
        ctx.beginPath();
        ctx.arc(lastPt.x, lastPt.y, 2.5, 0, Math.PI * 2);
        ctx.fillStyle = dirColor;
        ctx.fill();
      }

      // 2 · anchor 锚点（预测起点圆点，白描边隔离蜡烛）
      ctx.beginPath();
      ctx.arc(l.ax, l.ay, 3, 0, Math.PI * 2);
      ctx.fillStyle = PREDICT_COLORS.cone;
      ctx.fill();
      ctx.strokeStyle = "#0d1117";
      ctx.lineWidth = 1;
      ctx.stroke();

      // 3 · 概率标签盒（两行：标题 / 概率），风格对齐现有分析标签
      const b = l.label;
      ctx.fillStyle = "rgba(22, 27, 34, 0.92)";
      ctx.strokeStyle = "#30363d";
      ctx.lineWidth = 1;
      ctx.beginPath();
      const r = 4;
      ctx.moveTo(b.x + r, b.y);
      ctx.arcTo(b.x + b.w, b.y, b.x + b.w, b.y + b.h, r);
      ctx.arcTo(b.x + b.w, b.y + b.h, b.x, b.y + b.h, r);
      ctx.arcTo(b.x, b.y + b.h, b.x, b.y, r);
      ctx.arcTo(b.x, b.y, b.x + b.w, b.y, r);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();

      ctx.textBaseline = "top";
      ctx.font = `bold 10px ${FONT_STACK}`;
      ctx.fillStyle = PREDICT_COLORS.cone;
      ctx.fillText(b.title, b.x + 6, b.y + 4);

      // 概率行三段分色：↑绿 →灰 ↓红
      ctx.font = `10px ${FONT_STACK}`;
      const o = l.overlay.probability;
      const pct = (v: number) => `${Math.round(v * 100)}%`;
      let tx = b.x + 6;
      const ty = b.y + 17;
      const seg = (text: string, color: string) => {
        ctx.fillStyle = color;
        ctx.fillText(text, tx, ty);
        tx += ctx.measureText(text).width;
      };
      seg(`↑ ${pct(o.up)}`, PREDICT_COLORS.up);
      seg("  ·  ", "#8b949e");
      seg(`→ ${pct(o.sideways)}`, "#8b949e");
      seg("  ·  ", "#8b949e");
      seg(`↓ ${pct(o.down)}`, PREDICT_COLORS.down);

      ctx.textBaseline = "alphabetic";
    });
  }
}

class PredictionPaneView implements ISeriesPrimitivePaneView {
  private readonly _source: PredictionPrimitive;
  private readonly _layer: "cone" | "path";

  constructor(source: PredictionPrimitive, layer: "cone" | "path") {
    this._source = source;
    this._layer = layer;
  }

  zOrder(): SeriesPrimitivePaneViewZOrder {
    return this._layer === "cone" ? "bottom" : "top";
  }

  renderer(): ISeriesPrimitivePaneRenderer | null {
    const layout = this._source.layout();
    if (!layout) return null;
    return this._layer === "cone"
      ? new PredictionConeRenderer(layout)
      : new PredictionPathRenderer(layout);
  }
}

export class PredictionPrimitive implements ISeriesPrimitive<Time> {
  private _overlay: PredictionOverlay | null = null;
  private _layout: PredictionLayout | null = null;
  private readonly _views: readonly ISeriesPrimitivePaneView[];
  private _series: ISeriesApi<SeriesType, Time> | null = null;
  private _chart: AttachedChart | null = null;
  private _requestUpdate: (() => void) | null = null;

  constructor() {
    this._views = [
      new PredictionPaneView(this, "cone"),
      new PredictionPaneView(this, "path"),
    ];
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

  /** 更新预测载荷；null = 清空（预测关闭 / 数据不可用）。 */
  setPrediction(overlay: PredictionOverlay | null): void {
    this._overlay = overlay;
    this._requestUpdate?.();
  }

  layout(): PredictionLayout | null {
    return this._layout;
  }

  /** 面板像素坐标命中测试（标签盒 / 锥体三角形），返回悬停文案。 */
  predictionAt(x: number, y: number): string | null {
    const l = this._layout;
    if (!l) return null;
    const b = l.label;
    if (x >= b.x - 2 && x <= b.x + b.w + 2 && y >= b.y - 2 && y <= b.y + b.h + 2) {
      return l.overlay.tooltip;
    }
    // 锥体三角形（anchor → 终端上沿 → 终端下沿）内测：对每条边做叉积同号判定
    if (x >= l.ax && x <= l.ex) {
      const sign = (x1: number, y1: number, x2: number, y2: number, px: number, py: number) =>
        (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1);
      const d1 = sign(l.ax, l.ay, l.ex, l.zoneTopY, x, y);
      const d2 = sign(l.ex, l.zoneTopY, l.ex, l.zoneBottomY, x, y);
      const d3 = sign(l.ex, l.zoneBottomY, l.ax, l.ay, x, y);
      const hasNeg = d1 < 0 || d2 < 0 || d3 < 0;
      const hasPos = d1 > 0 || d2 > 0 || d3 > 0;
      if (!(hasNeg && hasPos)) return l.overlay.tooltip;
    }
    return null;
  }

  // 每次视口变化（缩放/平移/新数据/resize）重投影：bar 逻辑索引→x、价格→y。
  // 逻辑索引允许越过最新 K 线（未来区域），lightweight-charts 按 barSpacing
  // 线性外推坐标，缩放平移天然跟手。
  updateAllViews(): void {
    this._layout = null;
    const series = this._series;
    const chart = this._chart;
    const o = this._overlay;
    if (!series || !chart || !o) return;
    const timeScale = chart.timeScale();

    const ax = timeScale.logicalToCoordinate(o.anchorIdx as Logical);
    const ex = timeScale.logicalToCoordinate((o.anchorIdx + o.horizon) as Logical);
    const ay = series.priceToCoordinate(o.anchorPrice);
    const zoneTopY = series.priceToCoordinate(o.targetZone.high);
    const zoneBottomY = series.priceToCoordinate(o.targetZone.low);
    if (ax === null || ex === null || ay === null || zoneTopY === null || zoneBottomY === null) {
      return;
    }

    const pathPts: { x: number; y: number }[] = [];
    for (const p of o.points) {
      const px = timeScale.logicalToCoordinate(p.idx as Logical);
      const py = series.priceToCoordinate(p.price);
      if (px === null || py === null) continue;
      pathPts.push({ x: px, y: py });
    }

    // 标签盒尺寸：概率行最宽约 150px（三段 + 分隔），标题行较短；
    // canvas 无法在投影期 measureText（无 ctx），按字符数近似估宽
    const probLine = "↑ 100% · → 100% · ↓ 100%";
    const w = Math.max(o.title.length * 6.5 + 12, probLine.length * 5.4 + 12);
    const h = 30;
    // 默认贴锥起点上方；看跌路径向下时放下方，避免压住路径
    let ly = o.direction === "down" ? ay + 10 : ay - h - 10;
    if (ly < 4) ly = ay + 10;
    const lx = Math.max(4, ax - 4);

    this._layout = {
      ax,
      ay,
      ex,
      zoneTopY,
      zoneBottomY,
      pathPts,
      label: { x: lx, y: ly, w, h, title: o.title, probLine },
      overlay: o,
    };
  }

  paneViews(): readonly ISeriesPrimitivePaneView[] {
    return this._views;
  }
}
