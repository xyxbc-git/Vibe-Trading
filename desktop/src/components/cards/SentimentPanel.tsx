import { clsx } from "clsx";
import {
  Scale,
  TrendingUp,
  TrendingDown,
  Minus,
  AlertTriangle,
  ShieldAlert,
  Loader2,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import { api, type SentimentBias, type SentimentFactor } from "@/api/client";

const BIAS_META: Record<
  SentimentBias,
  { label: string; cls: string; icon: React.ReactNode }
> = {
  bullish: {
    label: "偏多",
    cls: "bg-jarvis-green/15 text-jarvis-green",
    icon: <TrendingUp size={11} />,
  },
  bearish: {
    label: "偏空",
    cls: "bg-jarvis-red/15 text-jarvis-red",
    icon: <TrendingDown size={11} />,
  },
  neutral: {
    label: "中性",
    cls: "bg-jarvis-border/40 text-jarvis-text-secondary",
    icon: <Minus size={11} />,
  },
};

function BiasBadge({ bias }: { bias: SentimentBias }) {
  const m = BIAS_META[bias];
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full font-medium whitespace-nowrap",
        m.cls,
      )}
    >
      {m.icon}
      {m.label}
    </span>
  );
}

/** 综合分横条：-100（深红）～ 0（中点）～ +100（深绿） */
function ScoreBar({ score }: { score: number }) {
  const pct = Math.max(-100, Math.min(100, score));
  const half = Math.abs(pct) / 2; // 从中点向一侧延伸的宽度%
  return (
    <div className="relative w-full h-2.5 bg-jarvis-bg rounded-full overflow-hidden">
      {/* 中线 */}
      <div className="absolute left-1/2 top-0 bottom-0 w-px bg-jarvis-border" />
      <div
        className={clsx(
          "absolute top-0 bottom-0 rounded-full transition-all",
          pct >= 0 ? "bg-jarvis-green" : "bg-jarvis-red",
        )}
        style={{
          left: pct >= 0 ? "50%" : `${50 - half}%`,
          width: `${half}%`,
        }}
      />
    </div>
  );
}

function FactorRow({ f }: { f: SentimentFactor }) {
  return (
    <div
      className={clsx(
        "rounded-lg p-2 bg-jarvis-bg",
        !f.available && "opacity-50",
      )}
      title={f.note}
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="text-[11px] text-jarvis-text font-medium whitespace-nowrap">
          {f.name}
        </span>
        {f.available ? (
          <span className="flex items-center gap-1.5">
            <span
              className={clsx(
                "text-[11px] font-mono",
                f.score > 0
                  ? "text-jarvis-green"
                  : f.score < 0
                    ? "text-jarvis-red"
                    : "text-jarvis-text-secondary",
              )}
            >
              {f.score > 0 ? "+" : ""}
              {f.score.toFixed(0)}
            </span>
            <BiasBadge bias={f.bias} />
          </span>
        ) : (
          <span
            className="text-[10px] px-1.5 py-0.5 rounded border font-medium
                       bg-jarvis-border/30 text-jarvis-text-secondary border-jarvis-border"
          >
            未接入
          </span>
        )}
      </div>
      <p className="text-[10px] text-jarvis-text-secondary font-mono mb-1">
        {f.display}
      </p>
      <p className="text-[10px] text-jarvis-text-secondary leading-relaxed">
        {f.note}
      </p>
    </div>
  );
}

/**
 * 供需情绪综合研判卡：多空比 / 资金费率 / 持仓量 OI / 恐贪指数 四因子量化，
 * 输出 -100～+100 综合分与逐因子解释；爆仓/链上为预留位（key 配置后自动参与）。
 * 极端拥挤时给出止盈止损收紧建议（仅建议展示，不强制改单）。
 */
export default function SentimentPanel({ symbol = "BTCUSDT" }: { symbol?: string }) {
  // 后端 60s 缓存 + 各源独立 TTL，前端 60s 轮询足够
  const { data, loading, error } = usePolling(() => api.sentiment(symbol), 60_000, [symbol]);

  const ok = Boolean(data?.ok);
  const score = data?.score ?? 0;
  const bias = data?.bias ?? "neutral";
  const factors = data?.factors ?? [];
  const warnings = data?.warnings ?? [];

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <p className="stat-label flex items-center gap-2 mb-0">
          <Scale size={14} />
          供需情绪综合研判
          <span className="text-[9px] text-jarvis-text-secondary">
            多空比 · 资金费率 · 持仓量 · 恐贪（BTC 大盘基准）
          </span>
        </p>
        {ok && <BiasBadge bias={bias} />}
      </div>

      {!data && loading ? (
        <div className="h-24 flex items-center justify-center gap-2 text-jarvis-text-secondary text-sm">
          <Loader2 size={15} className="animate-spin" />
          正在计算情绪因子...
        </div>
      ) : !ok ? (
        <div className="py-6 text-center">
          <p className="text-sm text-jarvis-text-secondary">情绪研判暂不可用</p>
          <p className="text-xs text-jarvis-text-secondary/70 mt-1">
            {data?.error ?? error ?? "等待后端 /api/sentiment 就绪后自动恢复"}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {/* ── 综合分 ── */}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[10px] text-jarvis-text-secondary">
                空头拥挤 / 恐慌 ← 综合情绪分 → 多头拥挤 / 贪婪前的顺风
              </span>
              <span
                className={clsx(
                  "text-lg font-mono font-bold",
                  score >= 15
                    ? "text-jarvis-green"
                    : score <= -15
                      ? "text-jarvis-red"
                      : "text-jarvis-text",
                )}
              >
                {score > 0 ? "+" : ""}
                {score.toFixed(0)}
              </span>
            </div>
            <ScoreBar score={score} />
            {data?.headline && (
              <p className="text-[11px] text-jarvis-text mt-2 leading-relaxed">
                {data.headline}
              </p>
            )}
          </div>

          {/* ── 极端警示 ── */}
          {warnings.length > 0 && (
            <div className="space-y-1">
              {warnings.map((w, i) => (
                <p
                  key={i}
                  className="flex items-start gap-1.5 text-[11px] leading-relaxed rounded px-2 py-1 bg-jarvis-yellow/10 text-jarvis-yellow"
                >
                  <AlertTriangle size={12} className="flex-shrink-0 mt-0.5" />
                  <span>{w}</span>
                </p>
              ))}
            </div>
          )}

          {/* ── 止盈止损收紧建议（仅建议，不强制改单） ── */}
          {data?.sl_tp_advice && (
            <p className="flex items-start gap-1.5 text-[11px] leading-relaxed rounded px-2 py-1.5 bg-jarvis-red/10 text-jarvis-red border border-jarvis-red/30">
              <ShieldAlert size={12} className="flex-shrink-0 mt-0.5" />
              <span>{data.sl_tp_advice}</span>
            </p>
          )}

          {/* ── 逐因子明细 ── */}
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {factors.map((f) => (
              <FactorRow key={f.key} f={f} />
            ))}
          </div>

          <p className="text-[9px] text-jarvis-text-secondary/70 leading-relaxed">
            口径：正分=供需面利多（含空头拥挤的轧空燃料），负分=利空（含多头拥挤反噬）。
            情绪层已同步叠加到十二系统共识（同向增益置信、极端背离降级警示），
            与 K 线技术面互为印证而非替代。
          </p>
        </div>
      )}
    </div>
  );
}
