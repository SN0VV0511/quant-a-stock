import { useCallback, useEffect, useRef, useState } from "react";

interface PollingState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  updatedAt: number | null;
  refresh: () => Promise<void>;
}

/**
 * 轻量轮询 Hook：保留最后一次成功数据，失败时只更新错误状态，避免页面闪烁。
 */
export function usePolling<T>(loader: () => Promise<T>, intervalMs: number, enabled = true): PollingState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const mountedRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const next = await loader();
      if (!mountedRef.current) {
        return;
      }
      setData(next);
      setError(null);
      setUpdatedAt(Date.now());
    } catch (err) {
      if (!mountedRef.current) {
        return;
      }
      setError(err instanceof Error ? err.message : "请求失败");
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  }, [loader]);

  useEffect(() => {
    mountedRef.current = true;
    if (!enabled) {
      setLoading(false);
      return undefined;
    }
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, intervalMs);
    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
    };
  }, [enabled, intervalMs, refresh]);

  return { data, error, loading, updatedAt, refresh };
}
