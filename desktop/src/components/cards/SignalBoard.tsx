import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { clsx } from "clsx";
import {
  Grid3X3,
  TrendingUp,
  TrendingDown,
  Minus,
  ChevronDown,
  AlertTriangle,
  History,
  Loader2,
  CandlestickChart,
  Sparkles,
  Clock,
  GitCommitHorizontal,
  Waypoints,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import {
  api,
  formatPrice,
  type ConsensusScope,
  type SignalDirection,
  type SignalGradeStats,
  type SignalTradePlan,
  type SignalWinrateStats,
  type TwelveSignal,
  type TwelveTf,
} from "@/api/client";
import SignalExplainDrawer, { type ExplainRequest } from "./SignalExplainDrawer";
import { positionZoneToQuery } from "@/lib/positionZone";

/** 计划多空：优先后端显式 side 字段；缺失时由 SL/TP 相对入场价派生（多单 SL<入场<TP） */
export function planSide(plan: {
  side?: "long" | "short" | null;
  entry?: number;
  entry_zone?: [number, number];
  stop_loss: number;
  take_profit?: number;
  take_profit_1?: number;
}): "long" | "short" | null {
  if (plan.side === "long" || plan.side === "short") return plan.side;
  const entry = plan.entry ?? (plan.entry_zone ? (plan.entry_zone[0] + plan.entry_zone[1]) / 2 : NaN);
  const tp = plan.take_profit ?? plan.take_profit_1 ?? NaN;
  const sl = plan.stop_loss;
  if (![entry, tp, sl].every((v) => Number.isFinite(Number(v)))) return null;
  if (sl < entry && entry < tp) return "long";
  if (tp < entry && entry < sl) return "short";
  return null;
}

/** 醒目多空徽章：做多绿底 / 做空红底（含图标 + 文本双通道，不只靠颜色） */
export function SideBadge({
  side,
  size = "md",
}: {
  side: "long" | "short" | null;
  size?: "sm" | "md";
}) {
  if (side == null) return null;
  const isLong = side === "long";
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-0.5 rounded font-medium text-white whitespace-nowrap",
        isLong ? "bg-jarvis-green" : "bg-jarvis-red",
        size === "md" ? "text-[10px] px-1.5 py-0.5" : "text-[9px] px-1 py-px",
      )}
    >
      {isLong ? <TrendingUp size={size === "md" ? 10 : 9} /> : <TrendingDown size={size === "md" ? 10 : 9} />}
      {isLong ? "做多" : "做空"}
    </span>
  );
}

/** 大白话计划摘要：如「看涨 · 现价买入，跌破 60,879 认输，目标 64,700」 */
function planSummary(
  side: "long" | "short",
  entryType: string,
  entry: number,
  sl: number,
  tp: number,
): string {
  const e = formatPrice(entry);
  const s = formatPrice(sl);
  const t = formatPrice(tp);
  if (side === "long") {
    const how =
      entryType === "breakout"
        ? `突破 ${e} 追多`
        : entryType === "pullback"
          ? `回踩 ${e} 买入`
          : "现价买入";
    return `看涨 · ${how}，跌破 ${s} 认输，目标 ${t}`;
  }
  const how =
    entryType === "breakout"
      ? `跌破 ${e} 追空`
      : entryType === "pullback"
        ? `反弹 ${e} 做空`
        : "现价卖出做空";
  return `看跌 · ${how}，涨破 ${s} 认输，目标 ${t}`;
}

/**
 * 追高/追空警示（防「买了就跌」）：现价越过计划入场价 1% 以上时，
 * 提示等回踩/反抽到入场区再进场，而不是看到信号就市价追。
 * 供信号格与共识计划面板共用。
 */
export function ChaseWarning({
  side,
  price,
  entryLo,
  entryHi,
}: {
  side: "long" | "short" | null;
  /** 当前现价；缺失时不判定 */
  price: number | null | undefined;
  entryLo: number | null | undefined;
  /** 入场区间上沿；单点入场计划可不传 */
  entryHi?: number | null | undefined;
}) {
  if (side == null || price == null || entryLo == null) return null;
  const lo = Number(entryLo);
  const hi = Number(entryHi ?? entryLo);
  const p = Number(price);
  if (![lo, hi, p].every((v) => Number.isFinite(v) && v > 0)) return null;
  let overPct = 0;
  if (side === "long" && p > hi) overPct = (p / hi - 1) * 100;
  if (side === "short" && p < lo) overPct = (1 - p / lo) * 100;
  if (overPct < 1) return null; // 1% 以内视为正常滑点容忍
  const zone = hi !== lo ? `${formatPrice(lo)}~${formatPrice(hi)}` : formatPrice(lo);
  return (
    <p className="flex items-start gap-1 text-[10px] text-jarvis-yellow bg-jarvis-yellow/10 rounded px-1.5 py-1 mt-1.5 leading-relaxed">
      <AlertTriangle size={10} className="shrink-0 mt-px" />
      <span>
        现价已{side === "long" ? "高出计划入场" : "跌破计划入场"}{" "}
        {overPct.toFixed(1)}%，{side === "long" ? "追高" : "追空"}
        易接在短期{side === "long" ? "顶部" : "底部"}：建议等
        {side === "long" ? "回踩" : "反抽"} {zone} 再入场，或放弃本次机会
      </span>
    </p>
  );
}

