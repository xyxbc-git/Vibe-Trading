import { useState } from "react";
import { clsx } from "clsx";
import {
  ChevronDown,
  ChevronRight,
  Eye,
  TrendingUp,
  TrendingDown,
  Minus,
  ShieldCheck,
  ShieldAlert,
  HelpCircle,
  Loader2,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api, type SdBias, type SdEvidence, type SdVerdictResponse } from "@/api/client";

/**
 * 量价核对「主力底牌」卡（威科夫×订单流 P1）。
 *
 * 数据：GET /api/sd-verdict（后端 60s 缓存；本卡 60s 链式轮询，不新增压力）。
 * 布局：bias 徽章 + score 横条 → 突破核验条 → 证据链五行 → 覆盖度/陈旧角标。
 */

const BIAS_META: Record<SdBias, { label: string; cls: string; icon: React.ReactNode }> = {
  accumulation: {
    label: "主力吸筹",
    cls: "bg-jarvis-green/15 text-jarvis-green",
    icon: <TrendingUp size={12} />,
  },
  distribution: {
    label: "主力派发",
    cls: "bg-jarvis-red/15 text-jarvis-red",
    icon: <TrendingDown size={12} />,
  },
  neutral: {
    label: "中性/均衡",
    cls: "bg-jarvis-border/40 text-jarvis-text-secondary",
    icon: <Minus size={12} />,
  },
};

const SOURCE_LABEL: Record<SdEvidence["source"], string> = {
  trap: "陷阱信号",
  cvd: "CVD 吸收",
  whale: "大单净流",
  book: "盘口失衡",
  vp: "价值区",
};

/** score 横条：-1（深红/派发）～ 0 ～ +1（深绿/吸筹） */
function ScoreBar({ score }: { score: number }) {
  const pct = Math.max(-1, Math.min(1, score)) * 50; // 单侧最大 50%
  return (
    <div className="relative w-full h-2 bg-jarvis-bg rounded-full overflow-hidden">
      <div className="absolute left-1/2 top-0 bottom-0 w-px bg-jarvis-border" />
      <div
        className={clsx(
          "absolute top-0 bottom-0 rounded-full transition-all",
          score >= 0 ? "bg-jarvis-green" : "bg-jarvis-red",
        )}
        style={{
          left: score >= 0 ? "50%" : `${50 + pct}%`,
          width: `${Math.abs(pct)}%`,
        }}
      />
    </div>
  );
}

function BreakoutStrip({ data }: { data: NonNullable<SdVerdictResponse["breakout_check"]> }) {
  if (!data.active) return null;
  const meta = {
    confirmed: {
      label: "真突破",
      cls: "border-jarvis-green/40 bg-jarvis-green/10 text-jarvis-green",
      icon: <ShieldCheck size={13} />,
    },
    suspect: {
      label: "假突破嫌疑",
      cls: "border-jarvis-red/40 bg-jarvis-red/10 text-jarvis-red",
      icon: <ShieldAlert size={13} />,
    },
    unknown: {
      label: "待确认",
      cls: "border-jarvis-border bg-jarvis-bg text-jarvis-text-secondary",
      icon: <HelpCircle size={13} />,
    },
  }[data.verdict];
  return (
    <div className={clsx("rounded-lg border p-2 space-y-1", meta.cls)}>
      <div className="flex items-center gap-1.5 text-xs font-medium">
        {meta.icon}
        {data.direction === "up" ? "向上突破" : "向下突破"}核验：{meta.label}
      </div>
      <ul className="space-y-0.5">
        {data.reasons.map((r, i) => (
          <li key={i} className="text-[11px] leading-snug opacity-90">
            · {r}
          </li>
        ))}
      </ul>
    </div>
  );
}

