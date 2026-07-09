import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Wand2,
  Sparkles,
  Play,
  RotateCcw,
  Activity,
  Code2,
  ChevronDown,
  ChevronRight,
  Lightbulb,
  Save,
  Settings as SettingsIcon,
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
import {
  api,
  type BacktestTrade,
  type StrategyGenResult,
} from "@/api/client";
import { useApi, usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";
import AIStrategySummary from "@/components/strategy/AIStrategySummary";
import DateRangePicker from "@/components/common/DateRangePicker";

const EXAMPLE_IDEAS = [
  "均线金叉的时候买入，死叉的时候卖出，要放量确认",
  "跌得太狠、市场恐慌的时候抄底做多，涨回来就止盈",
  "价格突破近期高点并且放量，就追进去做多，快进快出",
  "只做空：涨太急、指标超买的时候做空，跌下来就平仓",
];

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

function StepBadge({ n, active }: { n: number; active: boolean }) {
  return (
    <span
      className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold mr-2 ${
        active
          ? "bg-jarvis-blue text-white"
          : "bg-jarvis-border text-jarvis-text-secondary"
      }`}
    >
      {n}
    </span>
  );
}

export default function AIStrategy() {
  const { symbol } = useSymbol();

  // ── 第一步：想法输入 ──
  const [description, setDescription] = useState("");
  const [genBusy, setGenBusy] = useState(false);
  const [genError, setGenError] = useState("");

  // ── 生成状态轮询（生成中每 2s，空闲不刷）──
  const [genPolling, setGenPolling] = useState(false);
  const { data: genState, refetch: refetchGen } = usePolling(
    () => api.strategyGenerateResult(),
    genPolling ? 2000 : 0,
  );
  const generating = genState?.running ?? false;
  const genResult: StrategyGenResult | null = genState?.result ?? null;
  const generated = Boolean(genResult?.ok);

  useEffect(() => {
    if (genPolling && genState && !genState.running && genState.finished_at > 0) {
      setGenPolling(false);
    }
  }, [genPolling, genState]);

  // ── LLM 配置状态（未配置时引导）──
  const { data: llmCfg, refetch: refetchLlm } = useApi(() => api.llmConfig(), []);
  const llmReady = llmCfg?.configured ?? false;

  // ── 第三步：回测参数与状态 ──
  const [timeframe, setTimeframe] = useState("15m");
  const [start, setStart] = useState("2025-07-01");
  const [end, setEnd] = useState("2026-06-01");
  const [capital, setCapital] = useState(10000);
  const [btBusy, setBtBusy] = useState(false);
  const [btError, setBtError] = useState("");
  const [btStartedHere, setBtStartedHere] = useState(false);
  const [btPolling, setBtPolling] = useState(false);
  const { data: btState, refetch: refetchBt } = usePolling(
    () => api.backtestResult(),
    btPolling ? 2500 : 0,
  );
  const btRunning = btState?.running ?? false;
  const btResult = btStartedHere && !btRunning ? (btState?.result ?? null) : null;
  const btSucceeded = btResult?.status === "succeeded";

  useEffect(() => {
    if (btPolling && btState && !btState.running && btState.finished_at > 0) {
      setBtPolling(false);
    }
  }, [btPolling, btState]);

  // ── 保存到策略库 ──
  const [saveMsg, setSaveMsg] = useState("");
  const [saving, setSaving] = useState(false);

  const [showCode, setShowCode] = useState(false);
  const resultRef = useRef<HTMLDivElement | null>(null);

  const handleGenerate = async () => {
    if (!description.trim() || generating) return;
    setGenBusy(true);
    setGenError("");
    setSaveMsg("");
    setBtStartedHere(false);
    try {
      const res = await api.strategyGenerate({
        description: description.trim(),
        symbol,
        timeframe,
      });
      if (res.ok) {
        setGenPolling(true);
        refetchGen();
      } else {
        setGenError(res.error ?? "启动生成失败");
      }
    } catch (e) {
      setGenError(e instanceof Error ? e.message : "网络错误");
    } finally {
      setGenBusy(false);
    }
  };

  const handleBacktest = async () => {
    if (!genResult?.code || btRunning) return;
    setBtBusy(true);
    setBtError("");
    setSaveMsg("");
    try {
      const res = await api.backtestRun({
        name: genResult.name ?? "ai_strategy",
        code: genResult.code,
        symbol,
        timeframe,
        start,
        end,
        capital,
      });
      if (res.ok) {
        setBtStartedHere(true);
        setBtPolling(true);
        refetchBt();
      } else {
        setBtError(res.error ?? "启动回测失败");
      }
    } catch (e) {
      setBtError(e instanceof Error ? e.message : "网络错误");
    } finally {
      setBtBusy(false);
    }
  };

  const handleSave = async () => {
    if (!genResult?.code || !genResult.name) return;
    setSaving(true);
    setSaveMsg("");
    try {
      const res = await api.strategySaveToHall({
        name: genResult.name,
        code: genResult.code,
        rule: genResult.rule,
        result: (btResult ?? {}) as unknown as Record<string, unknown>,
        reasoning: genResult.reasoning,
      });
      setSaveMsg(res.ok ? "已存入策略库 ✓ 可在「回测」「策略」页使用" : `保存失败: ${res.error ?? ""}`);
    } catch (e) {
      setSaveMsg(`保存失败: ${e instanceof Error ? e.message : "网络错误"}`);
    } finally {
      setSaving(false);
    }
  };

  // 生成完成后滚动到方案区
  useEffect(() => {
    if (generated) {
      resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [generated]);

  const trades = useMemo<BacktestTrade[]>(
    () => (btSucceeded ? (btResult?.trades ?? []) : []),
    [btSucceeded, btResult],
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
    <div className="flex flex-col h-full overflow-y-auto pb-6">
      <h1 className="page-title flex items-center gap-2">
        <Wand2 size={22} />
        AI 策略工坊
        <span className="text-xs font-normal text-jarvis-text-secondary ml-2">
          不会写策略？说出你的想法，AI 帮你生成并回测
        </span>
      </h1>

      {/* 未配置大模型的引导条 */}
      {llmCfg && !llmReady && (
        <div className="card mb-3 border-jarvis-yellow/50 bg-jarvis-yellow/5">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-jarvis-text">
              还没配置大模型，AI 无法生成策略。先去设置页填入 API
              Key（支持 DeepSeek / OpenAI / 任意兼容中转）。
            </p>
            <Link
              to="/settings"
              className="btn-primary flex items-center gap-1.5 text-xs whitespace-nowrap"
              onClick={() => setTimeout(() => refetchLlm(), 500)}
            >
              <SettingsIcon size={14} />
              去配置大模型
            </Link>
          </div>
        </div>
      )}

      {/* 第一步：描述想法 */}
      <div className="card mb-3">
        <h3 className="text-sm font-medium text-jarvis-text mb-2 flex items-center">
          <StepBadge n={1} active />
          用大白话描述你的策略想法
        </h3>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={`例如：${EXAMPLE_IDEAS[0]}`}
          rows={3}
          className="w-full text-sm bg-jarvis-bg border border-jarvis-border rounded p-3 text-jarvis-text resize-none outline-none focus:border-jarvis-blue"
        />
        <div className="flex flex-wrap items-center gap-2 mt-2">
          <Lightbulb size={13} className="text-jarvis-yellow shrink-0" />
          {EXAMPLE_IDEAS.map((idea) => (
            <button
              key={idea}
              onClick={() => setDescription(idea)}
              className="text-xs px-2 py-1 rounded-full border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-blue transition-colors"
            >
              {idea.length > 18 ? idea.slice(0, 18) + "…" : idea}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3 mt-3">
          <button
            onClick={handleGenerate}
            disabled={genBusy || generating || !description.trim() || !llmReady}
            className="btn-primary flex items-center gap-2 disabled:opacity-50"
          >
            {generating || genBusy ? (
              <RotateCcw size={16} className="animate-spin" />
            ) : (
              <Sparkles size={16} />
            )}
            {generating ? "AI 生成中…" : "AI 生成策略"}
          </button>
          {generating && (
            <span className="text-xs text-jarvis-text-secondary">
              大模型思考中（约 10~60 秒）… 已等待 {genState?.elapsed_seconds ?? 0}s
            </span>
          )}
          {(genError || (!generating && genState?.error && !generated)) && (
            <span className="text-xs text-jarvis-red">
              {genError || genState?.error}
            </span>
          )}
        </div>
      </div>

      {/* 第二步：AI 方案 */}
      {generated && genResult && (
        <div className="card mb-3" ref={resultRef}>
          <h3 className="text-sm font-medium text-jarvis-text mb-3 flex items-center">
            <StepBadge n={2} active />
            AI 的策略方案
            <span className="ml-2 font-mono text-xs text-jarvis-blue">
              {genResult.name}
            </span>
          </h3>

          <div className="mb-3">
            <AIStrategySummary result={genResult} />
          </div>

          <button
            onClick={() => setShowCode((v) => !v)}
            className="flex items-center gap-1 text-xs text-jarvis-text-secondary hover:text-jarvis-text"
          >
            {showCode ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            <Code2 size={13} />
            查看策略代码（进阶）
          </button>
          {showCode && (
            <pre className="mt-2 max-h-64 overflow-y-auto text-[11px] font-mono bg-jarvis-bg border border-jarvis-border rounded p-3 text-jarvis-text-secondary whitespace-pre-wrap">
              {genResult.code}
            </pre>
          )}
        </div>
      )}

      {/* 第三步：一键回测 */}
      {generated && genResult && (
        <div className="card mb-3">
          <h3 className="text-sm font-medium text-jarvis-text mb-3 flex items-center">
            <StepBadge n={3} active />
            用历史行情检验它（回测）
          </h3>
          <div className="flex flex-wrap items-end gap-3 mb-3">
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
              <label className="block text-xs text-jarvis-text-secondary mb-1">时间范围</label>
              <DateRangePicker
                start={start}
                end={end}
                onChange={(s, e) => {
                  setStart(s);
                  setEnd(e);
                }}
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
              onClick={handleBacktest}
              disabled={btBusy || btRunning}
              className="btn-success flex items-center gap-2 disabled:opacity-50"
            >
              {btBusy || btRunning ? (
                <RotateCcw size={16} className="animate-spin" />
              ) : (
                <Play size={16} />
              )}
              {btRunning ? "回测中…" : "一键回测"}
            </button>
          </div>

          <div className="flex items-center gap-1 text-xs text-jarvis-text-secondary mb-3">
            <TerminalIcon size={12} />
            回测过程实时输出见
            <Link to="/terminal" className="text-jarvis-blue hover:underline">
              「终端」页
            </Link>
            {btError && <span className="text-jarvis-red ml-2">错误：{btError}</span>}
            {btStartedHere && btState?.error && (
              <span className="text-jarvis-red ml-2">错误：{btState.error}</span>
            )}
          </div>

          {/* 回测结果 */}
          {btSucceeded && btResult ? (
            <>
              <div className="grid grid-cols-3 gap-2 mb-3">
                <MetricCard
                  label="总收益"
                  value={`${btResult.total_return_pct >= 0 ? "+" : ""}${btResult.total_return_pct.toFixed(2)}%`}
                  tone={btResult.total_return_pct >= 0 ? "green" : "red"}
                />
                <MetricCard label="胜率" value={`${btResult.win_rate.toFixed(1)}%`} />
                <MetricCard
                  label="最大回撤"
                  value={`${btResult.max_drawdown_pct.toFixed(1)}%`}
                  tone="red"
                />
                <MetricCard label="成交数" value={`${btResult.total_trades}`} />
                <MetricCard label="盈亏比" value={`${btResult.profit_factor.toFixed(2)}`} />
                <MetricCard label="夏普" value={`${btResult.sharpe_ratio.toFixed(2)}`} />
              </div>

              {equityData.length > 0 && (
                <div className="mb-3">
                  <h4 className="text-xs font-medium text-jarvis-text mb-2 flex items-center gap-1.5">
                    <Activity size={13} className="text-jarvis-blue" />
                    资金曲线
                  </h4>
                  <div className="h-44">
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={equityData}>
                        <defs>
                          <linearGradient id="aiEq" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stopColor="#3fb950" stopOpacity={0.4} />
                            <stop offset="100%" stopColor="#3fb950" stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid strokeDasharray="3 3" stroke="#30363d" />
                        <XAxis dataKey="idx" tick={{ fill: "#8b949e", fontSize: 11 }} />
                        <YAxis
                          tick={{ fill: "#8b949e", fontSize: 11 }}
                          domain={["auto", "auto"]}
                        />
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
                          fill="url(#aiEq)"
                          strokeWidth={1.5}
                        />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              )}

              <div className="flex items-center gap-3">
                <button
                  onClick={handleSave}
                  disabled={saving}
                  className="btn-primary flex items-center gap-2 disabled:opacity-50"
                >
                  <Save size={14} />
                  {saving ? "保存中…" : "满意，存入策略库"}
                </button>
                {saveMsg && (
                  <span
                    className={`text-xs ${saveMsg.includes("✓") ? "text-jarvis-green" : "text-jarvis-red"}`}
                  >
                    {saveMsg}
                  </span>
                )}
                <span className="text-xs text-jarvis-text-secondary ml-auto">
                  不满意？改一改上面的想法再生成一次
                </span>
              </div>
            </>
          ) : (
            <div className="text-sm text-jarvis-text-secondary text-center py-4 bg-jarvis-bg rounded-md">
              {btRunning
                ? `回测进行中，请稍候…（已等待 ${btState?.elapsed_seconds ?? 0}s）`
                : btStartedHere && btState?.error
                  ? "回测未成功，请检查 QD 网关配置（设置页）或稍后重试。"
                  : "点「一键回测」，用真实历史行情检验这个策略。"}
            </div>
          )}
        </div>
      )}

      <p className="text-[11px] text-jarvis-text-secondary text-center mt-auto pt-2">
        回测结果基于历史数据，不代表未来表现；本工具为模拟研究，不构成投资建议。
      </p>
    </div>
  );
}
