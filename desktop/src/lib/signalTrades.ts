// 单信号胜率回测逐笔样本 → K 线图 L/S 徽章标记 + 悬停详情 的纯函数映射。
//
// 后端 /api/twelve/signal-winrate/trades 返回该信号系统的历史触发明细
// （与信号矩阵展示的聚合胜率同一次回测、同一口径）；本模块把它映射成
// TradeMarkersPrimitive 消费的 TradeMark 数组（QuantDinger 风格）：
//   入场徽章  绿色圆角方块「L」= 做多 / 红色方块「S」= 做空，
//             圆点 + 短引线锚到触发 K 线（多在 bar 下方 / 空在上方），
//             角标区分结果：盈 → 绿底对勾 / 亏 → 红底叉
//   出场圆点  标在出场 bar 的出场价上（盈绿亏红）
// 保持纯函数（无 IO、无图表依赖），方便单测锁定映射关系；像素级绘制
// 交给 TradeMarkersPrimitive。

import type { SignalWinrateTrade } from "../api/client";
import { fmt } from "./tradePlan";

/** 盈/亏结果色（角标与出场圆点用；徽章本体颜色编码多空方向） */
export const TRADE_MARK_COLORS = {
  win: "#3fb950",
  loss: "#f85149",
} as const;

/** 单笔历史样本的图上标记（时间与价格锚点 + 悬停文案） */
export interface TradeMark {
  /** 触发（入场）bar 开盘时间（秒，与 K 线 time 对齐） */
  timeSec: number;
  /** 出场 bar 开盘时间（秒）；出场超出已加载窗口时为 null（只画入场徽章） */
  exitTimeSec: number | null;
  side: "long" | "short";
  win: boolean;
  /** 入场价 */
  entry: number;
  /** 徽章锚点价：多单锚触发 bar 低点（徽章挂下方）/ 空单锚高点（挂上方）；
   *  无 bar 数据时回退入场价 */
  anchorPrice: number;
  /** 出场价（出场圆点的锚点价） */
  exitPrice: number;
  /** 入场徽章悬停详情 */
  tooltip: string;
  /** 出场圆点悬停详情 */
  exitTooltip: string;
}

export interface TradeMarksResult {
  marks: TradeMark[];
  /** 入场时间落在当前 K 线窗口内的样本数 */
  visible: number;
  /** 样本总数 */
  total: number;
}

/** 单笔样本的悬停详情（小白友好：方向 + 结果 + 价格路径 + 持有时长） */
export function tradeTip(t: SignalWinrateTrade, nameCn: string): string {
  const dir = t.side === "long" ? "做多" : "做空";
  const res = t.win ? "盈" : "亏";
  const pnl = `${t.pnl_pct >= 0 ? "+" : ""}${t.pnl_pct.toFixed(2)}%`;
  const how =
    t.mode === "plan"
      ? t.win
        ? "触止盈离场"
        : "触止损离场"
      : "满观察期收盘离场";
  return `${nameCn} ${dir} · ${res} ${pnl} · 入 ${fmt(t.entry)} → 出 ${fmt(t.exit_price)} · ${how} · 持有 ${t.bars_held} 根`;
}

/**
 * 逐笔样本 → L/S 徽章标记集合（按触发时间升序）。
 *
 * @param trades 后端逐笔明细（时间戳 ms，与 Binance K 线开盘时间对齐）
 * @param nameCn 信号系统中文名（悬停文案用）
 * @param rangeFromSec 当前已加载 K 线的首根开盘时间（秒）；入场时间早于窗口
 *   的样本整笔跳过（K 线不在图上，标记无处安放），计入 total 不计 visible
 * @param rangeToSec 最新 K 线开盘时间（秒）；出场时间超窗时 exitTimeSec=null
 * @param bars 可选：bar 开盘秒 → {high, low}，用于把徽章锚到 K 线影线外侧
 *   （多单锚低点、空单锚高点）；缺失时锚入场价
 */
export function tradesToMarks(
  trades: SignalWinrateTrade[],
  nameCn: string,
  rangeFromSec: number,
  rangeToSec: number,
  bars?: Map<number, { high: number; low: number }>,
): TradeMarksResult {
  const marks: TradeMark[] = [];
  let visible = 0;

  for (const t of trades) {
    const inSec = Math.floor(t.t / 1000);
    const outSec = Math.floor(t.exit_t / 1000);
    if (!Number.isFinite(inSec) || inSec < rangeFromSec || inSec > rangeToSec) continue;
    visible += 1;

    const tip = tradeTip(t, nameCn);
    const exitInRange = outSec >= rangeFromSec && outSec <= rangeToSec && outSec >= inSec;
    const bar = bars?.get(inSec);
    marks.push({
      timeSec: inSec,
      exitTimeSec: exitInRange ? outSec : null,
      side: t.side,
      win: t.win,
      entry: t.entry,
      anchorPrice: bar ? (t.side === "long" ? bar.low : bar.high) : t.entry,
      exitPrice: t.exit_price,
      tooltip: `入场 · ${tip}`,
      exitTooltip: `出场 · ${tip}`,
    });
  }

  marks.sort((a, b) => a.timeSec - b.timeSec);
  return { marks, visible, total: trades.length };
}
