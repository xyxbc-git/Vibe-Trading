// K 线历史分页的纯函数层：/api/kline 行提取 + 历史页与实时窗口合并去重 +
// 向前分页游标计算。无 IO、无图表依赖，供 useKlineHistory hook 与单测使用。
//
// 分页契约：GET /api/kline?symbol=&interval=&limit=&end_time=<毫秒>
// end_time 为「返回该时刻及之前的 limit 根」的游标（透传交易所 endTime）；
// 下一页游标 = 当前最早一根的 ts - 1（毫秒），避免端点重复。

/** /api/kline 响应 rows 单行（ts 为毫秒） */
export interface KlineRow {
  t?: string;
  ts: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v?: number;
}

/** 从 /api/kline 响应提取合法行（非封套/非数组/字段非法的行一律丢弃） */
export function extractKlineRows(raw: unknown): KlineRow[] {
  const rows = (raw as Record<string, unknown> | null | undefined)?.rows;
  if (!Array.isArray(rows)) return [];
  const out: KlineRow[] = [];
  for (const r of rows as Record<string, unknown>[]) {
    const ts = Number(r?.ts);
    const o = Number(r?.o);
    const h = Number(r?.h);
    const l = Number(r?.l);
    const c = Number(r?.c);
    if (![ts, o, h, l, c].every(Number.isFinite) || ts <= 0) continue;
    out.push({
      t: typeof r.t === "string" ? r.t : undefined,
      ts,
      o,
      h,
      l,
      c,
      v: Number.isFinite(Number(r?.v)) ? Number(r.v) : undefined,
    });
  }
  return out;
}

/**
 * 历史页与实时窗口合并：按 ts 去重（live 优先——实时窗口内的未收线蜡烛
 * 会随轮询更新，必须覆盖同 ts 的历史行），输出按 ts 升序。
 */
export function mergeKlineRows(older: KlineRow[], live: KlineRow[]): KlineRow[] {
  if (older.length === 0) return live;
  if (live.length === 0) return older;
  const byTs = new Map<number, KlineRow>();
  for (const r of older) byTs.set(r.ts, r);
  for (const r of live) byTs.set(r.ts, r);
  return [...byTs.values()].sort((a, b) => a.ts - b.ts);
}

/** 下一页（更早历史）的 end_time 游标：当前最早 ts - 1 毫秒；空数组返回 null */
export function olderPageCursor(rows: KlineRow[]): number | null {
  if (rows.length === 0) return null;
  let min = rows[0].ts;
  for (const r of rows) if (r.ts < min) min = r.ts;
  return min - 1;
}

/**
 * [R4] 数据集时间戳间隔一致性：全部相邻间隔都是 intervalMs 的正整数倍
 * （允许缺口=倍数>1，绝不允许小于一个周期的间隔——那是混入了更小周期的行）。
 * 切周期的单帧窗口若把旧周期历史页拼进新周期数据，本判定即暴露。
 */
export function rowsIntervalConsistent(rows: readonly KlineRow[], intervalMs: number): boolean {
  if (!(intervalMs > 0)) return false;
  for (let i = 1; i < rows.length; i++) {
    const gap = rows[i].ts - rows[i - 1].ts;
    if (gap <= 0 || gap % intervalMs !== 0) return false;
  }
  return true;
}
