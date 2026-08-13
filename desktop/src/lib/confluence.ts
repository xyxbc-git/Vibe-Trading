// 合流仪表纯函数层（C2）：分段色 / 分数滞回 / 折叠态四勾 / 冷静期倒计时 / 方向元数据。
// 全部无副作用，离线可测（__tests__/confluence.test.ts）。
//
// 防噪纪律（方案 §六）：
//   · 分数滞回——变化 <5 分不更新显示值，防 30-60s 缓存刷新心电图效应
//   · 分段色 ≥70 金 / 40-69 蓝 / <40 灰，不用红（红在本项目 tone 体系=空方向/风险）
//   · 证据不足（可用权重<50）/ 方向中性 / 演示数据 → 灰显

import type {
  ConfluenceDirection,
  ConfluenceItem,
  ConfluenceResponse,
  ConfluenceState,
} from "@/api/confluence";

/** 分数滞回阈值：显示值与新值差距小于该值时不重绘 */
export const SCORE_HYSTERESIS = 5;

/** 折叠态四勾对应的条目键（CP1 对齐四大项：HTF/BOS/扫单/FVG 折溢价） */
export const COLLAPSED_CHECK_KEYS = ["c1_htf", "c3_bos", "c4_sweep", "c5_fvg"] as const;

/** 分数滞回：|next-prev| < 阈值时保持旧显示值；null（中性/无数据）直通。 */
export function holdDisplayScore(
  prev: number | null,
  next: number | null,
  hysteresis: number = SCORE_HYSTERESIS,
): number | null {
  if (next == null) return null;
  if (prev == null) return next;
  return Math.abs(next - prev) < hysteresis ? prev : next;
}

export interface ScoreTone {
  /** 语义档：gold ≥70 / blue 40-69 / gray <40 或灰显 */
  band: "gold" | "blue" | "gray";
  /** 文本色类（Tailwind） */
  textCls: string;
  /** 分数是否灰显（证据不足/中性/演示） */
  dimmed: boolean;
}

/** 分段色（对齐 ReversalScorePanel 惯例：金/蓝/灰，不用红）。 */
export function scoreTone(
  score: number | null,
  opts: { insufficient?: boolean; mock?: boolean } = {},
): ScoreTone {
  const dimmed = score == null || Boolean(opts.insufficient) || Boolean(opts.mock);
  if (score == null || score < 40) {
    return { band: "gray", textCls: "text-jarvis-text-secondary", dimmed };
  }
  if (score >= 70) {
    return {
      band: "gold",
      textCls: dimmed ? "text-jarvis-yellow/50" : "text-jarvis-yellow",
      dimmed,
    };
  }
  return {
    band: "blue",
    textCls: dimmed ? "text-jarvis-blue/50" : "text-jarvis-blue",
    dimmed,
  };
}

export interface DirMeta {
  label: string;
  /** ▲ / ▼ / ＝ */
  arrow: string;
  textCls: string;
  borderCls: string;
}

/** 方向徽章元数据：措辞用「环境偏多/偏空」，绝不用 BUY/SELL（方案 §五.4）。 */
export function dirMeta(direction: ConfluenceDirection): DirMeta {
  if (direction === "bullish") {
    return {
      label: "环境偏多",
      arrow: "▲",
      textCls: "text-jarvis-green",
      borderCls: "border-jarvis-green/50",
    };
  }
  if (direction === "bearish") {
    return {
      label: "环境偏空",
      arrow: "▼",
      textCls: "text-jarvis-red",
      borderCls: "border-jarvis-red/50",
    };
  }
  return {
    label: "无方向共识",
    arrow: "＝",
    textCls: "text-jarvis-text-secondary",
    borderCls: "border-jarvis-border",
  };
}

/** 折叠态四勾：按 COLLAPSED_CHECK_KEYS 从各组条目中抽取；缺失按 skipped 兜底。 */
export function collapsedChecks(resp: ConfluenceResponse | null): ConfluenceState[] {
  const all = new Map<string, ConfluenceItem>();
  for (const g of resp?.groups ?? []) {
    for (const it of g.items) all.set(it.key, it);
  }
  return COLLAPSED_CHECK_KEYS.map((k) => all.get(k)?.state ?? "skipped");
}

/** 冷静期剩余秒数（未设置/已过期 → 0）。 */
export function cooldownRemaining(
  cooldownUntil: number | null | undefined,
  nowSec: number = Date.now() / 1000,
): number {
  if (cooldownUntil == null) return 0;
  return Math.max(0, Math.floor(cooldownUntil - nowSec));
}

/** 秒 → "m:ss"（≥1h 显示 "h:mm:ss"）。 */
export function fmtCountdown(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
  return `${m}:${String(r).padStart(2, "0")}`;
}

/** 数据新鲜度：updatedAt（epoch 秒）→「xx 秒前 / x 分钟前」；缺失返回空串。 */
export function fmtFreshness(
  updatedAt: number | null | undefined,
  nowSec: number = Date.now() / 1000,
): string {
  if (updatedAt == null || !Number.isFinite(updatedAt)) return "";
  const d = Math.max(0, Math.floor(nowSec - updatedAt));
  if (d < 60) return `${d} 秒前`;
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  return `${Math.floor(d / 3600)} 小时前`;
}

/** 窄容器判定（方案 §六：按 container 宽度非 viewport）。 */
export const NARROW_CONTAINER_PX = 768;

export function isNarrowContainer(widthPx: number | null | undefined): boolean {
  return widthPx != null && widthPx > 0 && widthPx < NARROW_CONTAINER_PX;
}

/** 旧响应守卫：慢响应的旧币种/旧周期数据不得覆盖当前口径（同 ReversalScorePanel 惯例）。 */
export function matchesScope(
  resp: ConfluenceResponse | null,
  symbol: string,
  tf: string,
): boolean {
  if (!resp) return false;
  if (resp.symbol != null && resp.symbol !== symbol) return false;
  if (resp.tf != null && resp.tf !== tf) return false;
  return true;
}
