import { clsx } from "clsx";
import {
  Sparkles,
  Loader2,
  ArrowDownToLine,
  AlertTriangle,
  PauseCircle,
  Cpu,
} from "lucide-react";
import { formatPrice } from "@/api/client";
import { SideBadge } from "@/components/cards/SignalBoard";
import type { AiTradeAdvice } from "@/lib/aiTradeAdvice";

const RISK_META = {
  low: { label: "低风险", cls: "bg-jarvis-green/15 text-jarvis-green" },
  medium: { label: "中风险", cls: "bg-jarvis-yellow/15 text-jarvis-yellow" },
  high: { label: "高风险", cls: "bg-jarvis-red/15 text-jarvis-red" },
} as const;

/** 建议值 vs 当前表单值相对差异超过该比例 → 高亮提示 */
const DIVERGENCE_RATIO = 0.3;

function diverges(current: number | null, advised: number | null): boolean {
  if (current == null || advised == null || advised <= 0) return false;
  return Math.abs(current - advised) / advised > DIVERGENCE_RATIO;
}

/** 建议指标格：与当前表单值差异大时黄色高亮 */
function AdviceCell({
  label,
  value,
  highlight,
  hint,
}: {
  label: string;
  value: string;
  highlight?: boolean;
  hint?: string;
}) {
  return (
    <div
      className={clsx(
        "rounded-lg p-2",
        highlight
          ? "bg-jarvis-yellow/10 border border-jarvis-yellow/40"
          : "bg-jarvis-bg",
      )}
      title={highlight ? `当前手动值与建议差异较大${hint ? `：${hint}` : ""}` : hint}
    >
      <p className="text-jarvis-text-secondary text-[9px]">
        {label}
        {highlight && <span className="text-jarvis-yellow ml-1">≠手动值</span>}
      </p>
      <p className="text-jarvis-text text-sm font-mono">{value}</p>
    </div>
  );
}

interface AiAdviceCardProps {
  advice: AiTradeAdvice | null;
  loading: boolean;
  onRun: () => void;
  onApply: (a: AiTradeAdvice) => void;
  /** 当前表单值（差异高亮对比）；未填时传 null */
  current: {
    leverage: number | null;
    positionPct: number | null;
    stopLossPrice: number | null;
    takeProfitPrice: number | null;
  };
}

/** AI 点位/风控建议卡：AI 优先、本地规则降级，一键填入表单 */
export default function AiAdviceCard({
  advice,
  loading,
  onRun,
  onApply,
  current,
}: AiAdviceCardProps) {
  const a = advice;
  const isWait = a?.side === "wait";
  const riskMeta = a ? RISK_META[a.riskLevel] : null;

  return (
    <div className="pt-2 border-t border-jarvis-border/60 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-jarvis-text-secondary flex items-center gap-1">
          <Sparkles size={12} className="text-jarvis-purple" />
          AI 点位与风控推荐
        </span>
        <button
          onClick={onRun}
          disabled={loading}
          className="flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-md border border-jarvis-purple/50 text-jarvis-purple hover:bg-jarvis-purple/10 transition-colors disabled:opacity-50"
        >
          {loading ? (
            <>
              <Loader2 size={12} className="animate-spin" />
              分析中...
            </>
          ) : (
            <>
              <Sparkles size={12} />
              {a ? "重新推荐" : "AI 推荐"}
            </>
          )}
        </button>
      </div>

      {a && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            {isWait ? (
              <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded font-medium bg-jarvis-border/40 text-jarvis-text-secondary">
                <PauseCircle size={11} />
                建议观望
              </span>
            ) : (
              <SideBadge side={a.side === "short" ? "short" : "long"} />
            )}
            {riskMeta && (
              <span
                className={clsx(
                  "text-[10px] px-1.5 py-0.5 rounded font-medium",
                  riskMeta.cls,
                )}
              >
                {riskMeta.label}
              </span>
            )}
            <span
              className={clsx(
                "inline-flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded",
                a.source === "llm"
                  ? "bg-jarvis-purple/15 text-jarvis-purple"
                  : "bg-jarvis-border/40 text-jarvis-text-secondary",
              )}
              title={a.note}
            >
              <Cpu size={10} />
              {a.source === "llm" ? "AI 生成" : "本地规则生成"}
            </span>
          </div>

          {a.note && (
            <p className="flex items-start gap-1 text-[10px] text-jarvis-yellow">
              <AlertTriangle size={11} className="flex-shrink-0 mt-0.5" />
              {a.note}
            </p>
          )}

          {!isWait && (
            <div className="grid grid-cols-2 gap-1.5 text-[11px]">
              <AdviceCell
                label="入场区间"
                value={
                  a.entryLow != null && a.entryHigh != null && a.entryHigh !== a.entryLow
                    ? `${formatPrice(a.entryLow)} ~ ${formatPrice(a.entryHigh)}`
                    : formatPrice(a.entryLow ?? a.entryHigh)
                }
              />
              <AdviceCell
                label="止损"
                value={formatPrice(a.stopLoss)}
                highlight={diverges(current.stopLossPrice, a.stopLoss)}
              />
              <AdviceCell
                label="止盈 1 / 2"
                value={`${formatPrice(a.takeProfit1)}${a.takeProfit2 != null ? ` / ${formatPrice(a.takeProfit2)}` : ""}`}
                highlight={diverges(current.takeProfitPrice, a.takeProfit1)}
              />
              <AdviceCell
                label="建议杠杆 · 仓位"
                value={`${a.leverage != null ? `${a.leverage}x` : "—"} · ${a.positionPct != null ? `${a.positionPct}%本金` : "—"}`}
                highlight={
                  diverges(current.leverage, a.leverage) ||
                  diverges(current.positionPct, a.positionPct)
                }
              />
            </div>
          )}

          <p className="text-[11px] text-jarvis-text leading-relaxed bg-jarvis-bg rounded-lg p-2">
            {a.reason}
          </p>

          {!isWait && (
            <button
              onClick={() => onApply(a)}
              className="w-full flex items-center justify-center gap-1.5 py-1.5 text-xs rounded-md border border-jarvis-blue/50 text-jarvis-blue hover:bg-jarvis-blue/10 transition-colors"
            >
              <ArrowDownToLine size={12} />
              一键填入表单（方向/止损/止盈/杠杆/比例）
            </button>
          )}

          <p className="text-[9px] text-jarvis-text-secondary/70 leading-relaxed">
            以上建议由{a.source === "llm" ? "大模型结合所选信号" : "本地规则引擎"}
            生成，仅供参考，不构成投资建议；合约交易风险极高，请自行判断并控制风险。
          </p>
        </div>
      )}
    </div>
  );
}
