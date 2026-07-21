import type { FootprintBar } from "@/types/footprint";
import { IMBALANCE_RATIO, isOhlcOnly } from "../renderer";

/** 图上信号徽标（挂在具体柱上） */
export interface FpSignal {
  id: string;
  type: "sweep" | "stack" | "divergence";
  side: "buy" | "sell";
  barIndex: number;
  /** 0-1，同屏密度控制取最强 */
  strength: number;
  title: string;
  /** 这个信号是什么（就当前柱数据的白话描述） */
  what: string;
  /** 历史上通常意味着什么 */
  meaning: string;
  /** 风险提示 */
  risk: string;
}

interface ImbCount {
  buy: number;
  sell: number;
  maxBuyVol: number;
  maxSellVol: number;
}

function countImbalances(bar: FootprintBar, floor: number): ImbCount {
  const c: ImbCount = { buy: 0, sell: 0, maxBuyVol: 0, maxSellVol: 0 };
  for (const lv of bar.levels) {
    if (lv.askVol >= IMBALANCE_RATIO * Math.max(lv.bidVol, 1) && lv.askVol >= floor) {
      c.buy++;
      c.maxBuyVol = Math.max(c.maxBuyVol, lv.askVol);
    }
    if (lv.bidVol >= IMBALANCE_RATIO * Math.max(lv.askVol, 1) && lv.bidVol >= floor) {
      c.sell++;
      c.maxSellVol = Math.max(c.maxSellVol, lv.bidVol);
    }
  }
  return c;
}

const mean = (xs: number[]): number =>
  xs.length === 0 ? 0 : xs.reduce((a, b) => a + b, 0) / xs.length;

/**
 * 扫描最近 scan 根柱，产出图上徽标信号。
 * 三类信号（均为经典订单流形态，纯前端规则）：
 * - sweep 大单扫盘：单柱成交量 ≥ 近期均量 2.2x 且 |delta| 占比 ≥ 35%
 * - stack 失衡堆积：单柱同向失衡格 ≥ 3 个
 * - divergence Delta 背离：价格创近期新高但 delta 为负（或反向）
 */
export function detectSignals(bars: FootprintBar[], scan = 60): FpSignal[] {
  const n = bars.length;
  if (n < 6) return [];
  const start = Math.max(0, n - scan);
  const out: FpSignal[] = [];

  // 基准均量（不含目标柱自身的滚动窗口，简单起见用全扫描窗均值）
  const vols = bars.slice(start, n).map((b) => b.totalVol);
  const avgVol = Math.max(1, mean(vols));

  for (let i = Math.max(start, 2); i < n; i++) {
    const bar = bars[i];
    // OHLC-only 柱无逐笔口径，delta/失衡不可信，跳过防误报
    if (isOhlcOnly(bar)) continue;
    const dPct = bar.totalVol > 0 ? bar.delta / bar.totalVol : 0;

    // --- sweep 大单扫盘 ---
    if (bar.totalVol >= avgVol * 2.2 && Math.abs(dPct) >= 0.35) {
      const side: FpSignal["side"] = bar.delta >= 0 ? "buy" : "sell";
      const volX = bar.totalVol / avgVol;
      out.push({
        id: `sweep:${bar.time}`,
        type: "sweep",
        side,
        barIndex: i,
        strength: Math.min(1, 0.5 + volX / 8 + Math.abs(dPct) / 2),
        title: side === "buy" ? "大单扫盘 · 买" : "大单扫盘 · 卖",
        what: `这根柱成交量是近期均值的 ${volX.toFixed(1)} 倍，且${side === "buy" ? "主动买" : "主动卖"}占优（Delta ${Math.round(dPct * 100)}%），像是大资金一口气${side === "buy" ? "吃掉卖单" : "砸穿买单"}。`,
        meaning:
          side === "buy"
            ? "历史上大单扫买常出现在拉升起点或突破时，短线偏多；但若价格随后不涨，可能是诱多。"
            : "历史上大单扫卖常出现在破位或恐慌时，短线偏空；若随后价格不跌反弹，可能是空头陷阱。",
        risk: "单根柱的大单不构成趋势，需要后续 2-3 根柱确认方向。追单进场容易买在情绪最高点，注意仓位控制。",
      });
    }

    // --- stack 失衡堆积 ---
    const maxCell = Math.max(
      1,
      ...bar.levels.map((l) => Math.max(l.bidVol, l.askVol)),
    );
    const imb = countImbalances(bar, maxCell * 0.05);
    if (imb.buy >= 3 || imb.sell >= 3) {
      const side: FpSignal["side"] = imb.buy >= imb.sell ? "buy" : "sell";
      const cnt = Math.max(imb.buy, imb.sell);
      out.push({
        id: `stack:${bar.time}`,
        type: "stack",
        side,
        barIndex: i,
        strength: Math.min(1, 0.45 + cnt / 8),
        title: side === "buy" ? "失衡堆积 · 买" : "失衡堆积 · 卖",
        what: `这根柱里有 ${cnt} 个价位出现${side === "buy" ? "买" : "卖"}方 ${IMBALANCE_RATIO} 倍以上碾压（绿框格子），${side === "buy" ? "买" : "卖"}方在连续价位持续吃单。`,
        meaning:
          side === "buy"
            ? "连续失衡买入说明买方志在必得，常构成短期支撑；回踩这些价位时常有承接。"
            : "连续失衡卖出说明卖方坚决出货，常构成短期压力；反弹到这些价位时容易再度回落。",
        risk: "失衡若出现在行情末端（已大涨/大跌后），可能是最后一波情绪宣泄，反而临近反转。别把单一信号当买卖依据。",
      });
    }

    // --- divergence Delta 背离（看近 5 根窗口的价格极值） ---
    if (i >= start + 4) {
      const win = bars.slice(i - 4, i);
      const isNewHigh = bar.close >= Math.max(...win.map((b) => b.high));
      const isNewLow = bar.close <= Math.min(...win.map((b) => b.low));
      if (isNewHigh && bar.delta < 0 && Math.abs(dPct) >= 0.12) {
        out.push({
          id: `div:${bar.time}`,
          type: "divergence",
          side: "sell",
          barIndex: i,
          strength: Math.min(1, 0.55 + Math.abs(dPct)),
          title: "Delta 背离 · 顶部预警",
          what: `价格创出近 5 根柱的新高，但这根柱的 Delta 是负的（卖量反而更大）——推价的人和真实成交方向不一致。`,
          meaning:
            "价涨量背离，历史上常见于主力借拉高出货或多头衰竭，短期回调概率增大。",
          risk: "背离可以持续多次才兑现，不是精确的反转点。逆势做空风险高，更稳妥的用法是持多单者减仓、别追高。",
        });
      } else if (isNewLow && bar.delta > 0 && Math.abs(dPct) >= 0.12) {
        out.push({
          id: `div:${bar.time}`,
          type: "divergence",
          side: "buy",
          barIndex: i,
          strength: Math.min(1, 0.55 + Math.abs(dPct)),
          title: "Delta 背离 · 底部预警",
          what: `价格创出近 5 根柱的新低，但这根柱的 Delta 是正的（买量反而更大）——下跌过程中有人在低位持续接货。`,
          meaning:
            "价跌量背离，历史上常见于恐慌盘被吸收、空头衰竭，短期反弹概率增大。",
          risk: "下跌趋势中的背离可能连续失效（阴跌不止）。抄底建议等价格站回 POC 上方再确认，严格设止损。",
        });
      }
    }
  }
  return out;
}
