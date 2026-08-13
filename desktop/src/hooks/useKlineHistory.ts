// useKlineHistory — K 线「实时轮询窗口 + 向前分页历史」组合 hook。
//
// 实时窗口沿用 usePolling 原节奏（1m 档 10s、其余 60s），行为与旧版
// usePolling(api.kline) 完全一致；历史页经 loadOlder() 以 end_time 游标向前
// 拉取并前插，按 symbol|interval 键控，切换即整组清空。合并去重由
// lib/klineHistory 纯函数完成（live 覆盖同 ts 历史行，未收线蜡烛不回退）。
//
// loadOlder 防重入（ref 守卫）：图表左缘的 visible-range 事件可能连发，
// 进行中/已到头/尚无数据时直接忽略；网络失败保留 hasMoreHistory，用户
// 再拖即重试。返回不足一整页视为历史尽头。

import { useState, useMemo, useRef, useCallback } from "react";
import { api } from "@/api/client";
import { usePolling } from "./useApi";
import {
  extractKlineRows,
  mergeKlineRows,
  olderPageCursor,
  type KlineRow,
} from "@/lib/klineHistory";

/** 总根数护栏：防连续分页把内存拖爆（≈10+ 页，远超肉眼回看需求） */
const MAX_TOTAL_BARS = 3000;

/**
 * 已加载历史段的内存缓存（symbol|interval → 历史页 + 到头标记）：
 * 来回切 TF/币种时免重新逐页拉取（历史已收线不会变，天然可缓存；
 * 实时窗口不缓存，仍由轮询保持新鲜）。LRU 上限防长会话内存膨胀
 * （12 键 × ≤3000 行，量级远小于一张截图）。
 */
const olderCache = new Map<string, { rows: KlineRow[]; hasMore: boolean }>();
const OLDER_CACHE_MAX_KEYS = 12;

function cacheOlderPages(key: string, rows: KlineRow[], hasMore: boolean): void {
  olderCache.delete(key);
  olderCache.set(key, { rows, hasMore });
  while (olderCache.size > OLDER_CACHE_MAX_KEYS) {
    const oldest = olderCache.keys().next().value;
    if (oldest === undefined) break;
    olderCache.delete(oldest);
  }
}

/** 测试专用：清空历史段缓存 */
export function clearOlderCacheForTest(): void {
  olderCache.clear();
}

export interface UseKlineHistoryResult {
  /** 历史页 + 实时窗口合并后的全量行（ts 升序） */
  rows: KlineRow[];
  /** 首屏实时窗口加载中 */
  loading: boolean;
  error: string | null;
  /** 正在拉更早历史 */
  loadingOlder: boolean;
  /** 是否还有更早历史可拉（到头/超护栏后 false） */
  hasMoreHistory: boolean;
  /** 向前加载一页更早历史（防重入，可被左缘事件高频调用） */
  loadOlder: () => void;
}

export function useKlineHistory(
  symbol: string,
  interval: string,
  limit: number,
  pollMs: number,
): UseKlineHistoryResult {
  // 实时窗口：与旧版 usePolling(api.kline) 同参数同节奏
  const { data: liveRaw, loading, error } = usePolling(
    () => api.kline(symbol, interval, limit),
    pollMs,
    [interval, symbol],
  );
  // 切币种/周期竞态防护（2026-08-09 切 5m↔15m 蜡烛拉宽的根因）：
  // usePolling 在依赖变化后仍保留旧参数的 data，一个渲染周期内会出现
  // 「新 datasetKey + 旧周期行」的组合——KlineChart 会在旧数据上 fitContent
  // 并把视口保持基准（prevDatasetKeyRef）提前记成新 key，等新周期数据到达
  // 时被误判为同数据集增量更新，恢复按旧周期算出的可视时间区间，蜡烛
  // 被拉宽/压扁。只放行封套 symbol/interval 与当前请求一致的响应，让
  // 数据与 datasetKey 原子切换。
  const liveRows = useMemo(() => {
    const env = liveRaw as { symbol?: string; interval?: string } | null;
    const want = symbol.toUpperCase().replace(/[-/]/g, "");
    const respSym = (env?.symbol ?? "").toUpperCase();
    const symOk = respSym === want || respSym === `${want}USDT`;
    if (!env || !symOk || env.interval !== interval) return [];
    return extractKlineRows(liveRaw);
  }, [liveRaw, symbol, interval]);

  const [older, setOlder] = useState<KlineRow[]>([]);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);
  const keyRef = useRef(`${symbol}|${interval}`);
  const olderRef = useRef<KlineRow[]>([]);
  const inFlightRef = useRef(false);

  // 切币种/周期：渲染期同步切换（React「adjusting state when props change」
  // 模式），先查段缓存（切回免重新分页），未命中整组清空复位。
  // [R4] 必须在渲染期而非 effect 里做：effect 晚一帧执行会产生「新 datasetKey +
  // 旧周期历史页」的单帧窗口——KlineChart 在这帧 fitContent 并记住新键，真数据
  // 到达时被误判为同数据集增量更新、恢复旧周期的可见时间区间，蜡烛被拉宽/压细
  // （liveRows 的回声校验只保护了实时窗，历史页此前无保护）。
  const [appliedKey, setAppliedKey] = useState(`${symbol}|${interval}`);
  const key = `${symbol}|${interval}`;
  if (appliedKey !== key) {
    setAppliedKey(key);
    const cached = olderCache.get(key);
    olderRef.current = cached?.rows ?? [];
    setOlder(olderRef.current);
    setHasMoreHistory(cached?.hasMore ?? true);
    setLoadingOlder(false);
    keyRef.current = key;
    inFlightRef.current = false;
  }

  const rows = useMemo(() => mergeKlineRows(older, liveRows), [older, liveRows]);

  const loadOlder = useCallback(() => {
    if (inFlightRef.current || !hasMoreHistory) return;
    const cursor = olderPageCursor(rows);
    if (cursor === null) return; // 实时窗口尚未就绪
    if (rows.length >= MAX_TOTAL_BARS) {
      setHasMoreHistory(false);
      return;
    }
    inFlightRef.current = true;
    setLoadingOlder(true);
    const key = keyRef.current;
    (async () => {
      try {
        const raw = await api.kline(symbol, interval, limit, cursor);
        if (keyRef.current !== key) return; // 已切币种/周期，丢弃慢响应
        // 只收严格不晚于游标的行（防交易所端点口径差异造成重复）
        const page = extractKlineRows(raw).filter((r) => r.ts <= cursor);
        if (page.length === 0) {
          setHasMoreHistory(false);
          cacheOlderPages(key, olderRef.current, false);
          return;
        }
        const merged = mergeKlineRows(page, olderRef.current);
        olderRef.current = merged;
        setOlder(merged);
        const more = page.length >= limit; // 不足一页 = 到头
        if (!more) setHasMoreHistory(false);
        cacheOlderPages(key, merged, more);
      } catch {
        // 网络失败：保留 hasMoreHistory，用户再向左拖即重试
      } finally {
        if (keyRef.current === key) {
          inFlightRef.current = false;
          setLoadingOlder(false);
        }
      }
    })();
  }, [rows, hasMoreHistory, symbol, interval, limit]);

  return { rows, loading, error, loadingOlder, hasMoreHistory, loadOlder };
}
