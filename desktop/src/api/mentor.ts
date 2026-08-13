/**
 * 情绪风控导师 API 层（任务 O）。
 *
 * 契约（与后端 agent-8 裁决核心 / agent-3 AI 解释约定）：
 *   POST /api/mentor/plan                → 提交交易计划，实时证据裁决红黄绿灯
 *   GET  /api/mentor/plans?symbol&days   → 计划台账（含裁决与结果回填）
 *   POST /api/mentor/plan/{id}/outcome   → 事后回填结果（win/loss/scratch/skipped）
 *   GET  /api/mentor/stats               → 红黄绿灯胜率 + 听导师 vs 不听盈亏对比
 *   POST /api/mentor/explain/stream      → AI 导师评语（SSE，仿信号解读流式写法）
 *
 * 后端未就绪期间：所有读写自动降级为本地 mock（返回值带 mock:true，
 * UI 据此显示「演示数据 · 待后端联调」徽标），联调后零改动切真数据。
 */

const BASE_URL = "/api";

// ─── 类型（契约为准） ───

export type MentorLight = "green" | "yellow" | "red";
export type MentorDirection = "long" | "short";
export type MentorOutcomeResult = "win" | "loss" | "scratch" | "skipped";

/** 逐条裁决证据行。
 * evidence/detail 契约上是字符串，但真实后端（任务 M 实测）detail 会给结构化
 * 对象（如 {direction, confidence} / {warns:[], passes:[]}），渲染层必须经
 * fmtEvidence 格式化，禁止直接作为 React child 输出。 */
export interface MentorVerdictItem {
  key: string;
  level: "pass" | "warn" | "fail";
  weight: number;
  /** 系统实时证据原文（可能为任意形态，渲染前过 fmtEvidence） */
  evidence: unknown;
  detail?: unknown;
}

export interface MentorVerdict {
  light: MentorLight;
  /** 0-100 */
  score: number;
  items: MentorVerdictItem[];
  summary: string;
  /** 红灯冷静期（分钟）；无冷静期缺省 */
  cooldown_min?: number;
  /** V2：个人军规逐条核对（后端 V1 rules 区段；mock 时本地军规现算） */
  rules?: MentorVerdictItem[];
}

/** 下单前计划输入 */
export interface MentorPlanInput {
  symbol: string;
  direction: MentorDirection;
  entry: number;
  stop_loss: number;
  take_profit: number;
  /** 本金 USDT（R1 新增，可选；配合杠杆算名义价值与最大亏损） */
  principal?: number;
  position_pct?: number;
  leverage?: number;
  reason_text?: string;
  reason_tags?: string[];
  /** 情绪自评 1-5（5=极度冲动） */
  emotion_score?: number;
}

export interface MentorPlanResponse {
  ok: boolean;
  plan_id: number | string;
  verdict: MentorVerdict;
  /** true = 本地 mock 结果（后端未就绪） */
  mock?: boolean;
  /** true = 军规区段为本地核对（后端 V1 rules 未就绪时前端兜底） */
  rules_local?: boolean;
  error?: string;
}

export interface MentorOutcomeInput {
  result: MentorOutcomeResult;
  pnl_pct?: number;
  /** 是否听了导师建议（红灯执行=false） */
  followed: boolean;
  note?: string;
}

/** 台账行（后端行 + 容错归一化） */
export interface MentorPlanRow {
  id: number | string;
  created_at?: string;
  symbol: string;
  direction: MentorDirection;
  entry: number;
  stop_loss: number;
  take_profit: number;
  principal?: number | null;
  position_pct?: number | null;
  leverage?: number | null;
  reason_text?: string | null;
  reason_tags?: string[] | null;
  emotion_score?: number | null;
  verdict?: MentorVerdict | null;
  outcome?: (MentorOutcomeInput & { recorded_at?: string }) | null;
}

export interface MentorPlansResponse {
  ok: boolean;
  rows: MentorPlanRow[];
  mock?: boolean;
  error?: string;
}

/** 灯色统计桶 */
export interface MentorLightStat {
  light: MentorLight;
  plans: number;
  executed: number;
  wins: number;
  losses: number;
  win_rate_pct: number | null;
  total_pnl_pct: number;
  /** 真后端口径为均值（avg_pnl_pct）时落此字段，展示层标「均」 */
  avg_pnl_pct?: number | null;
}

export interface MentorTrustBucket {
  trades: number;
  win_rate_pct: number | null;
  total_pnl_pct: number;
  avg_pnl_pct?: number | null;
}

export interface MentorStats {
  ok: boolean;
  by_light: MentorLightStat[];
  followed: MentorTrustBucket;
  not_followed: MentorTrustBucket;
  mock?: boolean;
  error?: string;
}

