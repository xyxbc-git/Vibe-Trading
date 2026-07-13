/**
 * 模拟盘交易记录共用元数据与工具。
 * 供「总览页 TradeHistory 卡片」与「交易记录」独立页共用，保持口径一致。
 */

/** 持仓/平仓行公共字段（后端 paper_positions 表 + /api/positions 补列） */
interface TradeRowBase {
  id: number;
  symbol: string;
  direction: "long" | "short";
  qty: number;
  entry_price: number | null;
  signal_source: string | null;
  /** JSON 数组字符串，如 '["turtle","dow"]' */
  signal_systems: string | null;
  signal_tf: string | null;
  signal_regime: string | null;
  opened_ts?: number | null;
}

/** 已平仓记录行 */
export interface ClosedTrade extends TradeRowBase {
  exit_price: number | null;
  exit_reason: string | null;
  realized_pnl_usdt: number | null;
  realized_pnl_pct: number | null;
  closed_ts: number | null;
  /** T1.4 平仓复盘行为标签（可空；交易记录页可补标） */
  behavior_tag?: string | null;
}

/** 持仓中记录行（/api/positions?status=open 已补现价与浮动盈亏） */
export interface OpenPosition extends TradeRowBase {
  current_price?: number | null;
  pnl_pct?: number | null;
  pnl_usdt?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
}

/** 后端 _close_position 实际写入的 reason 值 → 中文 */
export const REASON_CN: Record<string, string> = {
  stop: "止损",
  take: "止盈",
  time: "到期",
  signal: "信号反转",
  manual: "手动",
  limit_sell: "限价卖出",
};

export const SOURCE_META: Record<string, { label: string; cls: string }> = {
  twelve: { label: "12系统", cls: "bg-jarvis-purple/15 text-jarvis-purple" },
  brief: { label: "简报", cls: "bg-jarvis-blue/15 text-jarvis-blue" },
  limit: { label: "限价", cls: "bg-jarvis-yellow/15 text-jarvis-yellow" },
  manual: { label: "手动", cls: "bg-jarvis-border/40 text-jarvis-text-secondary" },
};

/** 12 信号系统英文 key → 中文名 */
export const SYSTEM_CN: Record<string, string> = {
  turtle: "海龟", dow: "道氏", elliott: "艾略特", volatility: "波动率",
  gann: "江恩", chanlun: "缠论", rule123: "123法则", gap: "跳空",
  martingale: "马丁", oscillator: "摆动", triple_rsi: "三重RSI", arbitrage: "套利",
};

/** 市场状态（signal_regime）→ 中文标签 */
export const REGIME_CN: Record<string, string> = {
  trending: "趋势",
  ranging: "震荡",
  breakout: "突破",
};

/** 共振档：与后端 _resonance_bucket 同口径（1 / 2-3 / 4+） */
export function resonanceBucket(n: number): string {
  if (n <= 1) return "单系统";
  if (n <= 3) return `${n}共振`;
  return "4+共振";
}

export function parseSystems(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.map(String) : [];
  } catch {
    return [];
  }
}

/** 时间戳（秒）→ 本地时间；withYear 用于跨年历史记录 */
export function fmtTradeTs(ts: number | null | undefined, withYear = false): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", {
    ...(withYear ? { year: "2-digit" as const } : {}),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
