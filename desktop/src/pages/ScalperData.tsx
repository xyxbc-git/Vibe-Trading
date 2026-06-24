import { Zap, Activity, TrendingUp, Clock, Target } from "lucide-react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/useApi";

interface ScalperStatus {
  running: boolean;
  strategy: string | null;
  symbol: string;
  timeframe: string;
  report: {
    total_trades: number;
    wins: number;
    losses: number;
    win_rate_pct: number;
    profit_factor: number;
    total_pnl: number;
    daily_pnl: number;
    avg_bars_held: number;
    open_positions: number;
    consecutive_losses: number;
  };
  config: {
    confidence_threshold: number;
    max_positions: number;
    aggressive_mode: boolean;
  };
}

interface ScalperLog {
  lines: string[];
  total: number;
}

function SignalIndicator({ label, value }: { label: string; value: string }) {
  const color =
    value === "开多信号"
      ? "text-jarvis-green"
      : value === "开空信号"
        ? "text-jarvis-red"
        : "text-jarvis-text-secondary";
  return (
    <div className="flex items-center justify-between py-2 border-b border-jarvis-border last:border-0">
      <span className="text-sm text-jarvis-text-secondary">{label}</span>
      <span className={`text-sm font-mono font-medium ${color}`}>{value}</span>
    </div>
  );
}

