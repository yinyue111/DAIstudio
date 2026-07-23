"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { normalizeReverseOperationList } from "../lib/reverseOperations";

export default function useRecentReverseOperations({ enabled = true, limit = 5 } = {}) {
  const [operations, setOperations] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestRef = useRef(0);

  const refresh = useCallback(async () => {
    if (!enabled) return [];
    const requestId = ++requestRef.current;
    setLoading(true);
    try {
      const rows = normalizeReverseOperationList(await api.reverseOperations({ limit, offset: 0 }));
      if (requestId === requestRef.current) {
        setOperations(rows);
        setError("");
      }
      return rows;
    } catch (caught) {
      if (requestId === requestRef.current) {
        setError(caught?.message || "最近反推加载失败");
      }
      return [];
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, [enabled, limit]);

  useEffect(() => {
    refresh();
    return () => {
      requestRef.current += 1;
    };
  }, [refresh]);

  const upsert = useCallback((operation) => {
    if (!operation?.id) return;
    setOperations((current) => [
      operation,
      ...current.filter((row) => String(row.id) !== String(operation.id)),
    ].slice(0, limit));
  }, [limit]);

  return { operations, loading, error, refresh, upsert };
}
