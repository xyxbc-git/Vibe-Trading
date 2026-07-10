// K 线图「分层视图」组合器：把 交易计划 / 智能支撑压力 / 自动画线 / 十二套
// 关键位 按三档视图模式（简洁/进阶/专业）做预算截断与大白话改写，输出图表
// 可直接渲染的载荷 + 当前可见线型的图例说明。
//
// 全部纯函数，Chart.tsx 只负责取数与状态，组合逻辑在这里单测锁定。
//
// 三档语义：
//   simple   简洁（默认）：≤6 条线——交易计划（入场区/止损/止盈，TP2 省略）
//            + 最近强支撑 + 最近强压力；label 全部大白话
//   advanced 进阶：简洁核心 + 自动画线（每类只取最可靠的 1~2 条）+ 十二套
//            关键位（强度≥0.6 且相近价位去重）；总预算 ≤12 条
//   pro      专业：全量现状，细粒度开关组只在此模式显示

import type {
  DrawHLine,
  DrawSegment,
  DrawBand,
  DrawingResult,
  SmartLevels,
  KeyLevel,
  DrawMode,
} from "./drawings";
import type { ConsensusTradePlan, TwelveSignal } from "../api/client";
import { planToOverlay, PLAN_COLORS, fmt } from "./tradePlan";
import { planToZones, type TradeZone } from "./tradeZones";

export type ViewMode = "simple" | "advanced" | "pro";

export const VIEW_MODES: { id: ViewMode; label: string; hint: string }[] = [
  { id: "simple", label: "简洁", hint: "只画买卖计划和最重要的支撑压力，一眼看懂" },
  { id: "advanced", label: "进阶", hint: "简洁 + 最可靠的自动画线和强信号关键位（≤12 条）" },
  { id: "pro", label: "专业", hint: "全量线型 + 细粒度开关，适合熟手" },
];

export const SIMPLE_BUDGET = 6;
export const ADVANCED_BUDGET = 12;

// 简洁/进阶模式的智能支撑压力配色（虚线细线，与交易计划实线区分）
export const SMART_LINE_COLORS = { support: "#3fb950", resistance: "#f85149" } as const;

// ── 大白话 label ────────────────────────────────────────────────────────

// emoji 前缀集中定义；若目标环境 canvas 渲染 emoji 异常，置空即可全线退回纯文字
const MARK = { buy: "✅ ", stop: "🛑 ", target: "🎯 " };

// 交易计划线 → 大白话改写（方向感知：空单入场区/止损文案不同）。
// 线型判定优先用结构化 kind，缺失时回退颜色（兼容无 kind 的旧数据）。
export function relabelPlanLinesPlain(hlines: DrawHLine[], direction: string): DrawHLine[] {
  const short = direction === "bearish";
  return hlines.map((l) => {
    if (l.kind === "entry" || (!l.kind && l.color === PLAN_COLORS.entry)) {
      return { ...l, label: `${MARK.buy}${short ? "建议做空区" : "建议买入区"} ${fmt(l.price)}` };
    }
    if (l.kind === "sl" || (!l.kind && l.color === PLAN_COLORS.stop)) {
      return { ...l, label: `${MARK.stop}止损 ${fmt(l.price)} ${short ? "涨破就跑" : "跌破就跑"}` };
    }
    return { ...l, label: `${MARK.target}止盈目标 ${fmt(l.price)}` };
  });
}

// TP2 判定：kind 优先，label 回退（避免文案改动静默失效）
export function isTp2Line(l: DrawHLine): boolean {
  if (l.kind) return l.kind === "tp2";
  return (l.label ?? "").includes("TP2");
}

// 回归中轴判定：kind 优先，label 回退
export function isChannelMid(s: DrawSegment): boolean {
  if (s.kind) return s.kind === "channel-mid";
  return (s.label ?? "").includes("回归中轴");
}

// 后端回声校验：响应里带的 symbol 与当前请求的 symbol 不一致 → 陈旧响应，
// 必须丢弃（快速切币种时旧币种的计划线不能画到新币种图上）。
// 响应未带 symbol（旧后端）视为匹配，不误伤。
export function isStaleEcho(requested: string, echoed?: string | null): boolean {
  if (!echoed) return false;
  const norm = (s: string) => s.toUpperCase().replace(/[-/]/g, "");
  return norm(echoed) !== norm(requested);
}