/** 方向徽章与信号卡涨跌标签矛盾时的黄色警示（理论不应发生，出现=后端数据异常） */
function MismatchWarn() {
  return (
    <span
      className="inline-flex items-center gap-0.5 text-[9px] px-1 py-px rounded bg-jarvis-yellow/20 text-jarvis-yellow whitespace-nowrap"
      title="计划方向与信号涨跌标签不一致，疑似数据异常，请勿按此计划操作"
    >
      <AlertTriangle size={9} />
      方向矛盾
    </span>
  );
}

/** 相对时间：<60s「刚刚」/ <1h「X分钟前」/ <24h「X小时前」/ 其余「X天前」 */
export function relTime(unixSec: number | null | undefined): string {
  if (unixSec == null || !Number.isFinite(Number(unixSec))) return "—";
  const diff = Date.now() / 1000 - Number(unixSec);
  if (diff < 0) return "刚刚";
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  return `${Math.floor(diff / 86400)}天前`;
}

/** 绝对时钟（悬停提示用）：本地时区 MM-DD HH:mm:ss */
export function clockTime(unixSec: number | null | undefined): string {
  if (unixSec == null || !Number.isFinite(Number(unixSec))) return "—";
  const d = new Date(Number(unixSec) * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 信号更新/变更时间徽章行（需求 1：记录每个信号的最新更新时间与上次变更） */
function SignalTimeLine({
  updatedAt,
  changedAt,
}: {
  updatedAt: number | null | undefined;
  changedAt: number | null | undefined;
}) {
  if (updatedAt == null && changedAt == null) return null;
  return (
    <p className="flex items-center gap-2 text-[9px] text-jarvis-text-secondary/80 font-mono mt-1.5 flex-wrap">
      {updatedAt != null && (
        <span
          className="inline-flex items-center gap-0.5 cursor-help"
          title={`最近一次信号计算：${clockTime(updatedAt)}`}
        >
          <Clock size={8} />
          更新 {relTime(updatedAt)}
        </span>
      )}
      {changedAt != null ? (
        <span
          className="inline-flex items-center gap-0.5 cursor-help text-jarvis-blue/80"
          title={`最近一次实质变更（方向/强度/计划）：${clockTime(changedAt)}`}
        >
          <GitCommitHorizontal size={8} />
          变更 {relTime(changedAt)}
        </span>
      ) : updatedAt != null ? (
        <span className="text-jarvis-text-secondary/50" title="有记录以来该信号尚未发生实质变更">
          无变更记录
        </span>
      ) : null}
    </p>
  );
}

const DIR_META: Record<
  SignalDirection,
  {
    label: string;
    text: string;
    bg: string;
    border: string;
    bar: string;
    icon: React.ReactNode;
  }
> = {
  bullish: {
    label: "看涨",
    text: "text-jarvis-green",
    bg: "bg-jarvis-green/10",
    border: "border-jarvis-green/30",
    bar: "bg-jarvis-green",
    icon: <TrendingUp size={13} />,
  },
  bearish: {
    label: "看跌",
    text: "text-jarvis-red",
    bg: "bg-jarvis-red/10",
    border: "border-jarvis-red/30",
    bar: "bg-jarvis-red",
    icon: <TrendingDown size={13} />,
  },
  neutral: {
    label: "中性",
    text: "text-jarvis-text-secondary",
    bg: "bg-jarvis-bg",
    border: "border-jarvis-border",
    bar: "bg-jarvis-text-secondary/50",
    icon: <Minus size={13} />,
  },
};

function normalizeDirection(d: unknown): SignalDirection {
  return d === "bullish" || d === "bearish" ? d : "neutral";
}

const TF_OPTIONS: TwelveTf[] = ["5m", "15m", "30m", "1h", "4h", "1d"];

/** "auto"（综合）口径下信号矩阵的默认取数周期 */
export const AUTO_SIGNAL_TF: TwelveTf = "4h";

/** 入场方式措辞按多空区分（旧版全写「买入」，做空计划被误读的根源之一） */
function entryTypeCn(entryType: string, side: "long" | "short" | null): string {
  if (side === "short") {
    return (
      { breakout: "跌破做空", pullback: "反弹做空", market: "市价做空" }[
        entryType
      ] ?? entryType
    );
  }
  return (
    { breakout: "突破买入", pullback: "回踩买入", market: "市价买入" }[
      entryType
    ] ?? entryType
  );
}

/** 历史胜率颜色：≥60 绿 / 45~60 黄 / <45 红（与胜率统计卡同口径） */
function winRateColor(rate: number): string {
  if (rate >= 60) return "text-jarvis-green";
  if (rate >= 45) return "text-jarvis-yellow";
  return "text-jarvis-red";
}

/**
 * 单信号历史胜率行（折叠态）：「近 30 次胜率 63% · 盈亏比 1.8」。
 * 数据来自信号胜率回测缓存；该系统该方向无历史样本时显示「无历史样本」。
 * onShowMarks 存在时追加「盈损点」入口：跳 K 线图标出每笔历史盈损位置。
 */
function WinRateLine({
  grade,
  expanded,
  onShowMarks,
}: {
  grade: SignalGradeStats | null | undefined;
  expanded: boolean;
  onShowMarks?: () => void;
}) {
  if (grade == null) {
    return (
      <p className="text-[10px] text-jarvis-text-secondary/70 mt-1">
        该方向暂无历史触发样本（可点右上角「胜率回测」积累）
      </p>
    );
  }
  return (
    <div className="mt-1">
      <p className="text-[10px] font-mono flex items-center gap-1 flex-wrap">
        <span className={winRateColor(grade.win_rate_pct)}>
          近 {grade.trades} 次胜率 {grade.win_rate_pct.toFixed(0)}%
        </span>
        {grade.payoff_ratio != null && (
          <span className="text-jarvis-text-secondary">
            · 盈亏比 {grade.payoff_ratio.toFixed(1)}
          </span>
        )}
        {grade.low_sample && (
          <span
            className="inline-flex items-center gap-0.5 text-[9px] px-1 py-px rounded bg-jarvis-yellow/15 text-jarvis-yellow cursor-help"
            title="该分组样本量 <30，统计结果仅供参考"
          >
            <AlertTriangle size={8} />
            样本不足
          </span>
        )}
        {onShowMarks && (
          // 信号格整体是 <button>，内嵌交互用 span+role 避免非法嵌套
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation();
              onShowMarks();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                e.stopPropagation();
                onShowMarks();
              }
            }}
            title="跳到 K 线图，把这条信号历史上每笔的入场/出场位置标出来（绿=盈、红=亏）"
            className="inline-flex items-center gap-0.5 text-[9px] px-1.5 py-px rounded border border-jarvis-blue/40 text-jarvis-blue hover:bg-jarvis-blue/10 transition-colors cursor-pointer"
          >
            <CandlestickChart size={9} />
            盈损点
          </span>
        )}
      </p>
      {expanded && (
        <p className="text-[10px] font-mono text-jarvis-text-secondary mt-0.5">
          期望{" "}
          <span className={grade.expectancy_pct >= 0 ? "text-jarvis-green" : "text-jarvis-red"}>
            {grade.expectancy_pct >= 0 ? "+" : ""}
            {grade.expectancy_pct.toFixed(2)}%
          </span>
          /笔 · 最大回撤{" "}
          <span className="text-jarvis-red">{grade.max_drawdown_pct.toFixed(1)}%</span>
          {" "}· 均持有 {grade.avg_bars_held.toFixed(0)} 根
        </p>
      )}
    </div>
  );
}

