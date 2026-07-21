import { useEffect, useRef, useState } from "react";
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type IPriceLine,
  type LineWidth,
  type MouseEventParams,
  ColorType,
  CrosshairMode,
  LineStyle,
  type CandlestickData,
  type HistogramData,
  type SeriesMarker,
  type SeriesMarkerShape,
  type Time,
} from "lightweight-charts";
import type { DrawingResult, SmartLevels, KeyLevel, DrawLineStyle, DrawHLine } from "@/lib/drawings";
import type { StructureDrawings, StructureMarker } from "@/api/client";
import type { TradeMark } from "@/lib/signalTrades";
import type { PredictionOverlay } from "@/lib/predict";
import type { CloudPoint } from "@/lib/ichimoku";
import { positionZoneWindow, type PositionZoneView } from "@/lib/positionZone";
import { TradeMarkersPrimitive } from "./TradeMarkersPrimitive";
import { PredictionPrimitive } from "./PredictionPrimitive";
import { PositionZonePrimitive } from "./PositionZonePrimitive";
import { IchimokuCloudPrimitive } from "./IchimokuCloudPrimitive";

/** 云图叠加载荷：三条线走 LineSeries，云带（含未来段）走 primitive */
export interface IchimokuOverlay {
  /** 转换线（橙）/ 基准线（青）/ 迟行线（紫虚线）的 time 对齐点列 */
  tenkan: { time: Time; value: number }[];
  kijun: { time: Time; value: number }[];
  chikou: { time: Time; value: number }[];
  cloud: CloudPoint[];
  futureStart: number | null;
  /** 云前移根数（右侧留白滚动量） */
  displacement: number;
  /** 悬停某根 K 线（time 秒）时的五线数值文案 */
  tipByTime: Map<number, string>;
}

/** 云图三条线的主题色：基准线青色对齐用户照片，转换橙、迟行紫与既有色板区分 */
const ICHIMOKU_LINE_COLORS = {
  tenkan: "#d29922",
  kijun: "#39c5cf",
  chikou: "#bc8cff",
} as const;

interface KlineChartProps {
  data: CandlestickData<Time>[];
  volumeData?: HistogramData<Time>[];
  height?: number;
  /** 智能视图：最近强压力/支撑 + 现价；null/undefined 时不渲染。 */
  smartLevels?: SmartLevels | null;
  /** 自动画线引擎输出（趋势线 / 支撑压力 / 斐波那契 / 通道 / 矩形）。 */
  drawings?: DrawingResult | null;
  /** 外部信号系统的关键价位叠加（如十二套技术体系）。 */
  keyLevels?: KeyLevel[];
  /** 交易计划线（入场区 / 止损 / 止盈），由 planToOverlay 生成。 */
  planLines?: DrawHLine[];
  /** 信号历史盈损标记（L/S 字母徽章 + 出场圆点，盈✓亏✕），由 tradesToMarks 生成。 */
  tradeMarks?: TradeMark[];
  /** 走势预测层（概率锥 + 路径虚线 + 概率标签），由 buildPredictionOverlay 生成；null/undefined 不渲染。 */
  prediction?: PredictionOverlay | null;
  /** 信号多空区间图（TradingView position 风格，同时只显一个），由 buildPositionZoneView 生成；null/undefined 不渲染。 */
  positionZone?: PositionZoneView | null;
  /**
   * 信号结构画线（缠论笔折线 / 中枢框 / 关键水平位），来自 /api/twelve/structure。
   * polylines → LineSeries 全点列；boxes → 上下两条 dashed 边缘线；hlines →
   * keyLevels 通道同风格的 dotted priceLine。null/undefined 不渲染。
   */
  structure?: StructureDrawings | null;
  /**
   * 信号结构买卖点标注 → 原生 candleSeries.setMarkers（替换式，卸载/清空时
   * 重设为空数组）；与 tradeMarks 的 canvas 自绘 primitive 互不干扰。
   */
  structMarkers?: StructureMarker[] | null;
  /**
   * 实时最新价（与顶栏同源的 ticker 报价）。存在时把最后一根未收线蜡烛的
   * close 流式更新为该价（high/low 相应扩展），使图表右侧最新价标签与顶栏
   * 价格一致；null/undefined 时保持 kline 轮询数据原样。
   */
  livePrice?: { price: number; timeSec: number } | null;
  /**
   * 一目均衡表云图叠加（buildIchimokuOverlay 生成）：转换/基准/迟行三条
   * LineSeries + SpanA/SpanB 双色云带 primitive（含未来 26 根延伸段）。
   * null/undefined 不渲染。
   */
  ichimoku?: IchimokuOverlay | null;
}

