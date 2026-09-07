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
  const requestsRef = useRef(new Map<() => Promise<T>, Promise<T>>());
  const refreshRef = useRef<() => Promise<void>>(async () => undefined);
  const refresh = useCallback(() => refreshRef.current(), []);

  useEffect(() => {
    let active = enabled;
    let pending: Promise<void> | null = null;
    const load = (): Promise<void> => {
      if (!active) return Promise.resolve();
      if (pending) return pending;
      // 切换面板或轮询周期时复用同一查询的在途请求。
      const request = requestsRef.current.get(loader) ?? Promise.resolve().then(loader);
      requestsRef.current.set(loader, request);
      pending = (async () => {
        try {
          const next = await request;
          if (!active) return;
          setData(next);
          setError(null);
          setUpdatedAt(Date.now());
        } catch (err) {
          if (active) setError(err instanceof Error ? err.message : "请求失败");
        } finally {
          if (active) setLoading(false);
          pending = null;
          if (requestsRef.current.get(loader) === request) requestsRef.current.delete(loader);
        }
      })();
      return pending;
    };
    refreshRef.current = load;
    if (!enabled) {
      setLoading(false);
      return () => {
        active = false;
        refreshRef.current = async () => undefined;
      };
    }
    const refreshWhenVisible = () => { if (!document.hidden) void load(); };
    refreshWhenVisible();
    const timer = window.setInterval(refreshWhenVisible, intervalMs);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      active = false;
      refreshRef.current = async () => undefined;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [enabled, intervalMs, loader]);

  return { data, error, loading, updatedAt, refresh };
}
