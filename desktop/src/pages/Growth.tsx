import { Sprout, Trophy, Skull, CheckCircle, XCircle, Clock, Tag } from "lucide-react";
import { clsx } from "clsx";
import {
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  Radar,
  PieChart,
  Pie,
  Cell,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { api, type BehaviorStatsResponse } from "@/api/client";
import { usePolling, useApi } from "@/hooks/useApi";

interface TimelineEvent {
  time: string;
  event: string;
  result: "success" | "fail";
  detail: string;
  metrics: { win_rate?: number; profit_factor?: number };
}

interface Milestone {
  title: string;
  achieved: boolean;
  detail: string;
}

interface GrowthStats {
  dimensions: string[];
  values: number[];
  total_strategies: number;
  success_count: number;
  failure_count: number;
  failure_reasons: Record<string, number>;
}

const COLORS = ["#f85149", "#58a6ff", "#3fb950", "#d29922", "#a371f7", "#79c0ff"];

export default function Growth() {
  const { data: timeline } = useApi<TimelineEvent[]>(
    () => api.growthTimeline() as unknown as Promise<TimelineEvent[]>,
  );
  const { data: milestones } = useApi<Milestone[]>(
    () => api.growthMilestones() as unknown as Promise<Milestone[]>,
  );
  const { data: stats } = usePolling<GrowthStats>(
    () => api.growthStats() as unknown as Promise<GrowthStats>,
    60_000,
  );
  // T1.4 平仓复盘行为标签分布（打标数据随交易积累，60s 轮询足够）
  const { data: behaviorStats } = usePolling<BehaviorStatsResponse>(
    api.behaviorStats,
    60_000,
  );

  const radarData =
    stats?.dimensions?.map((dim, i) => ({
      subject: dim,
      value: stats.values[i] ?? 0,
      fullMark: 100,
    })) ?? [];

  const pieData = stats?.failure_reasons
    ? Object.entries(stats.failure_reasons).map(([name, value]) => ({ name, value }))
    : [];

  const growthCurve =
    timeline
      ?.slice()
      .reverse()
      .reduce<{ name: string; winRate: number }[]>((acc, ev, i) => {
        const prevRate = acc.length > 0 ? acc[acc.length - 1].winRate : 50;
        const newRate =
          ev.result === "success"
            ? Math.min(100, prevRate + 3)
            : Math.max(0, prevRate - 1);
        acc.push({ name: `#${i + 1}`, winRate: newRate });
        return acc;
      }, []) ?? [];

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <Sprout size={22} />
        贾维斯成长进度
      </h1>

      {/* 统计概览卡片 */}
      <div className="grid grid-cols-4 gap-4 mb-4">
        <div className="card text-center">
          <p className="stat-label">总策略数</p>
          <p className="stat-value">{stats?.total_strategies ?? 0}</p>
        </div>
        <div className="card text-center">
          <p className="stat-label">达标策略</p>
          <p className="stat-value text-jarvis-green">{stats?.success_count ?? 0}</p>
        </div>
        <div className="card text-center">
          <p className="stat-label">失败策略</p>
          <p className="stat-value text-jarvis-red">{stats?.failure_count ?? 0}</p>
        </div>
        <div className="card text-center">
          <p className="stat-label">成功率</p>
          <p className="stat-value">
            {stats && stats.total_strategies > 0
              ? `${Math.round((stats.success_count / stats.total_strategies) * 100)}%`
              : "—"}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4">
        {/* 能力雷达图 */}
        <div className="card">
          <div className="flex items-center gap-2 mb-3">
            <Trophy size={16} className="text-jarvis-blue" />
            <p className="stat-label font-medium">能力雷达图</p>
          </div>
          {radarData.length > 0 ? (
            <ResponsiveContainer width="100%" height={240}>
              <RadarChart data={radarData}>
                <PolarGrid stroke="#30363d" />
                <PolarAngleAxis dataKey="subject" tick={{ fill: "#8b949e", fontSize: 12 }} />
                <PolarRadiusAxis angle={90} domain={[0, 100]} tick={{ fill: "#8b949e", fontSize: 10 }} />
                <Radar
                  name="能力值"
                  dataKey="value"
                  stroke="#58a6ff"
                  fill="#58a6ff"
                  fillOpacity={0.3}
                />
              </RadarChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-sm text-jarvis-text-secondary text-center py-12">
              暂无数据，运行进化引擎后生成
            </p>
          )}
        </div>

        {/* 失败原因饼图 */}
        <div className="card">
          <div className="flex items-center gap-2 mb-3">
            <Skull size={16} className="text-jarvis-red" />
            <p className="stat-label font-medium">策略试错统计</p>
          </div>
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height={240}>
              <PieChart>
                <Pie
                  data={pieData}
                  cx="50%"
                  cy="50%"
                  innerRadius={50}
                  outerRadius={80}
                  dataKey="value"
                  label={({ name, percent }) =>
                    `${name} ${(percent * 100).toFixed(0)}%`
                  }
                  labelLine={{ stroke: "#30363d" }}
                >
                  {pieData.map((_, i) => (
                    <Cell key={i} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                  labelStyle={{ color: "#e6edf3" }}
                />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-sm text-jarvis-text-secondary text-center py-12">
              暂无失败策略数据
            </p>
          )}
        </div>
      </div>

      {/* T1.4 行为标签分布：按复盘标签统计胜率/盈亏，照出自己的交易行为模式 */}
      <div className="card mb-4">
        <div className="flex items-center gap-2 mb-3">
          <Tag size={16} className="text-jarvis-yellow" />
          <p className="stat-label font-medium">行为标签分布（平仓复盘打标）</p>
          {behaviorStats?.total_closed != null && behaviorStats.total_closed > 0 && (
            <span className="ml-auto text-xs text-jarvis-text-secondary">
              已平仓 {behaviorStats.total_closed} 笔
            </span>
          )}
        </div>
        {behaviorStats?.buckets && behaviorStats.buckets.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-jarvis-border text-jarvis-text-secondary text-left">
                  <th className="pb-2 pr-3 font-medium">标签</th>
                  <th className="pb-2 pr-3 font-medium text-right">笔数</th>
                  <th className="pb-2 pr-3 font-medium text-right">胜率</th>
                  <th className="pb-2 pr-3 font-medium text-right">累计盈亏</th>
                  <th className="pb-2 font-medium">占比</th>
                </tr>
              </thead>
              <tbody>
                {behaviorStats.buckets.map((b) => {
                  const total = behaviorStats.total_closed ?? 0;
                  const sharePct = total > 0 ? (b.trades / total) * 100 : 0;
                  const untagged = b.tag === "未打标";
                  const bad = ["恐慌", "追高", "贪婪"].some((w) => b.tag.includes(w));
                  return (
                    <tr
                      key={b.tag}
                      className="border-b border-jarvis-border/40 last:border-0"
                    >
                      <td className="py-2 pr-3">
                        <span
                          className={clsx(
                            "inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-full whitespace-nowrap",
                            untagged
                              ? "bg-jarvis-border/40 text-jarvis-text-secondary"
                              : b.tag.includes("按计划")
                                ? "bg-jarvis-green/15 text-jarvis-green"
                                : bad
                                  ? "bg-jarvis-red/15 text-jarvis-red"
                                  : "bg-jarvis-yellow/15 text-jarvis-yellow",
                          )}
                        >
                          <Tag size={10} />
                          {b.tag}
                        </span>
                      </td>
                      <td className="py-2 pr-3 text-right font-mono text-jarvis-text">
                        {b.trades}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono text-jarvis-text">
                        {b.win_rate_pct != null ? `${b.win_rate_pct.toFixed(1)}%` : "—"}
                      </td>
                      <td
                        className={clsx(
                          "py-2 pr-3 text-right font-mono",
                          b.pnl_usdt >= 0 ? "text-jarvis-green" : "text-jarvis-red",
                        )}
                      >
                        {b.pnl_usdt >= 0 ? "+" : ""}
                        {b.pnl_usdt.toFixed(2)}U
                      </td>
                      <td className="py-2">
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 w-24 bg-jarvis-bg rounded-full overflow-hidden">
                            <div
                              className={clsx(
                                "h-full rounded-full",
                                untagged
                                  ? "bg-jarvis-border"
                                  : bad
                                    ? "bg-jarvis-red"
                                    : "bg-jarvis-blue",
                              )}
                              style={{ width: `${sharePct}%` }}
                            />
                          </div>
                          <span className="text-jarvis-text-secondary font-mono">
                            {sharePct.toFixed(0)}%
                          </span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="mt-2 text-[11px] text-jarvis-text-secondary">
              「恐慌割肉 / 追高被套 / 贪婪不止盈」占比高说明情绪在替你交易；标签在平仓时弹窗打标，历史单可去交易记录页补标
            </p>
          </div>
        ) : (
          <p className="text-sm text-jarvis-text-secondary text-center py-8">
            暂无打标数据 · 平仓时会弹出复盘标签选择，历史单可在交易记录页补标
          </p>
        )}
      </div>

      {/* 成长曲线 */}
      <div className="card mb-4">
        <div className="flex items-center gap-2 mb-3">
          <Sprout size={16} className="text-jarvis-green" />
          <p className="stat-label font-medium">成长曲线（胜率随进化轮次变化）</p>
        </div>
        {growthCurve.length > 2 ? (
          <ResponsiveContainer width="100%" height={160}>
            <LineChart data={growthCurve} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#30363d" />
              <XAxis dataKey="name" tick={{ fill: "#8b949e", fontSize: 11 }} />
              <YAxis domain={[0, 100]} tick={{ fill: "#8b949e", fontSize: 11 }} />
              <Tooltip
                contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                labelStyle={{ color: "#e6edf3" }}
              />
              <Line type="monotone" dataKey="winRate" stroke="#3fb950" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <p className="text-sm text-jarvis-text-secondary text-center py-8">
            需要更多进化轮次数据才能绘制成长曲线
          </p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-4">
        {/* 里程碑卡片 */}
        <div className="card">
          <div className="flex items-center gap-2 mb-3">
            <CheckCircle size={16} className="text-jarvis-green" />
            <p className="stat-label font-medium">里程碑</p>
          </div>
          <div className="space-y-3">
            {milestones && milestones.length > 0 ? (
              milestones.map((m, i) => (
                <div key={i} className="flex items-start gap-3 pb-3 border-b border-jarvis-border/50 last:border-0">
                  {m.achieved ? (
                    <CheckCircle size={16} className="text-jarvis-green mt-0.5 shrink-0" />
                  ) : (
                    <Clock size={16} className="text-jarvis-text-secondary mt-0.5 shrink-0" />
                  )}
                  <div>
                    <p className={`text-sm font-medium ${m.achieved ? "text-jarvis-text" : "text-jarvis-text-secondary"}`}>
                      {m.title}
                    </p>
                    <p className="text-xs text-jarvis-text-secondary">{m.detail}</p>
                  </div>
                </div>
              ))
            ) : (
              <p className="text-sm text-jarvis-text-secondary text-center py-4">
                运行进化引擎解锁里程碑
              </p>
            )}
          </div>
        </div>

        {/* 进化时间线 */}
        <div className="card">
          <div className="flex items-center gap-2 mb-3">
            <Clock size={16} className="text-jarvis-blue" />
            <p className="stat-label font-medium">进化时间线</p>
          </div>
          <div className="max-h-72 overflow-y-auto space-y-0">
            {timeline && timeline.length > 0 ? (
              timeline.slice(0, 20).map((ev, i) => (
                <div key={i} className="flex gap-3 py-2 border-b border-jarvis-border/50 last:border-0">
                  <div className="flex flex-col items-center">
                    {ev.result === "success" ? (
                      <CheckCircle size={14} className="text-jarvis-green shrink-0" />
                    ) : (
                      <XCircle size={14} className="text-jarvis-red shrink-0" />
                    )}
                    {i < (timeline?.length ?? 0) - 1 && (
                      <div className="w-px flex-1 bg-jarvis-border mt-1" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-jarvis-text-secondary">{ev.time}</p>
                    <p className="text-sm text-jarvis-text truncate">{ev.event}</p>
                    <p className="text-xs text-jarvis-text-secondary truncate">{ev.detail}</p>
                  </div>
                </div>
              ))
            ) : (
              <p className="text-sm text-jarvis-text-secondary text-center py-4">
                暂无进化记录
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