/** 真后端 /mentor/stats 归一化：
 * 兼容两种形态——契约版（by_light 数组 / not_followed）与实测版
 * （by_light 按灯色键控对象 / ignored / n·closed·win_rate·avg_pnl_pct 命名）。 */
export function normalizeStats(raw: unknown): MentorStats | null {
  if (!raw || typeof raw !== "object") return null;
  const d = raw as Record<string, unknown>;
  if (d.ok === false) return null;

  const num = (v: unknown): number => (Number.isFinite(Number(v)) ? Number(v) : 0);
  const rate = (v: unknown): number | null => {
    const n = Number(v);
    if (!Number.isFinite(n)) return null;
    // 0-1 视为小数占比转百分比；>1 视为已是百分比
    return Math.round((n <= 1 ? n * 100 : n) * 10) / 10;
  };

  const liftBucket = (b: unknown): MentorTrustBucket => {
    const o = (b && typeof b === "object" ? b : {}) as Record<string, unknown>;
    return {
      trades: num(o.trades ?? o.closed ?? o.n),
      win_rate_pct: rate(o.win_rate_pct ?? o.win_rate),
      total_pnl_pct: num(o.total_pnl_pct),
      avg_pnl_pct: o.avg_pnl_pct != null ? num(o.avg_pnl_pct) : undefined,
    };
  };

  const lights: MentorLight[] = ["green", "yellow", "red"];
  let byLight: MentorLightStat[];
  if (Array.isArray(d.by_light)) {
    byLight = (d.by_light as Record<string, unknown>[]).map((b) => ({
      light: (lights.includes(b.light as MentorLight) ? b.light : "yellow") as MentorLight,
      plans: num(b.plans ?? b.n),
      executed: num(b.executed ?? b.closed),
      wins: num(b.wins),
      losses: num(b.losses),
      win_rate_pct: rate(b.win_rate_pct ?? b.win_rate),
      total_pnl_pct: num(b.total_pnl_pct),
      avg_pnl_pct: b.avg_pnl_pct != null ? num(b.avg_pnl_pct) : undefined,
    }));
  } else if (d.by_light && typeof d.by_light === "object") {
    const m = d.by_light as Record<string, Record<string, unknown>>;
    byLight = lights.map((light) => {
      const b = m[light] ?? {};
      return {
        light,
        plans: num(b.n ?? b.plans),
        executed: num(b.closed ?? b.executed),
        wins: num(b.wins),
        losses: num(b.losses),
        win_rate_pct: rate(b.win_rate ?? b.win_rate_pct),
        total_pnl_pct: num(b.total_pnl_pct),
        avg_pnl_pct: b.avg_pnl_pct != null ? num(b.avg_pnl_pct) : undefined,
      };
    });
  } else {
    return null;
  }

  return {
    ok: true,
    by_light: byLight,
    followed: liftBucket(d.followed),
    not_followed: liftBucket(d.not_followed ?? d.ignored),
  };
}

// ─── V2：个人军规（我的军规引擎） ───

/** 单条军规。builtin 默认军规只可改参/停用；custom 自定义为文本型提醒 */
export interface MentorRule {
  id: string | number;
  /** 内置规则语义 key（与后端 V1 对齐；自定义规则为 custom_*） */
  kind: string;
  text: string;
  /** 数值参数（可编辑；无参数规则为 null） */
  param: number | null;
  param_label?: string | null;
  enabled: boolean;
  builtin: boolean;
}

/** 默认 8 条军规（mock 种子；kind 与后端 V1 引擎对齐可平滑切换） */
export const DEFAULT_RULES: MentorRule[] = [
  { id: "r1", kind: "max_daily_trades", text: "单日最多开单数", param: 3, param_label: "单/天", enabled: true, builtin: true },
  { id: "r2", kind: "stop_after_losses", text: "当日连亏即停手", param: 2, param_label: "笔", enabled: true, builtin: true },
  { id: "r3", kind: "min_rr", text: "盈亏比不得低于", param: 1.5, param_label: "", enabled: true, builtin: true },
  { id: "r4", kind: "max_risk_pct", text: "单笔预亏不超过本金", param: 5, param_label: "%", enabled: true, builtin: true },
  { id: "r5", kind: "no_late_night", text: "凌晨（0-6 点）不开单", param: null, enabled: true, builtin: true },
  { id: "r6", kind: "emotion_cap", text: "情绪自评达到该分不开单", param: 4, param_label: "分", enabled: true, builtin: true },
  { id: "r7", kind: "must_have_reason", text: "必须写开单理由（拒绝纯凭感觉）", param: null, enabled: true, builtin: true },
  { id: "r8", kind: "no_martingale", text: "亏损后不加仓摊平（文本提醒）", param: null, enabled: true, builtin: true },
];

const RULES_KEY = "jarvis.mentor.rules";