// 智能支撑/压力 → 大白话水平线（虚线细线，与计划实线区分）
export function smartToPlainLines(smart: SmartLevels | null): DrawHLine[] {
  const out: DrawHLine[] = [];
  if (smart?.support) {
    out.push({
      price: smart.support.level,
      color: SMART_LINE_COLORS.support,
      width: 1,
      style: "dashed",
      label: `强支撑 ${fmt(smart.support.level)} 碰到容易反弹`,
    });
  }
  if (smart?.resistance) {
    out.push({
      price: smart.resistance.level,
      color: SMART_LINE_COLORS.resistance,
      width: 1,
      style: "dashed",
      label: `强压力 ${fmt(smart.resistance.level)} 碰到容易回落`,
    });
  }
  return out;
}

// ── 十二套关键位过滤 / 去重 ────────────────────────────────────────────

export function twelveColor(direction?: string): string {
  const d = (direction ?? "").toLowerCase();
  if (d.includes("long") || d.includes("bull") || d.includes("多")) return "#3fb950";
  if (d.includes("short") || d.includes("bear") || d.includes("空")) return "#f85149";
  return "#8b949e";
}

export interface KeyLevelFilter {
  minStrength?: number; // 只保留信号强度 ≥ 此值的系统输出（默认 0 = 不过滤）
  dedupPct?: number;    // 相近价位合并阈值（价差比例，默认 0 = 不去重），保留强度高者
  maxCount?: number;    // 预算截断：按 strength 降序保留前 N 条（丢弱保强），再按价格升序返回
}

export function signalsToKeyLevels(
  signals: TwelveSignal[],
  opts: KeyLevelFilter = {},
): KeyLevel[] {
  const { minStrength = 0, dedupPct = 0, maxCount } = opts;
  interface Cand extends KeyLevel { strength: number }
  const cands: Cand[] = [];
  for (const sig of signals ?? []) {
    const strength = Number(sig?.strength ?? 0);
    if (!Number.isFinite(strength) || strength < minStrength) continue;
    const color = twelveColor(sig.direction);
    const name = sig.name_cn || sig.system || "信号";
    for (const kl of sig.key_levels ?? []) {
      const price = Number(kl?.price);
      if (!Number.isFinite(price) || price <= 0) continue;
      cands.push({ label: `${name}·${kl?.label ?? "关键位"}`, price, color, width: 1, strength });
    }
  }
  let picked: Cand[];
  if (dedupPct <= 0) {
    picked = cands;
  } else {
    const sorted = [...cands].sort((a, b) => a.price - b.price);
    picked = [];
    for (const c of sorted) {
      const tail = picked[picked.length - 1];
      if (tail && Math.abs(c.price - tail.price) / tail.price < dedupPct) {
        if (c.strength > tail.strength) picked[picked.length - 1] = c;
      } else {
        picked.push(c);
      }
    }
  }
  // 预算截断按强度降序（丢弱保强），返回前恢复价格升序保持渲染稳定
  if (maxCount !== undefined && picked.length > Math.max(0, maxCount)) {
    picked = [...picked]
      .sort((a, b) => b.strength - a.strength)
      .slice(0, Math.max(0, maxCount))
      .sort((a, b) => a.price - b.price);
  }
  return picked.map(({ strength: _s, ...kl }) => kl);
}

// ── 进阶模式：自动画线按可靠度裁剪 ────────────────────────────────────

// Fib 档位大白话（市场惯例：0 在波段端点，比例 = 回撤深度）。
// 进阶模式在标准 label（"Fib 0.618"）后追加；专业模式保持纯标准格式。
const FIB_PLAIN: Record<string, string> = {
  "0": "波段端点",
  "0.236": "浅回调位",
  "0.382": "常见回调位",
  "0.5": "中位回调",
  "0.618": "黄金回调位",
  "0.786": "深度回调位",
  "1": "波段另一端",
};