/** 展开态信号解释块：类型 / 触发依据 / 适用周期 / 滞后特性 */
function ExplainDetail({
  signal,
  currentTf,
}: {
  signal: TwelveSignal;
  currentTf: string;
}) {
  const ex = signal.explain;
  if (!ex) return null;
  const tfMismatch =
    ex.best_tfs.length > 0 && !ex.best_tfs.includes(currentTf);
  return (
    <div className="mt-2 pt-2 border-t border-jarvis-border/60 space-y-1">
      <p className="text-[10px] text-jarvis-text-secondary">
        <span className="text-jarvis-text">信号解释</span>
        <span className="ml-1.5 px-1 py-px rounded bg-jarvis-blue/10 text-jarvis-blue">
          {ex.type}
        </span>
      </p>
      <p className="text-[10px] text-jarvis-text-secondary leading-relaxed">
        触发依据：{ex.trigger}
      </p>
      {ex.best_tfs.length > 0 && (
        <p className="text-[10px] text-jarvis-text-secondary flex items-center gap-1 flex-wrap">
          适用周期：
          {ex.best_tfs.map((t) => (
            <span
              key={t}
              className={clsx(
                "px-1 py-px rounded font-mono",
                t === currentTf
                  ? "bg-jarvis-green/15 text-jarvis-green"
                  : "bg-jarvis-bg text-jarvis-text-secondary",
              )}
            >
              {t}
            </span>
          ))}
          {tfMismatch && (
            <span
              className="inline-flex items-center gap-0.5 text-[9px] px-1 py-px rounded bg-jarvis-yellow/15 text-jarvis-yellow cursor-help"
              title={`当前查看的是 ${currentTf} 周期，不在该系统的推荐周期内，信号参考价值打折`}
            >
              <AlertTriangle size={8} />
              周期不匹配
            </span>
          )}
        </p>
      )}
      <p className="text-[10px] text-jarvis-text-secondary/80 leading-relaxed">
        {ex.lag}
      </p>
    </div>
  );
}

