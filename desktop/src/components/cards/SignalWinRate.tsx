import { useEffect, useRef, useState } from "react";
import { clsx } from "clsx";
import { Target, AlertTriangle, History, Loader2 } from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api } from "@/api/client";

interface SystemStat {
  system: string;
  name_cn: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number | null;
  total_pnl_usdt: number;
  avg_pnl_usdt: number | null;
  avg_win_usdt: number | null;
  avg_loss_usdt: number | null;
  expectancy_usdt: number | null;
  low_sample: boolean;
}

interface SignalStatsResponse {
  ok: boolean;
  error?: string;
  overall: {
    closed_trades: number;
    wins: number;
    losses: number;
    win_rate_pct: number | null;
    total_pnl_usdt: number;
    avg_pnl_usdt: number | null;
    expectancy_usdt: number | null;
    low_sample: boolean;
  };
  systems: SystemStat[];
  open_twelve: number;
}

interface ReplayStatus {
  ok: boolean;
  running: boolean;
  progress: number;
  detail: string;
  error: string | null;
  result: { total_trades?: number } | null;
}

/** 样本来源（单选，始终有一个生效）：realtime=实时模拟单，replay=历史回放 */
const SOURCE_OPTIONS: { value: string; label: string }[] = [
  { value: "realtime", label: "仅实时" },
  { value: "all", label: "含回放" },
  { value: "replay", label: "仅回放" },
];

/** 筛选维度定义（value 与后端 query 参数一致，"" = 全部不过滤） */
const FILTER_GROUPS: {
  key: "direction" | "tf" | "resonance" | "regime";
  label: string;
  options: { value: string; label: string }[];
}[] = [
  {
    key: "direction",
    label: "方向",
    options: [
      { value: "long", label: "多" },
      { value: "short", label: "空" },
    ],
  },
  {
    key: "tf",
    label: "周期",
    options: [
      { value: "5m", label: "5m" },
      { value: "15m", label: "15m" },
      { value: "30m", label: "30m" },
      { value: "1h", label: "1h" },
      { value: "4h", label: "4h" },
    ],
  },
  {
    key: "resonance",
    label: "共振",
    options: [
      { value: "1", label: "单系统" },
      { value: "2-3", label: "2-3" },
      { value: "4+", label: "4+" },
    ],
  },
  {
    key: "regime",
    label: "状态",
    options: [
      { value: "trending", label: "趋势" },
      { value: "ranging", label: "震荡" },
      { value: "breakout", label: "突破" },
      { value: "unknown", label: "未知" },
    ],
  },
];

function rateColor(rate: number): string {
  if (rate >= 60) return "text-jarvis-green";
  if (rate >= 45) return "text-jarvis-yellow";
  return "text-jarvis-red";
}

function barColor(rate: number): string {
  if (rate >= 60) return "bg-jarvis-green";
  if (rate >= 45) return "bg-jarvis-yellow";
  return "bg-jarvis-red";
}

function pnlColor(v: number): string {
  return v >= 0 ? "text-jarvis-green" : "text-jarvis-red";
}

