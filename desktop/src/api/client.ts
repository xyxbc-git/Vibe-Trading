const BASE_URL = "/api";

/** 后端日志流的 SSE 地址（同源，经 Vite 代理转发到后端）。 */
export const LOG_STREAM_URL = `${BASE_URL}/logs/stream`;

export interface LogLine {
  seq: number;
  ts: string;
  level: "info" | "warn" | "error";
  source: string;
  text: string;
}

async function request<T>(
  endpoint: string,
  options?: RequestInit,
  timeoutMs = 15_000,
): Promise<T> {
  const url = `${BASE_URL}${endpoint}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...options?.headers },
      signal: controller.signal,
      ...options,
    });
    if (!res.ok) {
      throw new Error(`API ${res.status}: ${res.statusText}`);
    }
    return res.json();
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new Error("请求超时，后端可能正在获取外部数据");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  get: <T>(endpoint: string) => request<T>(endpoint),

  post: <T>(endpoint: string, body?: unknown) =>
    request<T>(endpoint, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    }),

  put: <T>(endpoint: string, body?: unknown) =>
    request<T>(endpoint, {
      method: "PUT",
      body: body ? JSON.stringify(body) : undefined,
    }),

  snapshot: (symbol = "BTCUSDT") =>
    api.get<Record<string, unknown>>(`/snapshot?symbol=${symbol}`),

  kline: (symbol = "BTCUSDT", interval = "15m", limit = 200) =>
    api.get<Record<string, unknown>>(
      `/kline?symbol=${symbol}&interval=${interval}&limit=${limit}`,
    ),

  wallet: () => api.get<Record<string, unknown>>("/wallet"),

  positions: () => api.get<Record<string, unknown>[]>("/positions"),

  orders: () => api.get<Record<string, unknown>[]>("/orders"),

  // 后端 /api/orders/* 均为 FastAPI query 参数签名（非 JSON body）
  placeOrder: (p: {
    symbol: string;
    side: "buy" | "sell";
    price: number;
    qty: number;
    stopLoss?: number;
    takeProfit?: number;
  }) => {
    const q = new URLSearchParams({
      symbol: p.symbol,
      side: p.side,
      price: String(p.price),
      qty: String(p.qty),
    });
    if (p.stopLoss != null) q.set("stop_loss", String(p.stopLoss));
    if (p.takeProfit != null) q.set("take_profit", String(p.takeProfit));
    return api.post<{ ok: boolean; order_id?: number; reason?: string }>(
      `/orders/place?${q.toString()}`,
    );
  },

  cancelOrder: (orderId: number) =>
    api.post<{ ok: boolean; order_id?: number; reason?: string }>(
      `/orders/cancel?order_id=${orderId}`,
    ),

  // 平掉该 symbol 的全部未平仓位：POST /api/positions/close?symbol=BTCUSDT
  closePosition: (symbol: string) =>
    api.post<{ closed: Record<string, unknown>[] }>(
      `/positions/close?symbol=${encodeURIComponent(symbol)}`,
    ),

  ledger: () => api.get<Record<string, unknown>[]>("/ledger"),

  traderStatus: () => api.get<Record<string, unknown>>("/trader/status"),

  // history = 多轮上下文（最近若干条 {role, content}），后端最多取 8 条
  ask: (question: string, symbol = "BTCUSDT", history?: ChatTurn[]) =>
    request<{ answer: string; engine?: string; lessons_cited?: number }>(
      "/ask",
      { method: "POST", body: JSON.stringify({ question, symbol, history }) },
      60_000,
    ),

  events: () => api.get<Record<string, unknown>>("/events"),

  series: (symbol = "BTCUSDT", days = 90) =>
    api.get<Record<string, unknown>>(`/series?symbol=${symbol}&days=${days}`),

  factor: () => api.get<Record<string, unknown>[]>("/factor"),

  scalperStatus: () => api.get<Record<string, unknown>>("/scalper/status"),
  scalperReport: () => api.get<Record<string, unknown>>("/scalper/report"),
  scalperLog: (limit = 50) =>
    api.get<{ lines: string[]; total: number }>(`/scalper/log?limit=${limit}`),
  scalperStart: (symbol = "BTCUSDT") =>
    api.post<Record<string, unknown>>("/scalper/start", { symbol }),
  scalperStop: () => api.post<Record<string, unknown>>("/scalper/stop"),
  evolveStatus: () => api.get<Record<string, unknown>>("/evolve/status"),
  evolveStart: (rounds = 10, symbol = "BTCUSDT", mode: "evolve" | "combo" = "evolve") =>
    api.post<Record<string, unknown>>(
      `/evolve/start?rounds=${rounds}&symbol=${symbol}&mode=${mode}`,
    ),
  evolveStop: () =>
    api.post<{ ok: boolean; error?: string }>("/evolve/stop"),
  evolveGraveyard: () => api.get<Record<string, unknown>[]>("/evolve/graveyard"),
  clearGraveyard: () =>
    api.post<{ ok: boolean; cleared?: number; reason?: string }>(
      "/evolve/graveyard/clear",
    ),
  evolveHallOfFame: () =>
    api.get<Record<string, unknown>[]>("/evolve/hall-of-fame"),
  growthTimeline: () => api.get<Record<string, unknown>[]>("/growth/timeline"),
  growthMilestones: () =>
    api.get<Record<string, unknown>[]>("/growth/milestones"),
  growthStats: () => api.get<Record<string, unknown>>("/growth/stats"),
  config: () => api.get<Record<string, unknown>>("/config"),
  updateConfig: (data: Record<string, unknown>) =>
    api.put<Record<string, unknown>>("/config", data),
  qdConfig: () => api.get<QdConfig>("/qd-config"),
  updateQdConfig: (data: { gateway_base?: string; agent_token?: string }) =>
    api.put<{ ok: boolean; reason?: string }>("/qd-config", data),
  testQdConfig: () =>
    request<QdConfigTest>("/qd-config/test", { method: "POST" }, 20_000),
  issueQdToken: (data: {
    username?: string;
    password: string;
    scopes?: string;
    gateway_base?: string;
  }) =>
    request<QdTokenIssue>(
      "/qd-config/issue-token",
      { method: "POST", body: JSON.stringify(data) },
      20_000,
    ),
  marketOverview: () => api.get<Record<string, unknown>>("/market/overview"),

  actionBrief: (symbol = "BTCUSDT") =>
    request<Record<string, unknown>>("/actions/brief?symbol=" + symbol, { method: "POST" }, 30_000),
  actionExecute: (symbol = "BTCUSDT", dryRun = true) =>
    request<Record<string, unknown>>(
      `/actions/execute?symbol=${symbol}&dry_run=${dryRun}`,
      { method: "POST" },
      30_000,
    ),
  actionRadar: (symbols?: string) =>
    request<Record<string, unknown>>(
      "/actions/radar" + (symbols ? `?symbols=${symbols}` : ""),
      { method: "POST" },
      60_000,
    ),
  actionOpen: (symbol = "BTCUSDT", dryRun = false) =>
    request<Record<string, unknown>>(
      `/actions/open?symbol=${symbol}&dry_run=${dryRun}`,
      { method: "POST" },
      30_000,
    ),
  traderCycle: (symbols = "BTC,ETH,SOL", dryRun = false) =>
    request<Record<string, unknown>>(
      `/trader/cycle?symbols=${symbols}&dry_run=${dryRun}`,
      { method: "POST" },
      60_000,
    ),

  health: () => api.get<HealthStatus>("/health"),

  track: (symbol = "BTCUSDT") =>
    api.get<TrackData>(`/track?symbol=${encodeURIComponent(symbol)}`),

  trackRecord: (symbol = "BTCUSDT") =>
    request<TrackRecordResult>(
      `/track/record?symbol=${encodeURIComponent(symbol)}`,
      { method: "POST" },
      60_000,
    ),

  circuitBreaker: () => api.get<CircuitBreakerStatus>("/circuit-breaker"),

  resetCircuitBreaker: () =>
    api.post<{ ok: boolean; result?: Record<string, unknown>; error?: string }>(
      "/circuit-breaker/reset",
    ),

  killSwitch: () =>
    request<{ ok: boolean; qd?: unknown; local_cancelled?: unknown[]; error?: string }>(
      "/actions/kill-switch",
      { method: "POST" },
      30_000,
    ),

  tradingConfig: () => api.get<TradingConfig>("/trading-config"),

  updateTradingConfig: (data: Partial<TradingConfig>) =>
    api.put<{ ok: boolean; config?: TradingConfig; reason?: string }>(
      "/trading-config",
      data,
    ),

  logs: (limit = 500) =>
    api.get<{ lines: LogLine[]; total: number }>(`/logs?limit=${limit}`),
  clearLogs: () => api.post<{ ok: boolean }>("/logs/clear"),

  backtestRun: (payload: {
    name?: string;
    code?: string;
    symbol?: string;
    timeframe?: string;
    start?: string;
    end?: string;
    capital?: number;
  }) =>
    request<{ ok: boolean; error?: string }>(
      "/backtest/run",
      { method: "POST", body: JSON.stringify(payload) },
      30_000,
    ),
  backtestResult: () => api.get<BacktestState>("/backtest/result"),
  backtestCode: (name: string) =>
    api.get<{ name: string; code: string; error?: string }>(
      `/backtest/code?name=${encodeURIComponent(name)}`,
    ),

  // ─── 大模型 (LLM) 配置 ───
  llmConfig: () => api.get<LlmConfig>("/llm-config"),
  updateLlmConfig: (data: {
    provider?: string;
    base_url?: string;
    model?: string;
    api_key?: string;
    clear_key?: boolean;
    temperature?: number;
    max_tokens?: number;
    system_prompt_extra?: string;
  }) =>
    api.put<{ ok: boolean; reason?: string; config?: LlmConfig }>(
      "/llm-config",
      data,
    ),
  testLlmConfig: () =>
    request<LlmTestResult>("/llm-config/test", { method: "POST" }, 40_000),

  // ─── LLM 用量/成本记账 + 调用内容日志 ───
  llmUsage: (days = 30, recent = 10, module?: string, offset = 0) =>
    api.get<LlmUsageResponse>(
      `/llm/usage?days=${days}&recent=${recent}&offset=${offset}` +
        (module ? `&module=${encodeURIComponent(module)}` : ""),
    ),
  llmUsageDetail: (id: number) =>
    api.get<LlmUsageDetailResponse>(`/llm/usage/detail?id=${id}`),

  // ─── AI 交易复盘（模拟盘已平仓交易 → 统计 + LLM 诊断）───
  jarvisReview: (symbol?: string, limit = 50) =>
    request<JarvisReviewResponse>(
      "/jarvis/review",
      { method: "POST", body: JSON.stringify({ symbol: symbol ?? "", limit }) },
      60_000,
    ),

  // ─── AI 策略工坊：自然语言 → 可回测策略 ───
  strategyGenerate: (payload: {
    description: string;
    symbol?: string;
    timeframe?: string;
  }) =>
    request<{ ok: boolean; error?: string }>(
      "/strategy/generate",
      { method: "POST", body: JSON.stringify(payload) },
      30_000,
    ),
  strategyGenerateResult: () =>
    api.get<StrategyGenState>("/strategy/generate/result"),
  strategySaveToHall: (payload: {
    name: string;
    code: string;
    rule?: Record<string, unknown>;
    result?: Record<string, unknown>;
    reasoning?: string;
  }) =>
    api.post<{ ok: boolean; error?: string; name?: string }>(
      "/strategy/save-to-hall",
      payload,
    ),

  // ─── 策略自动进化：生成→回测→复盘→改进循环 ───
  strategyEvolveStart: (payload: {
    description: string;
    rounds?: number;
    symbol?: string;
    timeframe?: string;
    start_date?: string;
    end_date?: string;
    initial_capital?: number;
    resume_run_id?: string;
  }) =>
    request<{ ok: boolean; run_id?: string; error?: string }>(
      "/strategy-evolve/start",
      { method: "POST", body: JSON.stringify(payload) },
      30_000,
    ),
  strategyEvolveStatus: () =>
    api.get<StrategyEvolveStatus>("/strategy-evolve/status"),
  strategyEvolveResult: (runId = "") =>
    api.get<{ ok: boolean; run?: StrategyEvolveRun; error?: string }>(
      `/strategy-evolve/result${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`,
    ),
  strategyEvolveRuns: () =>
    api.get<{ runs: StrategyEvolveRunBrief[] }>("/strategy-evolve/runs"),
  strategyEvolveStop: () =>
    api.post<{ ok: boolean; error?: string }>("/strategy-evolve/stop"),

  // ─── 价位邮件提醒 ───
  alertConfig: () => api.get<AlertConfig>("/alerts/config"),
  updateAlertConfig: (data: AlertConfigUpdate) =>
    api.put<{ ok: boolean; reason?: string; config?: AlertConfig }>(
      "/alerts/config",
      data,
    ),
  // 测试邮件单独处理：无论 HTTP 状态码如何都解析 body，把后端真实失败原因带回前端
  testAlertEmail: async (
    recipients?: string[],
  ): Promise<{ ok: boolean; reason?: string; to?: string[] }> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25_000);
    try {
      const res = await fetch(`${BASE_URL}/alerts/test-email`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recipients }),
        signal: controller.signal,
      });
      const body = (await res.json().catch(() => ({}))) as {
        ok?: boolean;
        reason?: string;
        to?: string[];
      };
      if (body && typeof body.ok === "boolean") return body as { ok: boolean };
      return { ok: res.ok, reason: body?.reason ?? `HTTP ${res.status}` };
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        return { ok: false, reason: "请求超时（SMTP 服务器可能不可达）" };
      }
      return { ok: false, reason: e instanceof Error ? e.message : "网络错误" };
    } finally {
      clearTimeout(timer);
    }
  },
  alertPlans: () => api.get<AlertPlan[]>("/alerts/plans"),
  createAlertPlan: (data: AlertPlanInput) =>
    api.post<{ ok: boolean; reason?: string; plan?: AlertPlan }>(
      "/alerts/plans",
      data,
    ),
  updateAlertPlan: (id: string, data: Partial<AlertPlanInput>) =>
    api.put<{ ok: boolean; reason?: string; plan?: AlertPlan }>(
      `/alerts/plans/${id}`,
      data,
    ),
  deleteAlertPlan: (id: string) =>
    request<{ ok: boolean; reason?: string }>(`/alerts/plans/${id}`, {
      method: "DELETE",
    }),
  alertCheck: (dryRun = false) =>
    request<AlertCheckResult>(
      "/alerts/check",
      { method: "POST", body: JSON.stringify({ dry_run: dryRun }) },
      30_000,
    ),
  alertPrice: (symbol = "BTCUSDT") =>
    api.get<{ symbol: string; price: number | null }>(
      `/alerts/price?symbol=${encodeURIComponent(symbol)}`,
    ),

  // ─── 贾维斯信号引擎（后端可能未就绪，调用方必须做空态/降级处理）───
  // 响应均为封套结构（含 ok 字段）；ok:false 时 HTTP 仍是 200，调用方须自行判断
  twelveConsensus: (symbol = "BTCUSDT") =>
    api.get<TwelveConsensusResponse>(
      `/twelve/consensus?symbol=${encodeURIComponent(symbol)}`,
    ),
  twelveSignals: (symbol = "BTCUSDT", tf = "4h") =>
    api.get<TwelveSignalsResponse>(
      `/twelve/signals?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}`,
    ),
  // 单信号级历史胜率（缓存读取；null = 尚未回测过该 symbol×tf）
  twelveSignalWinrate: (symbol = "BTCUSDT", tf = "4h") =>
    api.get<SignalWinrateResponse>(
      `/twelve/signal-winrate?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}`,
    ),
  twelveSignalWinrateRun: (payload?: {
    symbols?: string | string[];
    tfs?: string | string[];
    days?: number;
    stride?: number;
  }) =>
    api.post<{ ok: boolean; error?: string }>(
      "/twelve/signal-winrate/run",
      payload ?? {},
    ),
  twelveSignalWinrateStatus: () =>
    api.get<SignalWinrateStatus>("/twelve/signal-winrate/status"),
  // 单信号胜率回测逐笔明细（K 线标记历史盈损点；side 可选过滤 long/short）
  twelveSignalWinrateTrades: (symbol: string, tf: string, system: string, side?: "long" | "short") =>
    api.get<SignalWinrateTradesResponse>(
      `/twelve/signal-winrate/trades?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}&system=${encodeURIComponent(system)}${side ? `&side=${side}` : ""}`,
    ),
  // LLM 推理链耗时较长，超时放宽到 60s
  jarvisReason: (symbol = "BTCUSDT") =>
    request<JarvisReasonResponse>(
      "/jarvis/reason",
      { method: "POST", body: JSON.stringify({ symbol }) },
      60_000,
    ),
  jarvisInsights: (limit = 20) =>
    api.get<JarvisInsightsResponse>(`/jarvis/insights?limit=${limit}`),

  // ─── 合约仓位与风控计算器（Task #3）───
  positionCalc: (
    symbol = "BTCUSDT",
    tf: ConsensusScope = "auto",
    overrides?: Partial<PositionCalcConfig>,
    /** 用户手动入场价（临时预览，不落盘）；缺省用信号计划价 */
    entryOverride?: number | null,
  ) => {
    const q = new URLSearchParams({ symbol, tf });
    if (overrides?.poscalc_capital_usdt != null)
      q.set("capital", String(overrides.poscalc_capital_usdt));
    if (overrides?.poscalc_leverage != null)
      q.set("leverage", String(overrides.poscalc_leverage));
    if (overrides?.poscalc_risk_pct != null)
      q.set("risk_pct", String(overrides.poscalc_risk_pct));
    if (overrides?.poscalc_margin_pct != null)
      q.set("margin_pct", String(overrides.poscalc_margin_pct));
    if (entryOverride != null && Number.isFinite(entryOverride) && entryOverride > 0)
      q.set("entry", String(entryOverride));
    // 多币多周期信号计算冷启动较慢，独立 30s 超时
    return request<PositionCalcResponse>(`/position-calc?${q.toString()}`, undefined, 30_000);
  },
  positionCalcConfig: () =>
    api.get<PositionCalcConfig>("/position-calc/config"),
  updatePositionCalcConfig: (data: Partial<PositionCalcConfig>) =>
    api.put<{ ok: boolean; reason?: string; config?: PositionCalcConfig }>(
      "/position-calc/config",
      data,
    ),

  // ─── 市场情报页（免 Key 真实数据源；后端 TTL 缓存 + 降级）───
  marketIntel: () => api.get<MarketIntelResponse>("/market-intel"),
};

// ─── GET /api/market-intel 响应 ───

export interface MarketIntelResponse {
  ok: boolean;
  /** 各源中最近一次成功拉取的 unix 秒 */
  updated_at?: number | null;
  fng?: { value: number; classification: string; ts?: number | null } | null;
  /** symbol -> lastFundingRate（小数，如 0.0001 = 0.01%） */
  funding_rate?: Record<string, number> | null;
  funding_ts?: number | null;
  oi?: {
    symbol: string;
    value: number;
    change_pct: number | null;
    ts?: number | null;
  } | null;
  long_short?: {
    symbol: string;
    long_pct: number;
    short_pct: number;
    ratio: number;
    ts?: number | null;
  } | null;
  liquidations?: null;
  onchain?: null;
  /** 未接入源 -> 原因说明（前端渲染灰化占位态） */
  unavailable?: Record<string, string> | null;
  /** 拉取失败的源 -> 错误摘要（保留旧缓存时也会带上） */
  errors?: Record<string, string> | null;
  error?: string;
}

// ─── AI 问答多轮 + 流式 ───

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface AskStreamMeta {
  engine: "llm" | "rule";
  model?: string | null;
}

/**
 * 流式问答（POST + SSE 手工解析；EventSource 不支持 POST）。
 * 事件回调：onMeta 首包（引擎/模型）、onDelta 增量文本、onDone 收尾。
 * 抛错 = 连接/解析失败，调用方应回退 api.ask 非流式。
 */
export async function askStream(
  params: { question: string; symbol?: string; history?: ChatTurn[] },
  handlers: {
    onMeta?: (meta: AskStreamMeta) => void;
    onDelta: (text: string) => void;
    onDone?: (info: { lessons_cited?: number }) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE_URL}/ask/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question: params.question,
      symbol: params.symbol ?? "BTCUSDT",
      history: params.history ?? [],
    }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`API ${res.status}: ${res.statusText}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let doneSeen = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE 帧以空行分隔；最后一段可能是半帧，留在 buf 里等下一轮
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
        lessons_cited?: number;
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
        handlers.onDone?.({ lessons_cited: obj.lessons_cited });
      } else if (obj.type === "error") {
        throw new Error(obj.message ?? "流式输出异常");
      }
    }
  }
  if (!doneSeen) {
    // 流被服务端提前挂断且没有 done 事件：已渲染内容有效，不额外报错
    handlers.onDone?.({});
  }
}

