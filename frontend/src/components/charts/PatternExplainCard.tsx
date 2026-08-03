// Explanation card for classic chart patterns detected by lib/patterns.ts.
// Sits under the candlestick chart: shows the active pattern's direction,
// confidence, key levels (突破/目标/止损) and key points in plain Chinese,
// with a tab strip to switch between several detected patterns. The selected
// tab drives which pattern the host chart annotates.

import { cn } from "@/lib/utils";
import { fmtPrice, type DetectedPattern, type PatternDirection } from "@/lib/patterns";

const DIRECTION_META: Record<PatternDirection, { arrow: string; label: string; badge: string; target: string }> = {
  bullish: { arrow: "▲", label: "看涨", badge: "bg-success/15 text-success", target: "text-success" },
  bearish: { arrow: "▼", label: "看跌", badge: "bg-danger/15 text-danger", target: "text-danger" },
  neutral: { arrow: "＝", label: "中性·待突破", badge: "bg-muted/40 text-muted-foreground", target: "text-foreground" },
};

// Same tiers as the toolbar hit-rate badge, so "green means trustworthy"
// reads consistently across the chart UI.
const confidenceTone = (c: number) =>
  c >= 0.7 ? "text-success" : c >= 0.5 ? "text-warning" : "text-muted-foreground/60";
const confidenceBarTone = (c: number) =>
  c >= 0.7 ? "bg-success" : c >= 0.5 ? "bg-warning" : "bg-muted-foreground/50";

// Key-point labels from lib/patterns embed the formatted price ("顶1 3,250.00");
// strip that trailing number so the dedicated price column doesn't repeat it.
const stripTrailingPrice = (label: string) => label.replace(/\s[\d,.]+$/u, "");

interface Props {
  /** Detected patterns, sorted by confidence (may be empty → empty state). */
  patterns: DetectedPattern[];
  /** Index of the pattern currently annotated on the chart. */
  activeIndex: number;
  /** Invoked when the user switches pattern via the tab strip. */
  onSelect: (index: number) => void;
  className?: string;
}

export function PatternExplainCard({ patterns, activeIndex, onSelect, className }: Props) {
  if (patterns.length === 0) {
    return (
      <div className={cn("rounded-lg border border-border/40 px-3 py-2.5", className)}>
        <p className="text-[11px] text-muted-foreground">当前区间未发现明显形态</p>
        <p className="mt-0.5 text-[10px] text-muted-foreground/60">
          已扫描：楔形 / 矩形（箱体） / 旗形·三角旗 / 三角形 / 头肩 / 双顶底——可切换时间范围或等更多 K 线后再试
        </p>
      </div>
    );
  }

  const safeIndex = Math.min(activeIndex, patterns.length - 1);
  const active = patterns[safeIndex];
  const meta = DIRECTION_META[active.direction];
  const confPct = Math.round(active.confidence * 100);

  return (
    <div className={cn("rounded-lg border border-primary/30 bg-primary/5 p-2 text-[11px] leading-relaxed", className)}>
      {/* Pattern switcher — only rendered when several patterns coexist */}
      {patterns.length > 1 && (
        <div className="flex flex-wrap gap-1 pb-1.5 mb-1.5 border-b border-border/40" aria-label="识别到的形态列表">
          {patterns.map((p, i) => {
            const m = DIRECTION_META[p.direction];
            const selected = i === safeIndex;
            return (
              <button
                key={`${p.type}-${p.startIndex}`}
                aria-pressed={selected}
                onClick={() => onSelect(i)}
                className={cn(
                  "px-1.5 py-0.5 rounded border text-[10px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary",
                  selected
                    ? "border-primary/40 bg-primary/15 text-primary font-medium"
                    : "border-border/40 text-muted-foreground/70 hover:text-foreground hover:bg-muted/30",
                )}
              >
                {m.arrow} {p.nameCn}
                <span className="ml-1 font-mono opacity-70">{Math.round(p.confidence * 100)}%</span>
              </button>
            );
          })}
        </div>
      )}

      {/* Header: direction badge + name + confidence meter */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className={cn("px-1.5 py-0.5 rounded font-medium", meta.badge)}>
          {meta.arrow} {meta.label}
        </span>
        <span className="font-medium text-foreground">{active.nameCn}</span>
        <span className="flex items-center gap-1.5" title="按边界触点数量与突破确认情况综合打分">
          <span className={cn("font-mono text-[10px]", confidenceTone(active.confidence))}>置信度 {confPct}%</span>
          <span className="h-1 w-14 rounded-full bg-muted/40 overflow-hidden">
            <span
              className={cn("block h-full rounded-full transition-[width]", confidenceBarTone(active.confidence))}
              style={{ width: `${confPct}%` }}
            />
          </span>
        </span>
        <span className="text-[9px] text-muted-foreground/60">已叠加到图上</span>
      </div>

      {/* Plain-language bull/bear logic incl. measured target reasoning */}
      <p className="mt-1.5 text-muted-foreground">{active.summary}</p>

      {/* Key levels */}
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-[10px] text-muted-foreground/70">
        <span>
          突破位 <b className="font-semibold text-foreground">{fmtPrice(active.breakout)}</b>
        </span>
        <span>
          量度目标 <b className={cn("font-semibold", meta.target)}>{fmtPrice(active.target)}</b>
        </span>
        <span>
          建议止损 <b className="font-semibold text-warning">{fmtPrice(active.stop)}</b>
        </span>
      </div>

      {/* Key points (label + date + price; hover for the why) */}
      {active.keyPoints.length > 0 && (
        <div className="mt-1.5 border-t border-border/40 pt-1.5">
          <p className="text-[9px] uppercase tracking-wider text-muted-foreground/50">关键点位</p>
          <ul className="mt-0.5 grid gap-x-4 gap-y-0.5 sm:grid-cols-2">
            {active.keyPoints.map(kp => (
              <li key={`${kp.index}-${kp.label}`} className="flex items-baseline gap-1.5 min-w-0" title={kp.note}>
                <span className="text-muted-foreground truncate">{stripTrailingPrice(kp.label)}</span>
                <span className="text-[9px] text-muted-foreground/50 shrink-0">{kp.ts}</span>
                <span className="ml-auto font-mono text-foreground shrink-0">{fmtPrice(kp.price)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-1.5 text-[9px] text-muted-foreground/50">
        形态识别基于历史价格的几何特征，仅供学习参考，不构成投资建议。
      </p>
    </div>
  );
}
