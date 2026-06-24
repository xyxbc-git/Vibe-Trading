import { useEffect, useRef } from "react";
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  ColorType,
  CrosshairMode,
  type CandlestickData,
  type HistogramData,
  type Time,
} from "lightweight-charts";

interface KlineChartProps {
  data: CandlestickData<Time>[];
  volumeData?: HistogramData<Time>[];
  height?: number;
}

export default function KlineChart({
  data,
  volumeData,
  height = 500,
}: KlineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
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

  return <div ref={containerRef} className="w-full rounded-lg overflow-hidden" />;
}