function fmtPrice(v: number): string {
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

// lightweight-charts only accepts integer line widths 1..4.
function clampWidth(w: number): LineWidth {
  const r = Math.round(w);
  return (r < 1 ? 1 : r > 4 ? 4 : r) as LineWidth;
}

// No per-series opacity option — bake it into the color (#rrggbb → #rrggbbaa).
function withOpacity(color: string, opacity?: number): string {
  if (opacity === undefined) return color;
  const a = Math.max(0, Math.min(1, opacity));
  if (/^#[0-9a-fA-F]{6}$/.test(color)) {
    return color + Math.round(a * 255).toString(16).padStart(2, "0");
  }
  return color;
}

function toLineStyle(s?: DrawLineStyle): LineStyle {
  return s === "dashed" ? LineStyle.Dashed : s === "dotted" ? LineStyle.Dotted : LineStyle.Solid;
}

export default function KlineChart({
  data,
  volumeData,
  height = 500,
  smartLevels,
  drawings,
  keyLevels,
  planLines,
  tradeMarks,
  prediction,
  positionZone,
  structure,
  structMarkers,
  livePrice,
  ichimoku,
}: KlineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const overlaySeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const ichimokuSeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const marksPrimitiveRef = useRef<TradeMarkersPrimitive | null>(null);
  const predictionPrimitiveRef = useRef<PredictionPrimitive | null>(null);
  const positionZonePrimitiveRef = useRef<PositionZonePrimitive | null>(null);
  const ichimokuPrimitiveRef = useRef<IchimokuCloudPrimitive | null>(null);
  // 预测视口右扩去重：同一份预测（genKey）只扩一次，用户随后缩放/平移不被打断
  const predictionRef = useRef<PredictionOverlay | null>(null);
  const appliedGenKeyRef = useRef<string | null>(null);
  // 云图载荷（悬停 tip / 数据刷新后的右侧留白重算共用）；开关去重同预测层模式
  const ichimokuRef = useRef<IchimokuOverlay | null>(null);
  const ichimokuOnRef = useRef(false);
  const disposedRef = useRef(false);
  // 悬停命中（盈损徽章 / 预测层）时的浮层文案；null = 隐藏
  const [zoneTip, setZoneTip] = useState<{ x: number; y: number; text: string } | null>(null);
  // Bumped after every chart (re-)init so data/overlay effects re-apply onto
  // the freshly created series (e.g. after a height-change re-init).
  const [initVersion, setInitVersion] = useState(0);

  useEffect(() => {
    if (!containerRef.current) return;

    disposedRef.current = false;

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: "#161b22" },
        textColor: "#8b949e",
        fontFamily:
          "-apple-system, BlinkMacSystemFont, PingFang SC, sans-serif",
      },
      grid: {
        vertLines: { color: "#1c2128" },
        horzLines: { color: "#1c2128" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#30363d" },
      timeScale: {
        borderColor: "#30363d",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: "#3fb950",
      downColor: "#f85149",
      borderUpColor: "#3fb950",
      borderDownColor: "#f85149",
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
    });

    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });

    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    // 信号盈损 L/S 徽章 primitive：zOrder "top"，浮在蜡烛上方。缩放/平移/
    // 切周期由库的 updateAllViews 回调驱动重投影（价格→y、时间/bar 索引→x），
    // 天然不错位。
    const marksPrimitive = new TradeMarkersPrimitive();
    candleSeries.attachPrimitive(marksPrimitive);
    const predictionPrimitive = new PredictionPrimitive();
    candleSeries.attachPrimitive(predictionPrimitive);
    // 信号多空区间图（TradingView position 风格）：独立图元，与预测层互不影响
    const positionZonePrimitive = new PositionZonePrimitive();
    candleSeries.attachPrimitive(positionZonePrimitive);
    // 一目均衡表云带（双色 Kumo + 未来延伸段）：normal 层垫在蜡烛下
    const ichimokuPrimitive = new IchimokuCloudPrimitive();
    candleSeries.attachPrimitive(ichimokuPrimitive);

    // 悬停浮层（两类命中共用一个浮层，徽章 > 预测层）：
    //   1. 信号盈损 L/S 徽章 / 出场圆点：像素坐标命中测试 → 每笔详情
    //   2. 走势预测层：概率标签盒 / 概率锥命中 → 概率与目标区摘要
    const handleCrosshair = (param: MouseEventParams<Time>) => {
      if (disposedRef.current) return;
      const pt = param.point;
      if (!pt) {
        setZoneTip(null);
        return;
      }
      const markTip = marksPrimitive.markAt(pt.x, pt.y);
      if (markTip) {
        setZoneTip({ x: pt.x, y: pt.y, text: markTip });
        return;
      }
      const predictTip = predictionPrimitive.predictionAt(pt.x, pt.y);
      if (predictTip) {
        setZoneTip({ x: pt.x, y: pt.y, text: predictTip });
        return;
      }
      // 云图五线数值：悬停在某根 K 线上且该 bar 有指标值时展示
      const ichiTip =
        param.time !== undefined
          ? ichimokuRef.current?.tipByTime.get(Number(param.time))
          : undefined;
      setZoneTip(ichiTip ? { x: pt.x, y: pt.y, text: ichiTip } : null);
    };
    chart.subscribeCrosshairMove(handleCrosshair);

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    marksPrimitiveRef.current = marksPrimitive;
    predictionPrimitiveRef.current = predictionPrimitive;
    positionZonePrimitiveRef.current = positionZonePrimitive;
    ichimokuPrimitiveRef.current = ichimokuPrimitive;
    priceLinesRef.current = [];
    overlaySeriesRef.current = [];
    ichimokuSeriesRef.current = [];
    // 重建后的图表实例 rightOffset 归零，同一份预测需重新扩一次视野
    appliedGenKeyRef.current = null;
    setInitVersion(v => v + 1);

    const handleResize = () => {
      if (!disposedRef.current && containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", handleResize);

    return () => {
      disposedRef.current = true;
      window.removeEventListener("resize", handleResize);
      chart.unsubscribeCrosshairMove(handleCrosshair);
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      marksPrimitiveRef.current = null;
      predictionPrimitiveRef.current = null;
      positionZonePrimitiveRef.current = null;
      ichimokuPrimitiveRef.current = null;
      priceLinesRef.current = [];
      overlaySeriesRef.current = [];
      ichimokuSeriesRef.current = [];
      chartRef.current = null;
      setZoneTip(null);
      chart.remove();
    };
  }, [height]);

  useEffect(() => {
    if (disposedRef.current) return;
    try {
      if (candleSeriesRef.current && data.length > 0) {
        candleSeriesRef.current.setData(data);
      }
      if (volumeSeriesRef.current && volumeData && volumeData.length > 0) {
        volumeSeriesRef.current.setData(volumeData);
      }
      if (chartRef.current && data.length > 0) {
        chartRef.current.timeScale().fitContent();
        // 预测层/云图未来段开启时：fitContent 会把视口收到最后一根 K 线，
        // 右侧未来区域（概率锥/未来云所在）被挤出画面——刷新后滚出对应
        // 根数的右侧留白（两层同开取较大者）。数据轮询本来就周期性
        // fitContent 重置视口，这里跟随同一节奏，不额外打断用户手动缩放/平移。
        const pred = predictionRef.current;
        const ichi = ichimokuRef.current;
        const offset = Math.max(
          pred ? pred.horizon + 2 : 0,
          ichi && ichi.futureStart !== null ? ichi.displacement + 2 : 0,
        );
        if (offset > 0) {
          chartRef.current.timeScale().scrollToPosition(offset, false);
        }
      }
    } catch {
      // chart may have been disposed between render and effect
    }
  }, [data, volumeData, initVersion]);

  // 实时价补丁：用与顶栏同源的 ticker 报价流式更新最后一根未收线蜡烛
  // （series.update 只动最后一根，不触发 setData/fitContent，不打断用户
  // 缩放/平移）。声明在 setData effect 之后——kline 轮询覆盖后本 effect
  // 依赖 data 重跑，立即把可能滞后（后端 60s 缓存）的 close 再对齐回来。
  useEffect(() => {
    if (disposedRef.current) return;
    const series = candleSeriesRef.current;
    if (!series || !livePrice || data.length === 0) return;
    const last = data[data.length - 1];
    // 报价须不早于最后一根蜡烛的开始时间（防切币/切周期瞬间旧报价错画）
    if (livePrice.timeSec < Number(last.time)) return;
    try {
      series.update({
        time: last.time,
        open: last.open,
        high: Math.max(last.high, livePrice.price),
        low: Math.min(last.low, livePrice.price),
        close: livePrice.price,
      });
    } catch {
      // chart may have been disposed between render and effect
    }
  }, [livePrice, data, initVersion]);

  // 走势预测层：载荷灌进 primitive（内部 requestUpdate 触发重绘）；开启/换一份
  // 新预测（genKey 变化）时滚出右侧未来区域，关闭时回到贴右缘的默认视口。
  // genKey 去重保证同一份预测只调整一次视野，用户随后的缩放/平移不被打断。
  useEffect(() => {
    if (disposedRef.current) return;
    predictionRef.current = prediction ?? null;
    try {
      predictionPrimitiveRef.current?.setPrediction(prediction ?? null);
      const chart = chartRef.current;
      if (!chart || data.length === 0) return;
      if (prediction) {
        if (appliedGenKeyRef.current !== prediction.genKey) {
          appliedGenKeyRef.current = prediction.genKey;
          chart.timeScale().scrollToPosition(prediction.horizon + 2, false);
        }
      } else if (appliedGenKeyRef.current !== null) {
        appliedGenKeyRef.current = null;
        chart.timeScale().scrollToPosition(0, false);
      }
    } catch {
      // chart may have been disposed between render and effect
    }
  }, [prediction, data.length, initVersion]);

  // 一目均衡表云图：三条线（转换/基准/迟行）走 LineSeries，云带（含未来
  // 26 根延伸段）走 primitive。开启时滚出右侧未来云留白（与预测层同款
  // 去重：只在开关翻转时调整一次视野，不打断用户后续缩放/平移）。
  useEffect(() => {
    const chart = chartRef.current;
    if (disposedRef.current || !chart) return;
    ichimokuRef.current = ichimoku ?? null;

    for (const s of ichimokuSeriesRef.current) {
      try {
        chart.removeSeries(s);
      } catch {
        // chart already disposed
      }
    }
    ichimokuSeriesRef.current = [];

    try {
      ichimokuPrimitiveRef.current?.setCloud(
        ichimoku?.cloud ?? [],
        ichimoku?.futureStart ?? null,
      );

      if (ichimoku) {
        const addLine = (
          pts: { time: Time; value: number }[],
          color: string,
          width: LineWidth,
          style: LineStyle,
          title: string,
        ) => {
          if (pts.length < 2) return;
          const ls = chart.addLineSeries({
            color,
            lineWidth: width,
            lineStyle: style,
            priceLineVisible: false,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
            title,
            // 指标线不得撑大蜡烛价格轴（与画线引擎同约束）
            autoscaleInfoProvider: () => null,
          });
          ls.setData(pts);
          ichimokuSeriesRef.current.push(ls);
        };
        addLine(ichimoku.tenkan, ICHIMOKU_LINE_COLORS.tenkan, 1, LineStyle.Solid, "转换线9");
        addLine(ichimoku.kijun, ICHIMOKU_LINE_COLORS.kijun, 2, LineStyle.Solid, "基准线26");
        addLine(ichimoku.chikou, ICHIMOKU_LINE_COLORS.chikou, 1, LineStyle.Dashed, "迟行线");
      }

      // 视野调整只在开关翻转时做一次
      const on = !!ichimoku && ichimoku.futureStart !== null;
      if (on !== ichimokuOnRef.current && data.length > 0) {
        ichimokuOnRef.current = on;
        const pred = predictionRef.current;
        const offset = Math.max(
          pred ? pred.horizon + 2 : 0,
          on && ichimoku ? ichimoku.displacement + 2 : 0,
        );
        chart.timeScale().scrollToPosition(offset, false);
      }
    } catch {
      // chart may have been disposed between render and effect
    }
  }, [ichimoku, data.length, initVersion]);

  // 信号历史盈损标记：L/S 字母徽章（入场）+ 出场圆点，选中信号变化时整组重设；
  // 传空/未传即清空。悬停命中直接查 primitive 的投影结果。
  useEffect(() => {
    if (disposedRef.current) return;
    try {
      marksPrimitiveRef.current?.setMarks(tradeMarks ?? []);
    } catch {
      // chart may have been disposed between render and effect
    }
  }, [tradeMarks, initVersion]);

  // 信号结构买卖点标注 → 原生 setMarkers。替换式 API：prop 变化整组重设，
  // 清空/未传时重设为空数组；库要求按 time 升序，窗口外的标注直接丢弃。
  useEffect(() => {
    if (disposedRef.current) return;
    const series = candleSeriesRef.current;
    if (!series) return;
    try {
      if (!structMarkers || structMarkers.length === 0 || data.length === 0) {
        series.setMarkers([]);
        return;
      }
      const firstTs = Number(data[0].time);
      const lastTs = Number(data[data.length - 1].time);
      const shapeMap: Record<StructureMarker["shape"], SeriesMarkerShape> = {
        arrow_up: "arrowUp",
        arrow_down: "arrowDown",
        circle: "circle",
        square: "square",
      };
      const markers: SeriesMarker<Time>[] = structMarkers
        .filter((m) => {
          const t = Number(m?.ts);
          return Number.isFinite(t) && t >= firstTs && t <= lastTs;
        })
        .sort((a, b) => Number(a.ts) - Number(b.ts))
        .map((m) => ({
          time: Number(m.ts) as Time,
          position: m.position === "above" ? ("aboveBar" as const) : ("belowBar" as const),
          shape: shapeMap[m.shape] ?? "circle",
          color:
            m.color ??
            (m.shape === "arrow_up"
              ? "#3fb950"
              : m.shape === "arrow_down"
                ? "#f85149"
                : "#8b949e"),
          text: m.text,
        }));
      series.setMarkers(markers);
    } catch {
      // chart may have been disposed between render and effect
    }
  }, [structMarkers, data, initVersion]);

  // 信号多空区间图：view + 时间窗口（最新 K 线向右延伸固定根数）灌进独立
  // primitive；切换信号即整体替换（同时只显一个），传 null 即清除。
  useEffect(() => {
    if (disposedRef.current) return;
    try {
      positionZonePrimitiveRef.current?.setZone(
        positionZone ?? null,
        positionZone ? positionZoneWindow(data.length) : null,
      );
    } catch {
      // chart may have been disposed between render and effect
    }
  }, [positionZone, data.length, initVersion]);

  // Overlay pass: smart levels, auto-drawings and external key levels.
  //  - horizontal levels (S/R、fib、现价、外部关键位) → series.createPriceLine
  //  - sloped lines (趋势线、通道) → 2-point LineSeries mapped from bar index
  //  - rectangle box → top/bottom edge segments
  // Everything is cleared and rebuilt when inputs change, so drawings stay in
  // sync with live bars.
  useEffect(() => {
    const chart = chartRef.current;
    const series = candleSeriesRef.current;
    if (disposedRef.current || !chart || !series) return;

    for (const pl of priceLinesRef.current) {
      try {
        series.removePriceLine(pl);
      } catch {
        // series already disposed
      }
    }
    priceLinesRef.current = [];
    for (const s of overlaySeriesRef.current) {
      try {
        chart.removeSeries(s);
      } catch {
        // chart already disposed
      }
    }
    overlaySeriesRef.current = [];

    if (data.length === 0) return;

    const lastIdx = data.length - 1;
    const timeAt = (i: number): Time =>
      data[i < 0 ? 0 : i > lastIdx ? lastIdx : i].time;

    const addPriceLine = (
      price: number,
      color: string,
      title: string,
      style: LineStyle,
      width: LineWidth = 1,
    ) => {
      try {
        const pl = series.createPriceLine({
          price,
          color,
          lineWidth: width,
          lineStyle: style,
          axisLabelVisible: true,
          title,
        });
        priceLinesRef.current.push(pl);
      } catch {
        // series disposed between render and effect
      }
    };

    const addSegment = (
      i1: number,
      p1: number,
      i2: number,
      p2: number,
      color: string,
      width: LineWidth,
      style: LineStyle,
      title?: string,
    ) => {
      if (i1 >= i2) return;
      try {
        const ls = chart.addLineSeries({
          color,
          lineWidth: width,
          lineStyle: style,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          title: title ?? "",
          // Drawings must not stretch the candle price scale.
          autoscaleInfoProvider: () => null,
        });
        ls.setData([
          { time: timeAt(i1), value: p1 },
          { time: timeAt(i2), value: p2 },
        ]);
        overlaySeriesRef.current.push(ls);
      } catch {
        // chart disposed between render and effect
      }
    };

    // 1 · 智能视图：最近强压力（红）、最近强支撑（绿）、现价（白虚线）
    if (smartLevels) {
      if (smartLevels.resistance) {
        const z = smartLevels.resistance;
        addPriceLine(z.level, "#f85149", `压力位 ${fmtPrice(z.level)} · 碰过${z.touches}次`, LineStyle.Solid, 2);
      }
      if (smartLevels.support) {
        const z = smartLevels.support;
        addPriceLine(z.level, "#3fb950", `支撑位 ${fmtPrice(z.level)} · 碰过${z.touches}次`, LineStyle.Solid, 2);
      }
      addPriceLine(smartLevels.price, "#c9d1d9", `现价 ${fmtPrice(smartLevels.price)}`, LineStyle.Dashed, 2);
    }

    if (drawings) {
      // 2 · 水平线型（S/R 聚类、斐波那契）：可靠度已折算进宽度/透明度/label
      for (const hl of drawings.hlines) {
        addPriceLine(
          hl.price,
          withOpacity(hl.color, hl.opacity),
          hl.label ?? "",
          toLineStyle(hl.style),
          clampWidth(hl.width),
        );
      }
      // 3 · 斜线型（趋势线、回归通道）
      for (const seg of drawings.segments) {
        addSegment(
          seg.i1, seg.p1, seg.i2, seg.p2,
          withOpacity(seg.color, seg.opacity),
          clampWidth(seg.width),
          toLineStyle(seg.style),
          seg.label,
        );
      }
      // 4 · 矩形整理区间 → 上下边缘虚线
      for (const b of drawings.bands) {
        addSegment(b.i1, b.top, b.i2, b.top, withOpacity(b.color, b.opacity ?? 0.6), 1, LineStyle.Dashed, b.label);
        addSegment(b.i1, b.bottom, b.i2, b.bottom, withOpacity(b.color, b.opacity ?? 0.6), 1, LineStyle.Dashed);
      }
    }

    // 5 · 外部关键位（十二套技术体系 key_levels）
    if (keyLevels) {
      for (const kl of keyLevels) {
        if (!Number.isFinite(kl.price)) continue;
        addPriceLine(kl.price, kl.color ?? "#8b949e", kl.label, LineStyle.Dotted, clampWidth(kl.width ?? 1));
      }
    }

    // 6 · 交易计划线（入场区 / 止损 / 止盈），与画线引擎 hlines 同构复用 DrawHLine
    if (planLines) {
      for (const pl of planLines) {
        if (!Number.isFinite(pl.price)) continue;
        addPriceLine(
          pl.price,
          withOpacity(pl.color, pl.opacity),
          pl.label ?? "",
          toLineStyle(pl.style),
          clampWidth(pl.width),
        );
      }
    }

    // 7 · 信号结构画线（缠论笔折线 / 中枢框 / 关键水平位）。ts 与 K 线开盘时间
    // 同源，直接作 Time；窗口外的点裁剪掉，防止把共享时间轴撑出可见范围。
    if (structure) {
      const firstTs = Number(data[0].time);
      const lastTs = Number(data[lastIdx].time);

      // 把任意 ts 夹进窗口后吸附到最近 bar 开盘时间（正常契约即命中，兜底容错）
      const snapTs = (ts: number): number => {
        const t = Math.min(Math.max(ts, firstTs), lastTs);
        let lo = 0;
        let hi = lastIdx;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (Number(data[mid].time) < t) lo = mid + 1;
          else hi = mid;
        }
        const cur = Number(data[lo].time);
        const prev = lo > 0 ? Number(data[lo - 1].time) : cur;
        return t - prev <= cur - t ? prev : cur;
      };

      // 时间锚定的水平线段（中枢框上下边缘）；与 addSegment 的 bar 索引口径不同
      const addTimeSegment = (
        t1: number,
        t2: number,
        price: number,
        color: string,
        title?: string,
      ) => {
        if (!(t1 < t2) || !Number.isFinite(price)) return;
        try {
          const ls = chart.addLineSeries({
            color,
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            priceLineVisible: false,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
            title: title ?? "",
            autoscaleInfoProvider: () => null,
          });
          ls.setData([
            { time: t1 as Time, value: price },
            { time: t2 as Time, value: price },
          ]);
          overlaySeriesRef.current.push(ls);
        } catch {
          // chart disposed between render and effect
        }
      };

      // 7a · 笔/线段折线：全点列 LineSeries；窗口外点丢弃，去重保证严格升序
      for (const poly of structure.polylines ?? []) {
        const pts: { time: Time; value: number }[] = [];
        const sorted = [...(poly.points ?? [])]
          .filter((p) => {
            const t = Number(p?.ts);
            const v = Number(p?.price);
            return Number.isFinite(t) && Number.isFinite(v) && t >= firstTs && t <= lastTs;
          })
          .sort((a, b) => Number(a.ts) - Number(b.ts));
        for (const p of sorted) {
          const t = Number(p.ts);
          if (pts.length > 0 && t <= Number(pts[pts.length - 1].time)) continue;
          pts.push({ time: t as Time, value: Number(p.price) });
        }
        if (pts.length < 2) continue;
        try {
          const ls = chart.addLineSeries({
            color: poly.color ?? "#58a6ff",
            lineWidth: clampWidth(poly.width ?? 2),
            lineStyle: poly.style === "dashed" ? LineStyle.Dashed : LineStyle.Solid,
            priceLineVisible: false,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
            title: poly.label ?? "",
            autoscaleInfoProvider: () => null,
          });
          ls.setData(pts);
          overlaySeriesRef.current.push(ls);
        } catch {
          // chart disposed between render and effect
        }
      }

      // 7b · 中枢框：上下两条 dashed 边缘线（仿 bands top/bottom 模式），
      // ts1/ts2 吸附到窗口内最近 bar；label 挂 top 线 title
      for (const b of structure.boxes ?? []) {
        if (![b?.ts1, b?.ts2, b?.price_lo, b?.price_hi].every((v) => Number.isFinite(Number(v)))) {
          continue;
        }
        const t1 = snapTs(Number(b.ts1));
        const t2 = snapTs(Number(b.ts2));
        const color = b.color ?? "#d29922";
        const top = Math.max(Number(b.price_lo), Number(b.price_hi));
        const bottom = Math.min(Number(b.price_lo), Number(b.price_hi));
        addTimeSegment(t1, t2, top, color, b.label);
        addTimeSegment(t1, t2, bottom, color);
      }

      // 7c · 结构关键水平位：与外部关键位（keyLevels 通道）同风格 dotted priceLine
      for (const hl of structure.hlines ?? []) {
        if (!Number.isFinite(Number(hl?.price))) continue;
        addPriceLine(
          Number(hl.price),
          hl.color ?? "#8b949e",
          hl.label ?? "",
          hl.style === "dashed" ? LineStyle.Dashed : LineStyle.Dotted,
          1,
        );
      }
    }
  }, [data, smartLevels, drawings, keyLevels, planLines, structure, initVersion]);

  return (
    <div className="relative">
      <div ref={containerRef} className="w-full rounded-lg overflow-hidden" />
      {zoneTip && (
        <div
          className="pointer-events-none absolute z-10 px-2 py-1 rounded border border-jarvis-border bg-jarvis-card/95 text-xs text-jarvis-text whitespace-nowrap shadow-lg"
          style={{
            left: Math.min(zoneTip.x + 12, (containerRef.current?.clientWidth ?? 0) - 240),
            top: zoneTip.y + 12,
          }}
        >
          {zoneTip.text}
        </div>
      )}
    </div>
  );
}
