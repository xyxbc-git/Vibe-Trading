// Volume Profile（成交量分布/钟形曲线）纯计算模块。
// 把一段柱的 levels 按价位聚合成横向分布，并产出 POC / 价值区（70%）/
// HVN·LVN 节点与形态结论。渲染与解读共用同一份结果。
import type { FootprintBar } from "@/types/footprint";
import { isOhlcOnly } from "./renderer";

export type ProfileMode = "visible" | "session";

export interface ProfileRow {
  price: number;
  vol: number;
  bidVol: number;
  askVol: number;
}

export type ProfileShape = "bell" | "double" | "skew";

export interface VolumeProfile {
  /** 按 price 降序（与足迹 levels 一致） */
  rows: ProfileRow[];
  maxRowVol: number;
  totalVol: number;
  poc: number;
  /** 价值区上/下边界（覆盖 ~70% 总量） */
  vah: number;
  val: number;
  /** 高量节点（磁吸位）/ 低量节点（真空区），最多各 3 个 */
  hvns: number[];
  lvns: number[];
  /** 分布形态：bell=对称钟形（平衡市）double=双峰（P/b 型）skew=偏态 */
  shape: ProfileShape;
  /** 次峰价位（double 时有值） */
  secondPeak: number | null;
  /** 聚合覆盖的柱数与被跳过的 ohlcOnly 柱数 */
  coveredBars: number;
  skippedBars: number;
}

/**
 * 聚合区间 [start, end) 内柱的价位量分布。
 * tick 用于把浮点价位归到统一网格（避免相邻档位因精度分裂）。
 */
export function computeVolumeProfile(
  bars: FootprintBar[],
  start: number,
  end: number,
  tick: number,
): VolumeProfile | null {
  const t = tick > 0 && Number.isFinite(tick) ? tick : 1;
  const acc = new Map<number, ProfileRow>();
  let covered = 0;
  let skipped = 0;

  for (let i = Math.max(0, start); i < Math.min(bars.length, end); i++) {
    const bar = bars[i];
    if (!bar) continue;
    if (isOhlcOnly(bar)) {
      skipped++;
      continue;
    }
    covered++;
    for (const lv of bar.levels) {
      const key = Math.round(lv.price / t);
      let row = acc.get(key);
      if (!row) {
        row = { price: key * t, vol: 0, bidVol: 0, askVol: 0 };
        acc.set(key, row);
      }
      row.bidVol += lv.bidVol;
      row.askVol += lv.askVol;
      row.vol += lv.bidVol + lv.askVol;
    }
  }
  if (acc.size < 3) return null;

  const rows = [...acc.values()].sort((a, b) => b.price - a.price);
  let totalVol = 0;
  let maxRowVol = 0;
  let pocIdx = 0;
  rows.forEach((r, i) => {
    totalVol += r.vol;
    if (r.vol > maxRowVol) {
      maxRowVol = r.vol;
      pocIdx = i;
    }
  });
  if (totalVol <= 0) return null;

  // 价值区 70%：从 POC 出发，每步并入上/下相邻行中量更大的一侧
  let lo = pocIdx;
  let hi = pocIdx;
  let vaVol = rows[pocIdx].vol;
  const target = totalVol * 0.7;
  while (vaVol < target && (hi > 0 || lo < rows.length - 1)) {
    const upVol = hi > 0 ? rows[hi - 1].vol : -1;
    const dnVol = lo < rows.length - 1 ? rows[lo + 1].vol : -1;
    if (upVol >= dnVol) {
      hi--;
      vaVol += rows[hi].vol;
    } else {
      lo++;
      vaVol += rows[lo].vol;
    }
  }
  const vah = rows[hi].price;
  const val = rows[lo].price;

  // HVN/LVN：与 5 行邻域均值比（HVN ≥1.8x 且为局部最大；LVN ≤0.4x）
  const hvns: number[] = [];
  const lvns: number[] = [];
  const win = 2;
  for (let i = 0; i < rows.length; i++) {
    let sum = 0;
    let cnt = 0;
    for (let j = Math.max(0, i - win); j <= Math.min(rows.length - 1, i + win); j++) {
      if (j === i) continue;
      sum += rows[j].vol;
      cnt++;
    }
    if (cnt === 0) continue;
    const nbr = sum / cnt;
    const isLocalMax =
      (i === 0 || rows[i].vol >= rows[i - 1].vol) &&
      (i === rows.length - 1 || rows[i].vol >= rows[i + 1].vol);
    if (rows[i].vol >= nbr * 1.8 && isLocalMax && rows[i].price !== rows[pocIdx].price) {
      hvns.push(rows[i].price);
    } else if (rows[i].vol <= nbr * 0.4 && rows[i].vol < maxRowVol * 0.25) {
      lvns.push(rows[i].price);
    }
  }
  hvns.sort((a, b) => byVolDesc(a, b, acc, t));
  lvns.sort((a, b) => byVolDesc(b, a, acc, t));
  hvns.splice(3);
  lvns.splice(3);

  // 形态识别：
  // double —— 存在与 POC 距离 ≥20% 档数、量 ≥60% POC 的次峰（P/b 型，趋势尾声特征）
  // bell   —— POC 落在价值区中段（分布对称，平衡市）
  // skew   —— 其余（头重脚轻/脚重头轻的偏态）
  let secondPeak: number | null = null;
  const minDist = Math.max(2, Math.round(rows.length * 0.2));
  let bestPeakVol = 0;
  for (let i = 0; i < rows.length; i++) {
    if (Math.abs(i - pocIdx) < minDist) continue;
    const isLocalMax =
      (i === 0 || rows[i].vol >= rows[i - 1].vol) &&
      (i === rows.length - 1 || rows[i].vol >= rows[i + 1].vol);
    if (isLocalMax && rows[i].vol >= maxRowVol * 0.6 && rows[i].vol > bestPeakVol) {
      bestPeakVol = rows[i].vol;
      secondPeak = rows[i].price;
    }
  }
  let shape: ProfileShape;
  if (secondPeak != null) {
    shape = "double";
  } else {
    const mid = (vah + val) / 2;
    const vaSpan = Math.max(vah - val, t);
    shape = Math.abs(rows[pocIdx].price - mid) <= vaSpan * 0.25 ? "bell" : "skew";
  }

  return {
    rows,
    maxRowVol,
    totalVol,
    poc: rows[pocIdx].price,
    vah,
    val,
    hvns,
    lvns,
    shape,
    secondPeak,
    coveredBars: covered,
    skippedBars: skipped,
  };
}

function byVolDesc(
  a: number,
  b: number,
  acc: Map<number, ProfileRow>,
  t: number,
): number {
  const va = acc.get(Math.round(a / t))?.vol ?? 0;
  const vb = acc.get(Math.round(b / t))?.vol ?? 0;
  return vb - va;
}

/** 今日 0 点（本地时区）毫秒时间戳 */
export function sessionStartMs(now = Date.now()): number {
  const d = new Date(now);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}
