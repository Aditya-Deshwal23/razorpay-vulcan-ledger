"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { BatchSummary } from "@/types/api";

export function useBatches() {
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const hasData = useRef(false);

  const refresh = useCallback(async () => {
    if (!hasData.current) setLoading(true);
    setError(null);
    try {
      const result = await api.batches();
      setBatches(result.items);
      hasData.current = true;
    } catch (caught) {
      setError(caught);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    const onBatchChange = () => { void refresh(); };
    window.addEventListener("vulcan:batch-state-changed", onBatchChange);
    return () => window.removeEventListener("vulcan:batch-state-changed", onBatchChange);
  }, [refresh]);

  return { batches, loading, error, refresh };
}

export function useResource<T>(load: () => Promise<T>, dependencies: React.DependencyList) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  const hasData = useRef(false);

  useEffect(() => {
    let current = true;
    if (!hasData.current) setLoading(true);
    setError(null);
    load()
      .then((value) => {
        if (current) {
          hasData.current = true;
          setData(value);
        }
      })
      .catch((caught) => { if (current) setError(caught); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
    // `dependencies` belongs to the callsite, just like React's useEffect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, revision]);

  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  return { data, loading, error, refresh };
}
