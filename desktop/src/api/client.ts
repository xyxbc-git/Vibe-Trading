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

/** 限价单来源（limit_orders.source）：user-created=用户保存交易计划自创 / system=系统创建 */
export type OrderSource = "user-created" | "system";

/**
 * 结构化 API 错误：非 2xx 时保留响应体，业务拦截信息不再丢失。
 * 典型场景：423 冷静期锁单（body 含 reason 剩余分钟/触发原因 + cooldown 结构化字段）。
 * message 维持「API <status>: <文案>」旧格式（有业务 reason 用 reason，否则 statusText），
 * 既有 catch 只展示 e.message 的逻辑不受影响。
 */
export class ApiError extends Error {
  /** HTTP 状态码（423=冷静期锁单 / 400=参数错 …） */
  readonly status: number;
  /** 响应体里的业务原因（reason/detail/error 字段，可能为空） */
  readonly reason: string | null;
  /** 完整响应体（结构化字段如 cooldown 供调用方消费；非 JSON 响应为 null） */
  readonly body: Record<string, unknown> | null;

  constructor(status: number, statusText: string, body: Record<string, unknown> | null) {
    const reason =
      body && typeof body === "object"
        ? String(body.reason ?? body.detail ?? body.error ?? "") || null
        : null;
    super(`API ${status}: ${reason ?? statusText}`);
    this.name = "ApiError";
    this.status = status;
    this.reason = reason;
    this.body = body;
  }
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
      // 先尝试解析响应体：423 冷静期等业务拦截的 reason/cooldown 必须带给调用方
      const body = (await res.json().catch(() => null)) as Record<string, unknown> | null;
      throw new ApiError(res.status, res.statusText, body);
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

  // 活跃持仓（Trading/Dashboard 卡片与仓位统计口径）：只取未平仓。
  // 后端不带 status 默认返回 all（含已平仓），会把已平仓算进「活跃持仓」
  // 与持仓市值/风控仓位数（4/3 超限假象即由此而来）。
  positions: () => api.get<Record<string, unknown>[]>("/positions?status=open"),

  orders: () => api.get<Record<string, unknown>[]>("/orders"),

  // 后端 /api/orders/* 均为 FastAPI query 参数签名（非 JSON body）
  placeOrder: (p: {
    symbol: string;
    side: "buy" | "sell";
    price: number;
    qty: number;
    stopLoss?: number;
    takeProfit?: number;
    /** 订单来源：user-created=用户保存交易计划自创；缺省 system=系统创建 */
    source?: OrderSource;
    /** 附加上下文（如交易计划快照 JSON），落 limit_orders.note */
    note?: string;
  }) => {
    const q = new URLSearchParams({
      symbol: p.symbol,
      side: p.side,
      price: String(p.price),
      qty: String(p.qty),
    });
    if (p.stopLoss != null) q.set("stop_loss", String(p.stopLoss));
    if (p.takeProfit != null) q.set("take_profit", String(p.takeProfit));
    if (p.source) q.set("source", p.source);
    if (p.note) q.set("note", p.note);
    return api.post<{ ok: boolean; order_id?: number; reason?: string; source?: OrderSource }>(
      `/orders/place?${q.toString()}`,
    );
  },

  cancelOrder: (orderId: number) =>
    api.post<{ ok: boolean; order_id?: number; reason?: string }>(
      `/orders/cancel?order_id=${orderId}`,
    ),

  // 平掉该 symbol 的全部未平仓位：POST /api/positions/close?symbol=BTCUSDT
  // behaviorTag 可选：平仓同时打复盘行为标签（T1.4，标签集走配置 journal_tags）
  closePosition: (symbol: string, behaviorTag?: string) =>
    api.post<{ closed: Record<string, unknown>[] }>(
      `/positions/close?symbol=${encodeURIComponent(symbol)}` +
        (behaviorTag ? `&behavior_tag=${encodeURIComponent(behaviorTag)}` : ""),
    ),

  // T1.4 平仓复盘打标/补标；tag 传 null 清除标签
  setBehaviorTag: (positionId: number, tag: string | null) =>
    api.put<{ ok: boolean; position_id?: number; behavior_tag?: string | null; reason?: string }>(
      `/positions/${positionId}/behavior-tag`,
      { tag },
    ),

  // T1.4 成长页「行为标签分布」：按标签统计笔数/胜率/累计盈亏
  behaviorStats: () => api.get<BehaviorStatsResponse>("/journal/behavior-stats"),

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

  /** [M2 s5] 磁吸位（清算/止损密集区）：庄家扫单/插针目标位预判 */
  liqMap: (symbol = "BTCUSDT", tf = "15m") =>
    api.get<LiqMapResponse>(`/liq-map/${encodeURIComponent(symbol)}?tf=${tf}`),

  /** [Sprint1 T1.5] 冷静期状态 + 当日亏损归因摘要 */
  cbCooldown: () => api.get<CooldownResponse>("/circuit-breaker/cooldown"),

  /** [Sprint1 T1.5] 用户「已阅」当日亏损归因（到期解锁前置条件） */
  cbCooldownAck: () =>
    api.post<{ ok: boolean; cooldown?: CooldownState; error?: string }>(
      "/circuit-breaker/cooldown/acknowledge",
    ),

  /** [Sprint1 T1.5] 提前解锁冷静期（调用前必须经用户二次确认） */
  cbCooldownUnlock: () =>
    api.post<{ ok: boolean; cooldown?: CooldownState; reason?: string; error?: string }>(
      "/circuit-breaker/cooldown/unlock",
      { confirm: true },
    ),