export default function ScalperData() {
  const { data: status } = usePolling<ScalperStatus>(
    () => api.scalperStatus() as unknown as Promise<ScalperStatus>,
    15_000,
  );
  const { data: log } = usePolling<ScalperLog>(
    () => api.scalperLog(30) as unknown as Promise<ScalperLog>,
    30_000,
  );

  const report = status?.report;

  const winRateData = report
    ? [
        { name: "胜", value: report.wins },
        { name: "负", value: report.losses },
      ]
    : [];

  const winRate = report?.win_rate_pct ?? 0;
  const signalState = status?.running ? "运行中" : "待机中";
  const signalColor = status?.running
    ? "bg-jarvis-green"
    : "bg-jarvis-text-secondary";

  return (
    <div>
      <h1 className="page-title flex items-center gap-2">
        <Zap size={22} />
        短线数据
      </h1>

      {/* 信号状态指示灯 + 策略名称 */}
      <div className="card mb-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className={`w-3 h-3 rounded-full ${signalColor} animate-pulse`} />
            <div>
              <p className="text-sm font-medium text-jarvis-text">{signalState}</p>
              <p className="text-xs text-jarvis-text-secondary">
                策略：{status?.strategy ?? "未加载"} · {status?.symbol ?? "BTCUSDT"} · {status?.timeframe ?? "15m"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-jarvis-text-secondary">
              置信门槛: {status?.config?.confidence_threshold ?? "—"}
            </span>
            {status?.config?.aggressive_mode && (
              <span className="px-2 py-0.5 text-xs rounded bg-jarvis-red/20 text-jarvis-red">
                激进模式
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4">
        {/* 实时信号面板 */}
        <div className="card">
          <div className="flex items-center gap-2 mb-3">
            <Activity size={16} className="text-jarvis-blue" />
            <p className="stat-label font-medium">实时因子读数</p>
          </div>
          <SignalIndicator label="信号状态" value={signalState} />
          <SignalIndicator label="持仓数" value={`${report?.open_positions ?? 0} / ${status?.config?.max_positions ?? 3}`} />
          <SignalIndicator label="连续亏损" value={`${report?.consecutive_losses ?? 0} 笔`} />
          <SignalIndicator label="平均持仓" value={`${report?.avg_bars_held ?? 0} bar`} />
        </div>

        {/* 15m 战绩看板 */}
        <div className="card">
          <div className="flex items-center gap-2 mb-3">
            <TrendingUp size={16} className="text-jarvis-green" />
            <p className="stat-label font-medium">15m 战绩看板</p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <p className="text-xs text-jarvis-text-secondary">总交易</p>
              <p className="stat-value text-lg">{report?.total_trades ?? 0}</p>
            </div>
            <div>
              <p className="text-xs text-jarvis-text-secondary">胜率</p>
              <p className={`stat-value text-lg ${winRate >= 52 ? "text-jarvis-green" : winRate > 0 ? "text-jarvis-red" : ""}`}>
                {winRate}%
              </p>
            </div>
            <div>
              <p className="text-xs text-jarvis-text-secondary">盈亏比</p>
              <p className="stat-value text-lg">{report?.profit_factor ?? "—"}</p>
            </div>
            <div>
              <p className="text-xs text-jarvis-text-secondary">总盈亏</p>
              <p className={`stat-value text-lg ${(report?.total_pnl ?? 0) >= 0 ? "text-jarvis-green" : "text-jarvis-red"}`}>
                {(report?.total_pnl ?? 0) >= 0 ? "+" : ""}
                {report?.total_pnl ?? 0} U
              </p>
            </div>
            <div>
              <p className="text-xs text-jarvis-text-secondary">今日盈亏</p>
              <p className={`stat-value text-lg ${(report?.daily_pnl ?? 0) >= 0 ? "text-jarvis-green" : "text-jarvis-red"}`}>
                {(report?.daily_pnl ?? 0) >= 0 ? "+" : ""}
                {report?.daily_pnl ?? 0} U
              </p>
            </div>
            <div>
              <p className="text-xs text-jarvis-text-secondary">胜/负</p>
              <p className="stat-value text-lg">
                <span className="text-jarvis-green">{report?.wins ?? 0}</span>
                {" / "}
                <span className="text-jarvis-red">{report?.losses ?? 0}</span>
              </p>
            </div>
          </div>
        </div>
      </div>

      {/* 胜率趋势图 */}
      <div className="card mb-4">
        <div className="flex items-center gap-2 mb-3">
          <Target size={16} className="text-jarvis-blue" />
          <p className="stat-label font-medium">胜率概览</p>
        </div>
        {winRateData.length > 0 && report && report.total_trades > 0 ? (
          <div className="flex items-center gap-6">
            <div className="flex-1">
              <ResponsiveContainer width="100%" height={120}>
                <LineChart
                  data={[
                    { name: "起始", rate: 50 },
                    { name: "当前", rate: winRate },
                  ]}
                  margin={{ top: 5, right: 20, bottom: 5, left: 0 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#30363d" />
                  <XAxis dataKey="name" tick={{ fill: "#8b949e", fontSize: 12 }} />
                  <YAxis domain={[0, 100]} tick={{ fill: "#8b949e", fontSize: 12 }} />
                  <Tooltip
                    contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                    labelStyle={{ color: "#e6edf3" }}
                  />
                  <Line type="monotone" dataKey="rate" stroke="#3fb950" strokeWidth={2} dot={{ fill: "#3fb950" }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="text-center">
              <p className="text-3xl font-bold font-mono text-jarvis-text">{winRate}%</p>
              <p className="text-xs text-jarvis-text-secondary mt-1">当前胜率</p>
            </div>
          </div>
        ) : (
          <p className="text-sm text-jarvis-text-secondary text-center py-6">
            暂无足够交易数据生成趋势图
          </p>
        )}
      </div>

      {/* 交易日志 */}
      <div className="card">
        <div className="flex items-center gap-2 mb-3">
          <Clock size={16} className="text-jarvis-text-secondary" />
          <p className="stat-label font-medium">交易日志</p>
          <span className="text-xs text-jarvis-text-secondary ml-auto">
            共 {log?.total ?? 0} 条
          </span>
        </div>
        <div className="max-h-64 overflow-y-auto space-y-1">
          {log?.lines && log.lines.length > 0 ? (
            log.lines
              .slice()
              .reverse()
              .map((line, i) => (
                <p key={i} className="text-xs font-mono text-jarvis-text-secondary leading-5 border-b border-jarvis-border/50 pb-1">
                  {line}
                </p>
              ))
          ) : (
            <p className="text-sm text-jarvis-text-secondary text-center py-4">
              暂无交易日志，启动短线交易后会在此显示
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
