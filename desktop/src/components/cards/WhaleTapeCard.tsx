import { useMemo } from "react";
import { clsx } from "clsx";
import { Fish, AlertTriangle, Radio } from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api, type WhaleSymbolSummary, type WhaleEvent } from "@/api/client";

/**
 * 大单流监控卡（M2 s5 whale tape）：
 * 每币种窗口大单净流方向 + 强度条 + 最近异常事件（超大单/同向连续/量价背离）。
 * 数据源为 WS aggTrade 实时聚合，WS 未就绪时整卡占位（禁止假数据）。
 */

function fmtUsd(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}

function eventKindCn(kind: WhaleEvent["kind"]): string {
  if (kind === "single_super") return "巨单";
  if (kind === "consecutive") return "连续同向";
  return "量价背离";
}

function timeHm(tsMs: number): string {
  return new Date(tsMs).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 单币行：净流方向 + 双向强度条（以窗口内买卖额较大者归一） */
function SymbolRow({ s }: { s: WhaleSymbolSummary }) {
  const denom = Math.max(s.buy_usd, s.sell_usd, 1);
  const buyPct = (s.buy_usd / denom) * 100;
  const sellPct = (s.sell_usd / denom) * 100;
  const netPositive = s.net_usd >= 0;
  return (
    <div className="py-1.5">
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-jarvis-text font-medium">
          {s.symbol.replace("USDT", "")}
        </span>
        <div className="flex items-center gap-2">
          {s.divergence && (
            <span
              title={s.divergence.note}
              className="text-[10px] px-1.5 py-0.5 rounded-full bg-jarvis-yellow/15 text-jarvis-yellow whitespace-nowrap"
            >
              背离
            </span>
          )}
          <span
            className={clsx(
              "text-xs font-mono whitespace-nowrap",
              !s.active || s.whale_n === 0
                ? "text-jarvis-text-secondary"
                : netPositive
                  ? "text-jarvis-green"
                  : "text-jarvis-red",
            )}
            title={`窗口大单 ${s.whale_n} 笔 · 占总成交 ${s.whale_share_pct}%`}
          >
            {!s.active || s.whale_n === 0
              ? "无大单"
              : `${netPositive ? "净流入 +" : "净流出 -"}${fmtUsd(Math.abs(s.net_usd)).slice(1)}`}
          </span>
        </div>
      </div>
      {/* 买卖双向强度条：左绿买 / 右红卖 */}
      <div className="flex items-center gap-1">
        <div className="flex-1 h-1.5 bg-jarvis-bg rounded-full overflow-hidden flex justify-end">
          <div
            className="h-full bg-jarvis-green rounded-full transition-all"
            style={{ width: `${s.active ? buyPct : 0}%` }}
          />
        </div>
        <div className="flex-1 h-1.5 bg-jarvis-bg rounded-full overflow-hidden">
          <div
            className="h-full bg-jarvis-red rounded-full transition-all"
            style={{ width: `${s.active ? sellPct : 0}%` }}
          />
        </div>
      </div>
    </div>
  );
}

export default function WhaleTapeCard() {
  // WS 聚合为内存态，15s 轮询取窗口快照
  const { data } = usePolling(() => api.whaleSummary(), 15_000);

  const symbols = useMemo(
    () => Object.values(data?.symbols ?? {}),
    [data?.symbols],
  );
  // 全币种事件合流取最近 3 条（按时间倒序）
  const recentEvents = useMemo(() => {
    const all = symbols.flatMap((s) =>
      (s.events ?? []).map((e) => ({ ...e, symbol: s.symbol })),
    );
    return all.sort((a, b) => b.ts_ms - a.ts_ms).slice(0, 3);
  }, [symbols]);

  const wsReady = data?.ws_ready === true;

  return (
    <div className="card">
      <p className="stat-label mb-3 flex items-center gap-1.5">
        <Fish size={14} />
        大单流（{data?.window_min ?? 15}min 窗口）
        <span
          className={clsx(
            "text-[10px] px-1.5 py-0.5 rounded border font-medium flex-shrink-0 flex items-center gap-1",
            wsReady
              ? "bg-jarvis-green/10 text-jarvis-green border-jarvis-green/40"
              : "bg-jarvis-border/30 text-jarvis-text-secondary border-jarvis-border",
          )}
          title={
            wsReady
              ? `WS aggTrade 实时流 · 大单阈值 ${fmtUsd(data?.tier1_usd ?? 100000)}`
              : "WS 实时流未就绪（连接中或已关闭），暂无大单数据"
          }
        >
          <Radio size={9} />
          {wsReady ? "实时" : "未就绪"}
        </span>
      </p>

      {symbols.length > 0 && wsReady ? (
        <>
          <div className="divide-y divide-jarvis-border/40">
            {symbols.slice(0, 5).map((s) => (
              <SymbolRow key={s.symbol} s={s} />
            ))}
          </div>

          {recentEvents.length > 0 && (
            <div className="mt-3 pt-2 border-t border-jarvis-border/60 space-y-1.5">
              {recentEvents.map((e, i) => (
                <div key={`${e.symbol}-${e.ts_ms}-${i}`} className="flex items-start gap-1.5">
                  <AlertTriangle
                    size={11}
                    className={clsx(
                      "flex-shrink-0 mt-0.5",
                      e.kind === "divergence" ? "text-jarvis-yellow"
                        : e.side === "buy" ? "text-jarvis-green" : "text-jarvis-red",
                    )}
                  />
                  <p className="text-[11px] text-jarvis-text-secondary leading-snug">
                    <span className="text-jarvis-text font-mono">{timeHm(e.ts_ms)}</span>
                    {" · "}
                    <span
                      className={clsx(
                        e.kind === "divergence"
                          ? "text-jarvis-yellow"
                          : e.side === "buy"
                            ? "text-jarvis-green"
                            : "text-jarvis-red",
                      )}
                    >
                      [{e.symbol.replace("USDT", "")} {eventKindCn(e.kind)}]
                    </span>{" "}
                    {e.note}
                  </p>
                </div>
              ))}
            </div>
          )}
        </>
      ) : (
        <div className="py-6 text-center">
          <p className="text-xs text-jarvis-text-secondary">
            {wsReady
              ? "窗口内暂无大单成交，等待数据积累"
              : "WS 实时流未就绪 · 启用后自动聚合逐笔大单（设置 → system.ws_enabled）"}
          </p>
        </div>
      )}
    </div>
  );
}
