import { useState, useEffect, useCallback, useRef } from "react";

interface UseApiResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
): UseApiResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  // 请求序号：只有最新一次请求可写回 state，防止慢响应乱序覆盖新数据
  const seqRef = useRef(0);

  const refetch = useCallback(() => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);
    fetcherRef
      .current()
      .then((d) => {
        if (seq === seqRef.current) setData(d);
      })
      .catch((e: Error) => {
        if (seq === seqRef.current) setError(e.message);
      })
      .finally(() => {
        if (seq === seqRef.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, loading, error, refetch };
}

export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number = 30_000,
  deps: unknown[] = [],
): UseApiResult<T> {
  const result = useApi(fetcher, deps);

  useEffect(() => {
    if (intervalMs <= 0) return;
    const id = setInterval(result.refetch, intervalMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps]);

  return result;
}
