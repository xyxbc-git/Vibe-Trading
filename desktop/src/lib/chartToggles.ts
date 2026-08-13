// K 线页/足迹页指标开关统一持久化层（R6）：刷新/重启后恢复用户开过的按钮。
//
// 单键 JSON 对象（jarvis.chart.toggles.v1），按开关名存布尔/字符串/数组；
// 全局一份不按 symbol 隔离（开关表达的是「我看盘要哪些层」的个人偏好，
// 与币种无关）。读写全程防御：storage 不可用/JSON 损坏一律回退空对象，
// 开关仍可用只是不持久化；合并写回保留未知字段（向前兼容未来新增开关，
// 旧版本页面不吞新版本写入的键）。
//
// 历史遗留的分散键（viewMode / ichimoku / trap / wyckoff / 足迹 vpOn·vpMode）
// 保持原键不迁移——避免升级瞬间丢用户既有偏好；新增开关一律进本层。

const KEY = "jarvis.chart.toggles.v1";

/** 已知开关字段（消费方按需取用；存储中允许存在未知字段） */
export interface ChartToggles {
  /** K 线页：智能画线 */
  smart?: boolean;
  /** K 线页：画线自调参 */
  autoTune?: boolean;
  /** K 线页：十二套关键位 */
  twelve?: boolean;
  /** K 线页：交易计划线 */
  plan?: boolean;
  /** K 线页：走势预测层 */
  predictOn?: boolean;
  /** K 线页：形态分析 */
  patternOn?: boolean;
  /** K 线页：磁吸位（清算地图） */
  liqOn?: boolean;
  /** K 线页：MACD 副图 */
  macdOn?: boolean;
  /** K 线页：Delta 副图 */
  deltaOn?: boolean;
  /** K 线页：FVG 失衡区叠加（R8） */
  fvgOn?: boolean;
  /** K 线页：BOS/CHoCH 结构线叠加（R8 追加） */
  bosOn?: boolean;
  /** K 线页：折溢价区参考线（R9 独立开关，默认关） */
  pdOn?: boolean;
  /** K 线页：手动画线模式集合（DrawMode 名） */
  draws?: string[];
  /** K 线页：周期档 */
  tf?: string;
  /** 足迹页：周期档 */
  fpTf?: string;
  /** 未知字段透传（向前兼容） */
  [k: string]: unknown;
}

export function loadChartToggles(): ChartToggles {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as ChartToggles)
      : {};
  } catch {
    return {};
  }
}

/** 读-合并-写：只更新传入字段，保留其它（含未知）字段 */
export function saveChartToggles(patch: Partial<ChartToggles>): void {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...loadChartToggles(), ...patch }));
  } catch {
    /* storage 不可用 — 开关仍生效，只是不持久化 */
  }
}

/** 布尔字段读取（缺失/非布尔回退默认值） */
export function toggleOr(t: ChartToggles, key: string, fallback: boolean): boolean {
  const v = t[key];
  return typeof v === "boolean" ? v : fallback;
}

/** 字符串枚举字段读取（不在白名单内回退默认值） */
export function enumOr<T extends string>(
  t: ChartToggles,
  key: string,
  allowed: readonly T[],
  fallback: T,
): T {
  const v = t[key];
  return typeof v === "string" && (allowed as readonly string[]).includes(v)
    ? (v as T)
    : fallback;
}

/** 字符串数组字段读取（过滤非白名单项；非数组回退空） */
export function listOr<T extends string>(
  t: ChartToggles,
  key: string,
  allowed: readonly T[],
): T[] {
  const v = t[key];
  if (!Array.isArray(v)) return [];
  return v.filter(
    (x): x is T => typeof x === "string" && (allowed as readonly string[]).includes(x),
  );
}
