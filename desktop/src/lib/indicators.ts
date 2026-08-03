// 技术指标纯函数（无副作用、不依赖 DOM/chart 库），当前只有 MACD 一族。
// 口径与 frontend 工程 lib/indicators.ts 保持一致：
//   EMA 预热期输出 null，首个有效值用前 period 根的 SMA 作种子，之后标准递推；
//   MACD: DIF = EMA(fast) - EMA(slow)，DEA = 对非空 DIF 序列再做 EMA(signal)，
//   HIST = DIF - DEA（西方口径，不乘 2）。

export type IndicatorValue = number | null;

/** 指数移动平均：前 period-1 根为 null；第 period 根用 SMA 种子，之后 EMA 递推。 */
export function calcEMA(values: number[], period: number): IndicatorValue[] {
  const k = 2 / (period + 1);
  const out: IndicatorValue[] = [];
  let ema: number | null = null;
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) {
      out.push(null);
    } else if (ema === null) {
      let s = 0;
      for (let j = i - period + 1; j <= i; j++) s += values[j];
      ema = s / period;
      out.push(ema);
    } else {
      ema = values[i] * k + ema * (1 - k);
      out.push(ema);
    }
  }
  return out;
}

export interface MacdResult {
  /** 快慢 EMA 差离值（快线） */
  dif: IndicatorValue[];
  /** DIF 的 EMA 平滑（慢线，即 signal） */
  dea: IndicatorValue[];
  /** 柱状图：DIF - DEA */
  hist: IndicatorValue[];
}

/** MACD(fast, slow, signal)，默认 12/26/9。三个数组与输入等长、按位对齐。 */
export function calcMACD(
  values: number[],
  fast = 12,
  slow = 26,
  signal = 9,
): MacdResult {
  const emaFast = calcEMA(values, fast);
  const emaSlow = calcEMA(values, slow);
  const dif: IndicatorValue[] = values.map((_, i) =>
    emaFast[i] !== null && emaSlow[i] !== null ? emaFast[i]! - emaSlow[i]! : null,
  );

  // DEA = 对「非空 DIF 子序列」做 EMA，再映射回原下标（预热区保持 null）
  const valid: number[] = [];
  const idx: number[] = [];
  dif.forEach((v, i) => {
    if (v !== null) {
      valid.push(v);
      idx.push(i);
    }
  });
  const deaSeq = calcEMA(valid, signal);

  const dea: IndicatorValue[] = new Array(values.length).fill(null);
  const hist: IndicatorValue[] = new Array(values.length).fill(null);
  idx.forEach((origIdx, j) => {
    if (deaSeq[j] !== null) {
      dea[origIdx] = deaSeq[j];
      hist[origIdx] = dif[origIdx]! - deaSeq[j]!;
    }
  });
  return { dif, dea, hist };
}
