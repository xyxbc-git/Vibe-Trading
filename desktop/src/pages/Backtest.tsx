import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams, Link } from "react-router-dom";
import {
  FlaskConical,
  Play,
  RotateCcw,
  Activity,
  Code2,
  Wand2,
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
import { api, type BacktestTrade, type StrategyGenResult } from "@/api/client";
import { useApi, usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import DateRangePicker from "@/components/common/DateRangePicker";
import AIGeneratePanel from "@/components/strategy/AIGeneratePanel";
import { clsx } from "clsx";

interface HofItem {
  name: string;
  code?: string;
  result?: { win_rate?: number; total_return_pct?: number };
}

const TIMEFRAMES = ["15m", "1h", "4h", "1d"];
const CAPITAL_QUICK = [
  { label: "1万", value: 10_000 },
  { label: "5万", value: 50_000 },
  { label: "10万", value: 100_000 },
];

/** 工具栏分组：小标题 + 控件行 */
function ToolbarGroup({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] tracking-wider text-jarvis-text-secondary/70">
        {label}
      </span>
      <div className="flex items-center gap-2">{children}</div>
    </div>
  );
}

/** 周期分段按钮 */
function TimeframeSegmented({
  value,
  onChange,
}: {
  value: string;
  onChange: (tf: string) => void;
}) {
  return (
    <div className="flex rounded-md border border-jarvis-border overflow-hidden">
      {TIMEFRAMES.map((tf, i) => (
        <button
          key={tf}
          onClick={() => onChange(tf)}
          className={clsx(
            "px-2.5 py-1.5 text-xs font-mono transition-colors",
            i > 0 && "border-l border-jarvis-border",
            value === tf
              ? "bg-jarvis-blue/20 text-jarvis-blue"
              : "bg-jarvis-bg text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/5",
          )}
        >
          {tf}
        </button>
      ))}
    </div>
  );
}

/** 初始资金：$ 前缀 + 千分位显示 + 快捷额 */
function CapitalInput({
  value,
  onChange,
}: {
  value: number;
  onChange: (v: number) => void;
}) {
  const handleInput = (raw: string) => {
    const digits = raw.replace(/\D/g, "").slice(0, 12);
    onChange(digits ? Number(digits) : 0);
  };
  return (
    <>
      <div className="relative">
        <span className="absolute left-2 top-1/2 -translate-y-1/2 text-xs font-mono text-jarvis-text-secondary pointer-events-none">
          $
        </span>
        <input
          value={value ? value.toLocaleString("en-US") : ""}
          onChange={(e) => handleInput(e.target.value)}
          inputMode="numeric"
          placeholder="10,000"
          className="bg-jarvis-bg border border-jarvis-border rounded-md pl-5 pr-2 py-1.5 text-sm font-mono text-jarvis-text w-28 focus:outline-none focus:border-jarvis-blue"
        />
      </div>
      {CAPITAL_QUICK.map((q) => (
        <button
          key={q.value}
          onClick={() => onChange(q.value)}
          className={clsx(
            "px-1.5 py-1 text-[10px] rounded border transition-colors",
            value === q.value
              ? "bg-jarvis-blue/15 border-jarvis-blue text-jarvis-blue"
              : "bg-jarvis-bg border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
          )}
        >
          {q.label}
        </button>
      ))}
    </>
  );
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
  const { symbol, setSymbol, supported } = useSymbol();

  const [selected, setSelected] = useState(initialName);
  const [code, setCode] = useState("");
  const [timeframe, setTimeframe] = useState("15m");
  const [start, setStart] = useState("2025-01-01");
  const [end, setEnd] = useState("2026-06-01");
  const [capital, setCapital] = useState(10000);
  const [busy, setBusy] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);
  const [aiName, setAiName] = useState("");

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

  // AI 面板生成成功：代码填入编辑器，策略选择切到「自定义」防止名人堂联动覆盖
  const handleAiGenerated = useCallback(
    (g: { name: string; code: string; result: StrategyGenResult }) => {
      setSelected("__custom__");
      setAiName(g.name);
      setCode(g.code);
    },
    [],
  );

  const handleRun = async () => {
    if (!code.trim()) return;
    setBusy(true);
    try {
      await api.backtestRun({
        name: selected === "__custom__" ? aiName : selected,
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

      {/* 顶部工具栏：策略 | 市场参数 | 时间范围 | 资金 | 运行 */}
      <div className="card mb-3">
        <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
          <ToolbarGroup label="策略">
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              className="bg-jarvis-bg border border-jarvis-border rounded-md px-2 py-1.5 text-sm text-jarvis-text min-w-44 focus:outline-none focus:border-jarvis-blue"
            >
              {hall.map((h) => (
                <option key={h.name} value={h.name}>
                  {h.name}
                </option>
              ))}
              <option value="__custom__">✎ 自定义 / 新建</option>
            </select>
          </ToolbarGroup>

          <ToolbarGroup label="市场参数">
            <select
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              title="切换币种（全局生效，自定义币种在顶栏添加）"
              className="bg-jarvis-bg border border-jarvis-border rounded-md px-2 py-1.5 text-sm font-mono text-jarvis-text focus:outline-none focus:border-jarvis-blue"
            >
              {supported.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
            <TimeframeSegmented value={timeframe} onChange={setTimeframe} />
          </ToolbarGroup>

          <ToolbarGroup label="时间范围">
            <DateRangePicker
              start={start}
              end={end}
              onChange={(s, e) => {
                setStart(s);
                setEnd(e);
              }}
            />
          </ToolbarGroup>

          <ToolbarGroup label="初始资金">
            <CapitalInput value={capital} onChange={setCapital} />
          </ToolbarGroup>

          <button
            onClick={handleRun}
            disabled={busy || running || !code.trim()}
            className="btn-success flex items-center gap-2 disabled:opacity-50 ml-auto shrink-0 whitespace-nowrap"
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
        <div className="card flex flex-col min-h-0 overflow-y-auto">
          <h3 className="text-sm font-medium text-jarvis-text mb-2 flex items-center gap-2">
            <Code2 size={14} className="text-jarvis-blue" />
            策略代码（可编辑）
            {!aiOpen && (
              <button
                onClick={() => setAiOpen(true)}
                className="ml-auto flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-jarvis-blue/50 text-jarvis-blue hover:bg-jarvis-blue/10 transition-colors"
              >
                <Wand2 size={12} />
                AI 帮我写
              </button>
            )}
          </h3>

          {/* 内联 AI 生成面板 */}
          {aiOpen && (
            <AIGeneratePanel
              symbol={symbol}
              timeframe={timeframe}
              onGenerated={handleAiGenerated}
              onClose={() => setAiOpen(false)}
            />
          )}

          {/* 代码框为空时的友好引导 */}
          {!code.trim() && !aiOpen && (
            <div className="border border-dashed border-jarvis-border rounded-md p-4 mb-2 text-center">
              <p className="text-sm text-jarvis-text-secondary mb-2">
                这里放的是策略代码——不会写也没关系，用大白话说出想法，AI 自动生成可回测的策略。
              </p>
              <button
                onClick={() => setAiOpen(true)}
                className="btn-primary inline-flex items-center gap-1.5 text-xs"
              >
                <Wand2 size={13} />
                不会写代码？让 AI 帮你写
              </button>
            </div>
          )}

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
                <p className="text-sm text-jarvis-text-secondary text-center py-4">
                  本次回测无成交
                  {result?.diagnosis && (
                    <span className="block mt-1 text-xs text-jarvis-yellow">{result.diagnosis}</span>
                  )}
                </p>
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
