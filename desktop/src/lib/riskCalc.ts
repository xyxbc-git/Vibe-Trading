// 合约下单本地风控计算器（纯函数，不依赖 AI / 后端）。
//
// 口径说明（与 PositionAdvisor / planOrder 的保证金语义对齐）：
//   - 下单金额 = 保证金（占用本金的部分）；名义仓位 = 保证金 × 杠杆。
//   - 止损/止盈用「价格距离百分比」（与交易中心表单 stopLoss%/takeProfit% 同口径），
//     亏损金额 = 名义仓位 × 价格距离%，因此占本金比例 = 距离% × 杠杆 × 下单比例。
//   - 强平价为逐仓近似：价格反向走 (100/杠杆 − 维持保证金率)% 时保证金归零。
//     真实交易所强平还受资金费率/手续费/维持保证金阶梯影响，结果标注「估算」。
// 独立成模块便于单测锁定公式（见 __tests__/riskCalc.test.ts）。

export type Direction = "long" | "short";

/** 风险偏好三档：单笔最大亏损占本金 % */
export const RISK_PRESETS = {
  conservative: { riskPct: 1, label: "保守", desc: "单笔风险 1% 本金" },
  balanced: { riskPct: 2, label: "稳健", desc: "单笔风险 2% 本金" },
  aggressive: { riskPct: 5, label: "激进", desc: "单笔风险 5% 本金" },
} as const;

export type RiskPreset = keyof typeof RISK_PRESETS;

/** 维持保证金率近似值 %（主流币逐仓低档位约 0.4~0.5%，取保守值） */
export const DEFAULT_MMR_PCT = 0.5;

/** 强平边距安全系数：要求强平距离 ≥ 止损距离 × 该系数才算安全 */
export const SL_SAFETY_FACTOR = 1.5;

/** 常用杠杆档位（滑杆快捷点） */
export const LEVERAGE_STEPS = [1, 3, 5, 10, 20, 50, 100] as const;

export interface RiskCalcInput {
  /** 本金（USDT），> 0 */
  capital: number;
  /** 下单比例（占本金 %），0~100 */
  pctOfCapital: number;
  /** 杠杆倍数，1~125 */
  leverage: number;
  /** 方向 */
  direction: Direction;
  /** 入场价（现价或计划价），> 0 */
  entryPrice: number;
  /** 止损价格距离 %（相对入场价） */
  slPct: number;
  /** 止盈价格距离 %（相对入场价） */
  tpPct: number;
  /** 维持保证金率 %（缺省 0.5） */
  mmrPct?: number;
}

/** 危险项标识（下单前拦截用） */
export type RiskDangerKind =
  | "sl-beyond-liq"
  | "loss-over-threshold"
  | "leverage-over-safe";

export interface RiskDanger {
  kind: RiskDangerKind;
  /** 弹窗/提示用中文说明 */
  message: string;
}

export interface RiskCalcResult {
  /** 下单金额 = 占用保证金（USDT） */
  marginUsdt: number;
  /** 名义仓位（USDT） */
  notionalUsdt: number;
  /** 币数（名义 / 入场价） */
  qtyCoin: number;
  /** 止损价 */
  slPrice: number;
  /** 止盈价 */
  tpPrice: number;
  /** 估算强平价（逐仓近似） */
  liqPrice: number;
  /** 强平距离 %（相对入场价；杠杆过高时可能 ≤ 0） */
  liqDistPct: number;
  /** 强平距离 − 止损距离（负数 = 先爆仓后止损） */
  slLiqGapPct: number;
  /** 止损触发亏损（USDT） */
  slLossUsdt: number;
  /** 止损亏损占本金 % */
  slLossPctOfCapital: number;
  /** 止盈落袋（USDT） */
  tpProfitUsdt: number;
  /** 盈亏比 tp/sl */
  rr: number | null;
  /** 当前止损距离下的杠杆安全上限（含安全系数） */
  maxSafeLeverage: number;
  /** 往返 taker 手续费估算（0.05% × 2 × 名义） */
  estFeeUsdt: number;
  /** 危险项（空数组 = 无拦截项） */
  dangers: RiskDanger[];
}

const isPos = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v) && v > 0;

const round2 = (v: number) => Math.round(v * 100) / 100;
const round4 = (v: number) => Math.round(v * 1e4) / 1e4;
const round8 = (v: number) => Math.round(v * 1e8) / 1e8;

/** taker 单边费率 %（估算展示用） */
const TAKER_FEE_PCT = 0.05;

