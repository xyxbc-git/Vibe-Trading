import { useState } from "react";
import { clsx } from "clsx";
import {
  Grid3X3,
  TrendingUp,
  TrendingDown,
  Minus,
  ChevronDown,
  AlertTriangle,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import {
  api,
  formatPrice,
  type ConsensusScope,
  type SignalDirection,
  type SignalTradePlan,
  type TwelveSignal,
  type TwelveTf,
} from "@/api/client";

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

const TF_OPTIONS: TwelveTf[] = ["15m", "1h", "4h", "1d"];

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

/** 折叠态：多空徽章 + 入场/止损/止盈紧凑 chips 一行 */
function PlanChips({
  plan,
  mismatch,
}: {
  plan: SignalTradePlan;
  mismatch: boolean;
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
    </div>
  );
}

/** 展开态：完整交易计划（醒目多空徽章 + 大白话摘要 + 三价位 + 备注） */
function PlanDetail({
  plan,
  mismatch,
}: {
  plan: SignalTradePlan;
  mismatch: boolean;
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
        <RrBadge rr={plan.rr} />
      </div>
      {side != null && (
        <p className="text-[10px] text-jarvis-text leading-relaxed bg-jarvis-bg rounded px-1.5 py-1">
          {planSummary(side, plan.entry_type, plan.entry, plan.stop_loss, plan.take_profit)}
        </p>
      )}
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

function SignalCell({ signal }: { signal: TwelveSignal }) {
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
        <ChevronDown
          size={12}
          className={clsx(
            "text-jarvis-text-secondary transition-transform",
            open && "rotate-180",
          )}
        />
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

      {/* 交易计划：折叠态紧凑 chips / 展开态完整点位（均带醒目多空徽章） */}
      {signal.trade_plan &&
        (open ? (
          <PlanDetail plan={signal.trade_plan} mismatch={mismatch} />
        ) : (
          <PlanChips plan={signal.trade_plan} mismatch={mismatch} />
        ))}

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
    </button>
  );
}

interface SignalBoardProps {
  symbol: string;
  /** 页级共识口径（受控）："auto" = 多周期综合 */
  tf: ConsensusScope;
  onTfChange: (tf: ConsensusScope) => void;
}

/** 12 套系统信号矩阵：每格 = 名称 / 方向色块 / 强度条 / 一句话理由 */
export default function SignalBoard({ symbol, tf, onTfChange }: SignalBoardProps) {
  // "auto"（综合）口径下矩阵仍需具体周期取数，用默认 4h
  const dataTf: TwelveTf = tf === "auto" ? AUTO_SIGNAL_TF : tf;
  const { data, loading, error } = usePolling(
    () => api.twelveSignals(symbol, dataTf),
    60_000,
    [symbol, dataTf],
  );

  // 防旧数据回写：响应回声的 tf 或 symbol 与当前请求不一致均视为过期，显示骨架
  const stale =
    data != null &&
    ((data.tf != null && data.tf !== dataTf) ||
      (data.symbol != null && data.symbol !== symbol));
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
        <div className="flex gap-1">
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
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
          {signals.map((s, i) => (
            <SignalCell key={`${s.system}-${i}`} signal={s} />
          ))}
        </div>
      )}
    </div>
  );
}
