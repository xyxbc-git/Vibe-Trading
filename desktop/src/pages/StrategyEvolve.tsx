import { useEffect, useRef, useState } from "react";
import {
  Dna,
  Play,
  Square,
  Trophy,
  History,
  Loader2,
  Save,
  CheckCircle2,
  TrendingUp,
} from "lucide-react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import {
  api,
  type StrategyEvolveStatus,
  type StrategyEvolveRun,
  type StrategyEvolveRound,
  type StrategyEvolveRunBrief,
} from "@/api/client";

function MetricCell({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div>
      <p className="text-xs text-jarvis-text-secondary">{label}</p>
      <p className={`text-sm font-mono font-medium ${good === undefined ? "text-jarvis-text" : good ? "text-jarvis-green" : "text-jarvis-red"}`}>
        {value}
      </p>
    </div>
  );
}

function RoundBadge({ status }: { status?: string }) {
  const ok = status === "succeeded";
  return (
    <span className={`px-1.5 py-0.5 text-[10px] rounded ${ok ? "bg-jarvis-green/15 text-jarvis-green" : "bg-jarvis-red/15 text-jarvis-red"}`}>
      {ok ? "回测成功" : status === "gen_failed" ? "生成失败" : "回测失败"}
    </span>
  );
}

function TopCard({
  entry,
  rank,
  onSave,
  saved,
  saving,
}: {
  entry: StrategyEvolveRound;
  rank: number;
  onSave: (e: StrategyEvolveRound) => void;
  saved: boolean;
  saving: boolean;
}) {
  const m = entry.metrics ?? {};
  const medal = ["🥇", "🥈", "🥉"][rank] ?? "";
  return (
    <div className="card">
      <div className="flex items-center justify-between mb-2">
        <p className="text-sm font-medium text-jarvis-text">
          {medal} 第{entry.round}轮 · {entry.name}
        </p>
        <span className="text-xs font-mono text-jarvis-blue">评分 {entry.fitness ?? "—"}</span>
      </div>
      {entry.explain && (
        <p className="text-xs text-jarvis-text-secondary leading-5 mb-3">{entry.explain}</p>
      )}
      <div className="grid grid-cols-3 gap-2 mb-3">
        <MetricCell
          label="收益"
          value={`${m.total_return_pct ?? 0}%`}
          good={(m.total_return_pct ?? 0) >= 0}
        />
        <MetricCell label="胜率" value={`${m.win_rate ?? 0}%`} good={(m.win_rate ?? 0) >= 50} />
        <MetricCell
          label="回撤"
          value={`${m.max_drawdown_pct ?? 0}%`}
          good={Math.abs(m.max_drawdown_pct ?? 0) <= 15}
        />
        <MetricCell label="盈亏比" value={`${m.profit_factor ?? 0}`} good={(m.profit_factor ?? 0) >= 1.2} />
        <MetricCell label="夏普" value={`${m.sharpe_ratio ?? 0}`} />
        <MetricCell label="交易数" value={`${m.total_trades ?? 0} 笔`} />
      </div>
      <button
        onClick={() => onSave(entry)}
        disabled={saved || saving || !entry.code}
        className={`w-full flex items-center justify-center gap-1.5 px-3 py-1.5 text-xs rounded border transition-colors disabled:opacity-60 ${
          saved
            ? "border-jarvis-green/60 text-jarvis-green"
            : "border-jarvis-blue/60 text-jarvis-blue hover:bg-jarvis-blue/10"
        }`}
      >
        {saved ? <CheckCircle2 size={12} /> : <Save size={12} />}
        {saved ? "已存入策略库" : saving ? "保存中…" : "存入策略库"}
      </button>
    </div>
  );
}

