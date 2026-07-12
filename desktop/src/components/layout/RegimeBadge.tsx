import { clsx } from "clsx";
import { usePolling } from "@/hooks/useApi";
import { api, type MarketRegime } from "@/api/client";
import { useSymbol } from "@/hooks/useSymbol";

const REGIME_META: Record<
  MarketRegime,
  { emoji: string; label: string; cls: string; dot: string }
> = {
  bull: {
    emoji: "🐂",
    label: "牛市",
    cls: "border-jarvis-green/50 text-jarvis-green bg-jarvis-green/10",
    dot: "bg-jarvis-green",
  },
  bear: {
    emoji: "🐻",
    label: "熊市",
    cls: "border-jarvis-red/50 text-jarvis-red bg-jarvis-red/10",
    dot: "bg-jarvis-red",
  },
  range: {
    emoji: "↔",
    label: "震荡",
    cls: "border-jarvis-yellow/50 text-jarvis-yellow bg-jarvis-yellow/10",
    dot: "bg-jarvis-yellow",
  },
};

/**
 * 顶部栏牛熊体制徽标（独立组件增量挂载，不依赖顶栏价格链路）。
 * 后端 /api/regime 缓存 15min，前端 5min 轮询足够；hover 展开得分与主因子明细。
 */
export default function RegimeBadge() {
  const { symbol } = useSymbol();
  const { data } = usePolling(() => api.regime(symbol), 300_000, [symbol]);

  if (!data?.ok || !data.regime) return null;

  const meta = REGIME_META[data.regime];
  const score = data.score ?? 0;
  const factors = (data.factors ?? []).filter((f) => f.available);

  return (
    <div className="relative group">
      <span
        className={clsx(
          "flex items-center gap-1.5 h-7 px-2.5 rounded-full border text-xs font-medium cursor-default whitespace-nowrap",
          meta.cls,
        )}
      >
        <span className="text-[13px] leading-none">{meta.emoji}</span>
        {data.regime_cn ?? meta.label}
        <span className="font-mono text-[10px] opacity-80">
          {score > 0 ? "+" : ""}
          {score.toFixed(0)}
        </span>
      </span>

      {/* hover 明细面板 */}
      <div
        className="absolute right-0 top-full mt-2 w-72 p-3 rounded-lg bg-jarvis-card border border-jarvis-border shadow-xl
                   opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto
                   transition-opacity z-50"
      >
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-jarvis-text">
            大周期体制 · {data.symbol?.replace("USDT", "") ?? ""}
          </span>
          <span className="text-[10px] text-jarvis-text-secondary font-mono">
            置信 {((data.confidence ?? 0) * 100).toFixed(0)}%
          </span>
        </div>
        {data.headline && (
          <p className="text-[11px] text-jarvis-text leading-relaxed mb-2">
            {data.headline}
          </p>
        )}
        <div className="space-y-1">
          {factors.map((f) => (
            <div key={f.key} className="flex items-center justify-between gap-2">
              <span className="text-[10px] text-jarvis-text-secondary whitespace-nowrap">
                {f.name}
              </span>
              <span className="flex items-center gap-1.5 min-w-0">
                <span className="text-[10px] text-jarvis-text-secondary font-mono truncate">
                  {f.display}
                </span>
                <span
                  className={clsx(
                    "text-[10px] font-mono flex-shrink-0",
                    f.score > 0
                      ? "text-jarvis-green"
                      : f.score < 0
                        ? "text-jarvis-red"
                        : "text-jarvis-text-secondary",
                  )}
                >
                  {f.score > 0 ? "+" : ""}
                  {f.score.toFixed(0)}
                </span>
              </span>
            </div>
          ))}
        </div>
        <p className="text-[9px] text-jarvis-text-secondary/70 leading-relaxed mt-2 pt-2 border-t border-jarvis-border">
          详情见「市场情报」页体制卡片；判定存在滞后性，不构成投资建议。
        </p>
      </div>
    </div>
  );
}
