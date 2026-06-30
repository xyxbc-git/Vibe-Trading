import { useEffect, useRef } from "react";
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type IPriceLine,
  ColorType,
  CrosshairMode,
  LineStyle,
  type CandlestickData,
  type HistogramData,
  type Time,
} from "lightweight-charts";
import { computeSmartLevels } from "@/lib/smartLevels";

interface KlineChartProps {
  data: CandlestickData<Time>[];
  volumeData?: HistogramData<Time>[];
  height?: number;
  // When true, overlay beginner-friendly support/resistance/现价 markers.
  showSmart?: boolean;
}

function fmtPrice(v: number): string {
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

export default function KlineChart({
  data,
  volumeData,
  height = 500,
  showSmart = true,
}: KlineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const disposedRef = useRef(false);

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

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;

    const handleResize = () => {
      if (!disposedRef.current && containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", handleResize);

    return () => {
      disposedRef.current = true;
      window.removeEventListener("resize", handleResize);
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      chartRef.current = null;
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
  }, [data, volumeData]);

  // Beginner-friendly smart overlay: only the nearest strong resistance (red),
  // nearest strong support (green) and the current price (white), each drawn as
  // a single labelled price line. Recomputes as bars grow. Mirrors the web side.
  useEffect(() => {
    const series = candleSeriesRef.current;
    if (disposedRef.current || !series) return;

    for (const pl of priceLinesRef.current) {
      try {
        series.removePriceLine(pl);
      } catch {
        // series already disposed
      }
    }
    priceLinesRef.current = [];

    if (!showSmart || data.length < 20) return;

    const highs = data.map((d) => d.high);
    const lows = data.map((d) => d.low);
    const closes = data.map((d) => d.close);
    const levels = computeSmartLevels(highs, lows, closes);

    const add = (price: number, color: string, title: string, dashed = false) => {
      try {
        const pl = series.createPriceLine({
          price,
          color,
          lineWidth: 2,
          lineStyle: dashed ? LineStyle.Dashed : LineStyle.Solid,
          axisLabelVisible: true,
          title,
        });
        priceLinesRef.current.push(pl);
      } catch {
        // series disposed between render and effect
      }
    };

    if (levels.resistance) {
      const z = levels.resistance;
      add(z.level, "#f85149", `压力位 ${fmtPrice(z.level)} · 碰过${z.touches}次`);
    }
    if (levels.support) {
      const z = levels.support;
      add(z.level, "#3fb950", `支撑位 ${fmtPrice(z.level)} · 碰过${z.touches}次`);
    }
    add(levels.price, "#c9d1d9", `现价 ${fmtPrice(levels.price)}`, true);
  }, [data, showSmart]);

  return <div ref={containerRef} className="w-full rounded-lg overflow-hidden" />;
}
