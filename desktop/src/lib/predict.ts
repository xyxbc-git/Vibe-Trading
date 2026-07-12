// 走势预测（K 线预测可视化层）：数据契约类型 + 本地 mock 推演 + 图表 overlay
// 换算，全部纯函数（无 IO、无图表依赖），渲染交给 PredictionPrimitive。
//
// 契约来源：预测引擎 GET /api/predict?symbol=&timeframe=&horizon=（MCP-5）。
// 引擎未就绪 / 请求失败时，mockPredict 用最近 K 线的动量 + ATR 做本地推演：
// 同一批 K 线输入恒产出相同结果（generatedAt 取最后一根 K 线时刻、扰动用
// 正弦而非随机数），轮询刷新幂等、单测可断言；mock:true 标记让 UI 显示
// 「演示数据」角标，避免演示推演被误当真实研判。

export type PredictDirection = "up" | "down" | "sideways";

export interface PredictPathPoint {
  /** ISO8601 时间戳（未来第 N 根 bar 的收盘时刻） */
  t: string;
  price: number;
}

/** GET /api/predict 响应（MCP-5 数据契约；ok/error 为项目封套惯例，可缺省） */
export interface PredictResponse {
  ok?: boolean;
  symbol: string;
  timeframe: string;
  generatedAt: string;
  /** 预测覆盖的未来 bar 数 */
  horizon: number;
  direction: PredictDirection;
  probability: { up: number; down: number; sideways: number };
  targetZone: { high: number; low: number };
  path: PredictPathPoint[];
  /** 0..1 */
  confidence: number;
  /** AI 研判理由（中文） */
  rationale: string;
  /** 依据信号 id 列表（如 dow-rule123） */
  signals: string[];
  disclaimer?: string;
  mock?: boolean;
  error?: string;
}

/** 预测层输入 K 线（轻量结构，与 lightweight-charts 解耦，便于纯函数单测） */
export interface PredictBar {
  timeSec: number;
  close: number;
  high: number;
  low: number;
}

/** 预测层配色：锥/路径紫系（AI 语义，与 fib 紫呼应），方向沿用涨绿跌红铁律 */
export const PREDICT_COLORS = {
  cone: "#bc8cff",
  up: "#3fb950",
  down: "#f85149",
  sideways: "#bc8cff",
} as const;

/** 图表渲染载荷：价格 + bar 逻辑索引（索引可越过最后一根 K 线 → 未来区域） */
export interface PredictionOverlay {
  direction: PredictDirection;
  /** 已归一化（和恒为 1） */
  probability: { up: number; down: number; sideways: number };
  targetZone: { high: number; low: number };
  /** 锥起点：预测生成时刻对应的 bar 索引与收盘价 */
  anchorIdx: number;
  anchorPrice: number;
  horizon: number;
  confidence: number;
  mock: boolean;
  /** 预测路径（逻辑索引严格递增，均 > anchorIdx） */
  points: { idx: number; price: number }[];
  /** 视口扩展去重 key（同一份预测只扩一次视野） */
  genKey: string;
  /** 悬停提示（单行） */
  tooltip: string;
  /** 标签盒标题（mock 时带「演示」） */
  title: string;
}