// ─── 信号一键解读（大白话，SSE 流式）───

/** 解读模式：signal = 单信号卡；consensus = 12 系统整体分歧 */
export interface SignalExplainBody {
  mode: "signal" | "consensus";
  symbol: string;
  tf: string;
  /** 前端现成数据打包给后端进 prompt（不重复取数） */
  payload: Record<string, unknown>;
}

/**
 * 信号大白话解读（POST /twelve/signal-explain/stream，SSE 手工解析）。
 * 后端未配置 LLM 时返回 JSON {ok:false, code:"not_configured"}（非 SSE），
 * 此时回调 onNotConfigured 并结束（不抛错，前端引导去设置页）。
 */
export async function signalExplainStream(
  body: SignalExplainBody,
  handlers: {
    onMeta?: (meta: AskStreamMeta) => void;
    onDelta: (text: string) => void;
    onDone?: () => void;
    onNotConfigured?: (message: string) => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE_URL}/twelve/signal-explain/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`API ${res.status}: ${res.statusText}`);
  }
  // 未配置 LLM：后端直接回 JSON（非 event-stream）
  const ctype = res.headers.get("content-type") ?? "";
  if (!ctype.includes("text/event-stream")) {
    const d = (await res.json()) as { ok?: boolean; code?: string; message?: string };
    if (d.code === "not_configured") {
      handlers.onNotConfigured?.(d.message ?? "未配置 AI，请到设置页配置 API Key");
      return;
    }
    throw new Error(d.message ?? "解读服务响应异常");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let doneSeen = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const frames = buf.split("\n\n");
    buf = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      let obj: { type?: string; engine?: "llm" | "rule"; model?: string | null; content?: string; message?: string };
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
        throw new Error(obj.message ?? "解读输出异常");
      }
    }
  }
  if (!doneSeen) {
    handlers.onDone?.();
  }
}