function fibPlainLabel(label: string | undefined): string | undefined {
  if (!label) return label;
  const m = /^Fib\s+([\d.]+)$/.exec(label);
  const plain = m ? FIB_PLAIN[m[1]] : undefined;
  return plain ? `${label} ${plain}` : label;
}

// 每类线型在进阶模式下的保留规则（可靠度高的类型优先占预算）：
//   trend   → 支撑/压力趋势线 ≤2 条
//   sr      → 触碰次数最多的水平位 ≤2 条（引擎输出已按触碰数降序）
//   fib     → 离现价最近的 2 档（最可能马上用到的），label 附大白话
//   channel → 只保留回归中轴 1 条
//   rect    → 整理箱体 1 个（上下边缘算 2 条）
export function trimDrawings(
  perType: Partial<Record<DrawMode, DrawingResult>>,
  reliability: Partial<Record<DrawMode, number>> | undefined,
  price: number,
  budget: number,
): { result: DrawingResult; cost: number; usedTypes: DrawMode[] } {
  const order = (Object.keys(perType) as DrawMode[]).sort(
    (a, b) => (reliability?.[b] ?? 0) - (reliability?.[a] ?? 0),
  );
  const acc: DrawingResult = { segments: [], hlines: [], bands: [] };
  let cost = 0;
  const usedTypes: DrawMode[] = [];
  for (const t of order) {
    const d = perType[t];
    if (!d) continue;
    let segs: DrawSegment[] = [];
    let hls: DrawHLine[] = [];
    let bds: DrawBand[] = [];
    if (t === "trend") {
      segs = d.segments.slice(0, 2);
    } else if (t === "sr") {
      hls = d.hlines.slice(0, 2);
    } else if (t === "fib") {
      hls = [...d.hlines]
        .sort((a, b) => Math.abs(a.price - price) - Math.abs(b.price - price))
        .slice(0, 2)
        .map((l) => ({ ...l, label: fibPlainLabel(l.label) }));
    } else if (t === "channel") {
      segs = d.segments.filter(isChannelMid).slice(0, 1);
    } else if (t === "rect") {
      bds = d.bands.slice(0, 1);
    }
    const c = segs.length + hls.length + bds.length * 2;
    if (c === 0 || cost + c > budget) continue;
    acc.segments.push(...segs);
    acc.hlines.push(...hls);
    acc.bands.push(...bds);
    cost += c;
    usedTypes.push(t);
  }
  return { result: acc, cost, usedTypes };
}

// ── 图例 ────────────────────────────────────────────────────────────────

export interface LegendEntry {
  color: string;
  dashed?: boolean;
  name: string;
  explain: string;
}

// ── 组合器 ──────────────────────────────────────────────────────────────

export interface ComposeInput {
  mode: ViewMode;
  tradePlan: ConsensusTradePlan | null;
  planDirection: string; // bullish | bearish | neutral
  smart: SmartLevels | null;
  /** 专业模式的全量画线（按用户开关计算好的） */
  fullDrawings: DrawingResult | null;
  /** 进阶模式的分线型画线（每类单独 computeDrawings 的结果） */
  perTypeDrawings: Partial<Record<DrawMode, DrawingResult>> | null;
  reliability?: Partial<Record<DrawMode, number>>;
  price: number;
  signals: TwelveSignal[];
  /** 专业模式的「十二套关键位」开关状态 */
  twelveOn: boolean;
}

export interface ChartComposition {
  planLines: DrawHLine[];
  smartLevels: SmartLevels | null;
  drawings: DrawingResult | null;
  keyLevels: KeyLevel[];
  /** 交易区间带（入场/止损/止盈时间锚定矩形），三档视图共用，随计划自动多空镜像 */
  tradeZones: TradeZone[];
  legend: LegendEntry[];
  lineCount: number;
}

function drawingsCost(d: DrawingResult | null): number {
  if (!d) return 0;
  return d.segments.length + d.hlines.length + d.bands.length * 2;
}

