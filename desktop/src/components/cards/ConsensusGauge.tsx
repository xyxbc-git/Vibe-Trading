import { useState } from "react";
import { clsx } from "clsx";
import { Radar, ChevronDown, ChevronUp, Crosshair, AlertTriangle } from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import {
  api,
  formatPrice,
  type ConsensusScope,
  type ConsensusTradePlan,
  type PlanStatus,
  type SignalDirection,
  type TwelveConsensusResponse,
} from "@/api/client";
import GaugeChart from "@/components/common/GaugeChart";
import { planSide, SideBadge, ChaseWarning } from "@/components/cards/SignalBoard";

const DIR_META: Record<
  SignalDirection,
  { label: string; text: string; bg: string; bar: string }
> = {
  bullish: {
    label: "看涨",
    text: "text-jarvis-green",
    bg: "bg-jarvis-green/15",
    bar: "bg-jarvis-green",
  },
  bearish: {
    label: "看跌",
    text: "text-jarvis-red",
    bg: "bg-jarvis-red/15",
    bar: "bg-jarvis-red",
  },
  neutral: {
    label: "中性",
    text: "text-jarvis-text-secondary",
    bg: "bg-jarvis-text-secondary/15",
    bar: "bg-jarvis-text-secondary/60",
  },
};

function normalizeDirection(d: unknown): SignalDirection {
  return d === "bullish" || d === "bearish" ? d : "neutral";
}

/** 大白话共识计划摘要（做多/做空句式对称，消除只看数字的方向歧义） */
function consensusPlanSummary(side: "long" | "short", plan: ConsensusTradePlan): string {
  const zone = `${formatPrice(plan.entry_zone?.[0])}~${formatPrice(plan.entry_zone?.[1])}`;
  const sl = formatPrice(plan.stop_loss);
  const tp = formatPrice(plan.take_profit_1);
  return side === "long"
    ? `看涨 · ${zone} 区间买入，跌破 ${sl} 认输，目标 ${tp}`
    : `看跌 · ${zone} 区间做空，涨破 ${sl} 认输，目标 ${tp}`;
}