// ─── AI 交易复盘 ───

export interface ReviewTradeBrief {
  symbol?: string;
  side?: string;
  pnl_usdt?: number;
  pnl_pct?: number | null;
  exit_reason?: string | null;
}

export interface JarvisReviewStats {
  closed_trades: number;
  win_rate_pct: number | null;
  profit_factor: number | null;
  total_pnl_usdt: number;
  avg_win_usdt: number | null;
  avg_loss_usdt: number | null;
  avg_hold_days: number | null;
  max_consecutive_losses: number;
  exit_reason_dist: Record<string, number>;
  by_side: Record<string, { trades: number; win_rate_pct: number; pnl_usdt: number }>;
  best_trade: ReviewTradeBrief | null;
  worst_trade: ReviewTradeBrief | null;
}

export interface JarvisReviewContent {
  summary: string;
  diagnosis: string[];
  recommendations: string[];
  cautions: string[];
}

export interface JarvisReviewResponse {
  ok: boolean;
  symbol?: string;
  stats?: JarvisReviewStats;
  review?: JarvisReviewContent;
  source?: "llm" | "rules";
  cached?: boolean;
  error?: string;
}

export type AlertDirection = "above" | "below";

export interface AlertPlan {
  id: string;
  name: string;
  symbol: string;
  target_price: number;
  direction: AlertDirection;
  recipients: string[];
  enabled: boolean;
  repeat: boolean;
  note: string;
  created_at: number;
  last_price: number | null;
  last_triggered_at: number | null;
  triggered_count: number;
  last_send_result: string | null;
}

