// PredictionCard — K 线走势预测研判卡片（图表上方横条，风格对齐信号盈损状态条）：
//   折叠行  方向徽章（↑看涨/↓看跌/→震荡）+ 三档概率 + 信心条 + 演示数据角标
//   展开区  AI 研判理由（rationale）全文 + 依据信号 chips + disclaimer
// 状态覆盖：loading（骨架行）/ error（原因 + 重试）/ ok；mock:true 强制角标。
// 纯展示组件：取数、开关、轮询全部由 Chart.tsx 持有。

import { useState } from "react";
import { clsx } from "clsx";
import {
  Sparkles,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  TrendingUp,
  TrendingDown,
  MoveRight,
} from "lucide-react";
import type { PredictResponse } from "@/lib/predict";
import { normalizeProbability } from "@/lib/predict";

interface PredictionCardProps {
  resp: PredictResponse | null;
  loading: boolean;
  /** 请求失败原因；非空时展示失败态（resp 可能是上一份成功结果，失败态优先） */
  error: string | null;
  onRetry: () => void;
}

const DIR_META = {
  up: { label: "看涨", icon: TrendingUp, cls: "text-jarvis-green border-jarvis-green" },
  down: { label: "看跌", icon: TrendingDown, cls: "text-jarvis-red border-jarvis-red" },
  sideways: { label: "震荡", icon: MoveRight, cls: "text-jarvis-purple border-jarvis-purple" },
} as const;

export default function PredictionCard({ resp, loading, error, onRetry }: PredictionCardProps) {
  const [open, setOpen] = useState(false);

  return (
    <div className="bg-jarvis-card border border-jarvis-purple/40 rounded-lg px-3 py-2 text-xs">
      <div className="flex items-center gap-2 flex-wrap">
        <Sparkles size={13} className="text-jarvis-purple shrink-0" />
        <span className="text-jarvis-text font-medium">AI 走势研判</span>

        {loading && !resp && (
          <span className="flex items-center gap-1.5 text-jarvis-text-secondary">
            <span className="w-3 h-3 border border-jarvis-purple border-t-transparent rounded-full animate-spin" />
            正在生成预测…
          </span>
        )}

        {error && !loading && (
          <>
            <span className="text-jarvis-yellow" title={error}>
              预测获取失败：{error}
            </span>
            <button
              onClick={onRetry}
              className="flex items-center gap-1 px-1.5 py-0.5 rounded border border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
            >
              <RefreshCw size={11} />
              重试
            </button>
          </>
        )}

        {resp && !error && (
          <>
            {(() => {
              const meta = DIR_META[resp.direction] ?? DIR_META.sideways;
              const Icon = meta.icon;
              return (
                <span
                  className={clsx(
                    "flex items-center gap-1 px-1.5 py-px rounded border font-medium",
                    meta.cls,
                  )}
                >
                  <Icon size={11} />
                  {meta.label}
                </span>
              );
            })()}

            {(() => {
              const p = normalizeProbability(resp.probability);
              const pct = (v: number) => `${Math.round(v * 100)}%`;
              return (
                <span className="font-mono text-jarvis-text-secondary">
                  <span className="text-jarvis-green">↑ {pct(p.up)}</span>
                  {" · "}
                  <span>→ {pct(p.sideways)}</span>
                  {" · "}
                  <span className="text-jarvis-red">↓ {pct(p.down)}</span>
                </span>
              );
            })()}

            <span className="flex items-center gap-1.5 text-jarvis-text-secondary">
              信心
              <span className="inline-block w-16 h-1 rounded bg-jarvis-border overflow-hidden align-middle">
                <span
                  className={clsx(
                    "block h-full rounded",
                    resp.confidence >= 0.65
                      ? "bg-jarvis-green"
                      : resp.confidence >= 0.45
                        ? "bg-jarvis-yellow"
                        : "bg-jarvis-text-secondary",
                  )}
                  style={{ width: `${Math.round(Math.max(0, Math.min(1, resp.confidence)) * 100)}%` }}
                />
              </span>
              <span className="font-mono">{Math.round(resp.confidence * 100)}%</span>
            </span>

            <span className="font-mono text-jarvis-text-secondary">
              未来 {resp.horizon} 根 · 目标{" "}
              {resp.targetZone.low.toLocaleString("en-US", { maximumFractionDigits: 2 })} –{" "}
              {resp.targetZone.high.toLocaleString("en-US", { maximumFractionDigits: 2 })}
            </span>

            {resp.mock && (
              <span
                className="px-1.5 py-px rounded bg-jarvis-yellow/15 border border-jarvis-yellow/50 text-jarvis-yellow text-[10px] font-medium"
                title="预测引擎未接入，当前为本地演示推演结果"
              >
                演示数据
              </span>
            )}

            {loading && (
              <span
                className="w-3 h-3 border border-jarvis-purple border-t-transparent rounded-full animate-spin"
                title="正在刷新预测"
              />
            )}
          </>
        )}

        {resp && !error && (
          <button
            onClick={() => setOpen((v) => !v)}
            className="ml-auto flex items-center gap-0.5 px-1.5 py-0.5 rounded text-jarvis-text-secondary hover:text-jarvis-text transition-colors"
          >
            {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            {open ? "收起" : "研判详情"}
          </button>
        )}
      </div>

      {open && resp && !error && (
        <div className="mt-2 pt-2 border-t border-jarvis-border space-y-2">
          <p className="text-jarvis-text leading-relaxed">{resp.rationale || "（无研判理由）"}</p>
          {resp.signals.length > 0 && (
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-jarvis-text-secondary">依据信号</span>
              {resp.signals.map((s) => (
                <span
                  key={s}
                  className="px-1.5 py-px rounded bg-jarvis-purple/10 border border-jarvis-purple/40 text-jarvis-purple font-mono text-[10px]"
                >
                  {s}
                </span>
              ))}
            </div>
          )}
          {resp.disclaimer && (
            <p className="text-[10px] text-jarvis-text-secondary">{resp.disclaimer}</p>
          )}
        </div>
      )}
    </div>
  );
}
