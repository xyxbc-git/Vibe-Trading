/**
 * MACD 指标计算（经典 Appel 口径，默认 12/26/9）：
 *   DIF（快线）  = EMA(close, fast) − EMA(close, slow)
 *   DEA（慢线）  = EMA(DIF, signal)
 *   HIST（柱体） = DIF − DEA（TradingView 同口径；国内部分软件为 2×(DIF−DEA)，仅缩放差异不影响形态）
 *
 * EMA 以首值为种子递推（k = 2/(n+1)），前 slow+signal−2 根为收敛期，
 * 由 warmupIndex 标出供渲染端裁剪，避免曲线头部失真。
 */

export interface MacdSeriesPoint {
  /** 对应输入 closes 的下标 */
  index: number;
  dif: number;
  dea: number;
  hist: number;
}

export interface MacdResult {
  points: MacdSeriesPoint[];
  /** 首个建议展示的下标（EMA 收敛期之后）；输入过短时为 -1 */
  warmupIndex: number;
}

function emaSeries(values: number[], period: number): number[] {
  const out = new Array<number>(values.length);
  const k = 2 / (period + 1);
  for (let i = 0; i < values.length; i++) {
    out[i] = i === 0 ? values[0] : values[i] * k + out[i - 1] * (1 - k);
  }
  return out;
}

export function computeMacd(
  closes: number[],
  fast = 12,
  slow = 26,
  signal = 9,
): MacdResult {
  if (closes.length === 0) {
    return { points: [], warmupIndex: -1 };
  }
  const emaFast = emaSeries(closes, fast);
  const emaSlow = emaSeries(closes, slow);
  const dif = closes.map((_, i) => emaFast[i] - emaSlow[i]);
  const dea = emaSeries(dif, signal);

  const points: MacdSeriesPoint[] = closes.map((_, i) => ({
    index: i,
    dif: dif[i],
    dea: dea[i],
    hist: dif[i] - dea[i],
  }));
  const warmup = slow + signal - 2;
  return {
    points,
    warmupIndex: closes.length > warmup ? warmup : -1,
  };
}