export interface AlertPlanInput {
  name: string;
  symbol: string;
  target_price: number;
  direction: AlertDirection;
  recipients?: string[];
  enabled?: boolean;
  repeat?: boolean;
  note?: string;
}

export interface AlertConfig {
  smtp: {
    host: string;
    port: number;
    use_ssl: boolean;
    username: string;
    from_name: string;
    password_masked: string;
    has_password: boolean;
  };
  recipients: string[];
  contacts: AlertContact[];
  poll_interval_s: number;
  monitor: {
    running: boolean;
    last_run: string | null;
    last_summary: { checked: number; triggered: number } | null;
    last_error: string | null;
  };
}

export interface AlertContact {
  email: string;
  label: string;
}

export interface AlertConfigUpdate {
  smtp?: {
    host?: string;
    port?: number;
    use_ssl?: boolean;
    username?: string;
    from_name?: string;
    password?: string;
  };
  recipients?: string[];
  contacts?: AlertContact[];
  poll_interval_s?: number;
}

export interface AlertCheckResult {
  checked: number;
  triggered: number;
  dry_run: boolean;
  ts: string;
  results: {
    id: string;
    name: string;
    price?: number;
    target?: number;
    direction?: AlertDirection;
    sent?: boolean;
    to?: string[];
    reason?: string;
    skipped?: string;
  }[];
}

