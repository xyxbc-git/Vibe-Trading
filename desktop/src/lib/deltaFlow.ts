// Delta/CVD 订单流副图的数据契约 + 本地 mock 推演（纯函数，无 IO/图表依赖）。
//
// 契约来源：Delta 引擎 GET /api/delta?symbol=&timeframe=&limit=（MCP-5，
// jarvis_delta_flow）。引擎未就绪/请求失败时 mockDelta 用 K 线本地推演演示
// 数据：同一批 K 线输入恒产出相同结果（无随机数），mock:true 让 UI 显示
// 「演示数据」角标——与预测层（lib/predict.ts）同一套降级模式。
//
// 口径说明（安全带逻辑的用户口径）：
//   Delta = 主动买量 − 主动卖量（每根）；CVD = Delta 累计曲线。
//   「价格创新低但 CVD 不再新低」= 看涨吸收背离（有人在低位接货）；
//   「价格创新高但 CVD 不再新高」= 看跌派发背离。吸收出现，行情才拐弯。

export interface DeltaBar {
  /** bar 开盘时刻 unix 秒 */
  t: number;
  /** 主动买卖差（正=买方主导） */
  delta: number;
  /** 累计成交量 Delta（Cumulative Volume Delta） */
  cvd: number;
  volume: number;
}

/** 背离锚点：主图价格点位 + 副图 CVD 点位（画连线用） */
export interface DivergenceAnchor {
  t: number;
  price: number;
  cvd: number;
}

export interface DivergenceSide {
  active: boolean;
  /** 数值 0~1 或 strong/moderate/weak（后端两种形态都可能给） */
  strength?: number | string;
  note?: string;
  anchors?: DivergenceAnchor[];
}

/** GET /api/delta 响应（MCP-5 数据契约；ok/error 为项目封套惯例） */
export interface DeltaResponse {
  ok?: boolean;
  symbol: string;
  timeframe: string;
  bars: DeltaBar[];
  divergence: { bullish: DivergenceSide; bearish: DivergenceSide };
  absorption?: { detected: boolean; side?: string; note?: string } | null;
  updatedAt?: string;
  disclaimer?: string;
  mock?: boolean;
  error?: string;
}

/** 副图输入 K 线（与预测层 PredictBar 同构，避免依赖图表类型） */
export interface DeltaKline {
  timeSec: number;
  open: number;
  close: number;
  high: number;
  low: number;
  volume?: number;
}

export const DELTA_COLORS = {
  buy: "#3fb950",
  sell: "#f85149",
  cvd: "#bc8cff",
  divergence: "#d29922",
} as const;

/** strength（数值或文字）→ 三档文字（UI 标注「吸收背离 · strong」用） */
export function strengthGrade(s: number | string | undefined): "strong" | "moderate" | "weak" {
  if (typeof s === "string") {
    return s === "strong" || s === "weak" ? s : "moderate";
  }
  const v = Number(s);
  if (!Number.isFinite(v)) return "moderate";
  if (v >= 0.66) return "strong";
  if (v >= 0.33) return "moderate";
  return "weak";
}

// ── 本地 mock 推演 ──────────────────────────────────────────────────────
//
// 每根 Delta ≈ 收阳/收阴方向 × 实体占比 × 成交量（无 tick 数据的近似口径）；
// CVD = 累计。背离检测：近段价格新低/新高 vs CVD 是否跟随（窗口化极值对比）。

const DIV_LOOKBACK = 40; // 背离检测回看根数
const DIV_SPLIT = 20;    // 前后两半对比（前半极值 vs 后半极值）

