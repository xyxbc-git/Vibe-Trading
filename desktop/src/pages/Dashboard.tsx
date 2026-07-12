import { usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import { api, type ConsensusScope } from "@/api/client";
import PositionCard from "@/components/cards/PositionCard";
import { extractCardMetrics } from "@/lib/positionMetrics";
import HealthStatusCard from "@/components/cards/HealthStatusCard";
import ConsensusGauge from "@/components/cards/ConsensusGauge";
import SignalBoard from "@/components/cards/SignalBoard";
import PositionAdvisor from "@/components/cards/PositionAdvisor";
import InsightFeed from "@/components/cards/InsightFeed";
import TradeHistory from "@/components/cards/TradeHistory";
import SignalWinRate from "@/components/cards/SignalWinRate";
import {
  LayoutDashboard,
  Wallet,
  BookMarked,
  Loader2,
} from "lucide-react";
import { useState } from "react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
} from "recharts";
import GaugeChart from "@/components/common/GaugeChart";

function fmtUsd(n: number) {
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
  });
}

export default function Dashboard() {
  const { symbol } = useSymbol();
  const [journalBusy, setJournalBusy] = useState(false);
  const [journalMsg, setJournalMsg] = useState("");
  // 页级共识口径：共识仪表盘与信号矩阵联动（"auto" = 多周期综合）
  const [consensusTf, setConsensusTf] = useState<ConsensusScope>("auto");

  const { data: wallet } = usePolling(api.wallet, 30_000);
  const {
    data: health,
    loading: healthLoading,
    error: healthError,
  } = usePolling(api.health, 15_000);
  const { data: track, refetch: refetchTrack } = usePolling(
    () => api.track(symbol),
    60_000,
    [symbol],
  );
  const { data: snapshot } = usePolling(
    () => api.snapshot(symbol),
    60_000,
    [symbol],
  );
  const { data: positions } = usePolling(api.positions, 3_000);
  const {
    data: series,
    loading: seriesLoading,
    error: seriesError,
  } = usePolling(() => api.series(symbol, 7), 120_000, [symbol]);
  const { data: closedPositions } = usePolling(
    () => api.get<Record<string, unknown>[]>("/positions?status=closed"),
    30_000,
  );

  const w = wallet as Record<string, number> | null;
  const snap = snapshot as Record<string, unknown> | null;
  const pos = (positions ?? []) as Record<string, unknown>[];

  // 已平仓记录（今日盈亏计算用；完整滚动列表由 TradeHistory 组件自拉自渲染）
  const closed = (closedPositions ?? []) as Record<string, unknown>[];

  const cash = w?.cash_usdt ?? w?.cash ?? 0;
  const frozen = w?.frozen_usdt ?? w?.frozen ?? 0;
  // 持仓市值 = 未平仓 current_price * qty 之和（后端补齐后才有数值，否则 fallback 用 entry）
  const holdingsValue = pos.reduce((sum, p) => {
    const px = Number(p.current_price ?? p.entry_price ?? 0);
    const qty = Number(p.qty ?? 0);
    return sum + px * qty;
  }, 0);
  const totalAssets = cash + frozen + holdingsValue;

  const brief = snap?.brief as Record<string, unknown> | undefined;
  const confidence = Number(brief?.confidence ?? 0);
  const direction =
    confidence > 0.3 ? "偏多" : confidence < -0.3 ? "偏空" : "观望";

  // 今日盈亏 = 今天平仓的 realized_pnl_usdt 累加（按 closed_ts 当天判定）
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const todayStartTs = todayStart.getTime() / 1000;
  const pnlToday = closed
    .filter((p) => Number(p.closed_ts ?? 0) >= todayStartTs)
    .reduce(
      (sum, p) => sum + Number(p.realized_pnl_usdt ?? 0),
      0,
    );

  // /api/series 返回结构是 { dates: string[], close: number[], drawdown_pct, fng }
  const seriesObj = (series ?? null) as
    | { dates?: unknown[]; close?: unknown[] }
    | null;
  const dates = (seriesObj?.dates ?? []) as unknown[];
  const closes = (seriesObj?.close ?? []) as unknown[];
  const chartData = dates.map((d, i) => ({
    date: String(d ?? ""),
    value: Number(closes[i] ?? 0),
  }));

  const trackReport = (track?.report ?? {}) as Record<string, unknown>;
  const totalSnapshots = Number(trackReport.total_snapshots ?? 0);
  const hitRate = trackReport.overall_hit_pct;

  const handleJournalCycle = async () => {
    setJournalBusy(true);
    setJournalMsg("");
    try {
      const res = await api.trackRecord(symbol);
      const rec = res.record;
      const filled = res.evaluate?.outcomes_filled ?? 0;
      if (rec?.ok) {
        setJournalMsg(
          `已记录 ${rec.as_of_date ?? "今日"}，回填 ${filled} 条到期结果`,
        );
      } else {
        setJournalMsg(`记录失败: ${rec?.error ?? "未知错误"}`);
      }
      refetchTrack();
    } catch (e) {
      setJournalMsg(e instanceof Error ? e.message : "请求失败");
    } finally {
      setJournalBusy(false);
      setTimeout(() => setJournalMsg(""), 8000);
    }
  };

  return (
    <div className="space-y-6">
      <h1 className="page-title flex items-center gap-2">
        <LayoutDashboard size={22} />
        驾驶舱总览
      </h1>

      {/* ── 第一行 · 先看方向：贾维斯共识 | 持仓&钱包 | 系统健康 ── */}
      <div className="grid grid-cols-3 gap-4">
        <ConsensusGauge symbol={symbol} tf={consensusTf} />

        <div className="card flex flex-col">
          <p className="stat-label flex items-center gap-2 mb-3">
            <Wallet size={14} />
            持仓 & 钱包
          </p>
          <div className="mb-3">
            <p className="text-xs text-jarvis-text-secondary">总资产</p>
            <p className="stat-value">{fmtUsd(totalAssets)}</p>
          </div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-2 text-sm">
            <div>
              <p className="text-xs text-jarvis-text-secondary">可用余额</p>
              <p className="font-mono text-jarvis-text">{fmtUsd(cash)}</p>
            </div>
            <div>
              <p className="text-xs text-jarvis-text-secondary">持仓市值</p>
              <p className="font-mono text-jarvis-text">
                {fmtUsd(holdingsValue)}
              </p>
            </div>
            <div>
              <p className="text-xs text-jarvis-text-secondary">冻结</p>
              <p className="font-mono text-jarvis-text">{fmtUsd(frozen)}</p>
            </div>
            <div>
              <p className="text-xs text-jarvis-text-secondary">今日盈亏</p>
              <p
                className={`font-mono ${
                  pnlToday >= 0 ? "text-jarvis-green" : "text-jarvis-red"
                }`}
              >
                {pnlToday >= 0 ? "+" : ""}
                {fmtUsd(pnlToday)}
              </p>
            </div>
          </div>
          <div className="mt-auto pt-3 border-t border-jarvis-border/60">
            <p className="text-xs text-jarvis-text-secondary">
              活跃持仓{" "}
              <span className="text-jarvis-text font-mono">{pos.length}</span>{" "}
              笔 · 详情见下方持仓卡
            </p>
          </div>
        </div>

        <HealthStatusCard
          health={health ?? null}
          loading={healthLoading}
          error={healthError}
        />
      </div>

      {/* ── 第二行 · 再看信号：12 套系统信号矩阵 ── */}
      <SignalBoard symbol={symbol} tf={consensusTf} onTfChange={setConsensusTf} />

      {/* ── 第二行半 · 能开多少：仓位与风控建议（信号计划 × 本金/杠杆/风险%）── */}
      <PositionAdvisor symbol={symbol} tf={consensusTf} />

      {/* ── 第三行 · 主动汇报：洞察流 | 净值曲线 ── */}
      <div className="grid grid-cols-3 gap-4">
        <InsightFeed limit={20} />

        <div className="card col-span-2">
          <p className="stat-label mb-4">收益曲线（7天）</p>
          {chartData.length > 0 ? (
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id="colorValue" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#3fb950" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#3fb950" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis
                  dataKey="date"
                  stroke="#8b949e"
                  fontSize={11}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  stroke="#8b949e"
                  fontSize={11}
                  tickLine={false}
                  axisLine={false}
                  domain={["auto", "auto"]}
                  tickFormatter={(v: number) =>
                    v > 1000 ? `${(v / 1000).toFixed(0)}k` : String(v)
                  }
                />
                <Tooltip
                  contentStyle={{
                    background: "#161b22",
                    border: "1px solid #30363d",
                    borderRadius: 8,
                    color: "#e6edf3",
                    fontSize: 12,
                  }}
                />
                <Area
                  type="monotone"
                  dataKey="value"
                  stroke="#3fb950"
                  fill="url(#colorValue)"
                  strokeWidth={2}
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[220px] flex items-center justify-center text-jarvis-text-secondary text-sm">
              {seriesError
                ? `数据接口异常：${seriesError}`
                : seriesLoading
                  ? "加载中..."
                  : "暂无数据"}
            </div>
          )}
        </div>
      </div>

      {/* ── 细节区 · 战绩追踪 | 简报信心分 ── */}
      <div className="grid grid-cols-3 gap-4">
        <div className="card col-span-2">
          <div className="flex items-center justify-between mb-3">
            <p className="stat-label flex items-center gap-2 mb-0">
              <BookMarked size={14} />
              战绩追踪 · {symbol}
            </p>
            <button
              onClick={handleJournalCycle}
              disabled={journalBusy}
              className="btn-primary text-xs py-1.5 px-3 flex items-center gap-1.5 disabled:opacity-50"
            >
              {journalBusy ? (
                <Loader2 size={14} className="animate-spin" />
              ) : null}
              记录今日 + 回填
            </button>
          </div>
          <div className="grid grid-cols-3 gap-3 text-sm mb-2">
            <div>
              <p className="text-jarvis-text-secondary text-xs">快照数</p>
              <p className="font-mono text-jarvis-text">{totalSnapshots}</p>
            </div>
            <div>
              <p className="text-jarvis-text-secondary text-xs">命中率</p>
              <p className="font-mono text-jarvis-text">
                {hitRate != null ? `${hitRate}%` : "—"}
              </p>
            </div>
            <div>
              <p className="text-jarvis-text-secondary text-xs">最近快照</p>
              <p className="font-mono text-jarvis-text truncate">
                {String(
                  ((track?.recent ?? [])[0] as
                    | Record<string, unknown>
                    | undefined)?.as_of_date ?? "—",
                )}
              </p>
            </div>
          </div>
          {journalMsg && (
            <p
              className={`text-xs ${journalMsg.includes("失败") ? "text-jarvis-red" : "text-jarvis-green"}`}
            >
              {journalMsg}
            </p>
          )}
          <p className="text-xs text-jarvis-text-secondary mt-2">
            一键执行 Brief→Journal 落库 + Evaluate 回填，与 daemon 心跳同源逻辑。
          </p>
        </div>

        <div className="card flex flex-col items-center justify-center">
          <GaugeChart
            value={confidence}
            label="简报信心分"
            description={direction}
            size={140}
          />
        </div>
      </div>

      {/* ── 细节区 · 活跃持仓 | 交易记录（持久滚动） ── */}
      <div className="grid grid-cols-2 gap-4">
        <div className="card">
          <p className="stat-label mb-4">活跃持仓</p>
          {pos.length > 0 ? (
            <div className="space-y-3">
              {pos.map((p, i) => {
                const metrics = extractCardMetrics(p);
                return (
                  <PositionCard
                    key={`${String(p.symbol ?? "—")}-${i}`}
                    symbol={String(p.symbol ?? "—")}
                    direction={
                      String(p.direction ?? "long") === "short"
                        ? "short"
                        : "long"
                    }
                    entryPrice={Number(p.entry_price ?? 0)}
                    currentPrice={
                      p.current_price != null
                        ? Number(p.current_price)
                        : undefined
                    }
                    pnlPct={
                      p.pnl_pct != null ? Number(p.pnl_pct) : undefined
                    }
                    pnlUsdt={metrics.pnlUsdt}
                    stopLoss={p.stop_loss ? Number(p.stop_loss) : undefined}
                    takeProfit={
                      p.take_profit ? Number(p.take_profit) : undefined
                    }
                    qty={metrics.qty}
                    marginUsdt={metrics.marginUsdt}
                    notionalUsdt={metrics.notionalUsdt}
                    leverage={metrics.leverage}
                  />
                );
              })}
            </div>
          ) : (
            <p className="text-jarvis-text-secondary text-sm">暂无持仓</p>
          )}
        </div>

        <TradeHistory />
      </div>

      {/* ── 细节区 · 12 系统信号胜率归因 ── */}
      <SignalWinRate />
    </div>
  );
}