export interface QdConfig {
  gateway_base: string;
  agent_token_masked: string;
  has_token: boolean;
  env_token_active: boolean;
  env_base_active: boolean;
}

export interface LlmConfig {
  provider: string;
  base_url: string;
  model: string;
  api_key_masked: string;
  has_key: boolean;
  env_fallback_available: boolean;
  configured: boolean;
  source: "file" | "env" | "none";
  effective_base: string;
  effective_model: string;
  temperature: number;
  max_tokens: number;
  system_prompt_extra: string;
  presets: Record<string, { base_url: string; model: string }>;
}

export interface LlmTestResult {
  ok: boolean;
  latency_ms?: number;
  model?: string;
  base?: string;
  reply?: string;
  error?: string;
}

// ─── LLM 用量/成本记账 ───

export interface LlmUsageSum {
  calls: number;
  ok_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  estimated_calls: number;
}

export interface LlmUsageBucket {
  module?: string;
  model?: string;
  calls: number;
  ok_calls: number;
  total_tokens: number;
  cost_usd: number;
}

export interface LlmUsageRecent {
  /** jsonl 降级记录无 id（不可展开详情） */
  id: number | null;
  ts: number;
  module: string;
  model: string | null;
  total_tokens: number;
  cost_usd: number;
  latency_ms: number | null;
  ok: boolean;
  error: string | null;
  estimated: boolean;
  has_content: boolean;
}

export interface LlmUsageResponse {
  ok: boolean;
  error?: string;
  days?: number;
  today?: LlmUsageSum;
  month?: LlmUsageSum;
  window?: LlmUsageSum;
  by_day?: { day: string; calls: number; total_tokens: number; cost_usd: number }[];
  by_module?: LlmUsageBucket[];
  by_model?: LlmUsageBucket[];
  recent?: LlmUsageRecent[];
  recent_total?: number;
  recent_offset?: number;
  module_filter?: string | null;
  content_retention_days?: number;
  pricing_note?: string;
}