const DRAW_TYPE_LEGEND: Record<DrawMode, LegendEntry> = {
  trend: { color: "#3fb950", name: "趋势线", explain: "连接近期高低点：价格沿线上方走，说明趋势还健康" },
  sr: { color: "#d29922", dashed: true, name: "历史支撑/压力", explain: "过去多次触碰的价位，靠近时容易变盘" },
  fib: { color: "#a855f7", dashed: true, name: "斐波那契回撤", explain: "斐波回撤档位：0.382/0.5/0.618 常见回调支撑；0 在波段端点，数值 = 回撤深度（与 TradingView 同口径）" },
  channel: { color: "#58a6ff", name: "回归中轴", explain: "价格围绕这条线上下波动，偏离太远容易拉回" },
  rect: { color: "#58a6ff", dashed: true, name: "整理区间", explain: "价格反复震荡的箱体上下边界" },
};

function buildLegend(opts: {
  planLines: DrawHLine[];
  hasSmartPlain: boolean;
  proSmart: SmartLevels | null;
  usedTypes: DrawMode[];
  hasKeyLevels: boolean;
  short: boolean;
  hasZones?: boolean;
}): LegendEntry[] {
  const { planLines, hasSmartPlain, proSmart, usedTypes, hasKeyLevels, short, hasZones } = opts;
  const legend: LegendEntry[] = [];
  if (hasZones) {
    legend.push({
      color: PLAN_COLORS.entry,
      name: "交易区间带",
      explain: "彩色矩形 = 入场（蓝）/止损（红）/止盈（绿）区间，只覆盖最近一段 K 线，悬停可看盈亏比",
    });
  }
  if (planLines.some((l) => l.color === PLAN_COLORS.entry)) {
    legend.push({
      color: PLAN_COLORS.entry,
      dashed: true,
      name: short ? "建议做空区" : "建议买入区",
      explain: "蓝色虚线之间是 AI 共识给出的分批挂单区间",
    });
  }
  if (planLines.some((l) => l.color === PLAN_COLORS.stop)) {
    legend.push({
      color: PLAN_COLORS.stop,
      name: "止损线",
      explain: short ? "涨破这条线就认赔离场，保住本金" : "跌破这条线就认赔离场，保住本金",
    });
  }
  if (planLines.some((l) => l.color === PLAN_COLORS.profit)) {
    legend.push({
      color: PLAN_COLORS.profit,
      name: "止盈目标",
      explain: "价格到达后可以分批落袋，不贪最后一段",
    });
  }
  if (hasSmartPlain || proSmart) {
    legend.push(
      { color: SMART_LINE_COLORS.support, dashed: true, name: "强支撑", explain: "历史上碰到容易反弹的价位" },
      { color: SMART_LINE_COLORS.resistance, dashed: true, name: "强压力", explain: "历史上碰到容易回落的价位" },
    );
  }
  if (proSmart) {
    legend.push({ color: "#c9d1d9", dashed: true, name: "现价", explain: "当前最新成交价" });
  }
  for (const t of usedTypes) legend.push(DRAW_TYPE_LEGEND[t]);
  if (hasKeyLevels) {
    legend.push({
      color: "#8b949e",
      dashed: true,
      name: "信号关键位",
      explain: "十二套技术系统给出的入场/止损参考价（绿=看多系统、红=看空系统）",
    });
  }
  return legend;
}