function loadLocalRules(): MentorRule[] {
  try {
    const raw = localStorage.getItem(RULES_KEY);
    if (!raw) return DEFAULT_RULES.map((r) => ({ ...r }));
    const arr = JSON.parse(raw) as MentorRule[];
    return Array.isArray(arr) && arr.length ? arr : DEFAULT_RULES.map((r) => ({ ...r }));
  } catch {
    return DEFAULT_RULES.map((r) => ({ ...r }));
  }
}

function saveLocalRules(rules: MentorRule[]) {
  try {
    localStorage.setItem(RULES_KEY, JSON.stringify(rules));
  } catch {
    /* ignore */
  }
}

/** 后端规则行归一化（字段名容错） */
function normalizeRule(raw: Record<string, unknown>, i: number): MentorRule {
  return {
    id: (raw.id as string | number) ?? `srv${i}`,
    kind: typeof raw.kind === "string" ? raw.kind : typeof raw.key === "string" ? raw.key : `rule${i}`,
    text: typeof raw.text === "string" ? raw.text : typeof raw.name === "string" ? raw.name : `规则 ${i + 1}`,
    param: Number.isFinite(Number(raw.param)) && raw.param != null ? Number(raw.param) : null,
    param_label: typeof raw.param_label === "string" ? raw.param_label : null,
    enabled: raw.enabled !== false && raw.enabled !== 0,
    builtin: raw.builtin === true || raw.custom !== true,
  };
}

export interface MentorRulesResponse {
  ok: boolean;
  rules: MentorRule[];
  mock?: boolean;
  error?: string;
}

// ─── 裁决字段防御性格式化（热修 R1：后端字段可能是对象，直接渲染会崩 React） ───

/** detail 对象键 → 中文名（拼人话用；未收录的键原样展示） */
const DETAIL_KEY_CN: Record<string, string> = {
  direction: "方向",
  confidence: "置信度",
  rr: "盈亏比",
  sl_dist_pct: "止损距离%",
  tp_dist_pct: "止盈距离%",
  toll_ratio: "过路费占比",
  fee_pct: "费率%",
  plan_min_rr: "RR门槛",
  against: "逆势",
  reversal: "反转分",
  available: "数据可用",
  warns: "警示",
  passes: "通过",
  score: "分数",
  note: "说明",
};

/** 枚举值 → 中文（direction 等常见后端枚举） */
const DETAIL_VALUE_CN: Record<string, string> = {
  bullish: "看涨",
  bearish: "看跌",
  neutral: "中性",
  long: "做多",
  short: "做空",
};

/**
 * 任意后端值 → 可渲染字符串（渲染层唯一入口，任何形态都不崩）：
 * string 直出；number 最多 4 位小数去尾零；boolean 是/否；数组分号连接；
 * 对象提取键值拼人话（0-1 的 confidence 转百分比）；空值/空对象 → "—"；
 * 兜底 JSON.stringify。
 */
export function fmtEvidence(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "string") {
    const s = v.trim();
    return s ? (DETAIL_VALUE_CN[s] ?? s) : "—";
  }
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return "—";
    return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(4)));
  }
  if (typeof v === "boolean") return v ? "是" : "否";
  if (Array.isArray(v)) {
    const parts = v.map(fmtEvidence).filter((s) => s !== "—");
    return parts.length ? parts.join("；") : "—";
  }
  if (typeof v === "object") {
    const entries = Object.entries(v as Record<string, unknown>).filter(
      ([, val]) => val != null && val !== "",
    );
    if (!entries.length) return "—";
    const parts = entries.map(([k, val]) => {
      // confidence 0-1 → 百分比人话
      if (k === "confidence" && typeof val === "number" && val >= 0 && val <= 1) {
        return `置信度 ${Math.round(val * 100)}%`;
      }
      return `${DETAIL_KEY_CN[k] ?? k} ${fmtEvidence(val)}`;
    });
    return parts.join(" · ");
  }
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/** 裁决证据行数组归一化（items 与 V2 rules 区段共用） */
function normalizeItems(raw: unknown, prefix: string): MentorVerdictItem[] {
  if (!Array.isArray(raw)) return [];
  return (raw as Record<string, unknown>[]).map((it, i) => ({
    key: typeof it.key === "string" ? it.key : `${prefix}${i}`,
    level: (it.level === "pass" || it.level === "warn" || it.level === "fail"
      ? it.level
      : "warn") as "pass" | "warn" | "fail",
    weight: Number.isFinite(Number(it.weight)) ? Number(it.weight) : 0,
    evidence: it.evidence,
    detail: it.detail,
  }));
}