/** 单条调用完整日志（prompt_text 为 messages JSON 字符串，可解析出 role 结构） */
export interface LlmUsageDetail {
  id: number;
  ts: number;
  day: string;
  module: string;
  model: string | null;
  base: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  latency_ms: number | null;
  ok: boolean;
  error: string | null;
  estimated: boolean;
  prompt_text: string | null;
  response_text: string | null;
  prompt_chars: number | null;
  response_chars: number | null;
}

export interface LlmUsageDetailResponse {
  ok: boolean;
  error?: string;
  record?: LlmUsageDetail;
}

export interface StrategyFactor {
  id: string;
  name: string;
  description: string;
}

export interface StrategyGenResult {
  ok: boolean;
  rule?: Record<string, unknown>;
  code?: string;
  name?: string;
  explain?: string;
  reasoning?: string;
  summary?: {
    factors: StrategyFactor[];
    direction: string;
    logic: string;
    stop_loss: string;
    take_profit: string;
  };
  issues?: string[];
  error?: string;
}

export interface StrategyGenState {
  running: boolean;
  started_at: number;
  finished_at: number;
  elapsed_seconds: number;
  params: {
    description: string;
    symbol: string;
    timeframe: string;
  } | null;
  result: StrategyGenResult | null;
  error: string | null;
}

// ─── 策略自动进化 ───
export interface StrategyEvolveMetrics {
  status?: string;
  total_return_pct?: number;
  win_rate?: number;
  profit_factor?: number;
  max_drawdown_pct?: number;
  sharpe_ratio?: number;
  total_trades?: number;
  avg_trade_pnl?: number;
  error?: string | null;
}

export interface StrategyEvolveRound {
  round: number;
  name: string;
  metrics: StrategyEvolveMetrics;
  fitness: number | null;
  ts?: string;
  rule?: Record<string, unknown>;
  code?: string;
  explain?: string;
}

export interface StrategyEvolveRun {
  run_id: string;
  description: string;
  symbol: string;
  timeframe: string;
  start_date?: string;
  end_date?: string;
  status: string;
  rounds: number;
  history: StrategyEvolveRound[];
  best: StrategyEvolveRound | null;
  top3?: StrategyEvolveRound[];
  error?: string | null;
}

export interface StrategyEvolveStatus {
  running: boolean;
  run_id: string | null;
  elapsed_seconds: number;
  error: string | null;
  run: {
    status: string;
    rounds_planned: number;
    rounds_done: number;
    description: string;
    symbol: string;
    timeframe: string;
    history: StrategyEvolveRound[];
    best: StrategyEvolveRound | null;
  } | null;
}

export interface StrategyEvolveRunBrief {
  run_id: string;
  description: string;
  symbol: string;
  timeframe: string;
  status: string;
  rounds_done: number;
  rounds_planned: number;
  best_fitness: number | null;
  updated_at?: string;
}

export interface QdConfigTest {
  ok: boolean;
  healthy?: boolean;
  token_valid?: boolean;
  health_error?: string | null;
  token_error?: string | null;
  whoami?: Record<string, unknown> | null;
  reason?: string;
}

export interface QdTokenIssue {
  ok: boolean;
  agent_token_masked?: string;
  scopes?: string;
  gateway_base?: string;
  reason?: string;
}

export interface BacktestTrade {
  direction?: string;
  side?: string;
  pnl?: number;
  profit?: number;
  net_pnl?: number;
  entry_time?: string;
  exit_time?: string;
  entry_price?: number;
  exit_price?: number;
  balance?: number;
  equity?: number;
  bars_held?: number;
  exit_reason?: string;
  [k: string]: unknown;
}

export interface BacktestResult {
  status: string;
  total_return_pct: number;
  win_rate: number;
  profit_factor: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  total_trades: number;
  avg_trade_pnl: number;
  avg_bars_held: number;
  trades: BacktestTrade[];
  /** 0 成交时后端给出的友好诊断（K 线不足/预热吃光/策略无信号） */
  diagnosis?: string;
  error?: string;
}

export interface BacktestState {
  running: boolean;
  started_at: number;
  finished_at: number;
  elapsed_seconds: number;
  params: {
    name: string;
    symbol: string;
    timeframe: string;
    start: string;
    end: string;
    capital: number;
  } | null;
  result: BacktestResult | null;
  error: string | null;
}

export interface HealthCheckItem {
  ok?: boolean;
  error?: string;
  running?: boolean;
  last_run?: string | null;
  available?: boolean;
  reason?: string;
  started_at?: string;
  finished_at?: string;
  tripped?: boolean;
  should_halt?: boolean;
  drawdown_pct?: number;
  equity_usdt?: number;
  healthy?: boolean;
  [k: string]: unknown;
}

export interface HealthStatus {
  ok: boolean;
  ts: string;
  checks: Record<string, HealthCheckItem>;
  log_buffer_size?: number;
}

export interface TrackRecordResult {
  record: { ok?: boolean; as_of_date?: string; error?: string };
  evaluate: { outcomes_filled?: number; not_due?: number };
}

export interface TrackData {
  report: Record<string, unknown>;
  recent: Record<string, unknown>[];
}

export interface CircuitBreakerStatus {
  ok: boolean;
  evaluation?: Record<string, unknown>;
  state?: { tripped?: boolean; reason?: string; peak_equity?: number };
  error?: string;
}

export interface TradingConfig {
  max_position_pct?: number;
  max_portfolio_risk_pct?: number;
  account_equity_usdt?: number;
  min_conviction?: number;
  intraday_enabled?: boolean;
  intraday_max_open_positions?: number;
}

