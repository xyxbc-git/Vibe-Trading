// 一目均衡表（Ichimoku Kinko Hyo，俗称「云图」）指标计算 + 人话解读。
// 纯函数模块，无 IO / 图表依赖，供 K 线页叠加层与单测复用。
//
// 标准参数 9/26/52（转换/基准/先行B 窗口，云前移 26）：
//   转换线 Tenkan  = (9 周期最高 + 最低) / 2         —— 短期动量
//   基准线 Kijun   = (26 周期最高 + 最低) / 2        —— 中期均衡（青线）
//   先行带 A SpanA = (Tenkan + Kijun) / 2 前移 26    —— 云的一条边
//   先行带 B SpanB = (52 周期最高 + 最低) / 2 前移 26 —— 云的另一条边
//   迟行线 Chikou  = 收盘价 后移 26                  —— 与历史价比对确认
//   云 Kumo = SpanA 与 SpanB 之间区域：A≥B 绿云（支撑），A<B 红云（压力）。
//   价格上方的云 = 头顶压力带，下方的云 = 脚下支撑带——与用户照片语义一致。
//
// 所有输出数组与输入 K 线按索引对齐；窗口不足处为 null。云的未来段
// （前移越过最后一根 K 线的 26 根）单独给出 logical 索引（n..n+25），
// 渲染层用 logicalToCoordinate 外推 x，不污染时间轴。

export interface IchimokuParams {
  tenkan: number;
  kijun: number;
  senkouB: number;
  /** 云前移 / 迟行线后移的位移量（标准 = kijun 周期 26） */
  displacement: number;
}

export const DEFAULT_ICHIMOKU_PARAMS: IchimokuParams = {
  tenkan: 9,
  kijun: 26,
  senkouB: 52,
  displacement: 26,
};

export interface IchimokuBar {
  high: number;
  low: number;
  close: number;
}

/** 云上一个显示位置的点：logical = bar 索引（未来段 > n-1） */
export interface CloudPoint {
  logical: number;
  a: number;
  b: number;
}

export interface IchimokuResult {
  /** 与输入同长；窗口不足为 null */
  tenkan: (number | null)[];
  kijun: (number | null)[];
  /** 前移后的先行带（显示索引对齐输入；[0, displacement) 与窗口不足为 null） */
  spanA: (number | null)[];
  spanB: (number | null)[];
  /** 后移后的迟行线（尾部 displacement 根为 null） */
  chikou: (number | null)[];
  /** 云全量点列（含历史 + 未来延伸段），logical 连续递增 */
  cloud: CloudPoint[];
  /** 未来云段起点 logical（= 输入长度 n）；无未来段时为 null */
  futureStart: number | null;
  params: IchimokuParams;
}

/** 滚动窗口 (最高+最低)/2；窗口不足为 null。O(n·w)——n≤300、w≤52，量级毫秒 */
function midline(bars: readonly IchimokuBar[], window: number): (number | null)[] {
  const out: (number | null)[] = new Array(bars.length).fill(null);
  for (let i = window - 1; i < bars.length; i++) {
    let hh = -Infinity;
    let ll = Infinity;
    for (let j = i - window + 1; j <= i; j++) {
      const b = bars[j];
      if (b.high > hh) hh = b.high;
      if (b.low < ll) ll = b.low;
    }
    out[i] = (hh + ll) / 2;
  }
  return out;
}

export function computeIchimoku(
  bars: readonly IchimokuBar[],
  params: IchimokuParams = DEFAULT_ICHIMOKU_PARAMS,
): IchimokuResult {
  const n = bars.length;
  const { displacement } = params;
  const tenkan = midline(bars, params.tenkan);
  const kijun = midline(bars, params.kijun);
  const spanBRaw = midline(bars, params.senkouB);

  // 原始先行带（未前移，锚定计算 bar）
  const spanARaw: (number | null)[] = new Array(n).fill(null);
  for (let i = 0; i < n; i++) {
    const t = tenkan[i];
    const k = kijun[i];
    if (t !== null && k !== null) spanARaw[i] = (t + k) / 2;
  }

  // 前移 displacement：显示索引 = 计算索引 + displacement
  const spanA: (number | null)[] = new Array(n).fill(null);
  const spanB: (number | null)[] = new Array(n).fill(null);
  for (let i = 0; i + displacement < n; i++) {
    spanA[i + displacement] = spanARaw[i];
    spanB[i + displacement] = spanBRaw[i];
  }

  // 迟行线：显示索引 = 计算索引 − displacement（尾部 displacement 根为空）
  const chikou: (number | null)[] = new Array(n).fill(null);
  for (let i = displacement; i < n; i++) {
    chikou[i - displacement] = bars[i].close;
  }

  // 云点列：历史段（显示索引 < n）+ 未来段（logical n..n-1+displacement）
  const cloud: CloudPoint[] = [];
  for (let i = 0; i < n; i++) {
    const a = spanA[i];
    const b = spanB[i];
    if (a !== null && b !== null) cloud.push({ logical: i, a, b });
  }
  let futureStart: number | null = null;
  for (let i = Math.max(0, n - displacement); i < n; i++) {
    const a = spanARaw[i];
    const b = spanBRaw[i];
    if (a === null || b === null) continue;
    const logical = i + displacement;
    if (logical < n) continue;
    if (futureStart === null) futureStart = n;
    cloud.push({ logical, a, b });
  }

  return { tenkan, kijun, spanA, spanB, chikou, cloud, futureStart, params };
}