/** 共识交易计划区：多空徽章 + 大白话摘要 + 入场区间 / 止损 / TP1/TP2 / 盈亏比 / 建议仓位 */
function TradePlanSection({
  plan,
  planStatus,
  lowConfidence,
  consensusDirection,
  price,
}: {
  plan: ConsensusTradePlan | null | undefined;
  /** 计划状态（后端 v3）：无计划时携带观望原因（RR 不达标等） */
  planStatus?: PlanStatus | null;
  lowConfidence: boolean;
  consensusDirection: SignalDirection;
  /** 当前现价（追高/追空警示用） */
  price?: number | null;
}) {
  const hasTp2 = plan?.take_profit_2 != null;
  const side = plan ? planSide(plan) : null;
  // 计划方向与共识涨跌矛盾（理论不应发生）：黄色示警而非静默展示
  const mismatch =
    side != null &&
    ((consensusDirection === "bullish" && side !== "long") ||
      (consensusDirection === "bearish" && side !== "short"));
  return (
    <div className="mt-3 pt-3 border-t border-jarvis-border/60">
      <p className="text-[10px] text-jarvis-text-secondary mb-1.5 flex items-center gap-1.5 flex-wrap">
        <span className="flex items-center gap-1">
          <Crosshair size={11} />
          交易计划
        </span>
        <SideBadge side={side} />
        {mismatch && (
          <span
            className="inline-flex items-center gap-0.5 text-[9px] px-1 py-px rounded bg-jarvis-yellow/20 text-jarvis-yellow"
            title="计划方向与共识涨跌不一致，疑似数据异常，请勿按此计划操作"
          >
            <AlertTriangle size={9} />
            方向矛盾
          </span>
        )}
        {plan?.source_tf && (
          <span className="text-jarvis-text-secondary/70">
            · 依据 {plan.source_tf}
          </span>
        )}
      </p>
      {!plan ? (
        planStatus?.state === "watch" ? (
          <p className="text-xs text-jarvis-yellow" title="有方向但结构/盈亏比不达标，系统选择观望而不是硬造点位">
            观望：{planStatus.reason || "结构/盈亏比不达标，等待更优入场"}
          </p>
        ) : (
          <p className="text-xs text-jarvis-text-secondary">
            {planStatus?.reason || "当前共识不构成交易计划（中性/分歧）"}
          </p>
        )
      ) : (
        <div className={clsx(lowConfidence && "opacity-60")}>
          {side != null && (
            <p className="text-[10px] text-jarvis-text leading-relaxed bg-jarvis-bg rounded px-1.5 py-1 mb-1.5">
              {consensusPlanSummary(side, plan)}
            </p>
          )}
          <ChaseWarning
            side={side}
            price={price}
            entryLo={plan.entry_zone?.[0]}
            entryHi={plan.entry_zone?.[1]}
          />

          <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px] font-mono">
            <div>
              <p className="text-jarvis-text-secondary text-[9px]">入场区间</p>
              <p className="text-jarvis-blue">
                {formatPrice(plan.entry_zone?.[0])} ~{" "}
                {formatPrice(plan.entry_zone?.[1])}
              </p>
            </div>
            <div>
              <p className="text-jarvis-text-secondary text-[9px]">
                止损{side === "short" ? "（涨破离场）" : side === "long" ? "（跌破离场）" : ""}
              </p>
              <p className="text-jarvis-red">{formatPrice(plan.stop_loss)}</p>
              {plan.sl_basis && (
                <p className="text-[9px] text-jarvis-text-secondary/80 leading-snug" title="止损锚定的结构依据">
                  锚定 {plan.sl_basis}
                </p>
              )}
            </div>
            <div>
              <p className="text-jarvis-text-secondary text-[9px]">
                {hasTp2 ? "止盈 TP1 / TP2" : "止盈 TP1"}
              </p>
              <p className="text-jarvis-green">
                {formatPrice(plan.take_profit_1)}
                {hasTp2 && (
                  <span className="opacity-80">
                    {" "}
                    / {formatPrice(plan.take_profit_2)}
                  </span>
                )}
              </p>
            </div>
            <div>
              <p className="text-jarvis-text-secondary text-[9px]">
                盈亏比 / 建议仓位
              </p>
              <p className="text-jarvis-text">
                {plan.rr != null && Number.isFinite(Number(plan.rr))
                  ? Number(plan.rr).toFixed(1)
                  : "—"}
                {plan.min_rr != null && Number.isFinite(Number(plan.min_rr)) && (
                  <span
                    className="text-jarvis-green text-[9px]"
                    title={`盈亏比门槛 ≥${Number(plan.min_rr).toFixed(1)}：不达标的计划后端直接输出观望，不会硬造`}
                  >
                    {" "}≥{Number(plan.min_rr).toFixed(1)}达标
                  </span>
                )}
                <span className="text-jarvis-text-secondary"> · </span>
                {lowConfidence ? (
                  <span className="text-jarvis-yellow">低置信，仅供参考</span>
                ) : plan.position_pct != null ? (
                  `${plan.position_pct}%仓`
                ) : (
                  "—"
                )}
              </p>
            </div>
          </div>
          {plan.note && (
            <p className="text-[10px] text-jarvis-text-secondary mt-1.5 leading-relaxed">
              {plan.note}
            </p>
          )}
          {(plan.basis?.length ?? 0) > 0 && (
            <p className="text-[9px] text-jarvis-text-secondary/70 mt-1.5">
              依据：{plan.basis!.join(" · ")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** 带口径标记的共识响应：用于甄别「切换周期后旧口径数据回写」 */
type ScopedConsensusResp = TwelveConsensusResponse & { _scope: ConsensusScope };

interface ConsensusGaugeProps {
  symbol: string;
  /** 共识口径（受控）："auto" = 多周期综合；具体周期 = 单 TF 12 系统共识 */
  tf: ConsensusScope;
}

/** 贾维斯共识仪表盘：方向 + 置信度 + 12 系统投票分布 */
export default function ConsensusGauge({ symbol, tf }: ConsensusGaugeProps) {
  const { data: resp, loading, error } = usePolling(
    async (): Promise<ScopedConsensusResp> => {
      if (tf === "auto") {
        // 多周期综合：/api/twelve/consensus
        const r = await api.twelveConsensus(symbol);
        return { ...r, _scope: "auto" };
      }
      // 单周期：复用 /api/twelve/signals 响应内的 12 系统共识（含 trade_plan）
      const r = await api.twelveSignals(symbol, tf);
      return {
        ok: r.ok,
        symbol: r.symbol,
        price: r.price ?? null,
        tf_available: r.tf ? [r.tf] : [],
        consensus: r.consensus ?? null,
        error: r.error,
        _scope: tf,
      };
    },
    60_000,
    [symbol, tf],
  );
  const [showReason, setShowReason] = useState(false);

  // 防旧数据回写：口径（_scope）或币种（服务端回声 symbol）与当前不一致均视为过期
  const stale =
    resp != null &&
    (resp._scope !== tf || (resp.symbol != null && resp.symbol !== symbol));
  // 封套：{ok, symbol, price, tf_available, consensus:{...}}；ok:false 视同引擎未就绪
  const data = !stale && resp?.ok ? resp.consensus : null;
  const failed = Boolean(error) || (!stale && resp != null && !resp.ok);

  const direction = normalizeDirection(data?.direction);
  const meta = DIR_META[direction];
  const confidence = Math.max(0, Math.min(1, Number(data?.confidence ?? 0)));
  const lowConfidence = confidence < 0.5;
  // 仪表盘映射：看涨为正、看跌为负，幅度 = 置信度
  const gaugeValue =
    direction === "bullish"
      ? confidence
      : direction === "bearish"
        ? -confidence
        : 0;

  const votes = data?.votes ?? { bullish: 0, bearish: 0, neutral: 0 };
  const voteTotal =
    Number(votes.bullish ?? 0) +
    Number(votes.bearish ?? 0) +
    Number(votes.neutral ?? 0);
  const votePct = (n: number) => (voteTotal > 0 ? (n / voteTotal) * 100 : 0);

  const keyLevels = (data?.key_levels ?? []).slice(0, 4);

  return (
    <div className="card flex flex-col">
      <div className="flex items-center justify-between mb-3">
        <p className="stat-label flex items-center gap-2 mb-0">
          <Radar size={14} />
          贾维斯共识 · {symbol}
          {/* 当前口径徽章：与信号矩阵周期联动 */}
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-jarvis-blue/10 text-jarvis-blue font-mono">
            {tf === "auto" ? "综合" : tf}
          </span>
        </p>
        {data && (
          <span
            className={clsx(
              "text-xs px-2 py-0.5 rounded-full font-medium",
              meta.bg,
              meta.text,
              lowConfidence && "opacity-60",
            )}
          >
            {meta.label}
          </span>
        )}
      </div>

      {failed ? (
        <div className="flex-1 flex flex-col items-center justify-center py-6 gap-1">
          <p className="text-sm text-jarvis-text-secondary">信号引擎未启动</p>
          <p className="text-xs text-jarvis-text-secondary/70">
            {!stale && resp && !resp.ok && resp.error
              ? String(resp.error)
              : "等待后端 /api/twelve/consensus 就绪后自动恢复"}
          </p>
        </div>
      ) : (loading && !data) || stale ? (
        <div className="flex-1 flex flex-col gap-3 py-4 animate-pulse">
          <div className="h-16 rounded-lg bg-jarvis-border/40 mx-auto w-32" />
          <div className="h-2 rounded-full bg-jarvis-border/40" />
          <div className="h-3 rounded bg-jarvis-border/40 w-2/3" />
        </div>
      ) : !data ? (
        <p className="text-sm text-jarvis-text-secondary py-6 text-center">
          暂无共识数据
        </p>
      ) : (
        <>
          <div
            className={clsx(
              "flex flex-col items-center",
              lowConfidence && "opacity-60",
            )}
          >
            <GaugeChart
              value={gaugeValue}
              label="多空共识"
              size={130}
            />
            <p className="text-xs mt-1">
              <span className={meta.text}>
                {meta.label} · 置信度 {(confidence * 100).toFixed(0)}%
              </span>
              {lowConfidence && (
                <span className="text-jarvis-text-secondary ml-1.5">
                  （低置信，仅供参考）
                </span>
              )}
            </p>
          </div>

          {/* 「安全带」确认层：Delta 吸收背离 × 信号方向（confirmed/no-evidence/conflict） */}
          {data.seatbelt && data.seatbelt.status !== "unavailable" && data.seatbelt.status !== "idle" && (
            <p
              title={`${data.seatbelt.note}${data.seatbelt.divergence_note ? `\n背离依据：${data.seatbelt.divergence_note}` : ""}`}
              className={clsx(
                "mt-2 flex items-start gap-1.5 text-[11px] leading-relaxed rounded px-2 py-1",
                data.seatbelt.status === "confirmed"
                  ? "bg-jarvis-green/10 text-jarvis-green"
                  : data.seatbelt.status === "conflict"
                    ? "bg-jarvis-red/15 text-jarvis-red font-medium"
                    : "bg-jarvis-yellow/10 text-jarvis-yellow",
              )}
            >
              <span className="flex-shrink-0">
                {data.seatbelt.status === "confirmed" ? "🛡️" : data.seatbelt.status === "conflict" ? "🔴" : "⚠️"}
              </span>
              <span>
                安全带 · {data.seatbelt.status_cn}
                {data.seatbelt.grade ? `（${data.seatbelt.grade}）` : ""}
                ：{data.seatbelt.note}
                {data.seatbelt.confidence_delta !== 0 && (
                  <span className="font-mono ml-1">
                    置信度 {data.seatbelt.confidence_delta > 0 ? "+" : ""}
                    {(data.seatbelt.confidence_delta * 100).toFixed(0)}% → {(data.seatbelt.adjusted_confidence * 100).toFixed(0)}%
                  </span>
                )}
              </span>
            </p>
          )}

          {/* 12 系统投票分布（votes 和 = 12；TF 级投票见 tf_votes） */}
          <div className="mt-3">
            <p className="text-[10px] text-jarvis-text-secondary mb-1">
              12 系统投票分布
            </p>
            <div className="flex h-2 rounded-full overflow-hidden bg-jarvis-bg">
              {voteTotal > 0 && (
                <>
                  <div
                    className="bg-jarvis-green"
                    style={{ width: `${votePct(Number(votes.bullish ?? 0))}%` }}
                  />
                  <div
                    className="bg-jarvis-text-secondary/40"
                    style={{ width: `${votePct(Number(votes.neutral ?? 0))}%` }}
                  />
                  <div
                    className="bg-jarvis-red"
                    style={{ width: `${votePct(Number(votes.bearish ?? 0))}%` }}
                  />
                </>
              )}
            </div>
            <div className="flex justify-between mt-1.5 text-xs font-mono">
              <span className="text-jarvis-green">看涨 {votes.bullish ?? 0}</span>
              <span className="text-jarvis-text-secondary">
                中性 {votes.neutral ?? 0}
              </span>
              <span className="text-jarvis-red">看跌 {votes.bearish ?? 0}</span>
            </div>
          </div>

          {/* 交易计划：多空徽章 + 大白话摘要 + 追高警示 + 入场区间/止损/止盈/盈亏比/建议仓位 */}
          <TradePlanSection
            plan={data.trade_plan}
            planStatus={data.plan_status}
            lowConfidence={lowConfidence}
            consensusDirection={direction}
            price={!stale && resp?.ok ? resp.price : null}
          />

          {/* 关键价位 */}
          {keyLevels.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-3">
              {keyLevels.map((lv, i) => (
                <span
                  key={`${lv.label}-${i}`}
                  className="text-xs px-2 py-0.5 rounded bg-jarvis-bg border border-jarvis-border text-jarvis-text-secondary font-mono"
                >
                  {lv.label}{" "}
                  <span className="text-jarvis-text">
                    {Number(lv.price).toLocaleString()}
                  </span>
                </span>
              ))}
            </div>
          )}

          {/* 共识理由（可折叠） */}
          {data.reasoning && (
            <div className="mt-3 pt-3 border-t border-jarvis-border/60">
              <button
                onClick={() => setShowReason((v) => !v)}
                className="flex items-center gap-1 text-xs text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
              >
                {showReason ? (
                  <ChevronUp size={12} />
                ) : (
                  <ChevronDown size={12} />
                )}
                共识理由
              </button>
              <p
                className={clsx(
                  "text-xs text-jarvis-text-secondary mt-1.5 leading-relaxed",
                  !showReason && "line-clamp-2",
                )}
              >
                {data.reasoning}
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
