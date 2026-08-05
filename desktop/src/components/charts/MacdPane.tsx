import { useEffect, useMemo, useRef, useState } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type MouseEventParams,
} from "lightweight-charts";
import { clsx } from "clsx";
import { computeMacd } from "@/lib/macd";

interface MacdPaneProps {
  /** 主图同源 K 线（最近窗口即可，需 ≥ slow+signal-1 根才出图） */
  candles: Array<{ time: Time; close: number }>;
  height?: number;
}

const COLOR = {
  dif: "#f0b90b", // 快线·金黄
  dea: "#58a6ff", // 慢线·蓝
  histUp: "rgba(63, 185, 80, 0.65)",
  histDown: "rgba(248, 81, 73, 0.65)",
} as const;

function fmtVal(v: number): string {
  const a = Math.abs(v);
  if (a >= 100) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  return v.toFixed(4);
}

/**
 * MACD 指标副图（12/26/9，K 线图下方独立图实例，模式同 DeltaPane）：
 *   柱体 = DIF − DEA（正绿负红），金黄线 = DIF 快线，蓝线 = DEA 慢线。
 * 由主图 K 线本地计算，无额外请求；EMA 收敛期（前 33 根）自动裁剪不展示。
 */
export default function MacdPane({ candles, height = 170 }: MacdPaneProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const histSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const difSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const deaSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const [hover, setHover] = useState<string | null>(null);

  // MACD 本地计算：裁掉 EMA 收敛期，输出带时间戳的渲染点
  const points = useMemo(() => {
    const { points: raw, warmupIndex } = computeMacd(candles.map((c) => c.close));
    if (warmupIndex < 0) return [];
    return raw.slice(warmupIndex).map((p) => ({
      time: candles[p.index].time,
      dif: p.dif,
      dea: p.dea,
      hist: p.hist,
    }));
  }, [candles]);

  // 初始化图实例（一次）
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      width: el.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#8b949e",
        fontSize: 10,
      },
      grid: {
        vertLines: { color: "rgba(48, 54, 61, 0.4)" },
        horzLines: { color: "rgba(48, 54, 61, 0.4)" },
      },
      crosshair: { mode: CrosshairMode.Magnet },
      rightPriceScale: { borderColor: "#30363d" },
      timeScale: { borderColor: "#30363d", timeVisible: true, secondsVisible: false },
    });
    const histSeries = chart.addHistogramSeries({
      priceFormat: { type: "price", precision: 4, minMove: 0.0001 },
      priceScaleId: "right",
      base: 0,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    const difSeries = chart.addLineSeries({
      color: COLOR.dif,
      lineWidth: 1,
      priceScaleId: "right",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    const deaSeries = chart.addLineSeries({
      color: COLOR.dea,
      lineWidth: 1,
      priceScaleId: "right",
      lastValueVisible: false,
      priceLineVisible: false,
    });

    chartRef.current = chart;
    histSeriesRef.current = histSeries;
    difSeriesRef.current = difSeries;
    deaSeriesRef.current = deaSeries;

    const onResize = () => chart.applyOptions({ width: el.clientWidth });
    const ro = new ResizeObserver(onResize);
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      histSeriesRef.current = null;
      difSeriesRef.current = null;
      deaSeriesRef.current = null;
    };
  }, [height]);

  // 数据更新
  useEffect(() => {
    const chart = chartRef.current;
    const histSeries = histSeriesRef.current;
    const difSeries = difSeriesRef.current;
    const deaSeries = deaSeriesRef.current;
    if (!chart || !histSeries || !difSeries || !deaSeries) return;

    histSeries.setData(
      points.map((p) => ({
        time: p.time,
        value: p.hist,
        color: p.hist >= 0 ? COLOR.histUp : COLOR.histDown,
      })),
    );
    difSeries.setData(points.map((p) => ({ time: p.time, value: p.dif })));
    deaSeries.setData(points.map((p) => ({ time: p.time, value: p.dea })));
    chart.timeScale().fitContent();
  }, [points]);

  // 悬停提示
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const byTime = new Map(points.map((p) => [p.time as number, p]));
    const onMove = (param: MouseEventParams) => {
      const t = param.time as number | undefined;
      const p = t != null ? byTime.get(t) : undefined;
      if (!p) {
        setHover(null);
        return;
      }
      const dt = new Date((p.time as number) * 1000).toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      });
      setHover(
        `${dt} · DIF ${fmtVal(p.dif)} · DEA ${fmtVal(p.dea)} · MACD ${p.hist >= 0 ? "+" : ""}${fmtVal(p.hist)}`,
      );
    };
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [points]);

  // 尾部动能简报：柱体方向 + 金叉/死叉状态
  const brief = useMemo(() => {
    if (points.length < 2) return null;
    const last = points[points.length - 1];
    const prev = points[points.length - 2];
    const cross =
      prev.dif <= prev.dea && last.dif > last.dea
        ? "金叉"
        : prev.dif >= prev.dea && last.dif < last.dea
          ? "死叉"
          : last.dif > last.dea
            ? "多头排列"
            : "空头排列";
    const momentum = Math.abs(last.hist) >= Math.abs(prev.hist) ? "动能增强" : "动能衰减";
    return { cross, momentum, bullish: last.dif > last.dea };
  }, [points]);

  const notEnough = candles.length > 0 && points.length === 0;

  return (
    <div className="card p-0 overflow-hidden">
      <div className="flex items-center justify-between gap-2 px-3 pt-2 flex-wrap">
        <p className="stat-label mb-0 flex items-center gap-2">
          MACD（12/26/9）
          {brief && (
            <span
              className={clsx(
                "text-[9px] px-1.5 py-0.5 rounded border font-medium",
                brief.bullish
                  ? "bg-jarvis-green/10 text-jarvis-green border-jarvis-green/40"
                  : "bg-jarvis-red/10 text-jarvis-red border-jarvis-red/40",
              )}
            >
              {brief.cross} · {brief.momentum}
            </span>
          )}
        </p>
        <span className="text-[10px] text-jarvis-text-secondary font-mono truncate">
          {hover ?? "柱体 = DIF−DEA（正绿负红）；金线 = DIF 快线；蓝线 = DEA 慢线"}
        </span>
      </div>
      {notEnough ? (
        <div className="px-3 py-6 text-center text-xs text-jarvis-text-secondary">
          K 线数量不足（需 ≥ 34 根），暂无法计算 MACD
        </div>
      ) : null}
      <div ref={containerRef} className={clsx(notEnough && "hidden")} />
    </div>
  );
}
