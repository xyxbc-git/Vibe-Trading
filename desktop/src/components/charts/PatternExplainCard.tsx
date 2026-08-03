// 形态分析解释卡片（移植自 frontend PatternExplainCard，按 jarvis 样式体系适配）。
// 展示当前选中形态的方向徽标、置信度、中文多空逻辑、突破/目标/止损三价位与
// 关键点位列表；多形态时提供 tab 切换，选中项同时驱动图上标注（由宿主页联动）。

import { clsx } from "clsx";
import { fmtPrice, type DetectedPattern, type PatternDirection } from "@/lib/patterns";

const DIRECTION_META: Record<
  PatternDirection,
  { arrow: string; label: string; badge: string; target: string }
> = {
  bullish: {
    arrow: "▲",
    label: "看涨",
    badge: "bg-jarvis-green/10 border border-jarvis-green/60 text-jarvis-green",
    target: "text-jarvis-green",
  },
  bearish: {
    arrow: "▼",
    label: "看跌",
    badge: "bg-jarvis-red/10 border border-jarvis-red/60 text-jarvis-red",
    target: "text-jarvis-red",
  },
  neutral: {
    arrow: "＝",
    label: "中性·待突破",
    badge: "bg-jarvis-card border border-jarvis-border text-jarvis-text-secondary",
    target: "text-jarvis-text",
  },
};

// 与工具栏「命中率」徽标同一套阈值语义：≥70% 绿 / ≥50% 黄 / 其余灰
const confidenceTone = (c: number) =>
  c >= 0.7 ? "text-jarvis-green" : c >= 0.5 ? "text-jarvis-yellow" : "text-jarvis-text-secondary";
const confidenceBarTone = (c: number) =>
  c >= 0.7 ? "bg-jarvis-green" : c >= 0.5 ? "bg-jarvis-yellow" : "bg-jarvis-text-secondary";

// 引擎的关键点 label 内嵌了价格（"顶1 3,250.00"）——价格列单独展示，去掉重复
const stripTrailingPrice = (label: string) => label.replace(/\s[\d,.]+$/u, "");

// 桌面端 keyPoint.ts 是 unix 秒字符串 → "MM-DD HH:mm"；异常值原样返回
function fmtTs(ts: string): string {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return ts;
  const d = new Date(n * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

interface Props {
  /** 识别到的形态（按置信度降序；空数组 → 空态提示）。 */
  patterns: DetectedPattern[];
  /** 当前图上标注的形态下标。 */
  activeIndex: number;
  /** 用户在 tab 上切换形态时回调（宿主据此重画图上标注）。 */
  onSelect: (index: number) => void;
  className?: string;
}

export default function PatternExplainCard({ patterns, activeIndex, onSelect, className }: Props) {
  if (patterns.length === 0) {
    return (
      <div className={clsx("card p-3", className)}>
        <p className="text-sm text-jarvis-text-secondary">当前区间未发现明显形态</p>
        <p className="mt-1 text-xs text-jarvis-text-secondary/70">
          已扫描：楔形 / 矩形（箱体） / 旗形·三角旗 / 三角形 / 头肩 / 双顶底——可切换周期或等更多 K 线后再试
        </p>
      </div>
    );
  }

  const safeIndex = Math.min(activeIndex, patterns.length - 1);
  const active = patterns[safeIndex];
  const meta = DIRECTION_META[active.direction];
  const confPct = Math.round(active.confidence * 100);

  return (
    <div className={clsx("card p-3 text-xs leading-relaxed", className)}>
      {/* 多形态切换（仅识别到多个时显示） */}
      {patterns.length > 1 && (
        <div
          className="flex flex-wrap gap-1.5 pb-2 mb-2 border-b border-jarvis-border"
          aria-label="识别到的形态列表"
        >
          {patterns.map((p, i) => {
            const m = DIRECTION_META[p.direction];
            const selected = i === safeIndex;
            return (
              <button
                key={`${p.type}-${p.startIndex}`}
                aria-pressed={selected}
                onClick={() => onSelect(i)}
                className={clsx(
                  "px-2 py-0.5 rounded-md border text-xs transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-jarvis-blue",
                  selected
                    ? "bg-jarvis-blue/15 border-jarvis-blue text-jarvis-blue"
                    : "bg-jarvis-card border-jarvis-border text-jarvis-text-secondary hover:text-jarvis-text",
                )}
              >
                {m.arrow} {p.nameCn}
                <span className="ml-1 font-mono opacity-70">{Math.round(p.confidence * 100)}%</span>
              </button>
            );
          })}
        </div>
      )}

      {/* 头部：方向徽标 + 形态名 + 置信度 */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className={clsx("px-1.5 py-0.5 rounded text-xs font-medium", meta.badge)}>
          {meta.arrow} {meta.label}
        </span>
        <span className="text-sm font-medium text-jarvis-text">{active.nameCn}</span>
        <span className="flex items-center gap-1.5" title="按边界触点数量与突破确认情况综合打分">
          <span className={clsx("font-mono", confidenceTone(active.confidence))}>
            置信度 {confPct}%
          </span>
          <span className="h-1 w-16 rounded-full bg-jarvis-border overflow-hidden">
            <span
              className={clsx("block h-full rounded-full", confidenceBarTone(active.confidence))}
              style={{ width: `${confPct}%` }}
            />
          </span>
        </span>
        <span className="text-[10px] text-jarvis-text-secondary/70">已叠加到图上</span>
      </div>

      {/* 中文多空逻辑（含量度目标推导） */}
      <p className="mt-2 text-jarvis-text-secondary">{active.summary}</p>

      {/* 关键价位 */}
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-jarvis-text-secondary">
        <span>
          突破位 <b className="font-semibold text-jarvis-text">{fmtPrice(active.breakout)}</b>
        </span>
        <span>
          量度目标 <b className={clsx("font-semibold", meta.target)}>{fmtPrice(active.target)}</b>
        </span>
        <span>
          建议止损 <b className="font-semibold text-jarvis-yellow">{fmtPrice(active.stop)}</b>
        </span>
      </div>

      {/* 关键点位（label + 时间 + 价格，悬停看说明） */}
      {active.keyPoints.length > 0 && (
        <div className="mt-2 border-t border-jarvis-border pt-2">
          <p className="text-[10px] uppercase tracking-wider text-jarvis-text-secondary/70">
            关键点位
          </p>
          <ul className="mt-1 grid gap-x-6 gap-y-1 sm:grid-cols-2">
            {active.keyPoints.map((kp) => (
              <li
                key={`${kp.index}-${kp.label}`}
                className="flex items-baseline gap-2 min-w-0"
                title={kp.note}
              >
                <span className="text-jarvis-text truncate">{stripTrailingPrice(kp.label)}</span>
                <span className="text-[10px] text-jarvis-text-secondary/70 shrink-0">
                  {fmtTs(kp.ts)}
                </span>
                <span className="ml-auto font-mono text-jarvis-text shrink-0">
                  {fmtPrice(kp.price)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-2 text-[10px] text-jarvis-text-secondary/70">
        形态识别基于历史价格的几何特征，仅供学习参考，不构成投资建议。
      </p>
    </div>
  );
}
