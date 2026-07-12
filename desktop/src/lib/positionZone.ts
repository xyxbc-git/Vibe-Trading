// 信号多空点位 → K 线「多空区间图」（TradingView Long/Short Position 风格）。
//
// 信号矩阵每张卡的交易计划（entry/stop/target 三价 + 方向）点击「K线区间」后
// 经 URL query 跳到 K 线页，在图上画：
//   做多：入场→止盈 绿色半透明收益区在上，入场→止损 红色风险区在下；做空镜像
//   三条边界水平线（入/损/盈）+ 价格标注 + 盈亏比文字
//   区块从最新 K 线向右延伸固定根数（TradingView position 工具画法）
// 本模块保持纯函数（几何/编解码/校验），渲染交给 PositionZonePrimitive。

export interface PositionZoneParams {
  side: "long" | "short";
  entry: number;
  stopLoss: number;
  takeProfit: number;
  /** 信号系统中文名（状态条与区块标注用） */
  name?: string;
  /** 信号计算周期（跳转后图表自动对齐一次） */
  tf?: string;
}

/** 渲染层消费的区间视图：收益/风险矩形 + 盈亏比（几何已按多空校验） */
export interface PositionZoneView {
  side: "long" | "short";
  entry: number;
  stopLoss: number;
  takeProfit: number;
  /** reward/risk（>0） */
  rr: number;
  /** 收益区（top > bottom） */
  profit: { top: number; bottom: number };
  /** 风险区（top > bottom） */
  risk: { top: number; bottom: number };
  name?: string;
}

/** 区块从最新 K 线向右延伸的根数（固定宽度，逻辑索引允许越过最后一根） */
export const POSITION_ZONE_EXTEND_BARS = 18;

const isPos = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v) && v > 0;

/**
 * 三价 + 方向 → 区间视图。几何不合法（做多要求 SL < entry < TP、做空镜像、
 * 任一价缺失/非正）返回 null，调用方不渲染。
 */
export function buildPositionZoneView(
  p: PositionZoneParams | null | undefined,
): PositionZoneView | null {
  if (!p) return null;
  const { side, entry, stopLoss, takeProfit } = p;
  if (side !== "long" && side !== "short") return null;
  if (!isPos(entry) || !isPos(stopLoss) || !isPos(takeProfit)) return null;
  const sign = side === "short" ? -1 : 1;
  const risk = (entry - stopLoss) * sign;
  const reward = (takeProfit - entry) * sign;
  if (risk <= 0 || reward <= 0) return null;
  return {
    side,
    entry,
    stopLoss,
    takeProfit,
    rr: Math.round((reward / risk) * 100) / 100,
    profit: {
      top: Math.max(entry, takeProfit),
      bottom: Math.min(entry, takeProfit),
    },
    risk: {
      top: Math.max(entry, stopLoss),
      bottom: Math.min(entry, stopLoss),
    },
    name: p.name,
  };
}

/**
 * 区块时间窗口：锚定最新 K 线，向右延伸 extend 根（TradingView position
 * 工具「从当前时刻向右」画法）。无 K 线返回 null 不绘制。
 */
export function positionZoneWindow(
  barCount: number,
  extend: number = POSITION_ZONE_EXTEND_BARS,
): { from: number; to: number } | null {
  if (!Number.isFinite(barCount) || barCount <= 0) return null;
  const lastIdx = Math.floor(barCount) - 1;
  return { from: lastIdx, to: lastIdx + Math.max(1, Math.floor(extend)) };
}

// ── URL query 编解码（信号矩阵 → K 线页跳转载体） ─────────────────────────
//
// 键名带 pz 前缀，与「盈损点」的 sig* 参数正交，两者可独立清除互不干扰。

const QK = {
  side: "pzside",
  entry: "pzentry",
  sl: "pzsl",
  tp: "pztp",
  name: "pzname",
  tf: "pztf",
} as const;

/** 区间图参数 → URLSearchParams（SignalBoard 跳转用） */
export function positionZoneToQuery(p: PositionZoneParams): URLSearchParams {
  const q = new URLSearchParams();
  q.set(QK.side, p.side);
  q.set(QK.entry, String(p.entry));
  q.set(QK.sl, String(p.stopLoss));
  q.set(QK.tp, String(p.takeProfit));
  if (p.name) q.set(QK.name, p.name);
  if (p.tf) q.set(QK.tf, p.tf);
  return q;
}

/** URLSearchParams → 区间图参数；键缺失/数值非法返回 null（Chart 页解析用） */
export function positionZoneFromQuery(
  q: URLSearchParams,
): PositionZoneParams | null {
  const side = q.get(QK.side);
  if (side !== "long" && side !== "short") return null;
  const entry = parseFloat(q.get(QK.entry) ?? "");
  const stopLoss = parseFloat(q.get(QK.sl) ?? "");
  const takeProfit = parseFloat(q.get(QK.tp) ?? "");
  if (!isPos(entry) || !isPos(stopLoss) || !isPos(takeProfit)) return null;
  return {
    side,
    entry,
    stopLoss,
    takeProfit,
    name: q.get(QK.name) ?? undefined,
    tf: q.get(QK.tf) ?? undefined,
  };
}

/** 从 URLSearchParams 中删除全部区间图键（清除按钮用，不动其它参数） */
export function stripPositionZoneQuery(q: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(q);
  for (const k of Object.values(QK)) next.delete(k);
  return next;
}
