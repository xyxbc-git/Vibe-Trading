import { clsx } from "clsx";
import { Sparkles, AlertTriangle, Info, Siren } from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api, type JarvisInsight } from "@/api/client";

const SEVERITY_META: Record<
  string,
  { text: string; border: string; icon: React.ReactNode }
> = {
  critical: {
    text: "text-jarvis-red",
    border: "border-l-jarvis-red",
    icon: <Siren size={13} />,
  },
  warning: {
    text: "text-jarvis-yellow",
    border: "border-l-jarvis-yellow",
    icon: <AlertTriangle size={13} />,
  },
  info: {
    text: "text-jarvis-blue",
    border: "border-l-jarvis-blue",
    icon: <Info size={13} />,
  },
};

function severityMeta(sev: string) {
  // 兼容历史数据：后端曾产 "warn"，统一映射到 warning 样式
  const key = sev === "warn" ? "warning" : sev;
  return SEVERITY_META[key] ?? SEVERITY_META.info;
}

function fmtTime(ts: string | number): string {
  const d =
    typeof ts === "number"
      ? new Date(ts > 1e12 ? ts : ts * 1000)
      : new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const now = Date.now();
  const diffMin = Math.floor((now - d.getTime()) / 60_000);
  if (diffMin < 1) return "刚刚";
  if (diffMin < 60) return `${diffMin} 分钟前`;
  if (diffMin < 24 * 60) return `${Math.floor(diffMin / 60)} 小时前`;
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function sortDesc(items: JarvisInsight[]): JarvisInsight[] {
  const toMs = (ts: string | number) => {
    const d =
      typeof ts === "number"
        ? new Date(ts > 1e12 ? ts : ts * 1000)
        : new Date(ts);
    return Number.isNaN(d.getTime()) ? 0 : d.getTime();
  };
  return items.slice().sort((a, b) => toMs(b.ts) - toMs(a.ts));
}

/** 贾维斯主动洞察流：预测提醒 / 风险警示，按 severity 着色，时间倒序 */
export default function InsightFeed({ limit = 20 }: { limit?: number }) {
  const { data, loading, error } = usePolling(
    () => api.jarvisInsights(limit),
    30_000,
    [limit],
  );

  // 封套：{ok, insights:[...], total}；ok:false 视同引擎未就绪
  const failed = Boolean(error) || (data != null && !data.ok);
  const insights = sortDesc(
    data?.ok && Array.isArray(data.insights) ? data.insights : [],
  );

  return (
    <div className="card flex flex-col min-h-0">
      <p className="stat-label flex items-center gap-2 mb-3">
        <Sparkles size={14} />
        贾维斯主动洞察
      </p>

      {failed ? (
        <div className="flex-1 flex flex-col items-center justify-center py-8 gap-1">
          <p className="text-sm text-jarvis-text-secondary">洞察引擎未启动</p>
          <p className="text-xs text-jarvis-text-secondary/70">
            {data && !data.ok && data.error
              ? String(data.error)
              : "等待后端 /api/jarvis/insights 就绪后自动恢复"}
          </p>
        </div>
      ) : loading && !data ? (
        <div className="space-y-2.5 animate-pulse">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-14 rounded-lg bg-jarvis-border/30" />
          ))}
        </div>
      ) : insights.length === 0 ? (
        <p className="text-sm text-jarvis-text-secondary py-8 text-center">
          暂无洞察 · 贾维斯发现异动时会在这里主动汇报
        </p>
      ) : (
        <div className="space-y-2 overflow-y-auto max-h-[340px] pr-1">
          {insights.map((it, i) => {
            const meta = severityMeta(String(it.severity));
            return (
              <div
                key={`${it.ts}-${i}`}
                className={clsx(
                  "border-l-2 bg-jarvis-bg rounded-r-lg px-3 py-2",
                  meta.border,
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span
                    className={clsx(
                      "flex items-center gap-1.5 text-xs font-medium min-w-0",
                      meta.text,
                    )}
                  >
                    {meta.icon}
                    <span className="truncate text-jarvis-text">
                      {it.title}
                    </span>
                  </span>
                  <span className="text-[10px] text-jarvis-text-secondary whitespace-nowrap">
                    {fmtTime(it.ts)}
                  </span>
                </div>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-card border border-jarvis-border text-jarvis-text-secondary font-mono">
                    {it.symbol}
                  </span>
                  <span className="text-[10px] text-jarvis-text-secondary">
                    {it.kind}
                  </span>
                </div>
                {it.detail && (
                  <p className="text-xs text-jarvis-text-secondary mt-1 leading-relaxed">
                    {it.detail}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
