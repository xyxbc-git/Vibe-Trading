import { clsx } from "clsx";
import { Flame, PlugZap, Zap, Loader2 } from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api, formatPrice } from "@/api/client";

function fmtUsd(n: number): string {
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(2)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

function timeHm(unixSec: number): string {
  return new Date(unixSec * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * 爆仓流实时面板（M2 s5）：滚动窗口多/空爆仓金额对比 + 最近大额爆仓 + 簇警示。
 * 三状态：
 *   1. 实时正常 —— forceOrder 流在线，展示窗口统计
 *   2. 降级引导 —— 代理丢弃合约域帧（degraded），展示历史库数据 + 放行引导
 *   3. 无数据 —— 流可用但窗口内无爆仓（平静市况）
 */
export default function LiquidationPanel({ symbol }: { symbol?: string }) {
  // 后端 10s 缓存，前端 15s 轮询足够实时
  const { data, loading } = usePolling(
    () => api.liquidationSummary(symbol),
    15_000,
    [symbol],
  );

  const ok = Boolean(data?.ok);
  const stats = ok ? data?.stats : undefined;
  const total = stats?.total_usd ?? 0;
  const longPct = total > 0 ? ((stats?.long_usd ?? 0) / total) * 100 : 50;
  const clusters = data?.clusters ?? [];
  const large = data?.large ?? [];

  return (
    <div className="card">
      <p className="stat-label mb-3 flex items-center gap-1.5">
        <Flame size={14} />
        爆仓流 ({data?.window_min ?? 60}min)
        {data?.degraded ? (
          <span
            className="text-[10px] px-1.5 py-0.5 rounded border font-medium flex-shrink-0
                       bg-jarvis-yellow/10 text-jarvis-yellow border-jarvis-yellow/40"
            title={data.guidance ?? "实时流降级中，展示历史数据"}
          >
            历史回退
          </span>
        ) : ok ? (
          <span
            className="text-[10px] px-1.5 py-0.5 rounded border font-medium flex-shrink-0
                       bg-jarvis-green/10 text-jarvis-green border-jarvis-green/40"
          >
            实时
          </span>
        ) : null}
      </p>

      {!data && loading ? (
        <div className="py-5 flex items-center justify-center gap-2 text-jarvis-text-secondary text-xs">
          <Loader2 size={14} className="animate-spin" />
          正在统计爆仓数据...
        </div>
      ) : !ok ? (
        <div className="py-4 text-center">
          <p className="text-xs text-jarvis-text-secondary">
            爆仓面板暂不可用：{data?.error ?? "等待后端就绪"}
          </p>
        </div>
      ) : (
        <div className="space-y-2.5">
          {/* ── 多/空爆仓金额条形对比 ── */}
          {total > 0 ? (
            <>
              <div className="flex items-center justify-between text-xs">
                <span className="text-jarvis-red" title="多头被强平金额（下跌动能）">
                  多爆 {fmtUsd(stats?.long_usd ?? 0)} ({stats?.long_count ?? 0}笔)
                </span>
                <span className="text-jarvis-green" title="空头被强平金额（上涨动能）">
                  空爆 {fmtUsd(stats?.short_usd ?? 0)} ({stats?.short_count ?? 0}笔)
                </span>
              </div>
              <div className="w-full h-3 rounded-full overflow-hidden flex bg-jarvis-bg">
                {/* 多头爆仓=强制卖出压力，红色；空头爆仓=强制买入，绿色 */}
                <div
                  className="h-full bg-jarvis-red transition-all"
                  style={{ width: `${longPct}%` }}
                />
                <div
                  className="h-full bg-jarvis-green transition-all"
                  style={{ width: `${100 - longPct}%` }}
                />
              </div>
              <p className="text-[10px] text-jarvis-text-secondary text-center">
                {Math.abs(stats?.dominance ?? 0) < 0.2
                  ? "多空爆仓均衡"
                  : (stats?.dominance ?? 0) > 0
                    ? "🔻 多头爆仓主导 · 下跌中杠杆出清"
                    : "🔺 空头爆仓主导 · 上涨中轧空进行"}
              </p>
            </>
          ) : (
            <p className="py-1.5 text-xs text-jarvis-text-secondary text-center">
              窗口内无爆仓记录{data?.degraded ? "（历史库暂无数据）" : "（市况平静）"}
            </p>
          )}

          {/* ── 爆仓簇警示（行情加速信号） ── */}
          {clusters.length > 0 && (
            <div className="space-y-1">
              {clusters.slice(0, 2).map((c, i) => (
                <p
                  key={`${c.start_ts}-${i}`}
                  className="flex items-start gap-1.5 text-[10px] leading-relaxed rounded px-2 py-1 bg-jarvis-purple/10 text-jarvis-purple"
                  title={c.symbols.join(" · ")}
                >
                  <Zap size={11} className="flex-shrink-0 mt-0.5" />
                  <span>
                    {timeHm(c.end_ts)} {c.note}
                  </span>
                </p>
              ))}
            </div>
          )}

          {/* ── 最近大额爆仓列表 ── */}
          {large.length > 0 && (
            <div>
              <p
                className="text-[10px] text-jarvis-text-secondary mb-1"
                title={`单笔名义 ≥ ${fmtUsd(data?.thresholds?.large_usd ?? 50000)}（可在设置 signal.liq_large_usd 调整）`}
              >
                最近大额爆仓
              </p>
              <div className="space-y-1 max-h-28 overflow-y-auto">
                {large.slice(0, 6).map((e, i) => (
                  <div
                    key={`${e.trade_time}-${i}`}
                    className="flex items-center justify-between text-[11px] font-mono"
                  >
                    <span className="text-jarvis-text-secondary">
                      {timeHm(Math.floor(e.trade_time / 1000))}
                    </span>
                    <span className="text-jarvis-text">
                      {e.symbol.replace("USDT", "")}
                    </span>
                    <span
                      className={clsx(
                        e.side_liquidated === "long"
                          ? "text-jarvis-red"
                          : "text-jarvis-green",
                      )}
                    >
                      {e.side_liquidated === "long" ? "多爆" : "空爆"}
                    </span>
                    <span className="text-jarvis-text-secondary">
                      @{formatPrice(e.price)}
                    </span>
                    <span className="text-jarvis-text font-semibold">
                      {fmtUsd(e.notional)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── 降级引导（代理放行 fstream.binance.com 后自动恢复） ── */}
          {data?.degraded && data?.guidance && (
            <p className="flex items-start gap-1.5 text-[10px] leading-relaxed rounded px-2 py-1.5 bg-jarvis-yellow/10 text-jarvis-yellow border border-jarvis-yellow/30">
              <PlugZap size={12} className="flex-shrink-0 mt-0.5" />
              <span>{data.guidance}</span>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
