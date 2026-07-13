import { clsx } from "clsx";
import { Crown, Users, TrendingUp, TrendingDown, Minus, Zap } from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api, type SentimentBias } from "@/api/client";

const BIAS_META: Record<
  SentimentBias,
  { label: string; cls: string; icon: React.ReactNode }
> = {
  bullish: {
    label: "偏多",
    cls: "bg-jarvis-green/15 text-jarvis-green",
    icon: <TrendingUp size={11} />,
  },
  bearish: {
    label: "偏空",
    cls: "bg-jarvis-red/15 text-jarvis-red",
    icon: <TrendingDown size={11} />,
  },
  neutral: {
    label: "中性",
    cls: "bg-jarvis-border/40 text-jarvis-text-secondary",
    icon: <Minus size={11} />,
  },
};

function SideRow({
  icon,
  label,
  bias,
  longPct,
}: {
  icon: React.ReactNode;
  label: string;
  bias: SentimentBias;
  longPct: number | null;
}) {
  const m = BIAS_META[bias];
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="flex items-center gap-1.5 text-xs text-jarvis-text">
        {icon}
        {label}
      </span>
      <span className="flex items-center gap-1.5">
        {longPct != null && (
          <span className="text-[11px] font-mono text-jarvis-text-secondary">
            多 {longPct.toFixed(1)}%
          </span>
        )}
        <span
          className={clsx(
            "inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium whitespace-nowrap",
            m.cls,
          )}
        >
          {m.icon}
          {m.label}
        </span>
      </span>
    </div>
  );
}

/**
 * 大户 vs 散户背离状态小卡（T1.6）：大户（topLongShortAccountRatio）与全网
 * 多空比反向且占比差超阈值（jarvis_config signal.divergence_threshold）时，
 * 高亮「聪明钱背离」并给出跟随大户的建议倾向；未触发时展示两侧占比供观察。
 */
export default function TopDivergenceCard({ symbol = "BTCUSDT" }: { symbol?: string }) {
  // 与 SentimentPanel 同接口同 TTL（后端 60s 缓存），不产生额外计算
  const { data } = usePolling(() => api.sentiment(symbol), 60_000, [symbol]);
  const td = data?.ok ? (data.top_divergence ?? null) : null;
  const unavailable = !td || !td.available;

  return (
    <div className={clsx("card", unavailable && "opacity-60")}>
      <p className="stat-label mb-3 flex items-center gap-1.5">
        <Crown size={14} />
        大户 vs 散户背离
        {td?.active && (
          <span className="inline-flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded-full bg-jarvis-purple/15 text-jarvis-purple font-medium">
            <Zap size={9} />
            背离中
          </span>
        )}
      </p>

      {unavailable ? (
        <div className="py-4 text-center">
          <p className="text-xs text-jarvis-text-secondary">
            大户多空比数据暂不可用，等待自动恢复
          </p>
        </div>
      ) : (
        <div className="space-y-2.5">
          <SideRow
            icon={<Crown size={13} className="text-jarvis-yellow" />}
            label="大户"
            bias={td.top_bias}
            longPct={td.top_long_pct}
          />
          <SideRow
            icon={<Users size={13} className="text-jarvis-blue" />}
            label="散户(全网)"
            bias={td.retail_bias}
            longPct={td.global_long_pct}
          />

          {/* 背离强度：占比差 vs 触发阈值 */}
          {td.diff_pp != null && (
            <div>
              <div className="flex justify-between text-[10px] mb-1">
                <span className="text-jarvis-text-secondary">背离强度</span>
                <span
                  className={clsx(
                    "font-mono",
                    td.active ? "text-jarvis-purple" : "text-jarvis-text-secondary",
                  )}
                >
                  {td.diff_pp.toFixed(1)}pp / 阈值 {td.threshold_pp.toFixed(0)}pp
                </span>
              </div>
              <div className="h-1.5 bg-jarvis-bg rounded-full overflow-hidden">
                <div
                  className={clsx(
                    "h-full rounded-full transition-all",
                    td.active ? "bg-jarvis-purple" : "bg-jarvis-border",
                  )}
                  style={{
                    width: `${Math.min((td.diff_pp / Math.max(td.threshold_pp, 1)) * 100, 100)}%`,
                  }}
                />
              </div>
            </div>
          )}

          <p
            className={clsx(
              "text-[10px] leading-relaxed rounded px-2 py-1.5",
              td.active
                ? "bg-jarvis-purple/10 text-jarvis-purple"
                : "text-jarvis-text-secondary",
            )}
          >
            {td.suggestion}
          </p>
        </div>
      )}
    </div>
  );
}
