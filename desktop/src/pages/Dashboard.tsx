import { usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import { api } from "@/api/client";
import StatCard from "@/components/cards/StatCard";
import PositionCard from "@/components/cards/PositionCard";
import GaugeChart from "@/components/common/GaugeChart";
import {
  LayoutDashboard,
  Wallet,
  TrendingUp,
  BarChart3,
  Target,
} from "lucide-react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
} from "recharts";

function fmtUsd(n: number) {
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
  });
}

export default function Dashboard() {
  const { symbol } = useSymbol();
  const { data: wallet } = usePolling(api.wallet, 30_000);
  const { data: snapshot } = usePolling(
    () => api.snapshot(symbol),
    60_000,
    [symbol],
  );
  const { data: positions } = usePolling(api.positions, 15_000);
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

  // 已平仓最近 5 笔 = 真实交易 PnL（含 realized_pnl_usdt / pnl_pct / exit_reason）
  const closed = (closedPositions ?? []) as Record<string, unknown>[];
  const recentTrades = closed
    .slice()
    .sort(
      (a, b) =>
        Number(b.closed_ts ?? 0) - Number(a.closed_ts ?? 0),
    )
    .slice(0, 5);

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
  const pnlTrend = pnlToday >= 0 ? "up" : "down";

  // /api/series 返回结构是 { dates: string[], close: number[], drawdown_pct, fng }
  // 之前用 Array.isArray 判断，永远 false → 永远显示"加载中"。这里按真实结构组装。
  const seriesObj = (series ?? null) as
    | { dates?: unknown[]; close?: unknown[] }
    | null;
  const dates = (seriesObj?.dates ?? []) as unknown[];
  const closes = (seriesObj?.close ?? []) as unknown[];
  const chartData = dates.map((d, i) => ({
    date: String(d ?? ""),
    value: Number(closes[i] ?? 0),
  }));

  return (
    <div className="space-y-6">
      <h1 className="page-title flex items-center gap-2">
        <LayoutDashboard size={22} />
        总览
      </h1>

      <div className="grid grid-cols-4 gap-4">
        <StatCard
          label="总资产"
          value={fmtUsd(totalAssets)}
          icon={<Wallet size={18} />}
        />
        <StatCard
          label="可用余额"
          value={fmtUsd(cash)}
          icon={<BarChart3 size={18} />}
        />
        <StatCard
          label="持仓市值"
          value={fmtUsd(holdingsValue)}
          icon={<TrendingUp size={18} />}
        />
        <StatCard
          label="今日盈亏"
          value={`${pnlToday >= 0 ? "+" : ""}${fmtUsd(pnlToday)}`}
          icon={<Target size={18} />}
          trend={pnlTrend as "up" | "down"}
          subtitle={`${pnlToday >= 0 ? "▲" : "▼"} ${pnlTrend === "up" ? "盈利" : "亏损"}`}
        />
      </div>

      <div className="grid grid-cols-3 gap-4">
        <div className="card col-span-2">
          <p className="stat-label mb-4">收益曲线（7天）</p>
          {chartData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
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
            <div className="h-[200px] flex items-center justify-center text-jarvis-text-secondary text-sm">
              {seriesError
                ? `数据接口异常：${seriesError}`
                : seriesLoading
                  ? "加载中..."
                  : "暂无数据"}
            </div>
          )}
        </div>

        <div className="card flex flex-col items-center justify-center">
          <GaugeChart
            value={confidence}
            label="信心分"
            description={direction}
            size={140}
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div className="card">
          <p className="stat-label mb-4">活跃持仓</p>
          {pos.length > 0 ? (
            <div className="space-y-3">
              {pos.map((p, i) => (
                <PositionCard
                  key={i}
                  symbol={String(p.symbol ?? "—")}
                  direction={
                    String(p.direction ?? "long") === "short"
                      ? "short"
                      : "long"
                  }
                  entryPrice={Number(p.entry_price ?? 0)}
                  currentPrice={Number(p.current_price ?? p.entry_price ?? 0)}
                  pnlPct={Number(p.pnl_pct ?? 0)}
                  stopLoss={p.stop_loss ? Number(p.stop_loss) : undefined}
                  takeProfit={
                    p.take_profit ? Number(p.take_profit) : undefined
                  }
                />
              ))}
            </div>
          ) : (
            <p className="text-jarvis-text-secondary text-sm">暂无持仓</p>
          )}
        </div>

        <div className="card">
          <p className="stat-label mb-4">最近交易（已平仓）</p>
          {recentTrades.length > 0 ? (
            <div className="space-y-2">
              {recentTrades.map((t, i) => {
                const pnl = Number(t.realized_pnl_usdt ?? 0);
                const pnlPct = Number(t.realized_pnl_pct ?? 0);
                const isWin = pnl >= 0;
                const reasonMap: Record<string, string> = {
                  stop_loss: "止损",
                  take_profit: "止盈",
                  time_stop: "到期",
                  signal_flip: "反转",
                  manual: "手动",
                };
                const reason =
                  reasonMap[String(t.exit_reason ?? "")] ??
                  String(t.exit_reason ?? "");
                return (
                  <div
                    key={i}
                    className="flex items-center justify-between py-2 border-b border-jarvis-border last:border-0"
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-jarvis-text">
                        {String(t.symbol ?? "—")}
                      </span>
                      <span className="text-xs text-jarvis-text-secondary">
                        {reason}
                      </span>
                    </div>
                    <span
                      className={`text-sm font-mono ${isWin ? "text-jarvis-green" : "text-jarvis-red"}`}
                    >
                      {isWin ? "+" : ""}
                      {fmtUsd(pnl)}
                      <span className="text-xs ml-1 opacity-70">
                        ({isWin ? "+" : ""}
                        {pnlPct.toFixed(2)}%)
                      </span>
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-jarvis-text-secondary text-sm">
              暂无已平仓交易（开仓后按止盈 / 止损 / 到期 / 手动平仓后此处会显示真实盈亏）
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
