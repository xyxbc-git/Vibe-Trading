import { useEffect, useRef, useState } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type MouseEventParams,
} from "lightweight-charts";
import { clsx } from "clsx";
import {
  DELTA_COLORS,
  strengthGrade,
  type DeltaResponse,
} from "@/lib/deltaFlow";

interface DeltaPaneProps {
  resp: DeltaResponse | null;
  loading: boolean;
  error: string | null;
  height?: number;
}

function fmtNum(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return v.toFixed(2);
}

/**
 * Delta/CVD 订单流副图（K 线图下方，独立图实例）：
 *   - 每根 Delta 柱状图（正绿负红）+ CVD 累计曲线（紫）
 *   - 吸收/派发背离激活时：在 CVD 曲线上画锚点连线 + 「吸收背离 · strong」标注
 *   - 悬停显示该根 Delta/CVD/成交量；mock 数据带「演示数据」角标
 * 独立于主图（PredictionPrimitive / PositionZonePrimitive 均不受影响）。
 */
export default function DeltaPane({ resp, loading, error, height = 170 }: DeltaPaneProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const deltaSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const cvdSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const divSeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const [hover, setHover] = useState<string | null>(null);

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
    const deltaSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "right",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    // CVD 用独立 overlay 价格轴：量纲与单根 Delta 不同（累计值），共轴会压扁柱体
    const cvdSeries = chart.addLineSeries({
      color: DELTA_COLORS.cvd,
      lineWidth: 2,
      priceScaleId: "cvd",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("cvd").applyOptions({ visible: false });

    chartRef.current = chart;
    deltaSeriesRef.current = deltaSeries;
    cvdSeriesRef.current = cvdSeries;

    const onResize = () => chart.applyOptions({ width: el.clientWidth });
    const ro = new ResizeObserver(onResize);
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      deltaSeriesRef.current = null;
      cvdSeriesRef.current = null;
      divSeriesRef.current = [];
    };
  }, [height]);

  // 数据更新
  useEffect(() => {
    const chart = chartRef.current;
    const deltaSeries = deltaSeriesRef.current;
    const cvdSeries = cvdSeriesRef.current;
    if (!chart || !deltaSeries || !cvdSeries) return;

    const bars = resp?.bars ?? [];
    deltaSeries.setData(
      bars.map((b) => ({
        time: b.t as Time,
        value: b.delta,
        color: b.delta >= 0 ? "rgba(63, 185, 80, 0.65)" : "rgba(248, 81, 73, 0.65)",
      })),
    );
    cvdSeries.setData(bars.map((b) => ({ time: b.t as Time, value: b.cvd })));

    // 背离锚点连线（画在 CVD 轴上）：先清旧线
    for (const s of divSeriesRef.current) chart.removeSeries(s);
    divSeriesRef.current = [];
    for (const side of ["bullish", "bearish"] as const) {
      const d = resp?.divergence?.[side];
      const anchors = d?.anchors ?? [];
      if (!d?.active || anchors.length < 2) continue;
      const line = chart.addLineSeries({
        color: DELTA_COLORS.divergence,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        priceScaleId: "cvd",
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
      });
      line.setData(
        [...anchors]
          .sort((a, b) => a.t - b.t)
          .map((a) => ({ time: a.t as Time, value: a.cvd })),
      );
      divSeriesRef.current.push(line);
    }
    chart.timeScale().fitContent();
  }, [resp]);

  // 悬停提示
  useEffect(() => {
    const chart = chartRef.current;
    const deltaSeries = deltaSeriesRef.current;
    const cvdSeries = cvdSeriesRef.current;
    if (!chart || !deltaSeries || !cvdSeries) return;
    const bars = resp?.bars ?? [];
    const byTime = new Map(bars.map((b) => [b.t, b]));
    const onMove = (param: MouseEventParams) => {
      const t = param.time as number | undefined;
      const b = t != null ? byTime.get(t) : undefined;
      if (!b) {
        setHover(null);
        return;
      }
      const dt = new Date(b.t * 1000).toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      });
      setHover(`${dt} · Delta ${b.delta >= 0 ? "+" : ""}${fmtNum(b.delta)} · CVD ${fmtNum(b.cvd)} · 量 ${fmtNum(b.volume)}`);
    };
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [resp]);

  const activeDivs = (["bullish", "bearish"] as const).filter(
    (s) => resp?.divergence?.[s]?.active,
  );

  return (
    <div className="card p-0 overflow-hidden">
      <div className="flex items-center justify-between gap-2 px-3 pt-2 flex-wrap">
        <p className="stat-label mb-0 flex items-center gap-2">
          Delta / CVD 订单流
          {resp?.mock && (
            <span
              className="text-[9px] px-1.5 py-0.5 rounded border font-medium bg-jarvis-yellow/10 text-jarvis-yellow border-jarvis-yellow/40"
              title={resp.disclaimer ?? "Delta 引擎未就绪，当前为 K 线近似演示推演"}
            >
              演示数据
            </span>
          )}
          {activeDivs.map((side) => {
            const d = resp!.divergence[side];
            const grade = strengthGrade(d.strength);
            const cn = side === "bullish" ? "吸收背离（看涨）" : "派发背离（看跌）";
            return (
              <span
                key={side}
                title={d.note ?? cn}
                className={clsx(
                  "text-[9px] px-1.5 py-0.5 rounded border font-medium",
                  side === "bullish"
                    ? "bg-jarvis-green/10 text-jarvis-green border-jarvis-green/40"
                    : "bg-jarvis-red/10 text-jarvis-red border-jarvis-red/40",
                )}
              >
                {cn} · {grade}
              </span>
            );
          })}
        </p>
        <span className="text-[10px] text-jarvis-text-secondary font-mono truncate">
          {hover ?? "Delta 柱 = 主动买卖差（正绿负红）；紫线 = CVD 累计；黄虚线 = 背离锚点连线"}
        </span>
      </div>
      {error && !resp ? (
        <div className="px-3 py-6 text-center text-xs text-jarvis-text-secondary">{error}</div>
      ) : loading && !resp ? (
        <div className="px-3 py-6 text-center text-xs text-jarvis-text-secondary">
          正在获取订单流数据...
        </div>
      ) : null}
      <div ref={containerRef} className={clsx((error || loading) && !resp && "hidden")} />
    </div>
  );
}