export function fmtPredictPrice(v: number): string {
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

const TF_UNIT_SEC: Record<string, number> = { m: 60, h: 3600, d: 86400, w: 604800 };

/** "15m"→900、"1h"→3600、"1d"→86400；无法解析返回 0（调用方兜底） */
export function tfToSeconds(tf: string): number {
  const m = /^(\d+)([mhdw])$/.exec(tf.trim());
  if (!m) return 0;
  return Number(m[1]) * TF_UNIT_SEC[m[2]];
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/** 概率归一化（防后端三档和不为 1 / 含非法值） */
export function normalizeProbability(p: {
  up: number;
  down: number;
  sideways: number;
}): { up: number; down: number; sideways: number } {
  const up = Number.isFinite(p.up) && p.up > 0 ? p.up : 0;
  const down = Number.isFinite(p.down) && p.down > 0 ? p.down : 0;
  const sideways = Number.isFinite(p.sideways) && p.sideways > 0 ? p.sideways : 0;
  const sum = up + down + sideways;
  if (sum <= 0) return { up: 1 / 3, down: 1 / 3, sideways: 1 / 3 };
  return { up: up / sum, down: down / sum, sideways: sideways / sum };
}

// ── 本地 mock 推演 ──────────────────────────────────────────────────────
//
// 规则概率骨架：20 根动量 → 方向倾斜 tilt（-1..1），ATR(14) → 波动尺度。
//   probability  up/down = 0.34 ± 0.28·tilt，sideways 取剩余
//   targetZone   中心随 tilt 偏移 0.35·ATR·√h，宽度 0.8·ATR·√h（不确定性 ∝ √t）
//   path         线性漂移向区间中心 + 正弦小扰动（确定性）
//   confidence   0.45 + 0.3·|tilt|（动量越明确越自信）

/** 动量回看根数 / ATR 回看根数 */
const MOCK_MOM_BARS = 20;
const MOCK_ATR_BARS = 14;

export function mockPredict(
  symbol: string,
  timeframe: string,
  horizon: number,
  bars: PredictBar[],
): PredictResponse | null {
  if (bars.length < 2 || horizon <= 0) return null;
  const last = bars[bars.length - 1];
  const lastClose = last.close;
  if (!Number.isFinite(lastClose) || lastClose <= 0) return null;

  const momBase = bars[Math.max(0, bars.length - 1 - MOCK_MOM_BARS)].close;
  const mom = momBase > 0 ? (lastClose - momBase) / momBase : 0;

  let atrSum = 0;
  let atrN = 0;
  for (let i = Math.max(0, bars.length - MOCK_ATR_BARS); i < bars.length; i++) {
    const range = bars[i].high - bars[i].low;
    if (Number.isFinite(range) && range >= 0) {
      atrSum += range;
      atrN++;
    }
  }
  const atr = atrN > 0 ? atrSum / atrN : lastClose * 0.005;

  // 2% 动量视为满倾斜
  const tilt = clamp(mom / 0.02, -1, 1);
  const probability = normalizeProbability({
    up: 0.34 + 0.28 * tilt,
    down: 0.34 - 0.28 * tilt,
    sideways: 0.32,
  });
  const direction: PredictDirection =
    probability.up >= probability.down && probability.up >= probability.sideways
      ? "up"
      : probability.down >= probability.up && probability.down >= probability.sideways
        ? "down"
        : "sideways";

  const sqrtH = Math.sqrt(horizon);
  const centerShift = tilt * atr * sqrtH * 0.35;
  const spread = Math.max(atr * sqrtH * 0.8, lastClose * 0.001);
  const targetZone = {
    high: lastClose + centerShift + spread / 2,
    low: lastClose + centerShift - spread / 2,
  };

  const tfSec = tfToSeconds(timeframe) || (bars.length > 1 ? last.timeSec - bars[bars.length - 2].timeSec : 60);
  const path: PredictPathPoint[] = [];
  for (let i = 1; i <= horizon; i++) {
    const drift = centerShift * (i / horizon);
    const wiggle = Math.sin(i * 1.3) * atr * 0.15;
    path.push({
      t: new Date((last.timeSec + i * tfSec) * 1000).toISOString(),
      price: lastClose + drift + wiggle,
    });
  }

  const confidence = clamp(0.45 + 0.3 * Math.abs(tilt), 0, 1);
  const dirCn = direction === "up" ? "看涨" : direction === "down" ? "看跌" : "震荡";
  return {
    ok: true,
    symbol,
    timeframe,
    // 取最后一根 K 线时刻（而非 now）：同一批 K 线输入结果幂等
    generatedAt: new Date(last.timeSec * 1000).toISOString(),
    horizon,
    direction,
    probability,
    targetZone,
    path,
    confidence,
    rationale:
      `近 ${MOCK_MOM_BARS} 根 K 线动量 ${(mom * 100).toFixed(2)}%，` +
      `ATR 波动约 ${fmtPredictPrice(atr)}，规则概率倾向「${dirCn}」；` +
      `目标区间按 ATR·√${horizon} 概率锥推演。本结果为本地演示推演（预测引擎未接入），非 AI 研判输出。`,
    signals: ["mock-momentum", "mock-atr-cone"],
    disclaimer: "预测仅供研究参考，不构成投资建议。",
    mock: true,
  };
}

// ── 响应 → 图表 overlay ────────────────────────────────────────────────

/**
 * 把预测响应换算成图表渲染载荷：
 *  - anchor 定位在 generatedAt 时刻对应的 bar（响应生成后 K 线继续增长时，
 *    锥起点仍锚在生成时刻，path 时间→索引换算不漂移）
 *  - path 时间戳按 timeframe 步长换算成 bar 逻辑索引（可越过最后一根 K 线）
 * bars 为空 / targetZone 非法时返回 null（调用方直接不渲染）。
 */
export function buildPredictionOverlay(
  resp: PredictResponse,
  bars: PredictBar[],
): PredictionOverlay | null {
  if (bars.length === 0) return null;
  const zHigh = Number(resp.targetZone?.high);
  const zLow = Number(resp.targetZone?.low);
  if (!Number.isFinite(zHigh) || !Number.isFinite(zLow)) return null;
  const targetZone = { high: Math.max(zHigh, zLow), low: Math.min(zHigh, zLow) };

  const genSecRaw = Date.parse(resp.generatedAt) / 1000;
  const genSec = Number.isFinite(genSecRaw) ? genSecRaw : bars[bars.length - 1].timeSec;
  // 生成点几乎总在尾部，自尾向前找第一个 timeSec ≤ genSec 的 bar
  let anchorIdx = bars.length - 1;
  while (anchorIdx > 0 && bars[anchorIdx].timeSec > genSec) anchorIdx--;
  const anchorBar = bars[anchorIdx];
  const anchorPrice = anchorBar.close;
  if (!Number.isFinite(anchorPrice)) return null;

  const tfSec =
    tfToSeconds(resp.timeframe) ||
    (bars.length > 1 ? bars[bars.length - 1].timeSec - bars[bars.length - 2].timeSec : 60);

  const points: { idx: number; price: number }[] = [];
  for (const p of resp.path ?? []) {
    const sec = Date.parse(p.t) / 1000;
    if (!Number.isFinite(sec) || !Number.isFinite(p.price)) continue;
    const rel = Math.round((sec - anchorBar.timeSec) / tfSec);
    if (rel < 1) continue; // 历史点不画（预测层只画未来）
    const idx = anchorIdx + rel;
    // 同一索引保留最后一个点
    if (points.length > 0 && points[points.length - 1].idx === idx) {
      points[points.length - 1] = { idx, price: p.price };
    } else {
      points.push({ idx, price: p.price });
    }
  }
  points.sort((a, b) => a.idx - b.idx);

  const horizon = Math.max(
    Math.round(Number(resp.horizon) > 0 ? Number(resp.horizon) : 0),
    points.length > 0 ? points[points.length - 1].idx - anchorIdx : 0,
    1,
  );

  const probability = normalizeProbability(resp.probability ?? { up: 0, down: 0, sideways: 0 });
  const confidence = clamp(Number(resp.confidence) || 0, 0, 1);
  const mock = resp.mock === true;
  const pct = (v: number) => `${Math.round(v * 100)}%`;
  return {
    direction: resp.direction ?? "sideways",
    probability,
    targetZone,
    anchorIdx,
    anchorPrice,
    horizon,
    confidence,
    mock,
    points,
    genKey: `${resp.symbol}|${resp.timeframe}|${resp.generatedAt}`,
    tooltip:
      `AI 预测${mock ? "（演示数据）" : ""}：↑${pct(probability.up)} →${pct(probability.sideways)} ↓${pct(probability.down)}` +
      ` · 目标区 ${fmtPredictPrice(targetZone.low)} – ${fmtPredictPrice(targetZone.high)}` +
      ` · 信心 ${pct(confidence)}`,
    title: mock ? "AI 预测 · 演示" : "AI 预测",
  };
}