/** 盈亏比徽章：rr≥2 绿 / 1.5~2 黄 / <1.5 红 + 提示参考价值有限 */
function RrBadge({ rr }: { rr: number | null | undefined }) {
  if (rr == null || !Number.isFinite(Number(rr))) return null;
  const v = Number(rr);
  const low = v < 1.5;
  const cls =
    v >= 2
      ? "bg-jarvis-green/15 text-jarvis-green"
      : v >= 1.5
        ? "bg-jarvis-yellow/15 text-jarvis-yellow"
        : "bg-jarvis-red/15 text-jarvis-red";
  return (
    <span
      className={clsx(
        "text-[10px] px-1.5 py-0.5 rounded-full font-mono font-medium",
        low && "cursor-help",
        cls,
      )}
      title={low ? "盈亏比偏低（<1.5），潜在收益相对风险不划算，参考价值有限" : undefined}
    >
      盈亏比 {v.toFixed(1)}
      {low && " ⚠"}
    </span>
  );
}

/** 「K线区间」入口：跳 K 线图画该计划的多空区间图（TradingView position 风格）。
 *  信号格整体是 <button>，内嵌交互用 span+role 避免非法嵌套（与「盈损点」同模式）。 */
function ZoneChartEntry({
  onShowZone,
  size = "sm",
}: {
  onShowZone: () => void;
  size?: "sm" | "md";
}) {
  return (
    <span
      role="button"
      tabIndex={0}
      onClick={(e) => {
        e.stopPropagation();
        onShowZone();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          onShowZone();
        }
      }}
      title="跳到 K 线图，把这套计划画成多空区间图：绿色=盈利目标区、红色=止损风险区，入/损/盈三线+盈亏比一目了然"
      className={clsx(
        "inline-flex items-center gap-0.5 rounded border border-jarvis-blue/40 text-jarvis-blue hover:bg-jarvis-blue/10 transition-colors cursor-pointer",
        size === "md" ? "text-[10px] px-1.5 py-0.5" : "text-[9px] px-1.5 py-px",
      )}
    >
      <CandlestickChart size={size === "md" ? 10 : 9} />
      K线区间
    </span>
  );
}

/** 折叠态：多空徽章 + 入场/止损/止盈紧凑 chips 一行 */
function PlanChips({
  plan,
  mismatch,
  onShowZone,
}: {
  plan: SignalTradePlan;
  mismatch: boolean;
  /** 跳 K 线图画多空区间图；计划方向不可判定时不传（不显示入口） */
  onShowZone?: () => void;
}) {
  const side = planSide(plan);
  return (
    <div className="flex flex-wrap items-center gap-1 mt-1.5">
      <SideBadge side={side} size="sm" />
      {mismatch && <MismatchWarn />}
      <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-blue/10 text-jarvis-blue font-mono">
        入 {formatPrice(plan.entry)}
      </span>
      <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-red/10 text-jarvis-red font-mono">
        损 {formatPrice(plan.stop_loss)}
      </span>
      <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-green/10 text-jarvis-green font-mono">
        盈 {formatPrice(plan.take_profit)}
      </span>
      {onShowZone && <ZoneChartEntry onShowZone={onShowZone} />}
    </div>
  );
}

/** 展开态：完整交易计划（醒目多空徽章 + 大白话摘要 + 三价位 + 追高警示 + 备注） */
function PlanDetail({
  plan,
  mismatch,
  price,
  onShowZone,
}: {
  plan: SignalTradePlan;
  mismatch: boolean;
  /** 当前现价（用于追高/追空判定）；缺失时不判定 */
  price?: number | null;
  /** 跳 K 线图画多空区间图；计划方向不可判定时不传（不显示入口） */
  onShowZone?: () => void;
}) {
  const side = planSide(plan);
  return (
    <div className="mt-2 pt-2 border-t border-jarvis-border/60 space-y-1.5">
      <div className="flex items-center justify-between gap-1 flex-wrap">
        <span className="flex items-center gap-1.5">
          <SideBadge side={side} />
          {mismatch && <MismatchWarn />}
          <span className="text-[10px] text-jarvis-text-secondary">
            {entryTypeCn(plan.entry_type, side)}
          </span>
        </span>
        <span className="flex items-center gap-1.5">
          {onShowZone && <ZoneChartEntry onShowZone={onShowZone} size="md" />}
          <RrBadge rr={plan.rr} />
        </span>
      </div>
      {side != null && (
        <p className="text-[10px] text-jarvis-text leading-relaxed bg-jarvis-bg rounded px-1.5 py-1">
          {planSummary(side, plan.entry_type, plan.entry, plan.stop_loss, plan.take_profit)}
        </p>
      )}
      <ChaseWarning side={side} price={price} entryLo={plan.entry} />

      <div className="grid grid-cols-3 gap-1 text-[11px] font-mono">
        <div>
          <p className="text-jarvis-text-secondary text-[9px]">入场</p>
          <p className="text-jarvis-text">{formatPrice(plan.entry)}</p>
        </div>
        <div>
          <p className="text-jarvis-text-secondary text-[9px]">
            止损{side === "short" ? "（涨破离场）" : side === "long" ? "（跌破离场）" : ""}
          </p>
          <p className="text-jarvis-red">{formatPrice(plan.stop_loss)}</p>
        </div>
        <div>
          <p className="text-jarvis-text-secondary text-[9px]">止盈</p>
          <p className="text-jarvis-green">{formatPrice(plan.take_profit)}</p>
        </div>
      </div>
      {plan.note && (
        <p className="text-[10px] text-jarvis-text-secondary leading-relaxed">
          {plan.note}
        </p>
      )}
    </div>
  );
}

