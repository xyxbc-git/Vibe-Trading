import { clsx } from "clsx";
import {
  Compass,
  TrendingUp,
  TrendingDown,
  Minus,
  Loader2,
} from "lucide-react";
import { usePolling } from "@/hooks/useApi";
import {
  api,
  type MarketRegime,
  type RegimeFactor,
  type SentimentBias,
} from "@/api/client";

const REGIME_META: Record<MarketRegime, { emoji: string; cls: string }> = {
  bull: { emoji: "🐂", cls: "bg-jarvis-green/15 text-jarvis-green" },
  bear: { emoji: "🐻", cls: "bg-jarvis-red/15 text-jarvis-red" },
  range: { emoji: "↔", cls: "bg-jarvis-yellow/15 text-jarvis-yellow" },
};

const BIAS_META: Record<
  SentimentBias,
  { label: string; cls: string; icon: React.ReactNode }
> = {
  bullish: {
    label: "偏牛",
    cls: "bg-jarvis-green/15 text-jarvis-green",
    icon: <TrendingUp size={11} />,
  },
  bearish: {
    label: "偏熊",
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

/** 体制分横条：-100（深熊·红）～ 0 ～ +100（牛·绿），中带黄色震荡区标识 */
function RegimeScoreBar({ score }: { score: number }) {
  const pct = Math.max(-100, Math.min(100, score));
  const half = Math.abs(pct) / 2;
  return (
    <div className="relative w-full h-2.5 bg-jarvis-bg rounded-full overflow-hidden">
      {/* 震荡区（±25 分）底色 */}
      <div className="absolute left-[37.5%] w-[25%] top-0 bottom-0 bg-jarvis-yellow/10" />
      <div className="absolute left-1/2 top-0 bottom-0 w-px bg-jarvis-border" />
      <div
        className={clsx(
          "absolute top-0 bottom-0 rounded-full transition-all",
          pct >= 25 ? "bg-jarvis-green" : pct <= -25 ? "bg-jarvis-red" : "bg-jarvis-yellow",
        )}
        style={{
          left: pct >= 0 ? "50%" : `${50 - half}%`,
          width: `${half}%`,
        }}
      />
    </div>
  );
}

function FactorRow({ f }: { f: RegimeFactor }) {
  return (
    <div
      className={clsx("rounded-lg p-2 bg-jarvis-bg", !f.available && "opacity-50")}
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
      <p className="text-[10px] text-jarvis-text-secondary leading-relaxed">{f.note}</p>
    </div>
  );
}

/**
 * 牛熊市体制识别卡：200D MA / 周线结构 / 长周期动量 / 情绪面 多因子融合，
 * 输出 bull/bear/range 三态判定 + 综合分（-100～+100）与逐因子解释；
 * 链上估值（MVRV）为预留位（Glassnode key 配置后自动参与）。
 */
export default function RegimePanel({ symbol = "BTCUSDT" }: { symbol?: string }) {
  // 后端 15min 缓存（大周期数据变化慢），前端 5min 轮询足够
  const { data, loading, error } = usePolling(() => api.regime(symbol), 300_000, [symbol]);

  const ok = Boolean(data?.ok);
  const regime = data?.regime ?? "range";
  const meta = REGIME_META[regime];
  const score = data?.score ?? 0;
  const factors = data?.factors ?? [];

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <p className="stat-label flex items-center gap-2 mb-0">
          <Compass size={14} />
          牛熊市体制识别
          <span className="text-[9px] text-jarvis-text-secondary">
            200D 均线 · 周线结构 · 长周期动量 · 情绪面（大周期口径）
          </span>
        </p>
        {ok && (
          <span
            className={clsx(
              "inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full font-semibold",
              meta.cls,
            )}
          >
            <span>{meta.emoji}</span>
            {data?.regime_cn}
            <span className="font-mono text-[10px] opacity-80">
              置信 {((data?.confidence ?? 0) * 100).toFixed(0)}%
            </span>
          </span>
        )}
      </div>

      {!data && loading ? (
        <div className="h-24 flex items-center justify-center gap-2 text-jarvis-text-secondary text-sm">
          <Loader2 size={15} className="animate-spin" />
          正在判定大周期体制...
        </div>
      ) : !ok ? (
        <div className="py-6 text-center">
          <p className="text-sm text-jarvis-text-secondary">体制判定暂不可用</p>
          <p className="text-xs text-jarvis-text-secondary/70 mt-1">
            {data?.error ?? error ?? "等待后端 /api/regime 就绪后自动恢复"}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {/* ── 综合体制分 ── */}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[10px] text-jarvis-text-secondary">
                熊市 ← -25 · 震荡区 · +25 → 牛市
              </span>
              <span
                className={clsx(
                  "text-lg font-mono font-bold",
                  score >= 25
                    ? "text-jarvis-green"
                    : score <= -25
                      ? "text-jarvis-red"
                      : "text-jarvis-yellow",
                )}
              >
                {score > 0 ? "+" : ""}
                {score.toFixed(0)}
              </span>
            </div>
            <RegimeScoreBar score={score} />
            {data?.headline && (
              <p className="text-[11px] text-jarvis-text mt-2 leading-relaxed">
                {data.headline}
              </p>
            )}
          </div>

          {/* ── 逐因子明细 ── */}
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {factors.map((f) => (
              <FactorRow key={f.key} f={f} />
            ))}
          </div>

          {data?.disclaimer && (
            <p className="text-[9px] text-jarvis-text-secondary/70 leading-relaxed">
              {data.disclaimer}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
