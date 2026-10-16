import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, get } from '../api/client';

export interface Loadable<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
}

/** Fetch once (and on `deps` change); optional `refreshMs` re-fetches only while the tab is visible. */
export function useGet<T>(path: string | null, deps: unknown[] = [], refreshMs?: number): Loadable<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path);
  const alive = useRef(true);

  const reload = useCallback(async () => {
    if (!path) return;
    try {
      const d = await get<T>(path);
      if (alive.current) {
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (alive.current) setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    alive.current = true;
    setLoading(true);
    void reload();
    let timer: ReturnType<typeof setInterval> | undefined;
    if (refreshMs) {
      timer = setInterval(() => {
        if (document.visibilityState === 'visible') void reload();
      }, refreshMs);
    }
    return () => {
      alive.current = false;
      if (timer) clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, reload, refreshMs, ...deps]);

  return { data, error, loading, reload };
}
