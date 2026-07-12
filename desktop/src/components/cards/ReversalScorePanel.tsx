import { useMemo } from "react";
import { clsx } from "clsx";
import {
  Crosshair,
  TrendingUp,
  TrendingDown,
  CheckCircle2,
  CircleDashed,
  Circle,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import {
  api,
  type ReversalCondition,
  type ReversalScoreResponse,
} from "@/api/client";

// 四条件展示顺序与中文名（与后端 /api/reversal-score conditions 键对齐）
const CONDITIONS: { key: keyof ReversalScoreResponse["conditions"]; name: string; hint: string }[] = [
  {
    key: "delta_divergence",
    name: "Delta 背离",
    hint: "价格创新低而 Delta/CVD 不创新低——卖压正被主力吸收（做空镜像）",
  },
  {
    key: "multi_distribution",
    name: "过程多分布",
    hint: "行情由多个成交量正态分布台阶构成，非单边直拉——结构健康可回补",
  },
  {
    key: "triple_confirm",
    name: "三连分布确认",
    hint: "≥3 个相邻小正态分布——耐心等待回补到分布价值区再动手",
  },
  {
    key: "stop_hunt",
    name: "末端止损扫单",
    hint: "长影线刺破前低/前高快速收回 + 放量——同向止损被扫，与主力同行",
  },
];

const VERDICT_META = {
  "high-probability": {
    label: "高概率入场点",
    cls: "bg-jarvis-yellow/20 text-jarvis-yellow border-jarvis-yellow/60",
    ring: "#d29922",
  },
  watch: {
    label: "观察",
    cls: "bg-jarvis-blue/10 text-jarvis-blue border-jarvis-blue/40",
    ring: "#58a6ff",
  },
  "no-signal": {
    label: "无信号",
    cls: "bg-jarvis-border/30 text-jarvis-text-secondary border-jarvis-border",
    ring: "#30363d",
  },
} as const;

/** 条件行三态图标：✅ met / ⏳ 未满足（数据源正常）/ ⚪ 数据源未就绪 */
function ConditionIcon({ cond }: { cond: ReversalCondition }) {
  if (cond.unavailable) {
    return <Circle size={14} className="text-jarvis-text-secondary/40 shrink-0 mt-0.5" />;
  }
  if (cond.met) {
    return <CheckCircle2 size={14} className="text-jarvis-green shrink-0 mt-0.5" />;
  }
  return <CircleDashed size={14} className="text-jarvis-yellow/70 shrink-0 mt-0.5" />;
}

/** X/4 进度环（SVG，随 verdict 变色；4/4 金色高亮） */
function ScoreRing({
  satisfied,
  max,
  color,
}: {
  satisfied: number;
  max: number;
  color: string;
}) {
  const R = 26;
  const C = 2 * Math.PI * R;
  const frac = max > 0 ? satisfied / max : 0;
  return (
    <svg width="72" height="72" viewBox="0 0 72 72" className="shrink-0">
      <circle cx="36" cy="36" r={R} fill="none" stroke="#21262d" strokeWidth="6" />
      <circle
        cx="36"
        cy="36"
        r={R}
        fill="none"
        stroke={color}
        strokeWidth="6"
        strokeLinecap="round"
        strokeDasharray={`${C * frac} ${C * (1 - frac)}`}
        transform="rotate(-90 36 36)"
        className="transition-all duration-500"
      />
      <text
        x="36"
        y="34"
        textAnchor="middle"
        dominantBaseline="central"
        fill="#c9d1d9"
        fontSize="16"
        fontWeight="600"
        fontFamily="ui-monospace, monospace"
      >
        {satisfied}/{max}
      </text>
      <text
        x="36"
        y="50"
        textAnchor="middle"
        fill="#8b949e"
        fontSize="8"
      >
        条件
      </text>
    </svg>
  );
}

interface ReversalScorePanelProps {
  symbol: string;
  /** 信号周期（与 K 线页当前周期联动） */
  timeframe: string;
}

/**
 * 高胜率反转四条件叠加面板：Delta 背离 + 过程多分布 + 三连确认 + 止损扫单。
 * 条件越多胜率越高机会越少，4/4 = 高概率入场点（金色高亮）。
 * 上游数据源未就绪的条件显示 ⚪ 并降级，不阻塞其余条件。
 */
export default function ReversalScorePanel({ symbol, timeframe }: ReversalScorePanelProps) {
  const { data, loading, error } = usePolling(
    () => api.reversalScore(symbol, timeframe),
    60_000,
    [symbol, timeframe],
  );

  // 防旧数据回写：慢响应的旧币种/旧周期评分不得展示到当前口径
  const resp = useMemo(() => {
    if (!data) return null;
    if (data.symbol != null && data.symbol !== symbol) return null;
    if (data.timeframe != null && data.timeframe !== timeframe) return null;
    return data;
  }, [data, symbol, timeframe]);

  const failed = Boolean(error) || (resp != null && !resp.ok);
  const verdict = resp?.ok ? VERDICT_META[resp.verdict] ?? VERDICT_META["no-signal"] : null;
  const isHigh = resp?.ok && resp.verdict === "high-probability";

  return (
    <div
      className={clsx(
        "card transition-colors",
        isHigh && "border-jarvis-yellow/60 shadow-[0_0_12px_rgba(210,153,34,0.15)]",
      )}
    >
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <p className="stat-label flex items-center gap-2 mb-0">
          <Crosshair size={14} />
          高胜率反转 · 四条件叠加
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-blue/10 text-jarvis-blue font-mono">
            {symbol} · {timeframe}
          </span>
        </p>
        {resp?.ok && resp.direction !== "none" && (
          <span
            className={clsx(
              "inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium text-white",
              resp.direction === "bullish" ? "bg-jarvis-green" : "bg-jarvis-red",
            )}
          >
            {resp.direction === "bullish" ? (
              <TrendingUp size={10} />
            ) : (
              <TrendingDown size={10} />
            )}
            {resp.direction === "bullish" ? "看涨反转" : "看跌反转"}
          </span>
        )}
      </div>

      {failed ? (
        <div className="py-6 text-center">
          <p className="text-sm text-jarvis-text-secondary">评分计算失败</p>
          <p className="text-xs text-jarvis-text-secondary/70 mt-1">
            {resp?.error ?? error ?? "等待后端 /api/reversal-score 就绪后自动恢复"}
          </p>
        </div>
      ) : !resp || (loading && !data) ? (
        <div className="space-y-2 animate-pulse py-2">
          <div className="h-4 rounded bg-jarvis-border/30 w-2/3" />
          <div className="h-4 rounded bg-jarvis-border/30" />
          <div className="h-4 rounded bg-jarvis-border/30 w-1/2" />
        </div>
      ) : (
        <div className="flex gap-4">
          {/* 左：进度环 + verdict 徽标 */}
          <div className="flex flex-col items-center gap-2">
            <ScoreRing
              satisfied={resp.satisfied}
              max={resp.maxScore}
              color={verdict!.ring}
            />
            <span
              className={clsx(
                "text-[10px] px-2 py-0.5 rounded-full border font-medium whitespace-nowrap",
                verdict!.cls,
                isHigh && "animate-pulse",
              )}
            >
              {verdict!.label}
            </span>
          </div>

          {/* 右：四条件 checklist */}
          <div className="flex-1 min-w-0 space-y-1.5">
            {CONDITIONS.map(({ key, name, hint }) => {
              const cond = resp.conditions[key];
              if (!cond) return null;
              return (
                <div key={key} className="flex items-start gap-1.5" title={hint}>
                  <ConditionIcon cond={cond} />
                  <div className="min-w-0">
                    <p
                      className={clsx(
                        "text-[11px] font-medium leading-tight",
                        cond.unavailable
                          ? "text-jarvis-text-secondary/50"
                          : cond.met
                            ? "text-jarvis-text"
                            : "text-jarvis-text-secondary",
                      )}
                    >
                      {name}
                      {cond.unavailable && (
                        <span className="ml-1 text-[9px] text-jarvis-text-secondary/50">
                          （数据源未就绪）
                        </span>
                      )}
                    </p>
                    <p className="text-[10px] text-jarvis-text-secondary leading-relaxed">
                      {cond.note}
                    </p>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 底部一句话总结 */}
      {resp?.ok && (
        <p
          className={clsx(
            "mt-3 pt-2 border-t border-jarvis-border/60 text-[11px] leading-relaxed",
            isHigh ? "text-jarvis-yellow" : "text-jarvis-text-secondary",
          )}
        >
          {resp.note}
        </p>
      )}
    </div>
  );
}
