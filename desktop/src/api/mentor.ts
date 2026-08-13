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

/** 逐条裁决证据行 */
export interface MentorVerdictItem {
  key: string;
  level: "pass" | "warn" | "fail";
  weight: number;
  /** 系统实时证据原文（如「4h CVD 与价格背离」） */
  evidence: string;
  detail?: string;
}

export interface MentorVerdict {
  light: MentorLight;
  /** 0-100 */
  score: number;
  items: MentorVerdictItem[];
  summary: string;
  /** 红灯冷静期（分钟）；无冷静期缺省 */
  cooldown_min?: number;
}

/** 下单前计划输入 */
export interface MentorPlanInput {
  symbol: string;
  direction: MentorDirection;
  entry: number;
  stop_loss: number;
  take_profit: number;
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
}

export interface MentorStats {
  ok: boolean;
  by_light: MentorLightStat[];
  followed: { trades: number; win_rate_pct: number | null; total_pnl_pct: number };
  not_followed: { trades: number; win_rate_pct: number | null; total_pnl_pct: number };
  mock?: boolean;
  error?: string;
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
  /** 提交计划 → 裁决。后端不可达/未实现时本地 mock 裁决并落本地台账。 */
  async submitPlan(input: MentorPlanInput): Promise<MentorPlanResponse> {
    try {
      const d = await post<MentorPlanResponse>("/mentor/plan", input);
      if (d && d.ok !== false && d.verdict) return d;
      throw new Error(d?.error || "裁决响应异常");
    } catch {
      const verdict = mockVerdict(input);
      const rows = loadMockPlans();
      const id = `m${Date.now()}-${mockSeq++}`;
      rows.unshift({ id, created_at: now(), ...input, verdict, outcome: null });
      saveMockPlans(rows);
      return { ok: true, plan_id: id, verdict, mock: true };
    }
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

  /** 信任看板统计；后端未就绪时由本地 mock 台账现算 */
  async stats(): Promise<MentorStats> {
    try {
      const d = await get<MentorStats>("/mentor/stats");
      if (d && d.ok !== false && Array.isArray(d.by_light)) return d;
      throw new Error(d?.error || "统计响应异常");
    } catch {
      return mockStats(loadMockPlans());
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
