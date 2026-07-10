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
  type Time,
} from "lightweight-charts";
import type { DrawingResult, SmartLevels, KeyLevel, DrawLineStyle, DrawHLine } from "@/lib/drawings";
import { zoneTimeWindow, type TradeZone } from "@/lib/tradeZones";
import type { TradeMark } from "@/lib/signalTrades";
import { TradeZonesPrimitive } from "./TradeZonesPrimitive";
import { TradeMarkersPrimitive } from "./TradeMarkersPrimitive";

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
  /** 交易区间带（入场/止损/止盈时间锚定矩形），由 planToZones 生成；悬停显示盈亏比。 */
  tradeZones?: TradeZone[];
  /** 信号历史盈损标记（L/S 字母徽章 + 出场圆点，盈✓亏✕），由 tradesToMarks 生成。 */
  tradeMarks?: TradeMark[];
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
  tradeZones,
  tradeMarks,
}: KlineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const overlaySeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const zonesPrimitiveRef = useRef<TradeZonesPrimitive | null>(null);
  const marksPrimitiveRef = useRef<TradeMarkersPrimitive | null>(null);
  const disposedRef = useRef(false);
  // 悬停在区间带上时的浮层文案（含盈亏比）；null = 隐藏
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

    // 交易区间带 primitive：zOrder "bottom"，垫在蜡烛下方；信号盈损 L/S 徽章
    // primitive：zOrder "top"，浮在蜡烛上方。缩放/平移/切周期由库的
    // updateAllViews 回调驱动重投影（价格→y、时间/bar 索引→x），天然不错位。
    const zonesPrimitive = new TradeZonesPrimitive();
    candleSeries.attachPrimitive(zonesPrimitive);
    const marksPrimitive = new TradeMarkersPrimitive();
    candleSeries.attachPrimitive(marksPrimitive);

    // 悬停浮层（两类命中共用一个浮层，徽章优先）：
    //   1. 信号盈损 L/S 徽章 / 出场圆点：像素坐标命中测试 → 每笔详情
    //   2. 交易区间带：时间锚定矩形命中测试 → 区间详情（含盈亏比）
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
      const hit = zonesPrimitive.zoneAt(pt.x, pt.y);
      setZoneTip(hit ? { x: pt.x, y: pt.y, text: hit.tooltip } : null);
    };
    chart.subscribeCrosshairMove(handleCrosshair);

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    zonesPrimitiveRef.current = zonesPrimitive;
    marksPrimitiveRef.current = marksPrimitive;
    priceLinesRef.current = [];
    overlaySeriesRef.current = [];
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
      zonesPrimitiveRef.current = null;
      marksPrimitiveRef.current = null;
      priceLinesRef.current = [];
      overlaySeriesRef.current = [];
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
      }
    } catch {
      // chart may have been disposed between render and effect
    }
  }, [data, volumeData, initVersion]);

  // 交易区间带：把 zones + 时间窗口（覆盖最近 N 根 K 线 + 右侧延伸）灌进
  // primitive（内部 requestUpdate 触发重绘）。关闭开关 / 计划消失时传空数组
  // 即清空；窗口随 K 线根数变化滑动（新 K 线收线后矩形右移跟进）。
  useEffect(() => {
    if (disposedRef.current) return;
    try {
      zonesPrimitiveRef.current?.setZones(tradeZones ?? [], zoneTimeWindow(data.length));
    } catch {
      // chart may have been disposed between render and effect
    }
    setZoneTip(null);
  }, [tradeZones, data.length, initVersion]);

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
  }, [data, smartLevels, drawings, keyLevels, planLines, initVersion]);

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