  killSwitch: () =>
    request<{ ok: boolean; qd?: unknown; local_cancelled?: unknown[]; error?: string }>(
      "/actions/kill-switch",
      { method: "POST" },
      30_000,
    ),

  /** [Sprint0] 统一配置中心：全量分组视图 + 字段元数据（默认/范围/枚举） */
  configCenter: () => api.get<ConfigCenterResponse>("/config-center"),

  /** [Sprint0] 统一配置中心：批量写扁平键 patch，落 config.yaml 即热生效 */
  updateConfigCenter: (patch: Record<string, unknown>) =>
    api.put<{ ok: boolean; applied?: Record<string, unknown>; version?: number; reason?: string }>(
      "/config-center",
      patch,
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

  // ─── 12 系统信号变更邮件提醒（逐信号开关）───
  signalAlerts: () => api.get<SignalAlertState>("/signal-alerts"),
  updateSignalAlerts: (data: SignalAlertUpdate) =>
    api.put<SignalAlertState>("/signal-alerts", data),

  // ─── 交易订单邮件提醒（按笔配置）───
  // order_id 约定："order-<limit_order_id>" 挂单 / "pos-<position_id>" 持仓
  orderNotifyList: () => api.get<OrderNotifyConfig[]>("/order-notify"),
  orderNotifyGet: (orderId: string) =>
    api.get<{ ok: boolean; config: OrderNotifyConfig | null }>(
      `/order-notify/${encodeURIComponent(orderId)}`,
    ),
  orderNotifySet: (orderId: string, data: OrderNotifyInput) =>
    api.put<{ ok: boolean; reason?: string; config?: OrderNotifyConfig }>(
      `/order-notify/${encodeURIComponent(orderId)}`,
      data,
    ),
  orderNotifyDelete: (orderId: string) =>
    request<{ ok: boolean; deleted?: number }>(
      `/order-notify/${encodeURIComponent(orderId)}`,
      { method: "DELETE" },
    ),
  orderNotifyTest: (orderId: string) =>
    request<{ ok: boolean; reason?: string; to?: string[] }>(
      `/order-notify/${encodeURIComponent(orderId)}/test`,
      { method: "POST", body: JSON.stringify({}) },
      25_000,
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
  // 单系统结构画线几何（缠论笔折线/买卖点箭头/中枢框/关键水平位；K 线结构叠加用）
  twelveStructure: (symbol: string, tf: string, system: string) =>
    api.get<SignalStructureResponse>(
      `/twelve/structure?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}&system=${encodeURIComponent(system)}`,
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

  // ─── 大单流监控 whale tape（M2 s5：aggTrade 分层聚合，WS 未就绪时 active=false）───
  whaleSummary: (symbol?: string) =>
    api.get<WhaleSummaryResponse>(
      "/whale/summary" + (symbol ? `?symbol=${encodeURIComponent(symbol)}` : ""),
    ),

  liquidationSummary: (symbol?: string) =>
    api.get<LiquidationSummary>(
      `/liquidation/summary${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ""}`,
    ),

  /** [M2 s7] Delta 面板 AI 解读（force=1 跳过后端缓存强制重算） */
  deltaAiExplain: (symbol: string, timeframe: string, force = false) =>
    request<DeltaAiExplainResponse>(
      `/delta/ai-explain?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}${force ? "&force=1" : ""}`,
      undefined,
      90_000, // LLM 生成可能较慢，独立超时
    ),

  // ─── 供需情绪综合研判（多空比/资金费率/OI/恐贪 四因子 + 综合分）───
  sentiment: (symbol = "BTCUSDT") =>
    api.get<SentimentResponse>(`/sentiment?symbol=${encodeURIComponent(symbol)}`),

  // ─── 牛熊市体制识别（200D MA / 周线结构 / 长周期动量 / 情绪面）───
  regime: (symbol = "BTCUSDT") =>
    api.get<RegimeResponse>(`/regime?symbol=${encodeURIComponent(symbol)}`),

  // ─── 走势预测（预测引擎；未就绪时调用方回退本地 mock 推演）───
  // 响应契约见 src/lib/predict.ts 的 PredictResponse（规则概率 + AI 研判）
  predict: (symbol: string, timeframe: string, horizon = 16) =>
    request<import("../lib/predict").PredictResponse>(
      `/predict?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&horizon=${horizon}`,
      undefined,
      30_000,
    ),

  // ─── Delta/CVD 订单流（Delta 引擎；未就绪时调用方回退本地 mock 推演）───
  // 响应契约见 src/lib/deltaFlow.ts 的 DeltaResponse（每根 Delta + CVD + 吸收背离）
  delta: (symbol: string, timeframe: string, limit = 200) =>
    request<import("../lib/deltaFlow").DeltaResponse>(
      `/delta?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`,
      undefined,
      30_000,
    ),

  // ─── 高胜率反转四条件叠加评分（Delta 背离 + 多分布 + 三连确认 + 止损扫单）───
  reversalScore: (symbol: string, timeframe: string) =>
    request<ReversalScoreResponse>(
      `/reversal-score?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`,
      undefined,
      30_000,
    ),

  // ─── 信号变更历史（快照/流水/管理界面）───
  twelveSignalHistory: (p: {
    symbol?: string;
    tf?: string;
    system?: string;
    /** unix 秒 */
    since?: number;
    until?: number;
    limit?: number;
    offset?: number;
  }) => {
    const q = new URLSearchParams();
    if (p.symbol) q.set("symbol", p.symbol);
    if (p.tf) q.set("tf", p.tf);
    if (p.system) q.set("system", p.system);
    if (p.since != null) q.set("since", String(p.since));
    if (p.until != null) q.set("until", String(p.until));
    if (p.limit != null) q.set("limit", String(p.limit));
    if (p.offset != null) q.set("offset", String(p.offset));
    return api.get<SignalHistoryResponse>(`/twelve/signal-history?${q.toString()}`);
  },
  twelveSignalHistoryState: (symbol: string, tf: string) =>
    api.get<SignalHistoryStateResponse>(
      `/twelve/signal-history/state?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}`,
    ),
  twelveSignalHistoryDelete: (payload: {
    ids?: number[];
    symbol?: string;
    tf?: string;
    system?: string;
    /** 删除早于该时间（unix 秒）的记录 */
    before?: number;
  }) =>
    api.post<{ ok: boolean; deleted?: number; error?: string }>(
      "/twelve/signal-history/delete",
      payload,
    ),

  // ─── 盘口深度透视（REST 快照聚合 DOM 阶梯）───
  depthOrderbook: (symbol: string, limit = 500, maxBuckets = 30) =>
    api.get<DepthOrderbookResponse>(
      `/depth/orderbook?symbol=${encodeURIComponent(symbol)}&limit=${limit}&max_buckets=${maxBuckets}`,
    ),

  // ─── 成交流主体画像（散户/机构/做市商 + 指纹聚合 + 主力行为判定）───
  tapeFlow: (symbol: string, windowMin = 15) =>
    api.get<TapeFlowResponse>(
      `/tape/flow?symbol=${encodeURIComponent(symbol)}&window_min=${windowMin}`,
    ),

  // ─── 成交流 K 线柱（主动买卖额按周期聚合，盘口页柱状图视图）───
  tapeBars: (symbol: string, interval = "1m", limit = 200) =>
    api.get<TapeBarsResponse>(
      `/tape/bars?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=${limit}`,
    ),

  // ─── 成交流足迹图（每柱按价格档拆分买卖额 + 主体多空统计，盘口页足迹图视图）───
  tapeFootprint: (symbol: string, interval = "1m", limit = 30, buckets = 40) =>
    api.get<TapeFootprintResponse>(
      `/tape/footprint?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=${limit}&buckets=${buckets}`,
    ),

  // ─── 资金费率套利模拟盘（现货多 + 永续空，delta 中性赚费率）───
  fundingArbOpportunities: () =>
    request<FundingArbOpportunitiesResponse>("/funding-arb/opportunities", undefined, 30_000),

  fundingArbPositions: (status: "all" | "open" | "closed" = "all") =>
    request<FundingArbPositionsResponse>(
      `/funding-arb/positions?status=${status}`,
      undefined,
      30_000,
    ),

  fundingArbOpen: (symbol: string, capital: number) =>
    api.post<FundingArbOpenResponse>(
      `/funding-arb/positions/open?symbol=${encodeURIComponent(symbol)}&capital=${capital}`,
    ),

  fundingArbClose: (positionId: number) =>
    api.post<FundingArbCloseResponse>(
      `/funding-arb/positions/close?position_id=${positionId}`,
    ),

  fundingArbPnl: () =>
    request<FundingArbPnlResponse>("/funding-arb/pnl", undefined, 30_000),
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
  /** 24h 价格涨跌幅（情绪因子里与 OI 交叉判断量价健康度） */
  price_24h?: {
    symbol: string;
    last_price: number;
    change_pct: number;
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

// ─── GET /api/delta/ai-explain Delta 面板 AI 解读（M2 s7） ───

/** 结构化解读四段（LLM 与规则降级轨同构，一套渲染） */
export interface DeltaAiExplainBody {
  /** 一句话结论 */
  headline: string;
  /** 买卖力量对比（基于 Delta 正负与 CVD 趋势） */
  power: string;
  /** 关键信号（背离/吸收现象；无异常时说明常态） */
  signals: string;
  /** 建议倾向（观望/等回调/等突破 + 盈亏比提醒） */
  suggestion: string;
}

export interface DeltaAiExplainResponse {
  ok: boolean;
  symbol?: string;
  timeframe?: string;
  /** llm=大模型生成 / rule=规则引擎降级（LLM 未配置或失败） */
  source?: "llm" | "rule";
  explain?: DeltaAiExplainBody;
  /** 聚合上下文摘要（调试/透明度展示用） */
  context_digest?: {
    price?: number | null;
    cvd_trend?: "up" | "down" | "flat";
    has_divergence?: boolean;
    absorption?: boolean;
  };
  /** 生成时间（unix 秒） */
  generated_at?: number;
  /** true=命中后端 TTL 缓存（signal.ai_explain_cache_min） */
  cached?: boolean;
  /** 配置开关关闭（signal.ai_explain_enabled=false），前端隐藏入口 */
  disabled?: boolean;
  disclaimer?: string;
  error?: string;
}

// ─── GET /api/liquidation/summary 爆仓流面板（M2 s5） ───
// （Magnet / LiqMapResponse 类型统一定义在下方「磁吸位 liq map」段，全文件共用一份）

/** 归一化爆仓事件（side_liquidated：long=多头被强平 / short=空头被强平） */
export interface LiquidationEvent {
  symbol: string;
  side: "BUY" | "SELL";
  side_liquidated: "long" | "short";
  price: number;
  qty: number;
  /** 单笔名义金额（USDT） */
  notional: number;
  /** 毫秒时间戳 */
  trade_time: number;
}

/** 爆仓簇（短时同向密集强平=行情加速/磁吸位信号） */
export interface LiquidationCluster {
  side_liquidated: "long" | "short";
  count: number;
  total_usd: number;
  /** 秒时间戳 */
  start_ts: number;
  end_ts: number;
  symbols: string[];
  note: string;
}

export interface LiquidationSummary {
  ok: boolean;
  symbol?: string | null;
  window_min?: number;
  as_of?: number;
  stats?: {
    long_usd: number;
    short_usd: number;
    long_count: number;
    short_count: number;
    total_usd: number;
    /** +1=全是多头爆仓（下跌加速）… -1=全是空头爆仓 */
    dominance: number;
    series: { ts: number; long_usd: number; short_usd: number }[];
  };
  large?: LiquidationEvent[];
  clusters?: LiquidationCluster[];
  thresholds?: {
    large_usd: number;
    cluster_window_s: number;
    cluster_min_count: number;
  };
  /** forceOrder 实时流降级中（代理丢弃合约域帧/WS 未运行），数据为历史库回退 */
  degraded?: boolean;
  /** 降级引导文案（提示把 fstream.binance.com 加入代理放行名单） */
  guidance?: string | null;
  history_rows?: number;
  error?: string;
}

// ─── GET /api/sentiment 供需情绪研判 ───

export type SentimentBias = "bullish" | "bearish" | "neutral";

/** 单因子研判（多空比/大户背离/资金费率/OI/恐贪 + 预留的爆仓/链上接口位） */
export interface SentimentFactor {
  key:
    | "long_short"
    | "top_divergence"
    | "funding"
    | "oi"
    | "fng"
    | "liquidations"
    | "onchain";
  name: string;
  /** false = 数据源未接入/暂不可用，不参与计分（灰化展示） */
  available: boolean;
  value: number | null;
  display: string;
  bias: SentimentBias;
  /** -100～+100，正=偏多 */
  score: number;
  weight: number;
  /** 为什么看多/看空的解释性文案 */
  note: string;
}

/** 大户 vs 全网多空比背离摘要（T1.6，MarketIntel 背离小卡直取） */
export interface TopDivergenceSummary {
  /** 大户多空比数据是否可用（false=接口未返回 top_* 字段，卡片灰化） */
  available: boolean;
  /** 是否触发背离信号（反向 + 占比差超阈值） */
  active: boolean;
  top_bias: SentimentBias;
  /** 全网（散户主导）口径方向 */
  retail_bias: SentimentBias;
  top_long_pct: number | null;
  global_long_pct: number | null;
  /** 大户与全网多头占比差（百分点） */
  diff_pp: number | null;
  /** 触发阈值（百分点，jarvis_config signal.divergence_threshold × 100） */
  threshold_pp: number;
  score: number;
  note: string;
  /** 建议倾向文案（跟随大户方向 / 无背离说明） */
  suggestion: string;
}

export interface SentimentResponse {
  ok: boolean;
  symbol?: string;
  /** 综合情绪分 -100～+100（可用因子加权平均） */
  score?: number;
  bias?: SentimentBias;
  /** 一句话结论：综合偏置 + 主导因子 */
  headline?: string;
  factors?: SentimentFactor[];
  /** 极端因子警示（拥挤/逆向风险） */
  warnings?: string[];
  /** 情绪极端时的止盈止损收紧建议（仅建议展示，不强制改单） */
  sl_tp_advice?: string | null;
  /** 大户 vs 全网背离状态（T1.6 小卡） */
  top_divergence?: TopDivergenceSummary | null;
  as_of?: number;
  intel_updated_at?: number | null;
  unavailable?: Record<string, string> | null;
  error?: string;
}

// ─── GET /api/regime 牛熊市体制识别 ───

export type MarketRegime = "bull" | "bear" | "range";

/** 体制因子（结构与 SentimentFactor 同款，渲染逻辑可复用） */
export interface RegimeFactor {
  key: "ma200" | "weekly" | "momentum" | "sentiment" | "onchain";
  name: string;
  /** false = 数据不足/源未接入，不参与计分（灰化展示） */
  available: boolean;
  value: number | null;
  display: string;
  bias: SentimentBias;
  /** -100～+100，正=偏牛 */
  score: number;
  weight: number;
  note: string;
}

export interface RegimeResponse {
  ok: boolean;
  symbol?: string;
  regime?: MarketRegime;
  /** 中文判定（含震荡市偏牛/偏熊后缀） */
  regime_cn?: string;
  /** 综合体制分 -100～+100（可用因子加权平均） */
  score?: number;
  /** 0~0.95：判定强度 × 因子覆盖率 */
  confidence?: number;
  /** 一句话结论：主导因子 + 判定 */
  headline?: string;
  factors?: RegimeFactor[];
  disclaimer?: string;
  updatedAt?: number;
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

// ─── 12 系统信号变更邮件提醒（逐信号开关）───

export interface SignalAlertSub {
  symbol: string;
  tf: string;
  system: string;
  enabled: boolean;
  last_sent_at: number | null;
  sent_count: number;
}

export interface SignalAlertState {
  ok: boolean;
  error?: string;
  enabled: boolean;
  /** 全局收件邮箱；空 = 未配置（回退价位提醒通讯录） */
  email: string;
  cooldown_s: number;
  daily_limit: number;
  today_sent: number;
  subs: SignalAlertSub[];
}

export interface SignalAlertUpdate {
  /** 单个开关（信号卡铃铛） */
  sub?: { symbol: string; tf: string; system: string; enabled: boolean };
  /** 批量关闭（提醒页） */
  subs_off?: { symbol: string; tf: string; system: string }[];
  email?: string;
  enabled?: boolean;
  cooldown_s?: number;
  daily_limit?: number;
}

// ─── 交易订单邮件提醒（按笔配置）───

export interface OrderNotifyConfig {
  order_id: string;
  email: string;
  notify_take_profit: boolean;
  notify_stop_loss: boolean;
  created_at: number;
  updated_at: number;
  last_notified_at: number | null;
  last_notify_type: string | null;
  last_send_result: string | null;
}

export interface OrderNotifyInput {
  email: string;
  notify_take_profit: boolean;
  notify_stop_loss: boolean;
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

// ─── [Sprint1 T1.5] 熔断冷静期 ───

export interface CooldownState {
  active: boolean;
  until_ts: number | null;
  remaining_s: number;
  acknowledged: boolean;
  expired?: boolean;
  reason: string | null;
}

export interface LossAttribution {
  date: string;
  closed_trades: number;
  total_pnl_usdt: number;
  wins: number;
  losses: number;
  by_reason: { reason: string; count: number; pnl_usdt: number }[];
  by_symbol: { symbol: string; count: number; pnl_usdt: number }[];
  worst_trades: {
    symbol: string;
    side: string;
    entry: number | null;
    exit: number | null;
    pnl_usdt: number;
    reason: string | null;
  }[];
  error?: string;
}

export interface CooldownResponse {
  ok: boolean;
  cooldown?: CooldownState;
  attribution?: LossAttribution;
  error?: string;
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

// ─── [Sprint0] 统一配置中心（config.yaml）───

export type ConfigGroupName = "trading" | "risk" | "signal" | "data" | "notify" | "system";

export interface ConfigFieldMeta {
  group: string;
  default: unknown;
  type: "bool" | "int" | "float" | "list" | "str";
  min?: number;
  max?: number;
  enum?: string[];
}

export interface ConfigCenterResponse {
  groups: Record<ConfigGroupName, Record<string, unknown>>;
  group_comments: Record<string, string>;
  fields: Record<string, ConfigFieldMeta>;
  meta: { version?: number; updated_at?: string; source?: string };
  yaml_path: string;
}

// ─── 贾维斯信号引擎类型 ───

export type SignalDirection = "bullish" | "bearish" | "neutral";

/** 后端支持的单时间框架 */
export type TwelveTf = "5m" | "15m" | "30m" | "1h" | "4h" | "1d";

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

/** 「安全带」确认层（jarvis_seatbelt.apply_to_consensus 附加）：
 *  Delta 吸收背离 × 信号方向 → confirmed=吸收证据确认 / no-evidence=无证据
 *  谨慎 / conflict=反向背离顶撞 / idle=中性 / unavailable=Delta 引擎未就绪 */
export interface ConsensusSeatbelt {
  status: "confirmed" | "no-evidence" | "conflict" | "idle" | "unavailable";
  status_cn: string;
  /** 背离强度档（confirmed/conflict 时给） */
  grade?: "strong" | "moderate" | "weak" | null;
  /** 确认层对置信度的修正量（confirmed 为正、conflict 为负） */
  confidence_delta: number;
  /** 修正后的建议置信度（原 confidence 字段保持不变） */
  adjusted_confidence: number;
  note: string;
  divergence_note?: string | null;
  absorption?: { detected: boolean; side?: string; note?: string } | null;
  /** 大单流可选因子（M2 s5 whale tape；开关 signal.whale_seatbelt_enabled，无数据缺省） */
  whale?: SeatbeltWhaleCheck | null;
  /** [M2 s5] 磁吸位提醒：现价逼近清算/止损密集区（开关 signal.liq_map_seatbelt_enabled） */
  magnet_warning?: { near: boolean; magnet: Magnet | null; note: string } | null;
}

/** 共识上的供需情绪叠加层（jarvis_sentiment.apply_to_consensus 附加） */
export interface ConsensusSentimentOverlay {
  score: number;
  bias: SentimentBias;
  /** aligned=同向共振 / divergent=背离 / neutral=不构成修正 */
  alignment: "aligned" | "divergent" | "neutral";
  /** 情绪层对置信度的修正量（同向为正、极端背离为负） */
  confidence_delta: number;
  /** 修正后的建议置信度（原 confidence 字段保持不变） */
  adjusted_confidence: number;
  headline?: string;
  warnings?: string[];
  sl_tp_advice?: string | null;
  factors?: SentimentFactor[];
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
  /** 供需情绪叠加层（多空比/费率/OI/恐贪；旧缓存响应可能缺失） */
  sentiment?: ConsensusSentimentOverlay | null;
  /** 「安全带」确认层（Delta 吸收背离；旧缓存响应可能缺失） */
  seatbelt?: ConsensusSeatbelt | null;
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
  /** 该信号最近一次计算时间（unix 秒；旧后端缺失） */
  updated_at?: number | null;
  /** 该信号最近一次实质变更时间（unix 秒；从未变更/旧后端为 null） */
  last_change_at?: number | null;
}

// ─── 信号变更历史（GET /api/twelve/signal-history*）───

/** 单条变更流水（prev/new_json 为变更前后完整参数快照） */
export interface SignalChangeRow {
  id: number;
  /** unix 秒 */
  ts: number;
  symbol: string;
  tf: string;
  system: string;
  name_cn: string | null;
  prev_direction: SignalDirection | null;
  new_direction: SignalDirection;
  prev_strength: number | null;
  new_strength: number;
  /** 变更类型：direction / strength / plan / levels */
  change_kinds: string[] | null;
  summary: string;
  prev_json: {
    direction?: string | null;
    strength?: number | null;
    trade_plan?: SignalTradePlan | null;
    key_levels?: KeyLevel[];
  } | null;
  new_json: {
    direction?: string;
    strength?: number;
    trade_plan?: SignalTradePlan | null;
    key_levels?: KeyLevel[];
    reasoning?: string;
  } | null;
  price: number | null;
}

export interface SignalHistoryResponse {
  ok: boolean;
  total: number;
  rows: SignalChangeRow[];
  error?: string;
}

export interface SignalHistoryStateRow {
  system: string;
  name_cn: string | null;
  direction: SignalDirection;
  strength: number;
  updated_ts: number | null;
  changed_ts: number | null;
}

// ─── 信号结构画线（GET /api/twelve/structure）───
// ts 均为 bar 开盘 epoch 秒，与 api.kline rows 的 ts/1000 同源对齐

/** 结构折线（缠论笔/线段等）：按时间升序的点列 */
export interface StructurePolyline {
  points: { ts: number; price: number }[];
  color?: string;
  style?: "solid" | "dashed";
  width?: number;
  label?: string;
}

/** 结构标注（买卖点箭头等）：锚定单根 bar，由图表原生 marker 渲染 */
export interface StructureMarker {
  ts: number;
  price?: number;
  position: "above" | "below";
  shape: "arrow_up" | "arrow_down" | "circle" | "square";
  color?: string;
  text?: string;
}

/** 结构区域（中枢框等）：时间 × 价格矩形，渲染为上下两条边缘虚线 */
export interface StructureBox {
  ts1: number;
  ts2: number;
  price_lo: number;
  price_hi: number;
  color?: string;
  label?: string;
}

/** 结构关键水平位（与 KeyLevel 通道同风格渲染） */
export interface StructureHLine {
  price: number;
  color?: string;
  label?: string;
  style?: string;
}

export interface StructureDrawings {
  polylines: StructurePolyline[];
  markers: StructureMarker[];
  boxes: StructureBox[];
  hlines: StructureHLine[];
}

/** GET /api/twelve/structure 封套；无几何的系统 drawings 各数组为空 */
export interface SignalStructureResponse {
  ok: boolean;
  symbol?: string;
  tf?: string;
  system?: string;
  name_cn?: string;
  direction?: SignalDirection;
  as_of?: number;
  drawings?: StructureDrawings;
  error?: string;
}

export interface SignalHistoryStateResponse {
  ok: boolean;
  rows: SignalHistoryStateRow[];
  error?: string;
}

// ─── 盘口深度透视（GET /api/depth/orderbook）───

/** 单个价格桶（qty=币量 usd=名义额 cum_usd=向外累计深度） */
export interface DepthBucket {
  price: number;
  qty: number;
  usd: number;
  cum_usd: number;
}

export interface DepthOrderbookResponse {
  ok: boolean;
  symbol?: string;
  /** futures=合约簿 / spot=现货簿（合约域不可达时回退） */
  market?: "futures" | "spot";
  ts?: number;
  mid?: number;
  best_bid?: number | null;
  best_ask?: number | null;
  spread_pct?: number | null;
  /** 价格桶宽 */
  bucket?: number;
  /** 买盘（价格降序） */
  bids?: DepthBucket[];
  /** 卖盘（价格升序） */
  asks?: DepthBucket[];
  imbalance?: { bid_usd_10: number; ask_usd_10: number; ratio: number | null };
  /** true = 本次快照拉取失败，返回的是上一次成功结果 */
  stale?: boolean;
  error?: string;
}

// ─── 成交流主体画像（GET /api/tape/flow）───

export type TapeActor = "retail" | "mid" | "inst" | "maker";

export interface TapeActorStat {
  actor: TapeActor;
  actor_cn: string;
  usd: number;
  buy_usd: number;
  sell_usd: number;
  net_usd: number;
  n: number;
  pct: number;
}

/** 数量指纹聚合组（同一数量反复成交 = 疑似同一主体拆单） */
export interface TapeFingerprint {
  fp: string;
  qty: number;
  n: number;
  buy_n: number;
  sell_n: number;
  avg_usd: number;
  total_usd: number;
  net_usd: number;
  cls: TapeActor;
  cls_cn: string;
  first_ts: number;
  last_ts: number;
  last_price: number;
}

export interface TapeTrade {
  ts_ms: number;
  price: number;
  qty: number;
  usd: number;
  is_buy: boolean;
  fp: string;
  tier: "retail" | "mid" | "whale";
  cls: TapeActor;
  cls_cn: string;
  /** 同指纹累计笔数 */
  fp_n: number;
}

export interface TapeVerdict {
  dominant: TapeActor;
  dominant_cn: string;
  non_retail_share_pct: number;
  inst_net_usd: number;
  /** 砸盘 / 拉盘/操盘 / 吸筹 / 派发/出货 / 中性 */
  action: string;
  note: string;
  burst: { side: "buy" | "sell"; usd: number; note: string } | null;
  /** 砸盘力度减弱时的入场时机提示 */
  entry_hint: string | null;
}

export interface TapeFlowResponse {
  ok: boolean;
  ws_ready?: boolean;
  symbol?: string;
  active?: boolean;
  window_min?: number;
  price_change_pct?: number | null;
  breakdown?: { total_usd: number; actors: Record<TapeActor, TapeActorStat> };
  verdict?: TapeVerdict;
  fingerprints?: TapeFingerprint[];
  recent?: TapeTrade[];
  series?: {
    ts: number;
    buy: number;
    sell: number;
    nr_buy: number;
    nr_sell: number;
    price: number;
  }[];
  tier1_usd?: number;
  retail_max_usd?: number;
  disclaimer?: string;
  error?: string;
}

// ─── 成交流 K 线柱（GET /api/tape/bars）───

/** 单根成交流柱：周期内主动买/卖名义额聚合 + 非散户口径 + 价格 OHLC */
export interface TapeBar {
  /** 柱开始时间（unix 秒） */
  ts: number;
  /** 全部主动买入额（USD） */
  buy: number;
  /** 全部主动卖出额（USD） */
  sell: number;
  /** buy - sell */
  net: number;
  /** 非散户（单笔 ≥ 阈值）买入额 */
  nr_buy: number;
  /** 非散户卖出额 */
  nr_sell: number;
  /** nr_buy - nr_sell */
  nr_net: number;
  open: number;
  high: number;
  low: number;
  close: number;
  /** 周期内成交笔数 */
  trades: number;
}

export interface TapeBarsResponse {
  ok: boolean;
  symbol?: string;
  interval?: string;
  bars?: TapeBar[];
  /** 数据来源标注（如 ws_agg / kline_approx） */
  source?: string;
  error?: string;
}

// ─── 成交流足迹图（GET /api/tape/footprint）───

/** 足迹图单价格档：该价位主动买/卖名义额 + 失衡标记 */
export interface FootprintRow {
  price: number;
  buy: number;
  sell: number;
  /** buy_imb=买方失衡 / sell_imb=卖方失衡 / null=正常 */
  flag: "buy_imb" | "sell_imb" | null;
}

/** 足迹图单柱：OHLC + 汇总统计 + 价格档明细（price 降序） */
export interface FootprintBar {
  /** 柱开始时间（unix 秒） */
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  /** 周期内总成交额（USD） */
  total: number;
  buy: number;
  sell: number;
  /** buy - sell */
  delta: number;
  /** 窗口内累计 delta */
  cvd: number;
  rows: FootprintRow[];
}

/** 单主体多空口径统计（taker 主动方向启发式，非真实持仓） */
export interface FootprintActorStat {
  buy: number;
  sell: number;
  net: number;
  /** 主动买占比 0~100（做多情绪代理） */
  long_pct: number;
  verdict_cn: string;
}

export interface FootprintActors {
  retail: FootprintActorStat;
  mid: FootprintActorStat;
  inst: FootprintActorStat;
  maker: FootprintActorStat;
  overall: { buy: number; sell: number; delta: number; verdict_cn: string };
}

export interface TapeFootprintResponse {
  ok: boolean;
  symbol?: string;
  interval?: string;
  /** 价格桶宽（rows 价格按此步长对齐） */
  bucket?: number;
  bars?: FootprintBar[];
  actors?: FootprintActors;
  /** false = WS 数据未积累，暂无足迹 */
  active?: boolean;
  source?: string;
  disclaimer?: string;
  error?: string;
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

// ─── 高胜率反转四条件叠加评分（/api/reversal-score）───

/** 单个反转条件的判定结果 */
export interface ReversalCondition {
  met: boolean;
  note: string;
  /** 上游数据源未就绪（不计入 met，UI 显示 ⚪ 态） */
  unavailable?: boolean;
}

export interface ReversalScoreResponse {
  ok: boolean;
  error?: string;
  symbol?: string;
  timeframe?: string;
  direction: "bullish" | "bearish" | "none";
  conditions: {
    delta_divergence: ReversalCondition;
    multi_distribution: ReversalCondition;
    triple_confirm: ReversalCondition;
    stop_hunt: ReversalCondition;
  };
  satisfied: number;
  maxScore: number;
  verdict: "high-probability" | "watch" | "no-signal";
  note: string;
  updatedAt?: string;
  mock?: boolean;
  disclaimer?: string;
}

// ─── 合约仓位与风控计算器（Task #3）───

/** 仓位计算器旋钮（jarvis_config 持久化）：本金/杠杆/风险%(legacy)/保证金% */
export interface PositionCalcConfig {
  poscalc_capital_usdt: number;
  poscalc_leverage: number;
  poscalc_risk_pct: number;
  poscalc_margin_pct: number;
  /** [Sprint1 T1.1] 只读：超过该杠杆保存前必须二次确认（改动走配置中心） */
  max_leverage_no_confirm?: number;
  /** [Sprint1 T1.1] 只读：下单链路默认杠杆安全基线 */
  default_leverage?: number;
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

// ─── 资金费率套利模拟盘 ───

/** 单币套利机会（GET /api/funding-arb/opportunities 列表项） */
export interface FundingArbOpportunity {
  symbol: string;
  mark_price: number;
  /** 当期 8h 费率（小数，0.0001 = 0.01%） */
  funding_rate: number;
  funding_rate_pct: number;
  /** 当期费率年化 %（×3×365） */
  apr_now: number;
  /** 7 日均费率年化 %；历史拉取失败为 null */
  apr_7d: number | null;
  /** 下次结算 unix 秒 */
  next_funding_ts: number;
  /** 往返手续费回本天数；费率 ≤0 为 null */
  break_even_days: number | null;
  fee_roundtrip_pct: number;
  /** 费率为负等风险提示；无风险为 null */
  warning: string | null;
}

export interface FundingArbOpportunitiesResponse {
  ok: boolean;
  generated_ts?: number;
  opportunities: FundingArbOpportunity[];
  basis?: string;
  disclaimer?: string;
  error?: string;
}

/** 套利持仓（open 态附实时字段；closed 态附平仓结算字段） */
export interface FundingArbPosition {
  id: number;
  symbol: string;
  qty: number;
  capital_usdt: number;
  spot_entry: number;
  perp_entry: number;
  opened_ts: number;
  opened_at?: string;
  status: "open" | "closed";
  funding_accrued_usdt: number;
  settle_count: number;
  fees_usdt: number;
  note?: string | null;
  // open 态实时字段
  held_days?: number;
  funding_apr_pct?: number;
  net_exposure_pct?: number;
  current_funding_rate?: number | null;
  next_funding_ts?: number | null;
  mark_price?: number | null;
  warning?: string | null;
  // closed 态结算字段
  closed_ts?: number | null;
  closed_at?: string | null;
  spot_exit?: number | null;
  perp_exit?: number | null;
  basis_pnl_usdt?: number | null;
  total_pnl_usdt?: number | null;
}

export interface FundingArbPositionsResponse {
  ok: boolean;
  positions: FundingArbPosition[];
  disclaimer?: string;
  error?: string;
}

export interface FundingArbOpenResponse {
  ok: boolean;
  position_id?: number;
  symbol?: string;
  qty?: number;
  capital_usdt?: number;
  spot_entry?: number;
  perp_entry?: number;
  open_fee_usdt?: number;
  current_funding_rate?: number;
  warning?: string | null;
  error?: string;
}

export interface FundingArbCloseResponse {
  ok: boolean;
  position_id?: number;
  symbol?: string;
  funding_accrued_usdt?: number;
  basis_pnl_usdt?: number;
  fees_usdt?: number;
  total_pnl_usdt?: number;
  held_days?: number;
  realized_apr_pct?: number;
  error?: string;
}

export interface FundingArbPnlResponse {
  ok: boolean;
  open?: {
    count: number;
    capital_usdt: number;
    funding_accrued_usdt: number;
    funding_apr_pct: number;
  };
  closed?: {
    count: number;
    funding_accrued_usdt: number;
    total_pnl_usdt: number;
  };
  all_time_funding_usdt?: number;
  basis?: string;
  disclaimer?: string;
  error?: string;
}

// ─── 磁吸位 liq map（GET /api/liq-map/{symbol}，M2 s5）───

/** 单个磁吸簇（清算/止损密集区，jarvis_liq_map._fmt 输出） */
export interface Magnet {
  kind: "long_liq" | "short_liq" | "stop_cluster";
  side: "above" | "below";
  price_mid: number;
  price_low: number;
  price_high: number;
  /** 同类簇内归一强度 0~1 */
  strength: number;
  /** forceOrder 校准置信度（未校准基线 0.5） */
  confidence: number;
  /** 距现价 %（正=上方） */
  dist_pct: number;
  label: string;
}

export interface LiqMapResponse {
  ok: boolean;
  symbol?: string;
  timeframe?: string;
  price?: number;
  /** 合并视图（按距现价排序）；数据不足为空数组 */
  magnets?: Magnet[];
  liq_clusters?: Magnet[];
  stop_clusters?: Magnet[];
  calibration?: Record<string, unknown> | null;
  oi_notional_usdt?: number | null;
  note?: string;
  error?: string;
}

// ─── 大单流监控 whale tape（GET /api/whale/summary）───

/** 大单流异常事件：single_super=单笔巨单 / consecutive=同向连续 / divergence=量价背离 */
export interface WhaleEvent {
  ts_ms: number;
  kind: "single_super" | "consecutive" | "divergence";
  side: "buy" | "sell";
  note: string;
  usd: number;
  price: number;
}

/** 单币种窗口统计；active=false 表示 WS 未就绪/暂无该币数据（前端占位态） */
export interface WhaleSymbolSummary {
  symbol: string;
  active: boolean;
  window_min: number;
  /** 窗口大单净流（买-卖，USD；正=净买入） */
  net_usd: number;
  buy_usd: number;
  sell_usd: number;
  /** 大单买卖额比（无卖单时为 null） */
  buy_sell_ratio: number | null;
  /** 大单成交额占窗口总成交额 % */
  whale_share_pct: number;
  whale_n: number;
  total_usd: number;
  price_change_pct: number | null;
  /** 窗口级量价背离（无背离为 null） */
  divergence: { side: "sell_into_buys" | "buy_into_sells"; note: string } | null;
  recent_whales: { ts_ms: number; price: number; usd: number; is_buy: boolean }[];
  events: WhaleEvent[];
  tier1_usd: number;
  tier2_usd: number;
}

export interface WhaleSummaryResponse {
  ok: boolean;
  ws_ready?: boolean;
  window_min?: number;
  tier1_usd?: number;
  tier2_usd?: number;
  symbols?: Record<string, WhaleSymbolSummary>;
  disclaimer?: string;
  error?: string;
}

/** 安全带大单流可选因子（consensus.seatbelt.whale；WS 无数据时缺省） */
export interface SeatbeltWhaleCheck {
  status: "against" | "aligned" | "idle";
  net_usd: number;
  window_min: number | null;
  note: string;
}

// ─── T1.4 平仓复盘行为标签统计（GET /api/journal/behavior-stats）───

export interface BehaviorTagBucket {
  tag: string;
  trades: number;
  wins: number;
  win_rate_pct: number | null;
  pnl_usdt: number;
}

export interface BehaviorStatsResponse {
  ok: boolean;
  total_closed?: number;
  buckets?: BehaviorTagBucket[];
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