function detectDivergence(
  bars: DeltaKline[],
  cvds: number[],
): { bullish: DivergenceSide; bearish: DivergenceSide } {
  const none: DivergenceSide = { active: false };
  const n = bars.length;
  if (n < DIV_LOOKBACK) return { bullish: { ...none }, bearish: { ...none } };

  const seg = bars.slice(n - DIV_LOOKBACK);
  const segCvd = cvds.slice(n - DIV_LOOKBACK);
  const half = DIV_LOOKBACK - DIV_SPLIT;
  const firstBars = seg.slice(0, half);
  const lastBars = seg.slice(half);
  const firstCvd = segCvd.slice(0, half);
  const lastCvd = segCvd.slice(half);

  const idxOfMin = (xs: number[]) => xs.indexOf(Math.min(...xs));
  const idxOfMax = (xs: number[]) => xs.indexOf(Math.max(...xs));

  // 看涨吸收：后半段价格创出更低的低点，但 CVD 低点抬高（卖压被吸收）
  const fLowI = idxOfMin(firstBars.map((b) => b.low));
  const lLowI = idxOfMin(lastBars.map((b) => b.low));
  const priceLowerLow = lastBars[lLowI].low < firstBars[fLowI].low;
  const cvdHigherLow = Math.min(...lastCvd) > Math.min(...firstCvd);
  const bullish: DivergenceSide = priceLowerLow && cvdHigherLow
    ? {
        active: true,
        strength: 0.7,
        note: "价格创新低但 CVD 低点抬高——卖压被动吸收，低位有人接货（演示口径）",
        anchors: [
          { t: firstBars[fLowI].timeSec, price: firstBars[fLowI].low, cvd: firstCvd[idxOfMin(firstCvd)] },
          { t: lastBars[lLowI].timeSec, price: lastBars[lLowI].low, cvd: lastCvd[idxOfMin(lastCvd)] },
        ],
      }
    : { ...none };

  // 看跌派发：后半段价格创出更高的高点，但 CVD 高点降低（买盘被派发）
  const fHighI = idxOfMax(firstBars.map((b) => b.high));
  const lHighI = idxOfMax(lastBars.map((b) => b.high));
  const priceHigherHigh = lastBars[lHighI].high > firstBars[fHighI].high;
  const cvdLowerHigh = Math.max(...lastCvd) < Math.max(...firstCvd);
  const bearish: DivergenceSide = priceHigherHigh && cvdLowerHigh
    ? {
        active: true,
        strength: 0.7,
        note: "价格创新高但 CVD 高点降低——买盘被派发，高位有人出货（演示口径）",
        anchors: [
          { t: firstBars[fHighI].timeSec, price: firstBars[fHighI].high, cvd: firstCvd[idxOfMax(firstCvd)] },
          { t: lastBars[lHighI].timeSec, price: lastBars[lHighI].high, cvd: lastCvd[idxOfMax(lastCvd)] },
        ],
      }
    : { ...none };

  return { bullish, bearish };
}

/**
 * K 线 → 演示 Delta/CVD（确定性）。bars 不足 2 根返回 null。
 * Delta 近似：方向（收阳+/收阴−）× 实体占比 × 成交量；无成交量时用波幅代理。
 */
export function mockDelta(
  symbol: string,
  timeframe: string,
  klines: DeltaKline[],
): DeltaResponse | null {
  if (klines.length < 2) return null;
  const bars: DeltaBar[] = [];
  const cvds: number[] = [];
  let cvd = 0;
  for (const k of klines) {
    const range = Math.max(k.high - k.low, 1e-9);
    const bodyRatio = Math.min(1, Math.abs(k.close - k.open) / range);
    const vol = k.volume != null && Number.isFinite(k.volume) && k.volume > 0 ? k.volume : range;
    const sign = k.close >= k.open ? 1 : -1;
    const delta = sign * bodyRatio * vol;
    cvd += delta;
    bars.push({
      t: k.timeSec,
      delta: Math.round(delta * 1e4) / 1e4,
      cvd: Math.round(cvd * 1e4) / 1e4,
      volume: vol,
    });
    cvds.push(cvd);
  }
  const divergence = detectDivergence(klines, cvds);
  const absorption = divergence.bullish.active
    ? { detected: true, side: "buy", note: "低位吸收迹象（演示推演）" }
    : divergence.bearish.active
      ? { detected: true, side: "sell", note: "高位派发迹象（演示推演）" }
      : { detected: false };
  return {
    ok: true,
    symbol,
    timeframe,
    bars,
    divergence,
    absorption,
    updatedAt: new Date(klines[klines.length - 1].timeSec * 1000).toISOString(),
    disclaimer: "演示推演基于 K 线近似（非真实逐笔订单流），仅供联调参考。",
    mock: true,
  };
}
