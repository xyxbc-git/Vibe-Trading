import { createContext, useContext, useMemo, type ReactNode } from "react";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/useApi";
import { useSymbol } from "@/hooks/useSymbol";

/**
 * 全局实时现价（ticker）共享层。
 *
 * 背景：顶部导航栏与 K 线图此前各走一条价格链路——顶栏 10s 轮询
 * /alerts/price（Binance ticker 实时价，后端无缓存），K 线 60s 轮询
 * /kline（后端另有 60s 缓存），最坏相差约 2 分钟，导致两处价格肉眼可见
 * 不一致。把 ticker 轮询提升到 Provider 级别后，顶栏与图表消费同一份
 * 数据、同一时刻更新，且全应用只保留一路 10s 轮询。
 */
export interface LivePrice {
  /** 后端规范化后的交易对（如 BTCUSDT） */
  symbol: string;
  price: number;
  /** 本地收到报价的时刻（ms epoch） */
  at: number;
}

/** ticker 轮询周期（顶栏倒计时与 Provider 共用同一常量，避免视觉与真实节奏漂移） */
export const PRICE_POLL_INTERVAL_MS = 10_000;

/** 轮询元信息：供顶栏倒计时等 UI 反映真实刷新节奏，而非独立计时造假 */
export interface PriceMeta {
  /** 最近一次成功收到报价的时刻（ms epoch）；尚无成功请求时为 null */
  lastFetchAt: number | null;
  /** 最近一次请求的失败信息；成功后自动清空 */
  error: string | null;
  /** 轮询周期（ms） */
  intervalMs: number;
}

const PriceContext = createContext<LivePrice | null>(null);
const PriceMetaContext = createContext<PriceMeta>({
  lastFetchAt: null,
  error: null,
  intervalMs: PRICE_POLL_INTERVAL_MS,
});

export function PriceProvider({ children }: { children: ReactNode }) {
  const { symbol } = useSymbol();
  // 复用价位提醒的轻量接口（后端 Binance 主源 / OKX 兜底）
  const { data, error } = usePolling(
    () => api.alertPrice(symbol),
    PRICE_POLL_INTERVAL_MS,
    [symbol],
  );

  const value = useMemo<LivePrice | null>(() => {
    if (data?.price == null) return null;
    return { symbol: data.symbol, price: data.price, at: Date.now() };
  }, [data]);

  const meta = useMemo<PriceMeta>(
    () => ({
      lastFetchAt: value?.at ?? null,
      error: error ?? null,
      intervalMs: PRICE_POLL_INTERVAL_MS,
    }),
    [value, error],
  );

  return (
    <PriceContext.Provider value={value}>
      <PriceMetaContext.Provider value={meta}>
        {children}
      </PriceMetaContext.Provider>
    </PriceContext.Provider>
  );
}

/**
 * 最近一次 ticker 报价；尚无数据时为 null。
 * 消费方须自行校验 symbol 是否与当前选中币种一致（切币瞬间可能残留旧币报价）。
 */
export function useLivePrice(): LivePrice | null {
  return useContext(PriceContext);
}

/** ticker 轮询元信息（上次成功刷新时刻 / 失败状态 / 周期） */
export function usePriceMeta(): PriceMeta {
  return useContext(PriceMetaContext);
}