/** 单笔亏损超过本金该比例时列为危险项（独立于风险档，绝对上限） */
export const LOSS_HARD_CAP_PCT = 10;

/**
 * 由「单笔风险占本金 %」反推下单比例 %：
 *   亏损 = 本金 × 比例% × 杠杆 × 止损距离% = 本金 × riskPct%
 *   → 比例% = riskPct / (杠杆 × 止损距离%) × 100
 * 结果夹紧到 (0, 100]；参数非法返回 null。
 */
export function marginPctForRisk(
  riskPct: number,
  leverage: number,
  slPct: number,
): number | null {
  if (!isPos(riskPct) || !isPos(leverage) || !isPos(slPct)) return null;
  const pct = (riskPct * 100) / (leverage * slPct);
  return round2(Math.min(100, pct));
}

/**
 * 当前止损距离下的杠杆安全上限：
 *   要求 强平距离(100/lev − mmr) ≥ slPct × SL_SAFETY_FACTOR
 *   → lev ≤ 100 / (slPct × factor + mmr)
 */
export function maxSafeLeverage(slPct: number, mmrPct = DEFAULT_MMR_PCT): number {
  if (!isPos(slPct)) return 125;
  const lev = Math.floor(100 / (slPct * SL_SAFETY_FACTOR + mmrPct));
  return Math.max(1, Math.min(125, lev));
}

/** 主计算：任一关键输入非法返回 null（调用方渲染缺省态） */
export function calcRisk(input: RiskCalcInput): RiskCalcResult | null {
  const {
    capital,
    pctOfCapital,
    leverage,
    direction,
    entryPrice,
    slPct,
    tpPct,
    mmrPct = DEFAULT_MMR_PCT,
  } = input;
  if (
    !isPos(capital) ||
    !isPos(pctOfCapital) ||
    !isPos(leverage) ||
    !isPos(entryPrice) ||
    !isPos(slPct) ||
    !isPos(tpPct)
  ) {
    return null;
  }

  const marginUsdt = round2((capital * pctOfCapital) / 100);
  const notionalUsdt = round2(marginUsdt * leverage);
  const qtyCoin = round8(notionalUsdt / entryPrice);

  const sign = direction === "short" ? -1 : 1;
  const slPrice = round8(entryPrice * (1 - (sign * slPct) / 100));
  const tpPrice = round8(entryPrice * (1 + (sign * tpPct) / 100));

  // 逐仓强平近似：反向波动 (100/lev − mmr)% 时保证金归零
  const liqDistPct = round4(100 / leverage - mmrPct);
  const liqPrice = round8(entryPrice * (1 - (sign * liqDistPct) / 100));
  const slLiqGapPct = round4(liqDistPct - slPct);

  const slLossUsdt = round2((notionalUsdt * slPct) / 100);
  const slLossPctOfCapital = round2((slLossUsdt / capital) * 100);
  const tpProfitUsdt = round2((notionalUsdt * tpPct) / 100);
  const rr = slPct > 0 ? round2(tpPct / slPct) : null;

  const safeLev = maxSafeLeverage(slPct, mmrPct);
  const estFeeUsdt = round2((notionalUsdt * TAKER_FEE_PCT * 2) / 100);

  const dangers: RiskDanger[] = [];
  if (slLiqGapPct <= 0) {
    dangers.push({
      kind: "sl-beyond-liq",
      message: `止损距离 ${slPct}% ≥ 估算强平距离 ${liqDistPct}%：价格先触发强平、止损单形同虚设，将损失全部保证金 ${marginUsdt} U`,
    });
  }
  if (slLossPctOfCapital > LOSS_HARD_CAP_PCT) {
    dangers.push({
      kind: "loss-over-threshold",
      message: `单笔止损亏损 ${slLossUsdt} U（占本金 ${slLossPctOfCapital}%），超过 ${LOSS_HARD_CAP_PCT}% 硬上限，请降杠杆或减少下单比例`,
    });
  }
  if (leverage > safeLev) {
    dangers.push({
      kind: "leverage-over-safe",
      message: `杠杆 ${leverage}x 超过当前止损距离下的安全上限 ${safeLev}x（要求强平距离 ≥ 止损距离 × ${SL_SAFETY_FACTOR}）`,
    });
  }

  return {
    marginUsdt,
    notionalUsdt,
    qtyCoin,
    slPrice,
    tpPrice,
    liqPrice,
    liqDistPct,
    slLiqGapPct,
    slLossUsdt,
    slLossPctOfCapital,
    tpProfitUsdt,
    rr,
    maxSafeLeverage: safeLev,
    estFeeUsdt,
    dangers,
  };
}