/** 后端 verdict 归一化：light/score/items/rules 容错（字段缺失/形态漂移不崩 UI） */
export function normalizeVerdict(raw: unknown): MentorVerdict | null {
  if (!raw || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  const light: MentorLight =
    v.light === "green" || v.light === "yellow" || v.light === "red"
      ? v.light
      : "yellow";
  const score = Number(v.score);
  const cooldown = Number(v.cooldown_min);
  const rules = normalizeItems(v.rules, "rule");
  return {
    light,
    score: Number.isFinite(score) ? Math.round(score) : 0,
    items: normalizeItems(v.items, "item"),
    summary: fmtEvidence(v.summary),
    ...(Number.isFinite(cooldown) && cooldown > 0 ? { cooldown_min: cooldown } : {}),
    ...(rules.length ? { rules } : {}),
  };
}

// ─── RR 计算（表单实时显示与引擎门禁同口径） ───

/** 盈亏比：多单 (TP-入场)/(入场-SL)，空单 (入场-TP)/(SL-入场)；点位不自洽返回 null */
export function calcRR(
  direction: MentorDirection,
  entry: number,
  stopLoss: number,
  takeProfit: number,
): number | null {
  if (!(entry > 0) || !(stopLoss > 0) || !(takeProfit > 0)) return null;
  const risk = direction === "long" ? entry - stopLoss : stopLoss - entry;
  const reward = direction === "long" ? takeProfit - entry : entry - takeProfit;
  if (risk <= 0 || reward <= 0) return null;
  return reward / risk;
}

/** 引擎最小盈亏比门禁（与后端 twelve_min_rr 默认对齐） */
export const MIN_RR = 1.5;

// ─── mock 裁决引擎（后端未就绪时的本地降级；规则可解释、结果可复现） ───

let mockSeq = 1;
const now = () => new Date().toISOString().slice(0, 19).replace("T", " ");

function mockVerdict(input: MentorPlanInput): MentorVerdict {
  const items: MentorVerdictItem[] = [];
  const rr = calcRR(input.direction, input.entry, input.stop_loss, input.take_profit);

  // 1) 盈亏比（权重 30）
  if (rr == null) {
    items.push({
      key: "rr", level: "fail", weight: 30,
      evidence: "止盈/止损点位与方向不自洽，无法计算盈亏比",
      detail: "多单要求 SL < 入场 < TP；空单要求 TP < 入场 < SL",
    });
  } else if (rr < MIN_RR) {
    items.push({
      key: "rr", level: "fail", weight: 30,
      evidence: `盈亏比 ${rr.toFixed(2)} 低于引擎门禁下限 ${MIN_RR}`,
      detail: "止盈太近或止损太远，赢了赚的不够输一次亏的",
    });
  } else {
    items.push({
      key: "rr", level: "pass", weight: 30,
      evidence: `盈亏比 ${rr.toFixed(2)} ≥ ${MIN_RR}，风险回报结构合格`,
    });
  }

  // 2) 止损距离（权重 20；参考引擎 sl_too_tight 噪声带口径）
  const slDist = Math.abs(input.entry - input.stop_loss) / input.entry * 100;
  if (!Number.isFinite(slDist) || slDist <= 0) {
    items.push({ key: "sl_dist", level: "fail", weight: 20, evidence: "止损距离无效" });
  } else if (slDist < 0.5) {
    items.push({
      key: "sl_dist", level: "fail", weight: 20,
      evidence: `止损距离 ${slDist.toFixed(2)}% 在噪声带内（<0.5%），大概率被扫损`,
      detail: "参考引擎 sl_too_tight 门禁：今日模拟盘拒单主因就是这一条",
    });
  } else if (slDist > 8) {
    items.push({
      key: "sl_dist", level: "warn", weight: 20,
      evidence: `止损距离 ${slDist.toFixed(2)}% 偏宽，单笔亏损占比过大`,
    });
  } else {
    items.push({
      key: "sl_dist", level: "pass", weight: 20,
      evidence: `止损距离 ${slDist.toFixed(2)}% 在合理区间`,
    });
  }

  // 3) 情绪自评（权重 20）
  const emo = input.emotion_score ?? 3;
  if (emo >= 5) {
    items.push({
      key: "emotion", level: "fail", weight: 20,
      evidence: "情绪自评 5/5：极度冲动状态，历史上这种时候的单子最容易亏",
    });
  } else if (emo === 4) {
    items.push({
      key: "emotion", level: "warn", weight: 20,
      evidence: "情绪自评 4/5：偏冲动，建议先深呼吸再确认点位",
    });
  } else {
    items.push({
      key: "emotion", level: "pass", weight: 20,
      evidence: `情绪自评 ${emo}/5：状态平稳`,
    });
  }

  // 4) 开单理由（权重 15 + 15）
  const tags = input.reason_tags ?? [];
  if (tags.includes("凭感觉")) {
    items.push({
      key: "reason_tags", level: "fail", weight: 15,
      evidence: "理由标签含「凭感觉」：没有可验证的入场依据",
      detail: "感觉不是证据——补一条结构/订单流依据再来",
    });
  } else if (tags.length === 0) {
    items.push({
      key: "reason_tags", level: "warn", weight: 15,
      evidence: "未选任何理由标签，无法归因复盘",
    });
  } else {
    items.push({
      key: "reason_tags", level: "pass", weight: 15,
      evidence: `理由标签：${tags.join("、")}`,
    });
  }
  const text = (input.reason_text ?? "").trim();
  if (text.length >= 10) {
    items.push({
      key: "reason_text", level: "pass", weight: 15,
      evidence: "写了完整开单逻辑，事后可复盘验证",
    });
  } else {
    items.push({
      key: "reason_text", level: "warn", weight: 15,
      evidence: "开单逻辑描述过短（<10 字），复盘时会想不起来为什么开这单",
    });
  }

  // 加权得分：pass 全分 / warn 半分 / fail 0 分
  const total = items.reduce((s, i) => s + i.weight, 0);
  const got = items.reduce(
    (s, i) => s + (i.level === "pass" ? i.weight : i.level === "warn" ? i.weight / 2 : 0),
    0,
  );
  const score = Math.round((got / total) * 100);
  const fails = items.filter((i) => i.level === "fail").length;

  let light: MentorLight;
  if (fails === 0 && score >= 70) light = "green";
  else if (fails >= 2 || score < 40) light = "red";
  else light = "yellow";

  const summary =
    light === "green"
      ? `${score} 分：计划结构完整，按纪律执行并严格止损。`
      : light === "yellow"
        ? `${score} 分：存在 ${items.filter((i) => i.level !== "pass").length} 项瑕疵，修正后再进场更稳。`
        : `${score} 分：${fails} 项硬伤，这单大概率是情绪单——先冷静，行情不会跑。`;

  return {
    light,
    score,
    items,
    summary,
    ...(light === "red" ? { cooldown_min: 15 } : {}),
  };
}

// ─── mock 台账（内存 + localStorage 持久化，刷新不丢） ───

const MOCK_STORE_KEY = "jarvis.mentor.mockPlans";

function loadMockPlans(): MentorPlanRow[] {
  try {
    const raw = localStorage.getItem(MOCK_STORE_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw) as MentorPlanRow[];
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function saveMockPlans(rows: MentorPlanRow[]) {
  try {
    localStorage.setItem(MOCK_STORE_KEY, JSON.stringify(rows.slice(0, 200)));
  } catch {
    /* 存储满/隐私模式忽略 */
  }
}

function mockStats(rows: MentorPlanRow[]): MentorStats {
  const lights: MentorLight[] = ["green", "yellow", "red"];
  const byLight: MentorLightStat[] = lights.map((light) => {
    const plans = rows.filter((r) => r.verdict?.light === light);
    const executed = plans.filter((r) => r.outcome && r.outcome.result !== "skipped");
    const wins = executed.filter((r) => r.outcome?.result === "win").length;
    const losses = executed.filter((r) => r.outcome?.result === "loss").length;
    const pnl = executed.reduce((s, r) => s + (Number(r.outcome?.pnl_pct) || 0), 0);
    return {
      light,
      plans: plans.length,
      executed: executed.length,
      wins,
      losses,
      win_rate_pct: executed.length ? Math.round((wins / executed.length) * 1000) / 10 : null,
      total_pnl_pct: Math.round(pnl * 100) / 100,
    };
  });
  const bucket = (followed: boolean) => {
    const done = rows.filter(
      (r) => r.outcome && r.outcome.result !== "skipped" && r.outcome.followed === followed,
    );
    const wins = done.filter((r) => r.outcome?.result === "win").length;
    const pnl = done.reduce((s, r) => s + (Number(r.outcome?.pnl_pct) || 0), 0);
    return {
      trades: done.length,
      win_rate_pct: done.length ? Math.round((wins / done.length) * 1000) / 10 : null,
      total_pnl_pct: Math.round(pnl * 100) / 100,
    };
  };
  return { ok: true, by_light: byLight, followed: bucket(true), not_followed: bucket(false), mock: true };
}

// ─── V2：本地军规核对（后端 V1 rules 区段未就绪时的兜底；逐条人话证据） ───

function todayStrLocal(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 今日连亏笔数（按创建时间升序取尾部连续 loss） */
export function lossStreak(rows: MentorPlanRow[]): number {
  const done = rows
    .filter((r) => r.outcome && (r.outcome.result === "win" || r.outcome.result === "loss"))
    .sort((a, b) => String(a.created_at ?? "").localeCompare(String(b.created_at ?? "")));
  let streak = 0;
  for (let i = done.length - 1; i >= 0; i--) {
    if (done[i].outcome?.result === "loss") streak++;
    else break;
  }
  return streak;
}

/**
 * 用本地启用军规逐条核对当前计划（异步：计数类规则读当日台账）。
 * 返回 MentorVerdictItem[]（weight=0，渲染层军规区不展示权重）。
 */
export async function evalRulesLocal(input: MentorPlanInput): Promise<MentorVerdictItem[]> {
  const rules = loadLocalRules().filter((r) => r.enabled);
  if (!rules.length) return [];

  // 今日台账（真/mock 同一入口；失败时计数类规则降级为 warn）
  let todayRows: MentorPlanRow[] | null = null;
  try {
    const res = await mentorApi.plans(undefined, 1);
    const today = todayStrLocal();
    todayRows = res.rows.filter((r) => String(r.created_at ?? "").startsWith(today));
  } catch {
    todayRows = null;
  }

  const rr = calcRR(input.direction, input.entry, input.stop_loss, input.take_profit);
  const hour = new Date().getHours();
  const out: MentorVerdictItem[] = [];

  for (const r of rules) {
    const item = (level: "pass" | "warn" | "fail", evidence: string): MentorVerdictItem => ({
      key: r.kind,
      level,
      weight: 0,
      evidence,
    });
    switch (r.kind) {
      case "max_daily_trades": {
        const cap = r.param ?? 3;
        if (todayRows == null) {
          out.push(item("warn", `单日≤${cap}单：台账不可达，未核对`));
        } else {
          const n = todayRows.length;
          out.push(
            n >= cap
              ? item("fail", `今天已提交 ${n} 单，达到你定的上限 ${cap} 单——这单是超额的`)
              : item("pass", `今天第 ${n + 1} 单，在你定的 ${cap} 单以内`),
          );
        }
        break;
      }
      case "stop_after_losses": {
        const cap = r.param ?? 2;
        if (todayRows == null) {
          out.push(item("warn", `连亏${cap}笔停手：台账不可达，未核对`));
        } else {
          const streak = lossStreak(todayRows);
          out.push(
            streak >= cap
              ? item("fail", `今天已连亏 ${streak} 笔，按你的军规该收手了`)
              : item("pass", `今日连亏 ${streak} 笔，未触发 ${cap} 笔停手线`),
          );
        }
        break;
      }
      case "min_rr": {
        const min = r.param ?? MIN_RR;
        if (rr == null) out.push(item("fail", `盈亏比无法计算（点位不自洽），低于你定的 ${min}`));
        else
          out.push(
            rr < min
              ? item("fail", `RR ${rr.toFixed(2)} 低于你定的下限 ${min}`)
              : item("pass", `RR ${rr.toFixed(2)} ≥ ${min}，符合军规`),
          );
        break;
      }
      case "max_risk_pct": {
        const cap = r.param ?? 5;
        if (!input.principal || input.principal <= 0) {
          out.push(item("warn", `单笔预亏≤本金${cap}%：未填本金，未核对`));
        } else {
          const lev = input.leverage && input.leverage > 0 ? input.leverage : 1;
          const slDist = Math.abs(input.entry - input.stop_loss) / input.entry;
          const riskPct = lev * slDist * 100;
          out.push(
            riskPct > cap
              ? item("fail", `打到止损预亏约占本金 ${riskPct.toFixed(1)}%，超过你定的 ${cap}%`)
              : item("pass", `预亏约占本金 ${riskPct.toFixed(1)}%，在 ${cap}% 以内`),
          );
        }
        break;
      }
      case "no_late_night": {
        out.push(
          hour < 6
            ? item("fail", `现在是凌晨 ${hour} 点——你的军规说这个时段不开单（判断力最差的时候）`)
            : item("pass", `当前 ${hour} 点，不在凌晨禁开时段`),
        );
        break;
      }
      case "emotion_cap": {
        const cap = r.param ?? 4;
        const emo = input.emotion_score ?? 3;
        out.push(
          emo >= cap
            ? item("fail", `情绪自评 ${emo}/5 达到你定的 ${cap} 分红线——先冷静再说`)
            : item("pass", `情绪自评 ${emo}/5，低于 ${cap} 分红线`),
        );
        break;
      }
      case "must_have_reason": {
        const tags = (input.reason_tags ?? []).filter((t) => t !== "凭感觉");
        const hasText = (input.reason_text ?? "").trim().length >= 10;
        out.push(
          tags.length === 0 && !hasText
            ? item("fail", "没有可验证的开单理由（只有感觉不算）——违反你自己的军规")
            : item("pass", "有可验证的开单依据"),
        );
        break;
      }
      default: {
        // 文本型军规（含 no_martingale 与自定义）：展示型提醒，不判 pass/fail
        out.push(item("pass", `提醒：${r.text}`));
        break;
      }
    }
  }
  return out;
}

// ─── 请求封装（真接口优先，失败降级 mock） ───

async function post<T>(endpoint: string, body: unknown, timeoutMs = 15_000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE_URL}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`API ${res.status}`);
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

async function get<T>(endpoint: string, timeoutMs = 15_000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE_URL}${endpoint}`, { signal: controller.signal });
    if (!res.ok) throw new Error(`API ${res.status}`);
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

export const mentorApi = {
  /** 提交计划 → 裁决。后端不可达/未实现时本地 mock 裁决并落本地台账。
   *  V2：后端 verdict 无 rules 区段时，用本地军规现算补齐（rules_local=true）。 */
  async submitPlan(input: MentorPlanInput): Promise<MentorPlanResponse> {
    let real: MentorPlanResponse | null = null;
    try {
      const d = await post<MentorPlanResponse>("/mentor/plan", input);
      if (d && d.ok !== false && d.verdict) {
        // R1 热修：真实后端 verdict 字段形态漂移（detail 为对象等），入口归一化
        const verdict = normalizeVerdict(d.verdict);
        if (verdict) real = { ok: true, plan_id: d.plan_id, verdict };
      }
      if (!real) throw new Error(d?.error || "裁决响应异常");
    } catch {
      const verdict = mockVerdict(input);
      const rows = loadMockPlans();
      const id = `m${Date.now()}-${mockSeq++}`;
      rows.unshift({ id, created_at: now(), ...input, verdict, outcome: null });
      saveMockPlans(rows);
      real = { ok: true, plan_id: id, verdict, mock: true };
    }
    if (!real.verdict.rules || real.verdict.rules.length === 0) {
      try {
        const localRules = await evalRulesLocal(input);
        if (localRules.length) {
          real.verdict.rules = localRules;
          real.rules_local = true;
        }
      } catch {
        /* 军规核对失败不阻塞裁决主链路 */
      }
    }
    return real;
  },

  /** 台账列表；后端未就绪时读本地 mock 台账 */
  async plans(symbol?: string, days = 30): Promise<MentorPlansResponse> {
    try {
      const q = new URLSearchParams();
      if (symbol) q.set("symbol", symbol);
      q.set("days", String(days));
      const d = await get<{ ok?: boolean; rows?: unknown[]; plans?: unknown[]; error?: string }>(
        `/mentor/plans?${q.toString()}`,
      );
      const raw = (d.rows ?? d.plans) as MentorPlanRow[] | undefined;
      if (d.ok !== false && Array.isArray(raw)) return { ok: true, rows: raw };
      throw new Error(d?.error || "台账响应异常");
    } catch {
      let rows = loadMockPlans();
      if (symbol) rows = rows.filter((r) => r.symbol === symbol);
      return { ok: true, rows, mock: true };
    }
  },

  /** 回填结果；后端未就绪时写本地 mock 台账 */
  async recordOutcome(planId: number | string, outcome: MentorOutcomeInput): Promise<{ ok: boolean; mock?: boolean; error?: string }> {
    try {
      const d = await post<{ ok?: boolean; error?: string }>(
        `/mentor/plan/${encodeURIComponent(String(planId))}/outcome`,
        outcome,
      );
      if (d.ok !== false) return { ok: true };
      throw new Error(d?.error || "结果回填失败");
    } catch {
      const rows = loadMockPlans();
      const row = rows.find((r) => String(r.id) === String(planId));
      if (row) {
        row.outcome = { ...outcome, recorded_at: now() };
        saveMockPlans(rows);
        return { ok: true, mock: true };
      }
      return { ok: false, mock: true, error: "本地台账中找不到该计划" };
    }
  },

  /** 信任看板统计；真后端两种形态归一化，后端未就绪时由本地 mock 台账现算 */
  async stats(): Promise<MentorStats> {
    try {
      const d = await get<unknown>("/mentor/stats");
      const normalized = normalizeStats(d);
      if (normalized) return normalized;
      throw new Error("统计响应异常");
    } catch {
      return mockStats(loadMockPlans());
    }
  },

  /** V2 我的军规：GET /mentor/rules；后端未就绪读本地（默认 8 条种子） */
  async rules(): Promise<MentorRulesResponse> {
    try {
      const d = await get<{ ok?: boolean; rules?: unknown[]; error?: string }>("/mentor/rules");
      if (d && d.ok !== false && Array.isArray(d.rules)) {
        return {
          ok: true,
          rules: (d.rules as Record<string, unknown>[]).map(normalizeRule),
        };
      }
      throw new Error(d?.error || "军规响应异常");
    } catch {
      return { ok: true, rules: loadLocalRules(), mock: true };
    }
  },

  /** V2 我的军规：POST /mentor/rules 全量保存；后端未就绪落本地（联调后自动切真） */
  async saveRules(rules: MentorRule[]): Promise<{ ok: boolean; mock?: boolean; error?: string }> {
    // 本地永远留一份（军规核对 evalRulesLocal 依赖本地副本，双写保证一致）
    saveLocalRules(rules);
    try {
      const d = await post<{ ok?: boolean; error?: string }>("/mentor/rules", { rules });
      if (d.ok !== false) return { ok: true };
      throw new Error(d?.error || "军规保存失败");
    } catch {
      return { ok: true, mock: true };
    }
  },
};

// ─── AI 导师评语（SSE 流式，仿 signalExplainStream 写法） ───

export interface MentorStreamMeta {
  engine: "llm" | "rule";
  model?: string | null;
}

/**
 * POST /api/mentor/explain/stream（SSE 手工解析；EventSource 不支持 POST）。
 * 后端未配置 LLM 时回 JSON {ok:false, code:"not_configured"} → onNotConfigured。
 * 接口不存在（404/网络错误）→ onUnavailable（UI 显示「待后端联调」占位文案）。
 */
export async function mentorExplainStream(
  planId: number | string,
  handlers: {
    onMeta?: (meta: MentorStreamMeta) => void;
    onDelta: (text: string) => void;
    onDone?: () => void;
    onNotConfigured?: (message: string) => void;
    onUnavailable?: (message: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/mentor/explain/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: planId }),
      signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") return;
    handlers.onUnavailable?.("AI 导师服务暂不可达（后端联调中）");
    return;
  }
  if (res.status === 404 || res.status === 501 || res.status === 502) {
    handlers.onUnavailable?.("AI 导师详解接口尚未上线（后端联调中）");
    return;
  }
  if (!res.ok || !res.body) {
    throw new Error(`API ${res.status}: ${res.statusText}`);
  }
  const ctype = res.headers.get("content-type") ?? "";
  if (!ctype.includes("text/event-stream")) {
    const d = (await res.json().catch(() => null)) as
      | { ok?: boolean; code?: string; message?: string }
      | null;
    if (d?.code === "not_configured") {
      handlers.onNotConfigured?.(d.message ?? "未配置 AI，请到设置页配置 API Key");
      return;
    }
    handlers.onUnavailable?.(d?.message ?? "AI 导师详解服务响应异常（后端联调中）");
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let doneSeen = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE 帧以空行分隔；最后一段可能是半帧，留 buf 等下一轮
    const frames = buf.split("\n\n");
    buf = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      let obj: {
        type?: string;
        engine?: "llm" | "rule";
        model?: string | null;
        content?: string;
        message?: string;
      };
      try {
        obj = JSON.parse(line.slice(5).trim());
      } catch {
        continue;
      }
      if (obj.type === "meta") {
        handlers.onMeta?.({ engine: obj.engine ?? "llm", model: obj.model });
      } else if (obj.type === "delta" && obj.content) {
        handlers.onDelta(obj.content);
      } else if (obj.type === "done") {
        doneSeen = true;
        handlers.onDone?.();
      } else if (obj.type === "error") {
        throw new Error(obj.message ?? "评语输出异常");
      }
    }
  }
  if (!doneSeen) handlers.onDone?.();
}

// ─── 计划执行意向（本地记忆：契约无执行意向接口，回填结果时预填 followed） ───

const INTENT_KEY = "jarvis.mentor.intents";

export interface PlanIntent {
  /** executed=点了执行 / skipped=听导师放弃 */
  action: "executed" | "skipped";
  /** 是否听了导师（红灯执行/跳过冷静期=false） */
  followed: boolean;
  ts: number;
}

export function rememberIntent(planId: number | string, intent: PlanIntent) {
  try {
    const raw = localStorage.getItem(INTENT_KEY);
    const map = raw ? (JSON.parse(raw) as Record<string, PlanIntent>) : {};
    map[String(planId)] = intent;
    localStorage.setItem(INTENT_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

export function recallIntent(planId: number | string): PlanIntent | null {
  try {
    const raw = localStorage.getItem(INTENT_KEY);
    if (!raw) return null;
    const map = JSON.parse(raw) as Record<string, PlanIntent>;
    return map[String(planId)] ?? null;
  } catch {
    return null;
  }
}

// ─── 表单常量 ───

/** 开单理由标签（任务 O 指定集合） */
export const REASON_TAGS = [
  "趋势回踩",
  "突破",
  "FVG",
  "压力位反转",
  "抄底/摸顶",
  "消息面",
  "凭感觉",
] as const;

export const LIGHT_CN: Record<MentorLight, string> = {
  green: "绿灯 · 可执行",
  yellow: "黄灯 · 谨慎",
  red: "红灯 · 别下这单",
};