// ────────────────────────────────────────────────────── 人话解读

export type IchimokuTone = 'bullish' | 'bearish' | 'neutral';

export interface IchimokuReadout {
  /** 一句话状态（状态提示条主文案） */
  text: string;
  /** 悬浮补充（组成细节，多行） */
  detail: string;
  tone: IchimokuTone;
}

function fmt(v: number): string {
  const dp = v >= 1000 ? 0 : v >= 10 ? 2 : 4;
  return v.toLocaleString('en-US', { maximumFractionDigits: dp });
}

/**
 * 按最新柱生成新手友好的多空解读：价与云位置 + TK 交叉 + 云厚 + 未来云拐点。
 * bars 长度不足（无云）返回 null。
 */
export function ichimokuReadout(
  bars: readonly IchimokuBar[],
  r: IchimokuResult,
): IchimokuReadout | null {
  const n = bars.length;
  if (n === 0) return null;
  const i = n - 1;
  const price = bars[i].close;
  const a = r.spanA[i];
  const b = r.spanB[i];
  const t = r.tenkan[i];
  const k = r.kijun[i];
  if (a === null || b === null) return null;

  const top = Math.max(a, b);
  const bottom = Math.min(a, b);
  const greenCloud = a >= b;
  const cloudDesc = `${fmt(bottom)}-${fmt(top)}`;
  // 云厚相对价格：<0.5% 算薄云（易穿），>1.5% 算厚云（强区）
  const thicknessPct = price > 0 ? ((top - bottom) / price) * 100 : 0;
  const thickness = thicknessPct >= 1.5 ? '厚' : thicknessPct < 0.5 ? '薄' : '';

  // TK 交叉（最新两根内发生才提示）
  let cross: 'golden' | 'dead' | null = null;
  const tPrev = r.tenkan[i - 1];
  const kPrev = r.kijun[i - 1];
  if (t !== null && k !== null && tPrev !== null && kPrev !== null) {
    if (tPrev <= kPrev && t > k) cross = 'golden';
    else if (tPrev >= kPrev && t < k) cross = 'dead';
  }

  // 未来云拐点：延伸段内 A/B 相对关系翻转 = 趋势酝酿变化
  let futureFlip: 'to_green' | 'to_red' | null = null;
  if (r.futureStart !== null) {
    const future = r.cloud.filter((p) => p.logical >= n);
    for (const p of future) {
      const g = p.a >= p.b;
      if (g !== greenCloud) {
        futureFlip = g ? 'to_green' : 'to_red';
        break;
      }
    }
  }

  let text: string;
  let tone: IchimokuTone;
  if (price > top) {
    tone = 'bullish';
    text = `价在云上，多头格局；脚下云带 ${cloudDesc} 是${thickness ? `${thickness}` : ''}${greenCloud ? '支撑区' : '支撑区（红云偏弱）'}`;
    if (cross === 'golden') text = `TK 金叉 + 价在云上，看涨共振；回踩 ${cloudDesc} 云区是关注点`;
  } else if (price < bottom) {
    tone = 'bearish';
    text = `价在云下，空头格局；头顶云带 ${cloudDesc} 是${thickness ? `${thickness}` : ''}压力区`;
    if (cross === 'dead') text = `TK 死叉 + 价在云下，看跌共振；反弹到 ${cloudDesc} 云区留意受阻`;
  } else {
    tone = 'neutral';
    text = `价在云中（${cloudDesc}），多空争夺，观望为主`;
  }
  if (futureFlip) {
    text += futureFlip === 'to_green' ? '；未来云翻绿，趋势酝酿转多' : '；未来云翻红，趋势酝酿转空';
  }

  const detailLines = [
    `转换线 Tenkan(9)=${t !== null ? fmt(t) : '—'} · 基准线 Kijun(26)=${k !== null ? fmt(k) : '—'}`,
    `云带 SpanA=${fmt(a)} / SpanB=${fmt(b)}（${greenCloud ? '绿云=看涨' : '红云=看跌'}，厚度 ${thicknessPct.toFixed(2)}%）`,
    cross === 'golden' ? '刚发生 TK 金叉（转换线上穿基准线，短线转强）' : cross === 'dead' ? '刚发生 TK 死叉（转换线下穿基准线，短线转弱）' : null,
    '口诀：云上做多、云下做空、云中观望；云越厚支撑/压力越强',
  ].filter((s): s is string => s !== null);

  return { text, detail: detailLines.join('\n'), tone };
}

/** 悬停某根 K 线时的五线数值文案（tooltip 用）；该 bar 无云数据返回 null */
export function ichimokuTipAt(r: IchimokuResult, index: number): string | null {
  const parts: string[] = [];
  const push = (label: string, v: number | null | undefined) => {
    if (v !== null && v !== undefined) parts.push(`${label} ${fmt(v)}`);
  };
  push('转换', r.tenkan[index]);
  push('基准', r.kijun[index]);
  push('先行A', r.spanA[index]);
  push('先行B', r.spanB[index]);
  push('迟行', r.chikou[index]);
  return parts.length > 0 ? `云图 · ${parts.join(' · ')}` : null;
}