function SignalCell({
  signal,
  winrate,
  currentTf,
  price,
  onShowMarks,
  onExplain,
  onShowZone,
  onShowStructure,
}: {
  signal: TwelveSignal;
  /** 该 symbol×tf 的胜率回测缓存；null = 尚未回测（不渲染胜率行） */
  winrate: SignalWinrateStats | null;
  currentTf: string;
  /** 当前现价（追高/追空警示用） */
  price?: number | null;
  /** 跳 K 线图标记该信号历史盈损点（system + 方向） */
  onShowMarks?: (system: string, side: "long" | "short") => void;
  /** 一键解读：把该信号解释成大白话（携带当前方向的胜率统计） */
  onExplain?: (signal: TwelveSignal, grade: SignalGradeStats | null) => void;
  /** 跳 K 线图画该计划的多空区间图（TradingView position 风格） */
  onShowZone?: (plan: SignalTradePlan, name: string) => void;
  /** 跳 K 线图叠加该系统的趋势结构（关键位 + 区间 + 方向标注） */
  onShowStructure?: (signal: TwelveSignal) => void;
}) {
  const [open, setOpen] = useState(false);
  const dir = normalizeDirection(signal.direction);
  const meta = DIR_META[dir];
  const strength = Math.max(0, Math.min(1, Number(signal.strength ?? 0)));
  // 计划方向与信号涨跌标签矛盾检测（bullish↔做多 / bearish↔做空；不一致=数据异常要示警）
  const side = signal.trade_plan ? planSide(signal.trade_plan) : null;
  const mismatch =
    side != null &&
    ((dir === "bullish" && side !== "long") ||
      (dir === "bearish" && side !== "short"));
  // 当前信号方向对应的历史触发统计（中性信号不展示胜率）
  const grade =
    winrate && dir !== "neutral"
      ? (winrate.systems[signal.system]?.[dir === "bullish" ? "long" : "short"] ?? null)
      : undefined;
  const gradeSide: "long" | "short" = dir === "bearish" ? "short" : "long";

  return (
    <button
      onClick={() => setOpen((v) => !v)}
      className={clsx(
        "text-left rounded-lg border p-3 transition-colors hover:border-jarvis-blue/50",
        meta.bg,
        meta.border,
      )}
    >
      <div className="flex items-center justify-between gap-1">
        <span className="text-xs font-medium text-jarvis-text truncate">
          {signal.name_cn || signal.system}
        </span>
        <span className={clsx("flex items-center gap-0.5 text-xs", meta.text)}>
          {meta.icon}
          {meta.label}
        </span>
      </div>

      {/* 强度条 */}
      <div className="mt-2 h-1.5 rounded-full bg-jarvis-bg overflow-hidden">
        <div
          className={clsx("h-full rounded-full", meta.bar)}
          style={{ width: `${strength * 100}%` }}
        />
      </div>
      <div className="flex items-center justify-between mt-1">
        <span className="text-[10px] text-jarvis-text-secondary font-mono">
          强度 {(strength * 100).toFixed(0)}%
        </span>
        <span className="flex items-center gap-1.5">
          {onShowStructure && (signal.key_levels?.length ?? 0) > 0 && (
            // 信号格整体是 <button>，内嵌交互用 span+role 避免非法嵌套
            <span
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation();
                onShowStructure(signal);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  e.stopPropagation();
                  onShowStructure(signal);
                }
              }}
              title="跳到 K 线图，把该系统的趋势结构画上去：它的关键价位（突破位/回撤位/中枢等）+ 交易计划区间 + 当前方向标注"
              className="inline-flex items-center gap-0.5 text-[9px] px-1.5 py-px rounded border border-jarvis-blue/40 text-jarvis-blue hover:bg-jarvis-blue/10 transition-colors cursor-pointer"
            >
              <Waypoints size={9} />
              结构
            </span>
          )}
          {onExplain && (
            <span
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation();
                onExplain(signal, grade ?? null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  e.stopPropagation();
                  onExplain(signal, grade ?? null);
                }
              }}
              title="AI 用大白话解释这个信号：它是什么、现在在说什么、胜率怎么读、要注意什么"
              className="inline-flex items-center gap-0.5 text-[9px] px-1.5 py-px rounded border border-jarvis-purple/40 text-jarvis-purple hover:bg-jarvis-purple/10 transition-colors cursor-pointer"
            >
              <Sparkles size={9} />
              解读
            </span>
          )}
          <ChevronDown
            size={12}
            className={clsx(
              "text-jarvis-text-secondary transition-transform",
              open && "rotate-180",
            )}
          />
        </span>
      </div>

      {/* 一句话理由：折叠态截断但悬停可看全文，点击展开看完整（防截半句误导方向） */}
      {signal.reasoning && (
        <p
          title={open ? undefined : signal.reasoning}
          className={clsx(
            "text-[11px] text-jarvis-text-secondary mt-1.5 leading-relaxed",
            !open && "line-clamp-1 cursor-help",
          )}
        >
          {signal.reasoning}
        </p>
      )}

      {/* 更新/变更时间徽章（需求 1：每个信号的最新更新时间 + 上次变更时间） */}
      <SignalTimeLine updatedAt={signal.updated_at} changedAt={signal.last_change_at} />

      {/* 历史胜率：方向信号 + 已有回测缓存时展示「近 N 次胜率 x%」+ 盈损点入口 */}
      {grade !== undefined && (
        <WinRateLine
          grade={grade}
          expanded={open}
          onShowMarks={
            onShowMarks ? () => onShowMarks(signal.system, gradeSide) : undefined
          }
        />
      )}

      {/* 交易计划：折叠态紧凑 chips / 展开态完整点位（均带醒目多空徽章 + K线区间入口） */}
      {signal.trade_plan &&
        (() => {
          const plan = signal.trade_plan;
          // 方向可判定才给「K线区间」入口（区间几何要求多空明确）
          const showZone =
            onShowZone && planSide(plan) != null
              ? () => onShowZone(plan, signal.name_cn || signal.system)
              : undefined;
          return open ? (
            <PlanDetail plan={plan} mismatch={mismatch} price={price} onShowZone={showZone} />
          ) : (
            <PlanChips plan={plan} mismatch={mismatch} onShowZone={showZone} />
          );
        })()}

      {open && (signal.key_levels?.length ?? 0) > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {signal.key_levels!.map((lv, i) => (
            <span
              key={`${lv.label}-${i}`}
              className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-card border border-jarvis-border text-jarvis-text-secondary font-mono"
            >
              {lv.label} {Number(lv.price).toLocaleString()}
            </span>
          ))}
        </div>
      )}

      {/* 展开态信号解释：类型 / 触发依据 / 适用周期（当前周期高亮）/ 滞后特性 */}
      {open && <ExplainDetail signal={signal} currentTf={currentTf} />}
    </button>
  );
}