export default function StrategyEvolve() {
  const [description, setDescription] = useState("");
  const [rounds, setRounds] = useState(5);
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState("15m");
  const [status, setStatus] = useState<StrategyEvolveStatus | null>(null);
  const [result, setResult] = useState<StrategyEvolveRun | null>(null);
  const [runsList, setRunsList] = useState<StrategyEvolveRunBrief[]>([]);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [savedNames, setSavedNames] = useState<Record<string, boolean>>({});
  const [savingName, setSavingName] = useState<string | null>(null);
  const lastRunIdRef = useRef<string | null>(null);
  const resultLoadedForRef = useRef<string | null>(null);

  const running = status?.running ?? false;
  const run = status?.run;

  const refreshRuns = () => {
    api.strategyEvolveRuns().then((r) => setRunsList(r.runs ?? [])).catch(() => {});
  };

  // 状态轮询：运行中 3s 一次；结束后拉完整 result（含 Top3）
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const poll = async () => {
      try {
        const st = await api.strategyEvolveStatus();
        setStatus(st);
        if (st.run_id) lastRunIdRef.current = st.run_id;
        if (!st.running && st.run_id && resultLoadedForRef.current !== st.run_id) {
          resultLoadedForRef.current = st.run_id;
          const res = await api.strategyEvolveResult(st.run_id);
          if (res.ok && res.run) setResult(res.run);
          refreshRuns();
        }
      } catch {
        /* dashboard 未启动等场景静默 */
      }
    };
    poll();
    timer = setInterval(poll, 3000);
    return () => {
      if (timer) clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    refreshRuns();
  }, []);

  const start = async (resumeRunId?: string) => {
    if (busy) return;
    if (!resumeRunId && !description.trim()) {
      setActionMsg("请先用一句大白话描述你的策略想法");
      return;
    }
    setBusy(true);
    setActionMsg(null);
    setResult(null);
    setSavedNames({});
    try {
      const res = await api.strategyEvolveStart({
        description: description.trim(),
        rounds,
        symbol,
        timeframe,
        resume_run_id: resumeRunId ?? "",
      });
      if (!res.ok) {
        setActionMsg(res.error ?? "启动失败");
      } else {
        resultLoadedForRef.current = null;
        setActionMsg(null);
      }
    } catch (e) {
      setActionMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    try {
      const res = await api.strategyEvolveStop();
      if (!res.ok) setActionMsg(res.error ?? "停止失败");
      else setActionMsg("已请求停止：当前轮跑完后停下（快照已保留，可续跑）");
    } catch (e) {
      setActionMsg((e as Error).message);
    }
  };

  const saveToHall = async (entry: StrategyEvolveRound) => {
    if (!entry.code) return;
    setSavingName(entry.name);
    try {
      const res = await api.strategySaveToHall({
        name: entry.name,
        code: entry.code,
        rule: entry.rule,
        result: entry.metrics as Record<string, unknown>,
        reasoning: entry.explain ?? "",
      });
      if (res.ok) {
        setSavedNames((s) => ({ ...s, [entry.name]: true }));
      } else {
        setActionMsg(res.error ?? "保存失败");
      }
    } catch (e) {
      setActionMsg((e as Error).message);
    } finally {
      setSavingName(null);
    }
  };

  const history: StrategyEvolveRound[] = run?.history ?? result?.history ?? [];
  const chartData = history.map((h) => ({
    name: `R${h.round}`,
    收益: h.metrics?.status === "succeeded" ? h.metrics?.total_return_pct ?? 0 : null,
    评分: h.fitness,
    胜率: h.metrics?.status === "succeeded" ? h.metrics?.win_rate ?? 0 : null,
  }));
  const top3 = result?.top3 ?? [];

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <Dna size={22} />
        策略自动进化
      </h1>

      {/* 想法输入 + 启动 */}
      <div className="card mb-4">
        <p className="stat-label mb-2">你的想法（大白话即可，贾维斯自动迭代改进）</p>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          disabled={running}
          placeholder="例：跌得恐慌的时候买入，涨回来一点就卖，别扛单"
          className="w-full h-16 px-3 py-2 text-sm bg-jarvis-bg border border-jarvis-border rounded-lg text-jarvis-text placeholder:text-jarvis-text-secondary/50 focus:outline-none focus:border-jarvis-blue/60 resize-none"
        />
        <div className="flex items-center gap-3 mt-3 flex-wrap">
          <label className="flex items-center gap-1.5 text-xs text-jarvis-text-secondary">
            进化轮数
            <input
              type="number"
              min={1}
              max={10}
              value={rounds}
              disabled={running}
              onChange={(e) => setRounds(Math.max(1, Math.min(10, Number(e.target.value) || 5)))}
              className="w-14 px-2 py-1 bg-jarvis-bg border border-jarvis-border rounded text-jarvis-text font-mono"
            />
          </label>
          <label className="flex items-center gap-1.5 text-xs text-jarvis-text-secondary">
            币种
            <select
              value={symbol}
              disabled={running}
              onChange={(e) => setSymbol(e.target.value)}
              className="px-2 py-1 bg-jarvis-bg border border-jarvis-border rounded text-jarvis-text"
            >
              {["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"].map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-xs text-jarvis-text-secondary">
            周期
            <select
              value={timeframe}
              disabled={running}
              onChange={(e) => setTimeframe(e.target.value)}
              className="px-2 py-1 bg-jarvis-bg border border-jarvis-border rounded text-jarvis-text"
            >
              {["15m", "1h", "4h"].map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </label>
          <div className="ml-auto flex items-center gap-2">
            {running ? (
              <button
                onClick={stop}
                className="flex items-center gap-1.5 px-4 py-1.5 text-xs rounded border border-jarvis-red/60 text-jarvis-red hover:bg-jarvis-red/10 transition-colors"
              >
                <Square size={12} />
                停止进化
              </button>
            ) : (
              <button
                onClick={() => start()}
                disabled={busy}
                className="flex items-center gap-1.5 px-4 py-1.5 text-xs rounded border border-jarvis-green/60 text-jarvis-green hover:bg-jarvis-green/10 transition-colors disabled:opacity-50"
              >
                <Play size={12} />
                {busy ? "启动中…" : "开始进化"}
              </button>
            )}
          </div>
        </div>
        {actionMsg && <p className="text-xs text-jarvis-yellow mt-2">{actionMsg}</p>}
      </div>

      {/* 运行状态 + 进化曲线 */}
      {(running || history.length > 0) && (
        <div className="card mb-4">
          <div className="flex items-center justify-between mb-3">
            <p className="stat-label flex items-center gap-2 mb-0">
              <TrendingUp size={14} />
              进化曲线
            </p>
            <span className="text-xs text-jarvis-text-secondary flex items-center gap-1.5">
              {running && <Loader2 size={12} className="animate-spin" />}
              {running
                ? `进化中 · 第 ${(run?.rounds_done ?? 0) + 1}/${run?.rounds_planned ?? rounds} 轮 · 已用 ${Math.round(status?.elapsed_seconds ?? 0)}s`
                : `已完成 ${history.length} 轮`}
            </span>
          </div>
          {chartData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={chartData} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#30363d" />
                <XAxis dataKey="name" tick={{ fill: "#8b949e", fontSize: 12 }} />
                <YAxis tick={{ fill: "#8b949e", fontSize: 12 }} />
                <Tooltip
                  contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                  labelStyle={{ color: "#e6edf3" }}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Line type="monotone" dataKey="收益" stroke="#3fb950" strokeWidth={2} connectNulls />
                <Line type="monotone" dataKey="评分" stroke="#58a6ff" strokeWidth={2} connectNulls />
                <Line type="monotone" dataKey="胜率" stroke="#d29922" strokeWidth={1.5} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-sm text-jarvis-text-secondary text-center py-6">
              第 1 轮进行中（生成策略 + 回测约需 1~3 分钟）…
            </p>
          )}

          {/* 轮次明细 */}
          {history.length > 0 && (
            <div className="mt-3 max-h-44 overflow-y-auto">
              {history.slice().reverse().map((h) => (
                <div
                  key={h.round}
                  className="flex items-center gap-3 py-1.5 border-b border-jarvis-border/50 last:border-0 text-xs"
                >
                  <span className="font-mono text-jarvis-text-secondary w-8">R{h.round}</span>
                  <span className="font-mono text-jarvis-text flex-1 truncate" title={h.name}>
                    {h.name}
                  </span>
                  <RoundBadge status={h.metrics?.status} />
                  {h.metrics?.status === "succeeded" ? (
                    <span className="font-mono text-jarvis-text-secondary">
                      收益 {h.metrics?.total_return_pct}% · 胜率 {h.metrics?.win_rate}% · 交易{" "}
                      {h.metrics?.total_trades} 笔 · 评分 {h.fitness}
                    </span>
                  ) : (
                    <span className="font-mono text-jarvis-red/80 truncate max-w-64" title={h.metrics?.error ?? ""}>
                      {h.metrics?.error ?? "失败"}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Top3 结果卡片 */}
      {!running && top3.length > 0 && (
        <>
          <p className="stat-label flex items-center gap-2 mb-2">
            <Trophy size={14} />
            本次进化 Top{top3.length}（可一键存入策略库，供回测页与实盘引擎复用）
          </p>
          <div className="grid grid-cols-3 gap-4 mb-4">
            {top3.map((e, i) => (
              <TopCard
                key={e.name + e.round}
                entry={e}
                rank={i}
                onSave={saveToHall}
                saved={!!savedNames[e.name]}
                saving={savingName === e.name}
              />
            ))}
          </div>
        </>
      )}

      {/* 历史任务（断点续跑） */}
      {runsList.length > 0 && (
        <div className="card">
          <p className="stat-label flex items-center gap-2 mb-2">
            <History size={14} />
            历史进化任务
          </p>
          {runsList.map((r) => (
            <div
              key={r.run_id}
              className="flex items-center gap-3 py-1.5 border-b border-jarvis-border/50 last:border-0 text-xs"
            >
              <span className="font-mono text-jarvis-text-secondary">{r.updated_at ?? ""}</span>
              <span className="text-jarvis-text flex-1 truncate" title={r.description}>
                {r.description || "（无描述）"}
              </span>
              <span className="font-mono text-jarvis-text-secondary">
                {r.symbol} {r.timeframe} · {r.rounds_done}/{r.rounds_planned} 轮
              </span>
              <span
                className={`px-1.5 py-0.5 rounded text-[10px] ${
                  r.status === "succeeded"
                    ? "bg-jarvis-green/15 text-jarvis-green"
                    : r.status === "running"
                      ? "bg-jarvis-blue/15 text-jarvis-blue"
                      : "bg-jarvis-red/15 text-jarvis-red"
                }`}
              >
                {r.status === "succeeded" ? "完成" : r.status === "running" ? "运行中" : r.status === "stopped" ? "已停止" : "失败"}
              </span>
              {(r.status === "stopped" || r.status === "failed") &&
                r.rounds_done < r.rounds_planned && (
                  <button
                    onClick={() => start(r.run_id)}
                    disabled={running || busy}
                    className="px-2 py-0.5 rounded border border-jarvis-blue/60 text-jarvis-blue hover:bg-jarvis-blue/10 disabled:opacity-50"
                  >
                    续跑
                  </button>
                )}
              <button
                onClick={async () => {
                  const res = await api.strategyEvolveResult(r.run_id);
                  if (res.ok && res.run) {
                    setResult(res.run);
                    setStatus((s) => (s ? { ...s, run: null } : s));
                  }
                }}
                className="px-2 py-0.5 rounded border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text"
              >
                查看
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
