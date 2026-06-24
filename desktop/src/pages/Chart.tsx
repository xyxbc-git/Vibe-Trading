import { useState, useMemo } from "react";
import { usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import { api } from "@/api/client";
import KlineChart from "@/components/charts/KlineChart";
import { CandlestickChart } from "lucide-react";
import { clsx } from "clsx";
import type {
  CandlestickData,
  HistogramData,
  Time,
} from "lightweight-charts";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"] as const;
type Timeframe = (typeof TIMEFRAMES)[number];

const LIMITS: Record<Timeframe, number> = {
  "1m": 300,
  "5m": 200,
  "15m": 200,
  "1h": 168,
  "4h": 200,
  "1d": 180,
};

export default function Chart() {
  const [tf, setTf] = useState<Timeframe>("15m");
  const { symbol } = useSymbol();

  const { data: rawKline, loading, error } = usePolling(
    () => api.kline(symbol, tf, LIMITS[tf]),
    tf === "1m" ? 10_000 : 60_000,
    [tf, symbol],
  );

  const { candles, volumes } = useMemo(() => {
    const rows = (rawKline as Record<string, unknown>)?.rows;
    if (!rawKline || !Array.isArray(rows)) {
      return { candles: [] as CandlestickData<Time>[], volumes: [] as HistogramData<Time>[] };
    }
    const c: CandlestickData<Time>[] = [];
    const v: HistogramData<Time>[] = [];
    for (const k of rows as Record<string, number>[]) {
      const time = (k.ts / 1000) as Time;
      c.push({
        time,
        open: k.o,
        high: k.h,
        low: k.l,
        close: k.c,
      });
      v.push({
        time,
        value: k.v ?? 0,
        color:
          k.c >= k.o
            ? "rgba(63, 185, 80, 0.3)"
            : "rgba(248, 81, 73, 0.3)",
      });
    }
    return { candles: c, volumes: v };
  }, [rawKline]);

  const lastCandle = candles.length > 0 ? candles[candles.length - 1] : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="page-title flex items-center gap-2 mb-0">
          <CandlestickChart size={22} />
          {symbol.replace("USDT", "/USDT")}
        </h1>
        <div className="flex gap-1 bg-jarvis-card border border-jarvis-border rounded-lg p-1">
          {TIMEFRAMES.map((t) => (
            <button
              key={t}
              onClick={() => setTf(t)}
              className={clsx(
                "px-3 py-1 text-sm rounded-md transition-colors",
                t === tf
                  ? "bg-jarvis-blue text-white"
                  : "text-jarvis-text-secondary hover:text-jarvis-text",
              )}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      <div className="card p-0 overflow-hidden">
        {candles.length > 0 ? (
          <KlineChart
            data={candles}
            volumeData={volumes}
            height={Math.max(400, window.innerHeight - 280)}
          />
        ) : (
          <div
            className="flex flex-col items-center justify-center text-jarvis-text-secondary"
            style={{ height: Math.max(400, window.innerHeight - 280) }}
          >
            {loading ? (
              <>
                <div className="w-6 h-6 border-2 border-jarvis-blue border-t-transparent rounded-full animate-spin mb-3" />
                <p className="text-sm">正在获取 K 线数据...</p>
                <p className="text-xs mt-1">首次加载可能需要 10-30 秒</p>
              </>
            ) : error ? (
              <>
                <p className="text-sm text-jarvis-yellow mb-1">数据获取失败</p>
                <p className="text-xs">{error}</p>
                <p className="text-xs mt-2">可能原因：Binance API 不可达（需科学上网）</p>
              </>
            ) : (
              <p className="text-sm">暂无 K 线数据</p>
            )}
          </div>
        )}
      </div>

      {lastCandle && (
        <div className="flex gap-6 text-sm text-jarvis-text-secondary px-1">
          <span>
            开:{" "}
            <span className="text-jarvis-text font-mono">
              {lastCandle.open.toLocaleString()}
            </span>
          </span>
          <span>
            高:{" "}
            <span className="text-jarvis-text font-mono">
              {lastCandle.high.toLocaleString()}
            </span>
          </span>
          <span>
            低:{" "}
            <span className="text-jarvis-text font-mono">
              {lastCandle.low.toLocaleString()}
            </span>
          </span>
          <span>
            收:{" "}
            <span
              className={clsx("font-mono", {
                "text-jarvis-green": lastCandle.close >= lastCandle.open,
                "text-jarvis-red": lastCandle.close < lastCandle.open,
              })}
            >
              {lastCandle.close.toLocaleString()}
            </span>
          </span>
        </div>
      )}
    </div>
  );
}
