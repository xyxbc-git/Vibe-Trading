import { useEffect, useMemo, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import {
  FlaskConical,
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

interface HofItem {
  name: string;
  code?: string;
  result?: { win_rate?: number; total_return_pct?: number };
}

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
      <p className={`stat-value text-lg mt-1 font-mono ${color}`}>{value}</p>
    </div>
  );
}

export default function Backtest() {
  const [params] = useSearchParams();
  const initialName = params.get("name") ?? "";
  const { symbol } = useSymbol();

  const [selected, setSelected] = useState(initialName);
  const [code, setCode] = useState("");
  const [timeframe, setTimeframe] = useState("15m");
  const [start, setStart] = useState("2025-01-01");
  const [end, setEnd] = useState("2026-06-01");
  const [capital, setCapital] = useState(10000);
  const [busy, setBusy] = useState(false);

  const { data: rawHall } = useApi(() => api.evolveHallOfFame(), []);
  const hall = (rawHall as unknown as HofItem[]) ?? [];

  // 选择策略后把代码灌进编辑器（"__custom__" 表示自定义/新建，不覆盖编辑器）。
  useEffect(() => {
    if (hall.length === 0) return;
    if (selected === "__custom__") return;
    const name = selected || initialName || hall[0]?.name || "";
    if (!selected && name) setSelected(name);
    const entry = hall.find((h) => h.name === name);
    if (entry?.code !== undefined) setCode(entry.code ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rawHall, selected]);

  const { data: state, refetch } = usePolling(() => api.backtestResult(), 2500);
  const running = state?.running ?? false;
  const result = state?.result ?? null;
  const succeeded = result?.status === "succeeded";

  const handleRun = async () => {
    if (!code.trim()) return;
    setBusy(true);
    try {
      await api.backtestRun({
        name: selected,
        code,
        symbol,
        timeframe,
        start,
        end,
        capital,
      });
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
      if (typeof explicit === "number" && Number.isFinite(explicit)) bal = explicit;
      else bal += tradePnl(t);
      return { idx: i + 1, equity: Math.round(bal * 100) / 100 };
    });
  }, [trades, capital]);

  return (
    <div className="flex flex-col h-full">
      <h1 className="page-title flex items-center gap-2">
        <FlaskConical size={22} />
        回测工作台
      </h1>

      {/* 顶部工具栏：选策略 + 参数 + 运行 */}
      <div className="card mb-3">
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="block text-xs text-jarvis-text-secondary mb-1">策略</label>
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              className="bg-jarvis-bg border border-jarvis-border rounded px-2 py-1.5 text-sm text-jarvis-text min-w-44"
            >
              {hall.map((h) => (
                <option key={h.name} value={h.name}>
                  {h.name}
                </option>
              ))}
              <option value="__custom__">✎ 自定义 / 新建</option>
            </select>
          </div>
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
            <label className="block text-xs text-jarvis-text-secondary mb-1">开始</label>
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              className="bg-jarvis-bg border border-jarvis-border rounded px-2 py-1.5 text-sm text-jarvis-text"
            />
          </div>
          <div>
            <label className="block text-xs text-jarvis-text-secondary mb-1">结束</label>
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
            disabled={busy || running || !code.trim()}
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
        <div className="mt-2 flex items-center gap-1 text-xs text-jarvis-text-secondary">
          <TerminalIcon size={12} />
          回测过程实时输出见
          <Link to="/terminal" className="text-jarvis-blue hover:underline">
            「终端」页
          </Link>
          {state?.error && <span className="text-jarvis-red ml-2">错误：{state.error}</span>}
        </div>
      </div>

      {/* 主体：左代码编辑器 / 右结果 */}
      <div className="grid grid-cols-2 gap-3 flex-1 min-h-0">
        {/* 左：代码编辑器 */}
        <div className="card flex flex-col min-h-0">
          <h3 className="text-sm font-medium text-jarvis-text mb-2 flex items-center gap-2">
            <Code2 size={14} className="text-jarvis-blue" />
            策略代码（可编辑）
          </h3>
          <textarea
            value={code}
            onChange={(e) => setCode(e.target.value)}
            spellCheck={false}
            placeholder="选择一个策略，或在此编写/粘贴 QD IndicatorStrategy 代码"
            className="flex-1 min-h-[420px] w-full font-mono text-xs bg-jarvis-bg border border-jarvis-border rounded p-3 text-jarvis-text resize-none outline-none focus:border-jarvis-blue"
          />
        </div>

        {/* 右：结果 */}
        <div className="flex flex-col gap-3 min-h-0 overflow-y-auto">
          {/* 指标卡 */}
          {succeeded && result ? (
            <div className="grid grid-cols-3 gap-2">
              <MetricCard
                label="总收益"
                value={`${result.total_return_pct >= 0 ? "+" : ""}${result.total_return_pct.toFixed(2)}%`}
                tone={result.total_return_pct >= 0 ? "green" : "red"}
              />
              <MetricCard label="胜率" value={`${result.win_rate.toFixed(1)}%`} />
              <MetricCard label="回撤" value={`${result.max_drawdown_pct.toFixed(1)}%`} tone="red" />
              <MetricCard label="成交数" value={`${result.total_trades}`} />
              <MetricCard label="盈亏比" value={`${result.profit_factor.toFixed(2)}`} />
              <MetricCard label="夏普" value={`${result.sharpe_ratio.toFixed(2)}`} />
            </div>
          ) : (
            <div className="card text-sm text-jarvis-text-secondary text-center py-6">
              {running ? "回测进行中，请稍候…" : "点「运行回测」查看资金曲线与逐笔成交。"}
            </div>
          )}

          {/* 资金曲线 */}
          {succeeded && equityData.length > 0 && (
            <div className="card">
              <h3 className="text-sm font-medium text-jarvis-text mb-2 flex items-center gap-2">
                <Activity size={14} className="text-jarvis-blue" />
                资金曲线
              </h3>
              <div className="h-48">
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
            <div className="card overflow-x-auto">
              <h3 className="text-sm font-medium text-jarvis-text mb-2">
                交易记录（{trades.length}）
              </h3>
              {trades.length === 0 ? (
                <p className="text-sm text-jarvis-text-secondary text-center py-4">本次回测无成交</p>
              ) : (
                <div className="max-h-72 overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-jarvis-card">
                      <tr className="border-b border-jarvis-border text-jarvis-text-secondary text-left">
                        <th className="pb-2 pr-2 font-medium">#</th>
                        <th className="pb-2 pr-2 font-medium">类型</th>
                        <th className="pb-2 pr-2 font-medium">入场</th>
                        <th className="pb-2 pr-2 font-medium">出场</th>
                        <th className="pb-2 pr-2 font-medium text-right">入场价</th>
                        <th className="pb-2 pr-2 font-medium text-right">出场价</th>
                        <th className="pb-2 font-medium text-right">盈亏</th>
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
                            <td className="py-1 pr-2 text-jarvis-text-secondary">{i + 1}</td>
                            <td className="py-1 pr-2">
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
                            <td className="py-1 pr-2 text-jarvis-text-secondary">
                              {(t.entry_time as string) ?? "—"}
                            </td>
                            <td className="py-1 pr-2 text-jarvis-text-secondary">
                              {(t.exit_time as string) ?? "—"}
                            </td>
                            <td className="py-1 pr-2 text-right text-jarvis-text">
                              {t.entry_price != null ? num(t.entry_price).toFixed(2) : "—"}
                            </td>
                            <td className="py-1 pr-2 text-right text-jarvis-text">
                              {t.exit_price != null ? num(t.exit_price).toFixed(2) : "—"}
                            </td>
                            <td
                              className={`py-1 text-right ${
                                pnl >= 0 ? "text-jarvis-green" : "text-jarvis-red"
                              }`}
                            >
                              {pnl >= 0 ? "+" : ""}
                              {pnl.toFixed(2)}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
