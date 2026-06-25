import { useMemo, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import {
  TrendingUp,
  Play,
  RotateCcw,
  Activity,
  Code2,
  Terminal as TerminalIcon,
} from "lucide-react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { api, type BacktestTrade } from "@/api/client";
import { useApi, usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";

function num(v: unknown, d = 0): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : d;
}

function tradePnl(t: BacktestTrade): number {
  return num(t.pnl ?? t.profit ?? t.net_pnl, 0);
}

function MetricCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "green" | "red" | "default";
}) {
  const color =
    tone === "green"
      ? "text-jarvis-green"
      : tone === "red"
        ? "text-jarvis-red"
        : "text-jarvis-text";
  return (
    <div className="card">
      <p className="text-xs text-jarvis-text-secondary">{label}</p>
      <p className={`stat-value text-xl mt-1 font-mono ${color}`}>{value}</p>
    </div>
  );
}

export default function Backtest() {
  const [params] = useSearchParams();
  const name = params.get("name") ?? "";
  const { symbol } = useSymbol();

  const [timeframe, setTimeframe] = useState("15m");
  const [start, setStart] = useState("2025-01-01");
  const [end, setEnd] = useState("2026-06-01");
  const [capital, setCapital] = useState(10000);
  const [busy, setBusy] = useState(false);

  const { data: codeData } = useApi(
    () => api.backtestCode(name),
    [name],
  );

  const { data: state, refetch } = usePolling(
    () => api.backtestResult(),
    2500,
  );

  const running = state?.running ?? false;
  const result = state?.result ?? null;
  const succeeded = result?.status === "succeeded";

  const handleRun = async () => {
    if (!name) return;
    setBusy(true);
    try {
      await api.backtestRun(name, symbol, timeframe, start, end, capital);
    } catch {
      // 后端/网关未就绪
    } finally {
      setBusy(false);
      refetch();
    }
  };

  const trades = useMemo<BacktestTrade[]>(
    () => (succeeded ? (result?.trades ?? []) : []),
    [succeeded, result],
  );

  const equityData = useMemo(() => {
    let bal = capital;
    return trades.map((t, i) => {
      const explicit = t.balance ?? t.equity;
      if (typeof explicit === "number" && Number.isFinite(explicit)) {
        bal = explicit;
      } else {
        bal += tradePnl(t);
      }
      return { idx: i + 1, equity: Math.round(bal * 100) / 100 };
    });
  }, [trades, capital]);

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <TrendingUp size={22} />
        回测
        {name && <span className="text-sm text-jarvis-text-secondary">· {name}</span>}
      </h1>

      {!name && (
        <div className="card text-sm text-jarvis-text-secondary">
          未指定策略。请到
          <Link to="/strategy" className="text-jarvis-blue hover:underline mx-1">
            策略实验室
          </Link>
          的名人堂里点「查看回测」。
        </div>
      )}

      {name && (
        <>
          {/* 回测参数 */}
          <div className="card mb-4">
            <div className="flex flex-wrap items-end gap-4">
              <div>
                <label className="block text-xs text-jarvis-text-secondary mb-1">币种</label>
                <div className="text-sm text-jarvis-text font-mono px-2 py-1.5">{symbol}</div>
              </div>
              <div>
                <label className="block text-xs text-jarvis-text-secondary mb-1">周期</label>
                <select
                  value={timeframe}
                  onChange={(e) => setTimeframe(e.target.value)}
                  className="bg-jarvis-bg border border-jarvis-border rounded px-2 py-1.5 text-sm text-jarvis-text"
                >
                  {["5m", "15m", "30m", "1h", "4h", "1d"].map((tf) => (
                    <option key={tf} value={tf}>
                      {tf}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs text-jarvis-text-secondary mb-1">开始日期</label>
                <input
                  type="date"
                  value={start}
                  onChange={(e) => setStart(e.target.value)}
                  className="bg-jarvis-bg border border-jarvis-border rounded px-2 py-1.5 text-sm text-jarvis-text"
                />
              </div>
              <div>
                <label className="block text-xs text-jarvis-text-secondary mb-1">结束日期</label>
                <input
                  type="date"
                  value={end}
                  onChange={(e) => setEnd(e.target.value)}
                  className="bg-jarvis-bg border border-jarvis-border rounded px-2 py-1.5 text-sm text-jarvis-text"
                />
              </div>
              <div>
                <label className="block text-xs text-jarvis-text-secondary mb-1">初始资金</label>
                <input
                  type="number"
                  value={capital}
                  onChange={(e) => setCapital(Number(e.target.value))}
                  className="bg-jarvis-bg border border-jarvis-border rounded px-2 py-1.5 text-sm text-jarvis-text w-28"
                />
              </div>
              <button
                onClick={handleRun}
                disabled={busy || running}
                className="btn-success flex items-center gap-2 disabled:opacity-50 ml-auto"
              >
                {busy || running ? (
                  <RotateCcw size={16} className="animate-spin" />
                ) : (
                  <Play size={16} />
                )}
                {running ? "回测中..." : "运行回测"}
              </button>
            </div>
            <div className="mt-3 flex items-center gap-1 text-xs text-jarvis-text-secondary">
              <TerminalIcon size={12} />
              回测过程实时输出见
              <Link to="/terminal" className="text-jarvis-blue hover:underline">
                「终端」页
              </Link>
              {state?.error && <span className="text-jarvis-red ml-2">错误：{state.error}</span>}
            </div>
          </div>

          {/* 指标卡 */}
          {succeeded && result && (
            <div className="grid grid-cols-5 gap-3 mb-4">
              <MetricCard
                label="总收益"
                value={`${result.total_return_pct >= 0 ? "+" : ""}${result.total_return_pct.toFixed(2)}%`}
                tone={result.total_return_pct >= 0 ? "green" : "red"}
              />
              <MetricCard label="胜率" value={`${result.win_rate.toFixed(1)}%`} />
              <MetricCard
                label="回撤风险"
                value={`${result.max_drawdown_pct.toFixed(1)}%`}
                tone="red"
              />
              <MetricCard label="样本数量" value={`${result.total_trades}`} />
              <MetricCard label="盈亏比 / 夏普" value={`${result.profit_factor.toFixed(2)} / ${result.sharpe_ratio.toFixed(2)}`} />
            </div>
          )}

          {/* 资金曲线 */}
          {succeeded && equityData.length > 0 && (
            <div className="card mb-4">
              <h3 className="text-sm font-medium text-jarvis-text mb-3 flex items-center gap-2">
                <Activity size={14} className="text-jarvis-blue" />
                资金曲线
              </h3>
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={equityData}>
                    <defs>
                      <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#3fb950" stopOpacity={0.4} />
                        <stop offset="100%" stopColor="#3fb950" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#30363d" />
                    <XAxis dataKey="idx" tick={{ fill: "#8b949e", fontSize: 11 }} />
                    <YAxis tick={{ fill: "#8b949e", fontSize: 11 }} domain={["auto", "auto"]} />
                    <Tooltip
                      contentStyle={{
                        background: "#161b22",
                        border: "1px solid #30363d",
                        borderRadius: 8,
                        color: "#e6edf3",
                      }}
                    />
                    <Area
                      type="monotone"
                      dataKey="equity"
                      stroke="#3fb950"
                      fill="url(#eq)"
                      strokeWidth={1.5}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* 逐笔交易 */}
          {succeeded && (
            <div className="card mb-4 overflow-x-auto">
              <h3 className="text-sm font-medium text-jarvis-text mb-3">
                交易记录（{trades.length}）
              </h3>
              {trades.length === 0 ? (
                <p className="text-sm text-jarvis-text-secondary text-center py-4">本次回测无成交</p>
              ) : (
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-jarvis-border text-jarvis-text-secondary text-left">
                      <th className="pb-2 pr-3 font-medium">#</th>
                      <th className="pb-2 pr-3 font-medium">类型</th>
                      <th className="pb-2 pr-3 font-medium">入场时间</th>
                      <th className="pb-2 pr-3 font-medium">出场时间</th>
                      <th className="pb-2 pr-3 font-medium text-right">入场价</th>
                      <th className="pb-2 pr-3 font-medium text-right">出场价</th>
                      <th className="pb-2 pr-3 font-medium text-right">盈亏</th>
                      <th className="pb-2 font-medium">出场说明</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {trades.map((t, i) => {
                      const dir = (t.direction ?? t.side ?? "").toString().toLowerCase();
                      const isLong = dir.includes("long") || dir.includes("多");
                      const isShort = dir.includes("short") || dir.includes("空");
                      const pnl = tradePnl(t);
                      return (
                        <tr key={i} className="border-b border-jarvis-border/40 last:border-0">
                          <td className="py-1.5 pr-3 text-jarvis-text-secondary">{i + 1}</td>
                          <td className="py-1.5 pr-3">
                            <span
                              className={
                                isLong
                                  ? "text-jarvis-green"
                                  : isShort
                                    ? "text-jarvis-red"
                                    : "text-jarvis-text-secondary"
                              }
                            >
                              {isLong ? "多" : isShort ? "空" : dir || "—"}
                            </span>
                          </td>
                          <td className="py-1.5 pr-3 text-jarvis-text-secondary">
                            {(t.entry_time as string) ?? "—"}
                          </td>
                          <td className="py-1.5 pr-3 text-jarvis-text-secondary">
                            {(t.exit_time as string) ?? "—"}
                          </td>
                          <td className="py-1.5 pr-3 text-right text-jarvis-text">
                            {t.entry_price != null ? num(t.entry_price).toFixed(2) : "—"}
                          </td>
                          <td className="py-1.5 pr-3 text-right text-jarvis-text">
                            {t.exit_price != null ? num(t.exit_price).toFixed(2) : "—"}
                          </td>
                          <td
                            className={`py-1.5 pr-3 text-right ${
                              pnl >= 0 ? "text-jarvis-green" : "text-jarvis-red"
                            }`}
                          >
                            {pnl >= 0 ? "+" : ""}
                            {pnl.toFixed(2)}
                          </td>
                          <td className="py-1.5 text-jarvis-text-secondary">
                            {(t.exit_reason as string) ?? "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {/* 策略代码 */}
          <div className="card">
            <h3 className="text-sm font-medium text-jarvis-text mb-3 flex items-center gap-2">
              <Code2 size={14} className="text-jarvis-blue" />
              策略代码
            </h3>
            <pre className="text-xs font-mono text-jarvis-text-secondary bg-jarvis-bg rounded p-3 overflow-x-auto max-h-96 overflow-y-auto whitespace-pre">
              {codeData?.code || "（未找到该策略代码）"}
            </pre>
          </div>
        </>
      )}
    </div>
  );
}
