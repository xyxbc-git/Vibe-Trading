// 持仓卡片派生指标纯函数：盈亏比 R:R、杠杆推导、买入量/保证金提取。
//
// 数据口径说明（与后端 jarvis_wallet / jarvis_paper_trader 对齐）：
//   - 模拟盘钱包按现货全额记账：qty = 投入金额 / 入场价，故手动单杠杆视为 1x。
//   - 自创单（保存交易计划生成）的杠杆/保证金/名义仓位存在 limit_orders.note
//     的 trade-plan JSON 里，/api/positions 反查后以 plan_* 列附加到持仓行。
// 独立成模块便于单测锁定口径（多空方向、缺失字段、非法值回退）。

export type Direction = "long" | "short";

const isPos = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v) && v > 0;

/** "BTCUSDT" → "BTC"；非 USDT 后缀原样返回 */
export function baseAsset(symbol: string): string {
  return symbol.endsWith("USDT") && symbol.length > 4
    ? symbol.slice(0, -4)
    : symbol;
}

/**
 * 盈亏比 R:R（reward / risk，保留 2 位小数）。
 * 多头：risk = 入场 - 止损，reward = 止盈 - 入场；空头两腿取反。
 * 任一价缺失、或方向不合法（如多头止损 ≥ 入场）→ null，卡片显示缺失态。
 */
export function riskReward(
  direction: Direction,
  entry?: number | null,
  stopLoss?: number | null,
  takeProfit?: number | null,
): number | null {
  if (!isPos(entry) || !isPos(stopLoss) || !isPos(takeProfit)) return null;
  const sign = direction === "short" ? -1 : 1;
  const risk = (entry - stopLoss) * sign;
  const reward = (takeProfit - entry) * sign;
  if (risk <= 0 || reward <= 0) return null;
  return Math.round((reward / risk) * 100) / 100;
}

/**
 * 杠杆推导优先级：计划快照的 leverage → 名义/保证金反推 → 1x（现货全额）。
 * 反推结果保留 1 位小数（如 5.5x）。
 */
export function deriveLeverage(
  planLeverage?: number | null,
  notionalUsdt?: number | null,
  marginUsdt?: number | null,
): number {
  if (isPos(planLeverage)) return Math.round(planLeverage * 10) / 10;
  if (isPos(notionalUsdt) && isPos(marginUsdt) && notionalUsdt >= marginUsdt) {
    return Math.round((notionalUsdt / marginUsdt) * 10) / 10;
  }
  return 1;
}

/** 币数自适应格式：≥1000 整数、≥1 两位小数、<1 保留 4 位有效数字 */
export function fmtQty(qty?: number | null): string {
  if (qty == null || !Number.isFinite(qty) || qty <= 0) return "—";
  if (qty >= 1000)
    return qty.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (qty >= 1)
    return qty.toLocaleString("en-US", { maximumFractionDigits: 2 });
  return String(Number(qty.toPrecision(4)));
}

/** PositionCard 展示所需的派生指标集合 */
export interface PositionCardMetrics {
  /** 持仓数量（币数） */
  qty?: number;
  /** 投入/占用保证金（USDT）：计划快照优先，回退 qty×入场价 */
  marginUsdt?: number;
  /** 名义仓位（USDT）：计划快照优先，回退现货成本 */
  notionalUsdt?: number;
  /** 杠杆倍数，无计划快照按现货全额 1x */
  leverage: number;
  /** 浮盈金额（USDT，后端按现价补列；取价失败缺失） */
  pnlUsdt?: number;
}

/**
 * 从 /api/positions 行提取卡片指标，处理 plan_* 计划上下文的完整回退链。
 * 字段可能是 number/字符串/缺失（SQLite 动态类型），统一收敛成 number|undefined。
 */
export function extractCardMetrics(
  p: Record<string, unknown>,
): PositionCardMetrics {
  const num = (v: unknown): number | undefined => {
    if (v == null) return undefined;
    const n = typeof v === "string" ? parseFloat(v) : (v as number);
    return typeof n === "number" && Number.isFinite(n) ? n : undefined;
  };
  const qty = num(p.qty);
  const entry = num(p.entry_price);
  const spotCost =
    qty != null && entry != null && qty > 0 && entry > 0
      ? qty * entry
      : undefined;
  const planMargin = num(p.plan_margin_usdt);
  const planNotional = num(p.plan_notional_usdt);
  const margin = planMargin ?? spotCost;
  return {
    qty,
    marginUsdt: margin,
    notionalUsdt: planNotional ?? spotCost,
    leverage: deriveLeverage(num(p.plan_leverage), planNotional, margin),
    pnlUsdt: num(p.pnl_usdt),
  };
}
