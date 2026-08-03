import { useState, useEffect, useCallback, useRef } from "react";

interface UseApiResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

/**
 * 取数内核。请求序号（seq）只负责一件事：参数切换 / 新一轮请求启动后，
 * 丢弃更早发出的旧响应，防止旧参数的慢响应乱序覆盖新数据。
 */
function useApiCore<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
): UseApiResult<T> & { refetchAsync: () => Promise<void> } {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const seqRef = useRef(0);

  const refetchAsync = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);
    try {
      const d = await fetcherRef.current();
      if (seq === seqRef.current) setData(d);
    } catch (e) {
      if (seq === seqRef.current) setError((e as Error).message);
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refetchAsync();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, loading, error, refetch: refetchAsync, refetchAsync };
}

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
): UseApiResult<T> {
  return useApiCore(fetcher, deps);
}

/**
 * 轮询取数：上一次请求「落地」（成功/失败均可）之后，再隔 intervalMs 排下一次。
 *
 * 之前用固定 setInterval 触发 refetch：一旦单次请求耗时持续超过间隔
 * （上游慢 / 超时 / 挂起），每个响应到达时都已经有更新的 seq 在跑，
 * data 和 error 永远写不进 state —— 页面（如盘口深度阶梯）无限转圈。
 * 链式调度保证同参数下永不并发请求，每个响应必然落地，慢后端只是把
 * 实际轮询节奏自然放慢（间隔 = 响应耗时 + intervalMs），不再丢结果。
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number = 30_000,
  deps: unknown[] = [],
): UseApiResult<T> {
  const core = useApiCore(fetcher, deps);
  const refetchAsyncRef = useRef(core.refetchAsync);
  refetchAsyncRef.current = core.refetchAsync;

  useEffect(() => {
    if (intervalMs <= 0) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      timer = setTimeout(() => {
        refetchAsyncRef.current().finally(() => {
          if (alive) schedule();
        });
      }, intervalMs);
    };
    schedule();
    return () => {
      alive = false;
      if (timer !== undefined) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps]);

  return core;
}