// ─── 贾维斯信号引擎类型 ───

export type SignalDirection = "bullish" | "bearish" | "neutral";

/** 后端支持的单时间框架 */
export type TwelveTf = "15m" | "1h" | "4h" | "1d";

/** 驾驶舱共识口径："auto" = 多周期综合，其余 = 单周期 */
export type ConsensusScope = TwelveTf | "auto";

export interface KeyLevel {
  label: string;
  price: number;
}

export interface DirectionVotes {
  bullish: number;
  bearish: number;
  neutral: number;
}

/** 单信号交易计划（signal.trade_plan，可能为 null） */
export interface SignalTradePlan {
  /** 多空标识（后端 v2 补充）；旧缓存响应可能缺失，前端由 SL/TP 相对入场价派生兜底 */
  side?: "long" | "short" | null;
  entry: number;
  entry_type: "breakout" | "pullback" | "market";
  stop_loss: number;
  take_profit: number;
  rr?: number | null;
  note?: string;
}

/** 共识级交易计划（consensus.trade_plan，中性/分歧时为 null） */
export interface ConsensusTradePlan {
  /** 多空标识（后端 v2 补充）；旧缓存响应可能缺失，前端由 SL/TP1 相对入场区间派生兜底 */
  side?: "long" | "short" | null;
  entry_zone: [number, number];
  stop_loss: number;
  take_profit_1: number;
  take_profit_2?: number | null;
  rr?: number | null;
  /** RR 门槛（后端 v3：计划保证 rr ≥ 该值，jarvis_config.plan_min_rr 可调） */
  min_rr?: number | null;
  position_pct?: number;
  /** 计划依据的系统名列表 */
  basis?: string[];
  /** 入场区间口径（如「同向 3 系统入场中位数 ±0.3xATR」，后端 v3） */
  entry_basis?: string | null;
  /** 止损锚定依据（如「摆动低点 63200 下方 0.5xATR 缓冲」，后端 v3） */
  sl_basis?: string | null;
  /** 止盈推导口径（结构目标 / RR 门槛推导 + 结构验证，后端 v3） */
  tp_basis?: string | null;
  note?: string | null;
  /** 计划取自哪个时间框架（如 "4h"） */
  source_tf?: string | null;
}

/** 计划状态（后端 v3）：ok=有计划；watch=有方向但 RR/结构不达标（观望，不硬造）；neutral=中性 */
export interface PlanStatus {
  state: "ok" | "watch" | "neutral";
  reason?: string | null;
}

/** 价格动态精度格式化：≥1 两位小数；0.01~1 四位；<0.01 六位有效数字 */
export function formatPrice(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(Number(n))) return "—";
  const v = Number(n);
  if (v === 0) return "0.00";
  const abs = Math.abs(v);
  if (abs >= 1) {
    return v.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }
  if (abs >= 0.01) {
    return v.toLocaleString("en-US", {
      minimumFractionDigits: 4,
      maximumFractionDigits: 4,
    });
  }
  return v.toPrecision(6);
}

export interface TwelveConsensus {
  direction: SignalDirection;
  confidence: number;
  score?: number;
  /** 12 套系统投票分布（和 = 12） */
  votes: DirectionVotes;
  /** 时间框架级投票（15m/1h/4h 各投 1 票） */
  tf_votes?: DirectionVotes;
  layers?: Record<string, unknown>;
  reasoning?: string;
  key_levels?: KeyLevel[];
  tfs?: Record<string, unknown>;
  /** 共识交易计划；中性/分歧时为 null */
  trade_plan?: ConsensusTradePlan | null;
  /** 计划状态与无计划原因（后端 v3；旧缓存响应可能缺失） */
  plan_status?: PlanStatus | null;
}

/** GET /api/twelve/consensus 封套 */
export interface TwelveConsensusResponse {
  ok: boolean;
  symbol?: string;
  price?: number | null;
  tf_available?: string[];
  consensus?: TwelveConsensus | null;
  error?: string;
}

/** 信号静态画像（signal.explain）：类型/触发依据/适用周期/滞后特性 */
export interface SignalExplain {
  type: string;
  trigger: string;
  best_tfs: string[];
  lag: string;
}

export interface TwelveSignal {
  system: string;
  name_cn: string;
  direction: SignalDirection;
  strength: number;
  reasoning?: string;
  key_levels?: KeyLevel[];
  /** 单信号交易计划；无可执行计划时为 null */
  trade_plan?: SignalTradePlan | null;
  /** 静态画像；旧缓存响应可能缺失 */
  explain?: SignalExplain | null;
}

// ─── 单信号级历史胜率统计 ───

/** 某系统某方向的历史触发统计块 */
export interface SignalGradeStats {
  trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  avg_win_pct: number | null;
  /** 均亏（≤0） */
  avg_loss_pct: number | null;
  /** 平均盈亏比 = 均盈 / |均亏| */
  payoff_ratio: number | null;
  /** 期望值 %/笔 */
  expectancy_pct: number;
  /** 最大回撤（持有期最差不利偏移，≤0） */
  max_drawdown_pct: number;
  avg_bars_held: number;
  /** 样本 <30，统计置信度低 */
  low_sample: boolean;
}

export interface SignalWinrateStats {
  symbol: string;
  tf: string;
  days?: number;
  /** 观察期根数（与实时模拟盘时间止损同口径） */
  horizon_bars: number;
  bars: number;
  samples: number;
  systems: Record<
    string,
    { name_cn: string; long: SignalGradeStats | null; short: SignalGradeStats | null }
  >;
  directions: { long: SignalGradeStats | null; short: SignalGradeStats | null };
  computed_at: number;
  error?: string;
}

