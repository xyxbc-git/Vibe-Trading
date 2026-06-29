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

  ledger: () => api.get<Record<string, unknown>[]>("/ledger"),

  traderStatus: () => api.get<Record<string, unknown>>("/trader/status"),

  ask: (question: string, symbol = "BTCUSDT") =>
    api.post<{ answer: string }>("/ask", { question, symbol }),

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

  // ─── 价位邮件提醒 ───
  alertConfig: () => api.get<AlertConfig>("/alerts/config"),
  updateAlertConfig: (data: AlertConfigUpdate) =>
    api.put<{ ok: boolean; reason?: string; config?: AlertConfig }>(
      "/alerts/config",
      data,
    ),
  testAlertEmail: (recipients?: string[]) =>
    request<{ ok: boolean; reason?: string; to?: string[] }>(
      "/alerts/test-email",
      { method: "POST", body: JSON.stringify({ recipients }) },
      25_000,
    ),
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
};

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
  poll_interval_s: number;
  monitor: {
    running: boolean;
    last_run: string | null;
    last_summary: { checked: number; triggered: number } | null;
    last_error: string | null;
  };
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
