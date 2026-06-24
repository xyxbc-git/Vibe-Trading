const BASE_URL = "/api";

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
  evolveStart: (rounds = 10) =>
    api.post<Record<string, unknown>>("/evolve/start", { rounds }),
  evolveGraveyard: () => api.get<Record<string, unknown>[]>("/evolve/graveyard"),
  evolveHallOfFame: () =>
    api.get<Record<string, unknown>[]>("/evolve/hall-of-fame"),
  growthTimeline: () => api.get<Record<string, unknown>[]>("/growth/timeline"),
  growthMilestones: () =>
    api.get<Record<string, unknown>[]>("/growth/milestones"),
  growthStats: () => api.get<Record<string, unknown>>("/growth/stats"),
  config: () => api.get<Record<string, unknown>>("/config"),
  updateConfig: (data: Record<string, unknown>) =>
    api.put<Record<string, unknown>>("/config", data),
  marketOverview: () => api.get<Record<string, unknown>>("/market/overview"),
};
