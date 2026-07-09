import { Link } from "react-router-dom";
import { clsx } from "clsx";
import { History, ArrowRight } from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api, formatPrice } from "@/api/client";
import {
  type ClosedTrade,
  REASON_CN,
  SOURCE_META,
  SYSTEM_CN,
  REGIME_CN,
  resonanceBucket,
  parseSystems,
  fmtTradeTs,
} from "@/lib/tradeMeta";

/** 总览页只展示最近 N 笔，完整历史/筛选去「交易记录」页 */
const RECENT = 8;

/**
 * 交易记录（已平仓）精简卡片：最近 8 笔速览；
 * 完整表格、筛选与翻页历史在独立「交易记录」页（/trades）。
 */
export default function TradeHistory() {
  const { data, loading, error } = usePolling(
    () => api.get<ClosedTrade[]>(`/positions?status=closed&limit=${RECENT * 3}`),
    30_000,
  );

  const trades = (data ?? [])
    // 后端已默认排除历史回放样本；前端再过滤一道双保险，交易记录只展真实模拟单
    .filter((t) => t.signal_source !== "replay")
    .sort((a, b) => Number(b.closed_ts ?? 0) - Number(a.closed_ts ?? 0))
    .slice(0, RECENT);

  return (
    <div className="card flex flex-col min-h-0">
      <div className="flex items-center justify-between mb-3">
        <p className="stat-label flex items-center gap-2 mb-0">
          <History size={14} />
          交易记录（最近 {RECENT} 笔）
        </p>
        <Link
          to="/trades"
          className="flex items-center gap-1 text-xs text-jarvis-blue hover:underline"
        >
          查看全部
          <ArrowRight size={12} />
        </Link>
      </div>

      {error && !data ? (
        <p className="text-sm text-jarvis-text-secondary py-8 text-center">
          记录加载失败：{error}
        </p>
      ) : loading && !data ? (
        <div className="space-y-2 animate-pulse">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-12 rounded-lg bg-jarvis-border/30" />
          ))}
        </div>
      ) : trades.length === 0 ? (
        <p className="text-sm text-jarvis-text-secondary py-8 text-center">
          暂无已平仓交易 · 开仓后按止盈 / 止损 / 到期 / 信号反转平仓的每一笔都会永久记录
        </p>
      ) : (
        <div className="space-y-1.5 overflow-y-auto max-h-[420px] pr-1">
          {trades.map((t) => {
            const pnl = Number(t.realized_pnl_usdt ?? 0);
            const pnlPct = Number(t.realized_pnl_pct ?? 0);
            const isWin = pnl >= 0;
            const src = SOURCE_META[String(t.signal_source ?? "")] ?? null;
            const systems = parseSystems(t.signal_systems);
            return (
              <div
                key={t.id}
                className="bg-jarvis-bg rounded-lg px-3 py-2 border border-jarvis-border/40"
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-[10px] text-jarvis-text-secondary font-mono whitespace-nowrap">
                      {fmtTradeTs(t.closed_ts)}
                    </span>
                    <span className="text-sm text-jarvis-text font-medium">
                      {t.symbol}
                    </span>
                    <span
                      className={clsx(
                        "text-[10px] px-1.5 py-0.5 rounded-full whitespace-nowrap",
                        t.direction === "short"
                          ? "bg-jarvis-red/15 text-jarvis-red"
                          : "bg-jarvis-green/15 text-jarvis-green",
                      )}
                    >
                      {t.direction === "short" ? "空" : "多"}
                    </span>
                    {src && (
                      <span
                        className={clsx(
                          "text-[10px] px-1.5 py-0.5 rounded-full whitespace-nowrap",
                          src.cls,
                        )}
                      >
                        {src.label}
                        {t.signal_source === "twelve" && t.signal_tf
                          ? ` ${t.signal_tf}`
                          : ""}
                      </span>
                    )}
                    {t.signal_source === "twelve" && systems.length > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-jarvis-blue/10 text-jarvis-blue whitespace-nowrap">
                        {resonanceBucket(systems.length)}
                      </span>
                    )}
                    {t.signal_source === "twelve" && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-jarvis-border/40 text-jarvis-text-secondary whitespace-nowrap">
                        {REGIME_CN[String(t.signal_regime ?? "")] ?? "未知"}
                      </span>
                    )}
                    <span className="text-[10px] text-jarvis-text-secondary whitespace-nowrap">
                      {REASON_CN[String(t.exit_reason ?? "")] ??
                        String(t.exit_reason ?? "—")}
                    </span>
                  </div>
                  <span
                    className={clsx(
                      "text-sm font-mono whitespace-nowrap",
                      isWin ? "text-jarvis-green" : "text-jarvis-red",
                    )}
                  >
                    {isWin ? "+" : ""}
                    {pnl.toFixed(2)}U
                    <span className="text-[10px] ml-1 opacity-70">
                      ({isWin ? "+" : ""}
                      {pnlPct.toFixed(2)}%)
                    </span>
                  </span>
                </div>
                <div className="flex items-center justify-between gap-2 mt-1">
                  <span className="text-[10px] text-jarvis-text-secondary font-mono">
                    {formatPrice(t.entry_price)} → {formatPrice(t.exit_price)}
                    {" · "}
                    {Number(t.qty ?? 0)}
                  </span>
                  {systems.length > 0 && (
                    <span className="flex items-center gap-1 flex-wrap justify-end">
                      {systems.map((s) => (
                        <span
                          key={s}
                          className="text-[10px] px-1 py-px rounded bg-jarvis-card border border-jarvis-border text-jarvis-text-secondary"
                        >
                          {SYSTEM_CN[s] ?? s}
                        </span>
                      ))}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
          <Link
            to="/trades"
            className="flex items-center justify-center gap-1 py-2 text-xs text-jarvis-text-secondary hover:text-jarvis-blue transition-colors"
          >
            查看全部交易记录与筛选
            <ArrowRight size={12} />
          </Link>
        </div>
      )}
    </div>
  );
}
