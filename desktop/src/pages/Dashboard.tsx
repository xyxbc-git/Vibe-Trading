import { usePolling } from "@/hooks/useApi";
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
  const { data: wallet } = usePolling(api.wallet, 30_000);
  const { data: snapshot } = usePolling(
    () => api.snapshot("BTCUSDT"),
    60_000,
  );
  const { data: positions } = usePolling(api.positions, 15_000);
  const { data: series } = usePolling(
    () => api.series("BTCUSDT", 7),
    120_000,
  );
  const { data: ledger } = usePolling(api.ledger, 30_000);

  const w = wallet as Record<string, number> | null;
  const snap = snapshot as Record<string, unknown> | null;
  const pos = (positions ?? []) as Record<string, unknown>[];
  const recentTrades = ((ledger ?? []) as Record<string, unknown>[]).slice(-5).reverse();

  const cash = w?.cash_usdt ?? w?.cash ?? 0;
  const frozen = w?.frozen_usdt ?? w?.frozen ?? 0;
  const totalAssets = cash + frozen;

  const brief = snap?.brief as Record<string, unknown> | undefined;
  const confidence = Number(brief?.confidence ?? 0);
  const direction =
    confidence > 0.3 ? "偏多" : confidence < -0.3 ? "偏空" : "观望";

  const pnlToday = recentTrades.reduce(
    (sum, t) => sum + Number((t as Record<string, number>)?.pnl ?? 0),
    0,
  );
  const pnlTrend = pnlToday >= 0 ? "up" : "down";

  const chartData = Array.isArray(series)
    ? (series as Record<string, unknown>[]).map((d) => ({
        date: String(d.date ?? ""),
        value: Number(d.close ?? 0),
      }))
    : [];

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
          value={fmtUsd(frozen)}
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
              加载中...
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
          <p className="stat-label mb-4">最近交易</p>
          {recentTrades.length > 0 ? (
            <div className="space-y-2">
              {recentTrades.map((t, i) => {
                const pnl = Number((t as Record<string, number>).pnl ?? 0);
                const isWin = pnl >= 0;
                return (
                  <div
                    key={i}
                    className="flex items-center justify-between py-2 border-b border-jarvis-border last:border-0"
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-jarvis-text">
                        {String((t as Record<string, string>).symbol ?? "—")}
                      </span>
                      <span className="text-xs text-jarvis-text-secondary">
                        {String((t as Record<string, string>).type ?? "")}
                      </span>
                    </div>
                    <span
                      className={`text-sm font-mono ${isWin ? "text-jarvis-green" : "text-jarvis-red"}`}
                    >
                      {isWin ? "+" : ""}
                      {fmtUsd(pnl)} {isWin ? "✅" : "❌"}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-jarvis-text-secondary text-sm">暂无交易记录</p>
          )}
        </div>
      </div>
    </div>
  );
}
