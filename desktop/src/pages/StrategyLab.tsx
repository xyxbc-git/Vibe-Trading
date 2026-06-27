import { useState } from "react";
import { Link } from "react-router-dom";
import {
  FlaskConical,
  Play,
  Square,
  Trophy,
  Skull,
  Clock,
  Zap,
  RotateCcw,
  LineChart as LineChartIcon,
  Terminal as TerminalIcon,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Trash2,
} from "lucide-react";
import {
  PieChart,
  Pie,
  Cell,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";

const PIE_COLORS = ["#f85149", "#d29922", "#58a6ff", "#3fb950", "#bc8cff"];

interface EvolveStatus {
  running: boolean;
  mode: string | null;
  symbol: string | null;
  current_round: number;
  total_rounds: number;
  elapsed_seconds: number;
  last_line: string;
  status: string;
}

interface HofResult {
  total_return_pct?: number;
  win_rate?: number;
  profit_factor?: number;
  max_drawdown_pct?: number;
  sharpe_ratio?: number;
  total_trades?: number;
}

interface HofEntry {
  name: string;
  code?: string;
  reasoning?: string;
  result?: HofResult;
}

interface GraveEntry {
  name: string;
  failure_type?: string;
  lesson?: string;
  result?: HofResult;
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function n(v: number | undefined, d = 0): number {
  return typeof v === "number" && Number.isFinite(v) ? v : d;
}

const PLACEHOLDER_STATUS: EvolveStatus = {
  running: false,
  mode: null,
  symbol: null,
  current_round: 0,
  total_rounds: 0,
  elapsed_seconds: 0,
  last_line: "",
  status: "idle",
};

export default function StrategyLab() {
  const { symbol } = useSymbol();
  const [busy, setBusy] = useState(false);
  const [rounds, setRounds] = useState(10);
  const [mode, setMode] = useState<"evolve" | "combo">("evolve");
  const [gravePage, setGravePage] = useState(1);

  const { data: rawStatus, refetch: refetchStatus } = usePolling(
    () => api.evolveStatus(),
    3_000,
  );
  const { data: rawHall } = usePolling(() => api.evolveHallOfFame(), 8_000);
  const { data: rawGraveyard, refetch: refetchGraveyard } = usePolling(
    () => api.evolveGraveyard(),
    8_000,
  );

  const status = (rawStatus as unknown as EvolveStatus) ?? PLACEHOLDER_STATUS;
  const hall = (rawHall as unknown as HofEntry[]) ?? [];
  const graveyard = (rawGraveyard as unknown as GraveEntry[]) ?? [];

  const GRAVE_PAGE_SIZE = 8;
  const graveTotalPages = Math.max(1, Math.ceil(graveyard.length / GRAVE_PAGE_SIZE));
  const graveSafePage = Math.min(gravePage, graveTotalPages);
  const graveOffset = (graveSafePage - 1) * GRAVE_PAGE_SIZE;
  const pagedGraveyard = graveyard.slice(graveOffset, graveOffset + GRAVE_PAGE_SIZE);

  const handleStart = async () => {
    setBusy(true);
    try {
      await api.evolveStart(rounds, symbol, mode);
    } catch {
      // 后端 / QD 网关未就绪
    } finally {
      setBusy(false);
      refetchStatus();
    }
  };

  const handleStop = async () => {
    setBusy(true);
    try {
      await api.evolveStop();
    } catch {
      // 忽略
    } finally {
      setBusy(false);
      refetchStatus();
    }
  };

  const handleClearGraveyard = async () => {
    if (!window.confirm("确定清空策略墓地的全部失败记录吗？此操作不可撤销。")) return;
    try {
      await api.clearGraveyard();
    } catch {
      // 后端未就绪，忽略
    } finally {
      setGravePage(1);
      refetchGraveyard();
    }
  };

  const failureDistribution = graveyard.reduce<Record<string, number>>((acc, g) => {
    const key = g.failure_type || "未知";
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  const pieData = Object.entries(failureDistribution).map(([name, value]) => ({ name, value }));

  const progressPct =
    status.total_rounds > 0
      ? Math.min(100, Math.round((status.current_round / status.total_rounds) * 100))
      : 0;

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <FlaskConical size={22} />
        策略实验室
      </h1>

      {/* 进化引擎控制台 */}
      <div className="card mb-4">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <Zap size={16} className="text-jarvis-yellow" />
            <span className="stat-label">状态</span>
            <span
              className={`ml-1 text-sm font-medium ${
                status.running ? "text-jarvis-green" : "text-jarvis-text-secondary"
              }`}
            >
              {status.running ? "回测进化中" : status.status === "done" ? "已完成" : "空闲"}
            </span>
          </div>

          <div>
            <span className="stat-label">轮次</span>
            <span className="ml-2 text-sm text-jarvis-text font-mono">
              {status.current_round} / {status.total_rounds || "—"}
            </span>
          </div>

          <div className="flex items-center gap-1">
            <Clock size={14} className="text-jarvis-text-secondary" />
            <span className="stat-label">运行</span>
            <span className="ml-1 text-sm text-jarvis-text font-mono">
              {status.elapsed_seconds > 0 ? formatDuration(status.elapsed_seconds) : "—"}
            </span>
          </div>

          <div className="flex items-center gap-2 ml-auto">
            {!status.running ? (
              <>
                <select
                  value={mode}
                  onChange={(e) => setMode(e.target.value as "evolve" | "combo")}
                  className="bg-jarvis-bg border border-jarvis-border rounded px-2 py-1.5 text-xs text-jarvis-text"
                >
                  <option value="evolve">单策略进化</option>
                  <option value="combo">行情组合进化</option>
                </select>
                <select
                  value={rounds}
                  onChange={(e) => setRounds(Number(e.target.value))}
                  className="bg-jarvis-bg border border-jarvis-border rounded px-2 py-1.5 text-xs text-jarvis-text"
                >
                  {[5, 10, 20, 30, 50].map((r) => (
                    <option key={r} value={r}>
                      {r} 轮
                    </option>
                  ))}
                </select>
                <button
                  onClick={handleStart}
                  disabled={busy}
                  className="btn-success flex items-center gap-2 disabled:opacity-50"
                >
                  {busy ? <RotateCcw size={16} className="animate-spin" /> : <Play size={16} />}
                  {busy ? "启动中..." : `进化 ${symbol}`}
                </button>
              </>
            ) : (
              <button
                onClick={handleStop}
                disabled={busy}
                className="btn-danger flex items-center gap-2 disabled:opacity-50"
              >
                <Square size={16} />
                停止
              </button>
            )}
          </div>
        </div>

        {(status.running || status.current_round > 0) && (
          <div className="mt-3">
            <div className="h-1.5 w-full bg-jarvis-border rounded overflow-hidden">
              <div
                className="h-full bg-jarvis-green transition-all duration-500"
                style={{ width: `${progressPct}%` }}
              />
            </div>
            {status.last_line && (
              <p className="mt-2 text-xs font-mono text-jarvis-text-secondary truncate">
                {status.last_line}
              </p>
            )}
          </div>
        )}

        <div className="mt-3 flex items-center gap-1 text-xs text-jarvis-text-secondary">
          <TerminalIcon size={12} />
          回测的开始 / 过程 / 结束全程实时输出，详见
          <Link to="/terminal" className="text-jarvis-blue hover:underline">
            「终端」页
          </Link>
          ；下方结果会在每轮结束后自动刷新。
        </div>
      </div>

      {/* 名人堂 */}
      <div className="mb-4">
        <h2 className="flex items-center gap-2 text-sm font-medium text-jarvis-text mb-3">
          <Trophy size={16} className="text-jarvis-yellow" />
          名人堂（达标的高胜率策略）
        </h2>
        {hall.length === 0 ? (
          <div className="card text-sm text-jarvis-text-secondary text-center py-6">
            暂无达标策略。点上方「进化」开始回测，达标策略会自动进入名人堂。
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-4">
            {hall.map((s) => {
              const r = s.result ?? {};
              const ret = n(r.total_return_pct);
              return (
                <div key={s.name} className="card">
                  <div className="flex items-center justify-between mb-3">
                    <span className="text-sm font-medium text-jarvis-text truncate" title={s.name}>
                      {s.name}
                    </span>
                    <span
                      className={`text-xs px-2 py-0.5 rounded-full ${
                        ret >= 0
                          ? "bg-jarvis-green/20 text-jarvis-green"
                          : "bg-jarvis-red/20 text-jarvis-red"
                      }`}
                    >
                      {ret >= 0 ? "+" : ""}
                      {ret.toFixed(1)}%
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-y-2 text-xs mb-3">
                    <div>
                      <span className="text-jarvis-text-secondary">胜率</span>
                      <span className="ml-2 text-jarvis-text font-mono">
                        {n(r.win_rate).toFixed(1)}%
                      </span>
                    </div>
                    <div>
                      <span className="text-jarvis-text-secondary">盈亏比</span>
                      <span className="ml-2 text-jarvis-text font-mono">
                        {n(r.profit_factor).toFixed(2)}
                      </span>
                    </div>
                    <div>
                      <span className="text-jarvis-text-secondary">夏普</span>
                      <span className="ml-2 text-jarvis-text font-mono">
                        {n(r.sharpe_ratio).toFixed(2)}
                      </span>
                    </div>
                    <div>
                      <span className="text-jarvis-text-secondary">最大回撤</span>
                      <span className="ml-2 text-jarvis-red font-mono">
                        {n(r.max_drawdown_pct).toFixed(1)}%
                      </span>
                    </div>
                  </div>
                  <Link
                    to={`/backtest?name=${encodeURIComponent(s.name)}`}
                    className="btn-primary w-full flex items-center justify-center gap-2 text-xs"
                  >
                    <LineChartIcon size={14} />
                    查看回测
                  </Link>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* 策略墓地 */}
      <div className="mb-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="flex items-center gap-2 text-sm font-medium text-jarvis-text">
            <Skull size={16} className="text-jarvis-red" />
            策略墓地（失败教训）
            {graveyard.length > 0 && (
              <span className="text-xs font-normal text-jarvis-text-secondary">
                · 共 {graveyard.length} 条
              </span>
            )}
          </h2>
          {graveyard.length > 0 && (
            <button
              onClick={handleClearGraveyard}
              className="flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-jarvis-border text-jarvis-text-secondary hover:border-jarvis-red hover:text-jarvis-red transition-colors"
            >
              <Trash2 size={12} />
              清空墓地
            </button>
          )}
        </div>
        <div className="card overflow-x-auto">
          {graveyard.length === 0 ? (
            <p className="text-sm text-jarvis-text-secondary text-center py-4">暂无失败记录。</p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-jarvis-border text-jarvis-text-secondary text-left">
                  <th className="pb-2 font-medium">策略名</th>
                  <th className="pb-2 font-medium">失败原因</th>
                  <th className="pb-2 font-medium">教训</th>
                  <th className="pb-2 font-medium text-right">收益</th>
                  <th className="pb-2 font-medium text-right">最大回撤</th>
                </tr>
              </thead>
              <tbody>
                {pagedGraveyard.map((g, i) => {
                  const r = g.result ?? {};
                  const idx = graveOffset + i;
                  return (
                    <tr key={`${g.name}-${idx}`} className="border-b border-jarvis-border/50 last:border-0">
                      <td className="py-2 text-jarvis-text">{g.name}</td>
                      <td className="py-2">
                        <span className="text-xs px-2 py-0.5 rounded-full bg-jarvis-red/20 text-jarvis-red">
                          {g.failure_type || "未知"}
                        </span>
                      </td>
                      <td className="py-2 text-jarvis-text-secondary">{g.lesson || "—"}</td>
                      <td className="py-2 text-right font-mono text-jarvis-text-secondary">
                        {n(r.total_return_pct).toFixed(1)}%
                      </td>
                      <td className="py-2 text-right text-jarvis-red font-mono">
                        {n(r.max_drawdown_pct).toFixed(1)}%
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          {graveyard.length > GRAVE_PAGE_SIZE && (
            <div className="flex items-center justify-between pt-3 mt-1 border-t border-jarvis-border/50">
              <span className="text-xs text-jarvis-text-secondary">
                共 {graveyard.length} 条 · 第 {graveSafePage}/{graveTotalPages} 页
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setGravePage(1)}
                  disabled={graveSafePage <= 1}
                  className="flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-jarvis-border text-jarvis-text disabled:opacity-40 disabled:cursor-not-allowed hover:border-jarvis-blue"
                >
                  <ChevronsLeft size={12} />
                  首页
                </button>
                <button
                  onClick={() => setGravePage((p) => Math.max(1, p - 1))}
                  disabled={graveSafePage <= 1}
                  className="flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-jarvis-border text-jarvis-text disabled:opacity-40 disabled:cursor-not-allowed hover:border-jarvis-blue"
                >
                  <ChevronLeft size={12} />
                  上一页
                </button>
                <button
                  onClick={() => setGravePage((p) => Math.min(graveTotalPages, p + 1))}
                  disabled={graveSafePage >= graveTotalPages}
                  className="flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-jarvis-border text-jarvis-text disabled:opacity-40 disabled:cursor-not-allowed hover:border-jarvis-blue"
                >
                  下一页
                  <ChevronRight size={12} />
                </button>
                <button
                  onClick={() => setGravePage(graveTotalPages)}
                  disabled={graveSafePage >= graveTotalPages}
                  className="flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-jarvis-border text-jarvis-text disabled:opacity-40 disabled:cursor-not-allowed hover:border-jarvis-blue"
                >
                  末页
                  <ChevronsRight size={12} />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* 失败原因分布 */}
      {pieData.length > 0 && (
        <div className="card">
          <h3 className="text-sm font-medium text-jarvis-text mb-3">失败原因分布</h3>
          <div className="h-52">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={pieData}
                  cx="50%"
                  cy="50%"
                  outerRadius={70}
                  innerRadius={40}
                  dataKey="value"
                  label={({ name, percent }) => `${name} ${((percent ?? 0) * 100).toFixed(0)}%`}
                  labelLine={false}
                >
                  {pieData.map((_, i) => (
                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{
                    background: "#161b22",
                    border: "1px solid #30363d",
                    borderRadius: 8,
                    color: "#e6edf3",
                  }}
                />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  );
}
