import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export interface Resource<T> {
  data: T | undefined;
  error: ApiError | Error | undefined;
  loading: boolean;
  reload: () => void;
  setData: (d: T) => void;
}

/** Minimal data-fetching hook: re-runs when deps change, ignores stale responses. */
export function useResource<T>(fn: () => Promise<T>, deps: unknown[]): Resource<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<ApiError | Error>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  useEffect(() => {
    const my = ++seq.current;
    setLoading(true);
    fn().then(
      (d) => {
        if (my === seq.current) {
          setData(d);
          setError(undefined);
          setLoading(false);
        }
      },
      (e) => {
        if (my === seq.current) {
          setError(e);
          setLoading(false);
        }
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload, setData };
}

/** Wrap an action so the UI can show pending state and the structured error. */
export function useAction<A extends unknown[], R>(fn: (...a: A) => Promise<R>) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | Error>();
  const run = useCallback(
    async (...a: A): Promise<R | undefined> => {
      setPending(true);
      setError(undefined);
      try {
        return await fn(...a);
      } catch (e) {
        setError(e as Error);
        return undefined;
      } finally {
        setPending(false);
      }
    },
    [fn],
  );
  return { run, pending, error, clear: () => setError(undefined) };
}

/** Re-run `reload` every `ms` while `active` (e.g. while a job is queued or processing on a worker). */
export function usePolling(active: boolean, reload: () => void, ms = 2000): void {
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(reload, ms);
    return () => window.clearInterval(t);
  }, [active, reload, ms]);
}

export const RUNNING_JOB_STATES = ["queued", "processing"];
