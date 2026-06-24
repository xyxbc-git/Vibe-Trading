import { useState } from "react";
import {
  FlaskConical,
  Play,
  Trophy,
  Skull,
  TrendingUp,
  Clock,
  Zap,
  RotateCcw,
} from "lucide-react";
import {
  PieChart,
  Pie,
  Cell,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/useApi";

const PIE_COLORS = ["#f85149", "#d29922", "#58a6ff", "#3fb950", "#bc8cff"];

interface EvolveStatus {
  running: boolean;
  current_round: number;
  total_rounds: number;
  elapsed_seconds: number;
  status: string;
}

interface Strategy {
  name: string;
  win_rate: number;
  profit_factor: number;
  total_return: number;
  sharpe: number;
  max_drawdown: number;
  trades: number;
  equity_curve?: number[];
}

interface GraveyardEntry {
  name: string;
  failure_reason: string;
  lesson: string;
  generation: number;
  max_drawdown: number;
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

const PLACEHOLDER_STATUS: EvolveStatus = {
  running: false,
  current_round: 0,
  total_rounds: 0,
  elapsed_seconds: 0,
  status: "idle",
};

const PLACEHOLDER_HALL: Strategy[] = [
  {
    name: "MomentumBreak-v3",
    win_rate: 0.62,
    profit_factor: 2.1,
    total_return: 34.5,
    sharpe: 1.8,
    max_drawdown: -8.2,
    trades: 156,
    equity_curve: [100, 103, 101, 108, 112, 110, 118, 122, 120, 128, 134],
  },
  {
    name: "MeanRevert-alpha",
    win_rate: 0.58,
    profit_factor: 1.7,
    total_return: 21.3,
    sharpe: 1.5,
    max_drawdown: -6.1,
    trades: 203,
    equity_curve: [100, 102, 104, 103, 107, 109, 111, 114, 116, 119, 121],
  },
  {
    name: "TrendFollow-v2",
    win_rate: 0.45,
    profit_factor: 2.8,
    total_return: 28.7,
    sharpe: 1.6,
    max_drawdown: -12.4,
    trades: 89,
    equity_curve: [100, 98, 96, 102, 108, 105, 115, 118, 122, 126, 129],
  },
];

const PLACEHOLDER_GRAVEYARD: GraveyardEntry[] = [
  { name: "Grid-v1", failure_reason: "过拟合", lesson: "回测周期过短", generation: 3, max_drawdown: -45 },
  { name: "Arb-beta", failure_reason: "滑点过大", lesson: "未考虑流动性", generation: 5, max_drawdown: -28 },
  { name: "News-v2", failure_reason: "延迟过高", lesson: "数据源不稳定", generation: 2, max_drawdown: -35 },
  { name: "Scalp-v4", failure_reason: "手续费吞噬", lesson: "频率过高", generation: 7, max_drawdown: -18 },
  { name: "DCA-naive", failure_reason: "过拟合", lesson: "参数空间过大", generation: 1, max_drawdown: -52 },
];

export default function StrategyLab() {
  const [starting, setStarting] = useState(false);

  const { data: rawStatus } = usePolling(() => api.evolveStatus(), 10_000);
  const { data: rawHall } = usePolling(() => api.evolveHallOfFame(), 30_000);
  const { data: rawGraveyard } = usePolling(() => api.evolveGraveyard(), 30_000);

  const status = (rawStatus as unknown as EvolveStatus) ?? PLACEHOLDER_STATUS;
  const hall = (rawHall as unknown as Strategy[])?.length ? (rawHall as unknown as Strategy[]) : PLACEHOLDER_HALL;
  const graveyard = (rawGraveyard as unknown as GraveyardEntry[])?.length
    ? (rawGraveyard as unknown as GraveyardEntry[])
    : PLACEHOLDER_GRAVEYARD;

  const handleStartEvolve = async () => {
    setStarting(true);
    try {
      await api.evolveStart(10);
    } catch {
      // API 可能未就绪
    } finally {
      setStarting(false);
    }
  };

  const failureDistribution = graveyard.reduce<Record<string, number>>((acc, g) => {
    acc[g.failure_reason] = (acc[g.failure_reason] || 0) + 1;
    return acc;
  }, {});
  const pieData = Object.entries(failureDistribution).map(([name, value]) => ({ name, value }));

  const backtestData = hall[0]?.equity_curve
    ? hall[0].equity_curve.map((_, i) => {
        const point: Record<string, number> = { idx: i };
        hall.forEach((s) => {
          if (s.equity_curve) point[s.name] = s.equity_curve[i] ?? 0;
        });
        return point;
      })
    : [];

  const lineColors = ["#3fb950", "#58a6ff", "#d29922", "#bc8cff", "#f85149"];

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <FlaskConical size={22} />
        策略实验室
      </h1>

      {/* 进化引擎状态 */}
      <div className="card mb-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-6">
            <div className="flex items-center gap-2">
              <Zap size={16} className="text-jarvis-yellow" />
              <span className="stat-label">状态</span>
              <span
                className={`ml-1 text-sm font-medium ${
                  status.running ? "text-jarvis-green" : "text-jarvis-text-secondary"
                }`}
              >
                {status.running ? "运行中" : "空闲"}
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
              <span className="stat-label">运行时间</span>
              <span className="ml-1 text-sm text-jarvis-text font-mono">
                {status.elapsed_seconds > 0 ? formatDuration(status.elapsed_seconds) : "—"}
              </span>
            </div>
          </div>
          <button
            onClick={handleStartEvolve}
            disabled={starting || status.running}
            className="btn-success flex items-center gap-2 disabled:opacity-50"
          >
            {starting ? <RotateCcw size={16} className="animate-spin" /> : <Play size={16} />}
            {starting ? "启动中..." : "启动进化"}
          </button>
        </div>
      </div>

      {/* 名人堂 */}
      <div className="mb-4">
        <h2 className="flex items-center gap-2 text-sm font-medium text-jarvis-text mb-3">
          <Trophy size={16} className="text-jarvis-yellow" />
          名人堂
        </h2>
        <div className="grid grid-cols-3 gap-4">
          {hall.map((s) => (
            <div key={s.name} className="card">
              <div className="flex items-center justify-between mb-3">
                <span className="text-sm font-medium text-jarvis-text">{s.name}</span>
                <span
                  className={`text-xs px-2 py-0.5 rounded-full ${
                    s.total_return >= 0
                      ? "bg-jarvis-green/20 text-jarvis-green"
                      : "bg-jarvis-red/20 text-jarvis-red"
                  }`}
                >
                  {s.total_return >= 0 ? "+" : ""}
                  {s.total_return.toFixed(1)}%
                </span>
              </div>
              <div className="grid grid-cols-2 gap-y-2 text-xs mb-3">
                <div>
                  <span className="text-jarvis-text-secondary">胜率</span>
                  <span className="ml-2 text-jarvis-text font-mono">
                    {(s.win_rate * 100).toFixed(0)}%
                  </span>
                </div>
                <div>
                  <span className="text-jarvis-text-secondary">盈亏比</span>
                  <span className="ml-2 text-jarvis-text font-mono">
                    {s.profit_factor.toFixed(1)}
                  </span>
                </div>
                <div>
                  <span className="text-jarvis-text-secondary">夏普</span>
                  <span className="ml-2 text-jarvis-text font-mono">{s.sharpe.toFixed(2)}</span>
                </div>
                <div>
                  <span className="text-jarvis-text-secondary">最大回撤</span>
                  <span className="ml-2 text-jarvis-red font-mono">
                    {s.max_drawdown.toFixed(1)}%
                  </span>
                </div>
              </div>
              {s.equity_curve && (
                <div className="h-16">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={s.equity_curve.map((v, i) => ({ i, v }))}>
                      <Line
                        type="monotone"
                        dataKey="v"
                        stroke="#3fb950"
                        dot={false}
                        strokeWidth={1.5}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* 策略墓地 */}
      <div className="mb-4">
        <h2 className="flex items-center gap-2 text-sm font-medium text-jarvis-text mb-3">
          <Skull size={16} className="text-jarvis-red" />
          策略墓地
        </h2>
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-jarvis-border text-jarvis-text-secondary text-left">
                <th className="pb-2 font-medium">策略名</th>
                <th className="pb-2 font-medium">世代</th>
                <th className="pb-2 font-medium">失败原因</th>
                <th className="pb-2 font-medium">教训</th>
                <th className="pb-2 font-medium text-right">最大回撤</th>
              </tr>
            </thead>
            <tbody>
              {graveyard.map((g) => (
                <tr key={g.name} className="border-b border-jarvis-border/50 last:border-0">
                  <td className="py-2 text-jarvis-text">{g.name}</td>
                  <td className="py-2 text-jarvis-text-secondary font-mono">G{g.generation}</td>
                  <td className="py-2">
                    <span className="text-xs px-2 py-0.5 rounded-full bg-jarvis-red/20 text-jarvis-red">
                      {g.failure_reason}
                    </span>
                  </td>
                  <td className="py-2 text-jarvis-text-secondary">{g.lesson}</td>
                  <td className="py-2 text-right text-jarvis-red font-mono">
                    {g.max_drawdown.toFixed(1)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* 图表区 */}
      <div className="grid grid-cols-2 gap-4">
        {/* 失败原因分布饼图 */}
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
                  label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}
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

        {/* 回测对比图 */}
        <div className="card">
          <h3 className="text-sm font-medium text-jarvis-text mb-3 flex items-center gap-2">
            <TrendingUp size={14} />
            回测收益对比
          </h3>
          <div className="h-52">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={backtestData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#30363d" />
                <XAxis dataKey="idx" tick={{ fill: "#8b949e", fontSize: 11 }} />
                <YAxis tick={{ fill: "#8b949e", fontSize: 11 }} />
                <Tooltip
                  contentStyle={{
                    background: "#161b22",
                    border: "1px solid #30363d",
                    borderRadius: 8,
                    color: "#e6edf3",
                  }}
                />
                <Legend wrapperStyle={{ fontSize: 11, color: "#8b949e" }} />
                {hall.map((s, i) => (
                  <Line
                    key={s.name}
                    type="monotone"
                    dataKey={s.name}
                    stroke={lineColors[i % lineColors.length]}
                    dot={false}
                    strokeWidth={1.5}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </div>
  );
}