export function composeChartView(input: ComposeInput): ChartComposition {
  const {
    mode, tradePlan, planDirection, smart, fullDrawings, perTypeDrawings,
    reliability, price, signals, twelveOn,
  } = input;
  const short = planDirection === "bearish";
  // 区间带三档共用：随计划自动生成（多空镜像），无计划时为空数组
  const tradeZones = planToZones(tradePlan, planDirection);

  if (mode === "pro") {
    // 专业：全量现状——原始 label 计划线 + 智能视图（含现价）+ 用户开关画线 + 未过滤关键位
    const planLines = planToOverlay(tradePlan).hlines;
    const keyLevels = twelveOn ? signalsToKeyLevels(signals) : [];
    const usedTypes: DrawMode[] = [];
    if (fullDrawings) {
      // 线型识别 kind 优先，label 回退（兼容无 kind 的旧 payload）
      if (fullDrawings.segments.some((s) => s.kind === "trend" || (!s.kind && (s.label ?? "").includes("趋势")))) usedTypes.push("trend");
      if (fullDrawings.hlines.some((l) => l.kind === "sr" || (!l.kind && (l.label ?? "").startsWith("S/R")))) usedTypes.push("sr");
      if (fullDrawings.hlines.some((l) => l.kind === "fib" || (!l.kind && (l.label ?? "").startsWith("Fib")))) usedTypes.push("fib");
      if (fullDrawings.segments.some((s) => s.kind === "channel-mid" || s.kind === "channel-edge"
        || (!s.kind && ((s.label ?? "").includes("通道") || (s.label ?? "").includes("中轴"))))) usedTypes.push("channel");
      if (fullDrawings.bands.length > 0) usedTypes.push("rect");
    }
    return {
      planLines,
      smartLevels: smart,
      drawings: fullDrawings,
      keyLevels,
      tradeZones,
      legend: buildLegend({ planLines, hasSmartPlain: false, proSmart: smart, usedTypes, hasKeyLevels: keyLevels.length > 0, short, hasZones: tradeZones.length > 0 }),
      lineCount: planLines.length + drawingsCost(fullDrawings) + keyLevels.length + (smart ? (smart.support ? 1 : 0) + (smart.resistance ? 1 : 0) + 1 : 0),
    };
  }

  // 简洁核心：计划线（TP2 省略）大白话 + 强支撑/强压力，预算 ≤6
  const rawPlan = planToOverlay(tradePlan).hlines.filter((l) => !isTp2Line(l));
  const plainPlan = relabelPlanLinesPlain(rawPlan, planDirection);
  const srPlain = smartToPlainLines(smart);
  const core = [
    ...plainPlan.slice(0, SIMPLE_BUDGET),
    ...srPlain.slice(0, Math.max(0, SIMPLE_BUDGET - Math.min(plainPlan.length, SIMPLE_BUDGET))),
  ];

  if (mode === "simple") {
    return {
      planLines: core,
      smartLevels: null,
      drawings: null,
      keyLevels: [],
      tradeZones,
      legend: buildLegend({ planLines: core, hasSmartPlain: srPlain.length > 0, proSmart: null, usedTypes: [], hasKeyLevels: false, short, hasZones: tradeZones.length > 0 }),
      lineCount: core.length,
    };
  }

  // 进阶：简洁核心 + 裁剪后的自动画线 + 过滤去重后的关键位，总预算 ≤12
  let remaining = ADVANCED_BUDGET - core.length;
  const trimmed = trimDrawings(perTypeDrawings ?? {}, reliability, price, Math.max(0, remaining));
  remaining -= trimmed.cost;
  // 预算截断按强度降序丢弱保强（maxCount 内部处理），渲染顺序按价格稳定
  const keyLevels = signalsToKeyLevels(signals, {
    minStrength: 0.6,
    dedupPct: 0.003,
    maxCount: Math.max(0, remaining),
  });
  return {
    planLines: core,
    smartLevels: null,
    drawings: trimmed.cost > 0 ? trimmed.result : null,
    keyLevels,
    tradeZones,
    legend: buildLegend({ planLines: core, hasSmartPlain: srPlain.length > 0, proSmart: null, usedTypes: trimmed.usedTypes, hasKeyLevels: keyLevels.length > 0, short, hasZones: tradeZones.length > 0 }),
    lineCount: core.length + trimmed.cost + keyLevels.length,
  };
}

// ── 视图模式持久化 ──────────────────────────────────────────────────────

export const VIEW_MODE_KEY = "jarvis.chart.viewMode";

export function loadViewMode(): ViewMode {
  try {
    const v = localStorage.getItem(VIEW_MODE_KEY);
    return v === "advanced" || v === "pro" || v === "simple" ? v : "simple";
  } catch {
    return "simple";
  }
}

export function saveViewMode(m: ViewMode): void {
  try {
    localStorage.setItem(VIEW_MODE_KEY, m);
  } catch {
    /* storage unavailable — mode simply won't persist */
  }
}
