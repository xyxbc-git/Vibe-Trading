import { useState, useMemo, useEffect } from "react";
import { usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import { api } from "@/api/client";
import KlineChart from "@/components/charts/KlineChart";
import { computeSmartLevels, computeBias, type SmartBias } from "@/lib/smartLevels";
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
  const [smart, setSmart] = useState(true);
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

  // A · 纯几何方向：现价相对支撑/压力的位置 → 偏多/偏空/观望（双向）
  const smartBias = useMemo<SmartBias | null>(() => {
    if (!smart || candles.length < 20) return null;
    const highs = candles.map((c) => c.high);
    const lows = candles.map((c) => c.low);
    const closes = candles.map((c) => c.close);
    return computeBias(computeSmartLevels(highs, lows, closes));
  }, [smart, candles]);

  // B · AI 决策方向：拉 /actions/brief 的 偏多/偏空/中性（含信心分、建议仓位）
  type AiDir = { label: string; dir: "long" | "short" | "neutral"; score: number; pos: number };
  const [aiDir, setAiDir] = useState<AiDir | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiErr, setAiErr] = useState<string | null>(null);

  // Stale once the symbol changes — clear the previous symbol's decision.
  useEffect(() => {
    setAiDir(null);
    setAiErr(null);
  }, [symbol]);

  const loadBrief = async () => {
    setAiLoading(true);
    setAiErr(null);
    try {
      const res = (await api.actionBrief(symbol)) as {
        ok?: boolean;
        data?: { decision?: Record<string, unknown> };
        error?: string;
      };
      const dec = res?.data?.decision;
      if (!dec) {
        setAiErr(res?.error ?? "无决策数据");
        setAiDir(null);
        return;
      }
      const direction = String(dec.direction ?? "");
      const dir: AiDir["dir"] = direction.startsWith("偏多")
        ? "long"
        : direction.startsWith("偏空")
          ? "short"
          : "neutral";
      setAiDir({
        label: direction || "中性观望",
        dir,
        score: Number(dec.conviction_score ?? 0),
        pos: Number(dec.suggested_position_pct ?? 0),
      });
    } catch (e) {
      setAiErr(e instanceof Error ? e.message : String(e));
      setAiDir(null);
    } finally {
      setAiLoading(false);
    }
  };

  const biasCls = (dir: string) =>
    clsx(
      "px-2 py-1 rounded text-sm font-medium border",
      dir === "short"
        ? "text-jarvis-red border-jarvis-red"
        : dir === "long"
          ? "text-jarvis-green border-jarvis-green"
          : "text-jarvis-text-secondary border-jarvis-border",
    );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="page-title flex items-center gap-2 mb-0">
          <CandlestickChart size={22} />
          {symbol.replace("USDT", "/USDT")}
        </h1>
        <div className="flex items-center gap-2 flex-wrap justify-end">
          <button
            onClick={() => setSmart((v) => !v)}
            title="智能：在图上标注离现价最近的压力位、支撑位和现价，一眼看懂"
            className={clsx(
              "px-3 py-1 text-sm rounded-md border transition-colors",
              smart
                ? "bg-jarvis-blue/15 border-jarvis-blue text-jarvis-blue"
                : "bg-jarvis-card border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
            )}
          >
            智能 {smart ? "·开" : "·关"}
          </button>

          {/* A · 几何方向（双向，含明确做空提示），随智能视图自动出 */}
          {smart && smartBias && (
            <span className={biasCls(smartBias.dir)} title={smartBias.detail}>
              {smartBias.dir === "short" ? "▼ " : smartBias.dir === "long" ? "▲ " : "= "}
              {smartBias.label} · {smartBias.detail}
            </span>
          )}

          {/* B · AI 决策方向（按需拉 brief，含偏空） */}
          <button
            onClick={loadBrief}
            disabled={aiLoading}
            title="拉取 AI 决策简报：偏多 / 偏空 / 中性观望（含信心分与建议仓位）"
            className={clsx(
              "px-3 py-1 text-sm rounded-md border transition-colors",
              "bg-jarvis-card border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
              aiLoading && "opacity-60 cursor-wait",
            )}
          >
            {aiLoading ? "AI 决策…" : "AI 决策"}
          </button>
          {aiDir && (
            <span className={biasCls(aiDir.dir)} title={`AI 决策：${aiDir.label}`}>
              {aiDir.dir === "short" ? "▼ " : aiDir.dir === "long" ? "▲ " : "= "}
              {aiDir.label} · 信心 {aiDir.score} · 仓位 {aiDir.pos}%
            </span>
          )}
          {aiErr && (
            <span className="px-2 py-1 rounded text-sm text-jarvis-yellow" title={aiErr}>
              AI 决策失败
            </span>
          )}

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
      </div>

      <div className="card p-0 overflow-hidden">
        {candles.length > 0 ? (
          <KlineChart
            data={candles}
            volumeData={volumes}
            height={Math.max(400, window.innerHeight - 280)}
            showSmart={smart}
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