interface SignalBoardProps {
  symbol: string;
  /** 页级共识口径（受控）："auto" = 多周期综合 */
  tf: ConsensusScope;
  onTfChange: (tf: ConsensusScope) => void;
}

/** 12 套系统信号矩阵：每格 = 名称 / 方向色块 / 强度条 / 一句话理由 / 历史胜率 */
export default function SignalBoard({ symbol, tf, onTfChange }: SignalBoardProps) {
  // "auto"（综合）口径下矩阵仍需具体周期取数，用默认 4h
  const dataTf: TwelveTf = tf === "auto" ? AUTO_SIGNAL_TF : tf;
  const navigate = useNavigate();
  // 「盈损点」：跳 K 线页并携带 信号系统+回测周期+方向，由 Chart 页拉逐笔明细打标
  const showMarksOnChart = (system: string, side: "long" | "short") => {
    const q = new URLSearchParams({ sigmarks: system, sigtf: dataTf, sigside: side });
    navigate(`/chart?${q.toString()}`);
  };

  // 「结构」：跳 K 线页叠加该系统的趋势结构（关键位水平线 + 计划区间 + 方向状态条）
  const showStructureOnChart = (signal: TwelveSignal) => {
    const q = new URLSearchParams({ sysoverlay: signal.system, systf: dataTf });
    navigate(`/chart?${q.toString()}`);
  };

  // 「K线区间」：跳 K 线页画该计划的多空区间图（入/损/盈三价 + 方向经 query 传递）
  const showZoneOnChart = (plan: SignalTradePlan, name: string) => {
    const side = planSide(plan);
    if (side == null) return;
    const q = positionZoneToQuery({
      side,
      entry: plan.entry,
      stopLoss: plan.stop_loss,
      takeProfit: plan.take_profit,
      name,
      tf: dataTf,
    });
    navigate(`/chart?${q.toString()}`);
  };

  // 「一键解读」抽屉：null = 关闭；打开即流式请求大白话解释
  const [explainReq, setExplainReq] = useState<ExplainRequest | null>(null);
  const { data, loading, error } = usePolling(
    () => api.twelveSignals(symbol, dataTf),
    60_000,
    [symbol, dataTf],
  );

  // 单信号级历史胜率缓存（低频轮询即可；后端读 JSON 缓存，无重计算开销）
  const { data: wrResp, refetch: refetchWinrate } = usePolling(
    () => api.twelveSignalWinrate(symbol, dataTf),
    120_000,
    [symbol, dataTf],
  );
  // 防旧数据回写：胜率响应的 symbol/tf 与当前不一致视为过期
  const winrate =
    wrResp?.ok &&
    (wrResp.symbol == null || wrResp.symbol === symbol) &&
    (wrResp.tf == null || wrResp.tf === dataTf)
      ? wrResp.stats
      : null;

  // 单信号解读：信号卡现成数据 + 该方向胜率统计打包给后端进 prompt
  const explainSignal = (signal: TwelveSignal, grade: SignalGradeStats | null) => {
    setExplainReq({
      title: `${signal.name_cn || signal.system} · 信号解读`,
      body: {
        mode: "signal",
        symbol,
        tf: dataTf,
        payload: {
          name_cn: signal.name_cn,
          system: signal.system,
          direction: signal.direction,
          strength: signal.strength,
          reasoning: signal.reasoning,
          explain: signal.explain ?? null,
          trade_plan: signal.trade_plan ?? null,
          grade,
          winrate_meta: winrate
            ? { horizon_bars: winrate.horizon_bars, samples: winrate.samples, days: winrate.days ?? null }
            : null,
        },
      },
    });
  };

  const [wrBusy, setWrBusy] = useState(false);
  const [wrMsg, setWrMsg] = useState("");
  const wrTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (wrTimer.current != null) window.clearInterval(wrTimer.current);
    },
    [],
  );

  const startWinrateBacktest = async () => {
    setWrBusy(true);
    setWrMsg("正在启动胜率回测…");
    try {
      // 只跑当前 symbol 全周期，30 天窗口（几分钟量级）
      const res = await api.twelveSignalWinrateRun({ symbols: symbol, days: 30 });
      if (!res.ok) {
        setWrMsg(res.error ?? "启动失败");
        setWrBusy(false);
        return;
      }
      wrTimer.current = window.setInterval(async () => {
        try {
          const st = await api.twelveSignalWinrateStatus();
          if (st.running) {
            setWrMsg(`胜率回测中 ${st.progress}% · ${st.detail}`);
            return;
          }
          if (wrTimer.current != null) {
            window.clearInterval(wrTimer.current);
            wrTimer.current = null;
          }
          setWrBusy(false);
          if (st.error) {
            setWrMsg(`回测失败：${st.error}`);
          } else {
            setWrMsg(`回测完成，累计 ${st.result?.total_samples ?? 0} 个信号样本`);
            refetchWinrate();
          }
          window.setTimeout(() => setWrMsg(""), 10_000);
        } catch {
          // 单次进度查询失败忽略，下一轮再试
        }
      }, 2_000);
    } catch (e) {
      setWrMsg(e instanceof Error ? e.message : "启动失败");
      setWrBusy(false);
    }
  };

  // 防旧数据回写：响应回声的 tf 或 symbol 与当前请求不一致均视为过期，显示骨架
  const stale =
    data != null &&
    ((data.tf != null && data.tf !== dataTf) ||
      (data.symbol != null && data.symbol !== symbol));
  // 后端把不认识的周期回退到默认档（如旧进程白名单无 5m/30m → 回声恒为 4h）时，
  // 响应永远「过期」，按 stale 骨架处理会永久空白。请求已结束（!loading）而回声
  // tf 仍不匹配 = 本轮最终响应就是回退结果，单独识别为「周期不支持」错误态。
  const tfRejected =
    !loading &&
    data != null &&
    data.ok !== false &&
    data.tf != null &&
    data.tf !== dataTf &&
    (data.symbol == null || data.symbol === symbol);
  // 封套：{ok, signals:[...], consensus}；ok:false（如 K 线拉取失败）显示失败态而非空态；
  // 过期响应的 ok:false 不算当前口径失败
  const failed = Boolean(error) || (!stale && data != null && !data.ok);
  const signals = data?.ok && !stale ? (data.signals ?? []) : [];
  const bullCount = signals.filter(
    (s) => normalizeDirection(s.direction) === "bullish",
  ).length;
  const bearCount = signals.filter(
    (s) => normalizeDirection(s.direction) === "bearish",
  ).length;
  const neutralCount = signals.length - bullCount - bearCount;

  // 整体解读：把 12 套投票分布 + 各系统方向摘要交给 AI 解释「为什么分歧」
  const explainConsensus = () => {
    setExplainReq({
      title: `12 系统整体解读 · ${symbol}`,
      body: {
        mode: "consensus",
        symbol,
        tf: dataTf,
        payload: {
          votes: { bullish: bullCount, bearish: bearCount, neutral: neutralCount },
          consensus: data?.consensus
            ? {
                direction: data.consensus.direction,
                confidence: data.consensus.confidence,
                votes: data.consensus.votes,
              }
            : null,
          per_system: signals.map((s) => ({
            name_cn: s.name_cn || s.system,
            direction: s.direction,
            strength: s.strength,
          })),
        },
      },
    });
  };

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <p className="stat-label flex items-center gap-2 mb-0">
          <Grid3X3 size={14} />
          12 系统信号矩阵 · {symbol}
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-blue/10 text-jarvis-blue font-mono">
            {dataTf}
            {tf === "auto" && " 信号"}
          </span>
          {signals.length > 0 && (
            <span className="text-xs font-mono">
              <span className="text-jarvis-green">{bullCount}涨</span>
              <span className="text-jarvis-text-secondary mx-1">/</span>
              <span className="text-jarvis-red">{bearCount}跌</span>
            </span>
          )}
        </p>
        <div className="flex gap-1 items-center">
          {signals.length > 0 && (
            <button
              onClick={explainConsensus}
              title={
                bullCount > 0 && bearCount > 0
                  ? `当前 ${bullCount}多 ${bearCount}空 ${neutralCount}中性——AI 解释系统之间为什么分歧、该怎么看`
                  : "AI 用大白话解读 12 套系统当前的整体态度"
              }
              className="flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-jarvis-purple/40 text-jarvis-purple hover:bg-jarvis-purple/10 transition-colors mr-1"
            >
              <Sparkles size={11} />
              整体解读
            </button>
          )}
          <button
            onClick={startWinrateBacktest}
            disabled={wrBusy}
            title={
              winrate
                ? `用历史 K 线重算每格信号的触发胜率（当前样本 ${winrate.samples} 个，观察期 ${winrate.horizon_bars} 根）`
                : "用历史 K 线统计每格信号历史触发后的胜率/盈亏比/最大回撤，随信号展示"
            }
            className="flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-jarvis-purple/40 text-jarvis-purple hover:bg-jarvis-purple/10 transition-colors disabled:opacity-60 disabled:cursor-not-allowed mr-1"
          >
            {wrBusy ? (
              <Loader2 size={11} className="animate-spin" />
            ) : (
              <History size={11} />
            )}
            {wrBusy ? "回测中…" : "胜率回测"}
          </button>
          <button
            onClick={() => navigate(`/signal-history?symbol=${symbol}&tf=${dataTf}`)}
            title="查看该币种该周期 12 套信号的变更流水：谁翻多翻空、强度/计划怎么变的，逐条可追溯"
            className="flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-jarvis-blue/40 text-jarvis-blue hover:bg-jarvis-blue/10 transition-colors mr-1"
          >
            <GitCommitHorizontal size={11} />
            变更历史
          </button>
          <button
            onClick={() => onTfChange("auto")}
            className={clsx(
              "text-xs px-2.5 py-1 rounded-md transition-colors",
              tf === "auto"
                ? "bg-jarvis-blue/15 text-jarvis-blue"
                : "text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/5",
            )}
            title="共识仪表盘用多周期综合；矩阵默认展示 4h 信号"
          >
            综合
          </button>
          {TF_OPTIONS.map((t) => (
            <button
              key={t}
              onClick={() => onTfChange(t)}
              className={clsx(
                "text-xs px-2.5 py-1 rounded-md font-mono transition-colors",
                tf === t
                  ? "bg-jarvis-blue/15 text-jarvis-blue"
                  : "text-jarvis-text-secondary hover:text-jarvis-text hover:bg-white/5",
              )}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      {wrMsg && (
        <p className="text-xs text-jarvis-text-secondary mb-2 bg-jarvis-bg rounded-md px-2.5 py-1.5">
          {wrMsg}
        </p>
      )}

      {failed ? (
        <div className="py-8 text-center">
          <p className="text-sm text-jarvis-text-secondary">
            {error ? "信号引擎未启动" : "信号取数失败"}
          </p>
          <p className="text-xs text-jarvis-text-secondary/70 mt-1">
            {!stale && data && !data.ok && data.error
              ? String(data.error)
              : "等待后端 /api/twelve/signals 就绪后自动恢复"}
          </p>
        </div>
      ) : tfRejected ? (
        <div className="py-8 text-center">
          <p className="text-sm text-jarvis-yellow">
            后端暂不支持 {dataTf} 周期（响应回退到 {data?.tf}）
          </p>
          <p className="text-xs text-jarvis-text-secondary/70 mt-1">
            运行中的后端进程可能还是旧版本——重启后端服务（jarvis_dashboard.py）后自动恢复
          </p>
        </div>
      ) : (loading && !data) || stale ? (
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3 animate-pulse">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="h-[88px] rounded-lg bg-jarvis-border/30" />
          ))}
        </div>
      ) : signals.length === 0 ? (
        <p className="text-sm text-jarvis-text-secondary py-8 text-center">
          暂无信号数据（{dataTf} 周期）
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
            {signals.map((s, i) => (
              <SignalCell
                key={`${s.system}-${i}`}
                signal={s}
                winrate={winrate}
                currentTf={dataTf}
                price={data?.price ?? null}
                onShowMarks={showMarksOnChart}
                onExplain={explainSignal}
                onShowZone={showZoneOnChart}
                onShowStructure={showStructureOnChart}
              />
            ))}
          </div>
          <p className="text-[10px] text-jarvis-text-secondary mt-2">
            {winrate ? (
              <>
                历史胜率口径：信号方向翻转当根收盘价入场，{winrate.horizon_bars} 根内先触
                止损/止盈定输赢（无计划按期末收盘），同根双触保守计亏；共{" "}
                {winrate.samples} 个样本
                {winrate.days ? `（近 ${winrate.days} 天）` : ""}
                ，样本 &lt;30 的分组带「样本不足」徽标。点信号格可展开触发原因与适用周期；点「盈损点」跳
                K 线图查看该信号每笔历史盈亏的位置。
              </>
            ) : (
              "尚未做过该周期的信号胜率回测 · 点右上角「胜率回测」用历史 K 线统计每格信号的真实胜率，判断该不该信它。"
            )}
          </p>
        </>
      )}

      {/* 一键解读抽屉（单信号 / 整体分歧共用） */}
      <SignalExplainDrawer request={explainReq} onClose={() => setExplainReq(null)} />
    </div>
  );
}