/** GET /api/twelve/signal-winrate 封套；stats:null = 该 symbol×tf 尚未回测 */
export interface SignalWinrateResponse {
  ok: boolean;
  symbol?: string;
  tf?: string;
  stats: SignalWinrateStats | null;
  error?: string;
}

export interface SignalWinrateStatus {
  ok: boolean;
  running: boolean;
  progress: number;
  detail: string;
  error: string | null;
  result: { total_samples?: number } | null;
}

/** 单信号胜率回测的逐笔样本（K 线图标记历史盈损点用） */
export interface SignalWinrateTrade {
  /** 触发（入场）K 线开盘时间戳 ms */
  t: number;
  /** 出场 K 线开盘时间戳 ms */
  exit_t: number;
  side: "long" | "short";
  entry: number;
  sl: number | null;
  tp: number | null;
  exit_price: number;
  win: boolean;
  pnl_pct: number;
  bars_held: number;
  /** plan = 触 SL/TP 判定；horizon = 满观察期按期末收盘 */
  mode: "plan" | "horizon";
  system?: string;
}

/** GET /api/twelve/signal-winrate/trades 封套 */
export interface SignalWinrateTradesResponse {
  ok: boolean;
  symbol?: string;
  tf?: string;
  system?: string;
  side?: string | null;
  name_cn?: string;
  horizon_bars?: number;
  days?: number;
  computed_at?: number;
  trades?: SignalWinrateTrade[];
  /** true = 缓存缺失或旧版缓存无逐笔明细，需重跑一次胜率回测 */
  need_run?: boolean;
  error?: string;
}

/** GET /api/twelve/signals 封套 */
export interface TwelveSignalsResponse {
  ok: boolean;
  symbol?: string;
  tf?: string;
  price?: number;
  signals: TwelveSignal[];
  consensus?: TwelveConsensus | null;
  error?: string;
}

// ─── 合约仓位与风控计算器（Task #3）───

/** 仓位计算器旋钮（jarvis_config 持久化）：本金/杠杆/风险%(legacy)/保证金% */
export interface PositionCalcConfig {
  poscalc_capital_usdt: number;
  poscalc_leverage: number;
  poscalc_risk_pct: number;
  poscalc_margin_pct: number;
}

/** 止损安全等级：ok=边距充足 / warning=距爆仓过近 / danger=止损在爆仓之外 */
export type SlSafety = "ok" | "warning" | "danger";

export interface PositionAdvice {
  ok: boolean;
  error?: string;
  symbol?: string;
  side?: "long" | "short";
  entry?: number;
  entry_zone?: [number, number];
  capital_usdt?: number;
  leverage?: number;
  /** margin=保证金法（名义=本金×保证金%×杠杆）/ risk=风险法 legacy */
  sizing_mode?: "margin" | "risk";
  /** 保证金法时的保证金占本金%；风险法为 null */
  margin_pct?: number | null;
  /** 保证金法下为派生值（止损触发亏损 ÷ 本金） */
  risk_pct?: number;
  /** 止损触发时的计划亏损额 */
  risk_usdt?: number;
  /** 用户手动入场价生效标记（止损/止盈随之平移） */
  entry_overridden?: boolean;
  sl?: {
    price: number;
    dist_pct: number;
    safety: SlSafety;
    /** 爆仓距离 − 止损距离（负数 = 先爆仓后止损） */
    safety_margin_pct: number;
  };
  liquidation?: {
    price: number;
    dist_pct: number;
    /** 爆仓时损失 ≈ 全部保证金 */
    loss_usdt: number;
  };
  position?: {
    notional_usdt: number;
    margin_usdt: number;
    qty_coin: number;
    /** OKX 口径张数；无面值表的币种为 null */
    contracts: number | null;
    contract_size: number | null;
    capital_used_pct: number;
    capped: boolean;
  };
  take_profits?: { rr: number; price: number; profit_usdt: number }[];
  max_safe_leverage?: number;
  est_fee_usdt?: number;
  warnings?: string[];
  note?: string;
  plan_tp_ref?: number;
  source_tf?: string;
  basis?: string[];
}

/** GET /api/position-calc 封套 */
export interface PositionCalcResponse {
  ok: boolean;
  symbol?: string;
  tf?: string;
  price?: number | null;
  direction?: SignalDirection;
  config?: PositionCalcConfig;
  advice?: PositionAdvice;
  error?: string;
}

export interface JarvisSuggestion {
  action?: string;
  entry_zone?: string;
  stop_loss?: string | number;
  target?: string | number;
  position_pct?: number;
}

export interface JarvisReasonResult {
  direction: SignalDirection;
  confidence: number;
  reasoning_chain: string[];
  risks: string[];
  suggestion?: JarvisSuggestion;
  model?: string;
  degraded?: boolean;
}

/** POST /api/jarvis/reason 封套；ok:false 时 HTTP 仍为 200 */
export interface JarvisReasonResponse {
  ok: boolean;
  symbol?: string;
  market?: Record<string, unknown>;
  consensus?: TwelveConsensus | null;
  reasoning?: JarvisReasonResult;
  cached?: boolean;
  error?: string;
}

export type InsightSeverity = "info" | "warning" | "critical" | string;

export interface JarvisInsight {
  ts: string | number;
  symbol: string;
  kind: string;
  title: string;
  detail?: string;
  severity: InsightSeverity;
}

/** GET /api/jarvis/insights 封套 */
export interface JarvisInsightsResponse {
  ok: boolean;
  insights: JarvisInsight[];
  total?: number;
  error?: string;
}