function fmtSigned(v: number | null | undefined, suffix = "U"): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}${suffix}`;
}

/** 样本不足徽标（分组样本 <30 时统计置信度低） */
function LowSampleBadge() {
  return (
    <span
      className="inline-flex items-center gap-0.5 text-[10px] px-1 py-px rounded bg-jarvis-yellow/15 text-jarvis-yellow whitespace-nowrap"
      title="该分组样本量 <30，统计结果仅供参考"
    >
      <AlertTriangle size={9} />
      样本不足
    </span>
  );
}

/**
 * 12 系统信号胜率统计：每笔共识模拟单平仓后按来源信号系统归因，
 * 支持 方向×周期×共振档×市场状态 多维筛选，按期望值排序对比哪套系统更能赚钱。
 */
export default function SignalWinRate() {
  const [filters, setFilters] = useState<Record<string, string>>({
    direction: "",
    tf: "",
    resonance: "",
    regime: "",
  });
  const [source, setSource] = useState("realtime");
  const [replayBusy, setReplayBusy] = useState(false);
  const [replayMsg, setReplayMsg] = useState("");
  const pollTimer = useRef<number | null>(null);

  const query = [
    `source=${source}`,
    ...Object.entries(filters)
      .filter(([, v]) => v)
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`),
  ].join("&");

  const { data, loading, error, refetch } = usePolling(
    () => api.get<SignalStatsResponse>(`/twelve/signal-stats?${query}`),
    30_000,
    [query],
  );

  // 组件卸载时停掉回放进度轮询
  useEffect(
    () => () => {
      if (pollTimer.current != null) window.clearInterval(pollTimer.current);
    },
    [],
  );

  const startReplay = async () => {
    setReplayBusy(true);
    setReplayMsg("正在启动回放…");
    try {
      // 空 body：后端默认 watchlist × (15m/1h/4h) × 30 天
      const res = await api.post<{ ok: boolean; error?: string }>(
        "/twelve/replay",
        {},
      );
      if (!res.ok) {
        setReplayMsg(res.error ?? "启动失败");
        setReplayBusy(false);
        return;
      }
      pollTimer.current = window.setInterval(async () => {
        try {
          const st = await api.get<ReplayStatus>("/twelve/replay/status");
          if (st.running) {
            setReplayMsg(`回放中 ${st.progress}% · ${st.detail}`);
            return;
          }
          if (pollTimer.current != null) {
            window.clearInterval(pollTimer.current);
            pollTimer.current = null;
          }
          setReplayBusy(false);
          if (st.error) {
            setReplayMsg(`回放失败：${st.error}`);
          } else {
            setReplayMsg(
              `回放完成，新增 ${st.result?.total_trades ?? 0} 笔历史样本`,
            );
            setSource((s) => (s === "realtime" ? "replay" : s));
            refetch();
          }
          window.setTimeout(() => setReplayMsg(""), 10_000);
        } catch {
          // 单次进度查询失败忽略，下一轮再试
        }
      }, 2_000);
    } catch (e) {
      setReplayMsg(e instanceof Error ? e.message : "启动失败");
      setReplayBusy(false);
    }
  };

  const failed = Boolean(error) || (data != null && !data.ok);
  const overall = data?.ok ? data.overall : null;
  // 后端已按期望值降序返回；保险起见前端再稳定排序一次
  const systems = (data?.ok ? data.systems : [])
    .slice()
    .sort(
      (a, b) =>
        (b.expectancy_usdt ?? Number.NEGATIVE_INFINITY) -
        (a.expectancy_usdt ?? Number.NEGATIVE_INFINITY),
    );
  const hasActiveFilter = Object.values(filters).some(Boolean);

  const toggle = (key: string, value: string) => {
    setFilters((f) => ({ ...f, [key]: f[key] === value ? "" : value }));
  };

  return (
    <div className="card">
      <div className="flex items-center justify-between gap-2 mb-3 flex-wrap">
        <p className="stat-label flex items-center gap-2 mb-0">
          <Target size={14} />
          12 系统信号胜率统计
        </p>
        <div className="flex items-center gap-3">
          {overall && (
            <span className="text-xs text-jarvis-text-secondary">
              {hasActiveFilter ? "筛选后 " : ""}已平仓{" "}
              <span className="text-jarvis-text font-mono">
                {overall.closed_trades}
              </span>{" "}
              笔 · 在途{" "}
              <span className="text-jarvis-text font-mono">
                {data?.open_twelve ?? 0}
              </span>{" "}
              笔 · 累计{" "}
              <span className={clsx("font-mono", pnlColor(overall.total_pnl_usdt))}>
                {fmtSigned(overall.total_pnl_usdt)}
              </span>
              {overall.expectancy_usdt != null && (
                <>
                  {" · 期望 "}
                  <span
                    className={clsx("font-mono", pnlColor(overall.expectancy_usdt))}
                  >
                    {fmtSigned(overall.expectancy_usdt)}/笔
                  </span>
                </>
              )}
            </span>
          )}
          <button
            onClick={startReplay}
            disabled={replayBusy}
            title="用历史 K 线回放 12 系统信号（watchlist × 15m/1h/4h × 30 天），几分钟内预积累胜率样本；样本与实时模拟单严格隔离"
            className="flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md border border-jarvis-purple/40 text-jarvis-purple hover:bg-jarvis-purple/10 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {replayBusy ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <History size={12} />
            )}
            {replayBusy ? "回放中…" : "一键回放预积累"}
          </button>
        </div>
      </div>

      {replayMsg && (
        <p className="text-xs text-jarvis-text-secondary mb-2 bg-jarvis-bg rounded-md px-2.5 py-1.5">
          {replayMsg}
        </p>
      )}

      {/* 筛选 chips：来源（单选） + 方向 / 周期 / 共振档 / 市场状态（点选即筛，再点取消） */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 mb-3">
        <div className="flex items-center gap-1">
          <span className="text-[10px] text-jarvis-text-secondary mr-0.5">
            来源
          </span>
          {SOURCE_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setSource(opt.value)}
              className={clsx(
                "text-[11px] px-2 py-0.5 rounded-full border transition-colors",
                source === opt.value
                  ? "bg-jarvis-purple/20 border-jarvis-purple text-jarvis-purple"
                  : "bg-jarvis-bg border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-text-secondary",
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
        {FILTER_GROUPS.map((g) => (
          <div key={g.key} className="flex items-center gap-1">
            <span className="text-[10px] text-jarvis-text-secondary mr-0.5">
              {g.label}
            </span>
            {g.options.map((opt) => {
              const active = filters[g.key] === opt.value;
              return (
                <button
                  key={opt.value}
                  onClick={() => toggle(g.key, opt.value)}
                  className={clsx(
                    "text-[11px] px-2 py-0.5 rounded-full border transition-colors",
                    active
                      ? "bg-jarvis-blue/20 border-jarvis-blue text-jarvis-blue"
                      : "bg-jarvis-bg border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text hover:border-jarvis-text-secondary",
                  )}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
        ))}
        {hasActiveFilter && (
          <button
            onClick={() =>
              setFilters({ direction: "", tf: "", resonance: "", regime: "" })
            }
            className="text-[11px] px-2 py-0.5 rounded-full border border-jarvis-red/40 text-jarvis-red hover:bg-jarvis-red/10 transition-colors"
          >
            清除筛选
          </button>
        )}
      </div>

      {failed ? (
        <p className="text-sm text-jarvis-text-secondary py-6 text-center">
          统计接口未就绪
          {data && !data.ok && data.error ? `：${String(data.error)}` : ""}
        </p>
      ) : loading && !data ? (
        <div className="space-y-2 animate-pulse">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-8 rounded-lg bg-jarvis-border/30" />
          ))}
        </div>
      ) : systems.length === 0 ? (
        <p className="text-sm text-jarvis-text-secondary py-6 text-center">
          {hasActiveFilter
            ? "当前筛选组合下暂无已平仓样本，换个筛选条件试试"
            : source === "replay"
              ? "暂无回放样本 · 点右上角「一键回放预积累」用历史 K 线几分钟灌入样本"
              : "暂无归因数据 · 12 系统共识产生方向决策后会自动模拟下单，平仓后此处按信号系统统计胜率；等不及可点右上角「一键回放预积累」"}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-jarvis-text-secondary border-b border-jarvis-border text-left">
                <th className="py-2 font-medium">信号系统</th>
                <th className="py-2 font-medium text-right">笔数</th>
                <th className="py-2 font-medium text-right">胜 / 负</th>
                <th className="py-2 font-medium w-[24%]">胜率</th>
                <th className="py-2 font-medium text-right">期望/笔 ↓</th>
                <th className="py-2 font-medium text-right">累计盈亏</th>
                <th className="py-2 font-medium text-right">均盈 / 均亏</th>
              </tr>
            </thead>
            <tbody>
              {systems.map((s) => {
                const rate = s.win_rate_pct ?? 0;
                return (
                  <tr
                    key={s.system}
                    className="border-b border-jarvis-border/50 last:border-0"
                  >
                    <td className="py-2 text-jarvis-text">
                      <span className="inline-flex items-center gap-1.5 flex-wrap">
                        {s.name_cn}
                        <span className="text-[10px] text-jarvis-text-secondary font-mono">
                          {s.system}
                        </span>
                        {s.low_sample && <LowSampleBadge />}
                      </span>
                    </td>
                    <td className="py-2 text-right font-mono text-jarvis-text">
                      {s.trades}
                    </td>
                    <td className="py-2 text-right font-mono text-jarvis-text-secondary">
                      <span className="text-jarvis-green">{s.wins}</span>
                      {" / "}
                      <span className="text-jarvis-red">{s.losses}</span>
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex items-center gap-2">
                        <div className="flex-1 h-1.5 bg-jarvis-bg rounded-full overflow-hidden">
                          <div
                            className={clsx("h-full rounded-full", barColor(rate))}
                            style={{ width: `${Math.min(100, rate)}%` }}
                          />
                        </div>
                        <span
                          className={clsx(
                            "text-xs font-mono w-12 text-right",
                            rateColor(rate),
                          )}
                        >
                          {s.win_rate_pct != null ? `${s.win_rate_pct}%` : "—"}
                        </span>
                      </div>
                    </td>
                    <td
                      className={clsx(
                        "py-2 text-right font-mono font-medium",
                        pnlColor(s.expectancy_usdt ?? 0),
                      )}
                    >
                      {fmtSigned(s.expectancy_usdt)}
                    </td>
                    <td
                      className={clsx(
                        "py-2 text-right font-mono",
                        pnlColor(s.total_pnl_usdt),
                      )}
                    >
                      {fmtSigned(s.total_pnl_usdt)}
                    </td>
                    <td className="py-2 text-right font-mono text-jarvis-text-secondary text-xs">
                      {fmtSigned(s.avg_win_usdt)} / {fmtSigned(s.avg_loss_usdt)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="text-[10px] text-jarvis-text-secondary mt-2">
            归因口径：每笔共识单的依据系统各计一笔（多系统共振时同笔多计）；
            期望值 = 胜率×均盈 − 败率×|均亏|（U/笔），默认按期望值降序；
            样本 &lt;30 的分组带「样本不足」徽标，结果仅供参考。
          </p>
        </div>
      )}
    </div>
  );
}
