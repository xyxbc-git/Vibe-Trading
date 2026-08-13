// 非散户占比·本机滚动基线（U2）：给「28.5%」一个参照系。
//
// 后端接口不提供 30 日基准（本次任务红线：数据接口不动），采用诚实的替代口径：
// 本机每次看到画像数据就把 non_retail_share_pct 记入 localStorage（60s 节流、
// 按 UTC 日聚合成当日 min/max 包络、按币种隔离、保留 ≤30 日）。展示为
// 「近 N 日本机观测区间」——它是观测所得不是全量统计，样本不足 3 日时
// 只显示「基准累积中」，绝不假装有基准（与全系统诚实化改造同一原则）。
//
// 纯函数核心（mergeSample/rangeInfo）离线可测；storage 读写全程防御。

const KEY = "jarvis.tape.nonretail.baseline.v1";
/** 同币种两次记录的最小间隔（3s 轮询下防止写爆 storage） */
const RECORD_THROTTLE_MS = 60_000;
/** 保留天数上限 */
export const BASELINE_MAX_DAYS = 30;
/** 给出区间位置结论所需的最少观测天数（不足只报「累积中」） */
export const BASELINE_MIN_DAYS = 3;

/** 单日包络：UTC epoch 日序号 + 当日观测 min/max（百分数 0-100） */
export interface DayEnvelope {
  d: number;
  lo: number;
  hi: number;
}

type Store = Record<string, DayEnvelope[]>;

export function epochDay(nowMs: number): number {
  return Math.floor(nowMs / 86_400_000);
}

/** 把一个观测值并入包络序列（纯函数）：同日扩展 min/max，跨日新增，裁剪过期。 */
export function mergeSample(
  days: DayEnvelope[],
  pct: number,
  nowMs: number,
  maxDays: number = BASELINE_MAX_DAYS,
): DayEnvelope[] {
  if (!Number.isFinite(pct)) return days;
  const p = Math.max(0, Math.min(100, pct));
  const today = epochDay(nowMs);
  const cutoff = today - maxDays + 1;
  const kept = days.filter((e) => e.d >= cutoff && e.d <= today);
  const cur = kept.find((e) => e.d === today);
  if (cur) {
    cur.lo = Math.min(cur.lo, p);
    cur.hi = Math.max(cur.hi, p);
  } else {
    kept.push({ d: today, lo: p, hi: p });
  }
  kept.sort((a, b) => a.d - b.d);
  return kept;
}

export interface RangeInfo {
  /** 观测天数 */
  days: number;
  /** 近 N 日观测区间（跨日 min/max 包络合并） */
  lo: number;
  hi: number;
  /** 当前值在区间内的位置 0-100（区间退化时 50）；days<MIN_DAYS 时为 null */
  posPct: number | null;
}

/** 区间画像（纯函数）：不足 BASELINE_MIN_DAYS 天不给位置结论。 */
export function rangeInfo(
  days: DayEnvelope[],
  currentPct: number,
  minDays: number = BASELINE_MIN_DAYS,
): RangeInfo | null {
  if (days.length === 0) return null;
  const lo = Math.min(...days.map((e) => e.lo));
  const hi = Math.max(...days.map((e) => e.hi));
  const n = days.length;
  if (n < minDays) return { days: n, lo, hi, posPct: null };
  const span = hi - lo;
  const pos =
    span <= 0.5
      ? 50
      : Math.max(0, Math.min(100, ((currentPct - lo) / span) * 100));
  return { days: n, lo, hi, posPct: Math.round(pos) };
}

// ─────────────────── storage 层（防御式，不可用时静默降级） ───────────────────

function loadStore(): Store {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Store)
      : {};
  } catch {
    return {};
  }
}

const lastRecordAt: Record<string, number> = {};

/** 记录一次观测（60s 节流；storage 不可用静默）。 */
export function recordShare(symbol: string, pct: number, nowMs: number = Date.now()): void {
  if (!symbol || !Number.isFinite(pct)) return;
  const last = lastRecordAt[symbol] ?? 0;
  if (nowMs - last < RECORD_THROTTLE_MS) return;
  lastRecordAt[symbol] = nowMs;
  try {
    const store = loadStore();
    store[symbol] = mergeSample(
      Array.isArray(store[symbol]) ? store[symbol] : [],
      pct,
      nowMs,
    );
    localStorage.setItem(KEY, JSON.stringify(store));
  } catch {
    /* storage 不可用——基线只是参照系，不阻塞画像展示 */
  }
}

/** 读取某币种的区间画像；无记录返回 null。 */
export function shareBaseline(
  symbol: string,
  currentPct: number,
  nowMs: number = Date.now(),
): RangeInfo | null {
  try {
    const store = loadStore();
    const days = Array.isArray(store[symbol]) ? store[symbol] : [];
    const cutoff = epochDay(nowMs) - BASELINE_MAX_DAYS + 1;
    return rangeInfo(days.filter((e) => e.d >= cutoff), currentPct);
  } catch {
    return null;
  }
}