function EvidenceRow({ e }: { e: SdEvidence }) {
  const missing = e.weight === 0;
  return (
    <div
      className={clsx("flex items-start gap-2 rounded-lg p-1.5 bg-jarvis-bg", missing && "opacity-45")}
      title={e.detail}
    >
      <span className="text-[11px] text-jarvis-text font-medium whitespace-nowrap w-14 shrink-0">
        {SOURCE_LABEL[e.source]}
      </span>
      <span
        className={clsx(
          "text-[11px] font-mono shrink-0",
          e.direction > 0 ? "text-jarvis-green" : e.direction < 0 ? "text-jarvis-red" : "text-jarvis-text-secondary",
        )}
      >
        {e.direction > 0 ? "↑" : e.direction < 0 ? "↓" : "·"}
      </span>
      <span className="text-[11px] text-jarvis-text-secondary leading-snug min-w-0">
        {e.detail}
      </span>
    </div>
  );
}

export default function SupplyDemandCard({
  symbol,
  interval,
  pollMs = 60_000,
}: {
  symbol: string;
  interval: string;
  pollMs?: number;
}) {
  const { data, loading, error } = usePolling<SdVerdictResponse>(
    () => api.sdVerdict(symbol, interval),
    pollMs,
    [symbol, interval],
  );
  // 证据链明细折叠开关：bias 结论/score 条是核心信息常驻，五行明细属次级
  // 展示，可收起降噪；默认展开保持既有页面（Chart 侧栏等）观感不变
  const [evidenceOpen, setEvidenceOpen] = useState(true);

  return (
    <div className="card p-3 space-y-2.5 min-w-0">
      <div className="flex items-center justify-between gap-2">
        <p className="stat-label mb-0 flex items-center gap-1.5">
          <Eye size={14} />
          主力底牌（量价核对）
        </p>
        {data?.ok && data.bias && (
          <span
            className={clsx(
              "inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full font-medium whitespace-nowrap",
              BIAS_META[data.bias].cls,
            )}
          >
            {BIAS_META[data.bias].icon}
            {BIAS_META[data.bias].label}
          </span>
        )}
      </div>

      {loading && !data && (
        <div className="flex items-center justify-center py-6 text-jarvis-text-secondary">
          <Loader2 size={16} className="animate-spin mr-2" />
          <span className="text-xs">证据聚合中…</span>
        </div>
      )}

      {!loading && (error || (data && !data.ok)) && (
        <p className="text-xs text-jarvis-red py-2">
          裁决不可用：{error ?? data?.error ?? "未知错误"}
        </p>
      )}

      {data?.ok && (
        <>
          <div className="space-y-1">
            <ScoreBar score={data.score ?? 0} />
            <div className="flex items-center justify-between text-[10px] text-jarvis-text-secondary">
              <span>派发 ← {(data.score ?? 0).toFixed(2)} → 吸筹</span>
              <span>
                置信 {Math.round((data.confidence ?? 0) * 100)}% · 证据覆盖{" "}
                {Math.round((data.coverage ?? 0) * 100)}%
                {data.stale && (
                  <span className="ml-1 text-jarvis-yellow" title="数据层暂不可达，展示上一次成功结果">
                    ·旧值
                  </span>
                )}
              </span>
            </div>
          </div>

          {data.breakout_check && <BreakoutStrip data={data.breakout_check} />}

          {(data.evidence_chain ?? []).length > 0 && (
            <div>
              <button
                type="button"
                onClick={() => setEvidenceOpen((v) => !v)}
                className="flex items-center gap-1 text-[10px] text-jarvis-text-secondary hover:text-jarvis-text transition-colors mb-1"
                title={evidenceOpen ? "收起证据链明细" : "展开证据链明细"}
              >
                {evidenceOpen ? (
                  <ChevronDown size={11} />
                ) : (
                  <ChevronRight size={11} />
                )}
                证据链明细（{(data.evidence_chain ?? []).length} 条）
              </button>
              {/* 宽容器（如盘口页通栏）双列排布，窄容器单列，缓解条目拥挤 */}
              {evidenceOpen && (
                <div className="grid gap-1 md:grid-cols-2">
                  {(data.evidence_chain ?? []).map((e) => (
                    <EvidenceRow key={e.source} e={e} />
                  ))}
                </div>
              )}
            </div>
          )}

          {(data.coverage ?? 0) < 0.5 && (
            <p className="text-[10px] text-jarvis-text-secondary leading-snug">
              部分证据源不可用（如 WS 实时流离线），结论已按覆盖度自动降权。
            </p>
          )}
        </>
      )}
    </div>
  );
}
